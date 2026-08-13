"""Split out of views/legacy.py -- hoster tools to move an accepted
player back to pending, or restore a denied signup back to pending."""

import discord
from discord import ui

from pingu.embeds import TF2_CLASSES, SIXS_CLASSES
from pingu.db import matches as matches_db
from pingu.db import signups as signups_db
from pingu.services.roster_service import reorder_class_roster


class MoveToPendingClassSelect(ui.Select):
    def __init__(self, match_id, accepted_by_class, is_sixs=False):
        self.match_id = match_id
        class_list    = SIXS_CLASSES if is_sixs else TF2_CLASSES
        options = []
        for cls in class_list:
            count = len(accepted_by_class.get(cls, []))
            if count:
                options.append(discord.SelectOption(
                    label=cls,
                    value=cls,
                    description=f"{count} accepted",
                ))
        if not options:
            options = [discord.SelectOption(label="None", value="_none", description="No accepted players")]
        super().__init__(placeholder="Select a class\u2026", options=options, min_values=1, max_values=1)

    async def callback(self, interaction):
        class_name = self.values[0]
        if class_name == "_none":
            await interaction.response.edit_message(content="No accepted players.", view=None)
            return
        accepted = await signups_db.get_accepted_signups_for_class(self.match_id, class_name)
        if not accepted:
            await interaction.response.edit_message(
                content=f"No accepted players for **{class_name}** anymore.", view=None
            )
            return
        view = MoveToPendingPlayerView(self.match_id, class_name, accepted)
        await interaction.response.edit_message(
            content=f"**{class_name}** \u2014 select a player to move back to pending:",
            view=view
        )

class MoveToPendingPlayerButton(ui.Button):
    def __init__(self, match_id, signup_id, username, class_name, row):
        super().__init__(
            label=username,
            style=discord.ButtonStyle.secondary,
            custom_id=f"mtp:{match_id}:{signup_id}",
            row=row,
        )
        self.match_id  = match_id
        self.signup_id = signup_id
        self.username  = username
        self.class_name = class_name

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)

        current = await signups_db.get_signup_by_id(self.signup_id)
        user_id = current["user_id"] if current else None

        await signups_db.move_accepted_to_pending(self.signup_id)
        if current:
            await signups_db.restore_cancelled_to_pending(self.match_id, current["user_id"])

        await reorder_class_roster(interaction.client, self.match_id, self.class_name)
        interaction.client.ui_updater.schedule_refresh(self.match_id)

        if user_id:
            match = await matches_db.get_match(self.match_id)
            if match and match["thread_id"]:
                try:
                    thread = interaction.client.get_channel(match["thread_id"])
                    if thread:
                        await thread.send(
                            f"<@{user_id}> you've been moved back to pending by the hoster. "
                            "Please wait to be re-accepted."
                        )
                except Exception:
                    pass

        await interaction.followup.send(
            f"\u21a9\ufe0f **{self.username}** moved back to pending for **{self.class_name}**. "
            "Their other sign-ups have been restored.",
            ephemeral=True
        )

        try:
            accepted = await signups_db.get_accepted_signups_for_class(self.match_id, self.class_name)
            if accepted:
                view = MoveToPendingPlayerView(self.match_id, self.class_name, accepted)
                await interaction.message.edit(
                    content=f"**{self.class_name}** \u2014 select a player to move back to pending:",
                    view=view
                )
            else:
                await interaction.message.edit(
                    content=f"No more accepted players for **{self.class_name}**.", view=None
                )
        except Exception:
            pass

class MoveToPendingPlayerView(ui.View):
    def __init__(self, match_id, class_name, accepted_signups):
        super().__init__(timeout=300)
        self.match_id   = match_id
        self.class_name = class_name
        row = 0
        for i, s in enumerate(accepted_signups):
            if i > 0 and i % 5 == 0:
                row += 1
            if row > 3:
                break
            self.add_item(MoveToPendingPlayerButton(match_id, s["id"], s["username"], class_name, row))

