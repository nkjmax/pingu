"""Split out of views/legacy.py -- oPUG team-balancing/split views (both
HL and 6s), used from the balancing chat once a hoster splits teams."""

import discord
from discord import ui

from pingu.embeds import (
    TF2_CLASSES, CLASS_EMOJI, SIXS_CLASSES, SIXS_CLASS_EMOJI,
    build_opug_teams_message, build_split_view_text, build_6s_opug_teams_message, build_6s_split_view_text,
)
from pingu.db import matches as matches_db
from pingu.db import signups as signups_db


class SixsSwapClassButton(ui.Button):
    def __init__(self, class_name, match_id, row):
        super().__init__(
            label=class_name,
            emoji=SIXS_CLASS_EMOJI[class_name],
            style=discord.ButtonStyle.secondary,
            custom_id="sixs_swap:" + str(match_id) + ":" + class_name,
            row=row,
        )
        self.class_name = class_name
        self.match_id   = match_id

    async def callback(self, interaction):
        split    = await matches_db.get_team_split(self.match_id)
        signups  = await signups_db.get_signups_for_match(self.match_id)
        accepted = [s for s in signups if s["status"] == "accepted"]
        if not split:
            await interaction.response.send_message("No split data.", ephemeral=True)
            return
        red, blu = split["red"], split["blu"]
        red_s = [s for s in accepted if s["user_id"] in red and s["class_name"] == self.class_name]
        blu_s = [s for s in accepted if s["user_id"] in blu and s["class_name"] == self.class_name]
        if not red_s or not blu_s:
            await interaction.response.send_message("Can't swap \u2014 missing player on one side.", ephemeral=True)
            return
        red_uid, blu_uid = red_s[0]["user_id"], blu_s[0]["user_id"]
        new_red = [blu_uid if u == red_uid else u for u in red]
        new_blu = [red_uid if u == blu_uid else u for u in blu]
        await matches_db.save_team_split(self.match_id, new_red, new_blu)
        red_team = [s for s in accepted if s["user_id"] in new_red]
        blu_team = [s for s in accepted if s["user_id"] in new_blu]
        text = build_6s_split_view_text(red_team, blu_team)
        view = SixsSplitView(self.match_id, red_team, blu_team)
        await interaction.response.edit_message(content=text, view=view)

class SixsPostTeamsButton(ui.Button):
    def __init__(self, match_id):
        super().__init__(label="Post teams", style=discord.ButtonStyle.success,
                         custom_id="sixs_post_teams:" + str(match_id), row=2)
        self.match_id = match_id

    async def callback(self, interaction):
        await interaction.response.defer()
        split    = await matches_db.get_team_split(self.match_id)
        signups  = await signups_db.get_signups_for_match(self.match_id)
        accepted = [s for s in signups if s["status"] == "accepted"]
        match    = await matches_db.get_match(self.match_id)
        red_uids, blu_uids = split["red"], split["blu"]
        red_team = [s for s in accepted if s["user_id"] in red_uids]
        blu_team = [s for s in accepted if s["user_id"] in blu_uids]
        subs     = [s for s in accepted if s["user_id"] not in red_uids and s["user_id"] not in blu_uids]
        channel  = interaction.client.get_channel(match["channel_id"])
        if channel:
            await channel.send(build_6s_opug_teams_message(match, red_team, blu_team, subs))

        await matches_db.set_teams_posted(self.match_id)

        try:
            await interaction.message.delete()
        except Exception:
            pass
        await interaction.followup.send("\u2705 Teams posted!", ephemeral=True)

class SixsSplitView(ui.View):
    def __init__(self, match_id, red_team, blu_team):
        super().__init__(timeout=None)
        self.match_id = match_id
        for i, cls in enumerate(SIXS_CLASSES):
            row = i // 4
            self.add_item(SixsSwapClassButton(cls, match_id, row))
        self.add_item(SixsPostTeamsButton(match_id))

class SwapClassButton(ui.Button):
    def __init__(self, class_name, match_id, row):
        super().__init__(
            label=class_name,
            emoji=CLASS_EMOJI[class_name],
            style=discord.ButtonStyle.secondary,
            custom_id="swap:" + str(match_id) + ":" + class_name,
            row=row,
        )
        self.class_name = class_name
        self.match_id   = match_id

    async def callback(self, interaction):
        split = await matches_db.get_team_split(self.match_id)
        if not split:
            await interaction.response.send_message("No split data found.", ephemeral=True)
            return

        red = split["red"]
        blu = split["blu"]

        signups = await signups_db.get_signups_for_match(self.match_id)
        accepted = [s for s in signups if s["status"] == "accepted"]

        red_signups = [s for s in accepted if s["user_id"] in red and s["class_name"] == self.class_name]
        blu_signups = [s for s in accepted if s["user_id"] in blu and s["class_name"] == self.class_name]

        if not red_signups or not blu_signups:
            await interaction.response.send_message(
                "Can't swap \u2014 one team has no player for " + self.class_name + ".", ephemeral=True
            )
            return

        red_uid = red_signups[0]["user_id"]
        blu_uid = blu_signups[0]["user_id"]

        new_red = [blu_uid if uid == red_uid else uid for uid in red]
        new_blu = [red_uid if uid == blu_uid else uid for uid in blu]

        await matches_db.save_team_split(self.match_id, new_red, new_blu)

        red_s = [s for s in accepted if s["user_id"] in new_red]
        blu_s = [s for s in accepted if s["user_id"] in new_blu]

        text = build_split_view_text(red_s, blu_s)
        view = SplitView(self.match_id, red_s, blu_s)
        await interaction.response.edit_message(content=text, view=view)

class PostTeamsButton(ui.Button):
    def __init__(self, match_id):
        super().__init__(
            label="Post teams",
            style=discord.ButtonStyle.success,
            custom_id="post_teams:" + str(match_id),
            row=4,
        )
        self.match_id = match_id

    async def callback(self, interaction):
        await interaction.response.defer()
        split    = await matches_db.get_team_split(self.match_id)
        signups  = await signups_db.get_signups_for_match(self.match_id)
        accepted = [s for s in signups if s["status"] == "accepted"]
        match    = await matches_db.get_match(self.match_id)

        red_uids = split["red"]
        blu_uids = split["blu"]

        red_team = []
        blu_team = []
        subs     = []
        for cls in TF2_CLASSES:
            cls_accepted = [s for s in accepted if s["class_name"] == cls]
            for s in cls_accepted:
                if s["user_id"] in red_uids:
                    red_team.append(s)
                elif s["user_id"] in blu_uids:
                    blu_team.append(s)
                else:
                    subs.append(s)

        channel = interaction.client.get_channel(match["channel_id"])
        if channel:
            msg_text = build_opug_teams_message(match, red_team, blu_team, subs)
            await channel.send(msg_text)

        await matches_db.set_teams_posted(self.match_id)

        try:
            await interaction.message.delete()
        except Exception:
            pass

        await interaction.followup.send("\u2705 Teams posted!", ephemeral=True)

class SplitView(ui.View):
    def __init__(self, match_id, red_team, blu_team):
        super().__init__(timeout=None)
        self.match_id = match_id
        for i, cls in enumerate(TF2_CLASSES):
            row = 2 + i // 5
            self.add_item(SwapClassButton(cls, match_id, row))
        self.add_item(PostTeamsButton(match_id))