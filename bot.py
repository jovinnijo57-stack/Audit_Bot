
import discord
from discord.ext import commands
from dotenv import load_dotenv
from datetime import datetime, timezone
import os
import json
import asyncio

# ==========================================
# CONFIGURATION
# ==========================================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

AUDIT_CATEGORY_NAME = "AUDIT LOGS"
MAPPING_FILE = "audit_mapping.json"

# ==========================================
# INTENTS
# ==========================================

intents = discord.Intents.default()

intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

# Source channel ID -> Audit channel ID
audit_mapping = {}

# Prevent overlapping initial setup
setup_locks = {}


# ==========================================
# MAPPING MANAGEMENT
# ==========================================

def load_mapping():
    global audit_mapping

    try:
        if os.path.exists(MAPPING_FILE):
            with open(MAPPING_FILE, "r", encoding="utf-8") as file:
                audit_mapping = json.load(file)
        else:
            audit_mapping = {}

    except (json.JSONDecodeError, OSError) as error:
        print(f"Mapping load error: {error}")
        audit_mapping = {}


def save_mapping():
    temp_file = MAPPING_FILE + ".tmp"

    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(audit_mapping, file, indent=4)

    os.replace(temp_file, MAPPING_FILE)


# ==========================================
# HELPER FUNCTIONS
# ==========================================

def is_monitored_channel(channel):
    return isinstance(
        channel,
        (discord.TextChannel, discord.VoiceChannel)
    )


def make_audit_name(channel):
    name = channel.name.lower().replace(" ", "-")
    return f"{name[:85]}-audit"


async def get_audit_category(guild):
    category = discord.utils.get(
        guild.categories,
        name=AUDIT_CATEGORY_NAME
    )

    if category:
        return category

    bot_member = guild.me

    if bot_member is None:
        bot_member = await guild.fetch_member(bot.user.id)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=False
        ),
        bot_member: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            embed_links=True,
            read_message_history=True,
            attach_files=True
        )
    }

    category = await guild.create_category(
        name=AUDIT_CATEGORY_NAME,
        overwrites=overwrites,
        reason="Create private audit log category"
    )

    print("AUDIT LOGS category created.")

    return category