class MoveToPendingView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=300)
        self.match_id = match_id

    @classmethod
    async def create(cls, match_id, is_sixs=False):
        self    = cls(match_id)
        accepted = await signups_db.get_accepted_signups(match_id)
        accepted_by_class = {}
        for s in accepted:
            accepted_by_class.setdefault(s["class_name"], []).append(s)
        self.add_item(MoveToPendingClassSelect(match_id, accepted_by_class, is_sixs=is_sixs))
        return self

class RestoreDeniedClassSelect(ui.Select):
    def __init__(self, match_id, denied_by_class, is_sixs=False):
        self.match_id = match_id
        class_list    = SIXS_CLASSES if is_sixs else TF2_CLASSES
        options = []
        for cls in class_list:
            count = len(denied_by_class.get(cls, []))
            if count:
                options.append(discord.SelectOption(
                    label=cls,
                    value=cls,
                    description=f"{count} denied",
                ))
        if not options:
            options = [discord.SelectOption(label="None", value="_none", description="No denied players")]
        super().__init__(placeholder="Select a class\u2026", options=options, min_values=1, max_values=1)

    async def callback(self, interaction):
        class_name = self.values[0]
        if class_name == "_none":
            await interaction.response.edit_message(content="No denied players.", view=None)
            return
        signups = await signups_db.get_signups_for_match(self.match_id)
        denied  = [s for s in signups if s["status"] == "denied" and s["class_name"] == class_name]
        if not denied:
            await interaction.response.edit_message(
                content=f"No denied players for **{class_name}** anymore.", view=None
            )
            return
        view = RestoreDeniedPlayerView(self.match_id, class_name, denied)
        await interaction.response.edit_message(
            content=f"**{class_name}** \u2014 select a player to restore to pending:",
            view=view
        )

class RestoreDeniedPlayerButton(ui.Button):
    def __init__(self, match_id, signup_id, username, class_name, row):
        super().__init__(
            label=username,
            style=discord.ButtonStyle.success,
            custom_id=f"rden:{match_id}:{signup_id}",
            row=row,
        )
        self.match_id   = match_id
        self.signup_id  = signup_id
        self.username   = username
        self.class_name = class_name

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        await signups_db.update_signup_status(self.signup_id, "pending")
        interaction.client.ui_updater.schedule_refresh(self.match_id)

        await interaction.followup.send(
            f"\u21a9\ufe0f **{self.username}** restored to pending for **{self.class_name}**.",
            ephemeral=True
        )

        try:
            signups = await signups_db.get_signups_for_match(self.match_id)
            denied  = [s for s in signups if s["status"] == "denied" and s["class_name"] == self.class_name]
            if denied:
                view = RestoreDeniedPlayerView(self.match_id, self.class_name, denied)
                await interaction.message.edit(
                    content=f"**{self.class_name}** \u2014 select a player to restore to pending:",
                    view=view
                )
            else:
                await interaction.message.edit(
                    content=f"No more denied players for **{self.class_name}**.", view=None
                )
        except Exception:
            pass

class RestoreDeniedPlayerView(ui.View):
    def __init__(self, match_id, class_name, denied_signups):
        super().__init__(timeout=300)
        self.match_id   = match_id
        self.class_name = class_name
        row = 0
        for i, s in enumerate(denied_signups):
            if i > 0 and i % 5 == 0:
                row += 1
            if row > 3:
                break
            self.add_item(RestoreDeniedPlayerButton(match_id, s["id"], s["username"], class_name, row))

class RestoreDeniedView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=300)
        self.match_id = match_id

    @classmethod
    async def create(cls, match_id, is_sixs=False):
        self    = cls(match_id)
        signups = await signups_db.get_signups_for_match(self.match_id)
        denied  = [s for s in signups if s["status"] == "denied"]
        denied_by_class = {}
        for s in denied:
            denied_by_class.setdefault(s["class_name"], []).append(s)
        self.add_item(RestoreDeniedClassSelect(match_id, denied_by_class, is_sixs=is_sixs))
        return self