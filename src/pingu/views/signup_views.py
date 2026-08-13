"""Split out of views/legacy.py -- the sign-up flow (mix, opug, 6s mix),
clash detection, and the host-roster @mention block check."""

import discord
from discord import ui

from pingu.embeds import (
    TF2_CLASSES, CLASS_EMOJI, SIXS_CLASSES, SIXS_CLASS_EMOJI,
)
from pingu import config
from pingu.db import matches as matches_db
from pingu.db import signups as signups_db
from pingu.services import roster_service
from pingu.views.signout_views import SignOutButton


class ClassButton(ui.Button):
    def __init__(self, class_name, match_id):
        super().__init__(
            label=class_name,
            emoji=CLASS_EMOJI[class_name],
            custom_id=f"signup:{match_id}:{class_name}",
            style=discord.ButtonStyle.secondary,
            row=TF2_CLASSES.index(class_name) // 5,
        )
        self.class_name = class_name
        self.match_id   = match_id

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        match = await matches_db.get_match(self.match_id)

        if not match or match["ended"]:
            await interaction.followup.send(
                "This match has already ended or been cancelled.", ephemeral=True
            )
            return

        if interaction.user.id in roster_service.host_roster_user_ids(match["host_roster"]):
            await interaction.followup.send(
                "You're already on the host team roster for this match.", ephemeral=True
            )
            return

        all_signups = await signups_db.get_non_denied_signups_for_user(self.match_id, interaction.user.id)
        for s in all_signups:
            if s["status"] == "accepted":
                accepted_for = await signups_db.get_accepted_signups_for_class(self.match_id, s["class_name"])
                if accepted_for and accepted_for[0]["user_id"] == interaction.user.id:
                    await interaction.followup.send(
                        f"You're already on the main roster as **{s['class_name']}**. "
                        "Sign out first if you want to change classes.",
                        ephemeral=True,
                    )
                    return

        existing_class = await signups_db.get_signup_by_user_and_class(self.match_id, interaction.user.id, self.class_name)
        if existing_class and existing_class["status"] == "cancelled":
            existing_class = None
        if existing_class:
            if existing_class["status"] == "denied":
                await interaction.followup.send(
                    f"You've been denied for **{self.class_name}**. Please contact the hoster.",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                f"You're already signed up for **{self.class_name}**. "
                "Sign out of this class first if you want to change it.",
                ephemeral=True,
            )
            return

        clashing = await signups_db.get_accepted_matches_for_user(
            interaction.user.id,
            exclude_match_id=self.match_id,
            reference_timestamp=match["timestamp"]
        )
        if clashing:
            clash_names = ", ".join(
                f"{m['team_name'] or 'a mix'} (<#{m['channel_id']}>)" for m in clashing
            )
            view = ClashConfirmView(self.match_id, self.class_name, clash_names)
            warn = "\u26a0\ufe0f **Warning:** You are already accepted in " + clash_names + ". Are you sure you want to sign up for this mix too?"
            await interaction.followup.send(warn, view=view, ephemeral=True)
            return

        await _do_signup(interaction, self.match_id, self.class_name)

