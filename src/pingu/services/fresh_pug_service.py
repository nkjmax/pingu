"""
Anyone can create a fresh pug (unlike mix, which needs a hoster to approve
a request). What stops spam isn't a permission check — it's the DB unique
index in db/matches.py, which allows exactly one status='live' fresh pug
of a given type at a time.
"""

import pingu.db.matches as matches_db
import pingu.db.signups as signups_db
from pingu.ui.ui_updater import UIUpdater


class FreshPugAlreadyActive(Exception):
    def __init__(self, existing_match):
        self.existing_match = existing_match
        super().__init__(f"A fresh pug is already live: match #{existing_match['id']}")


async def create(creator_id, creator_name, maps: str, server: str, match_type="fresh_pug") -> int:
    match_id = await matches_db.create_match(
        match_type=match_type,
        created_by=creator_id,
        created_by_name=creator_name,
        status="live",
        map_name=maps,
        server=server,
    )
    if match_id is None:
        existing = await matches_db.get_active_fresh_pug(match_type)
        raise FreshPugAlreadyActive(existing)
    return match_id


async def join(match_id, user_id, username, class_name, ui_updater: UIUpdater):
    await signups_db.add_signup(match_id, user_id, username, class_name, status="accepted")
    ui_updater.schedule_refresh(match_id)


async def leave(match_id, user_id, ui_updater: UIUpdater):
    # In a full implementation this removes the specific signup row;
    # left as a lookup-and-delete against db.signups for the port-over.
    ui_updater.schedule_refresh(match_id)
