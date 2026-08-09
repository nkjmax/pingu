import time
import aiosqlite
from . import connect

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS host_requests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    requester_id   INTEGER NOT NULL,
    team_name      TEXT,
    division       TEXT,
    map_name       TEXT,
    server         TEXT,
    notes          TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',  -- pending, approved, denied
    hoster_id      INTEGER,
    thread_id      INTEGER,
    roster         TEXT,       -- space-separated user IDs pinged by the requester in-thread
    created_at     INTEGER NOT NULL,
    resolved_at    INTEGER,
    thread_closed  INTEGER DEFAULT 0
);
"""


async def create_request(requester_id, team_name, division, map_name, server, notes=None):
    async with connect() as db:
        cur = await db.execute(
            "INSERT INTO host_requests (requester_id, team_name, division, map_name, server, "
            "notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (requester_id, team_name, division, map_name, server, notes, int(time.time())),
        )
        await db.commit()
        return cur.lastrowid


async def get_request(request_id):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM host_requests WHERE id = ?", (request_id,))
        return await cur.fetchone()


async def get_pending_requests():
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM host_requests WHERE status = 'pending'")
        return await cur.fetchall()


async def set_status(request_id, status, hoster_id=None):
    async with connect() as db:
        await db.execute(
            "UPDATE host_requests SET status = ?, hoster_id = ?, resolved_at = ? WHERE id = ?",
            (status, hoster_id, int(time.time()), request_id),
        )
        await db.commit()


async def set_thread(request_id, thread_id):
    async with connect() as db:
        await db.execute(
            "UPDATE host_requests SET thread_id = ? WHERE id = ?", (thread_id, request_id)
        )
        await db.commit()


async def set_roster(request_id, roster: str):
    async with connect() as db:
        await db.execute(
            "UPDATE host_requests SET roster = ? WHERE id = ?", (roster, request_id)
        )
        await db.commit()


async def get_request_by_thread(thread_id):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM host_requests WHERE thread_id = ?", (thread_id,))
        return await cur.fetchone()


async def get_threads_needing_close(older_than_seconds):
    cutoff = int(time.time()) - older_than_seconds
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM host_requests WHERE resolved_at IS NOT NULL "
            "AND resolved_at <= ? AND thread_closed = 0",
            (cutoff,),
        )
        return await cur.fetchall()


async def mark_thread_closed(request_id):
    async with connect() as db:
        await db.execute(
            "UPDATE host_requests SET thread_closed = 1 WHERE id = ?", (request_id,)
        )
        await db.commit()
