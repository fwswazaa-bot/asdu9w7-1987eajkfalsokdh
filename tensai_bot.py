"""
Tensai Services Discord Bot
────────────────────────────────────────────────────────────────
Single-file bot with:
  - !lock       — Lock the current channel (admins-only bypass)
  - /vouch      — Submit a vouch (restricted to vouch role + admins)
  - /ticket     — Create a ticket panel with category dropdown
  - /close      — Close the current ticket channel
  - /add        — Add a user to the current ticket
  - /remove     — Remove a user from the current ticket
  - /claim      — Claim a ticket (staff only)
  - /stats      — Show ticket & vouch statistics
  - /setautorole — Set auto-role for new members

Requires: discord.py >= 2.4
           pip install discord.py

To run:
  1. Set your bot token below (or via DISCORD_BOT_TOKEN env var)
  2. python tensai_bot.py
────────────────────────────────────────────────────────────────
"""

import discord
from discord import app_commands, ui
from discord.ext import commands
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
import json
import os
import asyncio
import threading
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler


# ═══════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("tensai")


# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION  –  Edit these before running
# ═══════════════════════════════════════════════════════════════

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# Role that is allowed to use /vouch (besides admins)
VOUCH_ROLE_ID = int(os.getenv("VOUCH_ROLE_ID", "1522771389105443006"))

# Staff role name used when pinging in new ticket channels
STAFF_ROLE_NAME = os.getenv("STAFF_ROLE_NAME", "grail")

# Hardcoded role to ping when tickets are created
TICKET_PING_ROLE_ID = 1522771343022624919

# File used to persist ticket counters & open-ticket tracking
TICKET_DATA_FILE = "ticket_data.json"

# Gist URL for ticket data backup (leave empty to disable)
GIST_URL = os.getenv("GIST_URL", "")

# Branding
BRAND = os.getenv("BRAND_NAME", "Tensai")
BRAND_FOOTER = f"©2026 {BRAND}. All rights reserved."
BRAND_URL = os.getenv("BRAND_URL", "https://tensai.services")

# Brand color for embeds (deep blue-purple)
BRAND_COLOR = discord.Color.from_rgb(88, 101, 242)

# Auto-close inactive tickets after this many hours (0 = disabled)
AUTO_CLOSE_HOURS = int(os.getenv("AUTO_CLOSE_HOURS", "0"))

# ═══════════════════════════════════════════════════════════════
#  INTENTS & BOT
# ═══════════════════════════════════════════════════════════════

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.guild_messages = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ═══════════════════════════════════════════════════════════════
#  IN-MEMORY DATA  (also persisted to JSON)
# ═══════════════════════════════════════════════════════════════

# open_tickets[guild_id] = {user_id: {"type": str, "channel_id": int}}
open_tickets: Dict[int, Dict[int, Dict[str, Any]]] = {}
# ticket_channel_map[guild_id] = {channel_id: {"user_id": int, "type": str, "number": int, "created_at": str, "claimed_by": int|None}}
ticket_channel_map: Dict[int, Dict[int, Dict[str, Any]]] = {}
# ticket_counters[guild_id] = {type_str: int}
ticket_counters: Dict[int, Dict[str, int]] = {}
# vouch_counters[guild_id] = int
vouch_counters: Dict[int, int] = {}
# autorole_map[guild_id] = role_id (role to auto-assign to new members)
autorole_map: Dict[int, int] = {}
# closed_ticket_log[guild_id] = [{"user_id", "type", "number", "closed_by", "closed_at", "messages": int}]
closed_ticket_log: Dict[int, List[Dict[str, Any]]] = {}


TICKET_TYPES: Dict[str, str] = {
    "purchase": "Purchase Ticket",
    "support": "Support Ticket",
    "hwid": "HWID Reset Ticket",
}

CATEGORY_NAMES: Dict[str, str] = {
    "purchase": "Purchase Tickets",
    "support": "Support Tickets",
    "hwid": "HWID Reset Tickets",
}

TICKET_EMOJIS: Dict[str, str] = {
    "purchase": "\U0001f6d2",
    "support": "\U0001f527",
    "hwid": "\U0001f504",
}


# ── Persistence helpers ────────────────────────────────────────


def load_data() -> None:
    """Load ticket/vouch data from JSON file into global dicts. Falls back to gist."""
    global open_tickets, ticket_channel_map, ticket_counters, vouch_counters, autorole_map, closed_ticket_log
    if not os.path.exists(TICKET_DATA_FILE):
        if GIST_URL:
            try:
                import urllib.request
                with urllib.request.urlopen(GIST_URL, timeout=10) as response:
                    content = response.read().decode()
                    raw = json.loads(content) if content.strip() else {}
                if isinstance(raw, list):
                    raw = {}
                log.info("Loaded ticket data from gist")
            except Exception as e:
                log.warning("Could not load from gist: %s", e)
                raw = {}
        else:
            raw = {}
    else:
        try:
            with open(TICKET_DATA_FILE, "r") as f:
                raw = json.load(f)
        except Exception as e:
            log.warning("Could not load ticket data: %s", e)
            raw = {}

    if not isinstance(raw, dict):
        raw = {}

    open_tickets = {
        int(g): {int(u): v for u, v in users.items()}
        for g, users in raw.get("open_tickets", {}).items()
    }
    ticket_channel_map = {
        int(g): {int(c): v for c, v in chans.items()}
        for g, chans in raw.get("ticket_channel_map", {}).items()
    }
    ticket_counters = {int(g): v for g, v in raw.get("ticket_counters", {}).items()}
    vouch_counters = {int(g): v for g, v in raw.get("vouch_counters", {}).items()}
    autorole_map = {int(g): v for g, v in raw.get("autorole_map", {}).items()}
    closed_ticket_log = {
        int(g): v for g, v in raw.get("closed_ticket_log", {}).items()
    }


