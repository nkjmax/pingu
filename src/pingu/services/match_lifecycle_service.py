"""
Split out of views/legacy.py (was one 2,343-line file covering everything
from signup buttons to archive orchestration -- this piece is specifically
match lifecycle: refresh, archive, conclude, cancel. do_conclude/do_cancel
are the single entry points every "match is ending" flow goes through
(mix/opug conclude, cancel, fresh pug conclude/cancel) -- channels/VCs/
captain role all torn down immediately after archiving completes, not on
a delay; only the ongoing-matches line persists a while after.
"""

import asyncio
import logging
import time

import discord

log = logging.getLogger("match_lifecycle_service")

from pingu.embeds import (
    build_mix_message, build_match_embed, build_pending_message, build_denied_message,
    build_archive_message, match_label,
)
from pingu import config
from pingu.db import matches as matches_db
from pingu.db import signups as signups_db
from pingu.services import channel_service


async def refresh_message(client, match_id):
    match   = await matches_db.get_match(match_id)
    signups = await signups_db.get_signups_for_match(match_id)
    if not match or not match["message_id"]:
        return
    try:
        channel = client.get_channel(match["channel_id"])
        if not channel:
            return
        msg = await channel.fetch_message(match["message_id"])
        pug_role_id = config.PUG_ROLE_ID
        if match["type"] == "mix":
            await msg.edit(content=build_mix_message(match, signups, pug_role_id=pug_role_id), embed=None)
        elif match["type"] == "opug":
            from pingu.embeds import build_opug_message
            await msg.edit(content=build_opug_message(match, signups, pug_role_id=pug_role_id), embed=None)
        elif match["type"] == "6s_mix":
            from pingu.embeds import build_6s_mix_message
            await msg.edit(content=build_6s_mix_message(match, signups, pug_role_id=pug_role_id), embed=None)
        elif match["type"] == "6s_opug":
            from pingu.embeds import build_6s_opug_message
            await msg.edit(content=build_6s_opug_message(match, signups, pug_role_id=pug_role_id), embed=None)
        else:
            await msg.edit(embed=build_match_embed(match, signups))
    except Exception as e:
        log.warning(
            f"refresh_message (main) failed for match #{match_id}: {e} "
            f"[stored channel_id={match['channel_id']}, stored message_id={match['message_id']}, "
            f"resolved_channel={getattr(channel, 'id', None)}/{getattr(channel, 'name', None)}]"
        )

    if match["type"] in ("mix", "6s_mix", "opug", "6s_opug"):
        channel = client.get_channel(match["channel_id"])
        if channel:
            pending_msg_id = match["pending_msg_id"]
            denied_msg_id = match["denied_msg_id"]

            if pending_msg_id:
                try:
                    pmsg = await channel.fetch_message(pending_msg_id)
                    await pmsg.edit(content=build_pending_message(match, signups))
                except Exception as e:
                    log.warning(f"refresh_message (pending) failed for match #{match_id}: {e}")
            if denied_msg_id:
                try:
                    dmsg = await channel.fetch_message(denied_msg_id)
                    await dmsg.edit(content=build_denied_message(match, signups))
                except Exception as e:
                    log.warning(f"refresh_message (denied) failed for match #{match_id}: {e}")

    try:
        from pingu.cogs.hosting import refresh_ongoing_line
        await refresh_ongoing_line(client, match_id)
    except Exception:
        pass

async def archive_thread_to_channel(client, match, archive_ch, archive_summary_msg, on_progress=None):
    """
    Fetch all messages from the match thread and re-post them as a new
    thread on the archive summary message. Raises on critical failures so
    callers can handle retries correctly.

    on_progress, if given, is an async callback (current, total) called at
    roughly 25%/50%/75%/100% of the copy loop specifically -- that's the one
    step whose duration actually scales with thread size, so it's the only
    part worth reporting granular progress on. Not called per-message,
    which would risk Discord's message-edit rate limit on a big thread.
    """
    if not match["thread_id"]:
        return

    thread = client.get_channel(match["thread_id"])
    if not thread:
        try:
            thread = await client.fetch_channel(match["thread_id"])
        except Exception:
            return

    messages = []
    try:
        async for msg in thread.history(limit=500, oldest_first=True):
            messages.append(msg)
    except Exception as e:
        raise RuntimeError(f"Failed to fetch thread history: {e}")

    if not messages:
        return

    total = len(messages)

    match_type = match["type"] if match["type"] else "mix"
    if match_type in ("opug", "6s_opug"):
        thread_log_name = f"{match['division'] or 'PUG'} PUG \u2014 thread log"
    elif match_type == "6s_mix":
        thread_log_name = f"{match['team_name'] or 'Mix'} vs Mix 6s \u2014 thread log"
    elif match_type in ("fresh_pug", "6s_fresh_pug"):
        thread_log_name = "Fresh PUG \u2014 thread log"
    else:
        thread_log_name = f"{match['team_name'] or 'Mix'} vs Mix \u2014 thread log"

    try:
        archive_thread = await archive_summary_msg.create_thread(name=thread_log_name)
    except Exception as e:
        raise RuntimeError(f"Failed to create archive thread: {e}")

    last_checkpoint = 0
    for i, msg in enumerate(messages, start=1):
        if not msg.content and not msg.embeds and not msg.attachments:
            if on_progress and total:
                checkpoint = int((i / total) * 4)
                if checkpoint > last_checkpoint:
                    last_checkpoint = checkpoint
                    try:
                        await on_progress(i, total)
                    except Exception:
                        pass
            continue
        author = msg.author.display_name
        ts     = discord.utils.format_dt(msg.created_at, style="t")
        content_lines = [f"**{author}** {ts}"]
        if msg.content:
            content_lines.append(msg.content)
        text = "\n".join(content_lines)

        while len(text) > 2000:
            await archive_thread.send(text[:2000])
            text = text[2000:]
        if text.strip():
            try:
                await archive_thread.send(text)
            except Exception:
                pass

        for embed in msg.embeds:
            try:
                await archive_thread.send(embed=embed)
            except Exception:
                pass

        if on_progress and total:
            checkpoint = int((i / total) * 4)
            if checkpoint > last_checkpoint:
                last_checkpoint = checkpoint
                try:
                    await on_progress(i, total)
                except Exception:
                    pass

