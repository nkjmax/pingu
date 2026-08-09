"""
Replaces the earlier propose/approve-proposal design with something
simpler: captains screen incoming signups (accept -> limbo, deny ->
rejected), and hosters give the final word on anything in limbo,
including a bulk "accept all" for the picks a captain has already vetted.

    pending --(captain accepts)--> awaiting_hoster --(hoster accepts)--> accepted
    pending --(captain denies)---> declined
    awaiting_hoster --(hoster denies)--> declined
"""

import pingu.db.signups as signups_db
from pingu.ui.ui_updater import UIUpdater


class NotCaptain(Exception):
    pass


class NotHoster(Exception):
    pass


async def captain_accept_signup(signup_id, captain_id, match, ui_updater: UIUpdater):
    if match["captain_id"] != captain_id:
        raise NotCaptain(f"user {captain_id} is not captain of match #{match['id']}")
    await signups_db.set_signup_status(signup_id, "awaiting_hoster")
    ui_updater.schedule_refresh(match["id"])


async def captain_deny_signup(signup_id, captain_id, match, ui_updater: UIUpdater):
    if match["captain_id"] != captain_id:
        raise NotCaptain(f"user {captain_id} is not captain of match #{match['id']}")
    await signups_db.set_signup_status(signup_id, "declined")
    ui_updater.schedule_refresh(match["id"])


async def hoster_accept_pick(signup_id, match_id, ui_updater: UIUpdater):
    await signups_db.set_signup_status(signup_id, "accepted")
    ui_updater.schedule_refresh(match_id)


async def hoster_deny_pick(signup_id, match_id, ui_updater: UIUpdater):
    await signups_db.set_signup_status(signup_id, "declined")
    ui_updater.schedule_refresh(match_id)


async def hoster_accept_all(match_id, ui_updater: UIUpdater):
    await signups_db.set_all_status(match_id, "awaiting_hoster", "accepted")
    ui_updater.schedule_refresh(match_id)
