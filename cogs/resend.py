# cogs/resend.py
from discord import app_commands
from discord.ext import commands
import discord

class ResendCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="resend", description="רענון ידני של כל ההודעות האוטומטיות")
    async def resend(self, interaction: discord.Interaction):
        await interaction.response.send_message("🔄 מרענן את כל ההודעות…", ephemeral=True)

        # כל פונקציה פה כבר קיימת אצלך בקוגים האחרים
        tasks = [
            self.bot.refresh_tasks,
            self.bot.refresh_incidents,
            self.bot.refresh_notes,
            self.bot.refresh_kashag,
            self.bot.refresh_inventory,
            self.bot.refresh_sign,
            self.bot.refresh_attention,
            self.bot.refresh_nishkia,
            self.bot.refresh_weapon_issues,
        ]

        for fn in tasks:
            try:
                await fn()
            except Exception as e:
                print("RESEND ERROR:", e)

        await self.bot.send_log("RESEND", "בוצע רענון ידני לכל המערכות")

async def setup(bot):
    await bot.add_cog(ResendCog(bot), guild=bot.guild_obj)
