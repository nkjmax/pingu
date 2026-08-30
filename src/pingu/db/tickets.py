"""
Ticket numbering: CAT-YYYYMMDD-NN, resetting per CATEGORY (not globally)
at 00:00 SGT. NN is based on the count of ALL tickets ever created in
that category on that date -- resolved/cancelled ones still count, so the
sequence is monotonic and numbers are never reused within a day even if
an earlier ticket that day got cancelled.

ticket_number is UNIQUE in the schema as a safety net against the (very
unlikely, but not impossible) race of two tickets in the same category
landing in the same instant -- create_ticket retries a few times on a
collision rather than assuming it can never happen.
"""

import time
import aiosqlite
from datetime import datetime
from dateutil.tz import gettz
from . import connect

DEFAULT_TZ = "Asia/Singapore"  # same constant/library cogs/hosting.py uses, kept consistent

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS tickets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_number TEXT,
    user_id       INTEGER NOT NULL,
    category      TEXT    NOT NULL,
    subcategory   TEXT,
    ticket_type   TEXT    NOT NULL,
    body          TEXT    NOT NULL,
    channel_id    INTEGER,
    thread_id     INTEGER,
    status        TEXT    NOT NULL DEFAULT 'open',  -- open, resolved, cancelled
    created_at    INTEGER NOT NULL,
    closed_at     INTEGER,
    closed_by     INTEGER
);
"""

# SQLite's ALTER TABLE ADD COLUMN doesn't support inline UNIQUE, so this
# is a separate index instead -- same pattern matches.py already uses for
# its fresh-pug singleton constraint. SQLite treats each NULL as distinct
# in a unique index, so old rows from before this column existed (NULL
# ticket_number) don't collide with each other or with real values.
CREATE_TICKET_NUMBER_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_tickets_ticket_number ON tickets(ticket_number);
"""


def _sgt_date_str() -> str:
    return datetime.now(tz=gettz(DEFAULT_TZ)).strftime("%Y%m%d")


async def _next_ticket_number(category_code: str) -> str:
    prefix = f"{category_code}-{_sgt_date_str()}-"
    async with connect() as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM tickets WHERE ticket_number LIKE ?", (f"{prefix}%",)
        )
        row = await cur.fetchone()
        count = row[0] if row else 0
    return f"{prefix}{count + 1:02d}"


async def create_ticket(category_code: str, user_id, category, subcategory, ticket_type, body):
    """
    Returns (ticket_id, ticket_number).

    Also writes `type` (the legacy stub's column, still NOT NULL on
    anyone's already-created table from before this rewrite) with the
    same value as ticket_type -- ALTER TABLE ADD COLUMN can add new
    columns but can't relax a constraint on an old one, so the insert
    has to satisfy it regardless. type itself isn't read by anything
    going forward; ticket_type is the real field.
    """
    last_error = None
    for _ in range(3):
        ticket_number = await _next_ticket_number(category_code)
        try:
            async with connect() as db:
                cur = await db.execute(
                    "INSERT INTO tickets (ticket_number, user_id, category, subcategory, "
                    "ticket_type, type, body, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (ticket_number, user_id, category, subcategory, ticket_type, ticket_type, body, int(time.time())),
                )
                await db.commit()
                return cur.lastrowid, ticket_number
        except aiosqlite.IntegrityError as e:
            if "ticket_number" not in str(e):
                # Some other constraint failure (e.g. the legacy NOT NULL
                # bug this docstring mentions) -- retrying won't fix it,
                # so fail immediately and clearly instead of burning 3
                # attempts and hiding the real error behind this one.
                raise
            last_error = e
            continue
    raise RuntimeError(f"Could not generate a unique ticket number after 3 attempts: {last_error}")


async def set_channel_id(ticket_id, channel_id):
    async with connect() as db:
        await db.execute("UPDATE tickets SET channel_id = ? WHERE id = ?", (channel_id, ticket_id))
        await db.commit()


async def set_thread_id(ticket_id, thread_id):
    async with connect() as db:
        await db.execute("UPDATE tickets SET thread_id = ? WHERE id = ?", (thread_id, ticket_id))
        await db.commit()


async def get_ticket(ticket_id):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        return await cur.fetchone()


async def get_ticket_by_channel(channel_id):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM tickets WHERE channel_id = ?", (channel_id,))
        return await cur.fetchone()


async def get_open_tickets():
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM tickets WHERE status = 'open'")
        return await cur.fetchall()


async def close_ticket(ticket_id, status: str, closed_by):
    """status is 'resolved' or 'cancelled'."""
    async with connect() as db:
        await db.execute(
            "UPDATE tickets SET status = ?, closed_at = ?, closed_by = ? WHERE id = ?",
            (status, int(time.time()), closed_by, ticket_id),
        )
        await db.commit()