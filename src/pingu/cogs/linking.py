import re
import time
import discord
from discord import app_commands
from discord.ext import commands

import pingu.db.players as players_db
from pingu import config

PROFILE_RE = re.compile(r"logs\.tf/profile/(\d{17})")


class LinkingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="link-logs", description="Paste your logs.tf profile link to link your account.")
    @app_commands.describe(profile_url="Your logs.tf profile link, e.g. logs.tf/profile/7656119...")
    async def link_logs(self, interaction: discord.Interaction, profile_url: str):
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

        # A dedicated "logs linked" role, separate from PUG_ROLE_ID (which
        # is just the default ping role for match posts).
        role_note = ""
        if config.LOGS_LINKED_ROLE_ID:
            role = interaction.guild.get_role(config.LOGS_LINKED_ROLE_ID)
            if role:
                try:
                    await interaction.user.add_roles(role, reason="Linked logs.tf profile")
                except discord.HTTPException:
                    role_note = " (couldn't assign the logs-linked role -- check Pingu's role position/permissions)"

        await interaction.response.send_message(f"Linked. You're set for competitive matches.{role_note}", ephemeral=True)


    @app_commands.command(name="view-logs", description="Look up someone's linked logs.tf profile.")
    @app_commands.describe(user="Whose profile to look up (defaults to you)")
    async def view_logs(self, interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user
        player = await players_db.get_player(target.id)

        if not player or not player["logs_tf_profile"]:
            who = "You haven't" if target.id == interaction.user.id else f"{target.mention} hasn't"
            await interaction.response.send_message(
                f"{who} linked a logs.tf profile yet.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"{target.mention}'s logs.tf profile: {player['logs_tf_profile']}", ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(LinkingCog(bot))