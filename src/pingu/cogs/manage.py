"""
/manage is hoster-only now. Captains use their own dedicated
/manage-signups command instead (see ManageSignupsCog below) -- keeping
the two separate means a captain never accidentally sees hoster-level
controls (conclude/cancel/split teams) and vice versa.

/manage branches:
  1. a pending, roster-filled mix-request thread -> hoster accept/deny
  2. a live match -> hoster panel:
       - fresh_pug/6s_fresh_pug -> FreshPugManageView
       - everything else -> ManageView (accept/deny, roster edit,
         conclude/cancel, split teams)
  3. anyone who isn't a hoster -> denied

cog_load re-registers persistent views for every active match on startup,
so buttons on old messages keep working across restarts.
"""

import discord
from discord import app_commands
from discord.ext import commands

from pingu import config
from pingu.db import matches as matches_db
from pingu.db import host_requests as requests_db
from pingu.views.roster_views import CaptainReviewView
from pingu.views.hosting_views import MixRequestReviewView


def is_hoster(interaction: discord.Interaction) -> bool:
    if not config.HOSTER_ROLE_ID:
        return True
    return any(r.id == config.HOSTER_ROLE_ID for r in interaction.user.roles)


class ManageCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        from pingu.views.signup_views import SixsSignupView, OPugSignupView, SignupView
        from pingu.views.fresh_pug_manage_views import FreshPugSignupView
        matches = await matches_db.get_all_active_matches()
        for match in matches:
            match_type = match["type"]
            if match_type in ("6s_mix", "6s_opug"):
                view = SixsSignupView(match["id"])
            elif match_type == "opug":
                view = OPugSignupView(match["id"])
            elif match_type in ("fresh_pug", "6s_fresh_pug"):
                view = FreshPugSignupView(match["id"])
            else:
                view = SignupView(match["id"])
            self.bot.add_view(view)

    @app_commands.command(name="manage", description="Open the hoster manage panel for the match or mix request in this channel.")
    async def manage(self, interaction: discord.Interaction):
        # Case 1: mix-request thread, ready for a hoster to review.
        if isinstance(interaction.channel, discord.Thread):
            request = await requests_db.get_request_by_thread(interaction.channel_id)
            if request and request["status"] == "pending":
                if not request["roster"]:
                    await interaction.response.send_message(
                        "Waiting on the requester to post their roster before this can be reviewed.",
                        ephemeral=True,
                    )
                    return
                if not is_hoster(interaction):
                    await interaction.response.send_message(
                        "Only hosters can accept or deny mix requests.", ephemeral=True
                    )
                    return
                view = MixRequestReviewView(request["id"])
                await interaction.response.send_message(
                    f"Review mix request #{request['id']}.", view=view, ephemeral=True
                )
                return

        match = await matches_db.get_match_by_channel(interaction.channel_id)
        if not match:
            await interaction.response.send_message("No active match found in this channel.", ephemeral=True)
            return

        if not is_hoster(interaction):
            hint = " Captains should use /manage-signups instead." if match["captain_id"] == interaction.user.id else ""
            await interaction.response.send_message(
                f"You need to be a hoster to use this.{hint}", ephemeral=True
            )
            return

        from pingu.views.manage_views import ManageView, build_manage_text
        from pingu.views.fresh_pug_manage_views import FreshPugManageView

        if match["type"] in ("fresh_pug", "6s_fresh_pug"):
            mode = "Fresh PUG 6v6" if match["type"] == "6s_fresh_pug" else "Fresh PUG"
            view = FreshPugManageView(match["id"])
            await interaction.response.send_message(
                f"**{mode}** \u2014 {match['division']} | <t:{match['timestamp']}:F>",
                view=view, ephemeral=True
            )
        else:
            text, _ = await build_manage_text(match["id"])
            view = await ManageView.create(match["id"])
            await interaction.response.send_message(text, view=view, ephemeral=True)


class ManageSignupsCog(commands.Cog):
    """Captain-only. Screens incoming signups; accepted players wait on
    hoster approval via /manage's captain-picks review."""

    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="manage-signups", description="Screen incoming signups for the mix you're captaining.")
    async def manage_signups(self, interaction: discord.Interaction):
        match = await matches_db.get_match_by_channel(interaction.channel_id)
        if not match:
            await interaction.response.send_message("No active match found in this channel.", ephemeral=True)
            return

        if match["captain_id"] != interaction.user.id:
            await interaction.response.send_message(
                "You need to be this match's captain to use this.", ephemeral=True
            )
            return

        view = await CaptainReviewView.create(match, self.bot.ui_updater)
        await interaction.response.send_message(
            "Review incoming signups -- accepted players wait on hoster approval.",
            view=view, ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(ManageCog(bot))
    await bot.add_cog(ManageSignupsCog(bot))