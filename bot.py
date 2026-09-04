import discord
import os
import re
import hashlib
import threading
from flask import Flask
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, timedelta, timezone

import libsql_client
from better_profanity import profanity
import aiohttp

# =========================================================================
# SECTION A: CONFIG
# =========================================================================

# --- Discord IDs ---
SENIOR_MOD_ROLE_ID = 1485117523555909632
JUNIOR_MOD_ROLE_ID = 1485115911286423552

MODERATION_CHANNEL_ID = 1480529217597866084
MOD_LOG_CHANNEL_ID = 1480530316253724732

PARTNERSHIP_CHANNEL_ID = 1497940669845602345
SELF_PROMO_CHANNEL_ID = 1536726868072464425
ALLOWED_INVITE_CHANNELS = {PARTNERSHIP_CHANNEL_ID, SELF_PROMO_CHANNEL_ID}

# IMPORTANT: set this to the role ID of your "Muted" role (must already
# exist in the server, with permission overrides blocking it from sending
# messages/talking in every channel except mod-ticket threads).
# Falls back to searching for a role literally named "Muted" if left as 0.
MUTED_ROLE_ID = int(os.getenv("MUTED_ROLE_ID", "0"))

# --- Env vars (set these in Render's environment settings) ---
TOKEN = os.getenv('DISCORD_TOKEN')
try:
    OWNER_ID = int(os.getenv('OWNER_ID'))
except (TypeError, ValueError):
    OWNER_ID = 0

TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

# Free text-toxicity AI backup - get a token at https://huggingface.co
# (Settings -> Access Tokens -> Create new token -> Read). No billing required;
# free tier includes a small monthly credit allowance, plenty for moderation checks.
HF_API_TOKEN = os.getenv("HF_API_TOKEN")
HF_TOXICITY_MODEL = "unitary/toxic-bert"
HF_INFERENCE_URL = f"https://api-inference.huggingface.co/models/{HF_TOXICITY_MODEL}"
HF_THRESHOLD = 0.90  # 0-1 score; anything at/above this is flagged (specific categories only)
HF_THREAT_THRESHOLD = 0.96  # stricter bar - "threat" easily misfires on hyperbolic jokes ("imma kill you")
HF_SPECIFIC_LABELS = {"severe_toxic", "obscene", "insult", "identity_hate"}  # generic "toxic" is excluded - too noisy
HF_SEVERE_LABELS = {"severe_toxic", "threat", "identity_hate"}

# Free-tier avatar/image NSFW check - get keys at
# https://sightengine.com (free tier, no credit card required)
SIGHTENGINE_API_USER = os.getenv("SIGHTENGINE_API_USER")
SIGHTENGINE_API_SECRET = os.getenv("SIGHTENGINE_API_SECRET")
SIGHTENGINE_URL = "https://api.sightengine.com/1.0/check.json"
SIGHTENGINE_THRESHOLD = 0.5  # 0-1 score

# --- Auto-moderation tuning ---
SPAM_MESSAGE_THRESHOLD = 4
SPAM_TIME_WINDOW_SECONDS = 15

# Custom words to always block outright, on top of the better-profanity
# library's built-in list. Add whatever you want here.
EXTRA_BAD_WORDS = [
    # "example_word",
]

SUSPICIOUS_USERNAME_KEYWORDS = {
    "link", "18+", "onlyfans", "nsfw", "promo", "freenitro",
    "nitrofree", "porn", "xxx", "cp", "leak", "leaked",
}

NSFW_LINK_KEYWORDS = {
    "onlyfans.com", "pornhub", "xvideos", "xnxx", "chaturbate",
    "spankbang", "xhamster", "redtube",
}

NSFW_TELEGRAM_KEYWORDS = {
    "18+", "nsfw", "porn", "leak", "xxx", "hot", "onlyfans",
}

INVITE_REGEX = re.compile(
    r"(discord\.gg/|discord(?:app)?\.com/invite/)[a-zA-Z0-9-]+", re.IGNORECASE
)

# Case type -> human description template (uses {user} and {channel})
CASE_DESCRIPTIONS = {
    "bad_words": "{user} used bad words in {channel}",
    "invite_link": "{user} posted a server invite link in {channel}",
    "nsfw_link": "{user} posted an 18+ link in {channel}",
    "spam": "{user} was spamming messages in {channel}",
    "suspicious_profile": "{user} has a suspicious username/profile picture",
}

# --- Moderation-action / duration system (used by the modal + edit flows) ---
ACTION_LABELS = {"kick": "Kick", "ban": "Ban", "mute": "Mute", "timeout": "Timeout", "skip": "Skip"}

DURATION_KEYS_ORDER = [
    "permanent", "1_minute", "5_minutes", "10_minutes", "1_hour", "12_hour",
    "1_day", "3_days", "1_week", "2_weeks", "1_month", "2_months", "6_months", "1_year",
]
DURATION_LABELS = {
    "permanent": "Permanent", "1_minute": "1 Minute", "5_minutes": "5 Minutes",
    "10_minutes": "10 Minutes", "1_hour": "1 Hour", "12_hour": "12 Hour",
    "1_day": "1 Day", "3_days": "3 Days", "1_week": "1 Week", "2_weeks": "2 Weeks",
    "1_month": "1 Month", "2_months": "2 Months", "6_months": "6 Months", "1_year": "1 Year",
}
DURATION_DELTAS = {
    "permanent": None,
    "1_minute": timedelta(minutes=1), "5_minutes": timedelta(minutes=5), "10_minutes": timedelta(minutes=10),
    "1_hour": timedelta(hours=1), "12_hour": timedelta(hours=12), "1_day": timedelta(days=1),
    "3_days": timedelta(days=3), "1_week": timedelta(weeks=1), "2_weeks": timedelta(weeks=2),
    "1_month": timedelta(days=30), "2_months": timedelta(days=60), "6_months": timedelta(days=180),
    "1_year": timedelta(days=365),
}
VALID_DURATIONS_BY_ACTION = {
    "ban": DURATION_KEYS_ORDER,
    "mute": DURATION_KEYS_ORDER,
    "timeout": ["1_minute", "5_minutes", "10_minutes", "1_hour", "1_day", "1_week"],
    "kick": [],
    "skip": [],
}
DEFAULT_DURATION_BY_ACTION = {"ban": "permanent", "mute": "permanent", "timeout": "1_week", "kick": None, "skip": None}