async def get_audit_channel(guild, source_channel_id):
    audit_id = audit_mapping.get(str(source_channel_id))

    if audit_id is None:
        return None

    channel = guild.get_channel(int(audit_id))

    if channel is None:
        try:
            channel = await guild.fetch_channel(int(audit_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    if not isinstance(channel, discord.TextChannel):
        return None

    return channel


# ==========================================
# CREATE AUDIT CHANNEL
# ==========================================

async def create_audit_channel(guild, source_channel):

    category = await get_audit_category(guild)

    # Reuse an existing audit channel
    existing = await get_audit_channel(
        guild,
        source_channel.id
    )

    if existing:
        if existing.category_id != category.id:
            await existing.edit(category=category)

        return existing

    audit_name = make_audit_name(source_channel)

    # Avoid accidental name collisions
    existing_name = discord.utils.get(
        category.text_channels,
        name=audit_name
    )

    if existing_name:
        topic = existing_name.topic or ""

        if f"source_id={source_channel.id}" in topic:
            audit_channel = existing_name
        else:
            audit_channel = await guild.create_text_channel(
                name=audit_name[:90],
                category=category,
                topic=f"source_id={source_channel.id}",
                reason="Create dedicated audit channel"
            )
    else:
        audit_channel = await guild.create_text_channel(
            name=audit_name[:90],
            category=category,
            topic=f"source_id={source_channel.id}",
            reason="Create dedicated audit channel"
        )

    audit_mapping[str(source_channel.id)] = audit_channel.id
    save_mapping()

    embed = discord.Embed(
        title="Audit Logging Enabled",
        description=(
            f"Monitoring channel: **{source_channel.name}**\n"
            f"Channel ID: `{source_channel.id}`"
        ),
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc)
    )

    await audit_channel.send(embed=embed)

    print(
        f"Created audit channel: {audit_channel.name}"
    )

    return audit_channel


# ==========================================
# SEND AUDIT LOG
# ==========================================

async def send_audit_log(
    guild,
    source_channel_id,
    embed,
    files=None
):

    audit_channel = await get_audit_channel(
        guild,
        source_channel_id
    )

    if audit_channel is None:
        return

    try:
        await audit_channel.send(
            embed=embed,
            files=files or []
        )

    except discord.Forbidden:
        print(
            f"Permission denied: {audit_channel.name}"
        )

    except discord.HTTPException as error:
        print(f"Audit send error: {error}")


# ==========================================
# INITIAL SERVER SETUP
# ==========================================

async def setup_all_channels(guild):

    lock = setup_locks.setdefault(guild.id, asyncio.Lock())

    async with lock:

        category = await get_audit_category(guild)

        channels = [
            channel
            for channel in guild.channels
            if is_monitored_channel(channel)
            and channel.category_id != category.id
        ]

        created = 0
        reused = 0

        for source_channel in channels:

            existing = await get_audit_channel(
                guild,
                source_channel.id
            )

            if existing:
                reused += 1
                continue

            await create_audit_channel(
                guild,
                source_channel
            )

            created += 1

        print(f"\nServer: {guild.name}")
        print(f"Channels detected: {len(channels)}")
        print(f"Audit channels created: {created}")
        print(f"Existing audit channels reused: {reused}")


# ==========================================
# BOT READY
# ==========================================

@bot.event
async def on_ready():

    load_mapping()

    print("=" * 40)
    print(f"Bot online: {bot.user}")
    print(f"Bot ID: {bot.user.id}")
    print("=" * 40)

    for guild in bot.guilds:
        try:
            await setup_all_channels(guild)

        except discord.Forbidden:
            print(
                f"Missing permissions in {guild.name}"
            )

        except discord.HTTPException as error:
            print(f"Setup error: {error}")


# ==========================================
# MESSAGE SENT
# ==========================================

@bot.event
async def on_message(message):

    if message.guild and not message.author.bot:

        embed = discord.Embed(
            title="Message Sent",
            description=message.content[:3500] or "[No text content]",
            color=discord.Color.green(),
            timestamp=message.created_at
        )

        embed.add_field(
            name="Author",
            value=f"{message.author} (`{message.author.id}`)",
            inline=False
        )

        embed.add_field(
            name="Channel",
            value=f"#{message.channel.name}",
            inline=True
        )

        embed.add_field(
            name="Message ID",
            value=str(message.id),
            inline=True
        )

        if message.attachments:

            attachment_text = "\n".join(
                attachment.filename
                for attachment in message.attachments
            )

            embed.add_field(
                name="Attachments",
                value=attachment_text[:1000],
                inline=False
            )

        await send_audit_log(
            message.guild,
            message.channel.id,
            embed
        )

    await bot.process_commands(message)


# ==========================================
# MESSAGE EDITED
# ==========================================

@bot.event
async def on_message_edit(before, after):

    if not after.guild or after.author.bot:
        return

    if before.content == after.content:
        return

    embed = discord.Embed(
        title="Message Edited",
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc)
    )

    embed.add_field(
        name="Author",
        value=str(after.author),
        inline=False
    )

    embed.add_field(
        name="Before",
        value=before.content[:1000] or "[Empty]",
        inline=False
    )

    embed.add_field(
        name="After",
        value=after.content[:1000] or "[Empty]",
        inline=False
    )

    embed.add_field(
        name="Message ID",
        value=str(after.id)
    )

    await send_audit_log(
        after.guild,
        after.channel.id,
        embed
    )


# ==========================================
# MESSAGE DELETED
# ==========================================

@bot.event
async def on_raw_message_delete(payload):

    if payload.guild_id is None:
        return

    guild = bot.get_guild(payload.guild_id)

    if guild is None:
        return

    channel = guild.get_channel(payload.channel_id)

    if not isinstance(channel, discord.TextChannel):
        return

    message = payload.cached_message

    if message and message.author.bot:
        return

    embed = discord.Embed(
        title="Message Deleted",
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc)
    )

    embed.add_field(
        name="Channel",
        value=f"#{channel.name}",
        inline=True
    )

    embed.add_field(
        name="Message ID",
        value=str(payload.message_id),
        inline=True
    )

    if message:

        embed.add_field(
            name="Author",
            value=f"{message.author} (`{message.author.id}`)",
            inline=False
        )

        embed.add_field(
            name="Deleted Content",
            value=message.content[:3000] or "[No text content]",
            inline=False
        )

        if message.attachments:
            embed.add_field(
                name="Attachments",
                value="\n".join(
                    a.filename for a in message.attachments
                )[:1000],
                inline=False
            )

    else:

        embed.add_field(
            name="Message Information",
            value="Message was not cached. Author and content unavailable."
        )

    await send_audit_log(
        guild,
        payload.channel_id,
        embed
    )


# ==========================================
# VOICE ACTIVITY
# ==========================================

