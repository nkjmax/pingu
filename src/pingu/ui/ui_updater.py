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
        Delegates to the real, type-aware refresh functions in
        views/legacy.py -- refresh_message() already handles mix/opug/6s
        (main message + pending + denied + ongoing-line together), and
        fresh pug uses its own simpler signup-list refresh. No duplicate
        logic here; this just calls the one real implementation.
        """
        import pingu.db.matches as matches_db

        match = await matches_db.get_match(match_id)
        if not match:
            return

        from pingu.services.match_lifecycle_service import refresh_message
        from pingu.views.fresh_pug_manage_views import refresh_fresh_pug_signup_list

        try:
            if match["type"] in ("fresh_pug", "6s_fresh_pug"):
                await refresh_fresh_pug_signup_list(self.bot, match_id)
            else:
                await refresh_message(self.bot, match_id)
        except Exception as e:
            log.warning(f"ui_updater: refresh failed for match #{match_id}: {e}")