def clamp_duration(action_type: str, duration_key):
    """Kick/Skip never take a duration. Otherwise, if the chosen duration
    isn't valid for this action, snap to the closest valid one by actual
    time distance (e.g. Timeout + 12 Hour -> Timeout + 1 Hour, since that's
    numerically closer than the next step up, 1 Day)."""
    if action_type in ("kick", "skip"):
        return None
    allowed = VALID_DURATIONS_BY_ACTION.get(action_type, [])
    if not allowed:
        return None
    if not duration_key or duration_key not in DURATION_DELTAS:
        return DEFAULT_DURATION_BY_ACTION[action_type]
    if duration_key in allowed:
        return duration_key

    target = DURATION_DELTAS[duration_key]
    if target is None:
        # "Permanent" requested but not valid for this action (e.g. Timeout) -
        # substitute the longest duration this action actually allows.
        return allowed[-1]

    best_key, best_diff = None, None
    for key in allowed:
        delta = DURATION_DELTAS[key]
        if delta is None:
            continue
        diff = abs((delta - target).total_seconds())
        if best_diff is None or diff < best_diff:
            best_key, best_diff = key, diff
    return best_key or allowed[0]


profanity.load_censor_words(whitelist_words=[])
if EXTRA_BAD_WORDS:
    profanity.add_censor_words(EXTRA_BAD_WORDS)


# =========================================================================
# SECTION B: DATABASE (Turso / libsql)
# =========================================================================