class ClashConfirmView(ui.View):
    def __init__(self, match_id, class_name, clash_names):
        super().__init__(timeout=60)
        self.match_id   = match_id
        self.class_name = class_name
        self.clash_names = clash_names

    @ui.button(label="Yes, sign up anyway", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        await _do_signup(interaction, self.match_id, self.class_name)

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(
            content="Sign-up cancelled.", view=None
        )

async def _do_signup(interaction, match_id, class_name):
    """Shared signup logic used by ClassButton and ClashConfirmView."""
    match = await matches_db.get_match(match_id)

    signup_id = await signups_db.add_signup(
        match_id, interaction.user.id,
        interaction.user.display_name, class_name,
    )
    if signup_id is None:
        await interaction.followup.send("Could not add sign-up. Try again.", ephemeral=True)
        return

    # No confirmation message shown. This is a button click (a "component"
    # interaction) -- interaction.response.defer() for those ALWAYS uses
    # Discord's DEFERRED_MESSAGE_UPDATE type regardless of the ephemeral
    # flag passed (that flag only means anything for slash-command-style
    # interactions). Which means there was never an ephemeral placeholder
    # to dismiss in the first place -- a bare defer() already shows the
    # clicking user nothing at all for a button. The delete_original_
    # response() call that used to be here was therefore operating on the
    # PUBLIC match message itself (since that's what "original response"
    # resolves to for a deferred component interaction), silently
    # deleting the real mix message every time someone signed up. Do not
    # re-add a delete/edit of the original response here.

    match_row = await matches_db.get_match(match_id)

    clashing = await signups_db.get_accepted_matches_for_user(
        interaction.user.id,
        exclude_match_id=match_id,
        reference_timestamp=match["timestamp"]
    )
    if clashing and config.HOSTER_CHANNEL_ID:
        hoster_ch = interaction.client.get_channel(config.HOSTER_CHANNEL_ID)
        if hoster_ch:
            match_type = match_row["type"]
            if match_type in ("opug", "6s_opug"):
                this_match_label = f"{match_row['division'] or 'PUG'} PUG"
            elif match_type == "6s_mix":
                this_match_label = f"{match_row['team_name'] or 'Mix'} vs Mix 6s"
            else:
                this_match_label = f"{match_row['team_name'] or 'Mix'} vs Mix"

            hoster_pings = {match_row["created_by"]}
            for m in clashing:
                hoster_pings.add(m["created_by"])
            pings_str = " ".join(f"<@{uid}>" for uid in hoster_pings)

            def clash_label(m):
                t = m["type"] if m["type"] else "mix"
                if t in ("opug", "6s_opug"):
                    return f"<#{m['channel_id']}> ({m['division'] or 'PUG'} PUG)"
                elif t == "6s_mix":
                    return f"<#{m['channel_id']}> ({m['team_name'] or 'Mix'} vs Mix 6s)"
                else:
                    return f"<#{m['channel_id']}> ({m['team_name'] or 'Mix'} vs Mix)"

            clash_refs = ", ".join(clash_label(m) for m in clashing)
            await hoster_ch.send(
                f"{pings_str} \u26a0\ufe0f **{interaction.user.display_name}** signed up for **{class_name}** "
                f"in <#{match_row['channel_id']}> ({this_match_label}) "
                f"but is already accepted in {clash_refs}."
            )

    interaction.client.ui_updater.schedule_refresh(match_id)

class OPugClassButton(ui.Button):
    def __init__(self, class_name, match_id):
        super().__init__(
            label=class_name,
            emoji=CLASS_EMOJI[class_name],
            custom_id=f"opug_signup:{match_id}:{class_name}",
            style=discord.ButtonStyle.secondary,
            row=TF2_CLASSES.index(class_name) // 5,
        )
        self.class_name = class_name
        self.match_id   = match_id

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        match = await matches_db.get_match(self.match_id)

        if not match or match["ended"]:
            await interaction.followup.send(
                "This PUG has already ended or been cancelled.", ephemeral=True
            )
            return

        existing_class = await signups_db.get_signup_by_user_and_class(self.match_id, interaction.user.id, self.class_name)
        if existing_class and existing_class["status"] == "cancelled":
            existing_class = None
        if existing_class:
            if existing_class["status"] == "denied":
                await interaction.followup.send(
                    f"You've been denied for **{self.class_name}**. Please contact the hoster.",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                f"You're already signed up for **{self.class_name}**.", ephemeral=True
            )
            return

        all_signups = await signups_db.get_non_denied_signups_for_user(self.match_id, interaction.user.id)
        for s in all_signups:
            if s["status"] == "accepted":
                accepted_for = await signups_db.get_accepted_signups_for_class(self.match_id, s["class_name"])
                main_uids = [a["user_id"] for a in accepted_for[:2]]
                if interaction.user.id in main_uids:
                    await interaction.followup.send(
                        f"You're already on the main roster as **{s['class_name']}**. "
                        "Sign out first if you want to change classes.",
                        ephemeral=True,
                    )
                    return
        clashing = await signups_db.get_accepted_matches_for_user(
            interaction.user.id,
            exclude_match_id=self.match_id,
            reference_timestamp=match["timestamp"]
        )
        if clashing:
            clash_names = ", ".join(
                f"{m['team_name'] or 'a mix'} (<#{m['channel_id']}>)" for m in clashing
            )
            view = ClashConfirmView(self.match_id, self.class_name, clash_names)
            warn = "\u26a0\ufe0f **Warning:** You are already accepted in " + clash_names + ". Are you sure you want to sign up for this PUG too?"
            await interaction.followup.send(warn, view=view, ephemeral=True)
            return

        await _do_signup(interaction, self.match_id, self.class_name)

class OPugSignupView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=None)
        for cls in TF2_CLASSES:
            self.add_item(OPugClassButton(cls, match_id))
        self.add_item(SignOutButton(match_id))

