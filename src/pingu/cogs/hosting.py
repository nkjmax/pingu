"""
Ported faithfully from the original bot's schedule.py -- the entire
/host wizard (game mode -> match type -> division -> modal), /edit,
/connect-string, and /ping. Every class, message string, and threshold
kept exactly as-is. Config access converted from bot.config.get("key")
to this project's config.py attributes; db access converted from the
flat db module to db.matches / db.signups. Imports of SignupView,
OPugSignupView, SixsSignupView, SixsSplitView, FreshPugSignupView point
to pingu.views.legacy -- the original views.py port, landing next.
"""

import re
import time
import discord
from discord import app_commands, ui
from discord.ext import commands
from dateutil.tz import gettz
from datetime import datetime

from pingu import config
from pingu.db import matches as matches_db
from pingu.db import signups as signups_db
from pingu.services import channel_service
from pingu.templates.roster_instructions import roster_instructions_block
from pingu.embeds import (
    build_mix_message, build_match_embed, build_ongoing_line, build_fresh_pug_message, build_opug_message,
    build_6s_fresh_pug_message, build_6s_opug_message, build_6s_mix_message,
    FP_DIVISIONS, OPUG_DIVISIONS, SIXS_DIVISIONS, SIXS_CLASSES, SIXS_CLASS_EMOJI,
    build_archive_message, DIVISIONS, TF2_CLASSES, CLASS_EMOJI, build_pending_message, build_denied_message,
    build_fresh_pug_signup_list,
)

DEFAULT_TZ    = "Asia/Singapore"  # GMT+8
DATETIME_HINT = (
    "Format: DD/MM/YY or DD/MM/YYYY HH:MM AM/PM (GMT+8)\n"
    "e.g. `25/3/25 8:00 PM`  `5/3/2025 9PM`  `5/3/25 9pm`  `5/3/25 9 pm`"
)

DT_RE = re.compile(
    r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)$",
    re.IGNORECASE
)

CONNECT_RE = re.compile(r"connect\s+([\d.]+:\d+);\s*password\s+\"([^\"]+)\"", re.IGNORECASE)
SDR_RE     = re.compile(r"connect\s+(169\.254[\d.]+:\d+);\s*password\s+\"([^\"]+)\"", re.IGNORECASE)
TV_RE      = re.compile(r"connect\s+([\d.]+:270\d\d)(?:\s|$)", re.IGNORECASE)


def parse_datetime(raw):
    m = DT_RE.match(raw.strip())
    if not m:
        return None
    day, mon, yr, hour, minute, ampm = m.groups()
    day  = int(day)
    mon  = int(mon)
    yr   = int(yr)
    year = yr if yr >= 100 else 2000 + yr
    hour   = int(hour)
    minute = int(minute) if minute else 0
    ampm   = ampm.upper()
    if ampm == "PM" and hour != 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    try:
        dt = datetime(year, mon, day, hour, minute, tzinfo=gettz(DEFAULT_TZ))
        return int(dt.timestamp())
    except Exception:
        return None


def format_datetime_for_input(unix_timestamp):
    """Inverse of parse_datetime -- turns a stored timestamp back into the
    'DD/MM/YY H:MM AM/PM' text a modal's date field expects, for
    pre-populating an edit form with the current value."""
    try:
        dt = datetime.fromtimestamp(unix_timestamp, tz=gettz(DEFAULT_TZ))
        hour = dt.hour
        ampm = "AM" if hour < 12 else "PM"
        hour12 = hour % 12 or 12
        return f"{dt.day}/{dt.month}/{dt.year % 100} {hour12}:{dt.minute:02d} {ampm}"
    except Exception:
        return ""


def parse_connect_message(text):
    """Parse a forwarded server-bot message. Returns dict with keys: connect, sdr, tv."""
    result = {}
    all_connects = CONNECT_RE.findall(text)
    sdr_connects = SDR_RE.findall(text)

    sdr_ips = {ip for ip, _ in sdr_connects}

    for ip, pw in all_connects:
        if ip in sdr_ips:
            result["sdr"] = f'connect {ip}; password "{pw}"'
        else:
            result["connect"] = f'connect {ip}; password "{pw}"'

    tv_matches = TV_RE.findall(text)
    known = {ip for ip, _ in all_connects}
    for ip in tv_matches:
        if ip not in known:
            result["tv"] = f"connect {ip}"
            break

    return result


def thread_name_for_match(match):
    """Build the thread name for a match, including date."""
    t    = match["type"]
    date = thread_date_str(match["timestamp"])
    div  = match["division"] or ""
    team = match["team_name"] or "Mix"
    if t in ("opug", "6s_opug"):
        return f"{div} PUG \u2014 signup thread, {date}"
    elif t == "6s_mix":
        return f"{team} vs Mix 6s \u2014 {div}, {date}"
    elif t in ("fresh_pug", "6s_fresh_pug"):
        label = "FRESH PUG 6v6" if t == "6s_fresh_pug" else "FRESH PUG"
        return f"{label}, {date}"
    else:
        return f"{team} vs Mix \u2014 {div}, {date}"


def parse_class_ordered_roster(content: str) -> str:
    """
    The exact same parsing hosters use for their own roster: a comma-
    separated list, positionally matched to class order when displayed
    (see embeds.build_mix_message splitting on '\\n' and zipping against
    TF2_CLASSES/SIXS_CLASSES). Shared between the hoster-created mix flow
    (main.py's pending-roster handler) and the mix-request captain flow
    (cogs/host_request.py's thread listener) so both produce an identical
    host_roster format.
    """
    entries = [e.strip() for e in content.split(",") if e.strip()]
    return "\n".join(entries)


def thread_date_str(unix_timestamp):
    """Format a Unix timestamp as SGT date/time for thread names, e.g. '25 Mar 9PM'."""
    try:
        dt = datetime.fromtimestamp(unix_timestamp, tz=gettz(DEFAULT_TZ))
        hour = dt.hour
        ampm = "AM" if hour < 12 else "PM"
        hour12 = hour % 12 or 12
        minute = dt.minute
        time_str = f"{hour12}:{minute:02d}{ampm}" if minute else f"{hour12}{ampm}"
        month = dt.strftime("%b")
        return f"{dt.day} {month} {time_str}"
    except Exception:
        return ""


def is_hoster(interaction):
    if not config.HOSTER_ROLE_ID:
        return True
    return any(r.id == config.HOSTER_ROLE_ID for r in interaction.user.roles)


def is_fresh_pug_owner(interaction, match):
    """A fresh pug's own creator can /manage, /edit, /connect-string, and
    /ping it -- but ONLY that one fresh pug, never any other match.
    /host-request's fresh pug path is open to anyone, not just hosters,
    so its creator isn't necessarily a hoster at all -- this is the
    narrow exception."""
    return match["type"] in ("fresh_pug", "6s_fresh_pug") and match["created_by"] == interaction.user.id


async def post_to_ongoing(bot, match_id, channel_id):
    ongoing_channel = bot.get_channel(bot.ongoing_channel)
    if not ongoing_channel:
        return
    match   = await matches_db.get_match(match_id)
    signups = await signups_db.get_signups_for_match(match_id)
    line    = build_ongoing_line(match, channel_id=channel_id, signups=signups if match["type"] in ("mix", "opug", "6s_mix", "6s_opug") else None)
    msg     = await ongoing_channel.send(line)
    await matches_db.set_ongoing_msg_id(match_id, msg.id)


