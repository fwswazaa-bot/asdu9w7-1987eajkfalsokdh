"""
Tensai Services Discord Bot
────────────────────────────────────────────────────────────────
Single-file bot with:
  - !lock      — Lock the current channel (admins-only bypass)
  - /vouch     — Submit a vouch (restricted to vouch role + admins)
  - /ticket    — Create a ticket panel with category dropdown

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
from datetime import datetime
from typing import Optional, Dict, Any, List
import json
import os
import asyncio

# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION  –  Edit these before running
# ═══════════════════════════════════════════════════════════════

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# Role that is allowed to use /vouch (besides admins)
VOUCH_ROLE_ID = int(os.getenv("VOUCH_ROLE_ID", "1522771389105443006"))

# Staff role name used when pinging in new ticket channels
STAFF_ROLE_NAME = os.getenv("STAFF_ROLE_NAME", "grail")

# File used to persist ticket counters & open-ticket tracking
TICKET_DATA_FILE = "ticket_data.json"

# Gist URL for ticket data backup (leave empty to disable)
GIST_URL = os.getenv("GIST_URL", "")

# Branding
BRAND = os.getenv("BRAND_NAME", "Tensai")
BRAND_FOOTER = f"\u00a92026 {BRAND}. All rights reserved."
BRAND_URL = os.getenv("BRAND_URL", "https://tensai.services")

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
# ticket_channel_map[guild_id] = {channel_id: {"user_id": int, "type": str, "number": int}}
ticket_channel_map: Dict[int, Dict[int, Dict[str, Any]]] = {}
# ticket_counters[guild_id] = {type_str: int}
ticket_counters: Dict[int, Dict[str, int]] = {}
# vouch_counters[guild_id] = int
vouch_counters: Dict[int, int] = {}
# autorole_map[guild_id] = role_id (role to auto-assign to new members)
autorole_map: Dict[int, int] = {}

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


# ── Persistence helpers ────────────────────────────────────────

def load_data() -> None:
    """Load ticket/vouch data from JSON file into global dicts. Falls back to gist."""
    global open_tickets, ticket_channel_map, ticket_counters, vouch_counters, autorole_map
    if not os.path.exists(TICKET_DATA_FILE):
        if GIST_URL:
            try:
                import urllib.request
                with urllib.request.urlopen(GIST_URL, timeout=10) as response:
                    raw = json.loads(response.read().decode())
                print("Loaded ticket data from gist")
            except Exception as e:
                print(f"Warning: Could not load from gist: {e}")
                raw = {"open_tickets": {}, "ticket_channel_map": {}, "ticket_counters": {}, "vouch_counters": {}, "autorole_map": {}}
        else:
            raw = {"open_tickets": {}, "ticket_channel_map": {}, "ticket_counters": {}, "vouch_counters": {}, "autorole_map": {}}
    else:
        try:
            with open(TICKET_DATA_FILE, "r") as f:
                raw = json.load(f)
        except Exception as e:
            print(f"Warning: Could not load ticket data: {e}")
            raw = {"open_tickets": {}, "ticket_channel_map": {}, "ticket_counters": {}, "vouch_counters": {}, "autorole_map": {}}

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
    }
    try:
        with open(TICKET_DATA_FILE, "w") as f:
            json.dump(raw, f, indent=2)
    except Exception as e:
        print(f"Warning: Could not save ticket data: {e}")


# ── Helper functions ──────────────────────────────────────────

def get_staff_role(guild: discord.Guild) -> Optional[discord.Role]:
    """Return the staff role by name, or None."""
    return discord.utils.get(guild.roles, name=STAFF_ROLE_NAME)


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


async def find_or_create_category(
    guild: discord.Guild, ttype: str
) -> discord.CategoryChannel:
    """Return an existing category for *ttype* or create one (staff-only view)."""
    cat_name = CATEGORY_NAMES[ttype]
    existing = discord.utils.get(guild.categories, name=cat_name)
    if existing:
        return existing

    staff_role = get_staff_role(guild)
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
        await category.delete(reason="Category empty -- all tickets closed")


def make_ticket_channel_name(member: discord.Member, ttype: str, number: int) -> str:
    """Generate a channel name like ``johndoe-support-3``."""
    safe_name = member.name.lower().replace(" ", "-").replace("_", "-")
    safe_name = "".join(c for c in safe_name if c.isalnum() or c == "-")
    type_part = ttype.replace("_", "-").replace(" ", "-")
    return f"{safe_name}-{type_part}-{number}"


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
        placeholder="e.g. 5/5, 10/10, \u2605\u2605\u2605\u2605\u2605",
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
            title="\u2728 New Vouch!",
            description=f"**{interaction.user.mention}** has submitted a new vouch!",
            color=discord.Color.green(),
            timestamp=datetime.now(),
        )
        embed.add_field(name="Vouch", value=self.vouch_text.value, inline=False)
        embed.add_field(name="Product", value=self.product.value, inline=True)
        embed.add_field(name="Rating", value=self.rating.value, inline=True)
        if self.media.value.strip():
            embed.add_field(name="Media", value=self.media.value, inline=False)
            embed.set_image(url=self.media.value.strip())
        embed.add_field(
            name="Vouched At",
            value=f"<t:{int(datetime.now().timestamp())}:F>",
            inline=False,
        )
        embed.set_footer(text=f"{BRAND_FOOTER}  |  Vouch #{vouch_num}")

        await interaction.response.send_message(embed=embed)


# ── Ticket Modals (one per type) ──────────────────────────────

class BaseTicketModal(ui.Modal):
    """Shared logic for all ticket-type modals."""

    def __init__(
        self,
        channel: discord.TextChannel,
        member: discord.Member,
        ttype: str,
        number: int,
        title: str,
    ) -> None:
        super().__init__(title=title)
        self._channel = channel
        self._member = member
        self._ttype = ttype
        self._number = number

    async def _finish_ticket(self, interaction: discord.Interaction, embed: discord.Embed) -> None:
        """Send the embed + close button into the ticket channel, then notify the user."""
        content_parts = [interaction.guild.default_role.mention]
        staff_role = get_staff_role(interaction.guild)
        if staff_role:
            content_parts.append(staff_role.mention)
        await self._channel.send(content=" ".join(content_parts), embed=embed)

        # Register the channel before sending the close view so the button
        # callback can verify permissions immediately (no race condition).
        TicketCloseView._channel_registry[self._channel.id] = {
            "guild_id": interaction.guild_id,
            "user_id": self._member.id,
        }
        close_view = TicketCloseView()
        await self._channel.send("\u200b", view=close_view)

        await interaction.response.send_message(
            f"\u2705 Your ticket has been created in {self._channel.mention}",
            ephemeral=True,
        )


class PurchaseModal(BaseTicketModal):
    """Modal for purchase ticket details."""

    product = ui.TextInput(label="Product", placeholder="What product are you purchasing?", required=True, max_length=200)
    duration = ui.TextInput(label="Duration / Plan", placeholder="e.g. Monthly, Lifetime, 1 Year", required=True, max_length=100)
    payment_method = ui.TextInput(label="Payment Method", placeholder="e.g. PayPal, Crypto, Card", required=True, max_length=100)
    order_id = ui.TextInput(label="Order ID (optional)", placeholder="Transaction / invoice ID if you have one", required=False, max_length=200)

    def __init__(self, channel: discord.TextChannel, member: discord.Member, ttype: str, number: int) -> None:
        super().__init__(channel, member, ttype, number, title="Purchase Ticket")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title=f"\U0001f6d2 {BRAND} \u2013 Purchase Ticket",
            description=(
                "Please describe your question or issue below.\n\n"
                "\u2022 Do not ping staff.\n"
                "\u2022 Our team will assist you soon."
            ),
            color=discord.Color.blue(),
            timestamp=datetime.now(),
        )
        embed.add_field(name="Product", value=f"`{self.product.value}`", inline=True)
        embed.add_field(name="Duration", value=f"`{self.duration.value}`", inline=True)
        embed.add_field(name="Payment Method", value=f"`{self.payment_method.value}`", inline=True)
        embed.add_field(name="Order ID", value=f"`{self.order_id.value or 'N/A'}`", inline=True)
        embed.set_footer(text=BRAND_FOOTER)
        embed.set_author(name=self._member.display_name, icon_url=self._member.display_avatar.url)
        await self._finish_ticket(interaction, embed)


class SupportModal(BaseTicketModal):
    """Modal for support ticket details."""

    issue_category = ui.TextInput(label="Issue Category", placeholder="e.g. Billing, Technical, Account", required=True, max_length=100)
    description = ui.TextInput(label="Describe your issue", style=discord.TextStyle.long, placeholder="Provide as much detail as possible...", required=True, max_length=2000)
    steps_taken = ui.TextInput(label="Steps Already Taken", style=discord.TextStyle.long, placeholder="What have you tried so far?", required=False, max_length=2000)
    solution = ui.TextInput(label="Preferred Solution (optional)", placeholder="What solution are you hoping for?", required=False, max_length=500)

    def __init__(self, channel: discord.TextChannel, member: discord.Member, ttype: str, number: int) -> None:
        super().__init__(channel, member, ttype, number, title="Support Ticket")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title=f"\U0001f527 {BRAND} \u2013 Support Ticket",
            description=(
                "Please describe your question or issue below.\n\n"
                "\u2022 Do not ping staff.\n"
                "\u2022 Our team will assist you soon."
            ),
            color=discord.Color.orange(),
            timestamp=datetime.now(),
        )
        embed.add_field(name="Issue Category", value=f"`{self.issue_category.value}`", inline=False)
        embed.add_field(name="Description", value=f"`{self.description.value}`", inline=False)
        embed.add_field(name="Steps Taken", value=f"`{self.steps_taken.value or 'None provided'}`", inline=False)
        embed.add_field(name="Preferred Solution", value=f"`{self.solution.value or 'N/A'}`", inline=False)
        embed.set_footer(text=BRAND_FOOTER)
        embed.set_author(name=self._member.display_name, icon_url=self._member.display_avatar.url)
        await self._finish_ticket(interaction, embed)


class HWIDModal(BaseTicketModal):
    """Modal for HWID reset ticket details."""

    reason = ui.TextInput(label="Reason for Reset", placeholder="Why do you need an HWID reset?", required=True, max_length=500)
    old_hwid = ui.TextInput(label="Old HWID (if known)", placeholder="Leave blank if you don't know", required=False, max_length=200)
    new_hwid = ui.TextInput(label="New HWID", placeholder="Your new HWID / machine ID", required=True, max_length=200)
    proof = ui.TextInput(label="Proof of Purchase URL", placeholder="Link to purchase receipt or DM", required=False, max_length=500)

    def __init__(self, channel: discord.TextChannel, member: discord.Member, ttype: str, number: int) -> None:
        super().__init__(channel, member, ttype, number, title="HWID Reset Ticket")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title=f"\U0001f504 {BRAND} \u2013 HWID Reset Ticket",
            description=(
                "Please describe your question or issue below.\n\n"
                "\u2022 Do not ping staff.\n"
                "\u2022 Our team will assist you soon."
            ),
            color=discord.Color.purple(),
            timestamp=datetime.now(),
        )
        embed.add_field(name="Reason", value=f"`{self.reason.value}`", inline=False)
        embed.add_field(name="Old HWID", value=f"`{self.old_hwid.value or 'Unknown'}`", inline=True)
        embed.add_field(name="New HWID", value=f"`{self.new_hwid.value}`", inline=True)
        embed.add_field(name="Proof of Purchase", value=f"`{self.proof.value or 'N/A'}`", inline=False)
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
                description="For general support",
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
                    f"\u26a0\ufe0f You already have an open {TICKET_TYPES[existing['type']]} "
                    f"in {existing_channel.mention}. "
                    "Please close it before opening a new one.",
                    ephemeral=True,
                )
                return
            # Channel was deleted manually — clean up stale tracking
            del open_tickets[guild_id][member.id]
            save_data()

        # ── Create category if needed ─────────────────────────
        try:
            category = await find_or_create_category(guild, ttype)
        except Exception as e:
            await interaction.response.send_message(
                f"\u274c Failed to create category: {e}", ephemeral=True
            )
            return

        # ── Ticket number & channel name ──────────────────────
        number = get_ticket_number(guild_id, ttype)
        channel_name = make_ticket_channel_name(member, ttype, number)

        # ── Channel permission overwrites ─────────────────────
        staff_role = get_staff_role(guild)
        overwrites: Dict[discord.Role, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            member: discord.PermissionOverwrite(
                read_messages=True, send_messages=True,
                attach_files=True, embed_links=True,
            ),
            guild.me: discord.PermissionOverwrite(
                read_messages=True, send_messages=True,
                manage_channels=True, manage_permissions=True,
                manage_messages=True,
            ),
        }
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(
                read_messages=True, send_messages=True, manage_messages=True
            )

        # ── Create the channel ────────────────────────────────
        try:
            channel = await guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                reason=f"Ticket #{number} ({TICKET_TYPES[ttype]}) opened by {member}",
            )
        except Exception as e:
            await interaction.response.send_message(
                f"\u274c Failed to create ticket channel: {e}", ephemeral=True
            )
            return

        # ── Track it ──────────────────────────────────────────
        open_tickets.setdefault(guild_id, {})[member.id] = {
            "type": ttype, "channel_id": channel.id,
        }
        ticket_channel_map.setdefault(guild_id, {})[channel.id] = {
            "user_id": member.id,
            "type": ttype,
            "number": number,
        }
        save_data()

        # ── Show the appropriate modal ────────────────────────
        modals: Dict[str, type] = {
            "purchase": PurchaseModal,
            "support": SupportModal,
            "hwid": HWIDModal,
        }
        modal = modals[ttype](channel, member, ttype, number)
        await interaction.response.send_modal(modal)


class TicketPanelView(ui.View):
    """View containing the ticket-type dropdown. Persistent (timeout=None)."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(TicketSelect())


