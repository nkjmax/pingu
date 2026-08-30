"""
Split out of views/legacy.py -- the hoster's main /manage panel
(ManageView, SlimManageView), the conclude/cancel confirmation dialogs,
and build_manage_text (the panel's text body).

Imports ReviewView/DenyReviewView/SplitView/SixsSplitView/MoveToPendingView/
RestoreDeniedView at module level -- safe, since none of those files import
anything from this one at module level (review_views.py needs
build_manage_text back, but does so via a LOCAL import inside its own
functions specifically to avoid a circular import with this file).
"""

import time
import discord
from discord import ui

from pingu import config
from pingu.embeds import (
    TF2_CLASSES, SIXS_CLASSES, build_split_view_text, build_6s_split_view_text,
)
from pingu.db import matches as matches_db
from pingu.db import signups as signups_db
from pingu.services.match_lifecycle_service import do_conclude, do_cancel
from pingu.views.review_views import ReviewView, DenyReviewView
from pingu.views.split_views import SplitView, SixsSplitView
from pingu.views.roster_admin_views import MoveToPendingView, RestoreDeniedView


class ConcludeConfirmView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=60)
        self.match_id = match_id

    @ui.button(label="Yes, conclude match", style=discord.ButtonStyle.success)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        match = await matches_db.get_match(self.match_id)
        if not match:
            await interaction.followup.send("Match not found.", ephemeral=True)
            return

        opug_split = None
        if match["type"] in ("opug", "6s_opug"):
            split = await matches_db.get_team_split(self.match_id)
            if split:
                all_signups  = await signups_db.get_signups_for_match(self.match_id)
                accepted_all = [s for s in all_signups if s["status"] == "accepted"]
                red_uids     = set(split["red"])
                blu_uids     = set(split["blu"])
                opug_split   = {
                    "red":  [s for s in accepted_all if s["user_id"] in red_uids],
                    "blu":  [s for s in accepted_all if s["user_id"] in blu_uids],
                    "subs": [s for s in accepted_all if s["user_id"] not in red_uids and s["user_id"] not in blu_uids],
                }

        await do_conclude(interaction.client, interaction.guild, self.match_id, interaction.user.id, opug_split=opug_split)
        await interaction.followup.send("\u2705 Match concluded, archived, and channels removed.", ephemeral=True)

    @ui.button(label="Never mind", style=discord.ButtonStyle.secondary)
    async def abort(self, interaction, button):
        await interaction.response.edit_message(content="Conclusion cancelled.", view=None)

