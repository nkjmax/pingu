"""Split out of views/legacy.py -- sign-out flow, including the within-1-
hour LP-role warning path."""

import asyncio
import time
import discord
from discord import ui

from pingu.embeds import CLASS_EMOJI, SIXS_CLASS_EMOJI
from pingu import config
from pingu.db import matches as matches_db
from pingu.db import signups as signups_db


class SignOutConfirmView(ui.View):
    def __init__(self, match_id, user_id, class_name=None):
        super().__init__(timeout=60)
        self.match_id   = match_id
        self.user_id    = user_id
        self.class_name = class_name

    @ui.button(label="Yes, sign out anyway", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        await do_signout(interaction.client, self.match_id, self.user_id, self.class_name)
        await interaction.edit_original_response(
            content="You have been signed out. Please find a replacement as soon as possible.",
            view=None,
        )

    @ui.button(label="Stay signed up", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(
            content="Sign-out cancelled. You're still signed up.", view=None
        )

class SignOutAllConfirmView(ui.View):
    def __init__(self, match_id, user_id):
        super().__init__(timeout=60)
        self.match_id = match_id
        self.user_id  = user_id

    @ui.button(label="Yes, sign out of everything", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        signups = await signups_db.get_non_denied_signups_for_user(self.match_id, self.user_id)
        for s in signups:
            await do_signout(interaction.client, self.match_id, self.user_id, s["class_name"])
        await interaction.edit_original_response(
            content="You have been signed out of all classes. Please find replacements as soon as possible.",
            view=None,
        )

    @ui.button(label="Stay signed up", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(
            content="Sign-out cancelled. You're still signed up.", view=None
        )

class SignOutClassPickerView(ui.View):
    """Shown when a user is signed up on multiple classes and wants to sign out."""
    def __init__(self, match_id, user_id, signups, within_hour, rostered_classes):
        super().__init__(timeout=60)
        self.match_id        = match_id
        self.user_id         = user_id
        self.within_hour     = within_hour
        self.rostered_classes = rostered_classes
        all_emojis = {**CLASS_EMOJI, **SIXS_CLASS_EMOJI}
        options = [
            discord.SelectOption(
                label=s["class_name"],
                value=s["class_name"],
                emoji=all_emojis.get(s["class_name"]),
                description=f"Status: {s['status']}"
            )
            for s in signups
        ]
        options.append(discord.SelectOption(
            label="All classes",
            value="_all",
            emoji="\U0001f6aa",
            description="Sign out of everything"
        ))
        select = ui.Select(placeholder="Select class to sign out of\u2026", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction):
        value = interaction.data["values"][0]

        if value == "_all":
            if self.within_hour and self.rostered_classes:
                lp_warning = ""
                if config.LOW_PRIO_ROLE_ID:
                    lp_warning = f"\n\u26a0\ufe0f You will receive the <@&{config.LOW_PRIO_ROLE_ID}> role if you fail to find replacements."
                view = SignOutAllConfirmView(self.match_id, self.user_id)
                await interaction.response.edit_message(
                    content=f"\u26a0\ufe0f **Warning:** The match starts in less than 2 hours.{lp_warning}\nSign out of **all classes**?",
                    view=view,
                )
            else:
                await interaction.response.defer(ephemeral=True)
                signups = await signups_db.get_non_denied_signups_for_user(self.match_id, self.user_id)
                for s in signups:
                    await do_signout(interaction.client, self.match_id, self.user_id, s["class_name"])
                await interaction.followup.send("You have been signed out of all classes.", ephemeral=True)
            return

        class_name = value
        if self.within_hour and class_name in self.rostered_classes:
            lp_warning = ""
            if config.LOW_PRIO_ROLE_ID:
                lp_warning = f"\n\u26a0\ufe0f You will receive the <@&{config.LOW_PRIO_ROLE_ID}> role if you fail to find a replacement."
            view = SignOutConfirmView(self.match_id, self.user_id, class_name)
            await interaction.response.edit_message(
                content=f"\u26a0\ufe0f **Warning:** The match starts in less than 2 hours.{lp_warning}\nSign out of **{class_name}**?",
                view=view,
            )
        else:
            await interaction.response.defer(ephemeral=True)
            await do_signout(interaction.client, self.match_id, self.user_id, class_name)
            await interaction.followup.send(f"You have been signed out of **{class_name}**.", ephemeral=True)

async def do_signout(client, match_id, user_id, class_name=None):
    if class_name:
        signup = await signups_db.get_signup_by_user_and_class(match_id, user_id, class_name)
    else:
        signup = await signups_db.get_signup_by_user(match_id, user_id)
    if not signup:
        return None

    class_name   = signup["class_name"]
    was_accepted = signup["status"] == "accepted"
    match        = await matches_db.get_match(match_id)
    match_type   = match["type"] if match else "mix"
    is_opug      = match_type in ("opug", "6s_opug")

    accepted_for_class = await signups_db.get_accepted_signups_for_class(match_id, class_name)
    if is_opug:
        main_uids  = [s["user_id"] for s in accepted_for_class[:2]]
        is_rostered = was_accepted and user_id in main_uids
    else:
        is_rostered = was_accepted and len(accepted_for_class) > 0 and accepted_for_class[0]["user_id"] == user_id

    await signups_db.remove_signup(match_id, user_id, class_name)

    channel      = client.get_channel(match["channel_id"]) if match else None
    channel_name = channel.name if channel else "the match channel"

    if match_type in ("opug", "6s_opug"):
        match_label = f"{match['division'] or 'PUG'} PUG"
    elif match_type == "6s_mix":
        match_label = f"{match['team_name'] or 'Mix'} vs Mix 6s"
    else:
        match_label = f"{match['team_name'] or 'Mix'} vs Mix"

    if is_rostered:
        if is_opug:
            remaining = await signups_db.get_accepted_signups_for_class(match_id, class_name)
            if len(remaining) >= 2:
                newly_main = remaining[1]
                if match["thread_id"]:
                    try:
                        thread = client.get_channel(match["thread_id"])
                        if thread:
                            await thread.send(
                                f"<@{newly_main['user_id']}> you've been moved to the "
                                f"**{class_name}** main roster slot \u2014 the previous player signed out. \u2705"
                            )
                    except Exception:
                        pass
        else:
            next_sub = await signups_db.get_next_accepted_for_class(match_id, class_name, user_id)
            if next_sub:
                await signups_db.remove_sub_slots_for_user(match_id, next_sub["user_id"], class_name)
                if match["thread_id"]:
                    try:
                        thread = client.get_channel(match["thread_id"])
                        if thread:
                            await thread.send(
                                f"<@{next_sub['user_id']}> you've been moved to the "
                                f"**{class_name}** slot on the Mix Team \u2014 the previous player signed out. \u2705"
                            )
                    except Exception:
                        pass

                if config.HOSTER_CHANNEL_ID:
                    hoster_ch = client.get_channel(config.HOSTER_CHANNEL_ID)
                    if hoster_ch:
                        await hoster_ch.send(
                            f"<@{match['created_by']}> \u26a0\ufe0f **{signup['username']}** has signed out of "
                            f"**{class_name}** in <#{match['channel_id']}> ({match_label}). "
                            f"**{next_sub['username']}** has been moved to the main roster."
                        )
            else:
                if config.HOSTER_CHANNEL_ID:
                    hoster_ch = client.get_channel(config.HOSTER_CHANNEL_ID)
                    if hoster_ch:
                        await hoster_ch.send(
                            f"<@{match['created_by']}> \u26a0\ufe0f **{signup['username']}** has signed out of "
                            f"**{class_name}** in <#{match['channel_id']}> ({match_label})."
                        )

    elif was_accepted:
        pass

    client.ui_updater.schedule_refresh(match_id)
    return signup

class SignOutButton(ui.Button):
    def __init__(self, match_id):
        super().__init__(
            label="Sign Out",
            emoji="\U0001f6aa",
            custom_id=f"signout:{match_id}",
            style=discord.ButtonStyle.danger,
            row=4,
        )
        self.match_id = match_id

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        match = await matches_db.get_match(self.match_id)

        if not match or match["ended"]:
            await interaction.followup.send(
                "This match has already ended or been cancelled.", ephemeral=True
            )
            return

        signups = await signups_db.get_non_denied_signups_for_user(self.match_id, interaction.user.id)
        if not signups:
            await interaction.followup.send(
                "You're not signed up for this match.", ephemeral=True
            )
            return

        time_until = match["timestamp"] - time.time()
        within_hour = 0 < time_until <= 7200

        rostered_classes = set()
        if within_hour:
            for s in signups:
                if s["status"] == "accepted":
                    accepted_for_class = await signups_db.get_accepted_signups_for_class(self.match_id, s["class_name"])
                    if accepted_for_class and accepted_for_class[0]["user_id"] == interaction.user.id:
                        rostered_classes.add(s["class_name"])

        if len(signups) == 1:
            class_name = signups[0]["class_name"]
            if within_hour and class_name in rostered_classes:
                lp_warning = ""
                if config.LOW_PRIO_ROLE_ID:
                    lp_warning = f"\n\u26a0\ufe0f You will receive the <@&{config.LOW_PRIO_ROLE_ID}> role if you fail to find a replacement."
                view = SignOutConfirmView(self.match_id, interaction.user.id, class_name)
                await interaction.followup.send(
                    f"\u26a0\ufe0f **Warning:** The match starts in less than 2 hours.{lp_warning}\nAre you sure?",
                    view=view, ephemeral=True,
                )
            else:
                await do_signout(interaction.client, self.match_id, interaction.user.id, class_name)
                await interaction.followup.send("You have been signed out.", ephemeral=True)
        else:
            view = SignOutClassPickerView(self.match_id, interaction.user.id, signups, within_hour, rostered_classes)
            classes = ", ".join(f"**{s['class_name']}**" for s in signups)
            await interaction.followup.send(
                f"You're signed up on {classes}. Which class do you want to sign out of?",
                view=view, ephemeral=True,
            )