# ── Ticket Close Button ───────────────────────────────────────

class TicketCloseView(ui.View):
    """Self-contained close button for ticket channels.

    This view is fully derived from interaction data so it works as a
    persistent view (no constructor args needed).
    """

    # Maps channel_id -> {"guild_id": int, "user_id": int}
    # Populated when a ticket is created so the close button can verify permissions.
    _channel_registry: Dict[int, Dict[str, int]] = {}

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @ui.button(
        label="\U0001f512 Close Ticket",
        style=discord.ButtonStyle.danger,
        custom_id="close_ticket_btn",
    )
    async def close_ticket(
        self, interaction: discord.Interaction, button: ui.Button
    ) -> None:
        channel_id = interaction.channel_id
        guild = interaction.guild

        # Look up ticket info from our registry
        info = self._channel_registry.get(channel_id)
        if not info:
            # Fall back to the persisted map
            guild_id = guild.id
            chan_map = ticket_channel_map.get(guild_id, {})
            chan_info = chan_map.get(channel_id)
            if chan_info:
                info = {"guild_id": guild_id, "user_id": chan_info["user_id"]}

        if not info:
            await interaction.response.send_message(
                "\u274c Could not identify this ticket channel.", ephemeral=True
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
                "\u274c Only the ticket creator or staff can close this ticket.",
                ephemeral=True,
            )
            return

        # ── Confirm and close ─────────────────────────────────
        await interaction.response.send_message("\u23f3 Closing this ticket in 3 seconds...")
        await asyncio.sleep(3)

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return

        category = channel.category
        guild_id = guild.id

        try:
            await channel.delete(reason=f"Ticket closed by {user}")
        except discord.Forbidden:
            await interaction.followup.send(
                "\u274c I don't have permission to delete this channel."
            )
            return
        except discord.NotFound:
            pass

        # ── Cleanup tracking ──────────────────────────────────
        if guild_id in ticket_channel_map and channel_id in ticket_channel_map[guild_id]:
            uid = ticket_channel_map[guild_id][channel_id]["user_id"]
            del ticket_channel_map[guild_id][channel_id]
            if guild_id in open_tickets and uid in open_tickets[guild_id]:
                del open_tickets[guild_id][uid]
            save_data()

        # Remove from local registry
        self._channel_registry.pop(channel_id, None)

        # Cleanup empty category
        if category:
            await cleanup_empty_category(guild, category)