class CancelConfirmView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=60)
        self.match_id = match_id

    @ui.button(label="Yes, cancel the match", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        success = await do_cancel(interaction.client, interaction.guild, self.match_id)
        if success:
            await interaction.followup.send(
                "\u2705 Match cancelled, archived, and channels removed.", ephemeral=True
            )
        else:
            await interaction.followup.send(
                "\u274c Could not cancel \u2014 match may already be ended.", ephemeral=True
            )

    @ui.button(label="Never mind", style=discord.ButtonStyle.secondary)
    async def abort(self, interaction, button):
        await interaction.response.edit_message(
            content="Cancellation aborted. Match is still active.", view=None
        )

async def build_manage_text(match_id):
    signups = await signups_db.get_signups_for_match(match_id)
    match   = await matches_db.get_match(match_id)
    team    = match["team_name"] or "Mix"

    player_data = {}
    for s in signups:
        uid = s["user_id"]
        if uid not in player_data:
            player_data[uid] = {
                "username": s["username"], "accepted": [], "pending": [],
                "denied": [], "awaiting_hoster": [], "min_id": s["id"],
            }
        # captain_decision is a proposal, not a status -- a 'pending' signup
        # with a decision set goes in the awaiting-hoster bucket instead of
        # plain pending, everything else buckets by status as before.
        if s["status"] == "pending" and s["captain_decision"]:
            player_data[uid]["awaiting_hoster"].append((s["id"], s["class_name"], s["captain_decision"]))
        elif s["status"] == "pending":
            player_data[uid]["pending"].append((s["id"], s["class_name"]))
        elif s["status"] in player_data[uid]:
            player_data[uid][s["status"]].append((s["id"], s["class_name"]))

    class_list = SIXS_CLASSES if match["type"] in ("6s_mix", "6s_opug", "6s_fresh_pug") else TF2_CLASSES

    for uid in player_data:
        for key in ("accepted", "pending", "denied"):
            player_data[uid][key].sort(key=lambda x: x[0])
            player_data[uid][key] = [cls for _, cls in player_data[uid][key]]
        player_data[uid]["awaiting_hoster"].sort(key=lambda x: x[0])
        player_data[uid]["awaiting_hoster"] = [
            f"{cls} ({decision})" for _, cls, decision in player_data[uid]["awaiting_hoster"]
        ]

    def fmt(classes):
        return ", ".join(classes) if classes else "\u2014"

    if match["type"] in ("opug", "6s_opug"):
        division = match["division"] or "PUG"
        header = "**" + division + " PUG \u2014 signups**\n"
    elif match["type"] in ("fresh_pug", "6s_fresh_pug"):
        header = "**Fresh PUG \u2014 signups**\n"
    elif match["type"] == "6s_mix":
        header = "**" + team + " vs Mix 6s \u2014 signups**\n"
    else:
        header = "**" + team + " vs Mix \u2014 signups**\n"
    lines  = [header]

    accepted_players = [p for p in player_data.values() if p["accepted"]]
    pending_players  = sorted(
        [p for p in player_data.values() if p["pending"]],
        key=lambda p: p["min_id"]
    )
    awaiting_players = sorted(
        [p for p in player_data.values() if p["awaiting_hoster"]],
        key=lambda p: p["min_id"]
    )
    denied_players   = [
        p for p in player_data.values()
        if p["denied"] and not p["accepted"] and not p["pending"] and not p["awaiting_hoster"]
    ]

    if accepted_players:
        lines.append("\u2705 **Accepted:**")
        for p in accepted_players:
            lines.append("\u2022 **" + p["username"] + "** \u2014 " + fmt(p["accepted"]))

    if awaiting_players:
        lines.append("\n\U0001f4cb **Awaiting your approval** *(captain-screened, still counted as pending publicly)*:")
        for p in awaiting_players:
            lines.append("\u2022 **" + p["username"] + "** \u2014 " + fmt(p["awaiting_hoster"]))

    if pending_players:
        lines.append("\n\u23f3 **Pending** *(chronological order)*:")
        for p in pending_players:
            lines.append("\u2022 **" + p["username"] + "** \u2014 " + fmt(p["pending"]))

    if denied_players:
        lines.append("\n\u274c **Denied:**")
        for p in denied_players:
            lines.append("\u2022 **" + p["username"] + "** \u2014 " + fmt(p["denied"]))

    if not accepted_players and not pending_players and not awaiting_players and not denied_players:
        lines.append("No sign-ups yet.")

    total_pending = sum(len(p["pending"]) for p in player_data.values())
    return "\n".join(lines), total_pending

class OPugCancelAfterStartView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=60)
        self.match_id = match_id

    @ui.button(label="Yes, cancel anyway", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        success = await do_cancel(interaction.client, interaction.guild, self.match_id)
        if success:
            await interaction.followup.send("\u2705 Match cancelled.", ephemeral=True)
        else:
            await interaction.followup.send("\u274c Could not cancel.", ephemeral=True)

    @ui.button(label="Never mind", style=discord.ButtonStyle.secondary)
    async def abort(self, interaction, button):
        await interaction.response.edit_message(content="Cancellation aborted.", view=None)

class SlimManageView(ui.View):
    """Minimal manage view for 8h reminders -- Conclude and Cancel only."""
    def __init__(self, match_id):
        super().__init__(timeout=None)
        self.match_id = match_id

    @ui.button(label="Conclude match", style=discord.ButtonStyle.success, row=0)
    async def conclude_match(self, interaction, button):
        match = await matches_db.get_match(self.match_id)
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return
        if time.time() < match["timestamp"]:
            remaining = int(match["timestamp"] - time.time())
            h, m = divmod(remaining // 60, 60)
            await interaction.response.send_message(
                f"\u274c You can only conclude after the match has started. Starts in **{h}h {m}m**.",
                ephemeral=True,
            )
            return
        if match["type"] in ("opug", "6s_opug"):
            split = await matches_db.get_team_split(self.match_id)
            if not split:
                await interaction.response.send_message(
                    "\u274c Teams haven't been split yet. Use **Split teams** in /manage first.",
                    ephemeral=True,
                )
                return
            teams_posted = match["teams_posted"]
            if not teams_posted:
                await interaction.response.send_message(
                    "\u274c Teams have been split but not posted yet. Press **Post teams** in the balancing chat first.",
                    ephemeral=True,
                )
                return
        view = ConcludeConfirmView(self.match_id)
        await interaction.response.send_message(
            "Ready to conclude this match?", view=view, ephemeral=True
        )

    @ui.button(label="Cancel match", style=discord.ButtonStyle.danger, row=0)
    async def cancel_match(self, interaction, button):
        match = await matches_db.get_match(self.match_id)
        if match and match["type"] in ("opug", "6s_opug") and time.time() > match["timestamp"]:
            view = OPugCancelAfterStartView(self.match_id)
            await interaction.response.send_message(
                "\u26a0\ufe0f This PUG has already started. If the match was played, use **Conclude** instead.\n"
                "Are you sure you want to cancel?",
                view=view, ephemeral=True,
            )
            return
        view = CancelConfirmView(self.match_id)
        await interaction.response.send_message(
            "\u26a0\ufe0f Are you sure you want to cancel this match?",
            view=view, ephemeral=True,
        )

class ManageView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=300)
        self.match_id = match_id

    @classmethod
    async def create(cls, match_id):
        self = cls(match_id)
        # Only mixes have a captain concept at all -- oPUG and fresh pug
        # should never even see this button, not just have it be a
        # harmless no-op when clicked. Added dynamically here rather than
        # as a static @ui.button decorator, since a decorator has no way
        # to conditionally skip itself based on match type.
        match = await matches_db.get_match(match_id)
        if match and match["type"] in ("mix", "6s_mix"):
            btn = ui.Button(label="Review captain picks", style=discord.ButtonStyle.primary, row=0)
            btn.callback = self._review_captain_picks
            self.add_item(btn)
        return self

    @ui.button(label="Accept players", style=discord.ButtonStyle.primary, row=0)
    async def review_pending(self, interaction, button):
        pending = await signups_db.get_pending_signups(self.match_id)
        if not pending:
            await interaction.response.send_message("No pending sign-ups right now.", ephemeral=True)
            return
        text, _ = await build_manage_text(self.match_id)
        view     = await ReviewView.create(self.match_id)
        await interaction.response.send_message(text, view=view, ephemeral=True)

    @ui.button(label="Deny players", style=discord.ButtonStyle.danger, row=0)
    async def deny_players(self, interaction, button):
        pending = await signups_db.get_pending_signups(self.match_id)
        if not pending:
            await interaction.response.send_message("No pending sign-ups to deny right now.", ephemeral=True)
            return
        text, _ = await build_manage_text(self.match_id)
        view     = await DenyReviewView.create(self.match_id)
        await interaction.response.send_message(text, view=view, ephemeral=True)

    @ui.button(label="Move to pending", style=discord.ButtonStyle.secondary, row=0)
    async def move_to_pending(self, interaction, button):
        match   = await matches_db.get_match(self.match_id)
        accepted = await signups_db.get_accepted_signups(self.match_id)
        if not accepted:
            await interaction.response.send_message("No accepted players right now.", ephemeral=True)
            return
        is_sixs    = match["type"] in ("6s_mix", "6s_opug") if match else False
        view = await MoveToPendingView.create(self.match_id, is_sixs=is_sixs)
        await interaction.response.send_message(
            "Select a class to move a player back to pending:", view=view, ephemeral=True
        )

    @ui.button(label="Restore denied", style=discord.ButtonStyle.secondary, row=0)
    async def restore_denied(self, interaction, button):
        match   = await matches_db.get_match(self.match_id)
        signups = await signups_db.get_signups_for_match(self.match_id)
        denied  = [s for s in signups if s["status"] == "denied"]
        if not denied:
            await interaction.response.send_message("No denied players right now.", ephemeral=True)
            return
        is_sixs = match["type"] in ("6s_mix", "6s_opug") if match else False
        view = await RestoreDeniedView.create(self.match_id, is_sixs=is_sixs)
        await interaction.response.send_message(
            "Select a class to restore a denied player to pending:", view=view, ephemeral=True
        )

    async def _review_captain_picks(self, interaction: discord.Interaction):
        """Only reachable for mixes now -- see create() above."""
        from pingu.views.roster_views import HosterPicksReviewView
        view = await HosterPicksReviewView.create(self.match_id, interaction.client.ui_updater)
        await interaction.response.send_message(
            "Review picks your captain has already screened:", view=view, ephemeral=True
        )

    @ui.button(label="Conclude match", style=discord.ButtonStyle.success, row=1)
    async def conclude_match(self, interaction, button):
        match = await matches_db.get_match(self.match_id)
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return
        if time.time() < match["timestamp"]:
            remaining = int(match["timestamp"] - time.time())
            h, m = divmod(remaining // 60, 60)
            await interaction.response.send_message(
                f"\u274c You can only conclude after the match has started. "
                f"Starts in **{h}h {m}m**.",
                ephemeral=True,
            )
            return
        if match["type"] in ("opug", "6s_opug"):
            split = await matches_db.get_team_split(self.match_id)
            if not split:
                await interaction.response.send_message(
                    "\u274c Teams haven't been split yet. Use **Split teams** first, then post the teams before concluding.",
                    ephemeral=True,
                )
                return
            teams_posted = match["teams_posted"]
            if not teams_posted:
                await interaction.response.send_message(
                    "\u274c Teams have been split but not posted yet. Press **Post teams** in the balancing chat before concluding.",
                    ephemeral=True,
                )
                return
        view = ConcludeConfirmView(self.match_id)
        await interaction.response.send_message(
            "Ready to conclude this match? This will post a conclusion notice, "
            "archive the thread, and log it to the archive channel.",
            view=view, ephemeral=True,
        )

    @ui.button(label="Split teams", style=discord.ButtonStyle.primary, row=1)
    async def split_teams(self, interaction, button):
        match = await matches_db.get_match(self.match_id)
        if not match or match["type"] not in ("opug", "6s_opug"):
            await interaction.response.send_message(
                "\u274c Team splitting is only available for Organised PUGs.", ephemeral=True
            )
            return

        signups  = await signups_db.get_signups_for_match(self.match_id)
        accepted = [s for s in signups if s["status"] == "accepted"]

        is_sixs     = match["type"] == "6s_opug"
        class_list  = SIXS_CLASSES if is_sixs else TF2_CLASSES
        cap         = 12 if is_sixs else 18
        slot_counts = {}
        for s in accepted:
            slot_counts[s["class_name"]] = slot_counts.get(s["class_name"], 0) + 1
        total = sum(min(v, 2) for v in slot_counts.values())

        if total < cap:
            await interaction.response.send_message(
                f"\u274c Not all {cap} slots are filled yet ({total}/{cap}). Please wait until all players have signed up.",
                ephemeral=True,
            )
            return

        red_uids = []
        blu_uids = []
        for cls in class_list:
            cls_players = [s for s in accepted if s["class_name"] == cls][:2]
            if len(cls_players) >= 1:
                red_uids.append(cls_players[0]["user_id"])
            if len(cls_players) >= 2:
                blu_uids.append(cls_players[1]["user_id"])

        await matches_db.save_team_split(self.match_id, red_uids, blu_uids)

        red_team = [s for s in accepted if s["user_id"] in red_uids]
        blu_team = [s for s in accepted if s["user_id"] in blu_uids]

        if is_sixs:
            text = build_6s_split_view_text(red_team, blu_team)
            view = SixsSplitView(self.match_id, red_team, blu_team)
        else:
            text = build_split_view_text(red_team, blu_team)
            view = SplitView(self.match_id, red_team, blu_team)

        if config.BALANCING_CHAT_ID:
            bal_ch = interaction.client.get_channel(config.BALANCING_CHAT_ID)
            if bal_ch:
                await bal_ch.send(text, view=view)
                await interaction.response.send_message(
                    f"\u2705 Split posted to {bal_ch.mention}!", ephemeral=True
                )
                return
        await interaction.response.send_message(text, view=view, ephemeral=True)

    @ui.button(label="Cancel match", style=discord.ButtonStyle.danger, row=1)
    async def cancel_match(self, interaction, button):
        match = await matches_db.get_match(self.match_id)
        if match and match["type"] in ("opug", "6s_opug") and time.time() > match["timestamp"]:
            view = OPugCancelAfterStartView(self.match_id)
            await interaction.response.send_message(
                "\u26a0\ufe0f This PUG has already started. If the match was played, use **Conclude** instead.\n"
                "Are you sure you want to cancel?",
                view=view, ephemeral=True,
            )
            return
        view = CancelConfirmView(self.match_id)
        await interaction.response.send_message(
            "\u26a0\ufe0f Are you sure you want to cancel this match?\n"
            "This will delete the embed, ping accepted players with a 24h notice, "
            "and archive the thread.",
            view=view, ephemeral=True,
        )