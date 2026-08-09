import aiosqlite
from . import connect

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id                  INTEGER PRIMARY KEY,
    hoster_role_id             INTEGER,
    hoster_queue_channel_id    INTEGER,
    archive_channel_id         INTEGER,
    mod_log_channel_id         INTEGER,
    ticket_channel_id          INTEGER,
    competitive_category_id    INTEGER,
    verified_role_id           INTEGER
);
"""


async def get_settings(guild_id):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,))
        row = await cur.fetchone()
        return dict(row) if row else {}


async def set_setting(guild_id, key, value):
    async with connect() as db:
        await db.execute(
            f"INSERT INTO guild_settings (guild_id, {key}) VALUES (?, ?) "
            f"ON CONFLICT(guild_id) DO UPDATE SET {key} = excluded.{key}",
            (guild_id, value),
        )
        await db.commit()
