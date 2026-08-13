"""
Covers the whole /host-request flow: the initial Fresh PUG / Request a mix
choice, the Fresh PUG quick-create modal, and the hoster's Accept/Deny/Edit
buttons used inside the mix-request thread (via /manage). "Request a mix"
itself reuses cogs/hosting.py's GameModeSelect -> DivisionSelect -> MixModal
chain (tagged for_request=True) rather than a separate modal, so a
requester sees exactly the same steps a hoster does.
"""

import discord
from discord import ui
import logging

log = logging.getLogger("hosting_views")

from pingu import config
from pingu.services import hosting_service, fresh_pug_service, channel_service
from pingu.db import matches as matches_db
from pingu.db import signups as signups_db
from pingu.db import host_requests as requests_db
from pingu.embeds import DIVISIONS, SIXS_DIVISIONS


class FreshPugModal(ui.Modal, title="Fresh PUG"):
    maps = ui.TextInput(label="Map(s)", placeholder="e.g. cp_process, cp_gullywash")
    server = ui.TextInput(label="Server", placeholder="Connect string or server name")

    async def on_submit(self, interaction: discord.Interaction):
        # Fresh pug creation does real work -- category/channel/VC
        # creation, message posting, thread creation -- well past
        # Discord's 3s response window. Must defer immediately, not
        # respond only at the end (that was the actual bug here: this
        # modal never deferred, so the interaction died before it ever
        # got a response).
        await interaction.response.defer(ephemeral=True)
        try:
            match_id = await fresh_pug_service.create(
                interaction.client, interaction.guild,
                interaction.user.id, interaction.user.display_name,
                str(self.maps), str(self.server),
            )
        except fresh_pug_service.FreshPugAlreadyActive as e:
            await interaction.followup.send(
                f"A fresh PUG is already active: match #{e.existing_match['id']}. "
                f"Join that one instead of starting a new one.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(f"Fresh PUG #{match_id} created and posted.", ephemeral=True)


class HostRequestChoiceView(ui.View):
    """The two-button picker shown when /host-request is run."""

    def __init__(self, bot, timeout=120):
        super().__init__(timeout=timeout)
        self.bot = bot

    @ui.button(label="Fresh PUG", style=discord.ButtonStyle.primary)
    async def fresh_pug(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(FreshPugModal())

    @ui.button(label="Request a mix", style=discord.ButtonStyle.secondary)
    async def request_mix(self, interaction: discord.Interaction, button: ui.Button):
        # Same wizard a hoster gets from /host, just tagged for_request=True
        # so it skips the match-type step (always "mix") and, on submit,
        # creates a pending request + thread instead of a live match.
        from pingu.cogs.hosting import GameModeSelect
        await interaction.response.edit_message(
            content="**Step 1 of 2:** Select the game mode.",
            view=GameModeSelect(self.bot, for_request=True),
        )


async def _post_accepted_mix(interaction: discord.Interaction, match_id: int):
    """
    Shared by the Accept button: posts the actual mix roster message,
    thread, pending/denied lists, ongoing-matches entry, and creates the
    dynamic per-team captain role. Returns the requester Member (or None).
    """
    match = await matches_db.get_match(match_id)

    result = await channel_service.create_match_channels(
        interaction.guild, match_id, "mix",
        team_name=match["team_name"], division=match["division"],
    )
    if not result:
        return None, "channel_error"
    channel_id, _ = result
    channel = interaction.client.get_channel(channel_id)

    from pingu.embeds import build_mix_message, build_pending_message, build_denied_message
    from pingu.views.signup_views import SignupView
    from pingu.cogs.hosting import thread_date_str, post_to_ongoing

    signups = await signups_db.get_signups_for_match(match_id)
    content = build_mix_message(match, signups, pug_role_id=config.PUG_ROLE_ID)
    view = SignupView(match_id)
    msg = await channel.send(content=content, view=view)
    await matches_db.set_message_id(match_id, msg.id, channel.id)
    log.info(f"Posted mix message for match #{match_id}: message_id={msg.id}, channel_id={channel.id} ({channel.name})")

    pending_msg = await channel.send(content=build_pending_message(match, signups))
    denied_msg = await channel.send(content=build_denied_message(match, signups))
    await matches_db.set_pending_msg_id(match_id, pending_msg.id)
    await matches_db.set_denied_msg_id(match_id, denied_msg.id)

    try:
        thread_name = f"{match['team_name']} vs Mix — {match['division']}, {thread_date_str(match['timestamp'])}"
        thread = await msg.create_thread(name=thread_name, auto_archive_duration=1440)
        await matches_db.set_thread_id(match_id, thread.id)
    except Exception:
        pass

    await post_to_ongoing(interaction.client, match_id, channel.id)

    # Dynamic per-team captain role -- created here, not a static
    # CAPTAIN_ROLE_ID, so it can be deleted cleanly on teardown without
    # touching anyone else's roles.
    requester = interaction.guild.get_member(match["captain_id"])
    if requester:
        try:
            captain_role = await interaction.guild.create_role(
                name=f"{match['team_name']} Captain",
                reason=f"Captain role for match #{match_id}",
            )
            await requester.add_roles(captain_role, reason=f"Captain of match #{match_id}")
            await matches_db.set_captain_role_id(match_id, captain_role.id)
            await channel_service.grant_captain_channel_access(interaction.guild, match_id, captain_role)
        except discord.HTTPException:
            pass

    return requester, None


class MixRequestReviewView(ui.View):
    """Shown via /manage when run inside a mix-request thread, once the
    requester has posted their roster."""

    def __init__(self, request_id: int, timeout=None):
        super().__init__(timeout=timeout)
        self.request_id = request_id

    @ui.button(label="Accept mix req", style=discord.ButtonStyle.success, custom_id="mixreq_accept")
    async def accept(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)

        try:
            match_id = await hosting_service.approve_request(self.request_id, interaction.user.id)
        except hosting_service.AlreadyResolved as e:
            await interaction.followup.send(f"Already handled: {e}", ephemeral=True)
            return

        requester, error = await _post_accepted_mix(interaction, match_id)
        if error:
            await interaction.followup.send(
                f"Match #{match_id} created, but couldn't create channels -- check MATCH_CATEGORY_ID in .env.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Accepted. Match #{match_id} created — channels are up, "
            f"{requester.mention if requester else 'the requester'} is captain.",
            ephemeral=True,
        )

        # The mix now lives in its own channel/thread -- the request
        # thread has served its purpose.
        if isinstance(interaction.channel, discord.Thread):
            parent = interaction.channel.parent
            thread_id = interaction.channel.id
            try:
                await interaction.channel.delete()
            except discord.HTTPException:
                pass

            # Discord auto-posts a "X started a thread: Y" system message
            # in the PARENT channel when a thread is created -- deleting
            # the thread itself does NOT remove that notification. For a
            # thread created directly on a channel (not from an existing
            # message), that system message shares the thread's own ID,
            # so this is usually a direct hit; the history-scan fallback
            # covers cases where that assumption doesn't hold.
            if parent:
                try:
                    sys_msg = await parent.fetch_message(thread_id)
                    await sys_msg.delete()
                except discord.HTTPException:
                    try:
                        async for msg in parent.history(limit=50):
                            if (
                                msg.type == discord.MessageType.thread_created
                                and msg.thread and msg.thread.id == thread_id
                            ):
                                await msg.delete()
                                break
                    except Exception:
                        pass

        self.stop()

    @ui.button(label="Deny mix req", style=discord.ButtonStyle.danger, custom_id="mixreq_deny")
    async def deny(self, interaction: discord.Interaction, button: ui.Button):
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

    @ui.button(label="Edit request", style=discord.ButtonStyle.secondary, custom_id="mixreq_edit")
    async def edit(self, interaction: discord.Interaction, button: ui.Button):
        view = MixRequestEditChoiceView(self.request_id)
        await interaction.response.send_message(
            "What would you like to edit?", view=view, ephemeral=True
        )


class MixRequestEditChoiceView(ui.View):
    def __init__(self, request_id: int, timeout=60):
        super().__init__(timeout=timeout)
        self.request_id = request_id

    @ui.button(label="Division", style=discord.ButtonStyle.secondary)
    async def edit_division(self, interaction: discord.Interaction, button: ui.Button):
        request = await requests_db.get_request(self.request_id)
        is_sixs = request["division"] in SIXS_DIVISIONS
        options = [discord.SelectOption(label=d, value=d) for d in (SIXS_DIVISIONS if is_sixs else DIVISIONS)]
        view = MixRequestDivisionEditView(self.request_id, options)
        await interaction.response.edit_message(content="Select the new division:", view=view)

    @ui.button(label="Details", style=discord.ButtonStyle.secondary)
    async def edit_details(self, interaction: discord.Interaction, button: ui.Button):
        request = await requests_db.get_request(self.request_id)
        await interaction.response.send_modal(MixRequestEditModal(self.request_id, request))


class MixRequestDivisionEditView(ui.View):
    def __init__(self, request_id: int, options, timeout=60):
        super().__init__(timeout=timeout)
        self.request_id = request_id
        select = ui.Select(placeholder="Select division…", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        division = interaction.data["values"][0]
        await requests_db.update_request_fields(self.request_id, division=division)
        await interaction.response.edit_message(
            content=f"✅ Division updated to **{division}**.", view=None
        )
        request = await requests_db.get_request(self.request_id)
        if request and request["thread_id"]:
            thread = interaction.client.get_channel(request["thread_id"])
            if thread:
                try:
                    await thread.send(f"📋 Division updated to **{division}** by a hoster.")
                except Exception:
                    pass


class MixRequestEditModal(ui.Modal, title="Edit Mix Request"):
    """
    Same fields/labels/placeholders as MixModal (cogs/hosting.py) -- the
    exact form a hoster or requester would see when creating a mix --
    pre-populated here with the request's current values.
    """
    team_name_input = ui.TextInput(
        label="Host team name",
        placeholder="e.g. GAY BLACK MEN",
        style=discord.TextStyle.short,
        required=True, max_length=40,
    )
    datetime_input = ui.TextInput(
        label="Date & Time (GMT+8, DD/MM/YY)",
        placeholder="e.g.  25/3/25 8:00 PM  or  5/3/25 9PM",
        style=discord.TextStyle.short,
        required=True,
    )
    map_input = ui.TextInput(
        label="Map (leave blank for TBC)",
        placeholder="e.g. cp_process_final",
        style=discord.TextStyle.short,
        required=False, max_length=60,
    )
    server_input = ui.TextInput(
        label="Server & Location",
        placeholder="e.g. Matcha Singapore  or  Serveme Europe",
        style=discord.TextStyle.short,
        required=True, max_length=80,
    )

    def __init__(self, request_id: int, request):
        super().__init__()
        self.request_id = request_id
        self.team_name_input.default = request["team_name"] or ""
        self.map_input.default = request["map_name"] or ""
        self.server_input.default = request["server"] or "Matcha Singapore"
        if request["timestamp"]:
            from pingu.cogs.hosting import format_datetime_for_input
            self.datetime_input.default = format_datetime_for_input(request["timestamp"])

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        from pingu.cogs.hosting import parse_datetime, DATETIME_HINT
        import time

        unix = parse_datetime(self.datetime_input.value.strip())
        if unix is None:
            await interaction.followup.send(f"❌ Couldn't parse that date/time.\n{DATETIME_HINT}", ephemeral=True)
            return
        if unix < time.time():
            await interaction.followup.send("❌ That date/time is in the past.", ephemeral=True)
            return

        team_name = self.team_name_input.value.strip()
        map_name = self.map_input.value.strip() or "tbc"
        server = self.server_input.value.strip()

        await requests_db.update_request_fields(
            self.request_id, team_name=team_name, timestamp=unix, map_name=map_name, server=server,
        )

        await interaction.followup.send("✅ Request updated.", ephemeral=True)

        request = await requests_db.get_request(self.request_id)
        if request and request["thread_id"]:
            thread = interaction.client.get_channel(request["thread_id"])
            if thread:
                try:
                    await thread.send(
                        f"📋 Request details updated by a hoster:\n"
                        f"Team: {team_name} | Map: {map_name} | Server: {server} | Date: <t:{unix}:F>"
                    )
                except Exception:
                    pass