# ═══════════════════════════════════════════════════════════════
#  PREFIX COMMANDS
# ═══════════════════════════════════════════════════════════════

@bot.command(name="lock")
@commands.has_permissions(administrator=True)
async def lock(ctx: commands.Context) -> None:
    """Lock the current channel so only admins can speak."""
    channel = ctx.channel
    if not isinstance(channel, discord.TextChannel):
        await ctx.send("\u274c This command only works in text channels.")
        return

    overwrite = channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = False
    overwrite.add_reactions = False
    try:
        await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
        await ctx.send(
            f"\U0001f512 {channel.mention} has been **locked**. "
            "Only administrators may speak."
        )
    except discord.Forbidden:
        await ctx.send("\u274c I don't have permission to manage this channel.")


@lock.error
async def lock_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(
            "\u274c You need the **Administrator** permission to use this command."
        )


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
                "\u274c You do not have permission to use this command.",
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
            "To open a ticket, please select the category below that best "
            "matches your inquiry.\n\n"
            "\u2022 We do **not** assist customers who purchased through "
            "unauthorized resellers.\n"
            "\u2022 Please refrain from opening multiple tickets \u2014 "
            "duplicate tickets will be closed.\n"
            "\u2022 Provide a detailed explanation of your issue, including "
            "any relevant screenshots or videos.\n"
            "\u2022 Your cooperation throughout the process is expected.\n"
            "\u2022 Maintain a respectful and courteous attitude at all times."
        ),
        color=discord.Color.blue(),
    )
    embed.set_footer(text=BRAND_FOOTER)

    view = TicketPanelView()
    await interaction.response.send_message(embed=embed, view=view)