def save_data() -> None:
    """Persist ticket/vouch data from global dicts to JSON file."""
    raw = {
        "open_tickets": {
            str(g): {str(u): v for u, v in users.items()}
            for g, users in open_tickets.items()
        },
        "ticket_channel_map": {
            str(g): {str(c): v for c, v in chans.items()}
            for g, chans in ticket_channel_map.items()
        },
        "ticket_counters": {str(g): v for g, v in ticket_counters.items()},
        "vouch_counters": {str(g): v for g, v in vouch_counters.items()},
        "autorole_map": {str(g): v for g, v in autorole_map.items()},
        "closed_ticket_log": {str(g): v for g, v in closed_ticket_log.items()},
    }
    try:
        with open(TICKET_DATA_FILE, "w") as f:
            json.dump(raw, f, indent=2)
    except Exception as e:
        log.warning("Could not save ticket data: %s", e)


# ── Helper functions ──────────────────────────────────────────


def get_staff_role(guild: discord.Guild) -> Optional[discord.Role]:
    """Return the staff role by name, or None."""
    return discord.utils.get(guild.roles, name=STAFF_ROLE_NAME)


def get_ticket_ping_role(guild: discord.Guild) -> Optional[discord.Role]:
    """Return the hardcoded ticket ping role, or None."""
    return guild.get_role(TICKET_PING_ROLE_ID)


def get_ticket_number(guild_id: int, ttype: str) -> int:
    """Increment and return the next ticket number for *ttype* in *guild*."""
    ticket_counters.setdefault(guild_id, {})
    ticket_counters[guild_id].setdefault(ttype, 0)
    ticket_counters[guild_id][ttype] += 1
    save_data()
    return ticket_counters[guild_id][ttype]


def get_vouch_number(guild_id: int) -> int:
    """Increment and return the next vouch number."""
    vouch_counters.setdefault(guild_id, 0)
    vouch_counters[guild_id] += 1
    save_data()
    return vouch_counters[guild_id]


def is_staff_or_admin(member: discord.Member) -> bool:
    """Check if a member is staff or admin."""
    if member.guild_permissions.administrator:
        return True
    staff_role = get_staff_role(member.guild)
    return staff_role is not None and staff_role in member.roles


async def find_or_create_category(
    guild: discord.Guild, ttype: str
) -> discord.CategoryChannel:
    """Return an existing category for *ttype* or create one (staff-only view)."""
    cat_name = CATEGORY_NAMES[ttype]
    existing = discord.utils.get(guild.categories, name=cat_name)
    if existing:
        return existing

    staff_role = get_staff_role(guild)
    ping_role = get_ticket_ping_role(guild)
    overwrites: Dict[discord.Role, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me: discord.PermissionOverwrite(
            read_messages=True, send_messages=True,
            manage_channels=True, manage_permissions=True,
        ),
    }
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(
            read_messages=True, send_messages=True, manage_messages=True
        )
    if ping_role and ping_role != staff_role:
        overwrites[ping_role] = discord.PermissionOverwrite(
            read_messages=True, send_messages=True, manage_messages=True
        )

    category = await guild.create_category_channel(
        name=cat_name,
        overwrites=overwrites,
        reason=f"Auto-created category for {ttype} tickets",
    )
    return category


async def cleanup_empty_category(
    guild: discord.Guild, category: Optional[discord.CategoryChannel]
) -> None:
    """Delete *category* if it has no text channels left."""
    if category and len(category.text_channels) == 0:
        try:
            await category.delete(reason="Category empty — all tickets closed")
        except discord.HTTPException:
            pass


def make_ticket_channel_name(member: discord.Member, ttype: str, number: int) -> str:
    """Generate a channel name like ``johndoe-support-3``."""
    safe_name = member.name.lower().replace(" ", "-").replace("_", "-")
    safe_name = "".join(c for c in safe_name if c.isalnum() or c == "-")
    type_part = ttype.replace("_", "-").replace(" ", "-")
    return f"{safe_name}-{type_part}-{number}"


async def collect_transcript(channel: discord.TextChannel) -> str:
    """Collect all messages in a channel and return a formatted transcript string."""
    messages = []
    try:
        async for msg in channel.history(limit=500, oldest_first=True):
            ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            attachments = ""
            if msg.attachments:
                attachments = " " + ", ".join(a.url for a in msg.attachments)
            content = msg.content or "[embed/attachment]"
            messages.append(f"[{ts}] {msg.author}: {content}{attachments}")
    except discord.HTTPException:
        pass
    return "\n".join(messages) if messages else "No messages found."


def build_ping_content(guild: discord.Guild) -> str:
    """Build the ping content for new tickets: @everyone + hardcoded role."""
    parts = ["@everyone"]
    ping_role = get_ticket_ping_role(guild)
    if ping_role:
        parts.append(ping_role.mention)
    return " ".join(parts)


# ═══════════════════════════════════════════════════════════════
#  MODALS
# ═══════════════════════════════════════════════════════════════


