"""
Central DB entrypoint. DB_PATH keeps the original bot's filename
("matches.db") on purpose -- copy your existing matches.db into this
project's root and match history carries straight over. New tables use
CREATE TABLE IF NOT EXISTS (safe either way); new *columns* on the
original matches/signups tables need explicit ALTER TABLE migrations,
same pattern the original db.py used, since CREATE TABLE IF NOT EXISTS
is a no-op against an existing table and won't add the new columns.
"""

import os
import aiosqlite

DB_PATH = os.environ.get(
    "PINGU_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "matches.db"),
)

# New columns added on top of the original matches/signups schema, for the
# mix-request thread flow and archival features. Mirrors the original
# db.py's own migration pattern.
_NEW_MATCHES_COLUMNS = [
    ("captain_id", "INTEGER"),
    ("category_id", "INTEGER"),
    ("host_request_id", "INTEGER"),
    ("team_split", "TEXT"),
]


def connect():
    """Returns an aiosqlite connection. Use as `async with`."""
    return aiosqlite.connect(DB_PATH)


async def init_db():
    from . import matches, signups, host_requests, players, match_logs, penalties, tickets

    async with connect() as db:
        db.row_factory = aiosqlite.Row
        await db.execute(matches.CREATE_TABLE)
        await db.execute(signups.CREATE_TABLE)
        await db.execute(matches.CREATE_FRESH_PUG_SINGLETON_INDEX)

        for col, definition in _NEW_MATCHES_COLUMNS:
            try:
                await db.execute(f"ALTER TABLE matches ADD COLUMN {col} {definition}")
            except Exception:
                pass  # column already exists -- fine, matches original bot's migration style

        for module in (host_requests, players, match_logs, penalties, tickets):
            await db.execute(module.CREATE_TABLE)

        await db.commit()
