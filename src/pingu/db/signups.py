"""
Ported faithfully from the original bot's db.py (signups table + all
signup-level functions). Preserves every quirk on purpose:

- cancelled rows are tombstones, not deletions -- they exist so a
  captain/hoster undo can restore prior state (restore_cancelled_to_pending).
- accepted_at drives priority order (main roster before subs, LP sorted
  after non-LP -- LP sorting itself happens in the caller, which has
  guild/role context this module doesn't).
- swap_signup_order relies on autoincrement id order to re-prioritize --
  delete both rows, re-insert the one that should rank higher first.
"""

import time
import aiosqlite
from . import connect

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS signups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id    INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL,
    username    TEXT    NOT NULL,
    class_name  TEXT    NOT NULL,
    team        TEXT    NOT NULL DEFAULT 'mix',
    status      TEXT    DEFAULT 'pending',
    -- statuses: pending, accepted, denied, cancelled (tombstone),
    -- and the new 'awaiting_hoster' (captain-screened, pending hoster ok)
    accepted_at INTEGER
);
"""


async def add_signup(match_id, user_id, username, class_name, team="mix"):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, status FROM signups WHERE match_id=? AND user_id=? AND class_name=?",
            (match_id, user_id, class_name)
        ) as cur:
            existing = await cur.fetchone()
        if existing:
            if existing["status"] in ("pending", "accepted"):
                return None  # already actively signed up
            if existing["status"] == "cancelled":
                await db.execute("DELETE FROM signups WHERE id=?", (existing["id"],))
            # 'denied' rows: fall through and allow re-signup
        cur = await db.execute(
            "INSERT INTO signups (match_id, user_id, username, class_name, team) VALUES (?, ?, ?, ?, ?)",
            (match_id, user_id, username, class_name, team)
        )
        await db.commit()
        return cur.lastrowid


async def update_signup_status(signup_id, status):
    async with connect() as db:
        if status == "accepted":
            await db.execute(
                "UPDATE signups SET status=?, accepted_at=? WHERE id=?",
                (status, int(time.time()), signup_id)
            )
        else:
            await db.execute("UPDATE signups SET status=? WHERE id=?", (status, signup_id))
        await db.commit()


async def set_signup_status(signup_id, status):
    """Alias kept for the new roster_service call sites."""
    await update_signup_status(signup_id, status)


async def get_signups_for_match(match_id):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT *,
                  CASE WHEN status='accepted' THEN 0 ELSE 1 END AS sort_group
               FROM signups
               WHERE match_id=? AND status != 'cancelled'
               ORDER BY
                  sort_group ASC,
                  CASE WHEN status='accepted' THEN accepted_at END ASC NULLS LAST,
                  CASE WHEN status='accepted' THEN id END ASC,
                  CASE WHEN status!='accepted' THEN id END ASC""",
            (match_id,)
        ) as cur:
            return await cur.fetchall()


async def get_pending_signups(match_id):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM signups WHERE match_id=? AND status='pending' ORDER BY id ASC",
            (match_id,)
        ) as cur:
            return await cur.fetchall()


async def get_accepted_signups(match_id):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM signups WHERE match_id=? AND status='accepted' ORDER BY accepted_at ASC NULLS LAST, id ASC",
            (match_id,)
        ) as cur:
            return await cur.fetchall()


async def count_accepted_for_class(match_id, class_name):
    async with connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM signups WHERE match_id=? AND class_name=? AND status='accepted'",
            (match_id, class_name)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def count_accepted(match_id):
    async with connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM signups WHERE match_id=? AND status='accepted'",
            (match_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_next_accepted_for_class(match_id, class_name, exclude_user_id):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM signups
               WHERE match_id=? AND class_name=? AND status='accepted' AND user_id != ?
               ORDER BY accepted_at ASC NULLS LAST, id ASC LIMIT 1""",
            (match_id, class_name, exclude_user_id)
        ) as cur:
            return await cur.fetchone()


async def get_earliest_pending_for_class(match_id, class_name):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM signups
               WHERE match_id=? AND class_name=? AND status='pending'
               ORDER BY id ASC LIMIT 1""",
            (match_id, class_name)
        ) as cur:
            return await cur.fetchone()


async def get_signup_by_id(signup_id):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM signups WHERE id=?", (signup_id,)) as cur:
            return await cur.fetchone()


async def get_signup_by_user(match_id, user_id):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM signups WHERE match_id=? AND user_id=?", (match_id, user_id)
        ) as cur:
            return await cur.fetchone()


async def remove_signup(match_id, user_id, class_name=None):
    async with connect() as db:
        if class_name:
            await db.execute(
                "DELETE FROM signups WHERE match_id=? AND user_id=? AND class_name=?",
                (match_id, user_id, class_name)
            )
        else:
            await db.execute(
                "DELETE FROM signups WHERE match_id=? AND user_id=?", (match_id, user_id)
            )
        await db.commit()


