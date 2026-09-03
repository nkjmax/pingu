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


def _is_mix_banned(interaction: discord.Interaction) -> bool:
    """Checked at the top of every sign-up button's callback -- covers
    mix, oPUG, and 6s alike (Mix Ban is meant to keep someone out of
    organized games generally, not one specific match type). Does NOT
    cover a hoster manually @mentioning a banned player into the free-
    text host roster field -- that's a different code path entirely
    (parsed text, not a button click), out of scope here."""
    if not config.MIX_BAN_ROLE_ID:
        return False
    return any(r.id == config.MIX_BAN_ROLE_ID for r in interaction.user.roles)


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

        if _is_mix_banned(interaction):
            await interaction.followup.send(
                "\u274c You currently have a Mix Ban and can't sign up for matches.", ephemeral=True
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

        if _is_mix_banned(interaction):
            await interaction.followup.send(
                "\u274c You currently have a Mix Ban and can't sign up for matches.", ephemeral=True
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
        if _is_mix_banned(interaction):
            await interaction.followup.send(
                "\u274c You currently have a Mix Ban and can't sign up for matches.", ephemeral=True
            )
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

# --- Open For All oPUG: direct-accept, no hoster approval step ---

async def _do_open_for_all_signup(interaction, match_id, class_name):
    result, signup_id = await signups_db.try_direct_accept(
        match_id, interaction.user.id, interaction.user.display_name, class_name, cap=2
    )
    if result == "accepted":
        interaction.client.ui_updater.schedule_refresh(match_id)
        await interaction.followup.send(f"\u2705 You're in as **{class_name}**!", ephemeral=True)
        return
    if result == "already_signed_up":
        await interaction.followup.send(
            f"You're already signed up for **{class_name}**.", ephemeral=True
        )
        return
    # "full" -- offer a sub slot instead
    view = OpenForAllSubConfirmView(match_id, class_name)
    await interaction.followup.send(
        f"**{class_name}** is full (2/2). Want to join as a sub instead?",
        view=view, ephemeral=True,
    )


class OpenForAllSubConfirmView(ui.View):
    def __init__(self, match_id, class_name):
        super().__init__(timeout=60)
        self.match_id   = match_id
        self.class_name = class_name

    @ui.button(label="Yes, sub", style=discord.ButtonStyle.primary)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        # cap=4 -- 2 main + up to 2 subs, same atomic primitive, just a
        # higher cap. Race-safe the same way the main-slot check is.
        result, signup_id = await signups_db.try_direct_accept(
            self.match_id, interaction.user.id, interaction.user.display_name,
            self.class_name, cap=4,
        )
        if result == "accepted":
            interaction.client.ui_updater.schedule_refresh(self.match_id)
            await interaction.followup.send(
                f"\u2705 You're in as a sub for **{self.class_name}**!", ephemeral=True
            )
        elif result == "already_signed_up":
            await interaction.followup.send(
                f"You're already signed up for **{self.class_name}**.", ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"Sorry, **{self.class_name}** subs are full too (2/2).", ephemeral=True
            )

    @ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(
            content="No worries \u2014 pick another class if you'd like.", view=None
        )


class OpenForAllClashConfirmView(ui.View):
    def __init__(self, match_id, class_name, clash_names):
        super().__init__(timeout=60)
        self.match_id    = match_id
        self.class_name  = class_name
        self.clash_names = clash_names

    @ui.button(label="Yes, sign up anyway", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        await _do_open_for_all_signup(interaction, self.match_id, self.class_name)

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(content="Sign-up cancelled.", view=None)


class OpenForAllClassButton(ui.Button):
    """Parametrized on class_list/emoji_map rather than duplicated per
    HL/6s -- Open For All applies to both, and the callback logic is
    otherwise identical between them."""

    def __init__(self, class_name, match_id, class_list, emoji_map, row_size):
        super().__init__(
            label=class_name,
            emoji=emoji_map[class_name],
            custom_id=f"ofa_signup:{match_id}:{class_name}",
            style=discord.ButtonStyle.secondary,
            row=class_list.index(class_name) // row_size,
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

        if _is_mix_banned(interaction):
            await interaction.followup.send(
                "\u274c You currently have a Mix Ban and can't sign up for matches.", ephemeral=True
            )
            return

        # Same "already on the main roster for another class" check as
        # the normal OPugClassButton/SixsClassButton -- Open For All
        # only skips the hoster-approval step, not this.
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
            interaction.user.id, exclude_match_id=self.match_id, reference_timestamp=match["timestamp"]
        )
        if clashing:
            clash_names = ", ".join(
                f"{m['team_name'] or 'a mix'} (<#{m['channel_id']}>)" for m in clashing
            )
            view = OpenForAllClashConfirmView(self.match_id, self.class_name, clash_names)
            warn = (
                "\u26a0\ufe0f **Warning:** You are already accepted in " + clash_names +
                ". Are you sure you want to sign up for this PUG too?"
            )
            await interaction.followup.send(warn, view=view, ephemeral=True)
            return

        await _do_open_for_all_signup(interaction, self.match_id, self.class_name)


class OpenForAllSignupView(ui.View):
    def __init__(self, match_id, is_sixs=False):
        super().__init__(timeout=None)
        class_list = SIXS_CLASSES if is_sixs else TF2_CLASSES
        emoji_map  = SIXS_CLASS_EMOJI if is_sixs else CLASS_EMOJI
        row_size   = 4 if is_sixs else 5
        for cls in class_list:
            self.add_item(OpenForAllClassButton(cls, match_id, class_list, emoji_map, row_size))
        self.add_item(SignOutButton(match_id))