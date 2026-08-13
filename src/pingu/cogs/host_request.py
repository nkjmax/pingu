"""
The one command non-hosters use. /host command itself (hoster direct
mix creation) is untouched and lives wherever it already does in the
existing bot -- not rebuilt here.
"""

import discord
from discord import app_commands
from discord.ext import commands

from pingu import config
import pingu.db.host_requests as requests_db
from pingu.views.hosting_views import HostRequestChoiceView
from pingu.cogs.hosting import parse_class_ordered_roster
from pingu.embeds import build_roster_icon_lines, SIXS_DIVISIONS


class HostRequestCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="host-request", description="Host a fresh PUG, or request a mix (hosters will review).")
    async def host_request(self, interaction: discord.Interaction):
        view = HostRequestChoiceView(self.bot)
        await interaction.response.send_message(
            "What would you like to do?", view=view, ephemeral=True
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.Thread):
            return
        if message.channel.parent_id != config.MIX_REQUESTS_CHANNEL_ID:
            return

        request = await requests_db.get_request_by_thread(message.channel.id)
        if not request or request["status"] != "pending" or request["roster"]:
            return  # not a request thread, already resolved, or roster already captured
        if message.author.id != request["requester_id"]:
            return  # only the requester's ping message counts

        # Same parsing hosters use for their own roster -- comma-separated,
        # positionally matched to class order when displayed.
        roster_str = parse_class_ordered_roster(message.content)
        if not roster_str:
            return

        await requests_db.set_roster(request["id"], roster_str)

        # Same treatment a hoster's roster message gets: delete it, replace
        # with the icon-formatted class-by-class display.
        try:
            await message.delete()
        except Exception:
            pass

        is_sixs = request["division"] in SIXS_DIVISIONS
        roster_display = build_roster_icon_lines(roster_str, is_sixs=is_sixs)

        await message.channel.send(
            f"**Roster for mix request #{request['id']}**\n"
            f"Team: {request['team_name']} | Division: {request['division']} | "
            f"Map: {request['map_name']} | Server: {request['server'] or 'no preference'}\n\n"
            f"{roster_display}"
        )

        if config.HOSTER_ROLE_ID:
            await message.channel.send(
                f"<@&{config.HOSTER_ROLE_ID}> a mix request is ready for review — "
                f"run `/manage` in this thread to accept or deny it."
            )


async def setup(bot):
    await bot.add_cog(HostRequestCog(bot))