async def get_non_denied_signups_for_user(match_id, user_id):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM signups
               WHERE match_id=? AND user_id=? AND status NOT IN ('denied', 'cancelled')
               ORDER BY id ASC""",
            (match_id, user_id)
        ) as cur:
            return await cur.fetchall()


async def get_accepted_matches_for_user(user_id, exclude_match_id=None, reference_timestamp=None):
    """
    Returns active matches where this user is accepted that clash with
    reference_timestamp -- a clash means the other match falls within
    [ref_ts - 5400, ref_ts + 5400] (1.5h window each side).
    """
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        if reference_timestamp is not None:
            window_start = reference_timestamp - 5400
            window_end = reference_timestamp + 5400
            async with db.execute(
                """SELECT m.* FROM matches m
                   JOIN signups s ON s.match_id = m.id
                   WHERE s.user_id = ? AND s.status = 'accepted'
                     AND m.ended = 0
                     AND (? IS NULL OR m.id != ?)
                     AND m.timestamp > ? AND m.timestamp < ?""",
                (user_id, exclude_match_id, exclude_match_id, window_start, window_end)
            ) as cur:
                return await cur.fetchall()
        else:
            async with db.execute(
                """SELECT m.* FROM matches m
                   JOIN signups s ON s.match_id = m.id
                   WHERE s.user_id = ? AND s.status = 'accepted'
                     AND m.ended = 0
                     AND (? IS NULL OR m.id != ?)""",
                (user_id, exclude_match_id, exclude_match_id)
            ) as cur:
                return await cur.fetchall()


async def get_signup_by_user_non_denied(match_id, user_id):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM signups WHERE match_id=? AND user_id=? AND status != 'denied'",
            (match_id, user_id)
        ) as cur:
            return await cur.fetchone()


async def get_signup_by_user_and_class(match_id, user_id, class_name):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM signups WHERE match_id=? AND user_id=? AND class_name=?",
            (match_id, user_id, class_name)
        ) as cur:
            return await cur.fetchone()


async def swap_signup_order(main_signup, sub_signup):
    """Delete both, re-insert sub first (lower id = higher priority), main second."""
    async with connect() as db:
        await db.execute("DELETE FROM signups WHERE id IN (?, ?)", (main_signup["id"], sub_signup["id"]))
        await db.execute(
            "INSERT INTO signups (match_id, user_id, username, class_name, team, status) VALUES (?,?,?,?,?,?)",
            (sub_signup["match_id"], sub_signup["user_id"], sub_signup["username"],
             sub_signup["class_name"], sub_signup["team"], sub_signup["status"])
        )
        await db.execute(
            "INSERT INTO signups (match_id, user_id, username, class_name, team, status) VALUES (?,?,?,?,?,?)",
            (main_signup["match_id"], main_signup["user_id"], main_signup["username"],
             main_signup["class_name"], main_signup["team"], main_signup["status"])
        )
        await db.commit()


async def count_unique_signedup_players(match_id):
    async with connect() as db:
        async with db.execute(
            "SELECT COUNT(DISTINCT user_id) FROM signups WHERE match_id=? AND status != 'denied'",
            (match_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_accepted_signups_for_class(match_id, class_name):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM signups
               WHERE match_id=? AND class_name=? AND status='accepted'
               ORDER BY accepted_at ASC NULLS LAST, id ASC""",
            (match_id, class_name)
        ) as cur:
            return await cur.fetchall()


async def get_accepted_signups_for_class_ordered(match_id, class_name):
    """Same as get_accepted_signups_for_class -- LP re-sort happens in the caller (guild context)."""
    return await get_accepted_signups_for_class(match_id, class_name)


async def get_accepted_signups_for_class_with_user(match_id, class_name, user_id):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM signups
               WHERE match_id=? AND class_name=? AND user_id=? AND status='accepted'""",
            (match_id, class_name, user_id)
        ) as cur:
            return await cur.fetchone()


async def remove_pending_slots_for_user(match_id, user_id, keep_class):
    """Soft-delete pending signups on other classes when accepted on keep_class."""
    async with connect() as db:
        await db.execute(
            """UPDATE signups SET status='cancelled'
               WHERE match_id=? AND user_id=? AND class_name!=? AND status='pending'""",
            (match_id, user_id, keep_class)
        )
        await db.commit()


async def remove_sub_slots_for_user(match_id, user_id, keep_class):
    """Soft-delete other accepted sub signups when promoted to main roster on keep_class."""
    async with connect() as db:
        await db.execute(
            """UPDATE signups SET status='cancelled'
               WHERE match_id=? AND user_id=? AND class_name!=? AND status='accepted'""",
            (match_id, user_id, keep_class)
        )
        await db.commit()


async def restore_cancelled_to_pending(match_id, user_id):
    async with connect() as db:
        await db.execute(
            """UPDATE signups SET status='pending'
               WHERE match_id=? AND user_id=? AND status='cancelled'""",
            (match_id, user_id)
        )
        await db.commit()


async def set_accepted_at(signup_id, value):
    async with connect() as db:
        await db.execute("UPDATE signups SET accepted_at=? WHERE id=?", (value, signup_id))
        await db.commit()


async def batch_set_accepted_at(updates):
    """updates: list of (signup_id, value) tuples."""
    async with connect() as db:
        await db.executemany(
            "UPDATE signups SET accepted_at=? WHERE id=?",
            [(value, signup_id) for signup_id, value in updates]
        )
        await db.commit()


async def move_accepted_to_pending(signup_id):
    async with connect() as db:
        await db.execute(
            "UPDATE signups SET status='pending', accepted_at=NULL WHERE id=?",
            (signup_id,)
        )
        await db.commit()


async def get_match_by_id_for_user(user_id, match_id):
    """Get match only if user is accepted in it."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT m.* FROM matches m
               JOIN signups s ON s.match_id=m.id
               WHERE m.id=? AND s.user_id=? AND s.status='accepted'""",
            (match_id, user_id)
        ) as cur:
            return await cur.fetchone()


# --- new, additive: captain-screens / hoster-confirms lifecycle ---

async def get_signups_by_status(match_id, status):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM signups WHERE match_id = ? AND status = ?", (match_id, status)
        ) as cur:
            return await cur.fetchall()


async def set_all_status(match_id, from_status, to_status):
    """Bulk transition -- used by 'accept all' on the hoster's picks-review panel."""
    async with connect() as db:
        await db.execute(
            "UPDATE signups SET status = ? WHERE match_id = ? AND status = ?",
            (to_status, match_id, from_status),
        )
        await db.commit()