async def refresh_ongoing_line(bot, match_id):
    """Update the ongoing-matches line whenever the mix roster changes."""
    match = await matches_db.get_match(match_id)
    if not match or not match["ongoing_msg_id"]:
        return
    ongoing_channel = bot.get_channel(bot.ongoing_channel)
    if not ongoing_channel:
        return
    try:
        msg     = await ongoing_channel.fetch_message(match["ongoing_msg_id"])
        signups = await signups_db.get_signups_for_match(match_id)
        line    = build_ongoing_line(match, channel_id=match["channel_id"], signups=signups if match["type"] in ("mix", "opug", "6s_mix", "6s_opug") else None)
        await msg.edit(content=line)
    except Exception:
        pass


# ── Step 1: Game mode select ─────────────────────────────────────────────────

class GameModeSelect(ui.View):
    def __init__(self, bot, for_request=False):
        super().__init__(timeout=60)
        self.bot = bot
        self.for_request = for_request
        select = ui.Select(
            placeholder="Select game mode\u2026",
            options=[
                discord.SelectOption(label="Highlander", value="hl"),
                discord.SelectOption(label="6s", value="6s"),
            ] if for_request else [
                discord.SelectOption(label="Highlander", value="hl"),
                discord.SelectOption(label="6s", value="6s"),
                discord.SelectOption(label="Ultiduo", value="ultiduo"),
            ]
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction):
        mode = interaction.data["values"][0]
        if mode == "ultiduo":
            await interaction.response.edit_message(
                content="\U0001f6a7 **Ultiduo is currently under development.** Check back later!",
                view=None,
            )
            return

        # Requesting a mix always skips the match-type step -- it's always
        # "mix" -- straight to division select.
        if self.for_request:
            step_label = "**Step 2 of 2:** Select the division."
            if mode == "6s":
                view = SixsDivisionSelect(self.bot, for_request=True)
            else:
                view = DivisionSelect(self.bot, for_request=True)
            await interaction.response.edit_message(content=step_label, view=view)
            return

        if mode == "6s":
            view = SixsMatchTypeSelect(self.bot)
            await interaction.response.edit_message(
                content="**Step 2 of 3:** What type of match?",
                view=view,
            )
            return
        view = MatchTypeSelect(self.bot)
        await interaction.response.edit_message(
            content="**Step 2 of 3:** What type of match?",
            view=view,
        )


# ── Step 2: Match type select ─────────────────────────────────────────────────

class MatchTypeSelect(ui.View):
    def __init__(self, bot):
        super().__init__(timeout=60)
        self.bot = bot
        select = ui.Select(
            placeholder="Select match type\u2026",
            options=[
                discord.SelectOption(label="Mix", value="mix", description="One team is decided, 9 sign-ups"),
                discord.SelectOption(label="Organised PUG", value="opug", description="Host organised PUG \u2014 you balance the teams"),
                discord.SelectOption(label="Fresh PUG", value="fpug", description="Host a PUG once you hit 18 reacts"),
            ]
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction):
        match_type = interaction.data["values"][0]
        if match_type == "opug":
            view = OPugDivisionSelect(self.bot)
            await interaction.response.edit_message(
                content="**Step 3 of 3:** Select the division.",
                view=view,
            )
            return
        if match_type == "fpug":
            existing_fp = await matches_db.get_active_fresh_pug()
            if existing_fp:
                await interaction.response.edit_message(
                    content=f"\u274c There's already a Fresh PUG happening (match #{existing_fp['id']}). Please conclude or cancel it first.",
                    view=None,
                )
                return
            await interaction.response.send_modal(FreshPugModal(self.bot))
            return
        view = DivisionSelect(self.bot)
        await interaction.response.edit_message(
            content="**Step 3 of 3:** Select the division.",
            view=view,
        )


# ── Step 3: Division select ───────────────────────────────────────────────────

class DivisionSelect(ui.View):
    def __init__(self, bot, for_request=False):
        super().__init__(timeout=60)
        self.bot = bot
        self.for_request = for_request
        options  = [discord.SelectOption(label=d, value=d) for d in DIVISIONS]
        select   = ui.Select(placeholder="Select division\u2026", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction):
        division = interaction.data["values"][0]
        await interaction.response.send_modal(
            MixModal(self.bot, division, interaction.message, for_request=self.for_request)
        )


# ── Mix modal ─────────────────────────────────────────────────────────────────

class MixModal(ui.Modal, title="Schedule a Mix"):
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
        default="Matcha Singapore",
        style=discord.TextStyle.short,
        required=True, max_length=80,
    )

    def __init__(self, bot, division, origin_message=None, for_request=False):
        super().__init__()
        self.bot            = bot
        self.division       = division
        self.origin_message = origin_message
        self.for_request    = for_request

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)

        unix      = parse_datetime(self.datetime_input.value.strip())
        team_name = self.team_name_input.value.strip()

        if unix is None:
            await interaction.followup.send(
                f"\u274c Couldn't parse that date/time.\n{DATETIME_HINT}", ephemeral=True
            )
            return
        if unix < time.time():
            await interaction.followup.send(
                "\u274c That date/time is in the past.", ephemeral=True
            )
            return

        map_name = self.map_input.value.strip() or "tbc"
        server   = self.server_input.value.strip()

        if self.for_request:
            if unix > time.time() + (7 * 86400):
                await interaction.followup.send(
                    "\u274c There's a 1 week limit on mix requests \u2014 please pick a date within the next 7 days.",
                    ephemeral=True,
                )
                return
            await self._submit_as_request(interaction, unix, team_name, map_name, server)
            return

        notes       = None
        pug_role_id = config.PUG_ROLE_ID

        match_id = await matches_db.create_match(
            type_="mix", timestamp=unix,
            created_by=interaction.user.id,
            created_by_name=interaction.user.display_name,
            team_name=team_name, notes=notes,
            division=self.division, map_name=map_name,
            server=server, pug_role_id=pug_role_id,
        )

        result = await channel_service.create_match_channels(
            interaction.guild, match_id, "mix", team_name=team_name, division=self.division
        )
        if not result:
            await interaction.followup.send(
                "\u274c Could not create match channel \u2014 check MATCH_CATEGORY_ID in .env.", ephemeral=True
            )
            return
        channel_id, _ = result
        channel = self.bot.get_channel(channel_id)

        if self.origin_message:
            try:
                await self.origin_message.delete()
            except Exception:
                pass

        await interaction.followup.send(
            f"\u2705 Match created! Now **type your host team roster in {channel.mention}**\n\n"
            f"{roster_instructions_block(is_sixs=False)}",
            ephemeral=True,
        )
        interaction.client._pending_roster[interaction.user.id] = {
            "match_id":       match_id,
            "channel_id":     channel.id,
            "bot":            self.bot,
            "expires":        time.time() + 300,
            "roster_interaction": interaction,
        }

    async def _submit_as_request(self, interaction, unix, team_name, map_name, server):
        """Non-hoster path: creates a pending host_requests row and a
        thread for roster collection instead of a live match."""
        from pingu import config as _config
        from pingu.services import hosting_service

        request_id = await hosting_service.submit_request(
            interaction.user.id, team_name, self.division, map_name, server, unix,
        )

        thread = None
        if _config.MIX_REQUESTS_CHANNEL_ID:
            requests_channel = interaction.client.get_channel(_config.MIX_REQUESTS_CHANNEL_ID)
            if requests_channel:
                thread = await requests_channel.create_thread(
                    name=f"mix-request-{request_id}-{team_name}",
                    type=discord.ChannelType.public_thread,
                )
                await hosting_service.attach_thread(request_id, thread.id)
                await thread.send(
                    f"**Mix request #{request_id}** from {interaction.user.mention}\n"
                    f"Team: {team_name} | Division: {self.division} | Map: {map_name} | "
                    f"Server: {server or 'no preference'} | Date: <t:{unix}:F>\n\n"
                    f"{interaction.user.mention}, post your team's roster below.\n\n"
                    f"{roster_instructions_block(is_sixs=False)}"
                )

        if self.origin_message:
            try:
                await self.origin_message.delete()
            except Exception:
                pass

        if thread:
            await interaction.followup.send(
                f"Request #{request_id} submitted. Head to {thread.mention} and post your roster.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "Request submitted, but the mix-requests channel isn't configured "
                "(MIX_REQUESTS_CHANNEL_ID) -- a hoster will need to be told manually.",
                ephemeral=True,
            )


