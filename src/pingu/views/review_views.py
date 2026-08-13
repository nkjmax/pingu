"""
Split out of views/legacy.py -- the hoster's accept/deny review panels
(dropdown -> class -> player-pick buttons), plus _do_accept (the direct-
accept entry point; roster_service.finalise_accept does the real work,
this just handles the ephemeral confirmation + panel refresh).

build_manage_text lives in manage_views.py -- imported LOCALLY inside the
functions that need it here (not at module level) since manage_views.py
imports ReviewView/DenyReviewView from this file, and a module-level
import back would create a circular import between the two.
"""

import discord
from discord import ui

from pingu.embeds import TF2_CLASSES, CLASS_EMOJI, SIXS_CLASSES, SIXS_CLASS_EMOJI
from pingu.db import matches as matches_db
from pingu.db import signups as signups_db
from pingu.services import roster_service
from pingu.services.roster_service import is_lp


class ClassDropdownSelect(ui.Select):
    """Dropdown to pick a class -- only shows classes with pending signups."""
    def __init__(self, match_id, pending_by_class, is_sixs=False):
        self.match_id = match_id
        self.is_sixs  = is_sixs
        class_list    = SIXS_CLASSES if is_sixs else TF2_CLASSES
        options = []
        for cls in class_list:
            count = len(pending_by_class.get(cls, []))
            if count:
                options.append(discord.SelectOption(
                    label=cls,
                    value=cls,
                    description=str(count) + " pending",
                ))
        if not options:
            options = [discord.SelectOption(label="No pending", value="_none", description="No pending sign-ups")]
        super().__init__(
            placeholder="Select a class to review...",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction):
        class_name = self.values[0]
        pending    = await signups_db.get_pending_signups(self.match_id)
        class_pend = sorted([s for s in pending if s["class_name"] == class_name], key=lambda s: s["id"])

        if not class_pend:
            await interaction.response.edit_message(
                content="No pending sign-ups for **" + class_name + "** anymore.",
                view=await ReviewView.create(self.match_id),
            )
            return

        view = PlayerPickView(self.match_id, class_name, class_pend)
        text = "**" + class_name + "**  \u2014  click a player to accept *(chronological order)*"
        await interaction.response.edit_message(content=text, view=view)

