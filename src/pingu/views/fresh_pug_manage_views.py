"""Split out of views/legacy.py -- fresh pug sign-up buttons and the
hoster's conclude/cancel confirmation for fresh pug specifically (no
notice-then-delay pattern like mix/opug -- see match_lifecycle_service's
do_conclude/do_cancel docstring)."""

import time
import logging
import discord
from discord import ui

log = logging.getLogger("fresh_pug_manage_views")

from pingu import config
from pingu.embeds import build_fresh_pug_signup_list
from pingu.db import matches as matches_db
from pingu.db import signups as signups_db
from pingu.services.match_lifecycle_service import do_conclude, do_cancel


async def refresh_fresh_pug_signup_list(client, match_id):
    """Edit the signup list message for a fresh pug to reflect current signups."""
    match = await matches_db.get_match(match_id)
    if not match:
        return
    signup_list_msg_id = match["signup_list_msg_id"]
    if not signup_list_msg_id:
        return
    channel = client.get_channel(match["channel_id"])
    if not channel:
        return
    try:
        signups = await signups_db.get_signups_for_match(match_id)
        msg     = await channel.fetch_message(signup_list_msg_id)
        await msg.edit(content=build_fresh_pug_signup_list(signups))
    except Exception as e:
        log.warning(f"refresh_fresh_pug_signup_list failed for match #{match_id}: {e}")

class FreshPugSignupButton(ui.Button):
    def __init__(self, match_id):
        super().__init__(
            label="Sign Up",
            emoji="\U0001f427",  # 🐧 -- matches the "click 🐧 to join" instruction in the message template
            custom_id=f"fp_signup:{match_id}",
            style=discord.ButtonStyle.primary,
            row=0,
        )
        self.match_id = match_id

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        match = await matches_db.get_match(self.match_id)

        if not match or match["ended"]:
            await interaction.followup.send(
                "This Fresh PUG has already ended or been cancelled.", ephemeral=True
            )
            return

        existing = await signups_db.get_signup_by_user_and_class(self.match_id, interaction.user.id, "any")
        if existing and existing["status"] != "denied":
            await interaction.followup.send(
                "You're already signed up for this Fresh PUG.", ephemeral=True
            )
            return

        signup_id = await signups_db.add_signup(
            self.match_id, interaction.user.id,
            interaction.user.display_name, "any",
        )
        if signup_id is None:
            await interaction.followup.send("Could not sign up. Try again.", ephemeral=True)
            return

        await signups_db.update_signup_status(signup_id, "accepted")

        is_sixs = match["type"] == "6s_fresh_pug"
        cap     = 12 if is_sixs else 18
        signups = await signups_db.get_signups_for_match(self.match_id)
        count   = len([s for s in signups if s["status"] == "accepted"])

        if count == cap and config.HOSTER_CHANNEL_ID:
            hoster_ch = interaction.client.get_channel(config.HOSTER_CHANNEL_ID)
            if hoster_ch:
                mode = "6s Fresh PUG" if is_sixs else "Fresh PUG"
                try:
                    await hoster_ch.send(
                        f"<@{match['created_by']}> \U0001f389 **{mode}** is full! ({cap}/{cap})"
                    )
                except Exception:
                    pass

        await refresh_fresh_pug_signup_list(interaction.client, self.match_id)
        await interaction.followup.send("\u2705 You're signed up!", ephemeral=True)

class FreshPugSignOutButton(ui.Button):
    def __init__(self, match_id):
        super().__init__(
            label="Sign Out",
            emoji="\U0001f6aa",
            custom_id=f"fp_signout:{match_id}",
            style=discord.ButtonStyle.danger,
            row=0,
        )
        self.match_id = match_id

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        match = await matches_db.get_match(self.match_id)

        if not match or match["ended"]:
            await interaction.followup.send(
                "This Fresh PUG has already ended or been cancelled.", ephemeral=True
            )
            return

        existing = await signups_db.get_signup_by_user_and_class(self.match_id, interaction.user.id, "any")
        if not existing or existing["status"] == "denied":
            await interaction.followup.send(
                "You're not signed up for this Fresh PUG.", ephemeral=True
            )
            return

        await signups_db.remove_signup(self.match_id, interaction.user.id, "any")
        await refresh_fresh_pug_signup_list(interaction.client, self.match_id)
        await interaction.followup.send("You've been signed out.", ephemeral=True)

class FreshPugSignupView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=None)
        self.add_item(FreshPugSignupButton(match_id))
        self.add_item(FreshPugSignOutButton(match_id))

class FreshPugManageView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=300)
        self.match_id = match_id

    @ui.button(label="Conclude PUG", style=discord.ButtonStyle.success, row=0)
    async def conclude(self, interaction, button):
        match = await matches_db.get_match(self.match_id)
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return
        if time.time() < match["timestamp"]:
            remaining = int(match["timestamp"] - time.time())
            h, m = divmod(remaining // 60, 60)
            await interaction.response.send_message(
                f"\u274c You can only conclude after the PUG has started. Starts in **{h}h {m}m**.",
                ephemeral=True,
            )
            return
        view = FreshPugConcludeConfirmView(self.match_id)
        await interaction.response.send_message(
            "Conclude this Fresh PUG? This will archive the thread and clean up.",
            view=view, ephemeral=True,
        )

    @ui.button(label="Cancel PUG", style=discord.ButtonStyle.danger, row=0)
    async def cancel(self, interaction, button):
        view = FreshPugCancelConfirmView(self.match_id)
        await interaction.response.send_message(
            "\u26a0\ufe0f Cancel this Fresh PUG?",
            view=view, ephemeral=True,
        )

class FreshPugConcludeConfirmView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=60)
        self.match_id = match_id

    @ui.button(label="Yes, conclude", style=discord.ButtonStyle.success)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        await do_conclude(interaction.client, interaction.guild, self.match_id, interaction.user.id)
        try:
            await interaction.followup.send(
                "\u2705 Fresh PUG concluded, archived, and channels removed.", ephemeral=True
            )
        except discord.HTTPException:
            pass

    @ui.button(label="Never mind", style=discord.ButtonStyle.secondary)
    async def abort(self, interaction, button):
        await interaction.response.edit_message(content="Cancelled.", view=None)

class FreshPugCancelConfirmView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=60)
        self.match_id = match_id

    @ui.button(label="Yes, cancel", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        await do_cancel(interaction.client, interaction.guild, self.match_id)
        try:
            await interaction.followup.send(
                "\u2705 Fresh PUG cancelled, archived, and channels removed.", ephemeral=True
            )
        except discord.HTTPException:
            pass

    @ui.button(label="Never mind", style=discord.ButtonStyle.secondary)
    async def abort(self, interaction, button):
        await interaction.response.edit_message(content="Cancellation aborted.", view=None)