# ── Edit modal ────────────────────────────────────────────────────────────────

class EditMixModal(ui.Modal, title="Edit Match Details"):
    team_name_input = ui.TextInput(
        label="Team name \u2014 blank to keep current",
        style=discord.TextStyle.short,
        required=False, max_length=40,
    )
    datetime_input = ui.TextInput(
        label="Date & Time (DD/MM/YY) \u2014 blank to keep",
        placeholder="e.g.  25/3/25 8:00 PM",
        style=discord.TextStyle.short,
        required=False,
    )
    map_input = ui.TextInput(
        label="Map \u2014 blank to keep current",
        style=discord.TextStyle.short,
        required=False, max_length=60,
    )
    server_input = ui.TextInput(
        label="Server & Location \u2014 blank to keep current",
        style=discord.TextStyle.short,
        required=False, max_length=80,
    )

    def __init__(self, match_id, bot):
        super().__init__()
        self.match_id = match_id
        self.bot      = bot

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)

        updates = {}
        if self.team_name_input.value.strip():
            updates["team_name"] = self.team_name_input.value.strip()
        if self.datetime_input.value.strip():
            unix = parse_datetime(self.datetime_input.value.strip())
            if unix is None:
                await interaction.followup.send(
                    f"\u274c Couldn't parse date/time.\n{DATETIME_HINT}", ephemeral=True
                )
                return
            if unix < time.time():
                await interaction.followup.send(
                    "\u274c That date/time is in the past.", ephemeral=True
                )
                return
            updates["timestamp"] = unix
        if self.map_input.value.strip():
            updates["map_name"] = self.map_input.value.strip()
        if self.server_input.value.strip():
            updates["server"] = self.server_input.value.strip()

        if not updates:
            await interaction.followup.send(
                "No changes \u2014 all fields were blank.", ephemeral=True
            )
            return

        match = await matches_db.get_match(self.match_id)
        if not match or match["ended"]:
            await interaction.followup.send(
                "\u274c This match has already ended or been cancelled.", ephemeral=True
            )
            return

        await matches_db.update_match_fields(self.match_id, **updates)

        match   = await matches_db.get_match(self.match_id)
        signups = await signups_db.get_signups_for_match(self.match_id)
        pug_role_id = config.PUG_ROLE_ID

        try:
            channel = interaction.client.get_channel(match["channel_id"])
            msg     = await channel.fetch_message(match["message_id"])
            if match["type"] == "6s_mix":
                content = build_6s_mix_message(match, signups, pug_role_id=pug_role_id)
            elif match["type"] == "6s_opug":
                content = build_6s_opug_message(match, signups, pug_role_id=pug_role_id)
            elif match["type"] == "opug":
                content = build_opug_message(match, signups, pug_role_id=pug_role_id)
            else:
                content = build_mix_message(match, signups, pug_role_id=pug_role_id)
            await msg.edit(content=content)
        except Exception as e:
            await interaction.followup.send(
                f"\u26a0\ufe0f Details saved but couldn't refresh message: {e}", ephemeral=True
            )
            return

        if match["thread_id"] and ("timestamp" in updates or "team_name" in updates):
            try:
                thread = interaction.client.get_channel(match["thread_id"])
                if thread:
                    await thread.edit(name=thread_name_for_match(match))
            except Exception:
                pass

        await interaction.followup.send("\u2705 Match details updated.", ephemeral=True)


class EditFreshPugModal(ui.Modal, title="Edit Fresh PUG"):
    """
    Fresh pugs have no team name, no scheduled date/time, no server field
    (always None by design), and now no division either -- reusing
    EditMixModal's fields for them was the actual bug this replaces: it
    asked for several inapplicable fields, and on save it fell into
    EditMixModal's "anything not 6s_mix/6s_opug/opug defaults to
    build_mix_message" branch, rebuilding the message with the wrong
    builder entirely. This modal only has what a fresh pug actually has:
    its map.
    """
    map_input = ui.TextInput(
        label="Maps \u2014 blank to keep current",
        style=discord.TextStyle.short,
        required=False, max_length=120,
    )

    def __init__(self, match_id, bot):
        super().__init__()
        self.match_id = match_id
        self.bot      = bot

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)

        new_map = self.map_input.value.strip()
        if not new_map:
            await interaction.followup.send("No changes \u2014 map field was blank.", ephemeral=True)
            return

        match = await matches_db.get_match(self.match_id)
        if not match or match["ended"]:
            await interaction.followup.send(
                "\u274c This Fresh PUG has already ended or been cancelled.", ephemeral=True
            )
            return

        await matches_db.update_match_fields(self.match_id, map_name=new_map)

        match = await matches_db.get_match(self.match_id)
        pug_role_id = config.PUG_ROLE_ID

        try:
            channel = interaction.client.get_channel(match["channel_id"])
            msg     = await channel.fetch_message(match["message_id"])
            if match["type"] == "6s_fresh_pug":
                content = build_6s_fresh_pug_message(match, pug_role_id=pug_role_id)
            else:
                content = build_fresh_pug_message(match, pug_role_id=pug_role_id)
            await msg.edit(content=content)
        except Exception as e:
            await interaction.followup.send(
                f"\u26a0\ufe0f Map saved but couldn't refresh message: {e}", ephemeral=True
            )
            return

        await interaction.followup.send("\u2705 Fresh PUG map updated.", ephemeral=True)


# ── Edit select ───────────────────────────────────────────────────────────────

