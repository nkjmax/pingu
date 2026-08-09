import discord
from discord import app_commands
from discord.ext import commands

import pingu.db.tickets as tickets_db
from pingu.db.guild_settings import get_settings


class TicketsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="ticket", description="Submit a ban report, player report, or suggestion.")
    @app_commands.describe(ticket_type="ban_report, player_report, or suggestion", body="Details")
    async def ticket(self, interaction: discord.Interaction, ticket_type: str, body: str):
        ticket_id = await tickets_db.create_ticket(interaction.user.id, ticket_type, body)
        await interaction.response.send_message(f"Ticket #{ticket_id} submitted.", ephemeral=True)

        settings = await get_settings(interaction.guild_id)
        ticket_channel_id = settings.get("ticket_channel_id")
        if ticket_channel_id:
            ch = self.bot.get_channel(int(ticket_channel_id))
            if ch:
                await ch.send(
                    f"**New ticket #{ticket_id}** ({ticket_type}) from {interaction.user.mention}\n{body}"
                )


async def setup(bot):
    await bot.add_cog(TicketsCog(bot))
