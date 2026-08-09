import time
import aiosqlite
from . import connect

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS roster_proposals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id        INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    captain_id      INTEGER NOT NULL,
    target_user_id  INTEGER NOT NULL,
    target_username TEXT    NOT NULL,
    class_name      TEXT    NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending, approved, rejected
    created_at      INTEGER NOT NULL
);
"""


async def create_proposal(match_id, captain_id, target_user_id, target_username, class_name):
    async with connect() as db:
        cur = await db.execute(
            "INSERT INTO roster_proposals (match_id, captain_id, target_user_id, "
            "target_username, class_name, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (match_id, captain_id, target_user_id, target_username, class_name, int(time.time())),
        )
        await db.commit()
        return cur.lastrowid


async def get_proposal(proposal_id):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM roster_proposals WHERE id = ?", (proposal_id,))
        return await cur.fetchone()


async def get_pending_proposals(match_id):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM roster_proposals WHERE match_id = ? AND status = 'pending'",
            (match_id,),
        )
        return await cur.fetchall()


async def set_status(proposal_id, status):
    async with connect() as db:
        await db.execute(
            "UPDATE roster_proposals SET status = ? WHERE id = ?", (status, proposal_id)
        )
        await db.commit()