class EditSelectView(ui.View):
    def __init__(self, match_id, bot):
        super().__init__(timeout=60)
        self.match_id = match_id
        self.bot      = bot
        select = ui.Select(
            placeholder="Select what to edit...",
            options=[
                discord.SelectOption(label="Match details", value="match", description="Time, map, server"),
                discord.SelectOption(label="Division", value="division", description="Change the division"),
                discord.SelectOption(label="Host team roster", value="roster", description="Edit a class slot"),
            ]
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction):
        choice  = interaction.data["values"][0]
        match   = await matches_db.get_match(self.match_id)
        is_sixs = match["type"] in ("6s_mix", "6s_opug") if match else False
        is_opug = match["type"] in ("opug", "6s_opug") if match else False
        is_fresh_pug = match["type"] in ("fresh_pug", "6s_fresh_pug") if match else False

        if choice == "match":
            if is_fresh_pug:
                await interaction.response.send_modal(EditFreshPugModal(self.match_id, self.bot))
            else:
                await interaction.response.send_modal(EditMixModal(self.match_id, self.bot))
        elif choice == "division":
            if is_opug:
                await interaction.response.edit_message(
                    content="\u274c Division cannot be edited for Organised PUGs.", view=None
                )
                return
            if is_fresh_pug:
                await interaction.response.edit_message(
                    content="\u274c Fresh PUGs don't have a division.", view=None
                )
                return
            if is_sixs:
                options = [discord.SelectOption(label=d, value=d) for d in SIXS_DIVISIONS]
            elif match["type"] == "mix":
                options = [discord.SelectOption(label=d, value=d) for d in DIVISIONS]
            else:
                options = [discord.SelectOption(label=d, value=d) for d in FP_DIVISIONS]
            view = EditDivisionView(self.match_id, self.bot, options)
            await interaction.response.edit_message(
                content="Select the new division:", view=view
            )
        else:
            if is_opug or is_fresh_pug:
                reason = "Organised PUGs" if is_opug else "Fresh PUGs"
                await interaction.response.edit_message(
                    content=f"\u274c {reason} don't have a host team roster to edit.", view=None
                )
                return
            view = EditRosterClassSelect(self.match_id, self.bot, is_sixs=is_sixs)
            await interaction.response.edit_message(
                content="Which class slot would you like to edit?", view=view
            )


class EditDivisionView(ui.View):
    def __init__(self, match_id, bot, options):
        super().__init__(timeout=60)
        self.match_id = match_id
        self.bot      = bot
        select = ui.Select(placeholder="Select division\u2026", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction):
        await interaction.response.defer(ephemeral=True)
        division = interaction.data["values"][0]
        match    = await matches_db.get_match(self.match_id)
        if not match or match["ended"]:
            await interaction.followup.send("\u274c This match has already ended or been cancelled.", ephemeral=True)
            return
        await matches_db.update_match_fields(self.match_id, division=division)
        match   = await matches_db.get_match(self.match_id)
        signups = await signups_db.get_signups_for_match(self.match_id)
        pug_role_id = config.PUG_ROLE_ID
        try:
            channel = interaction.client.get_channel(match["channel_id"])
            msg     = await channel.fetch_message(match["message_id"])
            if match["type"] == "6s_mix":
                content = build_6s_mix_message(match, signups, pug_role_id=pug_role_id)
            else:
                content = build_mix_message(match, signups, pug_role_id=pug_role_id)
            await msg.edit(content=content)
        except Exception as e:
            await interaction.followup.send(f"\u26a0\ufe0f Saved but couldn't refresh message: {e}", ephemeral=True)
            return

        if match["thread_id"]:
            try:
                thread = interaction.client.get_channel(match["thread_id"])
                if thread:
                    await thread.edit(name=thread_name_for_match(match))
            except Exception:
                pass

        await interaction.followup.send(f"\u2705 Division updated to **{division}**.", ephemeral=True)


# ── Edit roster class select ──────────────────────────────────────────────────

class EditRosterClassSelect(ui.View):
    def __init__(self, match_id, bot, is_sixs=False):
        super().__init__(timeout=60)
        self.match_id = match_id
        self.bot      = bot
        self.is_sixs  = is_sixs
        class_list  = SIXS_CLASSES if is_sixs else TF2_CLASSES
        emoji_map   = SIXS_CLASS_EMOJI if is_sixs else CLASS_EMOJI
        options = [
            discord.SelectOption(label=cls, value=cls, emoji=emoji_map[cls])
            for cls in class_list
        ]
        select = ui.Select(placeholder="Select class to edit\u2026", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction):
        cls = interaction.data["values"][0]
        interaction.client._pending_roster[interaction.user.id] = {
            "match_id":   self.match_id,
            "channel_id": interaction.channel_id,
            "edit_class": cls,
            "expires":    time.time() + 120,
        }
        await interaction.response.edit_message(
            content=f"Type the player for **{cls}** in this channel \u2014 @mention or plain text.",
            view=None,
        )


# ── Roster prompt view ────────────────────────────────────────────────────────

class RosterPromptView(ui.View):
    def __init__(self, match_id, bot):
        super().__init__(timeout=300)
        self.match_id = match_id
        self.bot      = bot

    @ui.button(label="Fill in host team roster", style=discord.ButtonStyle.primary)
    async def fill_roster(self, interaction, button):
        await interaction.response.send_modal(TeamRosterModal(self.match_id, self.bot))

    @ui.button(label="Skip for now", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction, button):
        await interaction.response.edit_message(
            content="Skipped. You can fill in the roster later with /edit-match.", view=None
        )


# ── Team roster modal ─────────────────────────────────────────────────────────

class TeamRosterModal(ui.Modal, title="Host Team Roster"):
    roster_input = ui.TextInput(
        label="Players in class order (one per line)",
        placeholder=(
            "Scout: @player1\n"
            "Soldier: @player2\n"
            "Pyro: @player3\n"
            "Demo: @player4\n"
            "Heavy: @player5\n"
            "Engi: @player6\n"
            "Medic: @player7\n"
            "Sniper: @player8\n"
            "Spy: mugen (not in server)"
        ),
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, match_id, bot):
        super().__init__()
        self.match_id = match_id
        self.bot      = bot

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)

        raw = self.roster_input.value.strip()

        lines = []
        for line in raw.splitlines():
            cleaned = re.sub(r"^[^:]+:\s*", "", line).strip()
            lines.append(cleaned)

        roster_str = "\n".join(lines) if lines else None
        await matches_db.update_match_fields(self.match_id, host_roster=roster_str)

        match   = await matches_db.get_match(self.match_id)
        signups = await signups_db.get_signups_for_match(self.match_id)
        pug_role_id = config.PUG_ROLE_ID
        try:
            channel = interaction.client.get_channel(match["channel_id"])
            msg     = await channel.fetch_message(match["message_id"])
            await msg.edit(content=build_mix_message(match, signups, pug_role_id=pug_role_id))
        except Exception as e:
            await interaction.followup.send(f"\u26a0\ufe0f Saved but couldn't refresh message: {e}", ephemeral=True)
            return

        await interaction.followup.send("\u2705 Host team roster updated!", ephemeral=True)


# ── Organised PUG division select ────────────────────────────────────────────

class OPugDivisionSelect(ui.View):
    def __init__(self, bot):
        super().__init__(timeout=60)
        self.bot = bot
        options  = [discord.SelectOption(label=d, value=d) for d in OPUG_DIVISIONS]
        select   = ui.Select(placeholder="Select division\u2026", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction):
        division = interaction.data["values"][0]
        await interaction.response.send_modal(OPugModal(self.bot, division))


# ── Organised PUG modal ───────────────────────────────────────────────────────