class VouchModal(ui.Modal, title="Submit a Vouch"):
    """Modal shown to users submitting a vouch via /vouch."""

    vouch_text = ui.TextInput(
        label="Vouch Text",
        style=discord.TextStyle.long,
        placeholder="Write your vouch / review here...",
        required=True,
        max_length=2000,
    )
    product = ui.TextInput(
        label="Product",
        placeholder="What product did you purchase?",
        required=True,
        max_length=200,
    )
    rating = ui.TextInput(
        label="Rating",
        placeholder="e.g. 5/5, 10/10, ★★★★★",
        required=True,
        max_length=20,
    )
    media = ui.TextInput(
        label="Media URL (optional)",
        placeholder="Paste a screenshot or image URL (optional)",
        required=False,
        max_length=500,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        vouch_num = get_vouch_number(guild_id)

        embed = discord.Embed(
            title="✨ New Vouch!",
            description=f"**{interaction.user.mention}** has submitted a new vouch!",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Vouch", value=self.vouch_text.value, inline=False)
        embed.add_field(name="Product", value=self.product.value, inline=True)
        embed.add_field(name="Rating", value=self.rating.value, inline=True)
        if self.media.value.strip():
            embed.add_field(name="Media", value=self.media.value, inline=False)
            embed.set_image(url=self.media.value.strip())
        embed.add_field(
            name="Vouched At",
            value=f"<t:{int(datetime.now(timezone.utc).timestamp())}:F>",
            inline=False,
        )
        embed.set_footer(text=f"{BRAND_FOOTER}  •  Vouch #{vouch_num}")
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

        await interaction.response.send_message(embed=embed)


# ── Ticket Modals (one per type) ──────────────────────────────


class BaseTicketModal(ui.Modal):
    """Shared logic for all ticket-type modals."""

    def __init__(
        self,
        guild: discord.Guild,
        member: discord.Member,
        ttype: str,
        number: int,
        title: str,
    ) -> None:
        super().__init__(title=title)
        self._guild = guild
        self._member = member
        self._ttype = ttype
        self._number = number

    async def _finish_ticket(self, interaction: discord.Interaction, embed: discord.Embed) -> None:
        """Create the channel, send the embed + close button, then notify the user."""
        # ── Create category if needed ─────────────────────────
        try:
            category = await find_or_create_category(self._guild, self._ttype)
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Failed to create category: {e}", ephemeral=True
            )
            return

        # ── Channel permission overwrites ─────────────────────
        staff_role = get_staff_role(self._guild)
        ping_role = get_ticket_ping_role(self._guild)
        channel_name = make_ticket_channel_name(self._member, self._ttype, self._number)

        overwrites: Dict[discord.Role, discord.PermissionOverwrite] = {
            self._guild.default_role: discord.PermissionOverwrite(read_messages=False),
            self._member: discord.PermissionOverwrite(
                read_messages=True, send_messages=True,
                attach_files=True, embed_links=True,
                read_message_history=True,
            ),
            self._guild.me: discord.PermissionOverwrite(
                read_messages=True, send_messages=True,
                manage_channels=True, manage_permissions=True,
                manage_messages=True,
            ),
        }
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(
                read_messages=True, send_messages=True, manage_messages=True,
                read_message_history=True,
            )
        if ping_role and ping_role != staff_role:
            overwrites[ping_role] = discord.PermissionOverwrite(
                read_messages=True, send_messages=True, manage_messages=True,
                read_message_history=True,
            )

        # ── Create the channel ────────────────────────────────
        try:
            channel = await self._guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                reason=f"Ticket #{self._number} ({TICKET_TYPES[self._ttype]}) opened by {self._member}",
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Failed to create ticket channel: {e}", ephemeral=True
            )
            return

        # ── Track it ──────────────────────────────────────────
        guild_id = self._guild.id
        now_iso = datetime.now(timezone.utc).isoformat()
        open_tickets.setdefault(guild_id, {})[self._member.id] = {
            "type": self._ttype, "channel_id": channel.id,
        }
        ticket_channel_map.setdefault(guild_id, {})[channel.id] = {
            "user_id": self._member.id,
            "type": self._ttype,
            "number": self._number,
            "created_at": now_iso,
            "claimed_by": None,
        }
        save_data()

        # ── Send the embed and close button ───────────────────
        ping_content = build_ping_content(self._guild)
        await channel.send(ping_content, embed=embed, allowed_mentions=discord.AllowedMentions(everyone=True, roles=True))

        # Register the channel before sending the close view
        TicketCloseView._channel_registry[channel.id] = {
            "guild_id": guild_id,
            "user_id": self._member.id,
        }
        close_view = TicketCloseView()
        await channel.send("\u200b", view=close_view)

        await interaction.response.send_message(
            f"✅ Your ticket has been created in {channel.mention}",
            ephemeral=True,
        )
        log.info(
            "Ticket #%d (%s) created by %s in %s",
            self._number, self._ttype, self._member, channel.name,
        )


class PurchaseModal(BaseTicketModal):
    """Modal for purchase ticket details."""

    product = ui.TextInput(label="Product", placeholder="What product are you purchasing?", required=True, max_length=200)
    duration = ui.TextInput(label="Duration / Plan", placeholder="e.g. Monthly, Lifetime, 1 Year", required=True, max_length=100)
    payment_method = ui.TextInput(label="Payment Method", placeholder="e.g. PayPal, Crypto, Card", required=True, max_length=100)
    order_id = ui.TextInput(label="Order ID (optional)", placeholder="Transaction / invoice ID if you have one", required=False, max_length=200)

    def __init__(self, guild: discord.Guild, member: discord.Member, ttype: str, number: int) -> None:
        super().__init__(guild, member, ttype, number, title="Purchase Ticket")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title=f"\U0001f6d2 {BRAND} — Purchase Ticket #{self._number}",
            description=(
                "Thank you for your interest! A staff member will assist you shortly.\n\n"
                "• Do **not** ping staff directly.\n"
                "• Provide any additional details below."
            ),
            color=BRAND_COLOR,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Product", value=f"`{self.product.value}`", inline=True)
        embed.add_field(name="Duration", value=f"`{self.duration.value}`", inline=True)
        embed.add_field(name="Payment Method", value=f"`{self.payment_method.value}`", inline=True)
        embed.add_field(name="Order ID", value=f"`{self.order_id.value or 'N/A'}`", inline=True)
        embed.add_field(
            name="Opened",
            value=f"<t:{int(datetime.now(timezone.utc).timestamp())}:R> by {self._member.mention}",
            inline=False,
        )
        embed.set_footer(text=BRAND_FOOTER)
        embed.set_author(name=self._member.display_name, icon_url=self._member.display_avatar.url)
        await self._finish_ticket(interaction, embed)


