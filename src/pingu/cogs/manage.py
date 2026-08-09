"""
/manage branches, in order:
  1. a pending, roster-filled mix-request thread -> hoster accept/deny (new)
  2. a live match where the caller is captain -> screen incoming signups (new)
  3. a live match where the caller is a hoster:
       - fresh_pug/6s_fresh_pug -> FreshPugManageView (original behavior)
       - everything else -> ManageView (original behavior, full panel:
         accept/deny individual signups, roster edit trigger, conclude/cancel)
  4. anyone else -> denied

cog_load re-registers persistent views for every active match on startup,
same as the original -- so buttons on old messages keep working across
restarts. SignupView/OPugSignupView/SixsSignupView/FreshPugSignupView/
FreshPugManageView/ManageView are the original views.py's -- not yet
ported (next piece), so this cog will run once that lands.
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
        from pingu.views.legacy import (
            SixsSignupView, OPugSignupView, FreshPugSignupView, SignupView,
        )
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

    @app_commands.command(name="manage", description="Open the manage panel for the match or mix request in this channel.")
    async def manage(self, interaction: discord.Interaction):
        # Case 1: mix-request thread, ready for a hoster to review (new).
        if isinstance(interaction.channel, discord.Thread):
            request = await requests_db.get_request_by_thread(interaction.channel_id)
            if request and request["status"] == "pending":
                if not request["roster"]:
                    await interaction.response.send_message(
                        "Waiting on the requester to ping their team before this can be reviewed.",
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

        # Case 2: captain screening incoming signups (new).
        if match["captain_id"] == interaction.user.id:
            view = await CaptainReviewView.create(match, self.bot.ui_updater)
            await interaction.response.send_message(
                "Review incoming signups -- accepted players wait on hoster approval.",
                view=view, ephemeral=True,
            )
            return

        # Case 3: hoster panel -- original behavior, branched by match type.
        if is_hoster(interaction):
            from pingu.views.legacy import ManageView, FreshPugManageView, build_manage_text

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
            return

        await interaction.response.send_message(
            "You need to be this match's captain or a hoster to use this.", ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(ManageCog(bot))