class OPugModal(ui.Modal, title="Schedule an Organised PUG"):
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
        required=False,
        max_length=80,
    )
    server_input = ui.TextInput(
        label="Server & Location",
        default="Matcha Singapore",
        style=discord.TextStyle.short,
        required=True,
        max_length=80,
    )

    def __init__(self, bot, division):
        super().__init__()
        self.bot        = bot
        self.division   = division

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)

        unix = parse_datetime(self.datetime_input.value.strip())
        if unix is None:
            await interaction.followup.send(
                f"\u274c Couldn't parse that date/time.\n{DATETIME_HINT}", ephemeral=True
            )
            return
        if unix < time.time():
            await interaction.followup.send(
                "\u274c That date/time is in the past.", ephemeral=True
            )
            return

        map_name    = self.map_input.value.strip() or "tbc"
        server      = self.server_input.value.strip()
        pug_role_id = config.PUG_ROLE_ID

        match_id = await matches_db.create_match(
            type_="opug", timestamp=unix,
            created_by=interaction.user.id,
            created_by_name=interaction.user.display_name,
            team_name=None, notes=None,
            division=self.division, map_name=map_name,
            server=server, pug_role_id=pug_role_id,
        )

        result = await channel_service.create_match_channels(
            interaction.guild, match_id, "opug", division=self.division
        )
        if not result:
            await interaction.followup.send(
                "\u274c Could not create PUG channel \u2014 check MATCH_CATEGORY_ID in .env.", ephemeral=True
            )
            return
        channel_id, _ = result
        channel = self.bot.get_channel(channel_id)

        match = await matches_db.get_match(match_id)
        from pingu.views.signup_views import OPugSignupView
        content = build_opug_message(match, [], pug_role_id=pug_role_id)
        view    = OPugSignupView(match_id)
        msg     = await channel.send(content=content, view=view)
        await matches_db.set_message_id(match_id, msg.id, channel.id)

        pending_msg = await channel.send(content=build_pending_message(match, []))
        denied_msg  = await channel.send(content=build_denied_message(match, []))
        await matches_db.set_pending_msg_id(match_id, pending_msg.id)
        await matches_db.set_denied_msg_id(match_id, denied_msg.id)

        try:
            thread = await msg.create_thread(
                name=f"{self.division} PUG \u2014 signup thread, {thread_date_str(unix)}",
                auto_archive_duration=1440,
            )
            await matches_db.set_thread_id(match_id, thread.id)
        except Exception:
            pass

        await post_to_ongoing(self.bot, match_id, channel.id)
        await interaction.followup.send(
            f"\u2705 {self.division} PUG posted to {channel.mention}!", ephemeral=True
        )


# ── 6s Match Type Select ──────────────────────────────────────────────────────

class SixsMatchTypeSelect(ui.View):
    def __init__(self, bot):
        super().__init__(timeout=60)
        self.bot = bot
        select = ui.Select(
            placeholder="Select match type\u2026",
            options=[
                discord.SelectOption(label="Mix", value="mix", description="One team is decided, 6 sign-ups"),
                discord.SelectOption(label="Organised PUG", value="opug", description="Host organised PUG \u2014 you balance the teams"),
                discord.SelectOption(label="Fresh PUG", value="fpug", description="Host a PUG once you hit 12 reacts"),
            ]
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction):
        match_type = interaction.data["values"][0]
        if match_type == "fpug":
            existing = await matches_db.get_active_6s_fresh_pug()
            if existing:
                await interaction.response.edit_message(
                    content=f"\u274c There's already a 6s Fresh PUG happening (match #{existing['id']}). Please conclude or cancel it first.",
                    view=None,
                )
                return
            await interaction.response.send_modal(SixsFreshPugModal(self.bot))
        elif match_type == "opug":
            view = SixsOPugDivisionSelect(self.bot)
            await interaction.response.edit_message(content="**Step 3 of 3:** Select the division.", view=view)
        else:
            view = SixsDivisionSelect(self.bot)
            await interaction.response.edit_message(content="**Step 3 of 3:** Select the division.", view=view)


# ── 6s Fresh PUG ──────────────────────────────────────────────────────────────

class SixsFreshPugModal(ui.Modal, title="Schedule a 6s Fresh PUG"):
    map_input = ui.TextInput(label="Maps (leave blank for TBC)", placeholder="e.g. cp_process_final", style=discord.TextStyle.short, required=False, max_length=120)

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        # 6s fresh pugs have no scheduled time -- they're assumed to
        # happen as soon as enough people sign up. timestamp is still a
        # non-nullable column other things key off, so it's set to "now"
        # internally, but never parsed from input or shown to anyone.
        unix = int(time.time())
        map_name    = self.map_input.value.strip() or "tbc"
        pug_role_id = config.PUG_ROLE_ID

        match_id = await matches_db.create_match(type_="6s_fresh_pug", timestamp=unix, created_by=interaction.user.id,
            created_by_name=interaction.user.display_name, team_name=None, notes=None,
            division=None, map_name=map_name, server=None, pug_role_id=pug_role_id)

        result = await channel_service.create_match_channels(
            interaction.guild, match_id, "6s_fresh_pug", creator_id=interaction.user.id
        )
        if not result:
            await interaction.followup.send("\u274c Could not create PUG channel \u2014 check SIXS_MATCH_CATEGORY_ID in .env.", ephemeral=True)
            return
        channel_id, _ = result
        channel = self.bot.get_channel(channel_id)
        match   = await matches_db.get_match(match_id)
        content = build_6s_fresh_pug_message(match, pug_role_id=pug_role_id)
        from pingu.views.fresh_pug_manage_views import FreshPugSignupView
        view    = FreshPugSignupView(match_id)
        msg     = await channel.send(content, view=view)
        await matches_db.set_message_id(match_id, msg.id, channel.id)

        signup_list_msg = await channel.send(content=build_fresh_pug_signup_list([]))
        await matches_db.set_signup_list_msg_id(match_id, signup_list_msg.id)

        try:
            thread = await msg.create_thread(name=f"FRESH PUG 6v6, {thread_date_str(unix)}", auto_archive_duration=1440)
            await matches_db.set_thread_id(match_id, thread.id)
        except Exception:
            pass
        await post_to_ongoing(self.bot, match_id, channel.id)
        await interaction.followup.send(f"\u2705 6s Fresh PUG posted to {channel.mention}!", ephemeral=True)


# ── 6s Org PUG ────────────────────────────────────────────────────────────────

class SixsOPugDivisionSelect(ui.View):
    def __init__(self, bot):
        super().__init__(timeout=60)
        self.bot = bot
        options  = [discord.SelectOption(label=d, value=d) for d in SIXS_DIVISIONS]
        select   = ui.Select(placeholder="Select division\u2026", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction):
        division = interaction.data["values"][0]
        await interaction.response.send_modal(SixsOPugModal(self.bot, division))