class SupportModal(BaseTicketModal):
    """Modal for support ticket details."""

    issue_category = ui.TextInput(label="Issue Category", placeholder="e.g. Billing, Technical, Account", required=True, max_length=100)
    description = ui.TextInput(label="Describe your issue", style=discord.TextStyle.long, placeholder="Provide as much detail as possible...", required=True, max_length=2000)
    steps_taken = ui.TextInput(label="Steps Already Taken", style=discord.TextStyle.long, placeholder="What have you tried so far?", required=False, max_length=2000)
    solution = ui.TextInput(label="Preferred Solution (optional)", placeholder="What solution are you hoping for?", required=False, max_length=500)

    def __init__(self, guild: discord.Guild, member: discord.Member, ttype: str, number: int) -> None:
        super().__init__(guild, member, ttype, number, title="Support Ticket")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title=f"\U0001f527 {BRAND} — Support Ticket #{self._number}",
            description=(
                "Thank you for reaching out! A staff member will assist you shortly.\n\n"
                "• Do **not** ping staff directly.\n"
                "• Provide any additional details below."
            ),
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Issue Category", value=f"`{self.issue_category.value}`", inline=False)
        embed.add_field(name="Description", value=self.description.value[:1024], inline=False)
        steps = self.steps_taken.value or "None provided"
        embed.add_field(name="Steps Taken", value=steps[:1024], inline=False)
        embed.add_field(name="Preferred Solution", value=f"`{self.solution.value or 'N/A'}`", inline=False)
        embed.add_field(
            name="Opened",
            value=f"<t:{int(datetime.now(timezone.utc).timestamp())}:R> by {self._member.mention}",
            inline=False,
        )
        embed.set_footer(text=BRAND_FOOTER)
        embed.set_author(name=self._member.display_name, icon_url=self._member.display_avatar.url)
        await self._finish_ticket(interaction, embed)


class HWIDModal(BaseTicketModal):
    """Modal for HWID reset ticket details."""

    reason = ui.TextInput(label="Reason for Reset", placeholder="Why do you need an HWID reset?", required=True, max_length=500)
    old_hwid = ui.TextInput(label="Old HWID (if known)", placeholder="Leave blank if you don't know", required=False, max_length=200)
    new_hwid = ui.TextInput(label="New HWID", placeholder="Your new HWID / machine ID", required=True, max_length=200)
    proof = ui.TextInput(label="Proof of Purchase URL", placeholder="Link to purchase receipt or DM", required=False, max_length=500)

    def __init__(self, guild: discord.Guild, member: discord.Member, ttype: str, number: int) -> None:
        super().__init__(guild, member, ttype, number, title="HWID Reset Ticket")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title=f"\U0001f504 {BRAND} — HWID Reset Ticket #{self._number}",
            description=(
                "Your HWID reset request has been received. A staff member will assist you shortly.\n\n"
                "• Do **not** ping staff directly.\n"
                "• Provide any additional details below."
            ),
            color=discord.Color.purple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Reason", value=f"`{self.reason.value}`", inline=False)
        embed.add_field(name="Old HWID", value=f"`{self.old_hwid.value or 'Unknown'}`", inline=True)
        embed.add_field(name="New HWID", value=f"`{self.new_hwid.value}`", inline=True)
        embed.add_field(name="Proof of Purchase", value=self.proof.value or "N/A", inline=False)
        embed.add_field(
            name="Opened",
            value=f"<t:{int(datetime.now(timezone.utc).timestamp())}:R> by {self._member.mention}",
            inline=False,
        )
        embed.set_footer(text=BRAND_FOOTER)
        embed.set_author(name=self._member.display_name, icon_url=self._member.display_avatar.url)
        await self._finish_ticket(interaction, embed)


# ═══════════════════════════════════════════════════════════════
#  VIEWS
# ═══════════════════════════════════════════════════════════════


# ── Ticket Panel (Dropdown) ───────────────────────────────────


