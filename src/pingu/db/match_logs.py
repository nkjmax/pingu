import aiosqlite
from . import connect

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS match_logs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id       INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    logs_tf_log_id TEXT    NOT NULL,
    logs_tf_url    TEXT    NOT NULL,
    map_name       TEXT,
    score_red      INTEGER,
    score_blu      INTEGER,
    damage_red     INTEGER,
    damage_blu     INTEGER,
    confidence     REAL,          -- fraction of roster matched, for sanity-checking later
    added_by       TEXT           -- 'auto' or a discord user_id string, for the /addlog fallback
);
"""


async def add_log(match_id, logs_tf_log_id, logs_tf_url, map_name=None, score_red=None,
                   score_blu=None, damage_red=None, damage_blu=None, confidence=None,
                   added_by="auto"):
    async with connect() as db:
        cur = await db.execute(
            "INSERT INTO match_logs (match_id, logs_tf_log_id, logs_tf_url, map_name, "
            "score_red, score_blu, damage_red, damage_blu, confidence, added_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (match_id, logs_tf_log_id, logs_tf_url, map_name, score_red, score_blu,
             damage_red, damage_blu, confidence, added_by),
        )
        await db.commit()
        return cur.lastrowid


async def get_logs_for_match(match_id):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM match_logs WHERE match_id = ?", (match_id,))
        return await cur.fetchall()