class SixsOPugModal(ui.Modal, title="Schedule a 6s Organised PUG"):
    datetime_input = ui.TextInput(label="Date & Time (GMT+8, DD/MM/YY)", placeholder="e.g. 25/3/25 8:00 PM", style=discord.TextStyle.short, required=True)
    map_input      = ui.TextInput(label="Map (leave blank for TBC)", placeholder="e.g. cp_process_final", style=discord.TextStyle.short, required=False, max_length=80)
    server_input   = ui.TextInput(label="Server & Location", default="Matcha Singapore", style=discord.TextStyle.short, required=True, max_length=80)

    def __init__(self, bot, division):
        super().__init__()
        self.bot        = bot
        self.division   = division

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        unix = parse_datetime(self.datetime_input.value.strip())
        if unix is None:
            await interaction.followup.send(f"\u274c Couldn't parse date/time.\n{DATETIME_HINT}", ephemeral=True)
            return
        if unix < time.time():
            await interaction.followup.send("\u274c That date/time is in the past.", ephemeral=True)
            return
        map_name    = self.map_input.value.strip() or "tbc"
        server      = self.server_input.value.strip()
        pug_role_id = config.PUG_ROLE_ID

        match_id = await matches_db.create_match(type_="6s_opug", timestamp=unix, created_by=interaction.user.id,
            created_by_name=interaction.user.display_name, team_name=None, notes=None,
            division=self.division, map_name=map_name, server=server, pug_role_id=pug_role_id)

        result = await channel_service.create_match_channels(
            interaction.guild, match_id, "6s_opug", division=self.division
        )
        if not result:
            await interaction.followup.send("\u274c Could not create PUG channel \u2014 check SIXS_MATCH_CATEGORY_ID in .env.", ephemeral=True)
            return
        channel_id, _ = result
        channel = self.bot.get_channel(channel_id)
        match   = await matches_db.get_match(match_id)
        from pingu.views.signup_views import SixsSignupView
        content = build_6s_opug_message(match, [], pug_role_id=pug_role_id)
        view    = SixsSignupView(match_id)
        msg     = await channel.send(content=content, view=view)
        await matches_db.set_message_id(match_id, msg.id, channel.id)

        pending_msg = await channel.send(content=build_pending_message(match, []))
        denied_msg  = await channel.send(content=build_denied_message(match, []))
        await matches_db.set_pending_msg_id(match_id, pending_msg.id)
        await matches_db.set_denied_msg_id(match_id, denied_msg.id)
        try:
            thread = await msg.create_thread(name=f"{self.division} PUG \u2014 signup thread, {thread_date_str(unix)}", auto_archive_duration=1440)
            await matches_db.set_thread_id(match_id, thread.id)
        except Exception:
            pass
        await post_to_ongoing(self.bot, match_id, channel.id)
        await interaction.followup.send(f"\u2705 6s {self.division} PUG posted to {channel.mention}!", ephemeral=True)


# ── 6s Mix ────────────────────────────────────────────────────────────────────

class SixsDivisionSelect(ui.View):
    def __init__(self, bot, for_request=False):
        super().__init__(timeout=60)
        self.bot = bot
        self.for_request = for_request
        options  = [discord.SelectOption(label=d, value=d) for d in SIXS_DIVISIONS]
        select   = ui.Select(placeholder="Select division\u2026", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction):
        division = interaction.data["values"][0]
        await interaction.response.send_modal(
            SixsMixModal(self.bot, division, interaction.message, for_request=self.for_request)
        )


class SixsMixModal(ui.Modal, title="Schedule a 6s Mix"):
    team_name_input = ui.TextInput(label="Host team name", placeholder="e.g. GAY BLACK MEN", style=discord.TextStyle.short, required=True, max_length=40)
    datetime_input  = ui.TextInput(label="Date & Time (GMT+8, DD/MM/YY)", placeholder="e.g. 25/3/25 8:00 PM", style=discord.TextStyle.short, required=True)
    map_input       = ui.TextInput(label="Map (leave blank for TBC)", placeholder="e.g. cp_process_final", style=discord.TextStyle.short, required=False, max_length=60)
    server_input    = ui.TextInput(label="Server & Location", default="Matcha Singapore", style=discord.TextStyle.short, required=True, max_length=80)

    def __init__(self, bot, division, origin_message=None, for_request=False):
        super().__init__()
        self.bot            = bot
        self.division       = division
        self.origin_message = origin_message
        self.for_request    = for_request

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        unix      = parse_datetime(self.datetime_input.value.strip())
        team_name = self.team_name_input.value.strip()
        if unix is None:
            await interaction.followup.send(f"\u274c Couldn't parse date/time.\n{DATETIME_HINT}", ephemeral=True)
            return
        if unix < time.time():
            await interaction.followup.send("\u274c That date/time is in the past.", ephemeral=True)
            return
        map_name = self.map_input.value.strip() or "tbc"
        server   = self.server_input.value.strip()

        if self.for_request:
            if unix > time.time() + (7 * 86400):
                await interaction.followup.send(
                    "\u274c There's a 1 week limit on mix requests \u2014 please pick a date within the next 7 days.",
                    ephemeral=True,
                )
                return
            await self._submit_as_request(interaction, unix, team_name, map_name, server)
            return

        pug_role_id = config.PUG_ROLE_ID

        match_id = await matches_db.create_match(type_="6s_mix", timestamp=unix, created_by=interaction.user.id,
            created_by_name=interaction.user.display_name, team_name=team_name, notes=None,
            division=self.division, map_name=map_name, server=server, pug_role_id=pug_role_id)

        result = await channel_service.create_match_channels(
            interaction.guild, match_id, "6s_mix", team_name=team_name, division=self.division
        )
        if not result:
            await interaction.followup.send("\u274c Could not create match channel \u2014 check SIXS_MATCH_CATEGORY_ID in .env.", ephemeral=True)
            return
        channel_id, _ = result
        channel = self.bot.get_channel(channel_id)

        if self.origin_message:
            try:
                await self.origin_message.delete()
            except Exception:
                pass

        await interaction.followup.send(
            f"\u2705 Match created! Now **type your host team roster in {channel.mention}**\n\n"
            f"{roster_instructions_block(is_sixs=True)}",
            ephemeral=True,
        )
        interaction.client._pending_roster[interaction.user.id] = {
            "match_id":           match_id,
            "channel_id":         channel.id,
            "expires":            time.time() + 300,
            "type":               "6s_mix",
            "roster_interaction": interaction,
        }

    async def _submit_as_request(self, interaction, unix, team_name, map_name, server):
        """Non-hoster path -- same idea as MixModal's, just tagged for a
        6s mix (division comes from the 6s division list)."""
        from pingu import config as _config
        from pingu.services import hosting_service

        request_id = await hosting_service.submit_request(
            interaction.user.id, team_name, self.division, map_name, server, unix,
        )

        thread = None
        if _config.MIX_REQUESTS_CHANNEL_ID:
            requests_channel = interaction.client.get_channel(_config.MIX_REQUESTS_CHANNEL_ID)
            if requests_channel:
                thread = await requests_channel.create_thread(
                    name=f"mix-request-{request_id}-{team_name}-6s",
                    type=discord.ChannelType.public_thread,
                )
                await hosting_service.attach_thread(request_id, thread.id)
                await thread.send(
                    f"**6s mix request #{request_id}** from {interaction.user.mention}\n"
                    f"Team: {team_name} | Division: {self.division} | Map: {map_name} | "
                    f"Server: {server or 'no preference'} | Date: <t:{unix}:F>\n\n"
                    f"{interaction.user.mention}, post your team's roster below.\n\n"
                    f"{roster_instructions_block(is_sixs=True)}"
                )

        if self.origin_message:
            try:
                await self.origin_message.delete()
            except Exception:
                pass

        if thread:
            await interaction.followup.send(
                f"Request #{request_id} submitted. Head to {thread.mention} and post your roster.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "Request submitted, but the mix-requests channel isn't configured "
                "(MIX_REQUESTS_CHANNEL_ID) -- a hoster will need to be told manually.",
                ephemeral=True,
            )