class TicketSelect(ui.Select):
    """Dropdown component for selecting a ticket type."""

    def __init__(self) -> None:
        options = [
            discord.SelectOption(
                label="Purchase Ticket",
                value="purchase",
                emoji="\U0001f6d2",
                description="For purchasing inquiries",
            ),
            discord.SelectOption(
                label="Support Ticket",
                value="support",
                emoji="\U0001f527",
                description="For general support issues",
            ),
            discord.SelectOption(
                label="HWID Reset Ticket",
                value="hwid",
                emoji="\U0001f504",
                description="For HWID reset requests",
            ),
        ]
        super().__init__(
            placeholder="Select a ticket category...",
            options=options,
            min_values=1,
            max_values=1,
            custom_id="ticket_type_select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        ttype = self.values[0]
        guild = interaction.guild
        member = interaction.user
        guild_id = guild.id

        # ── Check if user already has an open ticket ──────────
        if guild_id in open_tickets and member.id in open_tickets[guild_id]:
            existing = open_tickets[guild_id][member.id]
            existing_channel = guild.get_channel(existing["channel_id"])
            if existing_channel:
                await interaction.response.send_message(
                    f"⚠️ You already have an open {TICKET_TYPES[existing['type']]} "
                    f"in {existing_channel.mention}. "
                    "Please close it before opening a new one.",
                    ephemeral=True,
                )
                return
            # Channel was deleted manually — clean up stale tracking
            del open_tickets[guild_id][member.id]
            save_data()

        # ── Ticket number ─────────────────────────────────────
        number = get_ticket_number(guild_id, ttype)

        # ── Show the appropriate modal (channel created on submit) ──
        modals: Dict[str, type] = {
            "purchase": PurchaseModal,
            "support": SupportModal,
            "hwid": HWIDModal,
        }
        modal = modals[ttype](guild, member, ttype, number)
        await interaction.response.send_modal(modal)


class TicketPanelView(ui.View):
    """View containing the ticket-type dropdown. Persistent (timeout=None)."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(TicketSelect())


# ── Confirm Close View ────────────────────────────────────────


class ConfirmCloseView(ui.View):
    """Confirmation dialog with Yes/No buttons for closing a ticket."""

    def __init__(self, original_interaction: discord.Interaction):
        super().__init__(timeout=30)
        self._original = original_interaction

    @ui.button(label="Yes, Close", style=discord.ButtonStyle.danger, emoji="🔒")
    async def confirm(self, interaction: discord.Interaction, button: ui.Button) -> None:
        await interaction.response.defer()
        self.stop()

        channel = self._original.channel
        if not isinstance(channel, discord.TextChannel):
            return

        guild = interaction.guild
        guild_id = guild.id
        channel_id = channel.id
        category = channel.category

        # Save transcript
        transcript = await collect_transcript(channel)
        chan_info = ticket_channel_map.get(guild_id, {}).get(channel_id, {})
        user_id = chan_info.get("user_id")

        # Log the closed ticket
        closed_ticket_log.setdefault(guild_id, []).append({
            "user_id": user_id,
            "type": chan_info.get("type", "unknown"),
            "number": chan_info.get("number", 0),
            "closed_by": str(interaction.user),
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "messages": transcript.count("\n") + 1,
        })

        # Cleanup tracking
        if guild_id in ticket_channel_map and channel_id in ticket_channel_map[guild_id]:
            uid = ticket_channel_map[guild_id][channel_id]["user_id"]
            del ticket_channel_map[guild_id][channel_id]
            if guild_id in open_tickets and uid in open_tickets[guild_id]:
                del open_tickets[guild_id][uid]
            save_data()

        TicketCloseView._channel_registry.pop(channel_id, None)

        # Send transcript to user if possible
        if user_id:
            try:
                user = guild.get_member(user_id)
                if user:
                    dm_embed = discord.Embed(
                        title=f"📋 Ticket Transcript — {channel.name}",
                        description=f"```\n{transcript[:3900]}\n```",
                        color=BRAND_COLOR,
                        timestamp=datetime.now(timezone.utc),
                    )
                    dm_embed.set_footer(text=BRAND_FOOTER)
                    await user.send(embed=dm_embed)
            except (discord.Forbidden, discord.HTTPException):
                pass

        await channel.delete(reason=f"Ticket closed by {interaction.user}")
        log.info("Ticket channel %s closed by %s", channel.name, interaction.user)

        if category:
            await cleanup_empty_category(guild, category)

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def cancel(self, interaction: discord.Interaction, button: ui.Button) -> None:
        await interaction.response.edit_message(
            content="❎ Ticket close cancelled.", embed=None, view=None
        )
        self.stop()


# ── Ticket Close Button ───────────────────────────────────────


class TicketCloseView(ui.View):
    """Self-contained close button for ticket channels."""

    _channel_registry: Dict[int, Dict[str, int]] = {}

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="close_ticket_btn",
    )
    async def close_ticket(
        self, interaction: discord.Interaction, button: ui.Button
    ) -> None:
        channel_id = interaction.channel_id
        guild = interaction.guild

        # Look up ticket info
        info = self._channel_registry.get(channel_id)
        if not info:
            guild_id = guild.id
            chan_map = ticket_channel_map.get(guild_id, {})
            chan_info = chan_map.get(channel_id)
            if chan_info:
                info = {"guild_id": guild_id, "user_id": chan_info["user_id"]}

        if not info:
            await interaction.response.send_message(
                "❌ Could not identify this ticket channel.", ephemeral=True
            )
            return

        # ── Permission check ──────────────────────────────────
        user = interaction.user
        is_creator = isinstance(user, discord.Member) and user.id == info["user_id"]
        is_staff = False
        staff_role = get_staff_role(guild)
        if staff_role and isinstance(user, discord.Member) and staff_role in user.roles:
            is_staff = True
        is_admin = isinstance(user, discord.Member) and user.guild_permissions.administrator

        if not (is_creator or is_staff or is_admin):
            await interaction.response.send_message(
                "❌ Only the ticket creator or staff can close this ticket.",
                ephemeral=True,
            )
            return

        # ── Show confirmation ─────────────────────────────────
        confirm_embed = discord.Embed(
            title="🔒 Close Ticket?",
            description="Are you sure you want to close this ticket? A transcript will be sent to the ticket creator.",
            color=discord.Color.red(),
        )
        confirm_view = ConfirmCloseView(interaction)
        await interaction.response.send_message(embed=confirm_embed, view=confirm_view)


# ── Ticket Claim View (shown in ticket for staff) ─────────────


class TicketClaimView(ui.View):
    """Button for staff to claim a ticket."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @ui.button(
        label="Claim Ticket",
        style=discord.ButtonStyle.primary,
        emoji="🙋",
        custom_id="claim_ticket_btn",
    )
    async def claim_ticket(
        self, interaction: discord.Interaction, button: ui.Button
    ) -> None:
        user = interaction.user
        if not isinstance(user, discord.Member) or not is_staff_or_admin(user):
            await interaction.response.send_message(
                "❌ Only staff can claim tickets.", ephemeral=True
            )
            return

        channel_id = interaction.channel_id
        guild_id = interaction.guild_id
        chan_info = ticket_channel_map.get(guild_id, {}).get(channel_id)
        if not chan_info:
            await interaction.response.send_message(
                "❌ This is not a tracked ticket channel.", ephemeral=True
            )
            return

        chan_info["claimed_by"] = user.id
        save_data()

        # Disable the button
        button.disabled = True
        button.label = f"Claimed by {user.display_name}"
        await interaction.response.edit_message(view=self)

        claim_embed = discord.Embed(
            description=f"🙋 This ticket has been claimed by **{user.mention}**.",
            color=BRAND_COLOR,
        )
        await interaction.channel.send(embed=claim_embed)


# ═══════════════════════════════════════════════════════════════
#  PREFIX COMMANDS
# ═══════════════════════════════════════════════════════════════


@bot.command(name="lock")
@commands.has_permissions(administrator=True)
async def lock(ctx: commands.Context) -> None:
    """Lock the current channel so only admins can speak."""
    channel = ctx.channel
    if not isinstance(channel, discord.TextChannel):
        await ctx.send("❌ This command only works in text channels.")
        return

    overwrite = channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = False
    overwrite.add_reactions = False
    try:
        await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
        embed = discord.Embed(
            description=f"🔒 {channel.mention} has been **locked**. Only administrators may speak.",
            color=discord.Color.red(),
        )
        await ctx.send(embed=embed)
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to manage this channel.")


@lock.error
async def lock_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need the **Administrator** permission to use this command.")


