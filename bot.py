import discord
import os
from discord.ext import commands
from discord import app_commands
from datetime import datetime

# --- Configuration via Environment Variables ---
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
        # This registers the slash commands
        await self.tree.sync()
        print(f"Logged in as {self.user} | Owner ID: {OWNER_ID}")

bot = MyBot()

def parse_variables(content, interaction: discord.Interaction):
    if not content: return content
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
    hex_str = colour.lstrip("#")
    embed_color = int(hex_str, 16)
    
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

# --- This is the fix for the Group Error ---
class EmbedGroup(app_commands.Group, name="embed"):
    """Group for embed commands"""

    @app_commands.command(name="create", description="Create and send an embed")
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
            
            success_embed = discord.Embed(description="✅ Your embed was successfully created!", color=discord.Color.green())
            await interaction.response.send_message(embed=success_embed, ephemeral=True)
            await interaction.channel.send(content=plain_text, embed=main_embed)
        except Exception as e:
            error_embed = discord.Embed(description=f"❌ Failed: {str(e)}", color=discord.Color.red())
            await interaction.followup.send(embed=error_embed, ephemeral=True)

    @app_commands.command(name="edit", description="Edit an existing embed")
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
            msg_id = int(message_url.split('/')[-1])
            message = await interaction.channel.fetch_message(msg_id)
            old_embed = message.embeds[0] if message.embeds else None
            
            final_heading = heading or (old_embed.title if old_embed else "Title")
            final_desc = description or (old_embed.description if old_embed else "Description")
            final_colour = colour or (hex(old_embed.color.value).replace('0x', '') if old_embed else "FFFFFF")
            
            updated_embed = await build_embed(interaction, final_heading, final_desc, final_colour, image, thumbnail, author, footer)
            plain_text = parse_variables(text, interaction) if text else message.content

            await message.edit(content=plain_text, embed=updated_embed)
            
            success_embed = discord.Embed(description="✅ Embed edited successfully!", color=discord.Color.green())
            await interaction.response.send_message(embed=success_embed, ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

# Add the group to the bot tree
bot.tree.add_command(EmbedGroup())

if TOKEN:
    bot.run(TOKEN)
else:
    print("No DISCORD_TOKEN found.")
    
