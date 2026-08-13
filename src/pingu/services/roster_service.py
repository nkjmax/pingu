"""
Captains PROPOSE (accept or deny), hosters COMMIT. A captain's decision is
stored on signups.captain_decision -- status stays 'pending' the whole
time, so a proposed-accept or proposed-deny signup stays visibly in the
public pending list until a hoster actually finalises it. Only the
hoster's action flips status to accepted/denied and clears the proposal.

    pending (captain_decision=NULL)
        --(captain proposes accept)--> pending (captain_decision='accept')
        --(captain proposes deny)----> pending (captain_decision='deny')
    pending (captain_decision set)
        --(hoster commits)--> accepted/denied (captain_decision cleared)

A player can have AT MOST ONE proposed accept at a time -- proposing
accept on one class clears their other still-undecided signups (same as
a hoster's own direct accept does), but never touches a class the captain
has already proposed to DENY; that stays on record for the hoster to see
and commit separately. So one player can carry one accept proposal plus
several deny proposals simultaneously.

Captain-side functions deliberately do NOT trigger a public refresh --
nothing about the public signup list is meant to change until a hoster
actually commits a decision, so there's nothing for players to see yet.

finalise_accept/finalise_deny are the ONE place a signup actually gets
resolved -- called by a hoster's direct Accept/Deny buttons AND by the
per-player captain-picks review (commit_player_decisions), so every entry
point behaves identically: same cleanup, same LP reordering, same thread
ping, same single refresh.

is_lp/reorder_class_roster live here rather than in views/legacy.py
because they're genuinely roster business logic, not view/display code --
legacy.py imports them back from here.
"""

import asyncio
import re
import time

import pingu.db.matches as matches_db
import pingu.db.signups as signups_db
from pingu import config
from pingu.ui.ui_updater import UIUpdater


class NotCaptain(Exception):
    pass


class NotHoster(Exception):
    pass


_MENTION_RE = re.compile(r"<@!?(\d+)>")


def host_roster_user_ids(host_roster_text: str) -> set:
    """
    Parses @mentions out of a match's host_roster text. This is the ONLY
    reliably-detectable subset -- host rosters are mostly typed as plain
    nicknames ("sol", "POOTIS"), which have no structured link back to a
    real Discord identity and can't be matched safely. A signup check
    against this can only ever catch @mention entries; plain-nickname
    ones are invisible to it by nature, not a bug.
    """
    if not host_roster_text:
        return set()
    return {int(uid) for uid in _MENTION_RE.findall(host_roster_text)}


async def is_lp(client, user_id):
    """Check if a user has the LP (Low Priority) role."""
    if not config.LOW_PRIO_ROLE_ID or not config.GUILD_ID:
        return False
    try:
        guild  = client.get_guild(config.GUILD_ID)
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        return any(r.id == config.LOW_PRIO_ROLE_ID for r in member.roles)
    except Exception:
        return False


async def reorder_class_roster(client, match_id, class_name):
    """
    Enforce priority order for a class roster:
    1. Non-LP players sorted by accepted_at ASC
    2. LP players sorted by accepted_at ASC (always after all non-LP)
    """
    accepted = await signups_db.get_accepted_signups_for_class(match_id, class_name)
    if len(accepted) < 2:
        return

    lp_results = await asyncio.gather(*[is_lp(client, s["user_id"]) for s in accepted])
    lp_flags   = {s["id"]: result for s, result in zip(accepted, lp_results)}

    sorted_order = sorted(
        accepted,
        key=lambda s: (lp_flags[s["id"]], s["accepted_at"] if s["accepted_at"] is not None else float("inf"))
    )

    if [s["id"] for s in sorted_order] == [s["id"] for s in accepted]:
        return

    base    = min((s["accepted_at"] for s in accepted if s["accepted_at"] is not None), default=int(time.time()))
    updates = [(s["id"], base + i) for i, s in enumerate(sorted_order)]
    await signups_db.batch_set_accepted_at(updates)


# ── Captain proposals -- no public refresh triggered ─────────────────────────

async def captain_propose_accept(signup_id, captain_id, match, ui_updater: UIUpdater = None):
    if match["captain_id"] != captain_id:
        raise NotCaptain(f"user {captain_id} is not captain of match #{match['id']}")

    current = await signups_db.get_signup_by_id(signup_id)
    if not current:
        return
    await signups_db.set_captain_decision(signup_id, "accept")

    # Only one class can be accept-proposed per player -- clear their
    # other still-undecided signups, but never a class already proposed
    # to deny (that stays on record for the hoster).
    await signups_db.remove_undecided_pending_slots_for_user(
        match["id"], current["user_id"], current["class_name"]
    )


async def captain_propose_deny(signup_id, captain_id, match, ui_updater: UIUpdater = None):
    if match["captain_id"] != captain_id:
        raise NotCaptain(f"user {captain_id} is not captain of match #{match['id']}")
    await signups_db.set_captain_decision(signup_id, "deny")


# Old names kept as aliases -- roster_views.CaptainReviewView's Accept/Deny
# buttons call these directly.
captain_accept_signup = captain_propose_accept
captain_deny_signup = captain_propose_deny


# ── Hoster finalisation -- the one place a signup gets resolved ──────────────

