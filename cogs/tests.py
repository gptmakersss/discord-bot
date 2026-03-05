# cogs/tests.py
from discord import app_commands
from discord.ext import commands
import discord
import os

class TestsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="tests", description="בדיקות מערכת לבוט")
    async def tests(self, interaction: discord.Interaction):
        results = []

        # בדיקת DB
        try:
            open("data/db.json", "r", encoding="utf-8")
            results.append("✅ DB נטען")
        except:
            results.append("❌ DB לא נטען")

        # בדיקת חדרים
        for name, cid in self.bot.CHANNELS.items():
            ch = self.bot.get_channel(cid)
            results.append(f"{'✅' if ch else '❌'} חדר {name}")

        # בדיקת בוט
        results.append("✅ בוט פעיל")

        msg = "🧪 **בדיקות מערכת**\n" + "\n".join(results)
        await interaction.response.send_message(msg, ephemeral=True)

async def setup(bot):
    await bot.add_cog(TestsCog(bot), guild=bot.guild_obj)
