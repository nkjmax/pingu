"""
Ported faithfully from the original bot's db.py (matches table + all
match-level functions). Original used ended/cancelled int flags rather
than a status enum -- kept as-is rather than replaced, since embeds.py,
scheduler.py etc all key off match["ended"]. New columns added on top
(captain_id, category_id, host_request_id, team_split) for the mix-request
and archival features -- purely additive, nothing removed or renamed.
"""

import json
import time
import aiosqlite
from . import connect

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS matches (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    type                TEXT    NOT NULL,
    team_name           TEXT,
    timestamp           INTEGER NOT NULL,
    notes               TEXT,
    division            TEXT,
    map_name            TEXT,
    server              TEXT,
    pug_role_id         TEXT,
    host_roster         TEXT,
    ongoing_msg_id      INTEGER,
    message_id          INTEGER,
    channel_id          INTEGER,
    thread_id           INTEGER,
    created_by          INTEGER NOT NULL,
    created_by_name     TEXT    NOT NULL,
    ended               INTEGER DEFAULT 0,
    cancelled           INTEGER DEFAULT 0,
    cancel_msg_id       INTEGER,
    cancel_delete_at    INTEGER,
    conclude_msg_id     INTEGER,
    conclude_delete_at  INTEGER,
    reminded_1h         INTEGER DEFAULT 0,
    reminded_8h         INTEGER DEFAULT 0,
    teams_posted        INTEGER DEFAULT 0,
    pending_msg_id      INTEGER,
    denied_msg_id       INTEGER,
    ping_msg_id         INTEGER,
    signup_list_msg_id  INTEGER,
    roster_edit_msg_id  INTEGER,
    team_split          TEXT,
    -- new, additive: mix-request / archival features
    captain_id          INTEGER,
    category_id         INTEGER,
    host_request_id     INTEGER REFERENCES host_requests(id)
);
"""

# Fresh pug singleton: extra safety net on top of the original bot's
# check-then-insert pattern (still respected in fresh_pug_service).
CREATE_FRESH_PUG_SINGLETON_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_fresh_pug
ON matches(type)
WHERE ended = 0 AND type IN ('fresh_pug', '6s_fresh_pug');
"""


async def create_match(type_, timestamp, created_by, created_by_name,
                        team_name=None, notes=None, division=None,
                        map_name=None, server=None, pug_role_id=None,
                        captain_id=None, host_request_id=None):
    async with connect() as db:
        await db.execute(CREATE_FRESH_PUG_SINGLETON_INDEX)
        try:
            cur = await db.execute(
                """INSERT INTO matches
                   (type, team_name, timestamp, notes, division, map_name,
                    server, pug_role_id, created_by, created_by_name,
                    captain_id, host_request_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (type_, team_name, timestamp, notes, division, map_name,
                 server, pug_role_id, created_by, created_by_name,
                 captain_id, host_request_id)
            )
            await db.commit()
            return cur.lastrowid
        except aiosqlite.IntegrityError:
            return None  # fresh-pug singleton violated


async def get_match(match_id):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM matches WHERE id = ?", (match_id,)) as cur:
            return await cur.fetchone()


async def get_match_by_channel(channel_id):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM matches WHERE channel_id=? AND ended=0", (channel_id,)
        ) as cur:
            return await cur.fetchone()


async def update_match_fields(match_id, **fields):
    allowed = {"team_name", "timestamp", "division", "map_name", "server", "notes",
               "host_roster", "captain_id", "category_id"}
    filtered = {k: v for k, v in fields.items() if k in allowed}
    if not filtered:
        return
    set_clause = ", ".join(f"{k}=?" for k in filtered)
    values = list(filtered.values()) + [match_id]
    async with connect() as db:
        await db.execute(f"UPDATE matches SET {set_clause} WHERE id=?", values)
        await db.commit()


async def set_message_id(match_id, message_id, channel_id):
    async with connect() as db:
        await db.execute(
            "UPDATE matches SET message_id=?, channel_id=? WHERE id=?",
            (message_id, channel_id, match_id)
        )
        await db.commit()


async def set_thread_id(match_id, thread_id):
    async with connect() as db:
        await db.execute("UPDATE matches SET thread_id=? WHERE id=?", (thread_id, match_id))
        await db.commit()


async def set_ongoing_msg_id(match_id, ongoing_msg_id):
    async with connect() as db:
        await db.execute(
            "UPDATE matches SET ongoing_msg_id=? WHERE id=?", (ongoing_msg_id, match_id)
        )
        await db.commit()


async def set_teams_posted(match_id):
    async with connect() as db:
        await db.execute("UPDATE matches SET teams_posted=1 WHERE id=?", (match_id,))
        await db.commit()


async def set_pending_msg_id(match_id, msg_id):
    async with connect() as db:
        await db.execute("UPDATE matches SET pending_msg_id=? WHERE id=?", (msg_id, match_id))
        await db.commit()


async def set_denied_msg_id(match_id, msg_id):
    async with connect() as db:
        await db.execute("UPDATE matches SET denied_msg_id=? WHERE id=?", (msg_id, match_id))
        await db.commit()


async def set_ping_msg_id(match_id, msg_id):
    async with connect() as db:
        await db.execute("UPDATE matches SET ping_msg_id=? WHERE id=?", (msg_id, match_id))
        await db.commit()


async def set_signup_list_msg_id(match_id, msg_id):
    async with connect() as db:
        await db.execute("UPDATE matches SET signup_list_msg_id=? WHERE id=?", (msg_id, match_id))
        await db.commit()


async def set_roster_edit_msg_id(match_id, msg_id):
    async with connect() as db:
        await db.execute("UPDATE matches SET roster_edit_msg_id=? WHERE id=?", (msg_id, match_id))
        await db.commit()


async def end_match(match_id):
    async with connect() as db:
        await db.execute("UPDATE matches SET ended=1 WHERE id=?", (match_id,))
        await db.commit()


async def cancel_match(match_id, cancel_msg_id):
    delete_at = int(time.time()) + 86400
    async with connect() as db:
        await db.execute(
            "UPDATE matches SET ended=1, cancelled=1, cancel_msg_id=?, cancel_delete_at=? WHERE id=?",
            (cancel_msg_id, delete_at, match_id)
        )
        await db.commit()


async def mark_reminded(match_id, reminder_type):
    col = "reminded_1h" if reminder_type == "1h" else "reminded_8h"
    async with connect() as db:
        await db.execute(f"UPDATE matches SET {col}=1 WHERE id=?", (match_id,))
        await db.commit()


async def get_expired_cancel_notices():
    now = int(time.time())
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM matches
               WHERE cancelled=1 AND cancel_msg_id IS NOT NULL
                 AND cancel_delete_at IS NOT NULL AND cancel_delete_at < ?""",
            (now,)
        ) as cur:
            return await cur.fetchall()