@bot.event
async def on_voice_state_update(member, before, after):

    if member.bot:
        return

    # Joined a voice channel
    if before.channel is None and after.channel is not None:

        channel = after.channel
        action = "Joined Voice Channel"
        color = discord.Color.green()

    # Left a voice channel
    elif before.channel is not None and after.channel is None:

        channel = before.channel
        action = "Left Voice Channel"
        color = discord.Color.red()

    # Moved to another voice channel
    elif before.channel != after.channel:

        channel = after.channel
        action = "Moved Voice Channel"
        color = discord.Color.blue()

    else:

        channel = after.channel

        changes = []

        if before.mute != after.mute:
            changes.append("Server mute changed")

        if before.deaf != after.deaf:
            changes.append("Server deafen changed")

        if before.self_mute != after.self_mute:
            changes.append("Self mute changed")

        if before.self_deaf != after.self_deaf:
            changes.append("Self deafen changed")

        if before.self_video != after.self_video:
            changes.append("Camera state changed")

        if before.self_stream != after.self_stream:
            changes.append("Screen sharing state changed")

        if not changes:
            return

        action = "Voice Status Updated"
        color = discord.Color.orange()

    embed = discord.Embed(
        title=action,
        color=color,
        timestamp=datetime.now(timezone.utc)
    )

    embed.add_field(
        name="Member",
        value=f"{member} (`{member.id}`)",
        inline=False
    )

    if before.channel:
        embed.add_field(
            name="Previous Channel",
            value=before.channel.name,
            inline=True
        )

    if after.channel:
        embed.add_field(
            name="Current Channel",
            value=after.channel.name,
            inline=True
        )

    if before.channel == after.channel:
        embed.add_field(
            name="Changes",
            value=", ".join(changes),
            inline=False
        )

    # Log in the corresponding voice channel's audit channel
    await send_audit_log(
        member.guild,
        channel.id,
        embed
    )


# ==========================================
# NEW CHANNEL CREATED
# ==========================================

@bot.event
async def on_guild_channel_create(channel):

    if not is_monitored_channel(channel):
        return

    category = discord.utils.get(
        channel.guild.categories,
        name=AUDIT_CATEGORY_NAME
    )

    if category and channel.category_id == category.id:
        return

    try:
        await create_audit_channel(
            channel.guild,
            channel
        )

    except discord.HTTPException as error:
        print(f"New channel setup failed: {error}")


# ==========================================
# CHANNEL DELETED
# ==========================================

@bot.event
async def on_guild_channel_delete(channel):

    # Was this a mapped audit channel?
    for source_id, audit_id in list(audit_mapping.items()):

        if int(audit_id) == channel.id:

            del audit_mapping[source_id]
            save_mapping()

            print("An audit channel was deleted.")
            return

    # Was this an original monitored channel?
    audit_id = audit_mapping.get(str(channel.id))

    if audit_id:

        audit_channel = channel.guild.get_channel(
            int(audit_id)
        )

        if audit_channel:

            embed = discord.Embed(
                title="Original Channel Deleted",
                description=(
                    f"Channel: **{channel.name}**\n"
                    f"Channel ID: `{channel.id}`"
                ),
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )

            try:
                await audit_channel.send(embed=embed)

            except discord.HTTPException as error:
                print(f"Deletion log error: {error}")

        del audit_mapping[str(channel.id)]
        save_mapping()


# ==========================================
# MEMBER JOINED
# ==========================================

@bot.event
async def on_member_join(member):

    category = discord.utils.get(
        member.guild.categories,
        name=AUDIT_CATEGORY_NAME
    )

    if category is None:
        return

    # Send member events to a dedicated server log
    audit_channel = discord.utils.get(
        category.text_channels,
        name="server-members-audit"
    )

    if audit_channel is None:
        return

    embed = discord.Embed(
        title="Member Joined",
        description=f"{member.mention} joined the server.",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc)
    )

    embed.add_field(name="Username", value=str(member))
    embed.add_field(name="User ID", value=str(member.id))

    await audit_channel.send(embed=embed)


# ==========================================
# MEMBER LEFT
# ==========================================

@bot.event
async def on_member_remove(member):

    category = discord.utils.get(
        member.guild.categories,
        name=AUDIT_CATEGORY_NAME
    )

    if category is None:
        return

    audit_channel = discord.utils.get(
        category.text_channels,
        name="server-members-audit"
    )

    if audit_channel is None:
        return

    embed = discord.Embed(
        title="Member Left",
        description=f"{member} left the server.",
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc)
    )

    embed.add_field(name="Username", value=str(member))
    embed.add_field(name="User ID", value=str(member.id))

    await audit_channel.send(embed=embed)


# ==========================================
# RUN
# ==========================================

if not TOKEN:
    raise ValueError(
        "DISCORD_TOKEN missing. Check your .env file."
    )

bot.run(TOKEN)