@bot.tree.command(
    name="setautorole",
    description="Set the role that is automatically given to all new members",
)
@app_commands.describe(role="The role to auto-assign to new members")
@app_commands.default_permissions(administrator=True)
async def setautorole(interaction: discord.Interaction, role: discord.Role) -> None:
    """Set a role to auto-assign when new members join."""
    guild_id = interaction.guild_id
    autorole_map[guild_id] = role.id
    save_data()
    await interaction.response.send_message(
        f"\u2705 Auto-role set to **{role.mention}** "
        f"({role.name}). New members will now automatically receive this role.",
        ephemeral=False,
    )


@setautorole.error
async def setautorole_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "\u274c You need the **Administrator** permission to use this command.",
            ephemeral=True,
        )

# ═══════════════════════════════════════════════════════════════
#  EVENTS
# ═══════════════════════════════════════════════════════════════

@bot.event
async def on_ready() -> None:
    """Fires when the bot has connected and cached data."""
    print(f"\u2705 Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"   Serving {len(bot.guilds)} guild(s)")

    # Register persistent views (they use custom_id for dispatch)
    bot.add_view(TicketPanelView())
    bot.add_view(TicketCloseView())

    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        print(f"   Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"   Failed to sync commands: {e}")

    print("\u2500\u2500 Bot ready \u2500\u2500")


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel) -> None:
    """Clean up tracking when a ticket channel is deleted."""
    if not isinstance(channel, discord.TextChannel):
        return

    guild_id = channel.guild.id
    channel_id = channel.id

    # Is this a tracked ticket channel?
    if guild_id in ticket_channel_map and channel_id in ticket_channel_map[guild_id]:
        info = ticket_channel_map[guild_id][channel_id]
        uid = info["user_id"]

        del ticket_channel_map[guild_id][channel_id]
        if guild_id in open_tickets and uid in open_tickets[guild_id]:
            del open_tickets[guild_id][uid]
        save_data()

        # Also clean the close-button registry
        TicketCloseView._channel_registry.pop(channel_id, None)

    # Check if the parent category should be cleaned up
    if channel.category:
        await cleanup_empty_category(channel.guild, channel.category)



@bot.event
async def on_member_join(member: discord.Member) -> None:
    """Auto-assign the configured role when a new member joins."""
    guild_id = member.guild.id
    role_id = autorole_map.get(guild_id)
    if role_id is None:
        return  # No auto-role configured for this guild

    role = member.guild.get_role(role_id)
    if role is None:
        print(f"Warning: Auto-role with ID {role_id} not found in guild {member.guild.name}")
        return

    try:
        await member.add_roles(role, reason="Auto-role on join")
        print(f"   Assigned role '{role.name}' to {member} ({member.id})")
    except discord.Forbidden:
        print(
            f"   ❌ Missing permissions to assign role '{role.name}' "
            f"to {member} — check role hierarchy."
        )
    except discord.HTTPException as e:
        print(f"   ❌ Failed to assign role to {member}: {e}")


# ═══════════════════════════════════════════════════════════════
#  RUN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    load_data()
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print(
            "\u274c Please set your bot token in the script or "
            "via the DISCORD_BOT_TOKEN environment variable."
        )
    else:
        bot.run(BOT_TOKEN, reconnect=True)