@bot.command(name="unlock")
@commands.has_permissions(administrator=True)
async def unlock(ctx: commands.Context) -> None:
    """Unlock the current channel."""
    channel = ctx.channel
    if not isinstance(channel, discord.TextChannel):
        await ctx.send("❌ This command only works in text channels.")
        return

    overwrite = channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = None
    overwrite.add_reactions = None
    try:
        await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
        embed = discord.Embed(
            description=f"🔓 {channel.mention} has been **unlocked**.",
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed)
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to manage this channel.")


@unlock.error
async def unlock_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need the **Administrator** permission to use this command.")


# ═══════════════════════════════════════════════════════════════
#  SLASH COMMANDS
# ═══════════════════════════════════════════════════════════════


@bot.tree.command(
    name="vouch",
    description="Submit a vouch / review (vouch role + admins only)",
)
async def vouch(interaction: discord.Interaction) -> None:
    """Only VOUCH_ROLE_ID holders and admins can use this."""
    member = interaction.user
    if isinstance(member, discord.Member):
        is_authorized = (
            member.guild_permissions.administrator
            or discord.utils.get(member.roles, id=VOUCH_ROLE_ID)
        )
        if not is_authorized:
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True,
            )
            return
    await interaction.response.send_modal(VouchModal())


@bot.tree.command(
    name="ticket",
    description="Create a ticket panel in the current channel",
)
@app_commands.default_permissions(administrator=True)
async def ticket(interaction: discord.Interaction) -> None:
    """Post the ticket panel (embed + dropdown) in the current channel."""
    embed = discord.Embed(
        title="\U0001f3ab Open a Ticket",
        description=(
            "To open a ticket, select the category below that best "
            "matches your inquiry.\n\n"
            "• We do **not** assist customers who purchased through "
            "unauthorized resellers.\n"
            "• Please refrain from opening duplicate tickets — "
            "they will be closed.\n"
            "• Provide a detailed explanation of your issue, including "
            "any relevant screenshots or videos.\n"
            "• Maintain a respectful and courteous attitude at all times."
        ),
        color=BRAND_COLOR,
    )
    embed.set_footer(text=BRAND_FOOTER)

    view = TicketPanelView()

    # Defer the interaction, then send as a regular channel message (not a reply)
    await interaction.response.defer(ephemeral=True)
    await interaction.channel.send(embed=embed, view=view)
    await interaction.followup.send("✅ Ticket panel created!", ephemeral=True)


@bot.tree.command(
    name="close",
    description="Close the current ticket channel",
)
async def close_cmd(interaction: discord.Interaction) -> None:
    """Close the current ticket (staff or ticket creator)."""
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("❌ This only works in text channels.", ephemeral=True)
        return

    guild = interaction.guild
    guild_id = guild.id
    channel_id = channel.id

    # Must be a tracked ticket channel
    chan_map = ticket_channel_map.get(guild_id, {})
    chan_info = chan_map.get(channel_id)
    if not chan_info:
        await interaction.response.send_message(
            "❌ This is not a ticket channel.", ephemeral=True
        )
        return

    # Permission check
    user = interaction.user
    is_creator = isinstance(user, discord.Member) and user.id == chan_info["user_id"]
    if not is_creator and not (isinstance(user, discord.Member) and is_staff_or_admin(user)):
        await interaction.response.send_message(
            "❌ Only the ticket creator or staff can close this ticket.", ephemeral=True
        )
        return

    confirm_embed = discord.Embed(
        title="🔒 Close Ticket?",
        description="Are you sure you want to close this ticket? A transcript will be sent to the ticket creator.",
        color=discord.Color.red(),
    )
    confirm_view = ConfirmCloseView(interaction)
    await interaction.response.send_message(embed=confirm_embed, view=confirm_view)


