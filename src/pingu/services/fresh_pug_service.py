"""
Anyone can create a fresh pug (unlike mix, which needs a hoster to approve
a request). What stops spam isn't a permission check -- it's the DB unique
index in db/matches.py, which allows exactly one active (ended=0) fresh
pug of a given type at a time.

create() does the full flow -- DB row, channels, the actual joinable
message with sign-up buttons, thread, signup list, and an ongoing-matches
entry -- same as the /host wizard's fresh pug path (cogs/hosting.py), just
without a date/time field (this is a "happening now" quick-create, same
convention as a mix request with no scheduled time).
"""

import time

import pingu.db.matches as matches_db
from pingu.services import channel_service


class FreshPugAlreadyActive(Exception):
    def __init__(self, existing_match):
        self.existing_match = existing_match
        super().__init__(f"A fresh pug is already live: match #{existing_match['id']}")


async def create(bot, guild, creator_id, creator_name, maps: str, server: str,
                  match_type: str = "fresh_pug") -> int:
    existing = await matches_db.get_active_fresh_pug() if match_type == "fresh_pug" \
        else await matches_db.get_active_6s_fresh_pug()
    if existing:
        raise FreshPugAlreadyActive(existing)

    match_id = await matches_db.create_match(
        type_=match_type,
        timestamp=int(time.time()),
        created_by=creator_id,
        created_by_name=creator_name,
        division=None,
        map_name=maps or "tbc",
        server=server,
    )
    if match_id is None:
        # Lost the race against the DB's own singleton constraint.
        existing = await matches_db.get_active_fresh_pug() if match_type == "fresh_pug" \
            else await matches_db.get_active_6s_fresh_pug()
        raise FreshPugAlreadyActive(existing)

    result = await channel_service.create_match_channels(guild, match_id, match_type, creator_id=creator_id)
    if not result:
        return match_id  # match exists, but no category configured -- caller should warn
    channel_id, _ = result
    channel = bot.get_channel(channel_id)

    from pingu.embeds import build_fresh_pug_message, build_6s_fresh_pug_message, build_fresh_pug_signup_list
    from pingu.views.fresh_pug_manage_views import FreshPugSignupView
    from pingu.cogs.hosting import thread_date_str, post_to_ongoing

    match = await matches_db.get_match(match_id)
    if match_type == "6s_fresh_pug":
        content = build_6s_fresh_pug_message(match)
        thread_label = "FRESH PUG 6v6"
    else:
        content = build_fresh_pug_message(match)
        thread_label = "FRESH PUG"

    view = FreshPugSignupView(match_id)
    msg = await channel.send(content, view=view)
    await matches_db.set_message_id(match_id, msg.id, channel.id)

    signup_list_msg = await channel.send(content=build_fresh_pug_signup_list([]))
    await matches_db.set_signup_list_msg_id(match_id, signup_list_msg.id)

    try:
        thread = await msg.create_thread(
            name=f"{thread_label}, {thread_date_str(match['timestamp'])}",
            auto_archive_duration=1440,
        )
        await matches_db.set_thread_id(match_id, thread.id)
    except Exception:
        pass

    await post_to_ongoing(bot, match_id, channel.id)
    return match_id