# ── Fresh Pug modal ───────────────────────────────────────────────────────────

class FreshPugModal(ui.Modal, title="Schedule a Fresh PUG"):
    map_input = ui.TextInput(
        label="Maps (leave blank for TBC)",
        placeholder="e.g. cp_process_final, koth_product_rc9",
        style=discord.TextStyle.short,
        required=False,
        max_length=120,
    )

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)

        # Fresh pugs have no scheduled time -- they're assumed to happen
        # as soon as enough people sign up. timestamp is still a non-
        # nullable column other things key off, so it's set to "now"
        # internally, but never parsed from input or shown to anyone.
        unix = int(time.time())

        map_name    = self.map_input.value.strip() or "tbc"
        pug_role_id = config.PUG_ROLE_ID

        existing_fp = await matches_db.get_active_fresh_pug()
        if existing_fp:
            await interaction.followup.send(
                f"\u274c There's already a Fresh PUG happening (match #{existing_fp['id']}). "
                "Please conclude or cancel it first.",
                ephemeral=True,
            )
            return

        match_id = await matches_db.create_match(
            type_="fresh_pug", timestamp=unix,
            created_by=interaction.user.id,
            created_by_name=interaction.user.display_name,
            team_name=None, notes=None,
            division=None, map_name=map_name,
            server=None, pug_role_id=pug_role_id,
        )

        result = await channel_service.create_match_channels(
            interaction.guild, match_id, "fresh_pug", creator_id=interaction.user.id
        )
        if not result:
            await interaction.followup.send(
                "\u274c Could not create PUG channel \u2014 check MATCH_CATEGORY_ID in .env.", ephemeral=True
            )
            return
        channel_id, _ = result
        channel = self.bot.get_channel(channel_id)

        match   = await matches_db.get_match(match_id)
        content = build_fresh_pug_message(match, pug_role_id=pug_role_id)
        from pingu.views.fresh_pug_manage_views import FreshPugSignupView
        view    = FreshPugSignupView(match_id)
        msg     = await channel.send(content, view=view)
        await matches_db.set_message_id(match_id, msg.id, channel.id)

        signup_list_msg = await channel.send(content=build_fresh_pug_signup_list([]))
        await matches_db.set_signup_list_msg_id(match_id, signup_list_msg.id)

        try:
            thread = await msg.create_thread(
                name=f"FRESH PUG, {thread_date_str(unix)}",
                auto_archive_duration=1440,
            )
            await matches_db.set_thread_id(match_id, thread.id)
        except Exception:
            pass

        await post_to_ongoing(self.bot, match_id, channel.id)
        await interaction.followup.send(
            f"\u2705 Fresh PUG posted to {channel.mention}!", ephemeral=True
        )


# ── Connect string modal ──────────────────────────────────────────────────────

class ConnectModal(ui.Modal, title="Post Connect String"):
    raw_input = ui.TextInput(
        label="Paste the full server message here",
        placeholder="Paste everything \u2014 connect strings will be extracted automatically.",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=4000,
    )

    def __init__(self, match_id):
        super().__init__()
        self.match_id = match_id

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)

        text = self.raw_input.value
        connect = None
        sdr     = None

        def extract_section(t, header):
            pat = re.compile(
                re.escape(header) + r'[^\n]*\n+(?:`+\n?)?(connect[^\n`]+)',
                re.IGNORECASE
            )
            m = pat.search(t)
            return m.group(1).strip() if m else None

        sdr     = extract_section(text, "SDR Connect String")
        connect = extract_section(text, "Connect String")
        if connect and sdr and connect == sdr:
            connect = None

        if not connect or not sdr:
            for line in text.splitlines():
                line = line.strip().strip("`").strip()
                if not line.lower().startswith("connect"):
                    continue
                if re.match(r"connect 169\.254\.", line, re.I):
                    sdr = sdr or line
                elif re.search(r":2702\d\b", line):
                    pass
                else:
                    connect = connect or line

        if not connect and not sdr:
            await interaction.followup.send(
                "\u274c Couldn't find any connect strings. "
                "Make sure the text contains lines starting with `connect`.",
                ephemeral=True,
            )
            return

        match   = await matches_db.get_match(self.match_id)
        channel = interaction.client.get_channel(match["channel_id"])

        accepted = await signups_db.get_accepted_signups(self.match_id)
        pings    = await build_connect_pings(self.match_id, match, accepted)

        out_lines = []
        if connect:
            out_lines.append("**Connect String**")
            out_lines.append(f"```{connect}```")
        if sdr:
            out_lines.append("**SDR Connect String**")
            out_lines.append(f"```{sdr}```")
        if pings:
            out_lines.append(" ".join(pings))

        await channel.send("\n".join(out_lines))
        await interaction.followup.send("\u2705 Connect string posted!", ephemeral=True)


async def build_connect_pings(match_id, match, accepted):
    """
    Build the list of user pings for a connect string post, based on match type:
    - mix / 6s_mix: first accepted per class only (main roster)
    - opug / 6s_opug: all accepted players, deduped by user
    - fresh_pug / 6s_fresh_pug: all signed-up players
    """
    match_type = match["type"]
    seen_users  = set()
    pings       = []

    if match_type in ("mix", "6s_mix"):
        seen_classes = set()
        for s in accepted:
            if s["class_name"] not in seen_classes:
                seen_classes.add(s["class_name"])
                if s["user_id"] not in seen_users:
                    seen_users.add(s["user_id"])
                    pings.append(f"<@{s['user_id']}>")
    elif match_type in ("opug", "6s_opug"):
        for s in accepted:
            if s["user_id"] not in seen_users:
                seen_users.add(s["user_id"])
                pings.append(f"<@{s['user_id']}>")
    elif match_type in ("fresh_pug", "6s_fresh_pug"):
        for s in accepted:
            if s["user_id"] not in seen_users:
                seen_users.add(s["user_id"])
                pings.append(f"<@{s['user_id']}>")

    return pings


def get_auto_ping_ids(match):
    """
    Returns a list of role IDs to ping automatically for opug/fresh pug types
    based on division. Returns None for match types that should use the dropdown.
    """
    match_type = match["type"]
    if match_type not in ("opug", "6s_opug", "fresh_pug", "6s_fresh_pug"):
        return None

    ping_roles = config.PING_ROLES
    iron_id   = ping_roles.get("Iron")
    steel_id  = ping_roles.get("Steel")
    silver_id = ping_roles.get("Silver")
    plat_id   = ping_roles.get("Plat")
    pug_id    = ping_roles.get("PUG")

    if match_type in ("fresh_pug", "6s_fresh_pug"):
        # Fresh pugs are divisionless now -- just ping the generic PUG
        # role, no per-division branching to do (there's no division to
        # branch on anymore).
        return [r for r in [pug_id] if r]

    division = (match["division"] or "").strip()

    if division == "Iron/Steel":
        return [r for r in [iron_id, steel_id] if r]
    elif division == "Steel":
        return [r for r in [steel_id] if r]
    elif division == "Silver":
        return [r for r in [silver_id] if r]
    elif division == "Plat":
        return [r for r in [plat_id] if r]
    return [r for r in [pug_id] if r]