@bot.tree.command(
    name="add",
    description="Add a user to the current ticket channel",
)
@app_commands.describe(user="The user to add to this ticket")
async def add_user(interaction: discord.Interaction, user: discord.Member) -> None:
    """Add a user to the current ticket."""
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("❌ This only works in text channels.", ephemeral=True)
        return

    guild = interaction.guild
    guild_id = guild.id
    channel_id = channel.id

    chan_map = ticket_channel_map.get(guild_id, {})
    if channel_id not in chan_map:
        await interaction.response.send_message("❌ This is not a ticket channel.", ephemeral=True)
        return

    if not (isinstance(interaction.user, discord.Member) and is_staff_or_admin(interaction.user)):
        await interaction.response.send_message("❌ Only staff can add users to tickets.", ephemeral=True)
        return

    await channel.set_permissions(
        user,
        read_messages=True,
        send_messages=True,
        attach_files=True,
        embed_links=True,
        read_message_history=True,
        reason=f"Added to ticket by {interaction.user}",
    )
    embed = discord.Embed(
        description=f"➕ {user.mention} has been added to this ticket by {interaction.user.mention}.",
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="remove",
    description="Remove a user from the current ticket channel",
)
@app_commands.describe(user="The user to remove from this ticket")
async def remove_user(interaction: discord.Interaction, user: discord.Member) -> None:
    """Remove a user from the current ticket."""
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("❌ This only works in text channels.", ephemeral=True)
        return

    guild = interaction.guild
    guild_id = guild.id
    channel_id = channel.id

    chan_map = ticket_channel_map.get(guild_id, {})
    chan_info = chan_map.get(channel_id)
    if not chan_info:
        await interaction.response.send_message("❌ This is not a ticket channel.", ephemeral=True)
        return

    if not (isinstance(interaction.user, discord.Member) and is_staff_or_admin(interaction.user)):
        await interaction.response.send_message("❌ Only staff can remove users from tickets.", ephemeral=True)
        return

    # Don't allow removing the ticket creator
    if user.id == chan_info["user_id"]:
        await interaction.response.send_message("❌ You cannot remove the ticket creator.", ephemeral=True)
        return

    await channel.set_permissions(user, overwrite=None, reason=f"Removed from ticket by {interaction.user}")
    embed = discord.Embed(
        description=f"➖ {user.mention} has been removed from this ticket by {interaction.user.mention}.",
        color=discord.Color.orange(),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="claim",
    description="Claim the current ticket (staff only)",
)
async def claim(interaction: discord.Interaction) -> None:
    """Claim a ticket to let others know you're handling it."""
    user = interaction.user
    if not isinstance(user, discord.Member) or not is_staff_or_admin(user):
        await interaction.response.send_message("❌ Only staff can claim tickets.", ephemeral=True)
        return

    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("❌ This only works in text channels.", ephemeral=True)
        return

    guild_id = interaction.guild_id
    channel_id = channel.id
    chan_info = ticket_channel_map.get(guild_id, {}).get(channel_id)
    if not chan_info:
        await interaction.response.send_message("❌ This is not a ticket channel.", ephemeral=True)
        return

    if chan_info.get("claimed_by"):
        claimmer = interaction.guild.get_member(chan_info["claimed_by"])
        name = claimmer.mention if claimmer else f"User {chan_info['claimed_by']}"
        await interaction.response.send_message(
            f"⚠️ This ticket is already claimed by {name}.", ephemeral=True
        )
        return

    chan_info["claimed_by"] = user.id
    save_data()

    embed = discord.Embed(
        description=f"🙋 This ticket has been claimed by **{user.mention}**. They will assist you shortly.",
        color=BRAND_COLOR,
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="stats",
    description="Show ticket and vouch statistics",
)
@app_commands.default_permissions(administrator=True)
async def stats(interaction: discord.Interaction) -> None:
    """Display ticket/vouch statistics for this server."""
    guild_id = interaction.guild_id

    # Count open tickets
    open_count = len(open_tickets.get(guild_id, {}))

    # Count total tickets by type
    counters = ticket_counters.get(guild_id, {})
    total_tickets = sum(counters.values())

    # Vouch count
    vouch_count = vouch_counters.get(guild_id, 0)

    # Closed tickets
    closed = closed_ticket_log.get(guild_id, [])
    closed_count = len(closed)

    embed = discord.Embed(
        title=f"📊 {BRAND} — Server Statistics",
        color=BRAND_COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="🎫 Total Tickets Created", value=str(total_tickets), inline=True)
    embed.add_field(name="📂 Currently Open", value=str(open_count), inline=True)
    embed.add_field(name="✅ Closed Tickets", value=str(closed_count), inline=True)
    embed.add_field(name="⭐ Total Vouches", value=str(vouch_count), inline=True)

    # Breakdown by type
    type_lines = []
    for ttype, count in counters.items():
        emoji = TICKET_EMOJIS.get(ttype, "📄")
        type_lines.append(f"{emoji} **{TICKET_TYPES.get(ttype, ttype)}**: {count}")
    if type_lines:
        embed.add_field(name="Tickets by Category", value="\n".join(type_lines), inline=False)

    embed.set_footer(text=BRAND_FOOTER)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="setautorole",
    description="Set the role that is automatically given to new members",
)
@app_commands.describe(role="The role to auto-assign to new members")
@app_commands.default_permissions(administrator=True)
async def setautorole(interaction: discord.Interaction, role: discord.Role) -> None:
    """Set a role to auto-assign when new members join."""
    guild_id = interaction.guild_id
    autorole_map[guild_id] = role.id
    save_data()
    embed = discord.Embed(
        description=f"✅ Auto-role set to **{role.mention}** ({role.name}). New members will now automatically receive this role.",
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="removeautorole",
    description="Remove the auto-role for new members",
)
@app_commands.default_permissions(administrator=True)
async def removeautorole(interaction: discord.Interaction) -> None:
    """Remove the auto-role configuration."""
    guild_id = interaction.guild_id
    if guild_id not in autorole_map:
        await interaction.response.send_message("⚠️ No auto-role is configured for this server.", ephemeral=True)
        return
    del autorole_map[guild_id]
    save_data()
    embed = discord.Embed(
        description="✅ Auto-role has been removed. New members will no longer receive an automatic role.",
        color=discord.Color.orange(),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="rename",
    description="Rename the current ticket channel",
)
@app_commands.describe(name="The new name for the ticket channel")
async def rename_ticket(interaction: discord.Interaction, name: str) -> None:
    """Rename the current ticket channel (staff only)."""
    user = interaction.user
    if not isinstance(user, discord.Member) or not is_staff_or_admin(user):
        await interaction.response.send_message("❌ Only staff can rename tickets.", ephemeral=True)
        return

    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("❌ This only works in text channels.", ephemeral=True)
        return

    guild_id = interaction.guild_id
    if channel.id not in ticket_channel_map.get(guild_id, {}):
        await interaction.response.send_message("❌ This is not a ticket channel.", ephemeral=True)
        return

    safe_name = name.lower().replace(" ", "-").replace("_", "-")
    safe_name = "".join(c for c in safe_name if c.isalnum() or c == "-")
    if not safe_name:
        await interaction.response.send_message("❌ Invalid channel name.", ephemeral=True)
        return

    old_name = channel.name
    try:
        await channel.edit(name=safe_name, reason=f"Renamed by {user}")
        embed = discord.Embed(
            description=f"✏️ Ticket renamed from `{old_name}` to `{safe_name}`",
            color=BRAND_COLOR,
        )
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message("❌ I don't have permission to rename this channel.", ephemeral=True)


# ═══════════════════════════════════════════════════════════════
#  ERROR HANDLER (global)
# ═══════════════════════════════════════════════════════════════

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    """Global error handler for slash commands."""
    if isinstance(error, app_commands.MissingPermissions):
        msg = "❌ You don't have the required permissions to use this command."
    elif isinstance(error, app_commands.BotMissingPermissions):
        msg = "❌ I don't have the required permissions to execute this command."
    elif isinstance(error, app_commands.CommandOnCooldown):
        msg = f"⏳ This command is on cooldown. Try again in {error.retry_after:.1f}s."
    else:
        log.error("Unhandled slash command error: %s", error, exc_info=error)
        msg = "❌ An unexpected error occurred. Please try again later."

    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# ═══════════════════════════════════════════════════════════════
#  EVENTS
# ═══════════════════════════════════════════════════════════════


