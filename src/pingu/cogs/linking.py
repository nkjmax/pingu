import re
import time
import discord
from discord import app_commands
from discord.ext import commands

import pingu.db.players as players_db

PROFILE_RE = re.compile(r"logs\.tf/profile/(\d{17})")


class LinkingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="verify", description="Paste your logs.tf profile link to verify your account.")
    @app_commands.describe(profile_url="Your logs.tf profile link, e.g. logs.tf/profile/7656119...")
    async def verify(self, interaction: discord.Interaction, profile_url: str):
        match = PROFILE_RE.search(profile_url)
        if not match:
            await interaction.response.send_message(
                "That doesn't look like a logs.tf profile link. It should look like "
                "`logs.tf/profile/<steamid64>`.",
                ephemeral=True,
            )
            return

        steamid64 = match.group(1)
        await players_db.link_player(interaction.user.id, steamid64, profile_url, int(time.time()))
        await interaction.response.send_message("Linked. You're set for competitive matches.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(LinkingCog(bot))
