"""
Both views use a select-then-act pattern (pick a player from a dropdown,
then hit Accept/Deny) rather than one button pair per player, since a
roster of 12-18 players would blow past Discord's 25-component limit if
each got its own row of buttons.

Note: selection state lives on the view instance, so these are ephemeral,
timeout-bound views (not persistent across a bot restart mid-review) --
acceptable for a short screening interaction, revisit if that becomes a
problem in practice.
"""

import discord
from discord import ui

import pingu.db.matches as matches_db
import pingu.db.signups as signups_db
from pingu.services import roster_service


class CaptainReviewView(ui.View):
    """/manage, shown to the match's captain: screen incoming signups."""

    def __init__(self, match, ui_updater, timeout=600):
        super().__init__(timeout=timeout)
        self.match = match
        self.ui_updater = ui_updater
        self.selected_signup_id: int | None = None
        self._select = ui.Select(placeholder="Select a player to review", options=[
            discord.SelectOption(label="(loading...)", value="0")
        ])
        self._select.callback = self._on_select
        self.add_item(self._select)

    @classmethod
    async def create(cls, match, ui_updater):
        view = cls(match, ui_updater)
        await view.refresh_options()
        return view

    async def refresh_options(self):
        pending = await signups_db.get_pending_undecided(self.match["id"])
        options = [
            discord.SelectOption(label=f"{s['username']} — {s['class_name']}", value=str(s["id"]))
            for s in pending[:25]
        ] or [discord.SelectOption(label="No pending signups", value="0")]
        self._select.options = options

    async def _on_select(self, interaction: discord.Interaction):
        self.selected_signup_id = int(self._select.values[0]) or None
        await interaction.response.defer()

    @ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: ui.Button):
        if not self.selected_signup_id:
            await interaction.response.send_message("Select a player first.", ephemeral=True)
            return
        try:
            await roster_service.captain_accept_signup(
                self.selected_signup_id, interaction.user.id, self.match, self.ui_updater
            )
        except roster_service.NotCaptain:
            await interaction.response.send_message("You're not the captain of this match.", ephemeral=True)
            return
        await self.refresh_options()
        await interaction.response.edit_message(
            content="Player accepted — awaiting hoster approval.", view=self
        )

    @ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: ui.Button):
        if not self.selected_signup_id:
            await interaction.response.send_message("Select a player first.", ephemeral=True)
            return
        try:
            await roster_service.captain_deny_signup(
                self.selected_signup_id, interaction.user.id, self.match, self.ui_updater
            )
        except roster_service.NotCaptain:
            await interaction.response.send_message("You're not the captain of this match.", ephemeral=True)
            return
        await self.refresh_options()
        await interaction.response.edit_message(content="Player denied.", view=self)


class HosterPicksReviewView(ui.View):
    """
    Shown when a hoster clicks 'Review captain picks'. Grouped by PLAYER,
    not by individual signup -- a player can carry one accept proposal
    plus several deny proposals at once (e.g. accept Soldier, deny Pyro
    and Demo), and selecting them shows that whole picture. The single
    Confirm button commits everything proposed for that player in one go;
    'Accept all' does the same across every player still awaiting review.
    """

    def __init__(self, match_id, ui_updater, timeout=600):
        super().__init__(timeout=timeout)
        self.match_id = match_id
        self.ui_updater = ui_updater
        self.selected_user_id: int | None = None
        self._select = ui.Select(placeholder="Select a player to review", options=[
            discord.SelectOption(label="(loading...)", value="0")
        ])
        self._select.callback = self._on_select
        self.add_item(self._select)

    @classmethod
    async def create(cls, match_id, ui_updater):
        view = cls(match_id, ui_updater)
        await view.refresh_options()
        return view

    async def refresh_options(self):
        by_player = await roster_service.get_captain_decisions_by_player(self.match_id)
        options = []
        for user_id, entry in list(by_player.items())[:25]:
            parts = []
            if entry["accept"]:
                parts.append(f"accept {entry['accept']['class_name']}")
            if entry["deny"]:
                parts.append(f"deny {', '.join(r['class_name'] for r in entry['deny'])}")
            options.append(discord.SelectOption(
                label=entry["username"],
                value=str(user_id),
                description=" | ".join(parts)[:100],
            ))
        self._select.options = options or [discord.SelectOption(label="Nothing awaiting review", value="0")]

    async def _on_select(self, interaction: discord.Interaction):
        self.selected_user_id = int(self._select.values[0]) or None
        await interaction.response.defer()

    @ui.button(label="Approve player", style=discord.ButtonStyle.success, row=0)
    async def approve_player(self, interaction: discord.Interaction, button: ui.Button):
        if not self.selected_user_id:
            await interaction.response.send_message("Select a player first.", ephemeral=True)
            return
        await roster_service.commit_player_decisions(
            interaction.client, self.match_id, self.selected_user_id, self.ui_updater
        )
        await self.refresh_options()
        await interaction.response.edit_message(content="Player's picks approved.", view=self)

    @ui.button(label="Reject player", style=discord.ButtonStyle.danger, row=0)
    async def reject_player(self, interaction: discord.Interaction, button: ui.Button):
        if not self.selected_user_id:
            await interaction.response.send_message("Select a player first.", ephemeral=True)
            return
        await roster_service.reject_player_decisions(
            interaction.client, self.match_id, self.selected_user_id, self.ui_updater
        )
        await self.refresh_options()
        await interaction.response.edit_message(content="Player rejected.", view=self)

    @ui.button(label="Approve all", style=discord.ButtonStyle.primary, row=1)
    async def approve_all(self, interaction: discord.Interaction, button: ui.Button):
        await roster_service.commit_all_captain_decisions(interaction.client, self.match_id, self.ui_updater)
        await self.refresh_options()
        await interaction.response.edit_message(content="All captain picks approved.", view=self)

    @ui.button(label="Reject all", style=discord.ButtonStyle.secondary, row=1)
    async def reject_all(self, interaction: discord.Interaction, button: ui.Button):
        await roster_service.reject_all_captain_decisions(interaction.client, self.match_id, self.ui_updater)
        await self.refresh_options()
        await interaction.response.edit_message(content="All captain picks rejected.", view=self)