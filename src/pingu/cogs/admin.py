import discord
from discord import app_commands
from discord.ext import commands

import pingu.db.guild_settings as settings_db

SETTABLE_KEYS = {
    "hoster_role", "hoster_queue_channel", "archive_channel",
    "mod_log_channel", "ticket_channel", "competitive_category", "verified_role",
}


class AdminCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="setup", description="Configure a channel or role for Pingu (admin only).")
    @app_commands.describe(key="Which setting to configure", channel="Channel, if applicable",
                            role="Role, if applicable")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup(self, interaction: discord.Interaction, key: str,
                     channel: discord.TextChannel = None, role: discord.Role = None):
        if key not in SETTABLE_KEYS:
            await interaction.response.send_message(
                f"Unknown setting. Valid keys: {', '.join(sorted(SETTABLE_KEYS))}", ephemeral=True
            )
            return

        value = (channel.id if channel else None) or (role.id if role else None)
        if value is None:
            await interaction.response.send_message("Provide a channel or role.", ephemeral=True)
            return

        column = f"{key}_id"
        await settings_db.set_setting(interaction.guild_id, column, value)
        await interaction.response.send_message(f"Set `{key}`.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(AdminCog(bot))
