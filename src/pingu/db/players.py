import aiosqlite
from . import connect

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS players (
    user_id          INTEGER PRIMARY KEY,
    steamid64        TEXT,
    logs_tf_profile  TEXT,
    linked_at        INTEGER
);
"""


async def link_player(user_id, steamid64, logs_tf_profile, linked_at):
    async with connect() as db:
        await db.execute(
            "INSERT INTO players (user_id, steamid64, logs_tf_profile, linked_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET steamid64=excluded.steamid64, "
            "logs_tf_profile=excluded.logs_tf_profile, linked_at=excluded.linked_at",
            (user_id, steamid64, logs_tf_profile, linked_at),
        )
        await db.commit()


async def get_player(user_id):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM players WHERE user_id = ?", (user_id,))
        return await cur.fetchone()


async def get_steamids_for_users(user_ids):
    """Returns {user_id: steamid64} for whichever of the given users are linked."""
    if not user_ids:
        return {}
    placeholders = ", ".join("?" for _ in user_ids)
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT user_id, steamid64 FROM players WHERE user_id IN ({placeholders}) "
            "AND steamid64 IS NOT NULL",
            list(user_ids),
        )
        rows = await cur.fetchall()
        return {row["user_id"]: row["steamid64"] for row in rows}
