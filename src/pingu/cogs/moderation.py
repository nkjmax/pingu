"""
/kill -- the LP/mix ban command. Flow: /kill @player (member picked
directly on the command, same pattern as /view-logs) -> ephemeral
dropdown (Low Priority / Mix Ban) -> one modal (number, unit, reason).
Applies the role via the existing penalties system (moderation_service.
apply_penalty/expire_penalties) rather than reinventing anything, and
posts the announcement to BANS_CHANNEL_ID.

Expiry: <number> <unit> from the moment the command is run, then the
TIME is snapped to 23:59:59 SGT on whatever calendar date that lands on
-- e.g. "1 day" run at 6:37pm on 24/8 computes to 25/8 6:37pm, then
snaps to 25/8 23:59:59 SGT. Month/year use calendar-aware arithmetic
(dateutil.relativedelta), not a fixed day count, so "1 month" lands on
the same day next month rather than a flat 30-day block.

No /unkill -- discussed and deliberately left out. Manually removing the
role and deleting the #bans message is safe either way: is_lp() (the
function that actually drives LP behavior) reads live Discord roles
directly, never the DB, so an early manual role removal takes effect
immediately with nothing left out of sync. The one cosmetic gap: the
penalties row stays 'active' until its original expires_at naturally
passes, but expire_penalties() already handles a role that's already
gone gracefully (no error, just skips and deactivates the row). The only
real cost of skipping /unkill is no audit trail of early manual releases
-- accepted tradeoff for keeping the command count down.
"""

import time
import logging
import discord
from discord import app_commands, ui
from discord.ext import commands
from datetime import datetime
from dateutil.tz import gettz
from dateutil.relativedelta import relativedelta

from pingu import config
from pingu.services import moderation_service

log = logging.getLogger("cogs.moderation")

DEFAULT_TZ = "Asia/Singapore"  # same constant/library used elsewhere (cogs/hosting.py, db/tickets.py)

PENALTY_LABELS = {
    "low_prio": "LOW PRIORITY",
    "mix_ban": "MIX BAN",
}


def _is_mod(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    if not config.MOD_ROLE_ID:
        return False
    return any(r.id == config.MOD_ROLE_ID for r in interaction.user.roles)


def _compute_expiry(number: int, unit: str) -> int:
    """<number> <unit> from now (SGT), then snapped to 23:59:59 SGT on
    the resulting calendar date. unit is one of d/w/m/y."""
    now_sgt = datetime.now(tz=gettz(DEFAULT_TZ))
    if unit == "d":
        raw = now_sgt + relativedelta(days=number)
    elif unit == "w":
        raw = now_sgt + relativedelta(weeks=number)
    elif unit == "m":
        raw = now_sgt + relativedelta(months=number)
    else:  # "y"
        raw = now_sgt + relativedelta(years=number)

    snapped = raw.replace(hour=23, minute=59, second=59, microsecond=0)
    return int(snapped.timestamp())


class KillTypeSelect(ui.View):
    def __init__(self, target: discord.Member, timeout=120):
        super().__init__(timeout=timeout)
        self.target = target
        select = ui.Select(
            placeholder="Select penalty type\u2026",
            options=[
                discord.SelectOption(label="Low Priority", value="low_prio"),
                discord.SelectOption(label="Mix Ban", value="mix_ban"),
            ],
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        penalty_type = interaction.data["values"][0]
        await interaction.response.send_modal(KillDurationModal(self.target, penalty_type))


class KillDurationModal(ui.Modal, title="Apply Penalty"):
    """
    Discord modals can't contain buttons at all (native platform
    limitation) -- there's no way to put a literal Cancel button inside
    this. Closing the modal without submitting already IS the cancel
    behaviour though: it fires no callback and has zero side effects,
    nothing gets applied.
    """
    number_input = ui.TextInput(
        label="Number", style=discord.TextStyle.short, required=True, max_length=5,
    )
    unit_input = ui.TextInput(
        label="Unit (d/w/m/y)",
        placeholder="d = days, w = weeks, m = months, y = years",
        style=discord.TextStyle.short, required=True, max_length=1,
    )
    reason_input = ui.TextInput(
        label="Reason", style=discord.TextStyle.paragraph, required=True, max_length=1000,
    )

    def __init__(self, target: discord.Member, penalty_type: str):
        super().__init__()
        self.target = target
        self.penalty_type = penalty_type

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        number_str = str(self.number_input).strip()
        unit = str(self.unit_input).strip().lower()
        reason = str(self.reason_input).strip()

        if not number_str.isdigit() or int(number_str) <= 0:
            await interaction.followup.send(
                "\u274c Number must be a positive whole number.", ephemeral=True
            )
            return
        number = int(number_str)

        if unit not in ("d", "w", "m", "y"):
            await interaction.followup.send(
                "\u274c Unit must be exactly one of: d, w, m, y.", ephemeral=True
            )
            return

        role_id = config.LOW_PRIO_ROLE_ID if self.penalty_type == "low_prio" else config.MIX_BAN_ROLE_ID
        if not role_id:
            missing = "LOW_PRIO_ROLE_ID" if self.penalty_type == "low_prio" else "MIX_BAN_ROLE_ID"
            await interaction.followup.send(f"\u274c {missing} isn't configured in .env.", ephemeral=True)
            return

        expires_at = _compute_expiry(number, unit)
        duration_seconds = expires_at - int(time.time())

        await moderation_service.apply_penalty(
            interaction.guild, self.target.id, self.penalty_type, interaction.user.id,
            reason=reason, duration_seconds=duration_seconds, role_id=role_id,
        )

        label = PENALTY_LABELS[self.penalty_type]
        emoji = "\U0001f6ab" if self.penalty_type == "mix_ban" else "\u23f3"  # :no_entry_sign: / :hourglass_flowing_sand:
        message = (
            f"{emoji} **{label}**\n"
            f"> **User**: {self.target.mention} by {interaction.user.mention}\n"
            f"> **Duration**: <t:{expires_at}:D> <t:{expires_at}:R>\n"
            f"> **Reason**: {reason}"
        )

        bans_ch = interaction.client.get_channel(config.BANS_CHANNEL_ID) if config.BANS_CHANNEL_ID else None
        if bans_ch:
            await bans_ch.send(message)
            await interaction.followup.send(
                f"\u2705 {label} applied to {self.target.mention} \u2014 posted to {bans_ch.mention}.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"\u26a0\ufe0f {label} applied, but BANS_CHANNEL_ID isn't configured "
                f"\u2014 couldn't post the announcement.\n\n{message}",
                ephemeral=True,
            )


class ModerationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not moderation_service.contains_flagged_content(message.content):
            return

        mod_log_channel = None
        if config.MOD_LOG_CHANNEL_ID:
            mod_log_channel = self.bot.get_channel(config.MOD_LOG_CHANNEL_ID)

        await moderation_service.handle_violation(message, mod_log_channel)

    @app_commands.command(name="kill", description="Apply a Low Priority or Mix Ban penalty to a player.")
    @app_commands.describe(user="The player to penalise")
    async def kill(self, interaction: discord.Interaction, user: discord.Member):
        if not _is_mod(interaction):
            await interaction.response.send_message(
                "\u274c You need to be a mod to use this.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"Applying a penalty to {user.mention} \u2014 select the type:",
            view=KillTypeSelect(user),
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(ModerationCog(bot))