@bot.event
async def on_ready() -> None:
    """Fires when the bot has connected and cached data."""
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    log.info("Serving %d guild(s)", len(bot.guilds))

    # Set status
    try:
        await bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=f"over {len(bot.guilds)} server(s) • {BRAND}",
            ),
            status=discord.Status.online,
        )
    except Exception:
        pass

    # Register persistent views
    bot.add_view(TicketPanelView())
    bot.add_view(TicketCloseView())
    bot.add_view(TicketClaimView())

    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        log.info("Synced %d slash command(s)", len(synced))
    except Exception as e:
        log.error("Failed to sync commands: %s", e)

    log.info("Bot ready")


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel) -> None:
    """Clean up tracking when a ticket channel is deleted."""
    if not isinstance(channel, discord.TextChannel):
        return

    guild_id = channel.guild.id
    channel_id = channel.id

    if guild_id in ticket_channel_map and channel_id in ticket_channel_map[guild_id]:
        info = ticket_channel_map[guild_id][channel_id]
        uid = info["user_id"]

        del ticket_channel_map[guild_id][channel_id]
        if guild_id in open_tickets and uid in open_tickets[guild_id]:
            del open_tickets[guild_id][uid]
        save_data()

        TicketCloseView._channel_registry.pop(channel_id, None)

    if channel.category:
        await cleanup_empty_category(channel.guild, channel.category)


@bot.event
async def on_member_join(member: discord.Member) -> None:
    """Auto-assign the configured role when a new member joins."""
    guild_id = member.guild.id
    role_id = autorole_map.get(guild_id)
    if role_id is None:
        return

    role = member.guild.get_role(role_id)
    if role is None:
        log.warning("Auto-role with ID %d not found in guild %s", role_id, member.guild.name)
        return

    try:
        await member.add_roles(role, reason="Auto-role on join")
        log.info("Assigned role '%s' to %s (%d)", role.name, member, member.id)
    except discord.Forbidden:
        log.error(
            "Missing permissions to assign role '%s' to %s — check role hierarchy.",
            role.name, member,
        )
    except discord.HTTPException as e:
        log.error("Failed to assign role to %s: %s", member, e)


# ═══════════════════════════════════════════════════════════════
#  AUTO-CLOSE INACTIVE TICKETS (optional background task)
# ═══════════════════════════════════════════════════════════════

@bot.event
async def on_message(message: discord.Message) -> None:
    """Update last-activity timestamp on ticket messages."""
    if message.author.bot:
        return
    if not isinstance(message.channel, discord.TextChannel):
        return

    guild_id = message.guild.id if message.guild else None
    if not guild_id:
        return

    chan_info = ticket_channel_map.get(guild_id, {}).get(message.channel.id)
    if chan_info:
        chan_info["last_activity"] = datetime.now(timezone.utc).isoformat()
        # Save periodically (every 10 messages or so to avoid excessive I/O)
        # For simplicity, just update in memory; save_data is called on ticket create/close


async def auto_close_loop() -> None:
    """Background task: close tickets inactive for AUTO_CLOSE_HOURS."""
    if AUTO_CLOSE_HOURS <= 0:
        return

    await bot.wait_until_ready()
    log.info("Auto-close task started (threshold: %d hours)", AUTO_CLOSE_HOURS)

    while not bot.is_closed():
        try:
            from datetime import timedelta
            now = datetime.now(timezone.utc)
            threshold = now - timedelta(hours=AUTO_CLOSE_HOURS)

            for guild_id, channels in list(ticket_channel_map.items()):
                for channel_id, chan_info in list(channels.items()):
                    last_activity = chan_info.get("last_activity") or chan_info.get("created_at")
                    if not last_activity:
                        continue
                    try:
                        last_dt = datetime.fromisoformat(last_activity)
                    except (ValueError, TypeError):
                        continue
                    if last_dt < threshold:
                        guild = bot.get_guild(guild_id)
                        if not guild:
                            continue
                        channel = guild.get_channel(channel_id)
                        if not isinstance(channel, discord.TextChannel):
                            continue

                        # Close it
                        await channel.send(
                            embed=discord.Embed(
                                description=f"⏰ This ticket has been automatically closed due to **{AUTO_CLOSE_HOURS} hours** of inactivity.",
                                color=discord.Color.red(),
                            )
                        )
                        await asyncio.sleep(2)

                        uid = chan_info.get("user_id")
                        category = channel.category

                        closed_ticket_log.setdefault(guild_id, []).append({
                            "user_id": uid,
                            "type": chan_info.get("type", "unknown"),
                            "number": chan_info.get("number", 0),
                            "closed_by": "Auto-Close",
                            "closed_at": now.isoformat(),
                            "messages": 0,
                        })

                        del ticket_channel_map[guild_id][channel_id]
                        if guild_id in open_tickets and uid in open_tickets[guild_id]:
                            del open_tickets[guild_id][uid]
                        TicketCloseView._channel_registry.pop(channel_id, None)
                        save_data()

                        await channel.delete(reason="Auto-closed due to inactivity")
                        log.info("Auto-closed ticket channel %d (inactive)", channel_id)

                        if category:
                            await cleanup_empty_category(guild, category)
        except Exception as e:
            log.error("Auto-close loop error: %s", e)

        await asyncio.sleep(3600)  # Check every hour


# ═══════════════════════════════════════════════════════════════
#  RUN
# ═══════════════════════════════════════════════════════════════


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")
    do_HEAD = do_GET
    def log_message(self, format, *args):
        pass


def start_health_server(port):
    try:
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        log.info("Health check server running on port %d", port)
        server.serve_forever()
    except Exception as e:
        log.error("Health check server error: %s", e)


if __name__ == "__main__":
    HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8080"))
    load_data()
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        log.error(
            "Please set your bot token in the script or "
            "via the DISCORD_BOT_TOKEN environment variable."
        )
    else:
        threading.Thread(target=start_health_server, args=(HEALTH_PORT,), daemon=True).start()
        if AUTO_CLOSE_HOURS > 0:
            bot.loop.create_task(auto_close_loop())
        bot.run(BOT_TOKEN, reconnect=True)
