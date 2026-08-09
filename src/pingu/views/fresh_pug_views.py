import discord
from discord import ui

from pingu.services import fresh_pug_service


class FreshPugSignupView(ui.View):
    def __init__(self, match_id: int, ui_updater):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.ui_updater = ui_updater

    @ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="fresh_pug_join")
    async def join(self, interaction: discord.Interaction, button: ui.Button):
        # class_name selection would come from a class-picker; simplified here
        await fresh_pug_service.join(
            self.match_id, interaction.user.id, interaction.user.display_name,
            class_name="any", ui_updater=self.ui_updater,
        )
        await interaction.response.send_message("You're in.", ephemeral=True)

    @ui.button(label="Leave", style=discord.ButtonStyle.secondary, custom_id="fresh_pug_leave")
    async def leave(self, interaction: discord.Interaction, button: ui.Button):
        await fresh_pug_service.leave(self.match_id, interaction.user.id, ui_updater=self.ui_updater)
        await interaction.response.send_message("You've left.", ephemeral=True)
