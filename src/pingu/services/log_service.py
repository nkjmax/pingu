"""
Servers auto-upload to logs.tf after every round via the server owner's
plugin — Pingu never uploads anything. This service just finds which
already-uploaded logs most likely belong to a given match, by overlapping
the log's player list against the match's roster within a time window
starting at the match's start timestamp.

Scans from the drafted roster's SteamIDs (db.players), never the hoster —
the hoster is an admin role and may not have played at all.
"""

import time
import logging
import aiohttp

import pingu.db.players as players_db
import pingu.db.signups as signups_db
import pingu.db.match_logs as match_logs_db

log = logging.getLogger("log_service")

LOGSTF_API_BASE = "https://logs.tf/api/v1"
MATCH_WINDOW_SECONDS = 4 * 60 * 60   # generous — covers multi-map mixes and late conclude
OVERLAP_THRESHOLD = 0.6              # fraction of roster that must appear in a log
REQUEST_TIMEOUT = 8


async def find_and_attach_logs(match, session: aiohttp.ClientSession = None) -> list[dict]:
    """
    Looks up candidate logs for `match` (a db row from db.matches), scores
    them, saves qualifying ones to match_logs, and returns the list of
    saved rows (as dicts) for the caller to build a summary from. Never
    raises — a failed lookup just means no logs get attached, and the
    hoster can /addlog manually.
    """
    close_session = False
    if session is None:
        session = aiohttp.ClientSession()
        close_session = True

    try:
        roster = await signups_db.get_accepted_signups(match["id"])
        steamids = await players_db.get_steamids_for_users([r["user_id"] for r in roster])
        if not steamids:
            log.info(f"log_service: no linked steamids for match #{match['id']}, skipping")
            return []

        window_start = match["timestamp"]
        window_end = window_start + MATCH_WINDOW_SECONDS
        roster_size = len(roster)

        candidate_log_ids = set()
        # Search from a handful of players, not the whole roster — cheaper,
        # and any one of them appearing is enough to surface the log.
        for steamid in list(steamids.values())[:5]:
            ids = await _query_logs_by_player(session, steamid)
            candidate_log_ids.update(ids)

        log.info(f"log_service: {len(candidate_log_ids)} candidate logs found for match #{match['id']}")

        saved = []
        checked = 0
        for log_id in candidate_log_ids:
            details = await _fetch_log(session, log_id)
            if not details:
                continue
            checked += 1
            if not (window_start <= details["date"] <= window_end):
                log.info(
                    f"log_service: log {log_id} outside match window for #{match['id']} "
                    f"(log date={details['date']}, window={window_start}-{window_end})"
                )
                continue

            overlap = _roster_overlap(details["players"], set(steamids.values()))
            confidence = overlap / roster_size if roster_size else 0
            if confidence < OVERLAP_THRESHOLD:
                log.info(
                    f"log_service: log {log_id} overlap {confidence:.2f} "
                    f"(threshold {OVERLAP_THRESHOLD}) below bar for match #{match['id']}"
                )
                continue

            row_id = await match_logs_db.add_log(
                match_id=match["id"],
                logs_tf_log_id=str(log_id),
                logs_tf_url=f"https://logs.tf/{log_id}",
                map_name=details.get("map_name"),
                score_red=details.get("score_red"),
                score_blu=details.get("score_blu"),
                damage_red=details.get("damage_red"),
                damage_blu=details.get("damage_blu"),
                confidence=confidence,
                added_by="auto",
            )
            saved.append({"id": row_id, "logs_tf_url": f"https://logs.tf/{log_id}", **details})

        if not saved:
            log.info(
                f"log_service: {len(candidate_log_ids)} candidate logs found, "
                f"{checked} fetched successfully, 0 qualified for match #{match['id']}"
            )

        return saved
    except Exception as e:
        log.warning(f"log_service: lookup failed for match #{match['id']}: {e}")
        return []
    finally:
        if close_session:
            await session.close()


async def _query_logs_by_player(session, steamid64) -> list[int]:
    """Returns recent logs.tf log IDs a given SteamID64 appears in."""
    try:
        async with session.get(
            f"{LOGSTF_API_BASE}/log",
            params={"player": steamid64, "limit": 20},
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            return [entry["id"] for entry in data.get("logs", [])]
    except Exception as e:
        log.warning(f"log_service: player query failed for {steamid64}: {e}")
        return []


async def _fetch_log(session, log_id) -> dict | None:
    """Fetches a single log and extracts the fields we care about."""
    try:
        async with session.get(f"https://logs.tf/json/{log_id}", timeout=REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception as e:
        log.warning(f"log_service: fetch failed for log {log_id}: {e}")
        return None

    players = data.get("players", {})
    teams = data.get("teams", {})

    damage_red = teams.get("Red", {}).get("dmg")
    damage_blu = teams.get("Blue", {}).get("dmg")
    score_red = teams.get("Red", {}).get("score")
    score_blu = teams.get("Blue", {}).get("score")

    return {
        "date": data.get("info", {}).get("date", int(time.time())),
        "map_name": data.get("info", {}).get("map"),
        "score_red": score_red,
        "score_blu": score_blu,
        "damage_red": damage_red,
        "damage_blu": damage_blu,
        "players": set(players.keys()),  # steamids as they key the players dict
    }


def _roster_overlap(log_players: set, roster_steamids: set) -> int:
    # log_players' keys always come from JSON (always strings). roster_steamids
    # may be ints if that DB column is stored as INTEGER -- normalizing both
    # to strings here means the intersection works regardless of which type
    # the DB actually uses, rather than silently returning zero overlap on
    # a type mismatch even when the actual players match perfectly.
    return len({str(p) for p in log_players} & {str(s) for s in roster_steamids})