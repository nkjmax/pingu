"""
/penalize is held off for now -- apply_penalty() and expire_penalties()
already exist in services/moderation_service.py and scheduler.py for
whenever it's ready. The message listener stays active on its own.
"""

import discord
from discord.ext import commands

from pingu import config
from pingu.services import moderation_service


class ModerationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not moderation_service.contains_flagged_content(message.content):
            return

        mod_log_channel = None
        if config.MOD_LOG_CHANNEL_ID:
            mod_log_channel = self.bot.get_channel(config.MOD_LOG_CHANNEL_ID)

        await moderation_service.handle_violation(message, mod_log_channel)


async def setup(bot):
    await bot.add_cog(ModerationCog(bot))
