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
import logging
import aiosqlite

log = logging.getLogger("db")

DB_PATH = os.environ.get(
    "PINGU_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "matches.db"),
)

# New columns added on top of the original matches/signups schema, for the
# mix-request thread flow and archival features. Mirrors the original
# db.py's own migration pattern.
_NEW_MATCHES_COLUMNS = [
    ("captain_id", "INTEGER"),
    ("captain_role_id", "INTEGER"),
    ("category_id", "INTEGER"),
    ("host_request_id", "INTEGER"),
    ("team_split", "TEXT"),
    ("ongoing_delete_at", "INTEGER"),
    ("channel_slot", "INTEGER"),
]

# host_requests is a new table (not retrofitted from the original bot), but
# it's been live since earlier testing in this project -- new columns on it
# need the same migration treatment as matches, or existing rows/tables
# from before this column existed won't pick it up.
_NEW_HOST_REQUESTS_COLUMNS = [
    ("timestamp", "INTEGER"),
]

# Same story for signups -- captain_decision was added after this table
# was already live in earlier testing.
_NEW_SIGNUPS_COLUMNS = [
    ("captain_decision", "TEXT"),
]


def connect():
    """Returns an aiosqlite connection. Use as `async with`."""
    return aiosqlite.connect(DB_PATH)


async def init_db():
    from . import matches, signups, host_requests, players, match_logs, penalties, tickets

    log.info(f"Initializing database at: {DB_PATH}")
    log.info(f"DB file already exists: {os.path.exists(DB_PATH)}")

    async with connect() as db:
        db.row_factory = aiosqlite.Row
        await db.execute(matches.CREATE_TABLE)
        await db.execute(signups.CREATE_TABLE)

        try:
            await db.execute(matches.CREATE_FRESH_PUG_SINGLETON_INDEX)
        except Exception as e:
            # Most likely cause: pre-existing rows already violate the
            # constraint (e.g. two fresh pugs both stuck "active" from
            # earlier testing/bugs) -- log it loudly rather than let it
            # silently abort the whole transaction before commit(), which
            # would leave even `matches`/`signups` uncommitted.
            log.error(
                f"Failed to create fresh-pug singleton index: {e}. "
                f"Check for matches with status ended=0 and type IN "
                f"('fresh_pug','6s_fresh_pug') -- there should be at most one."
            )

        for col, definition in _NEW_MATCHES_COLUMNS:
            try:
                await db.execute(f"ALTER TABLE matches ADD COLUMN {col} {definition}")
            except Exception:
                pass  # column already exists -- fine, matches original bot's migration style

        for module in (host_requests, players, match_logs, penalties, tickets):
            await db.execute(module.CREATE_TABLE)

        for col, definition in _NEW_HOST_REQUESTS_COLUMNS:
            try:
                await db.execute(f"ALTER TABLE host_requests ADD COLUMN {col} {definition}")
            except Exception:
                pass

        for col, definition in _NEW_SIGNUPS_COLUMNS:
            try:
                await db.execute(f"ALTER TABLE signups ADD COLUMN {col} {definition}")
            except Exception:
                pass

        await db.commit()

        # Verify the critical tables actually exist post-commit, rather
        # than finding out five minutes later from an unrelated scheduler
        # job with a cryptic traceback.
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('matches', 'signups', 'host_requests')"
        )
        found = {row["name"] for row in await cur.fetchall()}
        expected = {"matches", "signups", "host_requests"}
        missing = expected - found
        if missing:
            raise RuntimeError(
                f"init_db() completed but these tables are still missing: {missing}. "
                f"DB_PATH={DB_PATH} -- check PINGU_DB_PATH in .env and file permissions "
                f"for that location."
            )
        log.info("Database initialized successfully -- matches/signups/host_requests confirmed present.")