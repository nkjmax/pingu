"""
/ticket -- cascading dropdowns of variable depth (see
templates/ticket_taxonomy.py), ending in a modal with a single
description field. On submit: creates a fully private channel named
after the ticket's number (reporter + MOD_ROLE_ID only, @everyone can't
even see it), posts the ticket summary as the first message with a
persistent Resolve/Cancel view attached.

Resolve requires a mod (MOD_ROLE_ID or the Administrator permission,
which Discord already grants channel access to regardless of overwrites
-- "mods and above" is covered without needing a second role). Cancel is
open to anyone with channel access. Both go through a confirmation step
first, then archive + delete the channel in the background -- no
archiving skipped, since the full record lives in the DB + the synced
Excel export either way.
"""

import logging
import discord
from discord import app_commands, ui
from discord.ext import commands

from pingu import config
import pingu.db.tickets as tickets_db
from pingu.services import ticket_export_service, ticket_archive_service
from pingu.templates.ticket_taxonomy import TICKET_TREE, CATEGORY_CODES

log = logging.getLogger("cogs.tickets")


def _leaf_options(items):
    return [discord.SelectOption(label=item, value=item) for item in items]


def _is_mod(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    if not config.MOD_ROLE_ID:
        return False
    return any(r.id == config.MOD_ROLE_ID for r in interaction.user.roles)


def _build_ticket_summary(ticket_number: str, ticket_type: str, submitter_mention: str, body: str) -> str:
    """
    Shared by ticket creation AND archival, so both stay visually
    identical. Category shown is the LAST dropdown selection only (e.g.
    "Cheating/Alting"), not the full category -> subcategory -> type
    breadcrumb. Body goes straight into a blockquote with no "Reason:"
    label, one '> ' per line so the submitter's own line breaks survive.
    """
    body_lines = body.split("\n")
    blockquoted = "\n".join(f"> {line}" for line in body_lines)
    return (
        f"\U0001f4e9 **Ticket #{ticket_number}**\n"
        f"**Category:** {ticket_type}\n"
        f"**Submitted by:** {submitter_mention}\n"
        f"{blockquoted}\n"
        f"** **"
    )


class TicketCategorySelect(ui.View):
    def __init__(self, timeout=180):
        super().__init__(timeout=timeout)
        select = ui.Select(
            placeholder="Select a category\u2026",
            options=_leaf_options(TICKET_TREE.keys()),
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        category = interaction.data["values"][0]
        branch = TICKET_TREE[category]

        if branch is None:
            # "Other" -- the category itself is the leaf, straight to the modal.
            await interaction.response.send_modal(
                TicketDescriptionModal(category, subcategory=None, ticket_type=category)
            )
            return

        if isinstance(branch, dict):
            await interaction.response.edit_message(
                content=f"**{category}** \u2014 select a subcategory:",
                view=TicketSubcategorySelect(category, branch),
            )
            return

        # A flat list -- leaf types directly, subcategory step skipped.
        await interaction.response.edit_message(
            content=f"**{category}** \u2014 select a type:",
            view=TicketTypeSelect(category, subcategory=None, types=branch),
        )


class TicketSubcategorySelect(ui.View):
    def __init__(self, category, branch: dict, timeout=180):
        super().__init__(timeout=timeout)
        self.category = category
        self.branch = branch
        select = ui.Select(
            placeholder="Select a subcategory\u2026",
            options=_leaf_options(branch.keys()),
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        subcategory = interaction.data["values"][0]
        types = self.branch[subcategory]
        await interaction.response.edit_message(
            content=f"**{self.category} \u2192 {subcategory}** \u2014 select a type:",
            view=TicketTypeSelect(self.category, subcategory, types),
        )


class TicketTypeSelect(ui.View):
    def __init__(self, category, subcategory, types: list, timeout=180):
        super().__init__(timeout=timeout)
        self.category = category
        self.subcategory = subcategory
        select = ui.Select(
            placeholder="Select a type\u2026",
            options=_leaf_options(types),
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        ticket_type = interaction.data["values"][0]
        await interaction.response.send_modal(
            TicketDescriptionModal(self.category, self.subcategory, ticket_type)
        )


class TicketDescriptionModal(ui.Modal, title="Describe the issue"):
    body_input = ui.TextInput(
        label="Description",
        style=discord.TextStyle.paragraph,
        placeholder="Give as much detail as you can \u2014 who, what, when, and any evidence/links.",
        required=True,
        max_length=4000,
    )

    def __init__(self, category, subcategory, ticket_type):
        super().__init__()
        self.category = category
        self.subcategory = subcategory
        self.ticket_type = ticket_type

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        body = str(self.body_input).strip()
        category_code = CATEGORY_CODES.get(self.category, self.category.upper())
        ticket_id, ticket_number = await tickets_db.create_ticket(
            category_code, interaction.user.id, self.category, self.subcategory, self.ticket_type, body
        )

        channel = await _create_ticket_channel(interaction.guild, ticket_number, interaction.user)
        if not channel:
            await interaction.followup.send(
                "\u274c Ticket saved, but couldn't create a channel for it \u2014 check TICKET_CATEGORY_ID in .env.",
                ephemeral=True,
            )
            return

        await tickets_db.set_channel_id(ticket_id, channel.id)

        try:
            saved_ticket = await tickets_db.get_ticket(ticket_id)
            await ticket_export_service.upsert_ticket_row(saved_ticket)
        except Exception as e:
            log.warning(f"tickets: excel export failed for ticket #{ticket_number}: {e}")

        summary = _build_ticket_summary(ticket_number, self.ticket_type, interaction.user.mention, body)
        info_msg = await channel.send(summary, view=TicketActionsView())

        try:
            thread = await info_msg.create_thread(name=f"{ticket_number} discussion", auto_archive_duration=1440)
            await tickets_db.set_thread_id(ticket_id, thread.id)
            await thread.send(
                f"{interaction.user.mention}, discuss this ticket here. "
                f"A mod will follow up when they're able to."
            )
        except Exception as e:
            log.warning(f"tickets: thread creation failed for ticket #{ticket_number}: {e}")

        await interaction.followup.send(
            f"\u2705 Ticket #{ticket_number} submitted \u2014 {channel.mention}", ephemeral=True
        )


async def _create_ticket_channel(guild: discord.Guild, ticket_number: str, reporter: discord.Member):
    if not config.TICKET_CATEGORY_ID:
        return None
    category = guild.get_channel(config.TICKET_CATEGORY_ID)
    if not category or not isinstance(category, discord.CategoryChannel):
        return None

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        # Reporter is thread-only -- can see the channel and its buttons,
        # but actual discussion happens in the thread, not here.
        reporter: discord.PermissionOverwrite(view_channel=True, send_messages=False),
    }
    if config.MOD_ROLE_ID:
        mod_role = guild.get_role(config.MOD_ROLE_ID)
        if mod_role:
            # Mods keep full send access to the main channel itself, not
            # just the thread -- unlike the reporter.
            overwrites[mod_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    return await category.create_text_channel(name=ticket_number.lower(), overwrites=overwrites)


class TicketActionsView(ui.View):
    """Persistent -- the Resolve/Cancel buttons attached to every
    ticket's first message. Both route through a confirmation step
    before actually closing anything."""

    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Resolve", style=discord.ButtonStyle.success, custom_id="ticket_resolve")
    async def resolve(self, interaction: discord.Interaction, button: ui.Button):
        ticket = await tickets_db.get_ticket_by_channel(interaction.channel_id)
        if not ticket:
            await interaction.response.send_message("Couldn't find a ticket for this channel.", ephemeral=True)
            return
        if not _is_mod(interaction):
            await interaction.response.send_message("Only mods can resolve a ticket.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Are you sure you want to **resolve** ticket #{ticket['ticket_number']}?",
            view=TicketCloseConfirmView(ticket["id"], "resolved"),
            ephemeral=True,
        )

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="ticket_cancel")
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        ticket = await tickets_db.get_ticket_by_channel(interaction.channel_id)
        if not ticket:
            await interaction.response.send_message("Couldn't find a ticket for this channel.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Are you sure you want to **cancel** ticket #{ticket['ticket_number']}?",
            view=TicketCloseConfirmView(ticket["id"], "cancelled"),
            ephemeral=True,
        )


class TicketCloseConfirmView(ui.View):
    def __init__(self, ticket_id: int, status: str, timeout=60):
        super().__init__(timeout=timeout)
        self.ticket_id = ticket_id
        self.status = status

    @ui.button(label="Yes, confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)

        ticket = await tickets_db.get_ticket(self.ticket_id)
        if not ticket or ticket["status"] != "open":
            await interaction.followup.send("This ticket has already been closed.", ephemeral=True)
            return

        await tickets_db.close_ticket(self.ticket_id, self.status, interaction.user.id)
        updated = await tickets_db.get_ticket(self.ticket_id)

        try:
            await ticket_export_service.upsert_ticket_row(updated)
        except Exception as e:
            log.warning(f"tickets: excel export failed for ticket #{updated['ticket_number']}: {e}")

        verb = "resolved" if self.status == "resolved" else "cancelled"
        emoji = "\u2705" if self.status == "resolved" else "\U0001f6ab"

        try:
            await interaction.channel.send(
                f"{emoji} Ticket #{updated['ticket_number']} {verb} by {interaction.user.mention}. "
                f"Archiving and cleaning up now \u2014 this channel will be deleted shortly."
            )
        except Exception:
            pass

        await interaction.followup.send(f"Ticket {verb}.", ephemeral=True)

        # Archive-then-delete runs as a background task -- the confirming
        # user's own response above doesn't wait on it, and teardown is
        # sequenced strictly after archiving finishes.
        ticket_archive_service.fire_ticket_archive_and_teardown(
            interaction.client, updated, self.status, interaction.user.id
        )

    @ui.button(label="Never mind", style=discord.ButtonStyle.secondary)
    async def abort(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)


class TicketsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(TicketActionsView())

    @app_commands.command(name="ticket", description="Submit a report, appeal, or piece of feedback.")
    async def ticket(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "What would you like to submit?", view=TicketCategorySelect(), ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(TicketsCog(bot))