"""
Ported from the original bot's scheduler.py -- clean_cancel_notices and
clean_conclude_notices were removed entirely: they only served the old
in-channel-notice mechanism (cancel_msg_id/conclude_msg_id), which stopped
being used once do_conclude/do_cancel switched to immediate channel
teardown + immediate ongoing-line deletion (see match_lifecycle_service).
send_1h_reminders, send_8h_reminders, re_sort are still the originals.
New jobs (expire_penalties, close_expired_request_threads) added
alongside, for the mix-request thread flow and moderation features.
"""

import logging
import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from pingu import config
from pingu.db import matches as matches_db
from pingu.db import signups as signups_db
from pingu.db import host_requests as requests_db
from pingu.embeds import build_ongoing_line, TF2_CLASSES, SIXS_CLASSES
from pingu.services import moderation_service

log = logging.getLogger("scheduler")

TWENTY_FOUR_HOURS = 24 * 3600


def start_scheduler(bot):
    scheduler = AsyncIOScheduler()

    # ── 1-hour reminder: ping roster in the match channel ───────────────────
    async def send_1h_reminders():
        matches = await matches_db.get_matches_needing_1h_reminder()
        for match in matches:
            accepted = await signups_db.get_accepted_signups(match["id"])
            match_type = match["type"]
            class_order = SIXS_CLASSES if match_type in ("6s_mix", "6s_opug") else TF2_CLASSES

            pings = []
            seen_users = set()

            if match_type in ("mix", "6s_mix"):
                first_per_class = {}
                for s in accepted:
                    if s["class_name"] not in first_per_class:
                        first_per_class[s["class_name"]] = s
                for cls in class_order:
                    s = first_per_class.get(cls)
                    if s and s["user_id"] not in seen_users:
                        seen_users.add(s["user_id"])
                        pings.append(f"<@{s['user_id']}>")
            elif match_type in ("opug", "6s_opug"):
                by_class = {cls: [] for cls in class_order}
                for s in accepted:
                    if s["class_name"] in by_class:
                        by_class[s["class_name"]].append(s)
                for cls in class_order:
                    for s in by_class[cls]:
                        if s["user_id"] not in seen_users:
                            seen_users.add(s["user_id"])
                            pings.append(f"<@{s['user_id']}>")
            elif match_type in ("fresh_pug", "6s_fresh_pug"):
                for s in accepted:
                    if s["user_id"] not in seen_users:
                        seen_users.add(s["user_id"])
                        pings.append(f"<@{s['user_id']}>")

            if not pings:
                await matches_db.mark_reminded(match["id"], "1h")
                continue

            channel = bot.get_channel(match["channel_id"])
            if channel:
                try:
                    if match_type in ("opug", "6s_opug"):
                        match_label = f"{match['division'] or 'PUG'} PUG"
                    elif match_type == "6s_mix":
                        match_label = f"{match['team_name'] or 'Mix'} vs Mix 6s"
                    elif match_type in ("fresh_pug", "6s_fresh_pug"):
                        match_label = "Fresh PUG" if match_type == "fresh_pug" else "Fresh PUG 6v6"
                    else:
                        match_label = f"{match['team_name'] or 'Mix'} vs Mix"
                    await channel.send(
                        f"\u23f0 **1 hour reminder!** {' '.join(pings)}\n"
                        f"**{match_label}** starts <t:{match['timestamp']}:R>. Get ready!"
                    )
                except Exception as e:
                    log.warning(f"Could not send 1h reminder for match #{match['id']}: {e}")

            await matches_db.mark_reminded(match["id"], "1h")

    # ── 8-hour host reminder: ping the hoster channel to conclude ───────────
    async def send_8h_reminders():
        matches = await matches_db.get_matches_needing_8h_reminder()
        for match in matches:
            if config.HOSTER_CHANNEL_ID:
                hoster_ch = bot.get_channel(config.HOSTER_CHANNEL_ID)
                if hoster_ch:
                    match_type = match["type"]
                    if match_type in ("opug", "6s_opug"):
                        match_label = f"{match['division'] or 'PUG'} PUG"
                    elif match_type == "6s_mix":
                        match_label = f"{match['team_name'] or 'Mix'} vs Mix 6s"
                    else:
                        match_label = f"{match['team_name'] or 'Mix'} vs Mix"
                    # SlimManageView is part of the original views.py port, not yet landed.
                    from pingu.views.manage_views import SlimManageView
                    view = SlimManageView(match["id"])
                    await hoster_ch.send(
                        f"<@{match['created_by']}> \u23f0 It's been 8 hours since "
                        f"<#{match['channel_id']}> ({match_label}) started. "
                        f"If the match is over, please conclude it.",
                        view=view,
                    )
            await matches_db.mark_reminded(match["id"], "8h")

    # ── Re-sort #ongoing-matches ─────────────────────────────────────────────
    async def re_sort():
        if not config.RE_SORT_ENABLED:
            return
        ongoing_channel = bot.get_channel(bot.ongoing_channel)
        if not ongoing_channel:
            return
        matches = await matches_db.get_all_active_matches()
        for match in matches:
            if match["ongoing_msg_id"]:
                try:
                    old = await ongoing_channel.fetch_message(match["ongoing_msg_id"])
                    await old.delete()
                except (discord.NotFound, discord.HTTPException):
                    pass
            signups = await signups_db.get_signups_for_match(match["id"])
            line = build_ongoing_line(
                match,
                channel_id=match["channel_id"],
                signups=signups if match["type"] in ("mix", "opug", "6s_mix", "6s_opug") else None,
            )
            new_msg = await ongoing_channel.send(line)
            await matches_db.set_ongoing_msg_id(match["id"], new_msg.id)

    # ── New: penalty expiry sweep ────────────────────────────────────────────
    async def expire_penalties():
        for guild in bot.guilds:
            await moderation_service.expire_penalties(guild, role_ids={
                "low_prio": config.LOW_PRIO_ROLE_ID,
                "mix_ban": config.MIX_BAN_ROLE_ID,
            })

    # ── New: close mix-request threads 24h after resolution ─────────────────
    async def close_expired_request_threads():
        requests = await requests_db.get_threads_needing_close(TWENTY_FOUR_HOURS)
        for request in requests:
            if request["thread_id"]:
                thread = bot.get_channel(request["thread_id"])
                if thread:
                    try:
                        await thread.delete()
                    except Exception as e:
                        log.warning(f"Failed to delete thread for request #{request['id']}: {e}")
            await requests_db.mark_thread_closed(request["id"])

    scheduler.add_job(send_1h_reminders, "interval", minutes=2, id="remind_1h")
    scheduler.add_job(send_8h_reminders, "interval", minutes=10, id="remind_8h")
    scheduler.add_job(expire_penalties, "interval", minutes=5, id="expire_penalties")
    scheduler.add_job(close_expired_request_threads, "interval", minutes=30, id="close_expired_threads")

    if config.RE_SORT_ENABLED:
        scheduler.add_job(
            re_sort, "interval",
            minutes=config.RE_SORT_INTERVAL_MINUTES,
            id="re_sort",
        )

    scheduler.start()
    log.info("Scheduler started.")