async def do_archive(client, match_id, concluded: bool, opug_split=None, matched_logs=None, on_progress=None):
    """
    Shared archive logic for both conclude and cancel. Split into two
    phases so retries don't duplicate the summary message:
    - Phase 1: post summary (raises on failure)
    - Phase 2: post thread log and lock original (best effort, non-raising)

    matched_logs is new -- optional list from log_service, flows through
    to build_archive_message for the logs.tf score/damage/link section.
    """
    match   = await matches_db.get_match(match_id)
    signups = await signups_db.get_signups_for_match(match_id) if opug_split is None else opug_split

    if not config.ARCHIVE_CHANNEL_ID:
        raise RuntimeError("do_archive: no ARCHIVE_CHANNEL_ID in .env")

    archive_ch = client.get_channel(config.ARCHIVE_CHANNEL_ID)
    if not archive_ch:
        raise RuntimeError(f"do_archive: could not find archive channel {config.ARCHIVE_CHANNEL_ID}")

    status_line = "\U0001f3c1 Concluded" if concluded else "\u274c Cancelled"
    summary     = build_archive_message(match, signups, matched_logs=matched_logs)
    full_text   = f"{status_line}\n{summary}"

    archive_msg = await archive_ch.send(full_text)

    try:
        await archive_thread_to_channel(client, match, archive_ch, archive_msg, on_progress=on_progress)
    except Exception as e:
        log.warning(f"do_archive: thread log failed for match #{match_id}: {e}")

    if match["thread_id"]:
        try:
            thread = client.get_channel(match["thread_id"])
            if not thread:
                thread = await client.fetch_channel(match["thread_id"])
            if thread:
                await thread.edit(locked=True)
                await thread.edit(archived=True)
        except Exception as e:
            log.warning(f"do_archive: failed to lock/archive thread {match['thread_id']}: {e}")

def _render_bar(fraction: float, width: int = 10) -> str:
    filled = max(0, min(width, round(fraction * width)))
    return "\u2593" * filled + "\u2591" * (width - filled)


async def _get_thread_message_count(client, match):
    """Cheap upfront count so the 0% status message can show '(0/N
    messages)' immediately, matching the format every later checkpoint
    uses. Best-effort -- if the thread's gone or unfetchable, the bar
    just won't have a count until the real copy loop starts."""
    if not match["thread_id"]:
        return None
    thread = client.get_channel(match["thread_id"])
    if not thread:
        try:
            thread = await client.fetch_channel(match["thread_id"])
        except Exception:
            return None
    try:
        count = 0
        async for _ in thread.history(limit=500):
            count += 1
        return count
    except Exception:
        return None