class LPConfirmView(ui.View):
    def __init__(self, match_id, signup_id, username, class_name, filled):
        super().__init__(timeout=60)
        self.match_id   = match_id
        self.signup_id  = signup_id
        self.username   = username
        self.class_name = class_name
        self.filled     = filled

    @ui.button(label="Yes, accept anyway", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        current = await signups_db.get_signup_by_id(self.signup_id)
        already = current and current["status"] == "accepted"
        if already:
            await interaction.followup.send(
                f"\u274c **{self.username}** has already been accepted.", ephemeral=True
            )
            return
        await _do_accept(interaction, self.match_id, self.signup_id, self.username, self.class_name)

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(content="Cancelled.", view=None)

async def _do_accept(interaction, match_id, signup_id, username, class_name):
    """
    Thin wrapper -- the actual accept logic (status, cleanup, LP reorder,
    thread ping, refresh) lives in roster_service.finalise_accept so it's
    identical regardless of entry point. This just handles the ephemeral
    confirmation and refreshing whichever review panel the hoster is
    looking at.
    """
    result = await roster_service.finalise_accept(interaction.client, match_id, signup_id)
    if not result:
        await interaction.followup.send(f"\u274c **{username}** could not be accepted (signup not found).", ephemeral=True)
        return

    on_main = result["on_main"]

    outcome = "accepted on " + class_name if on_main else "added as sub"
    await interaction.followup.send(
        "\u2705  " + username + " \u2014 " + outcome + ".", ephemeral=True
    )

    try:
        pending    = await signups_db.get_pending_signups(match_id)
        class_pend = sorted([s for s in pending if s["class_name"] == class_name], key=lambda s: s["id"])
        if class_pend:
            view = PlayerPickView(match_id, class_name, class_pend)
            text = "**" + class_name + "**  \u2014  click a player to accept *(chronological order)*"
            await interaction.message.edit(content=text, view=view)
        else:
            from pingu.views.manage_views import build_manage_text
            view = await ReviewView.create(match_id)
            text, _ = await build_manage_text(match_id)
            await interaction.message.edit(content=text, view=view)
    except Exception:
        pass

class PlayerPickView(ui.View):
    """Shows pending players as plain buttons -- click to accept, no deny needed here."""
    def __init__(self, match_id, class_name, pending_signups):
        super().__init__(timeout=300)
        self.match_id   = match_id
        self.class_name = class_name
        row = 0
        for i, s in enumerate(pending_signups):
            if i > 0 and i % 5 == 0:
                row += 1
            if row > 3:
                break
            self.add_item(AcceptPlayerButton(match_id, s["id"], s["username"], class_name, row))
        self.add_item(BackToReviewButton(match_id, row=4))

class AcceptPlayerButton(ui.Button):
    def __init__(self, match_id, signup_id, username, class_name, row):
        super().__init__(
            label=username,
            style=discord.ButtonStyle.success,
            custom_id="acc:" + str(match_id) + ":" + str(signup_id),
            row=row,
        )
        self.match_id   = match_id
        self.signup_id  = signup_id
        self.username   = username
        self.class_name = class_name

    async def callback(self, interaction):
        current = await signups_db.get_signup_by_id(self.signup_id)
        already = current and current["status"] == "accepted"

        if already:
            await interaction.response.send_message(
                f"\u274c **{self.username}** has already been accepted.", ephemeral=True
            )
            return

        filled  = await signups_db.count_accepted_for_class(self.match_id, self.class_name)

        user_id    = current["user_id"] if current else None
        player_lp  = await is_lp(interaction.client, user_id) if user_id else False
        if player_lp and not already:
            view = LPConfirmView(self.match_id, self.signup_id, self.username, self.class_name, filled)
            await interaction.response.send_message(
                "\u26a0\ufe0f **" + self.username + "** currently has the Low Priority role. Are you sure you want to accept them?",
                view=view, ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        await _do_accept(interaction, self.match_id, self.signup_id, self.username, self.class_name)

class DenyPlayerButton(ui.Button):
    def __init__(self, match_id, signup_id, username, class_name, row):
        super().__init__(
            label="Deny  " + username,
            style=discord.ButtonStyle.danger,
            custom_id="den:" + str(match_id) + ":" + str(signup_id),
            row=row,
        )
        self.match_id   = match_id
        self.signup_id  = signup_id
        self.username   = username
        self.class_name = class_name

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        await roster_service.finalise_deny(interaction.client, self.match_id, self.signup_id)
        await interaction.followup.send(
            "\u274c  " + self.username + " denied for " + self.class_name + ".", ephemeral=True
        )

        pending    = await signups_db.get_pending_signups(self.match_id)
        class_pend = sorted([s for s in pending if s["class_name"] == self.class_name], key=lambda s: s["id"])
        if class_pend:
            view = PlayerPickView(self.match_id, self.class_name, class_pend)
            text = "**" + self.class_name + "**  \u2014  click a player to accept *(chronological order)*"
            await interaction.message.edit(content=text, view=view)
        else:
            from pingu.views.manage_views import build_manage_text
            view = await ReviewView.create(self.match_id)
            text, _ = await build_manage_text(self.match_id)
            await interaction.message.edit(content=text, view=view)

class BackToReviewButton(ui.Button):
    def __init__(self, match_id, row):
        super().__init__(
            label="Back",
            style=discord.ButtonStyle.secondary,
            custom_id="back_review:" + str(match_id),
            row=row,
        )
        self.match_id = match_id

    async def callback(self, interaction):
        from pingu.views.manage_views import build_manage_text
        view = await ReviewView.create(self.match_id)
        text, _ = await build_manage_text(self.match_id)
        await interaction.response.edit_message(content=text, view=view)

class ReviewView(ui.View):
    """The main review panel -- dropdown to pick a class."""
    def __init__(self, match_id):
        super().__init__(timeout=300)
        self.match_id = match_id

    @classmethod
    async def create(cls, match_id):
        self    = cls(match_id)
        match   = await matches_db.get_match(match_id)
        is_sixs = match["type"] in ("6s_opug", "6s_mix") if match else False
        pending = await signups_db.get_pending_signups(match_id)
        pending_by_class = {}
        for s in pending:
            pending_by_class.setdefault(s["class_name"], []).append(s)
        if pending_by_class:
            self.add_item(ClassDropdownSelect(match_id, pending_by_class, is_sixs=is_sixs))
        return self

class DenyClassDropdownSelect(ui.Select):
    def __init__(self, match_id, pending_by_class, is_sixs=False):
        self.match_id = match_id
        class_list    = SIXS_CLASSES if is_sixs else TF2_CLASSES
        options = []
        for cls in class_list:
            count = len(pending_by_class.get(cls, []))
            if count:
                options.append(discord.SelectOption(
                    label=cls,
                    value=cls,
                    description=str(count) + " pending",
                ))
        if not options:
            options = [discord.SelectOption(label="No pending", value="_none", description="No pending sign-ups")]
        super().__init__(
            placeholder="Select a class to deny from...",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction):
        class_name = self.values[0]
        pending    = await signups_db.get_pending_signups(self.match_id)
        class_pend = sorted([s for s in pending if s["class_name"] == class_name], key=lambda s: s["id"])

        if not class_pend:
            await interaction.response.edit_message(
                content="No pending sign-ups for **" + class_name + "** anymore.",
                view=await DenyReviewView.create(self.match_id),
            )
            return

        view = DenyPlayerPickView(self.match_id, class_name, class_pend)
        text = "**" + class_name + "**  \u2014  click a player to deny *(chronological order)*"
        await interaction.response.edit_message(content=text, view=view)

class DenyPlayerPickView(ui.View):
    def __init__(self, match_id, class_name, pending_signups):
        super().__init__(timeout=300)
        self.match_id   = match_id
        self.class_name = class_name
        row = 0
        for i, s in enumerate(pending_signups):
            if i > 0 and i % 5 == 0:
                row += 1
            if row > 3:
                break
            self.add_item(DenyOnlyButton(match_id, s["id"], s["username"], class_name, row))
        self.add_item(BackToDenyReviewButton(match_id, row=4))

class DenyOnlyButton(ui.Button):
    def __init__(self, match_id, signup_id, username, class_name, row):
        super().__init__(
            label=username,
            style=discord.ButtonStyle.danger,
            custom_id="dny:" + str(match_id) + ":" + str(signup_id),
            row=row,
        )
        self.match_id   = match_id
        self.signup_id  = signup_id
        self.username   = username
        self.class_name = class_name

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        await roster_service.finalise_deny(interaction.client, self.match_id, self.signup_id)
        await interaction.followup.send(
            "\u274c " + self.username + " denied for **" + self.class_name + "**.", ephemeral=True
        )
        pending    = await signups_db.get_pending_signups(self.match_id)
        class_pend = sorted([s for s in pending if s["class_name"] == self.class_name], key=lambda s: s["id"])
        try:
            if class_pend:
                view = DenyPlayerPickView(self.match_id, self.class_name, class_pend)
                text = "**" + self.class_name + "**  \u2014  click a player to deny *(chronological order)*"
            else:
                from pingu.views.manage_views import build_manage_text
                view = await DenyReviewView.create(self.match_id)
                text, _ = await build_manage_text(self.match_id)
            await interaction.edit_original_response(content=text, view=view)
        except Exception:
            pass

class BackToDenyReviewButton(ui.Button):
    def __init__(self, match_id, row):
        super().__init__(
            label="Back",
            style=discord.ButtonStyle.secondary,
            custom_id="back_deny_review:" + str(match_id),
            row=row,
        )
        self.match_id = match_id

    async def callback(self, interaction):
        from pingu.views.manage_views import build_manage_text
        view = await DenyReviewView.create(self.match_id)
        text, _ = await build_manage_text(self.match_id)
        await interaction.response.edit_message(content=text, view=view)

class DenyReviewView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=300)
        self.match_id = match_id

    @classmethod
    async def create(cls, match_id):
        self    = cls(match_id)
        match   = await matches_db.get_match(match_id)
        is_sixs = match["type"] in ("6s_opug", "6s_mix") if match else False
        pending = await signups_db.get_pending_signups(match_id)
        pending_by_class = {}
        for s in pending:
            pending_by_class.setdefault(s["class_name"], []).append(s)
        if pending_by_class:
            self.add_item(DenyClassDropdownSelect(match_id, pending_by_class, is_sixs=is_sixs))
        return self