async def clear_cancel_msg(match_id):
    async with connect() as db:
        await db.execute(
            "UPDATE matches SET cancel_msg_id=NULL, cancel_delete_at=NULL WHERE id=?",
            (match_id,)
        )
        await db.commit()


async def get_all_active_matches():
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM matches WHERE ended=0 AND message_id IS NOT NULL ORDER BY timestamp ASC"
        ) as cur:
            return await cur.fetchall()


async def get_active_channel_ids():
    async with connect() as db:
        async with db.execute(
            "SELECT channel_id FROM matches WHERE ended=0 AND message_id IS NOT NULL"
        ) as cur:
            rows = await cur.fetchall()
            return {row[0] for row in rows}


async def get_matches_needing_1h_reminder():
    now = int(time.time())
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM matches
               WHERE ended=0 AND reminded_1h=0 AND message_id IS NOT NULL
                 AND timestamp > ? AND timestamp <= ?""",
            (now, now + 3600)
        ) as cur:
            return await cur.fetchall()


async def get_matches_needing_8h_reminder():
    now = int(time.time())
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM matches
               WHERE ended=0 AND reminded_8h=0 AND message_id IS NOT NULL
                 AND timestamp <= ?""",
            (now - 8 * 3600,)
        ) as cur:
            return await cur.fetchall()


async def set_conclude_msg(match_id, msg_id, channel_id):
    delete_at = int(time.time()) + 86400
    async with connect() as db:
        await db.execute(
            "UPDATE matches SET conclude_msg_id=?, conclude_delete_at=? WHERE id=?",
            (msg_id, delete_at, match_id)
        )
        await db.commit()


async def clear_conclude_msg(match_id):
    async with connect() as db:
        await db.execute(
            "UPDATE matches SET conclude_msg_id=NULL, conclude_delete_at=NULL WHERE id=?",
            (match_id,)
        )
        await db.commit()


async def get_expired_conclude_notices():
    now = int(time.time())
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM matches
               WHERE ended=1 AND conclude_msg_id IS NOT NULL
                 AND conclude_delete_at IS NOT NULL AND conclude_delete_at < ?""",
            (now,)
        ) as cur:
            return await cur.fetchall()


async def get_conclude_msg_for_channel(channel_id):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM matches
               WHERE channel_id=? AND conclude_msg_id IS NOT NULL
               ORDER BY id DESC LIMIT 1""",
            (channel_id,)
        ) as cur:
            return await cur.fetchone()


async def get_active_fresh_pug():
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM matches WHERE type='fresh_pug' AND ended=0 LIMIT 1"
        ) as cur:
            return await cur.fetchone()


async def get_active_6s_fresh_pug():
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM matches WHERE type='6s_fresh_pug' AND ended=0 LIMIT 1"
        ) as cur:
            return await cur.fetchone()


async def save_team_split(match_id, red_user_ids, blu_user_ids):
    async with connect() as db:
        split_data = json.dumps({"red": red_user_ids, "blu": blu_user_ids})
        await db.execute("UPDATE matches SET team_split=? WHERE id=?", (split_data, match_id))
        await db.commit()


async def get_team_split(match_id):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT team_split FROM matches WHERE id=?", (match_id,)) as cur:
            row = await cur.fetchone()
            if row and row["team_split"]:
                return json.loads(row["team_split"])
    return None


# --- new, additive: captain / category / host_request lookups ---

async def set_captain(match_id, captain_id):
    async with connect() as db:
        await db.execute("UPDATE matches SET captain_id=? WHERE id=?", (captain_id, match_id))
        await db.commit()


async def set_category_id(match_id, category_id):
    async with connect() as db:
        await db.execute("UPDATE matches SET category_id=? WHERE id=?", (category_id, match_id))
        await db.commit()