def fire_archive_and_teardown(client, guild, match, concluded, opug_split=None,
                               hoster_channel_id=None, triggered_by=None):
    """
    Background task: archive (with retries + hoster-channel progress/
    completion messages), THEN tear down channels -- sequenced within ONE
    task so teardown can never race ahead of archiving and delete the
    thread do_archive still needs to read from. Fired via
    asyncio.create_task() so do_conclude/do_cancel return immediately --
    the hoster's own confirmation doesn't wait on any of this.

    The progress bar is tied specifically to copying the match thread's
    messages into the archive thread -- that's the one step whose
    duration actually scales with how big the thread got, so it's the
    only part worth showing granular movement on. Posting the summary and
    locking the thread are fast regardless of thread size, so they're
    just instant before/after states around the bar, not part of it.
    """
    match_id = match["id"]

    async def _run():
        label = match_label(match)
        ping = f"<@{triggered_by}> " if triggered_by else ""
        hoster_ch = client.get_channel(int(hoster_channel_id)) if hoster_channel_id else None

        total_hint = await _get_thread_message_count(client, match)
        status_msg = None
        if hoster_ch:
            try:
                if total_hint is not None:
                    status_msg = await hoster_ch.send(
                        f"\U0001f504 {ping}Archiving {label} thread... {_render_bar(0)} 0% (0/{total_hint} messages)"
                    )
                else:
                    status_msg = await hoster_ch.send(
                        f"\U0001f504 {ping}Archiving {label} thread... {_render_bar(0)} 0%"
                    )
            except Exception:
                pass

        async def on_progress(current, total):
            if not status_msg:
                return
            try:
                pct = round((current / total) * 100)
                await status_msg.edit(
                    content=f"\U0001f504 {ping}Archiving {label} thread... "
                            f"{_render_bar(current / total)} {pct}% ({current}/{total} messages)"
                )
            except Exception:
                pass

        matched_logs = None
        if concluded:
            try:
                from pingu.services import log_service
                matched_logs = await log_service.find_and_attach_logs(match)
            except Exception as e:
                log.warning(f"logs.tf lookup failed for match #{match_id}: {e}")

        archived_ok = False
        for attempt in range(1, 4):
            try:
                await do_archive(client, match_id, concluded=concluded, opug_split=opug_split,
                                  matched_logs=matched_logs, on_progress=on_progress)
                archived_ok = True
                break
            except Exception as e:
                log.warning(f"archive attempt {attempt}/3 failed for match #{match_id}: {e}")
                if attempt < 3:
                    await asyncio.sleep(3)

        from pingu.services import channel_service
        try:
            await channel_service.teardown_match_channels(guild, match_id)
        except Exception as e:
            log.warning(f"channel teardown failed for match #{match_id}: {e}")

        result_text = (
            f"\u2705 {ping}{label} thread archived." if archived_ok
            else f"\u274c {ping}Archiving failed for {label} (match #{match_id}). Please check logs."
        )
        if status_msg:
            try:
                await status_msg.edit(content=result_text)
                return
            except Exception:
                pass
        if hoster_ch:
            try:
                await hoster_ch.send(result_text)
            except Exception:
                pass

    asyncio.create_task(_run())


async def do_conclude(client, guild, match_id, triggered_by, opug_split=None):
    """
    Generic across every match type -- mix, opug, fresh pug, and their 6s
    variants. The ongoing-matches line is deleted immediately (the
    channel gets torn down in the background too, see below) -- the
    ongoing-matches channel should only ever reflect matches that are
    ACTUALLY currently active, nothing else. Player notification for a
    concluded/cancelled match is handled separately, not via this line.
    """
    match = await matches_db.get_match(match_id)
    if not match or match["ended"]:
        return False

    ongoing_channel_id = getattr(client, "ongoing_channel", None)
    if ongoing_channel_id and match["ongoing_msg_id"]:
        try:
            oc   = client.get_channel(ongoing_channel_id)
            omsg = await oc.fetch_message(match["ongoing_msg_id"])
            await omsg.delete()
        except Exception:
            pass
        await matches_db.clear_ongoing_msg(match_id)

    await matches_db.mark_ended(match_id, cancelled=False)

    fire_archive_and_teardown(client, guild, match, concluded=True, opug_split=opug_split,
                               hoster_channel_id=config.HOSTER_CHANNEL_ID, triggered_by=triggered_by)
    return True

async def _disable_live_messages(client, match):
    """
    No longer called from do_conclude/do_cancel -- the whole channel gets
    deleted (in the background, after archiving) which removes every
    message in it anyway. Left here in case a future flow needs to strip
    a match's buttons without deleting its channel.
    """
    channel = client.get_channel(match["channel_id"])
    if not channel:
        return

    if match["message_id"]:
        try:
            msg = await channel.fetch_message(match["message_id"])
            await msg.edit(view=None)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    for key in ("pending_msg_id", "denied_msg_id"):
        if match[key]:
            try:
                msg = await channel.fetch_message(match[key])
                await msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

async def do_cancel(client, guild, match_id):
    match = await matches_db.get_match(match_id)
    if not match or match["ended"]:
        return False

    ongoing_channel_id = getattr(client, "ongoing_channel", None)
    if ongoing_channel_id and match["ongoing_msg_id"]:
        try:
            oc   = client.get_channel(ongoing_channel_id)
            omsg = await oc.fetch_message(match["ongoing_msg_id"])
            await omsg.delete()
        except Exception:
            pass
        await matches_db.clear_ongoing_msg(match_id)

    await matches_db.mark_ended(match_id, cancelled=True)

    fire_archive_and_teardown(client, guild, match, concluded=False,
                               hoster_channel_id=config.HOSTER_CHANNEL_ID, triggered_by=match["created_by"])
    return True