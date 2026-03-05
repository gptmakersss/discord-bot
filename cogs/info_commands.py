# cogs/info_commands.py
from discord import app_commands
from discord.ext import commands
import discord

INFO_TEXT = """
📘 **פקודות הבוט – סמל V2**

👤 חיילים:
• /soldier add/remove – ניהול חיילים

📦 לוגיסטיקה:
• /sign – חתימות קבועות
• /tsign – חתימות זמניות
• /status – שינוי סטטוס לציוד
• /inventory – מלאי ציוד סמליה
• /logistica – דוח לוגיסטי
• /attention – מסדר סמל (פערים)

🔫 נשקייה:
• /link – קישור חייל ↔ מספר סידורי
• /find – איתור לפי חייל / מספר
• /gun storage – אפסון נשק/צלם
• /nishkia – דוח נשקייה
• /weapon_issues – פערי נשק

🗒️ ניהול:
• /task – משימות (דחיפות)
• /incident – אירועים חריגים
• /note – פתקים

🏃‍♂️ כושר:
• /kashag – תוצאות כש״ג ובחמס

📤 ייצוא:
• /whatsapp export – ייצוא לוואטסאפ

🔄 מערכת:
• /resend – רענון ידני
• /tests – בדיקות מערכת
"""

class InfoCommandsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="info_commands", description="רשימת כל פקודות הבוט")
    async def info_commands(self, interaction: discord.Interaction):
        await interaction.response.send_message(INFO_TEXT, ephemeral=True)

async def setup(bot):
    await bot.add_cog(InfoCommandsCog(bot), guild=bot.guild_obj)