async def finalise_accept(client, match_id, signup_id):
    """
    Used by both a hoster's direct Accept button and the per-player
    captain-picks review. Marks the signup accepted, clears the player's
    other pending signups on different classes, clears sub-slots if this
    promotes them to the main roster, re-sorts the class for LP/priority
    order, pings them in the match thread, and schedules one debounced
    public refresh.

    Returns a dict describing what happened, or None if the signup no
    longer exists.
    """
    current = await signups_db.get_signup_by_id(signup_id)
    if not current:
        return None

    class_name = current["class_name"]
    user_id    = current["user_id"]
    already    = current["status"] == "accepted"

    match   = await matches_db.get_match(match_id)
    is_opug = match and match["type"] in ("opug", "6s_opug")
    filled  = await signups_db.count_accepted_for_class(match_id, class_name)

    await signups_db.finalise_signup(signup_id, "accepted")

    if is_opug:
        is_main_roster = filled < 2 and not already
    else:
        is_main_roster = filled == 0 and not already

    if is_main_roster:
        await signups_db.remove_sub_slots_for_user(match_id, user_id, class_name)
        await signups_db.remove_pending_slots_for_user(match_id, user_id, class_name)

    await reorder_class_roster(client, match_id, class_name)
    client.ui_updater.schedule_refresh(match_id)

    accepted_after = await signups_db.get_accepted_signups_for_class(match_id, class_name)
    if is_opug:
        main_uids = [s["user_id"] for s in accepted_after[:2]]
        on_main = user_id in main_uids
    else:
        on_main = len(accepted_after) > 0 and accepted_after[0]["user_id"] == user_id

    if match and match["thread_id"]:
        try:
            thread = client.get_channel(match["thread_id"])
            if thread:
                role_str = f"**{class_name}**" if on_main else f"**{class_name}** (sub)"
                await thread.send(f"<@{user_id}> you've been accepted as {role_str}! \u2705")
        except Exception:
            pass

    return {"class_name": class_name, "user_id": user_id, "on_main": on_main}


async def finalise_deny(client, match_id, signup_id):
    """
    Symmetric with finalise_accept, though simpler -- denying doesn't need
    accept's cleanup (there's nothing elsewhere to remove). Kept as its
    own function anyway so every entry point calls one place, not several.
    """
    current = await signups_db.get_signup_by_id(signup_id)
    if not current:
        return None
    await signups_db.finalise_signup(signup_id, "denied")
    client.ui_updater.schedule_refresh(match_id)
    return {"class_name": current["class_name"], "user_id": current["user_id"]}


# ── Per-player captain-picks commit ───────────────────────────────────────────

async def get_captain_decisions_by_player(match_id):
    """
    Groups pending, captain-decided signups by player -- what the hoster's
    'Review captain picks' panel lists, one entry per player rather than
    one per signup. Returns {user_id: {"username": str, "accept": row|None,
    "deny": [rows]}}.
    """
    rows = await signups_db.get_signups_with_captain_decision(match_id)
    by_player = {}
    for row in rows:
        uid = row["user_id"]
        if uid not in by_player:
            by_player[uid] = {"username": row["username"], "accept": None, "deny": []}
        if row["captain_decision"] == "accept":
            by_player[uid]["accept"] = row
        else:
            by_player[uid]["deny"].append(row)
    return by_player


async def commit_player_decisions(client, match_id, user_id, ui_updater: UIUpdater):
    """
    Commits everything a captain proposed for ONE player in one action --
    finalises their accept (if any) and every one of their deny proposals.
    This is the hoster's per-player "confirm" action.
    """
    by_player = await get_captain_decisions_by_player(match_id)
    entry = by_player.get(user_id)
    if not entry:
        return

    if entry["accept"]:
        await finalise_accept(client, match_id, entry["accept"]["id"])
    for row in entry["deny"]:
        await finalise_deny(client, match_id, row["id"])


async def commit_all_captain_decisions(client, match_id, ui_updater: UIUpdater):
    """
    'Approve all' -- commits every player's full proposed decision set
    (their accept AND their denies), not just a blanket bulk-accept.
    """
    by_player = await get_captain_decisions_by_player(match_id)
    for user_id in by_player:
        await commit_player_decisions(client, match_id, user_id, ui_updater)


async def reject_player_decisions(client, match_id, user_id, ui_updater: UIUpdater):
    """
    The hoster overrides everything the captain proposed for ONE player --
    rejects them across the board, regardless of whether the captain
    proposed accepting or denying any given class. Symmetric with
    commit_player_decisions, opposite outcome.
    """
    by_player = await get_captain_decisions_by_player(match_id)
    entry = by_player.get(user_id)
    if not entry:
        return

    rows = ([entry["accept"]] if entry["accept"] else []) + entry["deny"]
    for row in rows:
        await finalise_deny(client, match_id, row["id"])


async def reject_all_captain_decisions(client, match_id, ui_updater: UIUpdater):
    """'Reject all' -- rejects every player still awaiting review, overriding whatever their captain proposed."""
    by_player = await get_captain_decisions_by_player(match_id)
    for user_id in by_player:
        await reject_player_decisions(client, match_id, user_id, ui_updater)