class Database:
    def __init__(self):
        self.client: libsql_client.Client | None = None

    async def connect(self):
        if not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
            raise RuntimeError("TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be set as env vars.")
        self.client = libsql_client.create_client(url=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
        await self._create_schema()

    async def close(self):
        if self.client:
            await self.client.close()

    async def _create_schema(self):
        statements = [
            """
            CREATE TABLE IF NOT EXISTS cases (
                case_id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_type TEXT NOT NULL,
                guild_id TEXT NOT NULL,
                target_user_id TEXT NOT NULL,
                reason TEXT,
                proof_url TEXT,
                message_link TEXT,
                message_deleted INTEGER DEFAULT 0,
                status TEXT DEFAULT 'open',
                claimed_by_id TEXT,
                claimed_at TEXT,
                action_type TEXT,
                action_expires_at TEXT,
                automod INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS active_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL,
                guild_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                expires_at TEXT,
                resolved INTEGER DEFAULT 0
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS message_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS log_sequence (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_active_actions_resolved ON active_actions (resolved)",
            "CREATE INDEX IF NOT EXISTS idx_message_history_user ON message_history (user_id, created_at)",
        ]
        for stmt in statements:
            await self.client.execute(stmt)

        # Migration: add channel_id if this DB was created before this column
        # existed. Harmless no-op (caught) if it's already there.
        try:
            await self.client.execute("ALTER TABLE cases ADD COLUMN channel_id TEXT")
        except Exception:
            pass
        try:
            await self.client.execute("ALTER TABLE cases ADD COLUMN reason_by_moderator TEXT")
        except Exception:
            pass
        try:
            await self.client.execute("ALTER TABLE cases ADD COLUMN ticket_close_reason TEXT")
        except Exception:
            pass

        print("[database] Turso schema ready.")

    # ---- case CRUD ----
    async def create_case(self, case_type, guild_id, target_user_id, reason=None,
                           proof_url=None, message_link=None, message_deleted=False, automod=False,
                           channel_id=None):
        rs = await self.client.execute(
            """INSERT INTO cases (case_type, guild_id, target_user_id, reason, proof_url,
               message_link, message_deleted, automod, channel_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING case_id""",
            [case_type, str(guild_id), str(target_user_id), reason, proof_url,
             message_link, int(message_deleted), int(automod), str(channel_id) if channel_id else None, now_iso()],
        )
        return rs.rows[0][0]

    async def get_case(self, case_id):
        rs = await self.client.execute("SELECT * FROM cases WHERE case_id = ?", [case_id])
        if not rs.rows:
            return None
        return dict(zip(rs.columns, rs.rows[0]))

    async def claim_case(self, case_id, moderator_id):
        await self.client.execute(
            "UPDATE cases SET status='claimed', claimed_by_id=?, claimed_at=? WHERE case_id=?",
            [str(moderator_id), now_iso(), case_id],
        )

    async def set_case_action_type(self, case_id, action_type):
        await self.client.execute(
            "UPDATE cases SET action_type=? WHERE case_id=?", [action_type, case_id]
        )

    async def finalize_case(self, case_id, action_type, expires_at_iso, message_deleted=None):
        if message_deleted is None:
            await self.client.execute(
                "UPDATE cases SET status='resolved', action_type=?, action_expires_at=? WHERE case_id=?",
                [action_type, expires_at_iso, case_id],
            )
        else:
            await self.client.execute(
                "UPDATE cases SET status='resolved', action_type=?, action_expires_at=?, message_deleted=? WHERE case_id=?",
                [action_type, expires_at_iso, int(message_deleted), case_id],
            )

    async def reset_case_for_edit(self, case_id):
        await self.client.execute(
            "UPDATE cases SET status='claimed', action_type=NULL, action_expires_at=NULL WHERE case_id=?",
            [case_id],
        )

    async def clear_action(self, case_id):
        """Used by 'Remove Moderation Action' - clears the action but keeps
        the case claimed (still resolved status stays as-is, it's just
        showing no active action anymore)."""
        await self.client.execute(
            "UPDATE cases SET action_type=NULL, action_expires_at=NULL WHERE case_id=?", [case_id]
        )

    async def update_action_expiry(self, case_id, expires_at_iso):
        await self.client.execute(
            "UPDATE cases SET action_expires_at=? WHERE case_id=?", [expires_at_iso, case_id]
        )
        await self.client.execute(
            "UPDATE active_actions SET expires_at=? WHERE case_id=? AND resolved=0", [expires_at_iso, case_id]
        )

    async def set_moderator_reason(self, case_id, reason_text):
        await self.client.execute(
            "UPDATE cases SET reason_by_moderator=? WHERE case_id=?", [reason_text, case_id]
        )

    async def set_ticket_close_reason(self, case_id, reason_text):
        await self.client.execute(
            "UPDATE cases SET ticket_close_reason=? WHERE case_id=?", [reason_text, case_id]
        )

    async def resolve_active_actions_for_case(self, case_id):
        await self.client.execute(
            "UPDATE active_actions SET resolved=1 WHERE case_id=? AND resolved=0", [case_id]
        )

    async def mark_message_deleted(self, case_id, proof_text=None):
        """Marks the case's message as deleted, blanks the message link, and
        optionally stores proof text (e.g. the message's original content)."""
        if proof_text is not None:
            await self.client.execute(
                "UPDATE cases SET message_deleted=1, message_link=NULL, proof_url=? WHERE case_id=?",
                [proof_text, case_id],
            )
        else:
            await self.client.execute(
                "UPDATE cases SET message_deleted=1, message_link=NULL WHERE case_id=?",
                [case_id],
            )

    # ---- log_sequence (independent numbering for mod-log embeds) ----
    async def next_log_number(self):
        rs = await self.client.execute(
            "INSERT INTO log_sequence (created_at) VALUES (?) RETURNING log_id", [now_iso()]
        )
        return rs.rows[0][0]

    # ---- active_actions ----
    async def create_active_action(self, case_id, guild_id, user_id, action_type, expires_at_iso):
        await self.client.execute(
            """INSERT INTO active_actions (case_id, guild_id, user_id, action_type, expires_at)
               VALUES (?, ?, ?, ?, ?)""",
            [case_id, str(guild_id), str(user_id), action_type, expires_at_iso],
        )

    async def get_expired_actions(self):
        rs = await self.client.execute(
            "SELECT * FROM active_actions WHERE resolved=0 AND expires_at IS NOT NULL AND expires_at <= ?",
            [now_iso()],
        )
        return [dict(zip(rs.columns, row)) for row in rs.rows]

    async def resolve_active_action(self, action_id):
        await self.client.execute("UPDATE active_actions SET resolved=1 WHERE id=?", [action_id])

    # ---- message_history (spam detection) ----
    async def log_message(self, guild_id, user_id, channel_id, content_hash):
        await self.client.execute(
            """INSERT INTO message_history (guild_id, user_id, channel_id, content_hash, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [str(guild_id), str(user_id), str(channel_id), content_hash, now_iso()],
        )

    async def count_recent_matches(self, guild_id, user_id, content_hash, since_iso):
        rs = await self.client.execute(
            """SELECT COUNT(*) FROM message_history
               WHERE guild_id=? AND user_id=? AND content_hash=? AND created_at >= ?""",
            [str(guild_id), str(user_id), content_hash, since_iso],
        )
        return rs.rows[0][0]


db = Database()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_iso(s):
    return datetime.fromisoformat(s)


def format_dt(dt: datetime) -> str:
    return dt.strftime("%d %B %Y, %H:%M UTC")


def discord_ts(dt: datetime) -> str:
    """Discord's native short date/time timestamp - renders localized and
    live-updating on the user's client, e.g. <t:1735689600:f>."""
    return f"<t:{int(dt.timestamp())}:f>"


# =========================================================================
# SECTION C: BOT SETUP
# =========================================================================

app = Flask('')

@app.route('/')
def home():
    return "Bot is online!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)


class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await db.connect()
        self.http_session = aiohttp.ClientSession()

        # Persistent views - registered once here so buttons/dropdowns on
        # OLD messages keep working after every Render redeploy. These
        # views read all state (case id, action, etc.) live from the
        # message/DB at click-time rather than from anything stored on
        # the view instance itself, which is what makes this safe.
        self.add_view(ClaimView())
        self.add_view(ActionButtonView())
        self.add_view(EditChoiceView())
        self.add_view(ChangeDurationView())
        self.add_view(PostActionView())
        self.add_view(TicketCloseView())

        await self.tree.sync()
        expiry_checker.start()
        print(f"Logged in as {self.user} | Owner ID: {OWNER_ID}")

    async def close(self):
        await db.close()
        if getattr(self, "http_session", None):
            await self.http_session.close()
        await super().close()

bot = MyBot()


def is_moderator(member: discord.Member) -> bool:
    if member.id == OWNER_ID:
        return True
    role_ids = {r.id for r in member.roles}
    return SENIOR_MOD_ROLE_ID in role_ids or JUNIOR_MOD_ROLE_ID in role_ids


def get_muted_role(guild: discord.Guild):
    if MUTED_ROLE_ID:
        role = guild.get_role(MUTED_ROLE_ID)
        if role:
            return role
    return discord.utils.get(guild.roles, name="Muted")


# =========================================================================
# SECTION D: EMBED COMMANDS (existing feature, unchanged)
# =========================================================================

def parse_variables(content, interaction: discord.Interaction):
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
    hex_str = colour.lstrip("#")
    try:
        embed_color = int(hex_str, 16)
    except ValueError:
        embed_color = 0x3498db

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

class EmbedGroup(app_commands.Group, name="embed"):
    """All /embed commands reside here."""

    @app_commands.command(name="create", description="Create an owner-only embed")
    async def create(
        self, interaction: discord.Interaction, heading: str, description: str, colour: str,
        text: str = None, image: str = None, thumbnail: str = None, author: str = None, footer: str = None
    ):
        if interaction.user.id != OWNER_ID:
            return await interaction.response.send_message("❌ Access Denied: Owner Only.", ephemeral=True)
        try:
            main_embed = await build_embed(interaction, heading, description, colour, image, thumbnail, author, footer)
            plain_text = parse_variables(text, interaction) if text else None
            success_msg = discord.Embed(description="✅ Your embed was successfully created!", color=discord.Color.green())
            await interaction.response.send_message(embed=success_msg, ephemeral=True)
            await interaction.channel.send(content=plain_text, embed=main_embed)
        except Exception as e:
            error_msg = discord.Embed(description=f"❌ Embed creation failed: {str(e)}", color=discord.Color.red())
            await interaction.followup.send(embed=error_msg, ephemeral=True)

    @app_commands.command(name="edit", description="Edit an existing embed via message URL")
    async def edit(
        self, interaction: discord.Interaction, message_url: str, heading: str = None, description: str = None,
        colour: str = None, text: str = None, image: str = None, thumbnail: str = None, author: str = None, footer: str = None
    ):
        if interaction.user.id != OWNER_ID:
            return await interaction.response.send_message("❌ Access Denied: Owner Only.", ephemeral=True)
        try:
            msg_id = int(message_url.split('/')[-1])
            message = await interaction.channel.fetch_message(msg_id)
            old_embed = message.embeds[0] if message.embeds else None
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

bot.tree.add_command(EmbedGroup())


# =========================================================================
# SECTION E: CASE EMBED + MOD LOG EMBED BUILDERS
# =========================================================================

def build_case_description(case: dict) -> str:
    case_type = case["case_type"]
    target_user_id = case["target_user_id"]
    user_mention = f"<@{target_user_id}>"
    channel_mention = f"<#{case['channel_id']}>" if case.get("channel_id") else ""

    template = CASE_DESCRIPTIONS.get(case_type, "{user} triggered a moderation case")
    return template.format(user=user_mention, channel=channel_mention)


def build_moderation_details_block(case: dict) -> str:
    claimed_by = f"<@{case['claimed_by_id']}>" if case["claimed_by_id"] else "Unclaimed"
    claimed_at = discord_ts(parse_iso(case["claimed_at"])) if case["claimed_at"] else "-"
    action = ACTION_LABELS.get(case["action_type"], case["action_type"].title() if case["action_type"] else None) or "Pending"
    if case["action_expires_at"]:
        action += f" till {discord_ts(parse_iso(case['action_expires_at']))}"
    msg_deleted = "Yes" if case["message_deleted"] else "No"
    proof_value = case["proof_url"] if case["proof_url"] else "-"

    return (
        f"## Moderation Details\n\n"
        f"### Claimed by:\n"
        f"{claimed_by}\n\n"
        f"### Claimed at:\n"
        f"{claimed_at}\n\n"
        f"### Action taken:\n"
        f"{action}\n\n"
        f"### Message deleted:\n"
        f"{msg_deleted}\n\n"
        f"### Proof:\n"
        f"{proof_value}"
    )


def build_case_embed(case: dict, guild: discord.Guild) -> discord.Embed:
    case_id = case["case_id"]
    reason_line = build_case_description(case)

    color = discord.Color.orange() if case["status"] == "open" else discord.Color.gold()
    message_link_value = case["message_link"] if case["message_link"] else "\u200b"

    description = (
        f"{reason_line}\n\n"
        f"### Message Link\n"
        f"{message_link_value}\n\n"
        f"{build_moderation_details_block(case)}"
    )

    embed = discord.Embed(title=f"Case #{case_id}", description=description, color=color)
    embed.timestamp = datetime.now(timezone.utc)
    return embed


def build_ticket_case_embed(case: dict) -> discord.Embed:
    """Same as the moderation case embed but without the Message Link
    section - used for the summary posted inside a ticket thread."""
    reason_line = build_case_description(case)
    description = f"{reason_line}\n\n{build_moderation_details_block(case)}"
    embed = discord.Embed(title=f"Case #{case['case_id']}", description=description, color=discord.Color.red())
    embed.timestamp = datetime.now(timezone.utc)
    return embed



async def post_mod_log(guild: discord.Guild, case: dict, action_label: str, user: discord.abc.User,
                        moderator_display: str, reason: str, expires_at, colour):
    channel = guild.get_channel(MOD_LOG_CHANNEL_ID)
    if not channel:
        return
    log_id = await db.next_log_number()

    lines = [
        f"### User\n{user} ({user.mention}) `{user.id}`",
        f"### Moderator\n{moderator_display}",
    ]
    if expires_at:
        lines.append(f"### Expires\n{discord_ts(expires_at)}")
    lines.append(f"### Reason\n{reason or 'No reason provided'}")

    embed = discord.Embed(
        title=f"Case #{log_id} | {action_label}",
        description="\n\n".join(lines),
        color=colour,
    )
    embed.timestamp = datetime.now(timezone.utc)
    await channel.send(embed=embed)


async def post_expiry_log(guild: discord.Guild, case_id: int, action_label: str, user_id: int):
    channel = guild.get_channel(MOD_LOG_CHANNEL_ID)
    if not channel:
        return
    try:
        user = await bot.fetch_user(user_id)
        user_display = f"{user} ({user.mention}) `{user.id}`"
    except discord.NotFound:
        user_display = f"`{user_id}`"

    expired_label_map = {"Unban": "Ban", "Unmute": "Mute", "Untimeout": "Timeout"}
    duration_kind = expired_label_map.get(action_label, action_label)

    log_id = await db.next_log_number()
    description = (
        f"### User\n{user_display}\n\n"
        f"### Moderator\nAutomod (Anime World)\n\n"
        f"### Reason\n{duration_kind} duration expired"
    )

    embed = discord.Embed(
        title=f"Case #{log_id} | {action_label}",
        description=description,
        color=discord.Color.green(),
    )
    embed.timestamp = datetime.now(timezone.utc)
    await channel.send(embed=embed)


# =========================================================================
# SECTION F: PERSISTENT VIEWS (Claim -> Action select -> Time select -> Edit/Ticket)
# =========================================================================

async def get_case_id_from_message(message: discord.Message) -> int | None:
    if not message.embeds:
        return None
    title = message.embeds[0].title or ""
    m = re.search(r"#(\d+)", title)
    return int(m.group(1)) if m else None


class ClaimView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, custom_id="modsys:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_moderator(interaction.user):
            return await interaction.response.send_message("❌ Moderators only.", ephemeral=True)

        case_id = await get_case_id_from_message(interaction.message)
        case = await db.get_case(case_id)
        if not case:
            return await interaction.response.send_message("❌ Case not found.", ephemeral=True)
        if case["claimed_by_id"]:
            return await interaction.response.send_message(
                f"❌ Already claimed by <@{case['claimed_by_id']}>.", ephemeral=True
            )

        await db.claim_case(case_id, interaction.user.id)
        case = await db.get_case(case_id)
        embed = build_case_embed(case, interaction.guild)
        await interaction.response.edit_message(embed=embed, view=ActionButtonView())


class ActionButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Action", style=discord.ButtonStyle.primary, custom_id="modsys:action_button")
    async def action(self, interaction: discord.Interaction, button: discord.ui.Button):
        case_id = await get_case_id_from_message(interaction.message)
        case = await db.get_case(case_id)
        if not case or not case["claimed_by_id"] or int(case["claimed_by_id"]) != interaction.user.id:
            return await interaction.response.send_message(
                "❌ Only the moderator who claimed this case can act on it.", ephemeral=True
            )
        await interaction.response.send_modal(ModerationActionModal(case_id=case_id))


class ModerationActionModal(discord.ui.Modal):
    """One modal used both for the first action on a case and for
    'Change Moderation Action' during editing. Select menus inside modals
    are a very recent Discord platform feature (added within the last few
    weeks); each select must be wrapped in a discord.ui.Label."""

    def __init__(self, case_id: int, prefill_reason: str = None):
        super().__init__(title="Moderation Action", custom_id="modsys:modal", timeout=None)
        self.case_id = case_id

        self.action_select = discord.ui.Select(
            custom_id="modsys:modal_action", min_values=1, max_values=1, required=True,
            options=[discord.SelectOption(label=v) for v in ACTION_LABELS.values()],
        )
        self.duration_select = discord.ui.Select(
            custom_id="modsys:modal_duration", min_values=0, max_values=1, required=False,
            options=[discord.SelectOption(label=DURATION_LABELS[k], value=k) for k in DURATION_KEYS_ORDER],
        )
        self.reason_input = discord.ui.TextInput(
            label="Reason", custom_id="modsys:modal_reason", style=discord.TextStyle.paragraph,
            required=False, max_length=1000, default=prefill_reason,
        )

        self.add_item(discord.ui.Label(text="Moderation Actions", component=self.action_select))
        self.add_item(discord.ui.Label(text="Duration", component=self.duration_select))
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction):
        action_type = self.action_select.values[0].lower()
        duration_raw = self.duration_select.values[0] if self.duration_select.values else None
        duration_key = clamp_duration(action_type, duration_raw)
        reason_text = self.reason_input.value.strip() if self.reason_input.value else None

        case = await db.get_case(self.case_id)
        is_edit = bool(case and case["action_type"])
        await apply_moderation_action(interaction, self.case_id, action_type, duration_key,
                                       reason_text, is_edit_change_action=is_edit)


async def revert_current_action(case: dict, guild: discord.Guild):
    """Undoes whatever punishment is currently live for this case (used
    before applying a replacement action, and for 'Remove Moderation
    Action')."""
    action_type = case["action_type"]
    if not action_type or action_type in ("skip", "kick"):
        pass
    else:
        user_id = int(case["target_user_id"])
        try:
            if action_type == "ban":
                await guild.unban(discord.Object(id=user_id), reason="Moderation action changed/removed")
            elif action_type == "mute":
                member = guild.get_member(user_id)
                role = get_muted_role(guild)
                if member and role:
                    await member.remove_roles(role, reason="Moderation action changed/removed")
            elif action_type == "timeout":
                member = guild.get_member(user_id)
                if member:
                    await member.timeout(None, reason="Moderation action changed/removed")
        except (discord.NotFound, discord.Forbidden):
            pass
    await db.resolve_active_actions_for_case(case["case_id"])


async def delete_case_message(guild: discord.Guild, message_link: str):
    """Attempts to delete the original offending message and returns its
    text content (for use as Proof) if it could be fetched first."""
    try:
        parts = message_link.split('/')
        channel_id = int(parts[-2])
        msg_id = int(parts[-1])
        channel = guild.get_channel(channel_id)
        if not channel:
            return None
        msg = await channel.fetch_message(msg_id)
        content = msg.content or None
        await msg.delete()
        return content
    except (discord.NotFound, discord.Forbidden, ValueError, IndexError):
        return None


async def apply_moderation_action(interaction: discord.Interaction, case_id: int, action_type: str,
                                   duration_key, reason_text, is_edit_change_action: bool = False):
    case = await db.get_case(case_id)
    guild = interaction.guild
    member = guild.get_member(int(case["target_user_id"]))
    case_reason = build_case_description(case)

    if is_edit_change_action:
        await revert_current_action(case, guild)

    delta = DURATION_DELTAS.get(duration_key) if duration_key else None
    expires_at = (datetime.now(timezone.utc) + delta) if delta else None
    expires_at_iso = expires_at.isoformat() if expires_at else None

    try:
        if action_type == "kick" and member:
            await member.kick(reason=case_reason)
        elif action_type == "ban":
            await guild.ban(member or discord.Object(id=int(case["target_user_id"])), reason=case_reason)
            await db.create_active_action(case_id, guild.id, case["target_user_id"], "ban", expires_at_iso)
        elif action_type == "mute" and member:
            role = get_muted_role(guild)
            if role:
                await member.add_roles(role, reason=case_reason)
            await db.create_active_action(case_id, guild.id, case["target_user_id"], "mute", expires_at_iso)
        elif action_type == "timeout" and member:
            await member.timeout(delta, reason=case_reason)
            await db.create_active_action(case_id, guild.id, case["target_user_id"], "timeout", expires_at_iso)
        # "skip" -> no action taken
    except discord.Forbidden:
        pass

    if action_type != "skip" and not case["message_deleted"] and case["message_link"]:
        content = await delete_case_message(guild, case["message_link"])
        await db.mark_message_deleted(case_id, proof_text=content)

    await db.finalize_case(case_id, action_type, expires_at_iso)
    if reason_text:
        await db.set_moderator_reason(case_id, reason_text)
    case = await db.get_case(case_id)

    if action_type != "skip":
        try:
            user = member or await bot.fetch_user(int(case["target_user_id"]))
            moderator_display = f"<@{case['claimed_by_id']}>" if not case["automod"] else "Automod (Anime World)"
            log_reason = "Changed Moderation Action" if is_edit_change_action else case_reason
            await post_mod_log(guild, case, ACTION_LABELS[action_type], user, moderator_display,
                                log_reason, expires_at, discord.Color.red())
        except discord.NotFound:
            pass

    embed = build_case_embed(case, guild)
    view = discord.ui.View() if action_type == "skip" else PostActionView()
    await interaction.response.edit_message(embed=embed, view=view)


class EditChoiceSelect(discord.ui.Select):
    def __init__(self, duration_capable: bool = True):
        if duration_capable:
            options = [
                discord.SelectOption(label="Remove Moderation Action", value="remove"),
                discord.SelectOption(label="Change Duration", value="change_duration"),
                discord.SelectOption(label="Change Moderation Action", value="change_action"),
                discord.SelectOption(label="Cancel", value="cancel"),
            ]
        else:
            options = [
                discord.SelectOption(label="Change Moderation Action", value="change_action"),
                discord.SelectOption(label="Cancel", value="cancel"),
            ]
        super().__init__(placeholder="Edit", options=options, custom_id="modsys:edit_choice",
                          min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        case_id = await get_case_id_from_message(interaction.message)
        case = await db.get_case(case_id)
        if not case or not case["claimed_by_id"] or int(case["claimed_by_id"]) != interaction.user.id:
            return await interaction.response.send_message("❌ Not your case to act on.", ephemeral=True)

        choice = self.values[0]
        guild = interaction.guild

        if choice == "cancel":
            embed = build_case_embed(case, guild)
            await interaction.response.edit_message(embed=embed, view=PostActionView())
            return

        if choice == "remove":
            await revert_current_action(case, guild)
            await db.clear_action(case_id)
            case = await db.get_case(case_id)
            user = guild.get_member(int(case["target_user_id"])) or await bot.fetch_user(int(case["target_user_id"]))
            await post_mod_log(guild, case, "Removed Action", user, f"<@{interaction.user.id}>",
                                "Removed Moderation Action", None, discord.Color.red())
            embed = build_case_embed(case, guild)
            await interaction.response.edit_message(embed=embed, view=PostActionView())
            return

        if choice == "change_duration":
            allowed = VALID_DURATIONS_BY_ACTION.get(case["action_type"], [])
            embed = build_case_embed(case, guild)
            await interaction.response.edit_message(embed=embed, view=ChangeDurationView(allowed))
            return

        if choice == "change_action":
            await interaction.response.send_modal(
                ModerationActionModal(case_id=case_id, prefill_reason=case.get("reason_by_moderator"))
            )
            return


class EditChoiceView(discord.ui.View):
    def __init__(self, duration_capable: bool = True):
        super().__init__(timeout=None)
        self.add_item(EditChoiceSelect(duration_capable))


class ChangeDurationSelect(discord.ui.Select):
    def __init__(self, allowed_keys=None):
        keys = allowed_keys or DURATION_KEYS_ORDER
        options = [discord.SelectOption(label=DURATION_LABELS[k], value=k) for k in keys]
        super().__init__(placeholder="Duration", options=options, custom_id="modsys:change_duration",
                          min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        case_id = await get_case_id_from_message(interaction.message)
        case = await db.get_case(case_id)
        if not case or not case["claimed_by_id"] or int(case["claimed_by_id"]) != interaction.user.id:
            return await interaction.response.send_message("❌ Not your case to act on.", ephemeral=True)

        duration_key = self.values[0]
        guild = interaction.guild
        member = guild.get_member(int(case["target_user_id"]))
        action_type = case["action_type"]
        delta = DURATION_DELTAS.get(duration_key)
        expires_at = (datetime.now(timezone.utc) + delta) if delta else None
        expires_at_iso = expires_at.isoformat() if expires_at else None

        try:
            if action_type == "timeout" and member:
                await member.timeout(delta, reason="Duration changed by moderator")
        except discord.Forbidden:
            pass

        await db.update_action_expiry(case_id, expires_at_iso)
        case = await db.get_case(case_id)

        user = member or await bot.fetch_user(int(case["target_user_id"]))
        await post_mod_log(guild, case, ACTION_LABELS.get(action_type, "Updated"), user,
                            f"<@{interaction.user.id}>", "Changed duration", expires_at, discord.Color.red())

        embed = build_case_embed(case, guild)
        await interaction.response.edit_message(embed=embed, view=PostActionView())


class ChangeDurationView(discord.ui.View):
    def __init__(self, allowed_keys=None):
        super().__init__(timeout=None)
        self.add_item(ChangeDurationSelect(allowed_keys))


class PostActionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.secondary, custom_id="modsys:edit")
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        case_id = await get_case_id_from_message(interaction.message)
        case = await db.get_case(case_id)
        if not case or not case["claimed_by_id"] or int(case["claimed_by_id"]) != interaction.user.id:
            return await interaction.response.send_message("❌ Not your case to edit.", ephemeral=True)
        duration_capable = case["action_type"] in ("mute", "ban", "timeout")
        embed = build_case_embed(case, interaction.guild)
        await interaction.response.edit_message(embed=embed, view=EditChoiceView(duration_capable))

    @discord.ui.button(label="Ticket", style=discord.ButtonStyle.primary, custom_id="modsys:ticket")
    async def ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        case_id = await get_case_id_from_message(interaction.message)
        case = await db.get_case(case_id)
        if not case:
            return await interaction.response.send_message("❌ Case not found.", ephemeral=True)
        if not case["claimed_by_id"] or int(case["claimed_by_id"]) != interaction.user.id:
            return await interaction.response.send_message("❌ Not your case.", ephemeral=True)
        if case["action_type"] in ("ban", "kick"):
            return await interaction.response.send_message(
                "❌ No ticket is opened for bans/kicks.", ephemeral=True
            )

        # Thread creation + two sends can take longer than Discord's 3s
        # interaction window, which is what was causing "didn't respond in
        # time" - defer immediately so we have room to finish the setup.
        await interaction.response.defer(ephemeral=True, thinking=True)

        guild = interaction.guild
        mod_channel = guild.get_channel(MODERATION_CHANNEL_ID)
        thread = await mod_channel.create_thread(
            name=f"Mod-ticket #{case_id}", type=discord.ChannelType.private_thread
        )
        target_user = guild.get_member(int(case["target_user_id"]))

        # Mentioning a non-member in a thread message silently adds them as
        # a thread member, unlike thread.add_user() which posts a visible
        # "X added Y to the thread" system message - this avoids that.
        mention_line = " ".join(m for m in (interaction.user.mention, target_user.mention if target_user else None) if m)

        opened_embed = discord.Embed(
            description=(
                f"{interaction.user.mention} opened ticket.\n\n"
                f"### Created at:\n{discord_ts(datetime.now(timezone.utc))}"
            ),
            color=discord.Color.green(),
        )
        opened_embed.timestamp = datetime.now(timezone.utc)
        await thread.send(content=mention_line, embed=opened_embed, view=TicketCloseView())

        await thread.send(embed=build_ticket_case_embed(case))

        await interaction.followup.send(f"✅ Ticket opened: {thread.mention}", ephemeral=True)


def get_case_id_from_thread(channel) -> int:
    m = re.search(r"#(\d+)", channel.name or "")
    return int(m.group(1)) if m else None


class TicketCloseModal(discord.ui.Modal):
    def __init__(self, case_id):
        super().__init__(title="Close Ticket", custom_id="modsys:ticket_close_modal", timeout=None)
        self.case_id = case_id
        self.reason_input = discord.ui.TextInput(
            label="Reason", custom_id="modsys:ticket_close_reason",
            style=discord.TextStyle.paragraph, required=False, max_length=1000,
        )
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction):
        reason_text = self.reason_input.value.strip() if self.reason_input.value else None
        if self.case_id and reason_text:
            await db.set_ticket_close_reason(self.case_id, reason_text)
        await interaction.response.send_message("✅ Closing ticket...", ephemeral=True)
        try:
            await interaction.channel.delete()
        except (discord.NotFound, discord.Forbidden):
            pass


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="modsys:ticket_close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        case_id = get_case_id_from_thread(interaction.channel)
        await interaction.response.send_modal(TicketCloseModal(case_id=case_id))


# =========================================================================
# SECTION G: AUTO-MODERATION DETECTION
# =========================================================================

async def create_case_and_post(guild: discord.Guild, case_type: str, target_user, reason=None,
                                proof_url=None, message_link=None, message_deleted=False, automod=False,
                                channel_id=None):
    case_id = await db.create_case(
        case_type, guild.id, target_user.id, reason=reason, proof_url=proof_url,
        message_link=message_link, message_deleted=message_deleted, automod=automod,
        channel_id=channel_id,
    )
    case = await db.get_case(case_id)
    channel = guild.get_channel(MODERATION_CHANNEL_ID)
    if not channel:
        return case_id
    embed = build_case_embed(case, guild)
    content = f"<@&{SENIOR_MOD_ROLE_ID}> <@&{JUNIOR_MOD_ROLE_ID}>"
    view = ClaimView() if not automod else discord.ui.View()
    await channel.send(content=content, embed=embed, view=view)
    return case_id


async def ai_moderate_text(text: str):
    """Free text-toxicity backup check via Hugging Face's Inference API
    (unitary/toxic-bert). Returns (flagged, set of flagged label names)."""
    if not HF_API_TOKEN or not text or not text.strip():
        return False, set()
    try:
        headers = {"Authorization": f"Bearer {HF_API_TOKEN}"}
        async with bot.http_session.post(
            HF_INFERENCE_URL, headers=headers, json={"inputs": text},
            timeout=aiohttp.ClientTimeout(total=8)
        ) as resp:
            if resp.status == 503:
                # model is cold-starting on HF's side - not an error, just skip this check
                return False, set()
            if resp.status != 200:
                print(f"[automod] Hugging Face API error: HTTP {resp.status}")
                return False, set()
            data = await resp.json()

        # Response shape: [[{"label": "toxic", "score": 0.98}, ...]]
        # The generic "toxic" label alone is the main source of false
        # positives on ordinary conversation ("hard to kill", "facial
        # expression", joking threats) - it's excluded entirely. "threat"
        # needs a much higher bar since violent-sounding jokes/hyperbole
        # trip it easily; the other specific categories use the normal bar.
        scores = data[0] if data and isinstance(data, list) else []
        flagged_labels = set()
        for item in scores:
            label = item.get("label")
            score = item.get("score", 0)
            if label == "threat" and score >= HF_THREAT_THRESHOLD:
                flagged_labels.add(label)
            elif label in HF_SPECIFIC_LABELS and score >= HF_THRESHOLD:
                flagged_labels.add(label)
        return bool(flagged_labels), flagged_labels
    except Exception as e:
        print(f"[automod] Hugging Face API error: {e}")
        return False, set()


async def ai_check_avatar(avatar_url: str) -> bool:
    """Free-tier avatar NSFW check via Sightengine."""
    if not SIGHTENGINE_API_USER or not SIGHTENGINE_API_SECRET or not avatar_url:
        return False
    try:
        params = {
            "url": avatar_url,
            "models": "nudity-2.1",
            "api_user": SIGHTENGINE_API_USER,
            "api_secret": SIGHTENGINE_API_SECRET,
        }
        async with bot.http_session.get(
            SIGHTENGINE_URL, params=params, timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:
            if resp.status != 200:
                print(f"[automod] Sightengine error: HTTP {resp.status}")
                return False
            data = await resp.json()

        nudity = data.get("nudity", {})
        scores = [
            nudity.get("sexual_activity", 0),
            nudity.get("sexual_display", 0),
            nudity.get("suggestive", 0) if isinstance(nudity.get("suggestive"), (int, float)) else 0,
        ]
        return max(scores) >= SIGHTENGINE_THRESHOLD
    except Exception as e:
        print(f"[automod] Sightengine error: {e}")
        return False


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    await bot.process_commands(message)
    content = message.content or ""

    # --- Spam detection ---
    content_hash = hashlib.sha256(content.strip().lower().encode()).hexdigest()
    if content.strip():
        since = (datetime.now(timezone.utc) - timedelta(seconds=SPAM_TIME_WINDOW_SECONDS)).isoformat()
        matches = await db.count_recent_matches(message.guild.id, message.author.id, content_hash, since)
        await db.log_message(message.guild.id, message.author.id, message.channel.id, content_hash)
        if matches + 1 >= SPAM_MESSAGE_THRESHOLD:
            proof_text = content
            try:
                await message.delete()
            except discord.NotFound:
                pass
            case_id = await create_case_and_post(
                message.guild, "spam", message.author,
                reason="Repeated identical messages", proof_url=proof_text,
                message_deleted=True, automod=True, channel_id=message.channel.id,
            )
            member = message.guild.get_member(message.author.id)
            if member:
                try:
                    await member.timeout(timedelta(minutes=5), reason=f"Automod: spam (Case #{case_id})")
                    await db.create_active_action(
                        case_id, message.guild.id, member.id, "timeout",
                        (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                    )
                    await db.finalize_case(case_id, "timeout",
                                            (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(), True)
                    case = await db.get_case(case_id)
                    await post_mod_log(message.guild, case, "Timeout", message.author,
                                        "Automod (Anime World)", build_case_description(case),
                                        datetime.now(timezone.utc) + timedelta(minutes=5), discord.Color.red())
                except discord.Forbidden:
                    pass
            return

    # --- Server invite links ---
    if INVITE_REGEX.search(content) and message.channel.id not in ALLOWED_INVITE_CHANNELS \
            and message.author.id != OWNER_ID:
        proof_text = content
        try:
            await message.delete()
        except discord.NotFound:
            pass
        case_id = await create_case_and_post(
            message.guild, "invite_link", message.author,
            reason="Posted a server invite outside allowed channels", proof_url=proof_text,
            message_deleted=True, automod=True, channel_id=message.channel.id,
        )
        member = message.guild.get_member(message.author.id)
        if member:
            try:
                await member.timeout(timedelta(minutes=10), reason=f"Automod: invite link (Case #{case_id})")
                expires = datetime.now(timezone.utc) + timedelta(minutes=10)
                await db.create_active_action(case_id, message.guild.id, member.id, "timeout", expires.isoformat())
                await db.finalize_case(case_id, "timeout", expires.isoformat(), True)
                case = await db.get_case(case_id)
                await post_mod_log(message.guild, case, "Timeout", message.author,
                                    "Automod (Anime World)", build_case_description(case), expires, discord.Color.red())
            except discord.Forbidden:
                pass
        return

    # --- NSFW links / NSFW Telegram links ---
    lowered = content.lower()
    is_nsfw_link = any(k in lowered for k in NSFW_LINK_KEYWORDS)
    is_nsfw_telegram = "t.me/" in lowered and any(k in lowered for k in NSFW_TELEGRAM_KEYWORDS)
    if is_nsfw_link or is_nsfw_telegram:
        proof_text = content
        try:
            await message.delete()
        except discord.NotFound:
            pass
        case_id = await create_case_and_post(
            message.guild, "nsfw_link", message.author,
            reason="Posted an 18+ link", proof_url=proof_text,
            message_deleted=True, automod=True, channel_id=message.channel.id,
        )
        member = message.guild.get_member(message.author.id)
        if member:
            try:
                await member.timeout(timedelta(minutes=10), reason=f"Automod: 18+ link (Case #{case_id})")
                expires = datetime.now(timezone.utc) + timedelta(minutes=10)
                await db.create_active_action(case_id, message.guild.id, member.id, "timeout", expires.isoformat())
                await db.finalize_case(case_id, "timeout", expires.isoformat(), True)
                case = await db.get_case(case_id)
                await post_mod_log(message.guild, case, "Timeout", message.author,
                                    "Automod (Anime World)", build_case_description(case), expires, discord.Color.red())
            except discord.Forbidden:
                pass
        return

    # --- Bad words: keyword filter first, AI moderation as backup ---
    flagged = profanity.contains_profanity(content)
    ai_categories = set()
    if not flagged:
        flagged, ai_categories = await ai_moderate_text(content)

    if flagged:
        severe = bool(ai_categories & HF_SEVERE_LABELS)

        if severe:
            proof_text = content
            try:
                await message.delete()
            except discord.NotFound:
                pass
            case_id = await create_case_and_post(
                message.guild, "bad_words", message.author,
                reason="Used inappropriate language", proof_url=proof_text,
                message_deleted=True, automod=True, channel_id=message.channel.id,
            )
            member = message.guild.get_member(message.author.id)
            if member:
                try:
                    await member.timeout(timedelta(minutes=10), reason=f"Automod: bad words (Case #{case_id})")
                    expires = datetime.now(timezone.utc) + timedelta(minutes=10)
                    await db.create_active_action(case_id, message.guild.id, member.id, "timeout", expires.isoformat())
                    await db.finalize_case(case_id, "timeout", expires.isoformat(), True)
                    case = await db.get_case(case_id)
                    await post_mod_log(message.guild, case, "Timeout", message.author,
                                        "Automod (Anime World)", build_case_description(case), expires,
                                        discord.Color.red())
                except discord.Forbidden:
                    pass
        else:
            await create_case_and_post(
                message.guild, "bad_words", message.author,
                reason="Used inappropriate language", message_link=message.jump_url,
                proof_url=content, message_deleted=False, automod=False, channel_id=message.channel.id,
            )
        return


async def check_suspicious_profile(member: discord.Member):
    if member.bot:
        return
    name_lower = (member.name + " " + (member.nick or "")).lower()
    username_flag = any(k in name_lower for k in SUSPICIOUS_USERNAME_KEYWORDS)
    avatar_flag = await ai_check_avatar(member.display_avatar.url if member.display_avatar else None)

    if username_flag or avatar_flag:
        reason = []
        if username_flag:
            reason.append("suspicious username")
        if avatar_flag:
            reason.append("flagged profile picture")
        await create_case_and_post(
            member.guild, "suspicious_profile", member,
            reason=", ".join(reason), proof_url=member.mention,
            automod=False,
        )


@bot.event
async def on_member_join(member: discord.Member):
    await check_suspicious_profile(member)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if before.nick != after.nick or before.display_avatar != after.display_avatar:
        await check_suspicious_profile(after)


@bot.event
async def on_user_update(before: discord.User, after: discord.User):
    if before.name != after.name or before.avatar != after.avatar:
        for guild in bot.guilds:
            member = guild.get_member(after.id)
            if member:
                await check_suspicious_profile(member)


# =========================================================================
# SECTION H: EXPIRY SCHEDULER (auto unmute / unban / untimeout)
# =========================================================================

@tasks.loop(minutes=1)
async def expiry_checker():
    expired = await db.get_expired_actions()
    for action in expired:
        guild = bot.get_guild(int(action["guild_id"]))
        if not guild:
            await db.resolve_active_action(action["id"])
            continue
        user_id = int(action["user_id"])
        action_type = action["action_type"]
        try:
            if action_type == "ban":
                await guild.unban(discord.Object(id=user_id), reason="Temporary ban expired")
                await post_expiry_log(guild, action["case_id"], "Unban", user_id)
            elif action_type == "mute":
                member = guild.get_member(user_id)
                role = get_muted_role(guild)
                if member and role:
                    await member.remove_roles(role, reason="Temporary mute expired")
                await post_expiry_log(guild, action["case_id"], "Unmute", user_id)
            elif action_type == "timeout":
                member = guild.get_member(user_id)
                if member:
                    await member.timeout(None, reason="Timeout expired")
                await post_expiry_log(guild, action["case_id"], "Untimeout", user_id)
        except discord.NotFound:
            pass
        except discord.Forbidden:
            pass
        finally:
            await db.resolve_active_action(action["id"])


@expiry_checker.before_loop
async def before_expiry_checker():
    await bot.wait_until_ready()


# =========================================================================
# SECTION I: EXECUTION
# =========================================================================

if TOKEN:
    threading.Thread(target=run_web, daemon=True).start()
    bot.run(TOKEN)
else:
    print("FATAL ERROR: DISCORD_TOKEN environment variable not found.")