class SignupView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=None)
        for cls in TF2_CLASSES:
            self.add_item(ClassButton(cls, match_id))
        self.add_item(SignOutButton(match_id))

class SixsClassButton(ui.Button):
    def __init__(self, class_name, match_id):
        super().__init__(
            label=class_name,
            emoji=SIXS_CLASS_EMOJI[class_name],
            custom_id=f"sixs_signup:{match_id}:{class_name}",
            style=discord.ButtonStyle.secondary,
            row=SIXS_CLASSES.index(class_name) // 4,
        )
        self.class_name = class_name
        self.match_id   = match_id

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        match = await matches_db.get_match(self.match_id)
        if not match or match["ended"]:
            await interaction.followup.send("This match has already ended.", ephemeral=True)
            return
        if interaction.user.id in roster_service.host_roster_user_ids(match["host_roster"]):
            await interaction.followup.send(
                "You're already on the host team roster for this match.", ephemeral=True
            )
            return
        existing_class = await signups_db.get_signup_by_user_and_class(self.match_id, interaction.user.id, self.class_name)
        if existing_class and existing_class["status"] == "cancelled":
            existing_class = None
        if existing_class:
            if existing_class["status"] == "denied":
                await interaction.followup.send(
                    f"You've been denied for **{self.class_name}**. Please contact the hoster.",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(f"You're already signed up for **{self.class_name}**.", ephemeral=True)
            return

        all_signups = await signups_db.get_non_denied_signups_for_user(self.match_id, interaction.user.id)
        for s in all_signups:
            if s["status"] == "accepted":
                accepted_for = await signups_db.get_accepted_signups_for_class(self.match_id, s["class_name"])
                if accepted_for and accepted_for[0]["user_id"] == interaction.user.id:
                    await interaction.followup.send(
                        f"You're already on the main roster as **{s['class_name']}**. "
                        "Sign out first if you want to change classes.",
                        ephemeral=True,
                    )
                    return

        clashing = await signups_db.get_accepted_matches_for_user(
            interaction.user.id, exclude_match_id=self.match_id, reference_timestamp=match["timestamp"]
        )
        if clashing:
            clash_names = ", ".join(f"{m['team_name'] or 'a match'} (<#{m['channel_id']}>)" for m in clashing)
            view = ClashConfirmView(self.match_id, self.class_name, clash_names)
            await interaction.followup.send(
                "\u26a0\ufe0f **Warning:** You are already accepted in " + clash_names + ". Sign up anyway?",
                view=view, ephemeral=True
            )
            return
        await _do_signup(interaction, self.match_id, self.class_name)

class SixsSignupView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=None)
        for cls in SIXS_CLASSES:
            self.add_item(SixsClassButton(cls, match_id))
        self.add_item(SignOutButton(match_id))