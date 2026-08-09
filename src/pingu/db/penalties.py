import time
import aiosqlite
from . import connect

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS penalties (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    type        TEXT    NOT NULL,   -- e.g. 'low_prio', 'ban'
    reason      TEXT,
    issued_by   INTEGER NOT NULL,
    issued_at   INTEGER NOT NULL,
    expires_at  INTEGER,            -- NULL = permanent
    active      INTEGER NOT NULL DEFAULT 1
);
"""


async def add_penalty(user_id, penalty_type, issued_by, reason=None, expires_at=None):
    async with connect() as db:
        cur = await db.execute(
            "INSERT INTO penalties (user_id, type, reason, issued_by, issued_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, penalty_type, reason, issued_by, int(time.time()), expires_at),
        )
        await db.commit()
        return cur.lastrowid


async def get_active_penalties(user_id):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM penalties WHERE user_id = ? AND active = 1", (user_id,)
        )
        return await cur.fetchall()


async def get_expired_active_penalties():
    now = int(time.time())
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM penalties WHERE active = 1 AND expires_at IS NOT NULL "
            "AND expires_at <= ?",
            (now,),
        )
        return await cur.fetchall()


async def deactivate(penalty_id):
    async with connect() as db:
        await db.execute("UPDATE penalties SET active = 0 WHERE id = ?", (penalty_id,))
        await db.commit()
