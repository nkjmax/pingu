"""
Orchestrates match conclusion. Kept as an orchestrator on purpose — the
actual work (log matching, channel teardown, message formatting) lives in
log_service, channel_service, and embeds.py so each is independently
testable.
"""

import logging
import discord

from pingu import config
import pingu.db.matches as matches_db
import pingu.db.signups as signups_db
from pingu.services import log_service, channel_service
from pingu.embeds import build_archive_message

log = logging.getLogger("archive_service")


async def conclude(client: discord.Client, match_id: int, guild: discord.Guild):
    match = await matches_db.get_match(match_id)
    if not match:
        raise RuntimeError(f"archive_service.conclude: no such match #{match_id}")

    signups = await signups_db.get_accepted_signups(match_id)

    # Log lookup is best-effort — a failure here must never block archiving.
    matched_logs = await log_service.find_and_attach_logs(match)

    await matches_db.set_status(match_id, "concluded")

    if config.ARCHIVE_CHANNEL_ID:
        archive_ch = client.get_channel(config.ARCHIVE_CHANNEL_ID)
        if archive_ch:
            text = build_archive_message(match, signups, matched_logs)
            await archive_ch.send(text)

    await channel_service.teardown_match_channels(guild, match_id)


async def cancel(client: discord.Client, match_id: int, guild: discord.Guild):
    match = await matches_db.get_match(match_id)
    if not match:
        raise RuntimeError(f"archive_service.cancel: no such match #{match_id}")

    signups = await signups_db.get_signups_for_match(match_id)
    await matches_db.set_status(match_id, "cancelled")

    if config.ARCHIVE_CHANNEL_ID:
        archive_ch = client.get_channel(config.ARCHIVE_CHANNEL_ID)
        if archive_ch:
            text = build_archive_message(match, signups, matched_logs=[])
            await archive_ch.send(f"Cancelled\n{text}")

    await channel_service.teardown_match_channels(guild, match_id)
