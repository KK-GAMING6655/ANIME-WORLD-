import discord
import os
import threading
from flask import Flask
from discord.ext import commands
from discord import app_commands
from datetime import datetime

# --- 1. HEARTBEAT SERVER FOR RENDER ---
# This satisfies Render's port requirement so the bot doesn't get shut down.
app = Flask('')

@app.route('/')
def home():
    return "Bot is online!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# --- 2. BOT CONFIGURATION ---
TOKEN = os.getenv('DISCORD_TOKEN')
try:
    OWNER_ID = int(os.getenv('OWNER_ID'))
except (TypeError, ValueError):
    OWNER_ID = 0 

class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True 
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        print(f"Logged in as {self.user} | Owner ID: {OWNER_ID}")

bot = MyBot()

# --- 3. UTILITY FUNCTIONS ---
def parse_variables(content, interaction: discord.Interaction):
    """Replaces placeholders with real server data."""
    if not content:
        return content
    
    variables = {
        "{guildname}": interaction.guild.name,
        "{membercount}": str(interaction.guild.member_count),
        "{date}": datetime.now().strftime("%Y-%m-%d"),
        "{time}": datetime.now().strftime("%H:%M:%S"),
        "{channel}": interaction.channel.mention,
        "{owner}": interaction.guild.owner.display_name,
        "{user}": interaction.user.display_name
    }
    
    for placeholder, value in variables.items():
        content = content.replace(placeholder, value)
    return content

async def build_embed(interaction, heading, description, colour, image, thumbnail, author_name, footer):
    """Helper to create the Discord Embed object."""
    # Clean hex code input
    hex_str = colour.lstrip("#")
    try:
        embed_color = int(hex_str, 16)
    except ValueError:
        embed_color = 0x3498db # Default Blue if color is invalid

    embed = discord.Embed(
        title=parse_variables(heading, interaction),
        description=parse_variables(description, interaction),
        color=embed_color
    )

    if image: embed.set_image(url=image)
    if thumbnail: embed.set_thumbnail(url=thumbnail)
    if footer: embed.set_footer(text=parse_variables(footer, interaction))
    
    if author_name:
        member = discord.utils.get(interaction.guild.members, name=author_name)
        icon_url = member.display_avatar.url if member else None
        embed.set_author(name=author_name, icon_url=icon_url)
        
    return embed

# --- 4. COMMAND GROUP ---
class EmbedGroup(app_commands.Group, name="embed"):
    """All /embed commands reside here."""

    @app_commands.command(name="create", description="Create an owner-only embed")
    async def create(
        self,
        interaction: discord.Interaction, 
        heading: str, 
        description: str, 
        colour: str,
        text: str = None,
        image: str = None,
        thumbnail: str = None,
        author: str = None,
        footer: str = None
    ):
        if interaction.user.id != OWNER_ID:
            return await interaction.response.send_message("❌ Access Denied: Owner Only.", ephemeral=True)

        try:
            main_embed = await build_embed(interaction, heading, description, colour, image, thumbnail, author, footer)
            plain_text = parse_variables(text, interaction) if text else None
            
            # Send Success Confirmation (Private)
            success_msg = discord.Embed(description="✅ Your embed was successfully created!", color=discord.Color.green())
            await interaction.response.send_message(embed=success_msg, ephemeral=True)

            # Send the actual Embed to the channel (Public)
            await interaction.channel.send(content=plain_text, embed=main_embed)
            
        except Exception as e:
            error_msg = discord.Embed(description=f"❌ Embed creation failed: {str(e)}", color=discord.Color.red())
            await interaction.followup.send(embed=error_msg, ephemeral=True)

    @app_commands.command(name="edit", description="Edit an existing embed via message URL")
    async def edit(
        self,
        interaction: discord.Interaction,
        message_url: str,
        heading: str = None,
        description: str = None,
        colour: str = None,
        text: str = None,
        image: str = None,
        thumbnail: str = None,
        author: str = None,
        footer: str = None
    ):
        if interaction.user.id != OWNER_ID:
            return await interaction.response.send_message("❌ Access Denied: Owner Only.", ephemeral=True)

        try:
            # Extract ID from URL (the last number in the link)
            msg_id = int(message_url.split('/')[-1])
            message = await interaction.channel.fetch_message(msg_id)
            
            old_embed = message.embeds[0] if message.embeds else None
            
            # Fallback to old values if new ones aren't provided
            f_heading = heading or (old_embed.title if old_embed else "Title")
            f_desc = description or (old_embed.description if old_embed else "Description")
            f_colour = colour or (hex(old_embed.color.value).replace('0x', '') if old_embed else "3498db")
            
            updated_embed = await build_embed(interaction, f_heading, f_desc, f_colour, image, thumbnail, author, footer)
            plain_text = parse_variables(text, interaction) if text else message.content

            await message.edit(content=plain_text, embed=updated_embed)
            
            success_msg = discord.Embed(description="✅ Embed edited successfully!", color=discord.Color.green())
            await interaction.response.send_message(embed=success_msg, ephemeral=True)
            
        except Exception as e:
            await interaction.response.send_message(f"❌ Editing failed: {str(e)}", ephemeral=True)

# Register the group
bot.tree.add_command(EmbedGroup())

# --- 5. EXECUTION ---
if TOKEN:
    # Start Web Server in background thread
    threading.Thread(target=run_web, daemon=True).start()
    # Start Discord Bot
    bot.run(TOKEN)
else:
    print("FATAL ERROR: DISCORD_TOKEN environment variable not found.")
    
