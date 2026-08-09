import time
import aiosqlite
from . import connect

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS tickets (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL,
    type             TEXT    NOT NULL,   -- ban_report, player_report, suggestion
    body             TEXT,
    related_match_id INTEGER REFERENCES matches(id),
    status           TEXT    NOT NULL DEFAULT 'open',  -- open, resolved
    created_at       INTEGER NOT NULL
);
"""


async def create_ticket(user_id, ticket_type, body, related_match_id=None):
    async with connect() as db:
        cur = await db.execute(
            "INSERT INTO tickets (user_id, type, body, related_match_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, ticket_type, body, related_match_id, int(time.time())),
        )
        await db.commit()
        return cur.lastrowid


async def get_open_tickets():
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM tickets WHERE status = 'open'")
        return await cur.fetchall()


async def resolve_ticket(ticket_id):
    async with connect() as db:
        await db.execute("UPDATE tickets SET status = 'resolved' WHERE id = ?", (ticket_id,))
        await db.commit()
