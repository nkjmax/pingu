"""
Covers the whole /host-request flow: the initial Fresh PUG / Request a mix
choice, both modals, and the hoster's Accept/Deny buttons used inside the
mix-request thread (via /manage).
"""

import discord
from discord import ui

from pingu import config
from pingu.services import hosting_service, fresh_pug_service
from pingu.db import matches as matches_db


class FreshPugModal(ui.Modal, title="Fresh PUG"):
    maps = ui.TextInput(label="Map(s)", placeholder="e.g. cp_process, cp_gullywash")
    server = ui.TextInput(label="Server", placeholder="Connect string or server name")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            match_id = await fresh_pug_service.create(
                interaction.user.id, interaction.user.display_name,
                str(self.maps), str(self.server),
            )
        except fresh_pug_service.FreshPugAlreadyActive as e:
            await interaction.response.send_message(
                f"A fresh PUG is already active: match #{e.existing_match['id']}. "
                f"Join that one instead of starting a new one.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(f"Fresh PUG #{match_id} created.", ephemeral=True)


class MixRequestModal(ui.Modal, title="Request a mix"):
    team_name = ui.TextInput(label="Team name")
    division = ui.TextInput(label="Division")
    map_name = ui.TextInput(label="Map")
    server = ui.TextInput(label="Server preference", required=False)

    async def on_submit(self, interaction: discord.Interaction):
        request_id = await hosting_service.submit_request(
            interaction.user.id, str(self.team_name), str(self.division),
            str(self.map_name), str(self.server) or None,
        )

        thread = None
        if config.MIX_REQUESTS_CHANNEL_ID:
            requests_channel = interaction.client.get_channel(config.MIX_REQUESTS_CHANNEL_ID)
            if requests_channel:
                thread = await requests_channel.create_thread(
                    name=f"mix-request-{request_id}-{self.team_name}",
                    type=discord.ChannelType.public_thread,
                )
                await hosting_service.attach_thread(request_id, thread.id)
                await thread.send(
                    f"**Mix request #{request_id}** from {interaction.user.mention}\n"
                    f"Team: {self.team_name} | Division: {self.division} | Map: {self.map_name} | "
                    f"Server: {self.server or 'no preference'}\n\n"
                    f"{interaction.user.mention}, ping your whole team below (@player @player ...)."
                )

        if thread:
            await interaction.response.send_message(
                f"Request #{request_id} submitted. Head to {thread.mention} and ping your team.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "Request submitted, but the mix-requests channel isn't configured "
                "(MIX_REQUESTS_CHANNEL_ID) -- a hoster will need to be told manually.",
                ephemeral=True,
            )


class HostRequestChoiceView(ui.View):
    """The two-button picker shown when /host-request is run."""

    def __init__(self, timeout=120):
        super().__init__(timeout=timeout)

    @ui.button(label="Fresh PUG", style=discord.ButtonStyle.primary)
    async def fresh_pug(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(FreshPugModal())

    @ui.button(label="Request a mix", style=discord.ButtonStyle.secondary)
    async def request_mix(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(MixRequestModal())


class MixRequestReviewView(ui.View):
    """Shown via /manage when run inside a mix-request thread, once the
    requester has pinged their roster."""

    def __init__(self, request_id: int, timeout=None):
        super().__init__(timeout=timeout)
        self.request_id = request_id

    @ui.button(label="Accept mix req", style=discord.ButtonStyle.success, custom_id="mixreq_accept")
    async def accept(self, interaction: discord.Interaction, button: ui.Button):
        try:
            match_id = await hosting_service.approve_request(self.request_id, interaction.user.id)
        except hosting_service.AlreadyResolved as e:
            await interaction.response.send_message(f"Already handled: {e}", ephemeral=True)
            return

        from pingu.services import channel_service
        match = await matches_db.get_match(match_id)
        await channel_service.create_match_channels(
            interaction.guild, match_id, "mix",
            team_name=match["team_name"], division=match["division"],
        )

        if config.CAPTAIN_ROLE_ID:
            requester = interaction.guild.get_member(interaction.user.id)
            # Note: assumption -- assigning an actual Discord role to the
            # requester on top of the DB captain_id gating that already
            # powers /manage's captain view. If you don't want a visible
            # role for this, leave CAPTAIN_ROLE_ID unset in .env.
            role = interaction.guild.get_role(config.CAPTAIN_ROLE_ID)
            if requester and role:
                await requester.add_roles(role, reason=f"Captain of match #{match_id}")

        await interaction.response.send_message(
            f"Accepted. Match #{match_id} created — channels are up, requester is captain."
        )
        self.stop()

    @ui.button(label="Deny mix req", style=discord.ButtonStyle.danger, custom_id="mixreq_deny")
    async def deny(self, interaction: discord.Interaction, button: ui.Button):
        import pingu.db.host_requests as requests_db
        request = await requests_db.get_request(self.request_id)
        try:
            await hosting_service.deny_request(self.request_id, interaction.user.id)
        except hosting_service.AlreadyResolved as e:
            await interaction.response.send_message(f"Already handled: {e}", ephemeral=True)
            return

        await interaction.response.send_message(
            f"<@{request['requester_id']}> your mix request was denied by a hoster."
        )
        self.stop()