async def send_ping(bot, match, pug_ping):
    """Build and send the ping message, deleting the previous one. Returns (success, error_str)."""
    match_id  = match["id"]
    is_sixs   = match["type"] in ("6s_mix", "6s_opug")
    is_opug   = match["type"] in ("opug", "6s_opug")
    class_list = SIXS_CLASSES if is_sixs else TF2_CLASSES
    emoji_map  = SIXS_CLASS_EMOJI if is_sixs else CLASS_EMOJI

    signups  = await signups_db.get_signups_for_match(match_id)

    if is_opug:
        seen_pings = set()
        pings_list = []
        for s in signups:
            if s["status"] == "accepted" and s["user_id"] not in seen_pings:
                seen_pings.add(s["user_id"])
                pings_list.append(s["user_id"])

        slot_counts = {}
        for s in signups:
            if s["status"] == "accepted":
                slot_counts[s["class_name"]] = slot_counts.get(s["class_name"], 0) + 1
        total_cap    = 12 if is_sixs else 18
        opug_threshold = total_cap - (4 if is_sixs else 6)
        total_filled = sum(min(v, 2) for v in slot_counts.values())
        missing_cls  = [cls for cls in class_list if slot_counts.get(cls, 0) < 2]
        if total_filled >= opug_threshold:
            class_emojis = ", ".join(emoji_map[cls] for cls in dict.fromkeys(missing_cls))
            msg = f"{pug_ping} Just {class_emojis} left"
        else:
            division = match["division"] or "PUG"
            msg = f"{pug_ping} Need players for {division} PUG"
    else:
        accepted = {}
        for s in signups:
            if s["status"] == "accepted" and s["class_name"] not in accepted:
                accepted[s["class_name"]] = s["user_id"]

        unfilled              = [cls for cls in class_list if cls not in accepted]
        filled                = len(class_list) - len(unfilled)
        almost_full_threshold = len(class_list) - (3 if not is_sixs else 2)

        if match["type"] in ("fresh_pug", "6s_fresh_pug"):
            division = match["division"] or "PUG"
            msg = f"{pug_ping} Need players for {division} Fresh PUG"
        elif filled >= almost_full_threshold:
            class_emojis = ", ".join(emoji_map[cls] for cls in unfilled)
            msg = f"{pug_ping} Just {class_emojis} left"
        else:
            team = match["team_name"] or "Mix"
            msg  = f"{pug_ping} Need players against {team}"

    channel = bot.get_channel(match["channel_id"])
    if not channel:
        return False, "Could not find the match channel."

    ping_msg_id = match["ping_msg_id"]
    if ping_msg_id:
        try:
            old_ping = await channel.fetch_message(ping_msg_id)
            await old_ping.delete()
        except Exception:
            pass

    try:
        new_ping = await channel.send(msg)
        await matches_db.set_ping_msg_id(match_id, new_ping.id)
        return True, None
    except Exception as e:
        return False, str(e)


# ── Ping role select ─────────────────────────────────────────────────────────

class PingRoleSelectView(ui.View):
    def __init__(self, bot, match):
        super().__init__(timeout=60)
        self.bot   = bot
        self.match = match

        ping_roles = config.PING_ROLES
        options = [
            discord.SelectOption(label=name, value=str(role_id))
            for name, role_id in ping_roles.items()
        ]
        select = ui.Select(
            placeholder="Select role(s) to ping\u2026",
            options=options,
            min_values=1,
            max_values=len(options) if options else 1,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction):
        await interaction.response.defer(ephemeral=True)

        selected_ids = interaction.data["values"]
        pug_ping     = " ".join(f"<@&{rid}>" for rid in selected_ids)

        success, error = await send_ping(self.bot, self.match, pug_ping)
        if not success:
            await interaction.followup.send(f"\u274c Failed to send ping: {error}", ephemeral=True)
            return

        await interaction.followup.send("\u2705 Ping sent!", ephemeral=True)
        try:
            await interaction.message.delete()
        except Exception:
            pass


class ScheduleCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="host", description="Host a match.")
    async def host(self, interaction):
        if not is_hoster(interaction):
            await interaction.response.send_message(
                "\u274c You need the hoster role to use this command.", ephemeral=True
            )
            return
        view = GameModeSelect(self.bot)
        await interaction.response.send_message(
            "**Step 1 of 3:** Select the game mode.",
            view=view,
            ephemeral=True,
        )

    @app_commands.command(name="edit", description="Edit match details or host team roster.")
    async def edit(self, interaction):
        match = await matches_db.get_match_by_channel(interaction.channel_id)
        if not match:
            await interaction.response.send_message(
                "\u274c No active match in this channel.", ephemeral=True
            )
            return
        if not is_hoster(interaction) and not is_fresh_pug_owner(interaction, match):
            await interaction.response.send_message(
                "\u274c You need the hoster role to use this command.", ephemeral=True
            )
            return
        view = EditSelectView(match["id"], self.bot)
        await interaction.response.send_message(
            "What would you like to edit?", view=view, ephemeral=True
        )

    @app_commands.command(name="connect-string", description="Parse and post server connect strings.")
    async def connect_string(self, interaction):
        match = await matches_db.get_match_by_channel(interaction.channel_id)
        if not match:
            await interaction.response.send_message(
                "\u274c No active match in this channel.", ephemeral=True
            )
            return
        if not is_hoster(interaction) and not is_fresh_pug_owner(interaction, match):
            await interaction.response.send_message(
                "\u274c You need the hoster role to use this command.", ephemeral=True
            )
            return
        await interaction.response.send_modal(ConnectModal(match["id"]))

    @app_commands.command(name="ping", description="Ping roles asking for players.")
    async def ping_players(self, interaction):
        match = await matches_db.get_match_by_channel(interaction.channel_id)
        if not match:
            await interaction.response.send_message(
                "\u274c No active match in this channel.", ephemeral=True
            )
            return
        if not is_hoster(interaction) and not is_fresh_pug_owner(interaction, match):
            await interaction.response.send_message(
                "\u274c You need the hoster role to use this command.", ephemeral=True
            )
            return

        ping_roles = config.PING_ROLES
        if not ping_roles:
            await interaction.response.send_message(
                "\u274c No PING_ROLE_* configured in .env.", ephemeral=True
            )
            return

        auto_ids = get_auto_ping_ids(match)
        if auto_ids:
            await interaction.response.defer(ephemeral=True)
            pug_ping = " ".join(f"<@&{rid}>" for rid in auto_ids)
            success, error = await send_ping(self.bot, match, pug_ping)
            if not success:
                await interaction.followup.send(f"\u274c Failed to send ping: {error}", ephemeral=True)
            else:
                await interaction.followup.send("\u2705 Ping sent!", ephemeral=True)
            return

        view = PingRoleSelectView(self.bot, match)
        await interaction.response.send_message(
            "Select the role(s) to ping:", view=view, ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(ScheduleCog(bot))