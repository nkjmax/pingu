"""
Every service that changes match/roster/signup state calls
`ui_updater.schedule_refresh(match_id)` instead of editing Discord itself.

Multiple calls for the same match within the debounce window collapse into
one flush, so three captains accepting players in the same second produce
one combined edit instead of three-plus separate ones.
"""

import asyncio
import logging

log = logging.getLogger("ui_updater")

DEBOUNCE_SECONDS = 0.3


class UIUpdater:
    def __init__(self, bot):
        self.bot = bot
        self._pending: dict[int, asyncio.Task] = {}

    def schedule_refresh(self, match_id: int):
        existing = self._pending.get(match_id)
        if existing and not existing.done():
            # Already queued for this match — the debounce window will pick up
            # whatever the latest state is when it fires, nothing to do.
            return
        self._pending[match_id] = asyncio.create_task(self._debounced_flush(match_id))

    async def _debounced_flush(self, match_id: int):
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
            await self._flush(match_id)
        except Exception as e:
            log.warning(f"ui_updater flush failed for match #{match_id}: {e}")
        finally:
            self._pending.pop(match_id, None)

    async def _flush(self, match_id: int):
        """
        One combined refresh: main signup/roster embed, pending-proposals
        message, and the ongoing-matches line. Import locally to avoid
        circular imports between ui/ and embeds/services.
        """
        import pingu.db.matches as matches_db
        import pingu.db.signups as signups_db
        from pingu.embeds import build_match_embed, build_ongoing_line

        match = await matches_db.get_match(match_id)
        if not match:
            return
        signups = await signups_db.get_signups_for_match(match_id)

        if match["channel_id"]:
            channel = self.bot.get_channel(match["channel_id"])
            if channel and match["message_id"]:
                try:
                    msg = await channel.fetch_message(match["message_id"])
                    await msg.edit(embed=build_match_embed(match, signups))
                except Exception as e:
                    log.warning(f"ui_updater: main embed edit failed for match #{match_id}: {e}")

        ongoing_channel = getattr(self.bot, "ongoing_channel", None)
        if ongoing_channel and match["ongoing_msg_id"]:
            channel = self.bot.get_channel(ongoing_channel)
            if channel:
                try:
                    msg = await channel.fetch_message(match["ongoing_msg_id"])
                    await msg.edit(content=build_ongoing_line(match, signups=signups))
                except Exception as e:
                    log.warning(f"ui_updater: ongoing line edit failed for match #{match_id}: {e}")
