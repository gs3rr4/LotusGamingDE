from __future__ import annotations

import asyncio
import base64
import binascii
import difflib
import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import discord
from discord.ext import commands

from lotus_bot.log_setup import get_logger
from lotus_bot.utils.managed_cog import ManagedTaskCog

from .api import (
    DEFAULT_LOCALE,
    DEFAULT_NAMESPACE,
    WoWAPIError,
    fetch_character_profile,
    fetch_guild_roster,
    wow_api_session,
)
from .data import (
    BankCharacter,
    CharacterClaim,
    CharacterKnownRecipe,
    CharacterProfession,
    Cooldown,
    GearMilestoneEvent,
    ProfessionSkillEvent,
    RecipeLearningEvent,
    RosterMember,
    WoWData,
    parse_roster_member,
)

logger = get_logger(__name__)

DEFAULT_REALM_SLUG = "soulseeker"
DEFAULT_GUILD_SLUG = "black-lotus"
DEFAULT_GUILD_NAME = "Black Lotus"
DEFAULT_POLL_INTERVAL = 3 * 60 * 60
# Officer bot channel — receives ALL officer-facing bot output: claim reviews,
# the sync report, gbank requests, departed-claim / main-died / promote notes.
# Moved off the human discussion channel (1184115540822855772) into #wow-logs so
# officer chat isn't drowned out by bot posts.
DEFAULT_CLAIM_REVIEW_CHANNEL_ID = 1544601948169314324
DEFAULT_PANEL_CHANNEL_ID = 1463577361562992807
DEFAULT_DIGEST_HOUR = 9
# Forum channel where Raid-Helper posts dungeon-run events. Raid-Helper is a
# THIRD-PARTY bot — we can't drive its /create flow ourselves, so this bot
# only maintains a pinned, purely informational how-to post here (see
# DUNGEONS_GUIDE_TEXT / publish_dungeons_guide).
DUNGEONS_FORUM_CHANNEL_ID = 1214627449804165170
# Raid-Helper's bot user id (not the /create command's own id, used below —
# those are two different Discord snowflakes), so we can @-mention it.
RAID_HELPER_BOT_ID = 579155972115660803
# Slash-command mention for Raid-Helper's own /create — a real </name:id>
# mention (not backtick text) so it's directly clickable and unambiguous,
# no need to tell people to look for the right bot icon anymore.
DUNGEONS_GUIDE_TEXT = (
    "# ⚔️ Dungeon-Run erstellen\n"
    "**1.** Klicke auf </create:885023455739777079> in "
    "<#1521609258066641037> oder <#1152491425422901248>.\n\n"
    f"**2.** <@{RAID_HELPER_BOT_ID}> fragt dich Schritt für Schritt nach Titel, "
    "Beschreibung, Forum (wähle <#1214627449804165170>) und Zeitpunkt "
    "(z. B. `Heute 19:00`).\n\n"
    "**⚠️ Wichtig, das übersehen viele:** Bei jeder dieser Fragen ganz "
    "normal **im Chat antworten** (tippen + abschicken) — **nicht** "
    "anklicken. Nur im **allerletzten** Schritt (die Übersicht mit der "
    "Nummernliste) klickst du auf **Finish**, um den Run wirklich zu "
    "erstellen.\n\n"
    "Danach landet dein Run automatisch hier in <#1214627449804165170>. 🪷"
)
# Guild role the bot fully owns: granted to every member with at least one
# verified char claim, stripped from everyone else. Reconciled hourly plus
# on each claim verify/release so it always mirrors the claim table.
GUILD_ROLE_ID = 1448935193921454100
# Lightweight hourly roster pull so freshly-joined chars are claimable without
# waiting for the next 9 o'clock digest run. Fetches only the guild roster
# (one API call) — no per-character profile calls, no digest, no posting.
DEFAULT_ROSTER_REFRESH_INTERVAL = 60 * 60
# In-game guild rank layout (Blizzard rank index → meaning). The roster API only
# exposes the integer rank; this fixed map gives it meaning. "Member or higher"
# is any rank <= MEMBER_RANK. Drives the claim-review hints and the Offi-Sync-
# Report. Adjust here if the guild's rank structure ever changes.
GUILD_RANKS = {
    0: "Gildenmeister",
    1: "Offizier",
    2: "Officer Twink",
    3: "Veteran",
    4: "Member",
    5: "Bank",
    6: "Twink",
    7: "Initiate",
}
# "Officer Twink" is a sanctioned SECOND high-rank char for an officer — it sits
# above Member but does NOT count as a main slot (see the multi-main check).
OFFICER_TWINK_RANK = 2
MEMBER_RANK = 4
BANK_RANK = 5
# Everyone at Twink rank or higher (rank <= TWINK_RANK) is expected to have a
# char claim; only Initiate (below Twink) is the unclaimed "probation" tier.
TWINK_RANK = 6
INITIATE_RANK = 7
try:
    DIGEST_TIMEZONE = ZoneInfo("Europe/Berlin")
except ZoneInfoNotFoundError:  # pragma: no cover - depends on host tzdata
    DIGEST_TIMEZONE = datetime.now().astimezone().tzinfo or timezone.utc
MILESTONE_LEVELS = {30, 40, 50, 60}
GHOST_REFRESH_CONCURRENCY = 8
ITEM_LEVEL_MILESTONES = {50, 55, 60, 65, 70, 75}
ITEM_LEVEL_MILESTONE_POINTS = {50: 2, 55: 2, 60: 5, 65: 8, 70: 15, 75: 25}
CLAIMED_MILESTONE_POINTS = {30: 5, 40: 10, 50: 20, 60: 50}
# Recipe rewards scale with two axes: how the recipe is acquired (source)
# and how skilled the crafter has to be (required_skill bracket).
RECIPE_POINTS_BY_SOURCE = {
    "world_drop": 4,  # rare BoE world drops
    "pickpocketed": 3,  # rogue-exclusive
    "drop": 2,  # regular mob/boss drops (incl. drop+vendor)
    "quest": 1,  # deterministic quest reward
}
RECIPE_SKILL_BRACKETS = [
    (276, 3.0),  # endgame
    (201, 2.0),
    (101, 1.5),
    (0, 1.0),
]
EPIC_RECIPE_BASE_POINTS = 10  # special hand-curated spell IDs
# Profession-skill milestones (cumulative): each threshold crossed awards
# the points once. Total for a 1→300 run is 46 points.
PROFESSION_SKILL_MILESTONES = {75: 3, 150: 6, 225: 12, 300: 25}
# Hardcoded craft-cooldown groups for WoW Classic Era 1.x. Add new entries
# here if a content patch introduces more (e.g. Spellcloth in TBC).
COOLDOWN_GROUPS: dict[str, dict] = {
    "alchemy_transmute": {
        "label": "Transmutationen",
        "hours": 48,
        "spell_ids": frozenset(
            {
                "spell.17187",  # Transmute: Arcanite
                "spell.11479",  # Transmute: Iron to Gold
                "spell.11480",  # Transmute: Mithril to Truesilver
                "spell.17559",  # Transmute: Air to Fire
                "spell.17560",  # Transmute: Fire to Earth
                "spell.17561",  # Transmute: Earth to Water
                "spell.17562",  # Transmute: Water to Air
                "spell.17563",  # Transmute: Undeath to Water
                "spell.17564",  # Transmute: Water to Undeath
                "spell.17565",  # Transmute: Life to Earth
                "spell.17566",  # Transmute: Earth to Life
                "spell.25146",  # Transmute: Elemental Fire
            }
        ),
    },
    "tailoring_mooncloth": {
        "label": "Mondstoff",
        "hours": 96,
        "spell_ids": frozenset({"spell.18560"}),
    },
    "leatherworking_salt_shaker": {
        "label": "Salt Shaker",
        "hours": 72,
        "spell_ids": frozenset({"spell.19566"}),
    },
}
# Reverse-lookup: spell_id → (group_key, group_config). Built once at
# module load — every COOLDOWN_GROUPS edit is reflected automatically.
COOLDOWN_SPELL_TO_GROUP: dict[str, tuple[str, dict]] = {
    spell_id: (group_key, group)
    for group_key, group in COOLDOWN_GROUPS.items()
    for spell_id in group["spell_ids"]
}
MAX_PRIMARY_PROFESSIONS = 2
SECONDARY_CRAFTING_PROFESSIONS = {"cooking"}
EXCLUDED_CRAFTING_PROFESSIONS = {"first-aid", "fishing"}
# The practitioner noun for a profession ("Schmied"), distinct from the
# skill's own name ("Schmiedekunst", returned by _profession_name — that one
# comes from the live Blizzard-API static data). Used e.g. in the
# Marketplace's Crafting-Gesuch thread titles ("Schmied gesucht für X").
# Classic doesn't track which armor/weapon specialization a recipe needs, so
# this deliberately stays generic (no "Rüstungsschmied" vs. "Waffenschmied").
PROFESSION_PRACTITIONER_TITLES = {
    "blacksmithing": "Schmied",
    "leatherworking": "Lederer",
    "tailoring": "Schneider",
    "engineering": "Ingenieur",
    "alchemy": "Alchemist",
    "enchanting": "Verzauberer",
    "cooking": "Koch",
    "jewelcrafting": "Juwelier",
}
EPIC_RECIPE_SPELL_IDS = {
    "spell.22749",  # Enchant Weapon - Spell Power
    "spell.22750",  # Enchant Weapon - Healing Power
    "spell.20034",  # Enchant Weapon - Crusader
    "spell.20036",  # Enchant 2H Weapon - Major Intellect
    "spell.20035",  # Enchant 2H Weapon - Major Spirit
    "spell.16994",  # Arcanite Reaper
    "spell.16990",  # Arcanite Champion
    "spell.19830",  # Arcanite Dragonling
    "spell.24121",  # Primal Batskin Jerkin
    "spell.24122",  # Primal Batskin Gloves
    "spell.24123",  # Primal Batskin Bracers
}

CLASS_NAMES_DE = {
    1: "Krieger",
    2: "Paladin",
    3: "Jäger",
    4: "Schurke",
    5: "Priester",
    7: "Schamane",
    8: "Magier",
    9: "Hexenmeister",
    11: "Druide",
}
CLASS_EMOJI_NAMES = {
    1: "wow_warrior",
    2: "wow_paladin",
    3: "wow_hunter",
    4: "wow_rogue",
    5: "wow_priest",
    7: "wow_shaman",
    8: "wow_mage",
    9: "wow_warlock",
    11: "wow_druid",
}
RAIDER_ROLE_ID = 1201977228167221248
HORDE_RED = discord.Colour(0xC41E3A)  # official Horde crimson
RACE_NAMES_DE = {
    1: "Mensch",
    2: "Orc",
    3: "Zwerg",
    4: "Nachtelf",
    5: "Untoter",
    6: "Tauren",
    7: "Gnom",
    8: "Troll",
}
DIGEST_OPENERS = [
    "🪷 **Black Lotus Tagesbericht**",
    "📜 **Was gestern bei Black Lotus passiert ist**",
    "🌅 **Guten Morgen, Black Lotus**",
    "🪷 **Neues aus der Gilde**",
    "⚔️ **Unser Hardcore-Tag — kurz zusammengefasst**",
]
DIGEST_POSITIVE_CLOSERS = [
    "Glückwunsch an alle, die gestern Fortschritt gemacht haben. Weiter so — sichere Wege! 🪷",
    "Stark gespielt. Mögen die nächsten Pulls genauso sauber laufen.",
    "Black Lotus gratuliert. Heute geht es weiter.",
]
DIGEST_MIXED_CLOSERS = [
    "Glückwunsch an die Aufsteiger — und Respekt für alle gefallenen Chars. 🕯️",
    "Hardcore bleibt gnadenlos. Passt heute gut aufeinander auf.",
    "Fortschritt und Verluste liegen nah beieinander. Bleibt wachsam da draußen.",
]
DIGEST_DEATH_CLOSERS = [
    "Ruhe in Frieden. Der nächste Char trägt die Geschichte weiter. 🕯️",
    "Hardcore vergisst nichts. Passt heute gut aufeinander auf.",
    "Ein stiller Gruß an alle gefallenen Chars.",
]


@dataclass
class Milestone:
    member: RosterMember
    level: int


@dataclass
class DeathEvent:
    member: RosterMember
    confirmed: bool = True
    average_item_level: float | None = None


@dataclass
class OfficerNote:
    member: RosterMember
    message: str


@dataclass
class ActivityDiff:
    new_members: list[RosterMember]
    milestones: list[Milestone]
    deaths: list[DeathEvent]
    officer_notes: list[OfficerNote]
    recipe_events: list[RecipeLearningEvent] | None = None
    gear_events: list[GearMilestoneEvent] | None = None
    skill_events: list[ProfessionSkillEvent] | None = None
    cooldowns_ready: list[Cooldown] | None = None

    @property
    def public_count(self) -> int:
        return (
            len(self.new_members)
            + len(self.milestones)
            + len(self.deaths)
            + len(self.recipe_events or [])
            + len(self.gear_events or [])
            + len(self.skill_events or [])
            + len(self.cooldowns_ready or [])
        )


@dataclass
class ScanResult:
    member_count: int
    milestones: list[Milestone]
    new_members: list[RosterMember] | None = None
    deaths: list[DeathEvent] | None = None
    officer_notes: list[OfficerNote] | None = None
    recipe_events: list[RecipeLearningEvent] | None = None
    gear_events: list[GearMilestoneEvent] | None = None
    skill_events: list[ProfessionSkillEvent] | None = None
    cooldowns_ready: list[Cooldown] | None = None
    posted: int = 0


@dataclass
class ClaimResult:
    claim: CharacterClaim | None
    member: RosterMember | None
    created: bool = False
    reason: str = ""
    review_posted: bool = False


@dataclass
class CraftingProfileResult:
    profession: CharacterProfession | None
    claim: CharacterClaim | None = None
    reason: str = ""


@dataclass
class CraftingSearchResult:
    status: str
    item: dict | None = None
    candidates: list[dict] | None = None
    recipe: dict | None = None
    crafters: list[CharacterProfession] | None = None
    required_skill: int = 0
    profession_id: str | None = None
    manual_recipe: bool = False


@dataclass
class RecipeSelectionResult:
    status: str
    claim: CharacterClaim | None = None
    profiles: list[CharacterProfession] | None = None
    profile: CharacterProfession | None = None
    recipes: list[dict] | None = None


@dataclass
class ProfessionImportResult:
    """Outcome of decoding + importing a LotusDiscordBridge addon export.

    Deliberately writes to NO new storage: skill level goes into the
    existing ``character_professions`` (the crafter search already reads
    this for trainer recipes via required_skill), and only the subset of
    imported recipes that also qualifies under the existing points-eligible
    character_known_recipes rules (non-trainer, hardcore_valid) is recorded
    there (the crafter search already reads this table for special recipes)
    - importing never awards points beyond what manual recipe curation
    already would, and the search command needs no changes at all.
    ``recipes_seen`` is just the informational count of recipes the export
    contained, for the confirmation message. ``unmatched`` lists recipes
    that matched nothing in our Wowhead-sourced data at all, surfaced to the
    caller; the same detail is always logged regardless.
    """

    status: str
    claim: CharacterClaim | None = None
    character_name: str | None = None
    professions_imported: int = 0
    recipes_seen: int = 0
    special_recipes_learned: int = 0
    unmatched: list[str] | None = None


@dataclass
class PanelPublishResult:
    channel_id: int
    message_id: int
    created: bool


@dataclass
class RoleSyncResult:
    """Outcome of a full guild-role reconcile."""

    eligible: int
    granted: int
    removed: int
    available: bool


@dataclass
class SyncReport:
    """Discrepancies between verified claims and in-game guild ranks.

    * ``initiate_claims`` — verified claim, but the char still sits on Initiate
      (confirmed yet never promoted; must NOT be kicked).
    * ``unclaimed_ranked`` — char on Twink rank or higher (rank <= TWINK_RANK)
      with no claim at all. Policy: Twink+ must be claimed, so these should be
      claimed or demoted. Registered guild-bank chars are excluded. Initiates
      are deliberately NOT listed — they are new/probation and must not be
      surfaced as kick candidates.
    * ``no_main_users`` — ``(discord_user_id, [member, ...])`` for users who own
      a Twink/Bank char but NO Member+ main (e.g. their main died). The highest
      remaining char should be promoted. Initiate-only owners are not flagged.
    * ``multi_member_users`` — ``(discord_user_id, [(claim, member), ...])`` for
      users with more than one Member+ "main slot" char (invariant: only one
      allowed). The sanctioned Officer-Twink rank is excluded from this count.

    Note: claims whose Discord owner has left the server are pruned
    automatically (see :meth:`WoWCog.prune_departed_claims`), so they never
    reach this report — the now-ownerless char simply shows up under
    ``unclaimed_ranked`` if it is Twink+.
    """

    initiate_claims: list[tuple["CharacterClaim", RosterMember]]
    unclaimed_ranked: list[RosterMember]
    no_main_users: list[tuple[int, list[RosterMember]]]
    multi_member_users: list[tuple[int, list[tuple["CharacterClaim", RosterMember]]]]

    @property
    def empty(self) -> bool:
        return not (
            self.initiate_claims
            or self.unclaimed_ranked
            or self.no_main_users
            or self.multi_member_users
        )


class WoWCog(ManagedTaskCog):
    """Track Black Lotus WoW Classic Hardcore milestones."""

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot
        self.data = WoWData("data/pers/wow/wow.db")
        self.realm_slug = DEFAULT_REALM_SLUG
        self.guild_slug = DEFAULT_GUILD_SLUG
        self.guild_name = DEFAULT_GUILD_NAME
        self.namespace = DEFAULT_NAMESPACE
        self.locale = DEFAULT_LOCALE
        self.poll_interval = DEFAULT_POLL_INTERVAL
        self.roster_refresh_interval = DEFAULT_ROSTER_REFRESH_INTERVAL
        self._scan_lock = asyncio.Lock()
        self._track_task = self.create_task
        self._track_task(self._poll_loop())
        self._track_task(self._roster_refresh_loop())
        if hasattr(self.bot, "add_view"):
            self.bot.add_view(ClaimReviewView(self))
            self.bot.add_view(WoWPanelLayoutView(self))

    async def _poll_loop(self) -> None:
        await self.bot.wait_until_ready()
        await self._auto_publish_panel()
        await self._auto_publish_dungeons_guide()
        while True:
            try:
                if await self._scheduled_digest_due():
                    await self._run_scheduled_digest()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.error("[WoWCog] Polling failed: %s", exc, exc_info=True)
            await asyncio.sleep(self._seconds_until_next_digest())

    async def _roster_refresh_loop(self) -> None:
        """Hourly: refresh the live roster and reconcile the guild role.

        Independent of the daily digest loop. The roster pull keeps freshly
        joined characters claimable within the hour; the role reconcile is the
        self-healing backbone that corrects any drift (missed verify/release
        events, manual role edits, members who left and rejoined).
        """
        await self.bot.wait_until_ready()
        # Reconcile once at startup before entering the hourly cadence so a
        # restart picks up role changes that happened while the bot was down.
        try:
            await self.sync_guild_role()
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.error(
                "[WoWCog] Initial guild-role sync failed: %s", exc, exc_info=True
            )
        while True:
            await asyncio.sleep(self.roster_refresh_interval)
            try:
                await self.refresh_live_roster()
                await self.prune_departed_claims()
                await self.sync_guild_role()
                await self._notify_duo_refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.error(
                    "[WoWCog] Roster refresh / role sync failed: %s",
                    exc,
                    exc_info=True,
                )

    async def refresh_live_roster(self) -> bool:
        """Pull the guild roster and update only the live snapshot.

        No per-character profile calls, no activity detection, no posting —
        this is the cheap path that makes new chars claimable between digests.
        Returns ``True`` if the snapshot was updated, ``False`` if the response
        looked unsafe (see :meth:`_roster_response_unsafe`) and was skipped.
        """
        previous = await self.data.get_snapshot()
        async with wow_api_session() as session:
            current = await self.fetch_roster(session=session)
        if self._roster_response_unsafe(previous, current):
            logger.warning(
                "[WoWCog] Live-Roster-Response unplausibel (%d → %d). "
                "Snapshot nicht aktualisiert.",
                len(previous),
                len(current),
            )
            return False
        await self.data.refresh_live_snapshot(current)
        return True

    def _guild_role(self) -> tuple[discord.Guild, discord.Role] | None:
        """Resolve the main guild and the managed role, or ``None`` if missing."""
        guild = getattr(self.bot, "main_guild", None)
        if not isinstance(guild, discord.Guild):
            logger.warning("[WoWCog] Guild not ready for role sync.")
            return None
        role = guild.get_role(GUILD_ROLE_ID)
        if role is None:
            logger.warning("[WoWCog] Guild role %s not found.", GUILD_ROLE_ID)
            return None
        return guild, role

    async def sync_guild_role(self) -> "RoleSyncResult":
        """Full reconcile: role membership == set of verified-claim owners.

        Adds the role to every entitled member that lacks it and strips it from
        every current holder that is no longer entitled. Stripping requires the
        member cache to be populated (Server Members Intent); without it
        ``role.members`` is incomplete and the cleanup direction is a no-op.
        """
        resolved = self._guild_role()
        if resolved is None:
            return RoleSyncResult(eligible=0, granted=0, removed=0, available=False)
        guild, role = resolved
        eligible = await self.data.role_eligible_user_ids()
        granted = 0
        removed = 0

        for user_id in eligible:
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except discord.NotFound:
                    continue
                except discord.HTTPException as exc:
                    logger.info("[WoWCog] Could not fetch member %s: %s", user_id, exc)
                    continue
            if role not in member.roles:
                await self._add_role(member, role)
                granted += 1

        # Guard against catastrophic mass-removal: if the roster snapshot is
        # empty (failed fetch, fresh DB) the eligible set is empty too, which
        # would otherwise strip the role from everyone. Skip the strip pass in
        # that state — grants are safe, removals are not.
        if await self.data.member_count() == 0:
            logger.warning("[WoWCog] Roster snapshot empty — skipping role strip pass.")
        else:
            for member in list(role.members):
                if member.id not in eligible:
                    await self._remove_role(member, role)
                    removed += 1

        return RoleSyncResult(
            eligible=len(eligible), granted=granted, removed=removed, available=True
        )

    async def reconcile_guild_role_for(self, user_id: int) -> None:
        """Reconcile the managed role for a single user (verify/release hook).

        Cheap, event-driven companion to :meth:`sync_guild_role`: grants the
        role if the user now owns a verified claim, removes it otherwise. Works
        without the member cache because the user ID is already known.
        """
        resolved = self._guild_role()
        if resolved is None:
            return
        guild, role = resolved
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                return
            except discord.HTTPException as exc:
                logger.info("[WoWCog] Could not fetch member %s: %s", user_id, exc)
                return
        eligible = user_id in await self.data.role_eligible_user_ids()
        has_role = any(r.id == GUILD_ROLE_ID for r in member.roles)
        if eligible and not has_role:
            await self._add_role(member, role)
        elif not eligible and has_role:
            await self._remove_role(member, role)

    async def _add_role(self, member: discord.Member, role: discord.Role) -> None:
        try:
            await member.add_roles(role, reason="Verifizierter WoW-Char-Claim")
            logger.info(
                "[WoWCog] Granted guild role to %s (%s).",
                member.display_name,
                member.id,
            )
        except discord.Forbidden:
            logger.warning(
                "[WoWCog] No permission to grant guild role to %s.", member.id
            )
        except discord.HTTPException as exc:
            logger.warning(
                "[WoWCog] Could not grant guild role to %s: %s", member.id, exc
            )

    async def _remove_role(self, member: discord.Member, role: discord.Role) -> None:
        try:
            await member.remove_roles(role, reason="Kein verifizierter WoW-Char-Claim")
            logger.info(
                "[WoWCog] Removed guild role from %s (%s).",
                member.display_name,
                member.id,
            )
        except discord.Forbidden:
            logger.warning(
                "[WoWCog] No permission to remove guild role from %s.", member.id
            )
        except discord.HTTPException as exc:
            logger.warning(
                "[WoWCog] Could not remove guild role from %s: %s", member.id, exc
            )

    def _seconds_until_next_digest(self, now: datetime | None = None) -> float:
        now = self._digest_now(now)
        target = now.replace(
            hour=DEFAULT_DIGEST_HOUR, minute=0, second=0, microsecond=0
        )
        if now >= target:
            target += timedelta(days=1)
        return max((target - now).total_seconds(), 1)

    def _digest_now(self, now: datetime | None = None) -> datetime:
        if now is None:
            return datetime.now(DIGEST_TIMEZONE)
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc).astimezone(DIGEST_TIMEZONE)
        return now.astimezone(DIGEST_TIMEZONE)

    async def _scheduled_digest_due(self, now: datetime | None = None) -> bool:
        now = self._digest_now(now)
        target = now.replace(
            hour=DEFAULT_DIGEST_HOUR, minute=0, second=0, microsecond=0
        )
        if now < target:
            return False
        last_scan_at = await self.data.last_scan_at()
        if not last_scan_at:
            return True
        try:
            last_scan = datetime.fromisoformat(last_scan_at)
        except ValueError:
            return True
        last_scan = self._digest_now(last_scan)
        return last_scan < target

    async def _run_scheduled_digest(self) -> None:
        channel_id = await self.data.get_setting("announcement_channel_id")
        if channel_id:
            await self.scan(post=True, persist=True)

    async def _auto_publish_panel(self) -> None:
        channel = self.bot.get_channel(DEFAULT_PANEL_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            logger.warning(
                "[WoWCog] Panel-Channel %s nicht gefunden.", DEFAULT_PANEL_CHANNEL_ID
            )
            return
        try:
            result = await self.publish_panel(channel)
            action = "erstellt" if result.created else "aktualisiert"
            logger.info(
                "[WoWCog] WoW-Panel beim Start %s (Message %s).",
                action,
                result.message_id,
            )
        except Exception as exc:
            logger.error("[WoWCog] Auto-Publish fehlgeschlagen: %s", exc, exc_info=True)

    async def publish_dungeons_guide(self) -> None:
        """Create or refresh the pinned how-to post in the dungeons forum.

        Purely informational (see ``DUNGEONS_GUIDE_TEXT``) — no tags, no
        buttons, nothing interactive, since the actual event creation
        happens entirely inside the third-party Raid-Helper bot. Mirrors
        Duo's/Marketplace's "edit the tracked post in place, else create a
        new one" hub pattern, using WoWData's generic settings store since
        this is a single string, not worth a dedicated table.
        """
        channel = self.bot.get_channel(DUNGEONS_FORUM_CHANNEL_ID)
        if not isinstance(channel, discord.ForumChannel):
            logger.warning(
                "[WoWCog] Dungeons-Forum %s nicht gefunden.",
                DUNGEONS_FORUM_CHANNEL_ID,
            )
            return
        guide_id = await self.data.get_setting("dungeons_guide_thread_id")
        if guide_id:
            try:
                thread = self.bot.get_channel(
                    int(guide_id)
                ) or await self.bot.fetch_channel(int(guide_id))
                if isinstance(thread, discord.Thread):
                    await thread.get_partial_message(int(guide_id)).edit(
                        content=DUNGEONS_GUIDE_TEXT
                    )
                    await self._try_pin_thread(thread)
                    return
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.info(
                    "[WoWCog] Bestehende Dungeons-Anleitung nicht editierbar — neu."
                )
        created = await channel.create_thread(
            name="⚔️ Dungeon-Run erstellen – Anleitung", content=DUNGEONS_GUIDE_TEXT
        )
        await self.data.set_setting("dungeons_guide_thread_id", str(created.thread.id))
        await self._try_pin_thread(created.thread)

    async def _try_pin_thread(self, thread: discord.Thread) -> None:
        try:
            await thread.edit(pinned=True)
        except (discord.Forbidden, discord.HTTPException, TypeError):
            pass

    async def _auto_publish_dungeons_guide(self) -> None:
        try:
            await self.publish_dungeons_guide()
            logger.info("[WoWCog] Dungeons-Anleitung beim Start veröffentlicht.")
        except Exception as exc:
            logger.error(
                "[WoWCog] Dungeons-Anleitung-Publish fehlgeschlagen: %s",
                exc,
                exc_info=True,
            )

    async def set_announcement_channel(self, channel_id: int) -> None:
        await self.data.set_setting("announcement_channel_id", str(channel_id))

    async def get_announcement_channel_id(self) -> int | None:
        value = await self.data.get_setting("announcement_channel_id")
        return int(value) if value else None

    async def get_claim_review_channel_id(self) -> int:
        value = await self.data.get_setting("claim_review_channel_id")
        return int(value) if value else DEFAULT_CLAIM_REVIEW_CHANNEL_ID

    async def build_panel_stats_line(self) -> str:
        """Live dashboard line for the hub header, refreshed on publish."""
        member_count = await self.data.member_count()
        claims = len(await self.data.list_claims("all"))
        ghosts = len(await self.data.ghost_members())
        running = await self.data.active_cooldown_count()
        return (
            f"📊 **{member_count}** Member · **{claims}** Chars geclaimt · "
            f"**{ghosts}** Geister · **{running}** Cooldowns laufen"
        )

    async def publish_panel(self, channel: discord.TextChannel) -> PanelPublishResult:
        """Send or update the Components-V2 hub message.

        Discord requires V2 messages to have no ``content`` — all visible
        text lives inside the ``LayoutView``'s ``TextDisplay`` items.
        Editing an existing classic-V1 panel into a V2 LayoutView works
        in discord.py 2.6+; if the old message can't be edited (deleted,
        wrong permissions, etc.) we fall back to a fresh send.
        """
        stats = await self.build_panel_stats_line()
        view = WoWPanelLayoutView(self, hub_text=f"{PANEL_HUB_TEXT}\n\n{stats}")
        message_id_value = await self.data.get_setting("panel_message_id")
        if message_id_value:
            try:
                message = await channel.fetch_message(int(message_id_value))
                await message.edit(content=None, view=view)
                await self.data.set_setting("panel_channel_id", str(channel.id))
                return PanelPublishResult(channel.id, message.id, created=False)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.info("[WoWCog] Existing WoW panel message not editable.")
            except AttributeError:
                logger.info("[WoWCog] Channel does not support fetching panel message.")

        message = await channel.send(view=view)
        await self.data.set_setting("panel_channel_id", str(channel.id))
        await self.data.set_setting("panel_message_id", str(message.id))
        return PanelPublishResult(channel.id, message.id, created=True)

    def format_panel_content(self) -> str:
        # Legacy helper retained for any callers/tests; the V2 hub builds
        # its own TextDisplays inside WoWPanelLayoutView. Kept here so
        # existing imports don't break.
        return "\n".join(
            [
                "**🪷 Black Lotus WoW-Hub**",
                "",
                (
                    "Hier verbindest du deine Charaktere, pflegst Berufe "
                    "und findest Crafter in der Gilde."
                ),
                "",
                "**Char claimen** — verbinde deinen Black-Lotus-Char mit deinem Discord-Account.",
                "**Berufe pflegen** — trage Beruf, Skill und Spezialisierung ein.",
                "**Rezepte pflegen** — halte deine gelernten Spezialrezepte aktuell.",
                "**Crafter suchen** — finde, wer in der Gilde ein bestimmtes Item herstellen kann.",
            ]
        )

    async def fetch_roster(self, session=None) -> list[RosterMember]:
        raw_members = await fetch_guild_roster(
            self.realm_slug,
            self.guild_slug,
            namespace=self.namespace,
            locale=self.locale,
            session=session,
        )
        members = [parse_roster_member(raw) for raw in raw_members]
        return [member for member in members if member is not None]

    async def scan(self, *, post: bool = True, persist: bool = True) -> ScanResult:
        """Fetch the roster, detect activity, optionally post and persist it."""
        async with self._scan_lock:
            # Diff against the frozen daily baseline, not the live snapshot —
            # the latter is refreshed hourly for the claim flow and would
            # otherwise have already absorbed today's level-ups/joins/deaths.
            previous = await self.data.get_digest_baseline()
            async with wow_api_session() as session:
                current = await self.fetch_roster(session=session)
                if self._roster_response_unsafe(previous, current):
                    logger.warning(
                        "[WoWCog] Roster-Response unplausibel (%d → %d). Scan ohne Persistenz.",
                        len(previous),
                        len(current),
                    )
                    return ScanResult(
                        member_count=len(previous),
                        milestones=[],
                        new_members=[],
                        deaths=[],
                        officer_notes=[],
                        recipe_events=[],
                        gear_events=[],
                        skill_events=[],
                        cooldowns_ready=[],
                        posted=0,
                    )
                await self._refresh_member_profiles(current, session=session)
                activity = await self._detect_activity(
                    previous, current, session=session
                )
            posted = 0

            if post and activity.public_count:
                posted = await self._post_activity_digest(activity)
                if posted:
                    await self._record_public_events(activity)
                else:
                    persist = False

            if post and activity.officer_notes:
                await self._post_officer_notes(activity.officer_notes)

            # Daily officer reconcile report: claim ⇄ in-game rank mismatches
            # (Initiate-but-claimed, Member+-without-claim, >1 Member+ per user).
            # A recurring status list, not an event — reposted every run until
            # the discrepancies are resolved.
            if post:
                await self._post_sync_report(current)

            if persist:
                await self.data.replace_snapshot(current)
                await self.data.mark_scanned()
                # Refresh the hub panel so its dashboard stats line stays
                # current. Fire-and-forget — a missing panel channel only
                # logs a warning inside _auto_publish_panel.
                self._track_task(self._auto_publish_panel())

            return ScanResult(
                member_count=len(current),
                milestones=activity.milestones,
                new_members=activity.new_members,
                deaths=activity.deaths,
                officer_notes=activity.officer_notes,
                recipe_events=activity.recipe_events or [],
                gear_events=activity.gear_events or [],
                skill_events=activity.skill_events or [],
                cooldowns_ready=activity.cooldowns_ready or [],
                posted=posted,
            )

    @staticmethod
    def _roster_response_unsafe(
        previous: dict[str, RosterMember], current: list[RosterMember]
    ) -> bool:
        """Reject obviously broken roster responses to protect the snapshot.

        Blizzard occasionally returns a partial or empty member list during
        outages — overwriting the snapshot with that would mark everyone as
        new on the next scan. The guard only kicks in for guilds of a real
        size (>5 previous members), so small test rosters and edge cases
        (last member leaves a 2-person guild) aren't blocked.
        """
        if len(previous) <= 5:
            return False
        if not current:
            return True
        return len(current) < len(previous) // 2

    async def _detect_activity(
        self,
        previous: dict[str, RosterMember],
        current: list[RosterMember],
        *,
        session=None,
    ) -> ActivityDiff:
        current_by_key = {member.character_key: member for member in current}
        new_members = [
            member for member in current if member.character_key not in previous
        ]
        milestones = await self._detect_milestones(previous, current)
        deaths = await self._detect_roster_deaths(previous, current)
        missing_deaths, officer_notes = await self._inspect_missing_members(
            previous, current_by_key, session=session
        )
        deaths.extend(missing_deaths)
        recipe_events = await self.data.pending_recipe_learning_events()
        gear_events = await self.data.pending_gear_milestone_events()
        skill_events = await self.data.pending_skill_milestone_events()
        cooldowns_ready = await self._cooldowns_ready_in_next_24h()
        return ActivityDiff(
            new_members=new_members,
            milestones=milestones,
            deaths=deaths,
            officer_notes=officer_notes,
            recipe_events=recipe_events,
            gear_events=gear_events,
            skill_events=skill_events,
            cooldowns_ready=cooldowns_ready,
        )

    async def _cooldowns_ready_in_next_24h(self) -> list[Cooldown]:
        """Cooldowns that fire from now until 24h ahead.

        Used for the daily digest. Window starts at "now" so freshly-ready
        CDs still appear on the day they unlock; ends 24h later so we don't
        spam multi-day previews.
        """
        now = datetime.now(timezone.utc)
        end = now + timedelta(hours=24)
        return await self.data.cooldowns_ready_in_window(
            now.isoformat(), end.isoformat()
        )

    async def _refresh_member_profiles(
        self, members: list[RosterMember], *, session=None
    ) -> None:
        """Patch ghost state and gear from the per-character profile endpoint.

        The guild roster endpoint omits ``is_ghost`` and gear info, so we hit
        the profile endpoint per member with bounded concurrency. One call
        gives us both the death signal and ``equipped_item_level`` — no
        equipment endpoint or local item-id lookup needed. Per-member failures
        are logged but never block the scan; affected members keep their
        roster defaults (alive, no fresh gear snapshot).
        """
        if not members:
            return
        semaphore = asyncio.Semaphore(GHOST_REFRESH_CONCURRENCY)

        async def refresh_one(member: RosterMember) -> None:
            async with semaphore:
                try:
                    profile = await fetch_character_profile(
                        member.realm_slug,
                        member.name,
                        namespace=self.namespace,
                        locale=self.locale,
                        session=session,
                    )
                except WoWAPIError as exc:
                    logger.info(
                        "[WoWCog] Profile für %s nicht abrufbar: %s",
                        member.name,
                        exc,
                    )
                    return
                except Exception as exc:
                    logger.info(
                        "[WoWCog] Profile-Abfrage für %s fehlgeschlagen: %s",
                        member.name,
                        exc,
                    )
                    return
                if not isinstance(profile, dict):
                    return
                if profile.get("is_ghost"):
                    member.is_ghost = True
                if member.level >= 60:
                    await self._apply_gear_from_profile(member, profile)

        await asyncio.gather(*(refresh_one(member) for member in members))

    async def _apply_gear_from_profile(
        self, member: RosterMember, profile: dict
    ) -> None:
        raw = profile.get("equipped_item_level")
        if not isinstance(raw, (int, float)):
            return
        average_item_level = float(raw)
        previous = await self.data.gear_snapshot(member.character_key)
        await self.data.set_gear_snapshot(member.character_key, average_item_level, 0)
        if previous is None or member.is_ghost:
            return
        for threshold in sorted(ITEM_LEVEL_MILESTONES):
            if (
                previous.average_item_level < threshold <= average_item_level
                and not await self.data.gear_milestone_exists(
                    member.character_key, threshold
                )
            ):
                await self.data.record_gear_milestone(
                    member.character_key,
                    threshold,
                    average_item_level,
                    ITEM_LEVEL_MILESTONE_POINTS.get(threshold, 0),
                )

    async def _detect_milestones(
        self,
        previous: dict[str, RosterMember],
        current: list[RosterMember],
    ) -> list[Milestone]:
        milestones: list[Milestone] = []
        for member in current:
            old = previous.get(member.character_key)
            if not old or member.level <= old.level:
                continue
            for level in sorted(MILESTONE_LEVELS):
                if (
                    old.level < level <= member.level
                    and not await self.data.milestone_exists(
                        member.character_key, level
                    )
                ):
                    milestones.append(Milestone(member=member, level=level))
        return milestones

    async def _detect_roster_deaths(
        self,
        previous: dict[str, RosterMember],
        current: list[RosterMember],
    ) -> list[DeathEvent]:
        deaths: list[DeathEvent] = []
        for member in current:
            old = previous.get(member.character_key)
            if (
                member.is_ghost
                and (old is None or not old.is_ghost)
                and not await self.data.death_exists(member.character_key)
            ):
                deaths.append(
                    DeathEvent(
                        member,
                        average_item_level=await self._member_average_item_level(
                            member.character_key
                        ),
                    )
                )
        return deaths

    async def _inspect_missing_members(
        self,
        previous: dict[str, RosterMember],
        current_by_key: dict[str, RosterMember],
        *,
        session=None,
    ) -> tuple[list[DeathEvent], list[OfficerNote]]:
        deaths: list[DeathEvent] = []
        notes: list[OfficerNote] = []
        for member in previous.values():
            if member.character_key in current_by_key:
                continue
            if await self.data.death_exists(member.character_key):
                continue
            if await self.data.officer_note_exists(member.character_key):
                # Already announced as "left guild" — don't re-notify even if
                # the snapshot wasn't replaced (e.g. previous digest failed).
                continue
            state, profile = await self._inspect_missing_profile(
                member, session=session
            )
            average_item_level = await self._member_average_item_level(
                member.character_key
            )
            if state == "dead":
                deaths.append(
                    DeathEvent(
                        member, confirmed=True, average_item_level=average_item_level
                    )
                )
            elif state == "alive":
                notes.append(
                    OfficerNote(
                        member,
                        await self._format_officer_note(
                            member, profile, average_item_level
                        ),
                    )
                )
            else:
                deaths.append(
                    DeathEvent(
                        member, confirmed=False, average_item_level=average_item_level
                    )
                )
        return deaths, notes

    async def _format_officer_note(
        self,
        member: RosterMember,
        profile: dict,
        average_item_level: float | None,
    ) -> str:
        """Build an enriched officer-channel message for a left-the-guild char.

        Includes identity (level/race/class/ilvl), the claim owner (if any)
        and the character's current guild status so officers have full
        context without having to look anything up.
        """
        header = f"**{member.name}** ist nicht mehr Teil von {self.guild_name}."

        identity = self._format_roster_line(member)
        if average_item_level is not None:
            identity += f" (Ø iLvl: **{self._format_item_level(average_item_level)}**)"

        claim = await self.data.get_claim(member.character_key)
        if claim is not None:
            status = "bestätigt" if claim.status == "verified" else "ungeprüft"
            claim_line = f"Claim: <@{claim.discord_user_id}> ({status})"
        else:
            claim_line = "Claim: _nicht geclaimed_"

        new_guild = self._profile_guild_name(profile)
        if new_guild and new_guild.casefold() != self.guild_name.casefold():
            guild_line = f"Aktuelle Gilde: **{new_guild}**"
        else:
            guild_line = "Aktuelle Gilde: _gildenlos_"

        # First-seen-in-our-roster timestamp. Wording is deliberately
        # "Bei uns gelistet seit" (not "im Roster seit") because for chars
        # that were already in the guild when the bot first scanned, the
        # date is just the bot-tracking start, not the actual guild join.
        joined = await self.data.first_seen_at(member.character_key)
        lines = [header, f"• {identity}", f"• {claim_line}", f"• {guild_line}"]
        if joined:
            lines.append(f"• Bei uns gelistet seit: **{joined[:10]}**")

        return "\n".join(lines)

    async def _member_average_item_level(self, character_key: str) -> float | None:
        snapshot = await self.data.gear_snapshot(character_key)
        return snapshot.average_item_level if snapshot else None

    async def _profile_life_state(self, member: RosterMember, *, session=None) -> str:
        state, _ = await self._inspect_missing_profile(member, session=session)
        return state

    async def _inspect_missing_profile(
        self, member: RosterMember, *, session=None
    ) -> tuple[str, dict]:
        """Return ``(life_state, profile)`` for a vanished roster member.

        ``life_state`` is ``"dead"``, ``"alive"`` or ``"unknown"``. ``profile``
        is the raw Blizzard payload (or an empty dict on failure) so callers
        can extract extra context like the current guild without a second
        API call.
        """
        try:
            profile = await fetch_character_profile(
                member.realm_slug,
                member.name,
                namespace=self.namespace,
                locale=self.locale,
                session=session,
            )
        except WoWAPIError as exc:
            logger.info(
                "[WoWCog] Could not inspect missing character %s: %s",
                member.name,
                exc,
            )
            return "unknown", {}
        except Exception as exc:
            logger.info(
                "[WoWCog] Profile inspection failed for %s: %s",
                member.name,
                exc,
                exc_info=True,
            )
            return "unknown", {}
        state = "dead" if self._profile_is_dead(profile) else "alive"
        return state, profile or {}

    def _profile_is_dead(self, profile: dict) -> bool:
        return bool(
            profile.get("is_ghost")
            or profile.get("is_dead")
            or profile.get("dead")
            or profile.get("ghost")
        )

    def _profile_guild_name(self, profile: dict) -> str | None:
        guild = profile.get("guild") if profile else None
        if not isinstance(guild, dict):
            return None
        name = guild.get("name")
        return name if isinstance(name, str) and name.strip() else None

    async def _post_activity_digest(self, activity: ActivityDiff) -> int:
        channel_id = await self.get_announcement_channel_id()
        if channel_id is None:
            logger.warning("[WoWCog] No announcement channel configured.")
            return 0
        channel = self.bot.get_channel(channel_id)
        if not channel:
            logger.warning("[WoWCog] Announcement channel %s not found.", channel_id)
            return 0

        sections = await self._digest_sections(activity)
        chunks = _pack_digest_sections_into_chunks(sections)
        posted = 0
        for chunk in chunks:
            try:
                await channel.send(chunk)
            except discord.Forbidden:
                logger.warning(
                    "[WoWCog] Missing access to announcement channel %s.",
                    channel_id,
                )
                return posted
            except discord.HTTPException as exc:
                logger.warning(
                    "[WoWCog] Could not post activity digest chunk to channel %s: %s",
                    channel_id,
                    exc,
                    exc_info=True,
                )
                return posted
            posted += 1
        return posted

    async def _post_milestones(self, milestones: list[Milestone]) -> int:
        activity = ActivityDiff(
            new_members=[], milestones=milestones, deaths=[], officer_notes=[]
        )
        return await self._post_activity_digest(activity)

    async def _post_officer_notes(self, notes: list[OfficerNote]) -> int:
        channel_id = await self.get_claim_review_channel_id()
        channel = self.bot.get_channel(channel_id)
        if not channel:
            logger.warning("[WoWCog] Claim review channel %s not found.", channel_id)
            return 0
        posted = 0
        for note in notes:
            try:
                await channel.send(f"⚠️ {note.message}")
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning(
                    "[WoWCog] Could not post officer note to channel %s: %s",
                    channel_id,
                    exc,
                )
                break
            # Persist immediately so a later scan failure can't cause a
            # duplicate "X ist nicht mehr Teil von …" message.
            await self.data.record_officer_note(note.member.character_key)
            posted += 1
        return posted

    def _rank_label(self, rank: int | None) -> str:
        if rank is None:
            return "kein Rang"
        return GUILD_RANKS.get(rank, f"Rang {rank}")

    def _main_guild(self) -> "discord.Guild | None":
        guild = getattr(self.bot, "main_guild", None)
        return guild if isinstance(guild, discord.Guild) else None

    async def _discord_member_present(
        self, guild: "discord.Guild", user_id: int
    ) -> bool:
        """Whether ``user_id`` is still a member of the Discord guild.

        Only returns ``False`` on a confirmed 404 (member left). Cache misses
        are double-checked via ``fetch_member``; any other error is treated as
        "present" so a transient hiccup never wrongly flags someone as gone.
        """
        if guild.get_member(user_id) is not None:
            return True
        try:
            await guild.fetch_member(user_id)
            return True
        except discord.NotFound:
            return False
        except discord.HTTPException:
            return True

    async def _remove_user_claims(self, user_id: int) -> list[CharacterClaim]:
        """Delete all of a user's char claims; return what was removed."""
        claims = await self.data.claims_for_user(user_id)
        for claim in claims:
            await self.data.remove_claim(claim.character_key)
        if claims:
            # Best-effort role cleanup; a no-op if the user is already gone.
            await self.reconcile_guild_role_for(user_id)
        return claims

    async def prune_departed_claims(self) -> int:
        """Drop claims whose Discord owner has left the server (self-healing).

        Runs on the hourly reconcile and catches anyone who left while the bot
        was offline (``on_member_remove`` handles the live case). A departed
        user's claim is meaningless — removing it lets the now-ownerless char
        surface as unclaimed instead of forever showing ``@unknown-user``.
        Officers get a one-time note so they can demote the freed chars in-game.
        """
        guild = self._main_guild()
        if guild is None:
            return 0
        by_user: dict[int, list[CharacterClaim]] = {}
        for claim in await self.data.list_claims("all"):
            by_user.setdefault(claim.discord_user_id, []).append(claim)

        removed: list[tuple[int, list[CharacterClaim]]] = []
        for user_id, user_claims in by_user.items():
            if await self._discord_member_present(guild, user_id):
                continue
            await self._remove_user_claims(user_id)
            removed.append((user_id, user_claims))

        if not removed:
            return 0
        total = sum(len(claims) for _, claims in removed)
        logger.info("[WoWCog] Pruned %d claim(s) from departed Discord users.", total)
        await self._post_departed_claims_note(removed)
        return total

    async def _post_departed_claims_note(
        self, removed: list[tuple[int, list[CharacterClaim]]]
    ) -> None:
        """One officer note listing auto-removed claims + ranks to demote."""
        channel_id = await self.get_claim_review_channel_id()
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return
        snapshot = await self.data.get_snapshot()
        body: list[str] = []
        for user_id, user_claims in removed:
            parts = []
            for claim in sorted(user_claims, key=lambda c: c.character_name.casefold()):
                snap = snapshot.get(claim.character_key)
                if snap is not None and snap.guild_rank is not None:
                    parts.append(
                        f"**{claim.character_name}** ({self._rank_label(snap.guild_rank)})"
                    )
                else:
                    parts.append(f"**{claim.character_name}**")
            body.append(f"- <@{user_id}>: " + ", ".join(parts))
        header = (
            "🚪 **Claims automatisch entfernt** — diese User haben Discord "
            "verlassen. Bitte Member+-Chars in-game runterstufen:"
        )
        for chunk in _pack_digest_sections_into_chunks([(header, body)]):
            try:
                await channel.send(chunk)
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning(
                    "[WoWCog] Could not post departed-claims note to %s: %s",
                    channel_id,
                    exc,
                )
                return

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Live counterpart to :meth:`prune_departed_claims` — drop a leaving
        member's claims the moment they leave the Discord server."""
        main_guild_id = getattr(self.bot, "main_guild_id", None)
        if main_guild_id is not None and member.guild.id != main_guild_id:
            return
        removed = await self._remove_user_claims(member.id)
        if removed:
            logger.info(
                "[WoWCog] Member %s left Discord — removed %d claim(s).",
                member.id,
                len(removed),
            )
            await self._post_departed_claims_note([(member.id, removed)])

    async def build_sync_report(self, members: list[RosterMember]) -> SyncReport:
        """Compare verified claims against in-game ranks (see :class:`SyncReport`).

        Ghosts and chars no longer in the roster are ignored. "Member or higher"
        is any rank <= ``MEMBER_RANK``; Initiate is ``INITIATE_RANK``.
        """
        members_by_key = {m.character_key: m for m in members if not m.is_ghost}
        verified = await self.data.list_claims("verified")
        claimed_keys = {c.character_key for c in await self.data.list_claims("all")}
        # Registered guild-bank chars are shared and intentionally unclaimed —
        # never flag them for "claim or kick".
        bank_keys = {b.character_key for b in await self.data.list_bank_characters()}

        initiate_claims: list[tuple[CharacterClaim, RosterMember]] = []
        member_claims_by_user: dict[int, list[tuple[CharacterClaim, RosterMember]]] = {}
        # All verified, alive claimed chars per user — drives the "no Main" check.
        chars_by_user: dict[int, list[RosterMember]] = {}
        for claim in verified:
            member = members_by_key.get(claim.character_key)
            if member is None or member.guild_rank is None:
                continue
            if member.guild_rank == INITIATE_RANK:
                initiate_claims.append((claim, member))
            # "Main slot" = Member+ but NOT the sanctioned Officer-Twink alt, so
            # an Offizier main + Officer-Twink second char isn't flagged.
            if (
                member.guild_rank <= MEMBER_RANK
                and member.guild_rank != OFFICER_TWINK_RANK
            ):
                member_claims_by_user.setdefault(claim.discord_user_id, []).append(
                    (claim, member)
                )
            chars_by_user.setdefault(claim.discord_user_id, []).append(member)

        unclaimed_ranked = [
            member
            for member in members_by_key.values()
            if member.guild_rank is not None
            and member.guild_rank <= TWINK_RANK
            and member.character_key not in claimed_keys
            and member.character_key not in bank_keys
        ]
        multi_member_users = [
            (user_id, claims)
            for user_id, claims in member_claims_by_user.items()
            if len(claims) > 1
        ]

        # Standing invariant: a user with a Twink/Bank char but NO Member+ main.
        # Initiate-only owners are new/probation and not flagged.
        no_main_users: list[tuple[int, list[RosterMember]]] = []
        for user_id, chars in chars_by_user.items():
            if any(m.guild_rank <= MEMBER_RANK for m in chars):
                continue
            if not any(m.guild_rank in (BANK_RANK, TWINK_RANK) for m in chars):
                continue
            ordered = sorted(
                chars, key=lambda m: (-m.level, m.guild_rank, m.name.casefold())
            )
            no_main_users.append((user_id, ordered))

        initiate_claims.sort(key=lambda pair: pair[0].character_name.casefold())
        unclaimed_ranked.sort(key=lambda m: (m.guild_rank, m.name.casefold()))
        no_main_users.sort(key=lambda pair: pair[0])
        multi_member_users.sort(key=lambda pair: pair[0])
        return SyncReport(
            initiate_claims=initiate_claims,
            unclaimed_ranked=unclaimed_ranked,
            no_main_users=no_main_users,
            multi_member_users=multi_member_users,
        )

    def _sync_report_sections(
        self, report: SyncReport
    ) -> list[tuple[str | None, list[str]]]:
        sections: list[tuple[str | None, list[str]]] = []
        if report.initiate_claims:
            body = [
                f"- **{member.name}** (Level {member.level}) - "
                f"<@{claim.discord_user_id}>"
                for claim, member in report.initiate_claims
            ]
            sections.append(
                (
                    "**Geclaimt & bestätigt, aber noch auf Initiate** — "
                    "auf Twink/Member hochstufen (NICHT kicken!):",
                    body,
                )
            )
        if report.unclaimed_ranked:
            body = [
                f"- **{member.name}** ({self._rank_label(member.guild_rank)}, "
                f"Level {member.level})"
                for member in report.unclaimed_ranked
            ]
            sections.append(
                (
                    "**Twink+ ohne Claim** — claimen lassen oder auf Initiate "
                    "runterstufen:",
                    body,
                )
            )
        if report.no_main_users:
            body = []
            for user_id, chars in report.no_main_users:
                chars_str = ", ".join(
                    f"**{m.name}** ({self._rank_label(m.guild_rank)}, Level {m.level})"
                    for m in chars
                )
                body.append(f"- <@{user_id}>: {chars_str}")
            sections.append(
                (
                    "**Twink/Bank ohne Main** — höchsten Char (zuerst gelistet) "
                    "zum Member befördern:",
                    body,
                )
            )
        if report.multi_member_users:
            body = []
            for user_id, claims in report.multi_member_users:
                chars = ", ".join(
                    f"**{member.name}** ({self._rank_label(member.guild_rank)})"
                    for _, member in sorted(claims, key=lambda p: p[1].guild_rank)
                )
                body.append(f"- <@{user_id}>: {chars}")
            sections.append(
                (
                    "**Mehr als ein Main-Char** — nur einer soll Member+ sein "
                    "(Officer Twink zählt nicht), Rest → Twink:",
                    body,
                )
            )
        return sections

    async def sync_report_chunks(self, members: list[RosterMember]) -> list[str]:
        """Render the sync report as Discord-postable chunks (empty if clean)."""
        report = await self.build_sync_report(members)
        sections = self._sync_report_sections(report)
        if not sections:
            return []
        header: tuple[str | None, list[str]] = (None, ["🔄 **Offi-Sync-Report**"])
        return _pack_digest_sections_into_chunks([header] + sections)

    async def _post_sync_report(self, members: list[RosterMember]) -> int:
        """Post the sync report to the officer channel (0 if clean/unavailable)."""
        chunks = await self.sync_report_chunks(members)
        if not chunks:
            return 0
        channel_id = await self.get_claim_review_channel_id()
        channel = self.bot.get_channel(channel_id)
        if not channel:
            logger.warning("[WoWCog] Claim review channel %s not found.", channel_id)
            return 0
        posted = 0
        for chunk in chunks:
            try:
                await channel.send(chunk)
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning(
                    "[WoWCog] Could not post sync report to channel %s: %s",
                    channel_id,
                    exc,
                )
                return posted
            posted += 1
        return posted

    async def _record_public_events(self, activity: ActivityDiff) -> None:
        for milestone in activity.milestones:
            await self.data.record_milestone(
                milestone.member.character_key, milestone.level
            )
            await self._award_claimed_milestone_points(milestone)
            # Duo hook AFTER record_milestone so the "both partners reached it"
            # check sees the just-recorded event (timing-independent).
            await self._notify_duo_milestone(milestone)
        for death in activity.deaths:
            await self.data.record_death(death.member.character_key)
            # Auto-release the dead char's claim so the owner can claim a
            # re-rolled character with the same name without first having
            # to manually release. The owner already learns about the
            # death via the digest's @mention.
            claim = await self.data.get_claim(death.member.character_key)
            if claim:
                await self.data.release_claim(
                    death.member.character_key, claim.discord_user_id
                )
                # Losing the last claimed char must cost the guild role; the
                # hourly reconcile would catch it too, but do it immediately.
                await self.reconcile_guild_role_for(claim.discord_user_id)
                # If a Member+ main fell, alert officers to promote a
                # replacement so the owner isn't left with only twinks/bank.
                await self._notify_main_died(claim.discord_user_id, death.member)
            # Duo hook: memorial in the team thread if the fallen char was in
            # an active team. Independent of the claim (team keeps the key).
            await self._notify_duo_death(death)
        for event in activity.recipe_events or []:
            await self.data.mark_recipe_learning_announced(
                event.character_key, event.spell_id
            )
            await self._award_recipe_learning_points(event)
        for event in activity.gear_events or []:
            await self.data.mark_gear_milestone_announced(
                event.character_key, event.threshold
            )
            await self._award_gear_milestone_points(event)
        for event in activity.skill_events or []:
            await self.data.mark_skill_milestone_announced(
                event.character_key, event.profession_id, event.threshold
            )
            await self._award_skill_milestone_points(event)
        # Cooldowns are read-only in the digest — no DB write needed here.
        await self._retry_unawarded_pending_events()

    def _duo_cog(self):
        get_cog = getattr(self.bot, "get_cog", None)
        return get_cog("DuoCog") if get_cog else None

    def _marketplace_cog(self):
        get_cog = getattr(self.bot, "get_cog", None)
        return get_cog("MarketplaceCog") if get_cog else None

    async def character_display_label(self, character_key: str) -> str:
        """Name + guild-role context, e.g. "Voidok (Twink von Lyxendra)" for
        a Bank/Twink char, or just the plain name for a Main/Officer/etc.

        Used wherever a character is attributed for a reader who doesn't
        necessarily know the guild roster (e.g. Marketplace posts) — shows
        who to actually message, not just an anonymous Discord mention.
        "Main" is the same "Member+ but not the sanctioned Officer-Twink"
        rule ``build_sync_report`` already uses for the main-slot check.
        """
        claim = await self.data.get_claim(character_key)
        if claim is None:
            return character_key
        snapshot = await self.data.get_snapshot()
        member = snapshot.get(character_key)
        if member is None or member.guild_rank not in (BANK_RANK, TWINK_RANK):
            return claim.character_name

        relation = "Bank" if member.guild_rank == BANK_RANK else "Twink"
        siblings = await self.data.claims_for_user(claim.discord_user_id)
        main_candidates = [
            sib_member
            for sibling in siblings
            if (sib_member := snapshot.get(sibling.character_key)) is not None
            and sib_member.guild_rank <= MEMBER_RANK
            and sib_member.guild_rank != OFFICER_TWINK_RANK
        ]
        if not main_candidates:
            return f"{claim.character_name} ({relation})"
        main = min(main_candidates, key=lambda m: m.guild_rank)
        return f"{claim.character_name} ({relation} von {main.name})"

    def crafting_search_response_view(
        self, result: CraftingSearchResult, owner_user_id: int
    ) -> discord.ui.View | None:
        """The optional follow-up view for a crafting-search result: the
        item-disambiguation picker, or a "Marktplatz-Anfrage erstellen"
        bridge button for a resolved result. ``None`` for not-found statuses.
        """
        if result.status == "ambiguous_item":
            return CraftingSearchSuggestionView(
                self, owner_user_id, result.candidates or []
            )
        if result.status in ("ok", "no_crafter", "manual_recipe"):
            return CraftingSearchMarketplaceActionsView(self, owner_user_id, result)
        return None

    async def _notify_duo_milestone(self, milestone: Milestone) -> None:
        """Fire-and-forget: let the Duo cog celebrate a team level-milestone."""
        duo = self._duo_cog()
        if duo is None or not hasattr(duo, "on_character_milestone"):
            return
        try:
            await duo.on_character_milestone(
                milestone.member.character_key, milestone.level
            )
        except Exception as exc:  # pragma: no cover - defensive integration logging
            logger.warning("[WoWCog] Duo-Milestone-Hook fehlgeschlagen: %s", exc)

    async def _notify_duo_death(self, death: DeathEvent) -> None:
        """Fire-and-forget: let the Duo cog post a memorial for a fallen partner."""
        duo = self._duo_cog()
        if duo is None or not hasattr(duo, "on_character_death"):
            return
        try:
            await duo.on_character_death(death.member.character_key, death.member.level)
        except Exception as exc:  # pragma: no cover - defensive integration logging
            logger.warning("[WoWCog] Duo-Death-Hook fehlgeschlagen: %s", exc)

    async def _notify_main_died(self, owner_id: int, dead_member: RosterMember) -> None:
        """Alert officers when a Member+ main dies and the owner is left with
        only twinks/bank — so a replacement can be promoted to Member.

        The invariant is: nobody keeps a Twink/Bank char without a Main. Fires
        only when, after the death, the owner still has claimed chars, none is
        Member+, and at least one is a Twink or Bank (Initiate-only survivors
        are new/probation and not flagged).
        """
        if dead_member.guild_rank is None or dead_member.guild_rank > MEMBER_RANK:
            return
        snapshot = await self.data.get_snapshot()
        remaining: list[RosterMember] = []
        for claim in await self.data.claims_for_user(owner_id):
            member = snapshot.get(claim.character_key)
            if member is None or member.is_ghost or member.guild_rank is None:
                continue
            remaining.append(member)
        if not remaining:
            return
        if any(m.guild_rank <= MEMBER_RANK for m in remaining):
            return  # still has a Main
        if not any(m.guild_rank in (BANK_RANK, TWINK_RANK) for m in remaining):
            return  # only Initiates left — nothing orphaned yet

        # Highest-level survivor first — the natural new-main candidate.
        remaining.sort(key=lambda m: (-m.level, m.guild_rank, m.name.casefold()))
        top = remaining[0]
        lines = [
            f"⚰️ **Main gefallen** — <@{owner_id}> hat mit "
            f"**{dead_member.name}** ({self._rank_label(dead_member.guild_rank)}) "
            "seinen Main verloren und hat jetzt keinen Member-Char mehr.",
            "Verbleibende Chars:",
        ]
        for member in remaining:
            hint = "  ← **Vorschlag: zum Member befördern**" if member is top else ""
            lines.append(
                f"- **{member.name}** ({self._rank_label(member.guild_rank)}, "
                f"Level {member.level}){hint}"
            )
        await self._post_officer_text("\n".join(lines))

    async def _post_officer_text(self, content: str) -> None:
        """Send a plain message to the officer/claim-review channel."""
        channel_id = await self.get_claim_review_channel_id()
        channel = self.bot.get_channel(channel_id)
        if not channel:
            logger.warning("[WoWCog] Officer channel %s not found.", channel_id)
            return
        try:
            await channel.send(content)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning(
                "[WoWCog] Could not post officer message to %s: %s", channel_id, exc
            )

    async def _notify_duo_refresh(self) -> None:
        """Hourly: let the Duo cog refresh open posts against the fresh roster."""
        duo = self._duo_cog()
        if duo is None or not hasattr(duo, "refresh_open_posts"):
            return
        try:
            await duo.refresh_open_posts()
        except Exception as exc:  # pragma: no cover - defensive integration logging
            logger.warning("[WoWCog] Duo-Refresh-Hook fehlgeschlagen: %s", exc)

    async def _retry_unawarded_pending_events(self) -> None:
        """Re-fund events that were announced but never landed Champion points.

        A row stuck in "announced, not awarded" means a previous
        ``champion.update_user_score()`` call failed (e.g. SQLite-lock). The
        row is invisible to the digest's ``pending_*_events`` filter because
        ``announced_at`` is already set, so without this loop the points
        would be permanently lost. Each ``_award_*_points`` call is
        idempotent — successful retries mark ``awarded_at`` and exit the
        retry pool; persistent failures stay queued for the next scan.
        """
        for event in await self.data.pending_award_retries_recipe_learning():
            await self._award_recipe_learning_points(event)
        for event in await self.data.pending_award_retries_gear_milestone():
            await self._award_gear_milestone_points(event)
        for event in await self.data.pending_award_retries_skill_milestone():
            await self._award_skill_milestone_points(event)

    async def _award_claimed_milestone_points(self, milestone: Milestone) -> None:
        claim = await self.data.get_claim(milestone.member.character_key)
        if not claim:
            return
        points = CLAIMED_MILESTONE_POINTS.get(milestone.level, 0)
        if points <= 0:
            return
        get_cog = getattr(self.bot, "get_cog", None)
        champion = get_cog("ChampionCog") if get_cog else None
        if champion is None or not hasattr(champion, "update_user_score"):
            logger.info("[WoWCog] ChampionCog not available for milestone bonus.")
            return
        reason = f"WoW-Meilenstein: {milestone.member.name} Level {milestone.level}"
        try:
            await champion.update_user_score(claim.discord_user_id, points, reason)
        except Exception as exc:  # pragma: no cover - defensive integration logging
            logger.warning(
                "[WoWCog] Could not award claimed milestone points: %s",
                exc,
                exc_info=True,
            )

    async def _award_recipe_learning_points(self, event: RecipeLearningEvent) -> None:
        if event.points <= 0:
            return
        get_cog = getattr(self.bot, "get_cog", None)
        champion = get_cog("ChampionCog") if get_cog else None
        if champion is None or not hasattr(champion, "update_user_score"):
            logger.info("[WoWCog] ChampionCog not available for recipe bonus.")
            return
        # CAS-reserve the slot first (`awarded_at IS NULL` guard). If
        # somebody already awarded this event (e.g. a previous scan or a
        # concurrent retry pass), exit silently — no double-vote possible.
        if not await self.data.mark_recipe_learning_awarded(
            event.character_key, event.spell_id
        ):
            return
        recipe = self._recipe_by_spell_id(event.spell_id)
        recipe_name = self._recipe_name(recipe) if recipe else event.spell_id
        reason = f"WoW-Rezept: {event.character_name} lernt {recipe_name}"
        try:
            await champion.update_user_score(
                event.discord_user_id, event.points, reason
            )
        except Exception as exc:  # pragma: no cover - defensive integration logging
            logger.warning(
                "[WoWCog] Could not award recipe learning points: %s",
                exc,
                exc_info=True,
            )
            # Roll back the CAS so the next scan's retry loop can pick
            # it up again. Without this the points would be lost forever.
            await self.data.unmark_recipe_learning_awarded(
                event.character_key, event.spell_id
            )

    async def _award_gear_milestone_points(self, event: GearMilestoneEvent) -> None:
        if event.points <= 0 or event.discord_user_id is None:
            return
        get_cog = getattr(self.bot, "get_cog", None)
        champion = get_cog("ChampionCog") if get_cog else None
        if champion is None or not hasattr(champion, "update_user_score"):
            logger.info("[WoWCog] ChampionCog not available for gear bonus.")
            return
        if not await self.data.mark_gear_milestone_awarded(
            event.character_key, event.threshold
        ):
            return
        reason = (
            f"WoW-iLvl-Meilenstein: {event.character_name} " f"Ø iLvl {event.threshold}"
        )
        try:
            await champion.update_user_score(
                event.discord_user_id, event.points, reason
            )
        except Exception as exc:  # pragma: no cover - defensive integration logging
            logger.warning(
                "[WoWCog] Could not award gear milestone points: %s",
                exc,
                exc_info=True,
            )
            await self.data.unmark_gear_milestone_awarded(
                event.character_key, event.threshold
            )

    async def cooldown_eligible_options(
        self, discord_user_id: int
    ) -> list[tuple[CharacterClaim, str, str, str]]:
        """Return (claim, spell_id, spell_name, group_label) per eligible (char, CD-spell).

        A spell is eligible if the user's claimed character has explicitly
        learned it AND it's in the cooldown spell-to-group lookup.
        """
        eligible: list[tuple[CharacterClaim, str, str, str]] = []
        for claim in await self.data.claims_for_user(discord_user_id):
            known = await self.data.known_recipe_spell_ids(claim.character_key)
            for spell_id in sorted(known):
                if spell_id not in COOLDOWN_SPELL_TO_GROUP:
                    continue
                _, group = COOLDOWN_SPELL_TO_GROUP[spell_id]
                recipe = self._recipe_by_spell_id(spell_id)
                spell_name = self._recipe_name(recipe) if recipe else spell_id
                eligible.append((claim, spell_id, spell_name, group["label"]))
        return eligible

    async def log_cooldown(
        self,
        discord_user_id: int,
        character_key: str,
        spell_id: str,
    ) -> tuple[str, Cooldown | None]:
        """Persist a cooldown for the given user's character + spell.

        Returns ``(status, cooldown)``. Status codes:
        * ``"ok"`` — saved.
        * ``"not_owner"`` — claim belongs to someone else.
        * ``"unknown_spell"`` — spell isn't a tracked CD recipe.
        * ``"recipe_missing"`` — char hasn't learned that recipe.
        """
        if spell_id not in COOLDOWN_SPELL_TO_GROUP:
            return "unknown_spell", None
        claim = await self.data.get_claim(character_key)
        if not claim or claim.discord_user_id != discord_user_id:
            return "not_owner", None
        known = await self.data.known_recipe_spell_ids(character_key)
        if spell_id not in known:
            return "recipe_missing", None
        group_key, group = COOLDOWN_SPELL_TO_GROUP[spell_id]
        recipe = self._recipe_by_spell_id(spell_id)
        spell_name = self._recipe_name(recipe) if recipe else spell_id
        now = datetime.now(timezone.utc)
        ready = now + timedelta(hours=int(group["hours"]))
        await self.data.set_cooldown(
            character_key,
            group_key,
            spell_id,
            spell_name,
            now.isoformat(),
            ready.isoformat(),
        )
        return "ok", Cooldown(
            character_key=character_key,
            character_name=claim.character_name,
            realm_slug=claim.realm_slug,
            discord_user_id=claim.discord_user_id,
            cooldown_group=group_key,
            last_spell_id=spell_id,
            last_spell_name=spell_name,
            used_at=now.isoformat(),
            ready_at=ready.isoformat(),
        )

    async def _award_skill_milestone_points(self, event: ProfessionSkillEvent) -> None:
        if event.points <= 0 or event.discord_user_id is None:
            return
        get_cog = getattr(self.bot, "get_cog", None)
        champion = get_cog("ChampionCog") if get_cog else None
        if champion is None or not hasattr(champion, "update_user_score"):
            logger.info("[WoWCog] ChampionCog not available for skill bonus.")
            return
        if not await self.data.mark_skill_milestone_awarded(
            event.character_key, event.profession_id, event.threshold
        ):
            return
        profession_name = self._profession_name(event.profession_id)
        reason = (
            f"WoW-Berufsskill: {event.character_name} ({profession_name}) "
            f"Skill {event.threshold}"
        )
        try:
            await champion.update_user_score(
                event.discord_user_id, event.points, reason
            )
        except Exception as exc:  # pragma: no cover - defensive integration logging
            logger.warning(
                "[WoWCog] Could not award skill milestone points: %s",
                exc,
                exc_info=True,
            )
            await self.data.unmark_skill_milestone_awarded(
                event.character_key, event.profession_id, event.threshold
            )

    def format_milestone(self, milestone: Milestone) -> str:
        return self._format_milestone_line(milestone, None)

    async def _format_activity_digest_legacy(self, activity: ActivityDiff) -> str:
        lines = [random.choice(DIGEST_OPENERS), ""]
        if activity.new_members:
            lines.append("**Neue Chars**")
            for member in activity.new_members:
                lines.append(f"🆕 {self._format_new_member_line(member)}")
            lines.append("")
        if activity.milestones:
            lines.append("**Level-Meilensteine**")
            for milestone in activity.milestones:
                claim = await self.data.get_claim(milestone.member.character_key)
                lines.append(f"🏅 {self._format_milestone_line(milestone, claim)}")
            lines.append("")
        if activity.deaths:
            lines.append("**Gefallene Chars**")
            for death in activity.deaths:
                lines.append(f"🕯️ {self._format_death_line(death)}")
            lines.append("")
        lines.append(self._digest_closer(activity))
        return "\n".join(lines)

    def _digest_closer(self, activity: ActivityDiff) -> str:
        has_good_news = bool(
            activity.new_members
            or activity.milestones
            or (activity.recipe_events or [])
            or (activity.gear_events or [])
            or (activity.skill_events or [])
            or (activity.cooldowns_ready or [])
        )
        has_deaths = bool(activity.deaths)
        if has_deaths and has_good_news:
            return random.choice(DIGEST_MIXED_CLOSERS)
        if has_deaths:
            return random.choice(DIGEST_DEATH_CLOSERS)
        return random.choice(DIGEST_POSITIVE_CLOSERS)

    def _format_new_member_line(self, member: RosterMember) -> str:
        return (
            "Willkommen bei Black Lotus: "
            f"**{self._display_character(member)}** "
            f"(Level **{member.level}**)."
        )

    def _format_milestone_line(
        self, milestone: Milestone, claim: CharacterClaim | None
    ) -> str:
        character = self._display_character(milestone.member)
        if claim:
            return (
                f"<@{claim.discord_user_id}> hat mit **{character}** "
                f"Level **{milestone.level}** erreicht "
                f"(+{CLAIMED_MILESTONE_POINTS.get(milestone.level, 0)} Champion-Punkte)."
            )
        return f"**{character}** ist auf Level **{milestone.level}** aufgestiegen."

    def _format_death_line(self, death: DeathEvent) -> str:
        character = self._display_character(death.member)
        level = death.member.level
        gear = self._format_death_gear_suffix(death)
        # Confirmed and unconfirmed cases are reported uniformly — both mean
        # the character is gone for good.
        return (
            f"**{character}** ist auf Level **{level}**{gear} gefallen. "
            "Ruhe in Frieden. 🕯️"
        )

    async def _digest_sections(
        self, activity: ActivityDiff
    ) -> list[tuple[str | None, list[str]]]:
        """Return the digest as ordered (header, body_lines) sections.

        ``header`` is ``None`` for headerless blocks (opener, closer).
        Blank-line separators between sections are NOT included — the
        chunker inserts them only where sections actually end up adjacent.
        """
        sections: list[tuple[str | None, list[str]]] = [
            (None, [random.choice(DIGEST_OPENERS)])
        ]

        if activity.new_members:
            body: list[str] = []
            for member in sorted(
                activity.new_members,
                key=lambda item: (-item.level, item.name.casefold()),
            ):
                line = self._format_roster_line(member)
                if member.level >= 60:
                    snapshot = await self.data.gear_snapshot(member.character_key)
                    if snapshot:
                        line = (
                            f"{line}, Ø iLvl "
                            f"**{self._format_item_level(snapshot.average_item_level)}**"
                        )
                claim = await self.data.get_claim(member.character_key)
                if claim:
                    line = f"{line} - <@{claim.discord_user_id}>"
                body.append(f"- {line}")
            sections.append(("**Herzlich willkommen in der Gilde** 👋", body))

        if activity.milestones:
            body = []
            for milestone in sorted(
                activity.milestones,
                key=lambda item: (-item.level, item.member.name.casefold()),
            ):
                claim = await self.data.get_claim(milestone.member.character_key)
                line = self._format_roster_line(milestone.member, level=milestone.level)
                if claim:
                    points = CLAIMED_MILESTONE_POINTS.get(milestone.level, 0)
                    line = (
                        f"{line} - <@{claim.discord_user_id}> "
                        f"(+{points} Champion-Punkte)"
                    )
                body.append(f"- {line}")
            sections.append(("**Glückwunsch zu diesen Meilensteinen** 🏆", body))

        if activity.recipe_events:
            body = []
            for event in sorted(
                activity.recipe_events,
                key=lambda item: (
                    -item.points,
                    self._profession_name(item.profession_id).casefold(),
                    item.character_name.casefold(),
                ),
            ):
                recipe = self._recipe_by_spell_id(event.spell_id)
                recipe_name = self._recipe_name(recipe) if recipe else event.spell_id
                source = self._recipe_source_label(recipe)
                required_skill = int(recipe.get("required_skill") or 0) if recipe else 0
                skill_label = f", ab Skill {required_skill}" if required_skill else ""
                tail_parts = []
                if event.discord_user_id is not None:
                    tail_parts.append(f"<@{event.discord_user_id}>")
                    if event.points > 0:
                        tail_parts.append(f"(+{event.points} Champion-Punkte)")
                tail = f" - {' '.join(tail_parts)}" if tail_parts else ""
                body.append(
                    f"- **{event.character_name}** "
                    f"({self._profession_name(event.profession_id)}{skill_label}): "
                    f"**{recipe_name}** *({event.rarity}, {source})*{tail}"
                )
            sections.append(("**Neue seltene Rezepte in der Gilde** 📖", body))

        if activity.gear_events:
            body = []
            for event in sorted(
                activity.gear_events,
                key=lambda item: (-item.threshold, item.character_name.casefold()),
            ):
                tail_parts: list[str] = []
                if event.discord_user_id is not None:
                    tail_parts.append(f"<@{event.discord_user_id}>")
                    if event.points > 0:
                        tail_parts.append(f"(+{event.points} Champion-Punkte)")
                tail = f" - {' '.join(tail_parts)}" if tail_parts else ""
                body.append(
                    f"- **{event.character_name}** hat ein durchschnittliches Itemlevel "
                    f"von **{self._format_item_level(event.average_item_level)}** "
                    f"erreicht{tail}"
                )
            sections.append(("**Ausrüstungs-Meilensteine** 🛡️", body))

        if activity.skill_events:
            # Collapse to one line per (character, profession): show only the
            # highest threshold reached, but sum the points across all crossed
            # thresholds so the user still sees the full reward. Avoids digest
            # spam when several thresholds are crossed at once.
            grouped: dict[tuple[str, str], list[ProfessionSkillEvent]] = {}
            for event in activity.skill_events:
                grouped.setdefault(
                    (event.character_key, event.profession_id), []
                ).append(event)

            collapsed: list[tuple[ProfessionSkillEvent, int]] = []
            for events in grouped.values():
                top = max(events, key=lambda e: e.threshold)
                total_points = sum(e.points for e in events)
                collapsed.append((top, total_points))

            body = []
            for top, total_points in sorted(
                collapsed,
                key=lambda item: (
                    -item[0].threshold,
                    item[0].character_name.casefold(),
                ),
            ):
                tail_parts: list[str] = []
                if top.discord_user_id is not None:
                    tail_parts.append(f"<@{top.discord_user_id}>")
                    if total_points > 0:
                        tail_parts.append(f"(+{total_points} Champion-Punkte)")
                tail = f" - {' '.join(tail_parts)}" if tail_parts else ""
                body.append(
                    f"- **{top.character_name}** "
                    f"({self._profession_name(top.profession_id)}): "
                    f"hat Skill **{top.threshold}** erreicht{tail}"
                )
            sections.append(("**Berufsskill-Meilensteine** 🧪", body))

        if activity.cooldowns_ready:
            body = []
            now = datetime.now(timezone.utc)
            for cd in sorted(activity.cooldowns_ready, key=lambda c: c.ready_at):
                ready_label = self._format_cooldown_ready_label(cd.ready_at, now)
                group = COOLDOWN_GROUPS.get(cd.cooldown_group, {})
                group_label = group.get("label", cd.cooldown_group)
                mention = (
                    f" - <@{cd.discord_user_id}>"
                    if cd.discord_user_id is not None
                    else ""
                )
                body.append(
                    f"- **{cd.character_name}** ({group_label} — "
                    f"{cd.last_spell_name}): {ready_label}{mention}"
                )
            sections.append(("**Cooldowns bereit in den nächsten 24h** ⏳", body))

        if activity.deaths:
            body = []
            for death in sorted(
                activity.deaths,
                key=lambda item: (-item.member.level, item.member.name.casefold()),
            ):
                gear = self._format_death_gear_suffix(death)
                claim = await self.data.get_claim(death.member.character_key)
                mention = f" - <@{claim.discord_user_id}>" if claim else ""
                # Both confirmed (Blizzard profile says dead) and unconfirmed
                # (vanished from roster) cases are listed equally — in
                # practice the unconfirmed ones turned out to be dead too.
                # The section header carries the framing for the whole list.
                body.append(
                    f"- {self._format_roster_line(death.member)}{gear}{mention}"
                )
            sections.append(("**Heute nehmen wir Abschied** 🕯️", body))

        sections.append((None, [self._digest_closer(activity)]))
        return sections

    async def format_activity_digest(self, activity: ActivityDiff) -> str:
        """Render the digest as a single joined string.

        The wire path uses ``_digest_sections`` directly via the chunker;
        this method exists for callers (tests, manual inspection) that
        want the full text in one piece.
        """
        sections = await self._digest_sections(activity)
        parts: list[str] = []
        for idx, (header, body) in enumerate(sections):
            if idx > 0:
                parts.append("")
            if header is not None:
                parts.append(header)
            parts.extend(body)
        return "\n".join(parts)

    def _format_roster_line(
        self, member: RosterMember, level: int | None = None
    ) -> str:
        parts = [f"**{member.name}**", f"Level **{level or member.level}**"]
        race = RACE_NAMES_DE.get(member.race_id)
        class_name = CLASS_NAMES_DE.get(member.class_id)
        if race:
            parts.append(race)
        if class_name:
            parts.append(class_name)
        return ", ".join(parts)

    def _format_item_level(self, average_item_level: float) -> str:
        return f"{average_item_level:.1f}"

    def _format_cooldown_ready_label(
        self, ready_at_iso: str, now: datetime | None = None
    ) -> str:
        """Render ``ready_at`` as 'ab heute HH:MM' / 'ab morgen HH:MM' / 'jetzt'.

        Times are shown in the digest timezone so users see local clock time.
        """
        now = now or datetime.now(timezone.utc)
        try:
            ready = datetime.fromisoformat(ready_at_iso)
        except ValueError:
            return "Zeitpunkt unklar"
        if ready.tzinfo is None:
            ready = ready.replace(tzinfo=timezone.utc)
        ready_local = ready.astimezone(DIGEST_TIMEZONE)
        now_local = now.astimezone(DIGEST_TIMEZONE)
        if ready <= now:
            return "ready **jetzt**"
        today = now_local.date()
        target = ready_local.date()
        when = (
            "heute"
            if target == today
            else (
                "morgen"
                if target == today + timedelta(days=1)
                else target.strftime("%d.%m.")
            )
        )
        return f"ab {when} **{ready_local.strftime('%H:%M')}**"

    def _format_death_gear_suffix(self, death: DeathEvent) -> str:
        if death.member.level < 60 or death.average_item_level is None:
            return ""
        return f", Ø iLvl **{self._format_item_level(death.average_item_level)}**"

    def _display_character(self, member: RosterMember) -> str:
        parts = []
        race = RACE_NAMES_DE.get(member.race_id)
        class_name = CLASS_NAMES_DE.get(member.class_id)
        if race:
            parts.append(race)
        if class_name:
            parts.append(class_name)
        parts.append(member.name)
        return " ".join(parts)

    async def claim_character(
        self, discord_user_id: int, char_name: str
    ) -> ClaimResult:
        member = await self.data.find_roster_member_by_name(char_name)
        if not member:
            return ClaimResult(
                claim=None,
                member=None,
                reason="not_found",
            )

        claim, created = await self.data.create_claim(member, discord_user_id)
        if not created:
            reason = (
                "already_own" if claim.discord_user_id == discord_user_id else "taken"
            )
            return ClaimResult(
                claim=claim,
                member=member,
                created=False,
                reason=reason,
            )

        review_posted = await self._post_claim_review(claim)
        return ClaimResult(
            claim=claim,
            member=member,
            created=True,
            reason="created",
            review_posted=review_posted,
        )

    # ---- "Meine Chars" detail view action handlers ----

    async def _my_chars_profession(
        self, interaction: discord.Interaction, view: "PanelMyCharsView"
    ) -> None:
        claim = view.selected_claim
        if claim is None:
            await view.refresh(interaction)
            return
        profiles = await self.data.professions_for_character(claim.character_key)
        sub_view = PanelProfessionSelectView(self, interaction.user.id, claim, profiles)
        await interaction.response.edit_message(
            content=(
                f"Berufe für **{claim.character_name}**:\n"
                f"{self.format_profession_slots(profiles)}\n\n"
                "Welchen Beruf möchtest du pflegen?"
            ),
            embed=None,
            view=sub_view,
        )

    async def _my_chars_recipes(
        self, interaction: discord.Interaction, view: "PanelMyCharsView"
    ) -> None:
        claim = view.selected_claim
        if claim is None:
            await view.refresh(interaction)
            return
        profiles = await self.data.professions_for_character(claim.character_key)
        if not profiles:
            await interaction.response.edit_message(
                content=(
                    f"Für **{claim.character_name}** sind noch keine Berufe gepflegt. "
                    "Nutze zuerst **Berufe pflegen**."
                ),
                embed=None,
                view=None,
            )
            return
        sub_view = PanelRecipeProfessionSelectView(
            self, interaction.user.id, claim, profiles
        )
        await interaction.response.edit_message(
            content="Für welchen Beruf möchtest du Spezialrezepte pflegen?",
            embed=None,
            view=sub_view,
        )

    async def _my_chars_release(
        self, interaction: discord.Interaction, view: "PanelMyCharsView"
    ) -> None:
        claim = view.selected_claim
        if claim is None:
            await view.refresh(interaction)
            return
        await self.data.release_claim(claim.character_key, claim.discord_user_id)
        # May have been the user's last char — drop the guild role if so.
        await self.reconcile_guild_role_for(claim.discord_user_id)
        view.selected_claim = None
        await view.refresh(interaction)

    async def _my_chars_notify_toggle(
        self, interaction: discord.Interaction, view: "PanelMyCharsView"
    ) -> None:
        claim = view.selected_claim
        if claim is None:
            await view.refresh(interaction)
            return
        await self.data.set_crafting_notify(
            claim.character_key, not claim.crafting_notify
        )
        await view.refresh(interaction)

    async def _my_chars_claim_new(
        self, interaction: discord.Interaction, view: "PanelMyCharsView"
    ) -> None:
        # Direct text-modal: a 25-char dropdown was useless for larger guilds.
        await interaction.response.send_modal(PanelCharacterSearchModal(self))

    async def _my_chars_addon_import(
        self, interaction: discord.Interaction, view: "PanelMyCharsView"
    ) -> None:
        await interaction.response.send_modal(PanelAddonImportModal(self))

    async def build_whois_view(
        self, char_name: str, viewer_id: int
    ) -> "discord.ui.LayoutView | None":
        """Render an ephemeral Components-V2 profile card.

        Returns ``None`` if no matching roster member exists. Uses only
        existing DB helpers — no extra Blizzard API calls.
        """
        member = await self.data.find_roster_member_by_name(char_name)
        if member is None:
            return None
        claim = await self.data.get_claim(member.character_key)
        gear = await self.data.gear_snapshot(member.character_key)
        professions = await self.data.professions_for_character(member.character_key)

        twins: list[tuple[CharacterClaim, RosterMember | None]] = []
        if claim is not None:
            all_claims = await self.data.claims_for_user(claim.discord_user_id)
            snapshot = await self.data.get_snapshot()
            others = [c for c in all_claims if c.character_key != claim.character_key]
            twins = [(c, snapshot.get(c.character_key)) for c in others]
            # Sort main-first: best in-game rank, then highest level.
            twins.sort(
                key=lambda pair: (
                    (
                        pair[1].guild_rank
                        if pair[1] is not None and pair[1].guild_rank is not None
                        else 99
                    ),
                    -(pair[1].level if pair[1] is not None else 0),
                    pair[0].character_name.casefold(),
                )
            )

        viewer_is_owner = claim is not None and claim.discord_user_id == viewer_id
        return _WhoisLayoutView(
            self, member, claim, gear, professions, twins, viewer_id, viewer_is_owner
        )

    async def submit_gbank_request(
        self,
        requester: discord.abc.User,
        requester_char_name: str,
        bank_character_key: str,
        bank_character_name: str,
        request_text: str,
    ) -> None:
        """Deliver a guild-bank request to the bank char's owner.

        Tries a DM to the claiming owner first; on failure (DMs closed)
        or if the bank char is unclaimed, posts to the officer channel so
        nothing is lost. The requester always sees success.
        """
        claim = await self.data.get_claim(bank_character_key)
        body = (
            f"🏦 **Neue Gildenbank-Anfrage** von **{requester_char_name}** "
            f"(von {requester.mention})\n"
            f'„{request_text}"\n'
            f"Die Items liegen auf dem Bank-Char **{bank_character_name}**."
        )

        if claim is not None:
            owner = self.bot.get_user(claim.discord_user_id)
            if owner is None:
                try:
                    owner = await self.bot.fetch_user(claim.discord_user_id)
                except discord.HTTPException:
                    owner = None
            if owner is not None:
                try:
                    await owner.send(body)
                    return
                except (discord.Forbidden, discord.HTTPException) as exc:
                    logger.info(
                        "[WoWCog] gbank DM to %s failed: %s",
                        claim.discord_user_id,
                        exc,
                    )

        reason = (
            "Bank-Char ist nicht geclaimed"
            if claim is None
            else "DM an den Owner fehlgeschlagen"
        )
        await self._post_gbank_to_officer_channel(f"_({reason})_\n{body}")

    async def _post_gbank_to_officer_channel(self, content: str) -> None:
        channel_id = await self.get_claim_review_channel_id()
        channel = self.bot.get_channel(channel_id)
        if not channel:
            logger.warning("[WoWCog] gbank officer channel %s not found.", channel_id)
            return
        try:
            await channel.send(content)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning(
                "[WoWCog] Could not post gbank request to channel %s: %s",
                channel_id,
                exc,
            )

    async def _post_claim_review(self, claim: CharacterClaim) -> bool:
        channel_id = await self.get_claim_review_channel_id()
        channel = self.bot.get_channel(channel_id)
        if not channel:
            logger.warning("[WoWCog] Claim review channel %s not found.", channel_id)
            return False

        try:
            message = await channel.send(
                await self.format_claim_review_message(claim),
                view=ClaimReviewView(self),
            )
        except discord.Forbidden:
            logger.warning(
                "[WoWCog] Missing access to claim review channel %s.", channel_id
            )
            return False
        except discord.HTTPException as exc:
            logger.warning(
                "[WoWCog] Could not post claim review to channel %s: %s",
                channel_id,
                exc,
                exc_info=True,
            )
            return False

        await self.data.set_claim_review_message(claim.character_key, message.id)
        return True

    async def _claim_review_message_exists(self, channel, message_id: int) -> bool:
        """True if the stored review message still lives in ``channel``.

        Returns ``False`` only when Discord confirms the message is gone
        (``NotFound``) — e.g. it was manually deleted, or it lives in the old
        review channel after a channel switch. On transient/permission errors
        (or no channel) it returns ``True`` so a momentarily unreachable channel
        never triggers duplicate cards.
        """
        if channel is None:
            return True
        try:
            await channel.fetch_message(message_id)
            return True
        except discord.NotFound:
            return False
        except (discord.Forbidden, discord.HTTPException):
            return True

    async def repost_missing_claim_reviews(self) -> tuple[int, int]:
        """Re-post review cards for unverified claims that lack a live one.

        Fixes two cases: (1) a claim made while the bot lacked permission in the
        review channel — the claim persisted in the DB (``create_claim`` runs
        before the post) but its review card never appeared; (2) a review card
        that was manually deleted in Discord (or left behind in the old channel
        after a channel switch) — the DB still points at a message that no longer
        exists. A claim is reposted when it has no ``review_message_id`` **or**
        that message is confirmed gone, so live cards are never duplicated.
        Returns ``(reposted, failed)``.
        """
        channel_id = await self.get_claim_review_channel_id()
        channel = self.bot.get_channel(channel_id)
        reposted = 0
        failed = 0
        for claim in await self.data.list_claims("unverified"):
            if claim.review_message_id is not None and (
                await self._claim_review_message_exists(
                    channel, claim.review_message_id
                )
            ):
                continue
            if await self._post_claim_review(claim):
                reposted += 1
            else:
                failed += 1
        return reposted, failed

    def format_claim_review(self, claim: CharacterClaim) -> str:
        return (
            f"<@{claim.discord_user_id}> möchte **{claim.character_name}** verbinden "
            f"— bitte bestätigen oder ablehnen."
        )

    async def format_claim_review_message(self, claim: CharacterClaim) -> str:
        """Claim-review text enriched with the user's other claims + a hint.

        Shows every already-claimed char of the requester with its current
        in-game rank, and recommends the target rank: first Member+ char →
        Member; if they already have one → the new claim is a Twink. Lets the
        officer decide Member-vs-Twink without looking it up in-game.
        """
        base = self.format_claim_review(claim)
        snapshot = await self.data.get_snapshot()
        others = [
            existing
            for existing in await self.data.claims_for_user(claim.discord_user_id)
            if existing.character_key != claim.character_key
        ]
        lines = [base, ""]
        has_member = False
        if others:
            lines.append("**Bereits geclaimt von diesem User:**")
            for existing in sorted(others, key=lambda c: c.character_name.casefold()):
                member = snapshot.get(existing.character_key)
                if member is None:
                    rank_label = "nicht mehr im Roster"
                else:
                    rank_label = self._rank_label(member.guild_rank)
                    if (
                        member.guild_rank is not None
                        and member.guild_rank <= MEMBER_RANK
                    ):
                        has_member = True
                marker = "✅" if existing.status == "verified" else "⏳"
                lines.append(f"- {marker} **{existing.character_name}** ({rank_label})")
            lines.append("")
        if has_member:
            lines.append(
                "⚠️ Hat bereits einen **Member+**-Char → dieser Claim ist "
                "vermutlich ein **Twink**."
            )
        else:
            lines.append(
                "➡️ Noch kein Member+-Char → dieser Claim wird der " "**Member**-Char."
            )
        return "\n".join(lines)

    def _wow_records(self, table: str) -> list[dict]:
        data = getattr(self.bot, "data", {}).get("wow", {})
        records = data.get(table, [])
        return records if isinstance(records, list) else []

    def _localized_text(self, value: object, language: str = "de") -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return value.get(language) or value.get("de") or value.get("en") or ""
        return ""

    def _profession_name(self, profession_id: str, language: str = "de") -> str:
        profession = self._get_static_record("professions", profession_id)
        return self._localized_text(profession.get("name"), language) or profession_id

    def profession_practitioner_title(self, profession_id: str | None) -> str | None:
        """The "who does this" noun for a profession — see PROFESSION_PRACTITIONER_TITLES.

        Falls back to the profession's own skill name if it's not one of the
        mapped Classic crafting professions; ``None`` only when no
        profession_id was given at all.
        """
        if not profession_id:
            return None
        return PROFESSION_PRACTITIONER_TITLES.get(
            profession_id
        ) or self._profession_name(profession_id)

    def normalize_recipe_language(self, language: str | None) -> str:
        if language in {"de", "en"}:
            return str(language)
        return "de"

    def _profession_type(self, profession_id: str) -> str:
        profession = self._get_static_record("professions", profession_id)
        return str(profession.get("type") or "primary")

    def _is_primary_profession(self, profession_id: str) -> bool:
        return self._profession_type(profession_id) == "primary"

    def _is_crafting_profession(self, profession: dict) -> bool:
        profession_id = profession.get("id")
        if not profession_id:
            return False
        if profession_id in EXCLUDED_CRAFTING_PROFESSIONS:
            return False
        profession_type = profession.get("type") or "primary"
        return (
            profession_type == "primary"
            or profession_id in SECONDARY_CRAFTING_PROFESSIONS
        )

    def _crafting_professions(self) -> list[dict]:
        return [
            profession
            for profession in self._wow_records("professions")
            if self._is_crafting_profession(profession)
        ]

    def _get_static_record(self, table: str, record_id: str | None) -> dict:
        if not record_id:
            return {}
        for record in self._wow_records(table):
            if record.get("id") == record_id:
                return record
        return {}

    def _spell_for_recipe(self, recipe: dict) -> dict:
        return self._get_static_record("spells", recipe.get("spell_id"))

    def _item_for_recipe(self, recipe: dict) -> dict:
        return self._get_static_record("items", recipe.get("creates_item_id"))

    def _recipe_name(self, recipe: dict, language: str = "de") -> str:
        language = self.normalize_recipe_language(language)
        spell = self._spell_for_recipe(recipe)
        item = self._item_for_recipe(recipe)
        return (
            self._localized_text(item.get("name"), language)
            or self._localized_text(spell.get("name"), language)
            or str(recipe.get("id") or recipe.get("spell_id") or "")
        )

    def _recipe_secondary_name(self, recipe: dict, language: str = "de") -> str:
        other_language = (
            "en" if self.normalize_recipe_language(language) == "de" else "de"
        )
        name = self._recipe_name(recipe, other_language)
        return name if name != self._recipe_name(recipe, language) else ""

    def _recipe_source_label(self, recipe: dict) -> str:
        sources = [str(source) for source in recipe.get("recipe_item_sources") or []]
        if not sources:
            return "Quelle unbekannt"
        labels = {
            "drop": "Drop",
            "world_drop": "World-Drop",
            "pickpocketed": "Taschendiebstahl",
            "quest": "Quest",
            "vendor": "Haendler",
            "crafted": "Crafting",
        }
        return ", ".join(labels.get(source, source) for source in sources)

    def recipe_learning_reward(self, recipe: dict) -> tuple[str, int]:
        """Return ``(rarity, points)`` for a recipe.

        Points scale with two factors:
        * **Source**: World-drops > pickpocketed > regular drops > quest.
          Vendor-only or trainer recipes don't trigger an event (0 points).
        * **Skill bracket**: required_skill 276-300 → ×3, 201-275 → ×2,
          101-200 → ×1.5, 1-100 → ×1.

        Hand-curated "epic" recipes (Crusader enchant, Arcanite gear, etc.)
        start at a 10-point base and still scale with the skill multiplier.
        """
        if not recipe or recipe.get("learned_from") == "trainer":
            return "common", 0
        spell_id = str(recipe.get("spell_id") or "")
        sources = {str(source) for source in recipe.get("recipe_item_sources") or []}
        required_skill = int(recipe.get("required_skill") or 0)

        if spell_id in EPIC_RECIPE_SPELL_IDS:
            base = EPIC_RECIPE_BASE_POINTS
            rarity = "epic"
        else:
            base = max(
                (
                    RECIPE_POINTS_BY_SOURCE[src]
                    for src in sources
                    if src in RECIPE_POINTS_BY_SOURCE
                ),
                default=0,
            )
            if base == 0:
                return "common", 0
            rarity = "rare"

        multiplier = next(
            mult
            for threshold, mult in RECIPE_SKILL_BRACKETS
            if required_skill >= threshold
        )
        points = max(1, int(round(base * multiplier)))
        return rarity, points

    def _recipe_search_text(self, recipe: dict) -> str:
        spell = self._spell_for_recipe(recipe)
        item = self._item_for_recipe(recipe)
        parts = [
            recipe.get("id", ""),
            recipe.get("spell_id", ""),
            self._localized_text(spell.get("name"), "de"),
            self._localized_text(spell.get("name"), "en"),
            self._localized_text(item.get("name"), "de"),
            self._localized_text(item.get("name"), "en"),
        ]
        return " ".join(_norm(part) for part in parts if part)

    def resolve_profession_id(self, value: str) -> str | None:
        needle = _norm(value)
        for profession in self._crafting_professions():
            names = [
                profession.get("id", ""),
                self._localized_text(profession.get("name"), "de"),
                self._localized_text(profession.get("name"), "en"),
            ]
            if needle in {_norm(name) for name in names if name}:
                return profession["id"]
        return None

    def profession_choices(self, current: str = "") -> list[tuple[str, str]]:
        needle = _norm(current)
        choices = []
        for profession in self._crafting_professions():
            name = self._localized_text(profession.get("name"))
            profession_id = profession.get("id")
            if not profession_id or not name:
                continue
            searchable = f"{profession_id} {name} {self._localized_text(profession.get('name'), 'en')}"
            if not needle or needle in _norm(searchable):
                choices.append((name, profession_id))
        return choices[:25]

    def profession_choices_for_claim(
        self, profiles: list[CharacterProfession], current: str = ""
    ) -> list[tuple[str, str, str]]:
        needle = _norm(current)
        current_ids = {profile.profession_id for profile in profiles}
        primary_count = sum(
            1
            for profile in profiles
            if self._is_primary_profession(profile.profession_id)
        )
        choices = []
        for profession in self._crafting_professions():
            profession_id = profession.get("id")
            if not profession_id:
                continue
            is_existing = profession_id in current_ids
            is_primary = self._is_primary_profession(profession_id)
            if (
                not is_existing
                and is_primary
                and primary_count >= MAX_PRIMARY_PROFESSIONS
            ):
                continue
            if (
                not is_existing
                and not is_primary
                and profession_id not in SECONDARY_CRAFTING_PROFESSIONS
            ):
                continue
            name = self._localized_text(profession.get("name"))
            if not name:
                continue
            searchable = f"{profession_id} {name} {self._localized_text(profession.get('name'), 'en')}"
            if needle and needle not in _norm(searchable):
                continue
            profile = next(
                (
                    profile
                    for profile in profiles
                    if profile.profession_id == profession_id
                ),
                None,
            )
            if profile:
                description = f"Aktuell: Skill {profile.skill_level}"
            elif is_primary:
                description = "Freier Hauptberuf-Slot"
            else:
                description = "Nebenberuf"
            choices.append((name, profession_id, description))
        return choices[:25]

    def format_profession_slots(self, profiles: list[CharacterProfession]) -> str:
        primary_profiles = [
            profile
            for profile in profiles
            if self._is_primary_profession(profile.profession_id)
        ]
        primary_profiles.sort(
            key=lambda profile: self._profession_name(profile.profession_id)
        )
        lines = []
        for index in range(MAX_PRIMARY_PROFESSIONS):
            if index < len(primary_profiles):
                profile = primary_profiles[index]
                lines.append(
                    f"Hauptberuf {index + 1}: {self.format_profession(profile)}"
                )
            else:
                lines.append(f"Hauptberuf {index + 1}: frei")

        cooking = next(
            (profile for profile in profiles if profile.profession_id == "cooking"),
            None,
        )
        if cooking:
            lines.append(f"Kochen: {self.format_profession(cooking)}")
        else:
            lines.append("Kochen: frei")
        return "\n".join(lines)

    def format_profession_slots_short(self, profiles: list[CharacterProfession]) -> str:
        primary_profiles = [
            profile
            for profile in profiles
            if self._is_primary_profession(profile.profession_id)
        ]
        primary_profiles.sort(
            key=lambda profile: self._profession_name(profile.profession_id)
        )
        lines = []
        for index in range(MAX_PRIMARY_PROFESSIONS):
            if index < len(primary_profiles):
                profile = primary_profiles[index]
                lines.append(
                    f"Hauptberuf {index + 1}: {self.format_profession_short(profile)}"
                )
            else:
                lines.append(f"Hauptberuf {index + 1}: frei")

        cooking = next(
            (profile for profile in profiles if profile.profession_id == "cooking"),
            None,
        )
        if cooking:
            lines.append(f"Kochen: {self.format_profession_short(cooking)}")
        else:
            lines.append("Kochen: frei")
        return "\n".join(lines)

    async def set_crafting_profile(
        self,
        discord_user_id: int,
        char_name: str,
        profession_value: str,
        skill_level: int,
        specialization: str | None,
        *,
        is_mod: bool = False,
    ) -> CraftingProfileResult:
        if skill_level < 1 or skill_level > 300:
            return CraftingProfileResult(None, reason="invalid_skill")
        profession_id = self.resolve_profession_id(profession_value)
        if not profession_id:
            return CraftingProfileResult(None, reason="unknown_profession")
        claim = await self.data.get_claim_by_name(char_name)
        if not claim:
            return CraftingProfileResult(None, reason="not_claimed")
        if not is_mod and claim.discord_user_id != discord_user_id:
            return CraftingProfileResult(None, claim=claim, reason="forbidden")
        profiles = await self.data.professions_for_character(claim.character_key)
        already_set = any(
            profile.profession_id == profession_id for profile in profiles
        )
        primary_count = sum(
            1
            for profile in profiles
            if self._is_primary_profession(profile.profession_id)
        )
        if (
            self._is_primary_profession(profession_id)
            and not already_set
            and primary_count >= MAX_PRIMARY_PROFESSIONS
        ):
            return CraftingProfileResult(None, claim=claim, reason="primary_limit")
        previous = await self.data.get_character_profession(
            claim.character_key, profession_id
        )
        old_skill = int(previous.skill_level) if previous else 0
        profession = await self.data.set_character_profession(
            claim, profession_id, skill_level, specialization
        )
        await self._record_skill_milestones(
            claim.character_key, profession_id, old_skill, int(skill_level)
        )
        return CraftingProfileResult(profession, claim=claim, reason="saved")

    async def _record_skill_milestones(
        self,
        character_key: str,
        profession_id: str,
        old_skill: int,
        new_skill: int,
    ) -> None:
        """Record milestone events for every threshold crossed in this update.

        Cumulative: a jump from 50 to 250 awards 75 and 150 and 225 in one go.
        Idempotent via the table's primary key (character_key, profession_id,
        threshold) plus an explicit ``skill_milestone_exists`` guard so we
        only ever count the same crossing once.
        """
        if new_skill <= old_skill:
            return
        for threshold in sorted(PROFESSION_SKILL_MILESTONES):
            if not (old_skill < threshold <= new_skill):
                continue
            if await self.data.skill_milestone_exists(
                character_key, profession_id, threshold
            ):
                continue
            await self.data.record_skill_milestone(
                character_key,
                profession_id,
                threshold,
                new_skill,
                PROFESSION_SKILL_MILESTONES[threshold],
            )

    async def remove_crafting_profile(
        self,
        discord_user_id: int,
        char_name: str,
        profession_value: str,
        *,
        is_mod: bool = False,
    ) -> CraftingProfileResult:
        profession_id = self.resolve_profession_id(profession_value)
        if not profession_id:
            return CraftingProfileResult(None, reason="unknown_profession")
        claim = await self.data.get_claim_by_name(char_name)
        if not claim:
            return CraftingProfileResult(None, reason="not_claimed")
        if not is_mod and claim.discord_user_id != discord_user_id:
            return CraftingProfileResult(None, claim=claim, reason="forbidden")
        removed = await self.data.remove_character_profession(
            claim.character_key, profession_id
        )
        return CraftingProfileResult(
            None, claim=claim, reason="removed" if removed else "not_set"
        )

    async def prepare_recipe_selection(
        self,
        discord_user_id: int,
        char_name: str,
        profession_value: str | None,
        search: str | None,
        *,
        is_mod: bool = False,
    ) -> RecipeSelectionResult:
        claim = await self.data.get_claim_by_name(char_name)
        if not claim:
            return RecipeSelectionResult("not_claimed")
        if not is_mod and claim.discord_user_id != discord_user_id:
            return RecipeSelectionResult("forbidden", claim=claim)

        profiles = await self.data.professions_for_character(claim.character_key)
        if not profiles:
            return RecipeSelectionResult("no_professions", claim=claim, profiles=[])

        if profession_value:
            profession_id = self.resolve_profession_id(profession_value)
            if not profession_id:
                return RecipeSelectionResult("unknown_profession", claim=claim)
            profile = next(
                (
                    profile
                    for profile in profiles
                    if profile.profession_id == profession_id
                ),
                None,
            )
            if not profile:
                return RecipeSelectionResult("profession_not_set", claim=claim)
            return await self._recipe_selection_for_profile(claim, profile, search)

        if len(profiles) == 1:
            return await self._recipe_selection_for_profile(claim, profiles[0], search)
        return RecipeSelectionResult(
            "choose_profession", claim=claim, profiles=profiles
        )

    async def _recipe_selection_for_profile(
        self,
        claim: CharacterClaim,
        profile: CharacterProfession,
        search: str | None,
    ) -> RecipeSelectionResult:
        known_spell_ids = await self.data.known_recipe_spell_ids(claim.character_key)
        recipes = self._available_manual_recipes(profile, known_spell_ids, search)
        return RecipeSelectionResult(
            "ok",
            claim=claim,
            profile=profile,
            recipes=recipes,
        )

    def _available_manual_recipes(
        self,
        profile: CharacterProfession,
        known_spell_ids: set[str],
        search: str | None,
    ) -> list[dict]:
        needle = _norm(search or "")
        recipes = []
        for recipe in self._wow_records("profession_recipes"):
            if recipe.get("profession_id") != profile.profession_id:
                continue
            if recipe.get("learned_from") == "trainer":
                continue
            if not recipe.get("hardcore_valid"):
                continue
            if int(recipe.get("required_skill") or 0) > profile.skill_level:
                continue
            if recipe.get("spell_id") in known_spell_ids:
                continue
            if needle and needle not in self._recipe_search_text(recipe):
                continue
            recipes.append(recipe)
        return sorted(
            recipes,
            key=lambda recipe: (
                int(recipe.get("required_skill") or 0),
                self._recipe_name(recipe).casefold(),
            ),
        )

    async def save_known_recipes(
        self,
        claim: CharacterClaim,
        profession_id: str,
        spell_ids: list[str],
    ) -> int:
        valid_recipes = {
            recipe.get("spell_id"): recipe
            for recipe in self._wow_records("profession_recipes")
            if recipe.get("profession_id") == profession_id
            and recipe.get("learned_from") != "trainer"
            and recipe.get("hardcore_valid")
        }
        accepted = [spell_id for spell_id in spell_ids if spell_id in valid_recipes]
        inserted = await self.data.add_known_recipes_returning_inserted(
            claim.character_key, profession_id, accepted
        )
        for spell_id in inserted:
            rarity, points = self.recipe_learning_reward(valid_recipes[spell_id])
            if points > 0 and claim.status == "verified":
                await self.data.record_recipe_learning_event(
                    claim.character_key, spell_id, profession_id, rarity, points
                )
        return len(inserted)

    def _decode_profession_export(self, code: str) -> dict:
        """Decode a LotusDiscordBridge addon export string (base64 -> JSON).

        Raises ``ValueError`` with a user-facing message on any malformed
        input - a truncated paste, wrong addon, or corrupted code should
        never surface as a raw exception to the caller.
        """
        try:
            raw = base64.b64decode(code.strip(), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Der Code ist kein gueltiger Export (Base64).") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Der Code enthaelt kein gueltiges Export-Format.") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("addon") != "LotusDiscordBridge"
        ):
            raise ValueError("Das ist kein LotusDiscordBridge-Export-Code.")
        return payload

    def _match_special_recipes(
        self, profession_id: str, recipes: list[dict]
    ) -> tuple[list[str], list[str]]:
        """Cross-check imported recipes against our Wowhead-sourced data.

        Returns ``(matched_spell_ids, unmatched_labels)``. A matched recipe
        only ends up in ``matched_spell_ids`` (fed into the EXISTING
        ``save_known_recipes`` points system) when it is non-trainer and
        hardcore_valid - identical criteria to manual recipe curation, so
        importing never awards more than picking recipes by hand would.
        ``unmatched`` covers ids that exist in neither trainer nor special
        recipe data for this profession at all - a real gap worth surfacing,
        not just "this one happens to be an ordinary trainer recipe".
        """
        static_recipes = [
            record
            for record in self._wow_records("profession_recipes")
            if record.get("profession_id") == profession_id
        ]
        by_item_id = {
            record.get("creates_item_id"): record
            for record in static_recipes
            if record.get("creates_item_id")
        }
        by_spell_id = {
            record.get("spell_id"): record
            for record in static_recipes
            if record.get("spell_id")
        }

        matched: list[str] = []
        unmatched: list[str] = []
        for recipe in recipes:
            item_id = recipe.get("itemId")
            spell_id = recipe.get("spellId")
            static_recipe = None
            if item_id is not None:
                static_recipe = by_item_id.get(f"item.{item_id}")
            if static_recipe is None and spell_id is not None:
                static_recipe = by_spell_id.get(f"spell.{spell_id}")

            if static_recipe is None:
                unmatched.append(
                    f"{recipe.get('name', '?')} (item={item_id}, spell={spell_id})"
                )
                continue
            if static_recipe.get("learned_from") != "trainer" and static_recipe.get(
                "hardcore_valid"
            ):
                matched.append(static_recipe.get("spell_id"))
        return matched, unmatched

    async def import_profession_export(
        self,
        discord_user_id: int,
        code: str,
        *,
        is_mod: bool = False,
    ) -> ProfessionImportResult:
        """Import a LotusDiscordBridge addon export for the character it names.

        Updates the character's skill level, replaces its full crafting-
        recipe index (search data - see ``character_crafting_recipes``), and
        feeds whatever qualifies as a points-eligible special recipe into
        the existing ``save_known_recipes`` flow. Anything that matches
        nothing in our static data is logged (never silently dropped) and
        also returned so the caller can surface a count to the user.
        """
        try:
            payload = self._decode_profession_export(code)
        except ValueError as exc:
            logger.warning("[WoWCog] Profession-Import abgelehnt: %s", exc)
            return ProfessionImportResult("invalid")

        character_name = payload.get("character")
        claim = (
            await self.data.get_claim_by_name(character_name)
            if character_name
            else None
        )
        if not claim:
            return ProfessionImportResult("not_claimed", character_name=character_name)
        if not is_mod and claim.discord_user_id != discord_user_id:
            return ProfessionImportResult(
                "forbidden", claim=claim, character_name=character_name
            )

        professions = (payload.get("data") or {}).get("professions") or []
        if not professions:
            return ProfessionImportResult(
                "no_professions", claim=claim, character_name=character_name
            )

        recipes_seen = 0
        special_learned = 0
        unmatched: list[str] = []

        for profession in professions:
            profession_id = profession.get("professionId")
            if not profession_id:
                continue

            skill_level = profession.get("skillLevel")
            if isinstance(skill_level, int) and skill_level > 0:
                # The crafter search already reads this table for trainer
                # recipes (skill_level >= required_skill) - no separate
                # storage needed for those.
                await self.data.set_character_profession(
                    claim, profession_id, skill_level
                )

            recipes = profession.get("recipes") or []
            recipes_seen += len(recipes)

            matched_spell_ids, profession_unmatched = self._match_special_recipes(
                profession_id, recipes
            )
            for detail in profession_unmatched:
                logger.warning(
                    "[WoWCog] Profession-Import: kein DB-Treffer (char=%s, beruf=%s): %s",
                    character_name,
                    profession_id,
                    detail,
                )
            unmatched.extend(
                f"{profession_id}: {detail}" for detail in profession_unmatched
            )

            if matched_spell_ids:
                # The crafter search already reads character_known_recipes
                # for special recipes - same table manual curation writes to.
                special_learned += await self.save_known_recipes(
                    claim, profession_id, matched_spell_ids
                )

        return ProfessionImportResult(
            "ok",
            claim=claim,
            character_name=character_name,
            professions_imported=len(professions),
            recipes_seen=recipes_seen,
            special_recipes_learned=special_learned,
            unmatched=unmatched,
        )

    def format_profession_import_result(self, result: ProfessionImportResult) -> str:
        """Renders a ``import_profession_export`` result as one chat message.

        Shared by the ``/wow crafting import`` slash command and the Panel's
        Addon-Import modal so both surfaces stay in sync automatically.
        """
        if result.status == "invalid":
            return (
                "Der Code konnte nicht gelesen werden - bitte frisch per `/ldx` "
                "im Spiel exportieren und komplett kopieren."
            )
        if result.status == "not_claimed":
            return (
                f"**{result.character_name or '?'}** ist nicht geclaimed — erst "
                "claimen (`/wow claim` oder **Neuen Char claimen** im Panel)."
            )
        if result.status == "forbidden":
            return f"**{result.claim.character_name}** gehört nicht dir."
        if result.status == "no_professions":
            return "Im Export waren keine Berufe enthalten."

        lines = [
            f"✅ **{result.character_name}**: {result.professions_imported} Beruf(e) "
            f"aktualisiert, {result.recipes_seen} Rezepte geprüft."
        ]
        if result.special_recipes_learned:
            lines.append(
                f"🌟 {result.special_recipes_learned} davon neu als Spezialrezept gewertet."
            )
        if result.unmatched:
            lines.append(
                f"⚠️ {len(result.unmatched)} Rezept(e) nicht in unserer Datenbank "
                "gefunden — siehe Bot-Logs für Details."
            )
        return "\n".join(lines)

    async def known_recipes_for_character(
        self,
        discord_user_id: int,
        char_name: str,
        *,
        is_mod: bool = False,
    ) -> RecipeSelectionResult:
        claim = await self.data.get_claim_by_name(char_name)
        if not claim:
            return RecipeSelectionResult("not_claimed")
        if not is_mod and claim.discord_user_id != discord_user_id:
            return RecipeSelectionResult("forbidden", claim=claim)
        records = await self.data.known_recipes_for_character(claim.character_key)
        return RecipeSelectionResult("ok", claim=claim, recipes=list(records))

    async def remove_known_recipe(
        self,
        discord_user_id: int,
        char_name: str,
        recipe_value: str,
        *,
        is_mod: bool = False,
    ) -> RecipeSelectionResult:
        claim = await self.data.get_claim_by_name(char_name)
        if not claim:
            return RecipeSelectionResult("not_claimed")
        if not is_mod and claim.discord_user_id != discord_user_id:
            return RecipeSelectionResult("forbidden", claim=claim)
        recipe = await self.resolve_known_recipe(claim.character_key, recipe_value)
        if not recipe:
            return RecipeSelectionResult("recipe_not_found", claim=claim)
        removed = await self.data.remove_known_recipe(
            claim.character_key, recipe.spell_id
        )
        return RecipeSelectionResult(
            "removed" if removed else "recipe_not_found", claim=claim
        )

    async def resolve_known_recipe(
        self, character_key: str, recipe_value: str
    ) -> CharacterKnownRecipe | None:
        needle = _norm(recipe_value)
        for recipe in await self.data.known_recipes_for_character(character_key):
            static = self._recipe_by_spell_id(recipe.spell_id)
            names = [
                recipe.spell_id,
                self._recipe_name(static) if static else "",
                (
                    self._localized_text(
                        self._spell_for_recipe(static).get("name"), "de"
                    )
                    if static
                    else ""
                ),
                (
                    self._localized_text(
                        self._spell_for_recipe(static).get("name"), "en"
                    )
                    if static
                    else ""
                ),
            ]
            if needle in {_norm(name) for name in names if name}:
                return recipe
        return None

    def _recipe_by_spell_id(self, spell_id: str | None) -> dict:
        for recipe in self._wow_records("profession_recipes"):
            if recipe.get("spell_id") == spell_id:
                return recipe
        return {}

    async def search_crafting(self, item_name: str) -> CraftingSearchResult:
        matches = self._match_items(item_name)
        if not matches:
            return CraftingSearchResult("item_not_found")
        # Filter: keep only items that are the output of some profession recipe.
        # Drops recipe-teaching items ("Rezept: ...") and other non-craftable
        # matches that the fuzzy name search returned.
        craftable_ids = {
            str(r.get("creates_item_id"))
            for r in self._wow_records("profession_recipes")
            if r.get("creates_item_id")
        }
        craftable = [m for m in matches if str(m.get("id")) in craftable_ids]
        # Enchants create no item, so they're matched separately and added
        # as pseudo-item candidates (id "enchant:<spell_id>").
        enchants = self._match_enchant_recipes(item_name)
        candidates = craftable + enchants
        if not candidates:
            return CraftingSearchResult("item_not_found")
        if len(candidates) > 1:
            return CraftingSearchResult("ambiguous_item", candidates=candidates[:5])
        return await self._resolve_candidate(candidates[0])

    async def _resolve_candidate(self, candidate: dict) -> CraftingSearchResult:
        """Dispatch a search candidate to the item- or enchant-resolver."""
        if str(candidate.get("id")).startswith("enchant:"):
            return await self._search_crafting_for_enchant(candidate["_recipe"])
        return await self._search_crafting_for_item(candidate)

    async def search_crafting_by_item_id(self, item_id: str) -> CraftingSearchResult:
        if item_id.startswith("enchant:"):
            spell_id = item_id.split(":", 1)[1]
            recipe = self._recipe_by_spell_id(spell_id)
            if not recipe:
                return CraftingSearchResult("item_not_found")
            return await self._search_crafting_for_enchant(recipe)
        item = self._get_static_record("items", item_id)
        if not item:
            return CraftingSearchResult("item_not_found")
        return await self._search_crafting_for_item(item)

    async def _search_crafting_for_item(self, item: dict) -> CraftingSearchResult:
        recipes = [
            recipe
            for recipe in self._wow_records("profession_recipes")
            if recipe.get("creates_item_id") == item.get("id")
            and recipe.get("hardcore_valid")
            and recipe.get("profession_id") != "first-aid"
        ]
        if not recipes:
            return CraftingSearchResult("recipe_not_found", item=item)

        trainer_recipes = [
            recipe for recipe in recipes if recipe.get("learned_from") == "trainer"
        ]
        manual_recipes = [
            recipe for recipe in recipes if recipe.get("learned_from") != "trainer"
        ]
        recipe = min(
            trainer_recipes,
            key=lambda record: int(record.get("required_skill") or 0),
            default=None,
        )
        manual_recipe = False
        if recipe is None and manual_recipes:
            recipe = min(
                manual_recipes,
                key=lambda record: int(record.get("required_skill") or 0),
            )
            manual_recipe = True
        if recipe is None:
            return CraftingSearchResult("recipe_not_found", item=item)

        profession_id = recipe.get("profession_id")
        required_skill = int(recipe.get("required_skill") or 1)
        if manual_recipe:
            crafters = await self.data.find_crafters_with_known_recipe(
                profession_id, required_skill, str(recipe.get("spell_id"))
            )
        else:
            crafters = await self.data.find_crafters(profession_id, required_skill)
        if not crafters:
            status = "manual_recipe" if manual_recipe else "no_crafter"
            return CraftingSearchResult(
                status,
                item=item,
                recipe=recipe,
                required_skill=required_skill,
                profession_id=profession_id,
                crafters=[],
                manual_recipe=manual_recipe,
            )
        return CraftingSearchResult(
            "ok",
            item=item,
            recipe=recipe,
            crafters=crafters,
            required_skill=required_skill,
            profession_id=profession_id,
            manual_recipe=manual_recipe,
        )

    async def _search_crafting_for_enchant(self, recipe: dict) -> CraftingSearchResult:
        """Resolve crafters for an enchant recipe (no item output).

        Trainer-learned enchants → any enchanter with enough skill.
        Drop/recipe-learned enchants (e.g. Crusader) → only enchanters who
        have explicitly maintained the formula as learned.
        """
        spell = self._spell_for_recipe(recipe)
        pseudo_item = {
            "name": spell.get("name") or recipe.get("recipe_item_name") or {}
        }
        profession_id = "enchanting"
        required_skill = int(recipe.get("required_skill") or 1)
        manual = recipe.get("learned_from") != "trainer"
        if manual:
            crafters = await self.data.find_crafters_with_known_recipe(
                profession_id, required_skill, str(recipe.get("spell_id"))
            )
        else:
            crafters = await self.data.find_crafters(profession_id, required_skill)
        if not crafters:
            status = "manual_recipe" if manual else "no_crafter"
            return CraftingSearchResult(
                status,
                item=pseudo_item,
                recipe=recipe,
                required_skill=required_skill,
                profession_id=profession_id,
                crafters=[],
                manual_recipe=manual,
            )
        return CraftingSearchResult(
            "ok",
            item=pseudo_item,
            recipe=recipe,
            crafters=crafters,
            required_skill=required_skill,
            profession_id=profession_id,
            manual_recipe=manual,
        )

    def _match_items(self, item_name: str) -> list[dict]:
        needle = _norm(item_name)
        exact = []
        partial = []
        fuzzy: list[tuple[float, dict]] = []
        for item in self._wow_records("items"):
            names = [
                self._localized_text(item.get("name"), "de"),
                self._localized_text(item.get("name"), "en"),
            ]
            normalized = [_norm(name) for name in names if name]
            if needle in normalized:
                exact.append(item)
            elif any(needle and needle in name for name in normalized):
                partial.append(item)
            elif needle:
                score = max(
                    (
                        difflib.SequenceMatcher(None, needle, name).ratio()
                        for name in normalized
                    ),
                    default=0,
                )
                if score >= 0.82:
                    fuzzy.append((score, item))
        if exact:
            return exact[:25]
        if partial:
            return partial[:25]
        fuzzy.sort(
            key=lambda match: (
                -match[0],
                self._localized_text(match[1].get("name")).casefold(),
            )
        )
        return [item for _, item in fuzzy[:25]]

    def _match_enchant_recipes(self, name: str) -> list[dict]:
        """Match enchant recipes by spell name (de/en) + formula name (de/en).

        Enchants create no item, so they never appear in ``items.json`` and
        can't be found via ``_match_items``. They flow through the rest of
        the crafting-search pipeline as pseudo-item dicts with a synthetic
        ``enchant:<spell_id>`` id and the original recipe under ``_recipe``.
        """
        needle = _norm(name)
        if not needle:
            return []
        exact: list[dict] = []
        partial: list[dict] = []
        fuzzy: list[tuple[float, dict]] = []
        for recipe in self._wow_records("profession_recipes"):
            if recipe.get("profession_id") != "enchanting":
                continue
            if not recipe.get("hardcore_valid"):
                continue
            spell = self._spell_for_recipe(recipe)
            names = [
                self._localized_text(spell.get("name"), "de"),
                self._localized_text(spell.get("name"), "en"),
                self._localized_text(recipe.get("recipe_item_name"), "de"),
                self._localized_text(recipe.get("recipe_item_name"), "en"),
            ]
            normalized = [_norm(n) for n in names if n]
            if not normalized:
                continue
            candidate = {
                "id": f"enchant:{recipe.get('spell_id')}",
                "name": spell.get("name") or recipe.get("recipe_item_name"),
                "_recipe": recipe,
            }
            if needle in normalized:
                exact.append(candidate)
            elif any(needle in n for n in normalized):
                partial.append(candidate)
            else:
                score = max(
                    (
                        difflib.SequenceMatcher(None, needle, n).ratio()
                        for n in normalized
                    ),
                    default=0,
                )
                if score >= 0.82:
                    fuzzy.append((score, candidate))
        if exact:
            return exact[:25]
        if partial:
            return partial[:25]
        fuzzy.sort(key=lambda match: -match[0])
        return [candidate for _, candidate in fuzzy[:25]]

    def format_profession(self, profile: CharacterProfession) -> str:
        specialization = (
            f" ({profile.specialization})" if profile.specialization else ""
        )
        return (
            f"**{profile.character_name}** - "
            f"{self._profession_name(profile.profession_id)} "
            f"{profile.skill_level}{specialization}"
        )

    def format_profession_short(self, profile: CharacterProfession) -> str:
        specialization = (
            f" ({profile.specialization})" if profile.specialization else ""
        )
        return (
            f"{self._profession_name(profile.profession_id)} "
            f"{profile.skill_level}{specialization}"
        )

    def format_crafting_search_result(self, result: CraftingSearchResult) -> str:
        if result.status == "item_not_found":
            return "Dieses Item wurde in den WoW-Daten nicht gefunden."
        if result.status == "ambiguous_item":
            return "Mehrere Items gefunden — bitte aus dem Menü wählen."
        item_name = self._localized_text((result.item or {}).get("name"))
        if result.status == "recipe_not_found":
            return f"Für **{item_name}** wurde kein Crafting-Rezept gefunden."
        if result.status == "manual_recipe":
            return (
                f"**{item_name}** ist ein Spezialrezept. "
                "Aktuell hat es noch niemand gepflegt."
            )
        profession_name = self._profession_name(result.profession_id or "")
        if result.status == "no_crafter":
            return (
                f"**{item_name}** benötigt {profession_name} "
                f"{result.required_skill}. Kein geclaimter Crafter passt aktuell."
            )
        lines = [
            f"**{item_name}** kann gecraftet werden von:",
            "",
        ]
        for crafter in result.crafters or []:
            specialization = (
                f" ({crafter.specialization})" if crafter.specialization else ""
            )
            recipe_note = " - Spezialrezept gepflegt" if result.manual_recipe else ""
            lines.append(
                f"- <@{crafter.discord_user_id}> mit **{crafter.character_name}** "
                f"({profession_name} {crafter.skill_level}{specialization}){recipe_note}"
            )
        return "\n".join(lines)

    async def update_claim_review_message(
        self, interaction: discord.Interaction, claim: CharacterClaim, action: str
    ) -> None:
        if action == "verified":
            content = (
                f"{self.format_claim_review(claim)}\n"
                f"Bestätigt von <@{interaction.user.id}>."
            )
        else:
            content = (
                f"{self.format_claim_review(claim)}\n"
                f"Abgelehnt von <@{interaction.user.id}>."
            )
        await interaction.response.edit_message(content=content, view=None)

    async def status(self) -> dict[str, object]:
        channel_id = await self.get_announcement_channel_id()
        panel_channel_id = await self.data.get_setting("panel_channel_id")
        panel_message_id = await self.data.get_setting("panel_message_id")
        return {
            "guild": self.guild_name,
            "realm": self.realm_slug,
            "channel_id": channel_id,
            "officer_channel_id": await self.get_claim_review_channel_id(),
            "panel_channel_id": int(panel_channel_id) if panel_channel_id else None,
            "panel_message_id": int(panel_message_id) if panel_message_id else None,
            "last_scan_at": await self.data.last_scan_at(),
            "member_count": await self.data.member_count(),
            "poll_interval": self.poll_interval,
            "recipe_events": "aktiv",
        }

    def cog_unload(self) -> None:
        super().cog_unload()
        self.create_task(self.data.close())


class CraftingProfessionSelect(discord.ui.Select):
    def __init__(
        self,
        cog: WoWCog,
        owner_user_id: int,
        claim: CharacterClaim,
        profiles: list[CharacterProfession],
        search: str | None,
        language: str = "de",
    ) -> None:
        self.cog = cog
        self.owner_user_id = owner_user_id
        self.claim = claim
        self.profiles = profiles
        self.search = search
        self.language = cog.normalize_recipe_language(language)
        options = [
            discord.SelectOption(
                label=cog._profession_name(profile.profession_id),
                value=profile.profession_id,
                description=f"Skill {profile.skill_level}",
            )
            for profile in profiles[:25]
        ]
        super().__init__(
            placeholder="Beruf auswählen",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        profile = next(
            (
                profile
                for profile in self.profiles
                if profile.profession_id == self.values[0]
            ),
            None,
        )
        if profile is None:
            await interaction.response.send_message(
                "Dieser Beruf ist nicht mehr verfügbar.", ephemeral=True
            )
            return
        result = await self.cog._recipe_selection_for_profile(
            self.claim, profile, self.search
        )
        if not result.recipes:
            await interaction.response.edit_message(
                content=(
                    f"Für **{self.claim.character_name}** gibt es aktuell keine "
                    f"offenen Spezialrezepte für "
                    f"{self.cog._profession_name(profile.profession_id)}."
                ),
                view=None,
            )
            return
        view = CraftingRecipeSelectionView(
            self.cog,
            self.owner_user_id,
            self.claim,
            profile,
            result.recipes,
            language=self.language,
        )
        await interaction.response.edit_message(content=view.content(), view=view)


class CraftingProfessionSelectView(discord.ui.View):
    def __init__(
        self,
        cog: WoWCog,
        owner_user_id: int,
        claim: CharacterClaim,
        profiles: list[CharacterProfession],
        search: str | None,
        language: str = "de",
    ) -> None:
        super().__init__(timeout=300)
        self.owner_user_id = owner_user_id
        self.add_item(
            CraftingProfessionSelect(
                cog, owner_user_id, claim, profiles, search, language
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_user_id:
            return True
        await interaction.response.send_message(
            "Diese Auswahl gehört nicht dir.", ephemeral=True
        )
        return False


class CraftingRecipeSelect(discord.ui.Select):
    def __init__(self, parent: "CraftingRecipeSelectionView") -> None:
        self.parent_view = parent
        options = [
            discord.SelectOption(
                label=parent.option_label(recipe),
                value=str(recipe.get("spell_id")),
                description=parent.option_description(recipe),
                default=str(recipe.get("spell_id")) in parent.selected_spell_ids,
            )
            for recipe in parent.current_page_recipes()
        ]
        super().__init__(
            placeholder="Gelernte Rezepte auswählen",
            min_values=0,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        current_spell_ids = {
            str(recipe.get("spell_id"))
            for recipe in self.parent_view.current_page_recipes()
        }
        self.parent_view.selected_spell_ids -= current_spell_ids
        self.parent_view.selected_spell_ids.update(self.values)
        self.parent_view.refresh_items()
        await interaction.response.edit_message(
            content=self.parent_view.content(),
            view=self.parent_view,
        )


class CraftingRecipeSelectionView(discord.ui.View):
    page_size = 25

    def __init__(
        self,
        cog: WoWCog,
        owner_user_id: int,
        claim: CharacterClaim,
        profile: CharacterProfession,
        recipes: list[dict],
        language: str = "de",
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_user_id = owner_user_id
        self.claim = claim
        self.profile = profile
        self.recipes = recipes
        self.language = cog.normalize_recipe_language(language)
        self.page = 0
        self.selected_spell_ids: set[str] = set()
        self.refresh_items()

    @property
    def max_page(self) -> int:
        return max(0, (len(self.recipes) - 1) // self.page_size)

    def current_page_recipes(self) -> list[dict]:
        start = self.page * self.page_size
        return self.recipes[start : start + self.page_size]

    def option_label(self, recipe: dict) -> str:
        label = self.cog._recipe_name(recipe, self.language)
        return label[:100] or str(recipe.get("spell_id"))[:100]

    def option_description(self, recipe: dict) -> str:
        skill = int(recipe.get("required_skill") or 0)
        spell = self.cog._spell_for_recipe(recipe)
        other_name = self.cog._recipe_secondary_name(recipe, self.language)
        spell_name = self.cog._localized_text(spell.get("name"), self.language)
        text = f"Skill {skill}"
        if other_name:
            text = f"{text} - {other_name}"
        elif spell_name and spell_name != self.option_label(recipe):
            text = f"{text} - {spell_name}"
        return text[:100]

    def content(self) -> str:
        return _recipe_selection_content(
            self.cog,
            self.claim,
            self.profile,
            self.recipes,
            self.page,
            self.selected_spell_ids,
        )

    def refresh_items(self) -> None:
        self.clear_items()
        self.add_item(CraftingRecipeSelect(self))
        self.previous_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= self.max_page
        self.add_item(self.previous_page)
        self.add_item(self.next_page)
        self.add_item(self.save)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_user_id:
            return True
        await interaction.response.send_message(
            "Diese Auswahl gehört nicht dir.", ephemeral=True
        )
        return False

    @discord.ui.button(label="Zurück", style=discord.ButtonStyle.secondary)
    async def previous_page(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        self.page = max(0, self.page - 1)
        self.refresh_items()
        await interaction.response.edit_message(content=self.content(), view=self)

    @discord.ui.button(label="Weiter", style=discord.ButtonStyle.secondary)
    async def next_page(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        self.page = min(self.max_page, self.page + 1)
        self.refresh_items()
        await interaction.response.edit_message(content=self.content(), view=self)

    @discord.ui.button(label="Speichern", style=discord.ButtonStyle.success)
    async def save(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        if not self.selected_spell_ids:
            await interaction.response.send_message(
                "Bitte wähle mindestens ein Rezept aus.", ephemeral=True
            )
            return
        saved = await self.cog.save_known_recipes(
            self.claim,
            self.profile.profession_id,
            sorted(self.selected_spell_ids),
        )
        await interaction.response.edit_message(
            content=(
                f"{saved} Rezepte für **{self.claim.character_name}** gespeichert."
            ),
            view=None,
        )


class CraftingSearchSuggestionSelect(discord.ui.Select):
    def __init__(self, parent: "CraftingSearchSuggestionView") -> None:
        self.parent_view = parent
        super().__init__(
            placeholder="Item auswählen",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=parent.cog._localized_text(item.get("name"), "de")[:100],
                    value=str(item.get("id")),
                    description=parent.cog._localized_text(item.get("name"), "en")[
                        :100
                    ],
                )
                for item in parent.items[:25]
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        result = await self.parent_view.cog.search_crafting_by_item_id(self.values[0])
        view = self.parent_view.cog.crafting_search_response_view(
            result, interaction.user.id
        )
        await interaction.response.edit_message(
            content=self.parent_view.cog.format_crafting_search_result(result),
            view=view,
        )


class CraftingSearchSuggestionView(discord.ui.View):
    def __init__(self, cog: WoWCog, owner_user_id: int, items: list[dict]) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.owner_user_id = owner_user_id
        self.items = items
        self.add_item(CraftingSearchSuggestionSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_user_id:
            return True
        await interaction.response.send_message(
            "Diese Auswahl gehört nicht dir.", ephemeral=True
        )
        return False


class CraftingSearchMarketplaceActionsView(discord.ui.View):
    """Bridges an already-resolved crafting search into a Marktplatz
    Crafting-Gesuch, so the item never needs to be re-typed/re-searched."""

    def __init__(
        self, cog: WoWCog, owner_user_id: int, result: CraftingSearchResult
    ) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.owner_user_id = owner_user_id
        self.result = result

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_user_id:
            return True
        await interaction.response.send_message("Das gehört nicht dir.", ephemeral=True)
        return False

    @discord.ui.button(
        label="Marktplatz-Anfrage erstellen",
        style=discord.ButtonStyle.success,
        emoji="🛒",
    )
    async def create_request(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        marketplace = self.cog._marketplace_cog()
        if marketplace is None:
            await interaction.response.send_message(
                "❌ Marktplatz-System nicht verfügbar.", ephemeral=True
            )
            return
        await marketplace.begin_crafting_request_from_result(
            interaction, self.result, note=None
        )


def _recipe_selection_content(
    cog: WoWCog,
    claim: CharacterClaim,
    profile: CharacterProfession,
    recipes: list[dict],
    page: int = 0,
    selected: set[str] | None = None,
) -> str:
    selected_count = len(selected or set())
    return (
        f"**{claim.character_name}** - "
        f"{cog._profession_name(profile.profession_id)} {profile.skill_level}\n"
        f"Offene Spezialrezepte: {len(recipes)} | Seite {page + 1} | "
        f"ausgewählt: {selected_count}"
    )


class PanelCharacterSearchModal(discord.ui.Modal):
    def __init__(self, cog: WoWCog) -> None:
        super().__init__(title="Charakter suchen")
        self.cog = cog
        self.query = discord.ui.TextInput(
            label="Charaktername",
            placeholder="z.B. Voidok",
            min_length=2,
            max_length=32,
        )
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        query = str(self.query.value).strip()
        snapshot = await self.cog.data.get_snapshot()
        matches = [
            member
            for member in sorted(
                snapshot.values(), key=lambda item: item.name.casefold()
            )
            if query.casefold() in member.name.casefold()
        ][:25]
        if not matches:
            await interaction.response.send_message(
                f"Kein Black-Lotus-Charakter zu **{query}** gefunden.",
                ephemeral=True,
            )
            return
        view = PanelRosterCharacterSelectView(self.cog, interaction.user.id, matches)
        await interaction.response.send_message(
            "Bitte wähle deinen Charakter aus.",
            view=view,
            ephemeral=True,
        )


class PanelRosterCharacterSelect(discord.ui.Select):
    def __init__(self, parent: "PanelRosterCharacterSelectView") -> None:
        self.parent_view = parent
        super().__init__(
            placeholder="Charakter auswählen",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=member.name[:100],
                    value=member.name,
                    description=(
                        f"Level {member.level} - "
                        f"{parent.cog._display_character(member)}"
                    )[:100],
                )
                for member in parent.members
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        result = await self.parent_view.cog.claim_character(
            interaction.user.id, self.values[0]
        )
        await interaction.response.edit_message(
            content=_format_panel_claim_result(result, self.values[0]),
            view=None,
        )


class PanelRosterCharacterSelectView(discord.ui.View):
    def __init__(
        self, cog: WoWCog, owner_user_id: int, members: list[RosterMember]
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_user_id = owner_user_id
        self.members = members
        self.add_item(PanelRosterCharacterSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_user_id:
            return True
        await interaction.response.send_message(
            "Diese Auswahl gehört nicht dir.", ephemeral=True
        )
        return False


class PanelOwnedCharacterSelect(discord.ui.Select):
    def __init__(
        self, parent: "PanelOwnedCharacterSelectView", claims: list[CharacterClaim]
    ) -> None:
        self.parent_view = parent
        super().__init__(
            placeholder="Charakter auswählen",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=claim.character_name[:100],
                    value=claim.character_name,
                    description=(
                        "bestätigt" if claim.status == "verified" else "ungeprüft"
                    ),
                )
                for claim in claims[:25]
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        claim = await self.parent_view.cog.data.get_claim_by_name(self.values[0])
        if not claim or claim.discord_user_id != interaction.user.id:
            await interaction.response.edit_message(
                content="Dieser Charakter ist nicht mehr verfügbar.",
                view=None,
            )
            return
        if self.parent_view.mode == "profession":
            profiles = await self.parent_view.cog.data.professions_for_character(
                claim.character_key
            )
            view = PanelProfessionSelectView(
                self.parent_view.cog, interaction.user.id, claim, profiles
            )
            await interaction.response.edit_message(
                content=(
                    f"Berufe fuer **{claim.character_name}**:\n"
                    f"{self.parent_view.cog.format_profession_slots(profiles)}\n\n"
                    "Welchen Beruf moechtest du pflegen?"
                ),
                view=view,
            )
            return
        profiles = await self.parent_view.cog.data.professions_for_character(
            claim.character_key
        )
        if not profiles:
            await interaction.response.edit_message(
                content=(
                    f"Für **{claim.character_name}** sind noch keine Berufe gepflegt. "
                    "Nutze zuerst **Berufe pflegen**."
                ),
                view=None,
            )
            return
        view = PanelRecipeProfessionSelectView(
            self.parent_view.cog, interaction.user.id, claim, profiles
        )
        await interaction.response.edit_message(
            content="Für welchen Beruf möchtest du Spezialrezepte pflegen?",
            view=view,
        )


class PanelOwnedCharacterSelectView(discord.ui.View):
    def __init__(
        self,
        cog: WoWCog,
        owner_user_id: int,
        claims: list[CharacterClaim],
        mode: str,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_user_id = owner_user_id
        self.mode = mode
        self.add_item(PanelOwnedCharacterSelect(self, claims))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_user_id:
            return True
        await interaction.response.send_message(
            "Diese Auswahl gehört nicht dir.", ephemeral=True
        )
        return False


class PanelProfessionSelect(discord.ui.Select):
    def __init__(self, parent: "PanelProfessionSelectView") -> None:
        self.parent_view = parent
        choices = parent.cog.profession_choices_for_claim(parent.profiles)
        super().__init__(
            placeholder="Beruf auswählen",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=name[:100],
                    value=value,
                    description=description[:100],
                )
                for name, value, description in choices
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            PanelProfessionEditModal(
                self.parent_view.cog,
                self.parent_view.claim,
                self.values[0],
            )
        )


class PanelProfessionSelectView(discord.ui.View):
    def __init__(
        self,
        cog: WoWCog,
        owner_user_id: int,
        claim: CharacterClaim,
        profiles: list[CharacterProfession],
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_user_id = owner_user_id
        self.claim = claim
        self.profiles = profiles
        self.add_item(PanelProfessionSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_user_id:
            return True
        await interaction.response.send_message(
            "Diese Auswahl gehört nicht dir.", ephemeral=True
        )
        return False


class PanelProfessionEditModal(discord.ui.Modal):
    def __init__(self, cog: WoWCog, claim: CharacterClaim, profession_id: str) -> None:
        super().__init__(title="Beruf pflegen")
        self.cog = cog
        self.claim = claim
        self.profession_id = profession_id
        self.skill = discord.ui.TextInput(
            label="Skill",
            placeholder="1-300",
            min_length=1,
            max_length=3,
        )
        self.specialization = discord.ui.TextInput(
            label="Spezialisierung",
            placeholder="optional, z.B. Tränke",
            required=False,
            max_length=64,
        )
        self.add_item(self.skill)
        self.add_item(self.specialization)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            skill = int(str(self.skill.value).strip())
        except ValueError:
            await interaction.response.send_message(
                "Skill muss eine Zahl zwischen 1 und 300 sein.",
                ephemeral=True,
            )
            return
        result = await self.cog.set_crafting_profile(
            interaction.user.id,
            self.claim.character_name,
            self.profession_id,
            skill,
            str(self.specialization.value).strip() or None,
        )
        await interaction.response.send_message(
            _format_panel_profession_result(self.cog, result),
            ephemeral=True,
        )


class PanelRecipeProfessionSelect(discord.ui.Select):
    def __init__(self, parent: "PanelRecipeProfessionSelectView") -> None:
        self.parent_view = parent
        super().__init__(
            placeholder="Beruf auswählen",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=parent.cog._profession_name(profile.profession_id)[:100],
                    value=profile.profession_id,
                    description=f"Skill {profile.skill_level}",
                )
                for profile in parent.profiles[:25]
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        result = await self.parent_view.cog.prepare_recipe_selection(
            interaction.user.id,
            self.parent_view.claim.character_name,
            self.values[0],
            None,
        )
        if result.status != "ok" or not result.recipes:
            await interaction.response.edit_message(
                content=(
                    f"Für **{self.parent_view.claim.character_name}** gibt es "
                    "aktuell keine offenen Spezialrezepte."
                ),
                view=None,
            )
            return
        view = CraftingRecipeSelectionView(
            self.parent_view.cog,
            interaction.user.id,
            result.claim,
            result.profile,
            result.recipes,
        )
        await interaction.response.edit_message(
            content=view.content(),
            view=view,
        )


class PanelRecipeProfessionSelectView(discord.ui.View):
    def __init__(
        self,
        cog: WoWCog,
        owner_user_id: int,
        claim: CharacterClaim,
        profiles: list[CharacterProfession],
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_user_id = owner_user_id
        self.claim = claim
        self.profiles = profiles
        self.add_item(PanelRecipeProfessionSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_user_id:
            return True
        await interaction.response.send_message(
            "Diese Auswahl gehört nicht dir.", ephemeral=True
        )
        return False


class PanelCraftingSearchModal(discord.ui.Modal):
    def __init__(self, cog: WoWCog) -> None:
        super().__init__(title="Crafter in der Gilde suchen")
        self.cog = cog
        self.item = discord.ui.TextInput(
            label="Welches Item brauchst du?",
            placeholder="z. B. Wuttrank, Klingenstein-Rüstung ...",
            min_length=2,
            max_length=80,
        )
        self.add_item(self.item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        result = await self.cog.search_crafting(str(self.item.value).strip())
        content = self.cog.format_crafting_search_result(result)
        view = self.cog.crafting_search_response_view(result, interaction.user.id)
        # discord.py's send_message uses MISSING as the "no view" sentinel
        # and does ``not view.is_finished()`` on whatever else is passed —
        # explicitly passing ``view=None`` raises AttributeError. Only
        # forward ``view=...`` when we actually have a View instance.
        if view is not None:
            await interaction.response.send_message(content, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)


class PanelAddonImportModal(discord.ui.Modal):
    """Panel counterpart of ``/wow crafting import`` — same
    ``import_profession_export`` call, just reachable without typing a
    slash command. The target character comes from the export code itself
    (not from any char currently selected in the Panel), so this works
    regardless of which char is selected in "Deine Chars".

    Discord caps a modal TextInput at 4000 characters; a very large export
    (many professions/recipes) may not fit — the slash command allows up to
    6000 and is the fallback for that case.
    """

    def __init__(self, cog: WoWCog) -> None:
        super().__init__(title="Beruf per Addon importieren")
        self.cog = cog
        self.code = discord.ui.TextInput(
            label="Export-Code",
            placeholder="/ldx im Spiel, Button 'Exportieren', hier einfügen",
            style=discord.TextStyle.paragraph,
            min_length=10,
            max_length=4000,
        )
        self.add_item(self.code)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        result = await self.cog.import_profession_export(
            interaction.user.id, str(self.code.value).strip()
        )
        await interaction.followup.send(
            self.cog.format_profession_import_result(result), ephemeral=True
        )


class PanelCooldownStartView(discord.ui.View):
    """Ephemeral picker for logging a craft-cooldown.

    Each option is a (char, CD-spell) pair derived from the user's
    explicitly-learned recipes. Click → "now" is persisted via
    :meth:`WoWCog.log_cooldown` and the user gets a confirmation with the
    computed ready-at time.
    """

    def __init__(
        self,
        cog: WoWCog,
        owner_user_id: int,
        options: list[tuple[CharacterClaim, str, str, str]],
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_user_id = owner_user_id
        self.options = options
        self.add_item(PanelCooldownStartSelect(self))
        _add_dismiss_button(self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_user_id:
            return True
        await interaction.response.send_message(
            "Diese Auswahl gehört nicht dir.", ephemeral=True
        )
        return False


class PanelCooldownStartSelect(discord.ui.Select):
    def __init__(self, parent: PanelCooldownStartView) -> None:
        self.parent_view = parent
        # Each value encodes "<character_key>|<spell_id>" so the callback
        # knows both pieces without juggling extra state.
        select_options = []
        for claim, spell_id, spell_name, group_label in parent.options[:25]:
            label = f"{claim.character_name}: {spell_name}"[:100]
            description = f"{group_label} CD"[:100]
            select_options.append(
                discord.SelectOption(
                    label=label,
                    value=f"{claim.character_key}|{spell_id}",
                    description=description,
                )
            )
        super().__init__(
            placeholder="Cooldown auswählen",
            min_values=1,
            max_values=1,
            options=select_options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        character_key, spell_id = self.values[0].split("|", 1)
        status, cooldown = await self.parent_view.cog.log_cooldown(
            interaction.user.id, character_key, spell_id
        )
        if status != "ok" or cooldown is None:
            error_messages = {
                "not_owner": "Dieser Char gehört nicht dir.",
                "unknown_spell": "Unbekannter Cooldown-Spell.",
                "recipe_missing": (
                    "Dieser Char hat das Rezept nicht als gelernt eingetragen."
                ),
            }
            await interaction.response.edit_message(
                content=error_messages.get(status, "Speichern fehlgeschlagen."),
                view=None,
            )
            return
        ready_label = self.parent_view.cog._format_cooldown_ready_label(
            cooldown.ready_at
        )
        await interaction.response.edit_message(
            content=(
                f"✅ **{cooldown.character_name}** hat "
                f"**{cooldown.last_spell_name}** eingesetzt. "
                f"Wieder verfügbar {ready_label}."
            ),
            view=None,
        )


_PANEL_CHANNEL_OVERVIEW = (
    "**WoW Channels im Überblick**\n"
    "**#wow-info** — Infos & dieses Panel\n"
    "**#wow-general** — Allgemeiner WoW-Chat\n"
    "**#wow-dungeons** — Dungeon- & Raid-Runs (Raid Helper: `/create`)\n"
    "**#wow-classic-news** — Aktuelle News zu WoW Classic\n"
    "**#wow-content** — Streams, Videos & Clips\n"
    "**#wow-classes** — Klassen, Builds, Talente, Spielweise\n"
    "**#wow-welcome** — Neu im WoW-Bereich? Hier starten!"
)
PANEL_HUB_TEXT = (
    "## 🪷 Black Lotus WoW-Hub\n"
    "Hier verbindest du deine Chars, pflegst Berufe, loggst Cooldowns und "
    "findest Crafter in der Gilde. Wähle einen Bereich aus."
)
PANEL_HELP_TEXT = (
    "**Kurzanleitung**\n\n"
    "**👤 Deine Chars** — verbinde Black-Lotus-Charaktere mit deinem "
    "Discord-Account, pflege Berufe & Spezialrezepte, schau dir aktive "
    "Cooldowns an und gib Claims frei. **📥 Per Addon importieren** übernimmt "
    "Beruf, Skill und Rezepte direkt aus dem `/ldx`-Export-Code des Addons — "
    "Alternative zum manuellen Eintragen.\n\n"
    "**🔎 In der Gilde suchen** — finde, wer ein bestimmtes Item craften "
    "kann, oder schlag einen Char nach (Owner, Berufe, Status).\n\n"
    "**⏳ Cooldown loggen** — wenn du als Alchemist eine Transmute oder "
    "als Schneider Mondstoff machst, trag das hier über den Hub-Button ein. "
    "Der Bot erinnert im täglichen Digest, sobald sie wieder bereit sind.\n\n"
    "**Daily Digest** — jeden Morgen um **09:00 Berlin** postet der Bot "
    "Aufstiege, Tode, neue Crafts, Berufsskill-Meilensteine und "
    "bereitstehende Cooldowns. Geclaimte Chars werden gepingt.\n\n"
    "**🏆 Champion-System** — serverweites Punkte-System. Punkte gibt es "
    "automatisch für WoW-Aktivitäten (Chars claimen, Levelaufstiege, "
    "Berufs-Meilensteine, Rezepte). Commands: `/champion score` · "
    "`/champion rank` · `/champion leaderboard` · `/champion myhistory`\n\n"
    "Power-User: alle Funktionen gibt's auch als `/wow ...` Slash-Command.\n\n"
    "──────────────────────\n" + _PANEL_CHANNEL_OVERVIEW
)


def _add_dismiss_button(view: discord.ui.View) -> None:
    """Adds a ✖ Schließen button that deletes the ephemeral message."""
    btn = discord.ui.Button(label="✖ Schließen", style=discord.ButtonStyle.secondary)

    async def _close(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except discord.NotFound:
            pass

    btn.callback = _close
    view.add_item(btn)


class _PanelTextView(discord.ui.View):
    """Minimal view for ephemeral text-only panel responses."""

    def __init__(self) -> None:
        super().__init__(timeout=300)
        _add_dismiss_button(self)


class WoWPanelLayoutView(discord.ui.LayoutView):
    """Components-V2 hub for the WoW cog.

    Layout: a single ``Container`` with a header ``TextDisplay`` followed
    by ``Section`` blocks — one per top-level concern. Each section has an
    accessory button that opens a classic-V1 sub-flow (claim select,
    char-detail view, cooldown picker, etc.). All sub-flows are ephemeral
    so the public hub message stays stable.

    Persistence: the LayoutView is registered via ``bot.add_view`` at
    cog-load so buttons survive bot restarts. The hub message itself is
    re-issued automatically on startup via ``_auto_publish_panel``.
    """

    def __init__(self, cog: WoWCog, hub_text: str | None = None) -> None:
        super().__init__(timeout=None)
        self.cog = cog

        chars_btn = discord.ui.Button(
            label="Verwalten",
            style=discord.ButtonStyle.primary,
            custom_id="wow_panel_v2:chars",
        )
        chars_btn.callback = self._open_my_chars

        search_btn = discord.ui.Button(
            label="Suchen",
            style=discord.ButtonStyle.secondary,
            custom_id="wow_panel_v2:search",
        )
        search_btn.callback = self._open_search

        gbank_btn = discord.ui.Button(
            label="Anfragen",
            style=discord.ButtonStyle.secondary,
            custom_id="wow_panel_v2:gbank",
        )
        gbank_btn.callback = self._open_gbank_request

        cooldown_btn = discord.ui.Button(
            label="Loggen",
            style=discord.ButtonStyle.secondary,
            custom_id="wow_panel_v2:cooldown",
        )
        cooldown_btn.callback = self._open_cooldown_log

        help_btn = discord.ui.Button(
            label="Hilfe",
            style=discord.ButtonStyle.secondary,
            custom_id="wow_panel_v2:help",
        )
        help_btn.callback = self._open_help

        champion_btn = discord.ui.Button(
            label="Mein Rang",
            style=discord.ButtonStyle.secondary,
            custom_id="wow_panel_v2:champion",
        )
        champion_btn.callback = self._open_champion

        raider_btn = discord.ui.Button(
            label="An / Aus",
            style=discord.ButtonStyle.secondary,
            custom_id="wow_panel_v2:raider",
        )
        raider_btn.callback = self._toggle_raider_role

        container = discord.ui.Container(
            discord.ui.TextDisplay(hub_text or PANEL_HUB_TEXT),
            discord.ui.Separator(),
            discord.ui.Section(
                discord.ui.TextDisplay(
                    "### 👤 Deine Chars\n"
                    "Claimen, Berufe & Rezepte pflegen, Claim freigeben."
                ),
                accessory=chars_btn,
            ),
            discord.ui.Separator(),
            discord.ui.Section(
                discord.ui.TextDisplay(
                    "### 🔎 In der Gilde suchen\n"
                    "Crafter finden, Chars nachschlagen, Member-Lookup."
                ),
                accessory=search_btn,
            ),
            discord.ui.Separator(),
            discord.ui.Section(
                discord.ui.TextDisplay(
                    "### 🏦 Gildenbank-Anfrage\n"
                    "Anfrage stellen — der Verwalter bekommt eine DM."
                ),
                accessory=gbank_btn,
            ),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                "### 📅 Event erstellen\n"
                "Erstelle ein Raid- oder Dungeon-Event mit "
                "</create:885023455739777079>. Du bekommst danach eine "
                "private Nachricht von Raid-Helper mit den weiteren Schritten."
            ),
            discord.ui.Separator(),
            discord.ui.Section(
                discord.ui.TextDisplay(
                    "### ⏳ Cooldown loggen\n"
                    "Transmute, Mondstoff oder Salt Shaker eintragen."
                ),
                accessory=cooldown_btn,
            ),
            discord.ui.Separator(),
            discord.ui.Section(
                discord.ui.TextDisplay(
                    "### ❓ Hilfe & Übersicht\n" "Kanal-Übersicht & Bot-Kurzanleitung."
                ),
                accessory=help_btn,
            ),
            discord.ui.Separator(),
            discord.ui.Section(
                discord.ui.TextDisplay(
                    "### 🏆 Mein Champion-Rang\n" "Dein Punktestand & Rang."
                ),
                accessory=champion_btn,
            ),
            discord.ui.Separator(),
            discord.ui.Section(
                discord.ui.TextDisplay(
                    "### 🛡️ Raider-Rolle\n" "Raider-Rolle holen oder ablegen."
                ),
                accessory=raider_btn,
            ),
            accent_colour=HORDE_RED,
        )
        self.add_item(container)

    async def _open_my_chars(self, interaction: discord.Interaction) -> None:
        view = PanelMyCharsView(self.cog, interaction.user.id)
        await view.send(interaction)

    async def _open_search(self, interaction: discord.Interaction) -> None:
        view = PanelSearchSubView(self.cog)
        await interaction.response.send_message(
            "Was suchst du?", view=view, ephemeral=True
        )

    async def _open_gbank_request(self, interaction: discord.Interaction) -> None:
        claims = await self.cog.data.claims_for_user(interaction.user.id)
        if not claims:
            await interaction.response.send_message(
                "Du musst zuerst einen Char claimen, bevor du eine "
                "Gildenbank-Anfrage stellen kannst. Nutze **Deine Chars → "
                "Neuen Char claimen**.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(GBankRequestModal(self.cog))

    async def _open_cooldown_log(self, interaction: discord.Interaction) -> None:
        eligible = await self.cog.cooldown_eligible_options(interaction.user.id)
        if not eligible:
            await interaction.response.send_message(
                "Keiner deiner Chars hat ein Rezept mit Cooldown gelernt. "
                "Trag das passende Rezept zuerst über **Deine Chars → Rezepte "
                "pflegen** ein.",
                ephemeral=True,
            )
            return
        view = PanelCooldownStartView(self.cog, interaction.user.id, eligible)
        await interaction.response.send_message(
            "Welchen Cooldown willst du eintragen?",
            view=view,
            ephemeral=True,
        )

    async def _open_help(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            PANEL_HELP_TEXT, view=_PanelTextView(), ephemeral=True
        )

    async def _open_champion(self, interaction: discord.Interaction) -> None:
        champion = interaction.client.get_cog("ChampionCog")
        if champion is None or not hasattr(champion, "data"):
            await interaction.response.send_message(
                "Champion-System nicht verfügbar.", ephemeral=True
            )
            return
        user_id_str = str(interaction.user.id)
        total = await champion.data.get_total(user_id_str)
        rank_result = await champion.data.get_rank(user_id_str)
        role = champion.get_current_role(total)

        if total == 0:
            text = (
                "**🏆 Champion-Status**\n\n"
                "Du hast noch keine Punkte gesammelt.\n\n"
                "Punkte gibt es automatisch für WoW-Aktivitäten: Chars claimen, "
                "Levelaufstiege, Berufs-Meilensteine, Spezialrezepte und mehr."
            )
        else:
            role_text = f"**{role.name}**" if role else "Champion"
            rank_text = f"Rang **#{rank_result[0]}**" if rank_result else "kein Rang"
            text = (
                f"**🏆 Champion-Status**\n\n"
                f"Rolle: {role_text}\n"
                f"Punkte: **{total}**  ·  {rank_text}\n\n"
                f"`/champion score` · `/champion rank`\n"
                f"`/champion leaderboard` · `/champion myhistory`"
            )
        await interaction.response.send_message(
            text, view=_PanelTextView(), ephemeral=True
        )

    async def _toggle_raider_role(self, interaction: discord.Interaction) -> None:
        """Add or remove the Raider role from the clicking member.

        Defers the response immediately so a fast double-click can't trigger
        an InteractionResponded error on the late-arriving send.
        """
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Geht nur im Server, nicht in DMs.", ephemeral=True
            )
            return
        # Acknowledge within Discord's 3s window; do the role work afterwards
        # and answer via followup (idempotent on double-clicks).
        await interaction.response.defer(ephemeral=True)
        role = guild.get_role(RAIDER_ROLE_ID)
        if role is None:
            await interaction.followup.send(
                "Raider-Rolle nicht gefunden. Bitte einen Mod fragen.",
                ephemeral=True,
            )
            return
        member = interaction.user
        if not isinstance(member, discord.Member):
            member = guild.get_member(member.id)
            if member is None:
                try:
                    member = await guild.fetch_member(interaction.user.id)
                except discord.HTTPException:
                    await interaction.followup.send(
                        "Konnte dich im Server nicht finden.", ephemeral=True
                    )
                    return
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Panel: Raider-Toggle")
                msg = "🛡️ Raider-Rolle **entfernt**."
            else:
                await member.add_roles(role, reason="Panel: Raider-Toggle")
                msg = "🛡️ Raider-Rolle **hinzugefügt**."
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ Mir fehlt die Berechtigung. Bot braucht *Manage Roles* "
                "und muss eine Rolle ÜBER der Raider-Rolle haben.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            logger.warning(
                "[WoWCog] Raider-Toggle für %s fehlgeschlagen: %s",
                interaction.user.id,
                exc,
            )
            await interaction.followup.send(
                "❌ Discord-Fehler beim Rollen-Update.", ephemeral=True
            )
            return
        await interaction.followup.send(msg, ephemeral=True)


class PanelSearchSubView(discord.ui.View):
    """Sub-menu for the 🔎 Search section: crafter lookup, char-whois or
    member-lookup (Discord user → their claimed chars)."""

    def __init__(self, cog: WoWCog) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        # Decorator buttons (crafter, whois) are registered by
        # super().__init__ — items added here keep stable child indices
        # for them: [crafter, whois, lookup, dismiss].
        lookup_btn = discord.ui.Button(
            label="Chars eines Members", style=discord.ButtonStyle.secondary
        )
        lookup_btn.callback = self._open_member_lookup
        self.add_item(lookup_btn)
        _add_dismiss_button(self)

    @discord.ui.button(label="Crafter suchen", style=discord.ButtonStyle.primary)
    async def crafter(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(PanelCraftingSearchModal(self.cog))

    @discord.ui.button(label="Char nachschlagen", style=discord.ButtonStyle.secondary)
    async def whois(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(PanelWhoisModal(self.cog))

    async def _open_member_lookup(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(PanelMemberSearchModal(self.cog))


async def _render_member_claims(cog: WoWCog, user_id: int, display_name: str) -> str:
    """Render the claimed chars of a Discord user, main-first with rank+level."""
    claims = await cog.data.claims_for_user(user_id)
    if not claims:
        return f"**{display_name}** hat keine Chars geclaimt."
    snapshot = await cog.data.get_snapshot()
    enriched = [(c, snapshot.get(c.character_key)) for c in claims]
    # Best in-game rank first, then highest level → likely the main on top.
    enriched.sort(
        key=lambda pair: (
            (
                pair[1].guild_rank
                if pair[1] is not None and pair[1].guild_rank is not None
                else 99
            ),
            -(pair[1].level if pair[1] is not None else 0),
            pair[0].character_name.casefold(),
        )
    )
    lines = [f"**Chars von {display_name}** (nach Rang sortiert):"]
    for claim, roster in enriched:
        status = "bestätigt" if claim.status == "verified" else "ungeprüft"
        if roster is not None:
            rank = cog._rank_label(roster.guild_rank)
            ghost = " 🕯️" if roster.is_ghost else ""
            lines.append(
                f"- {cog._format_roster_line(roster)} — "
                f"**{rank}**{ghost} ({status})"
            )
        else:
            lines.append(
                f"- **{claim.character_name}** ({status}, nicht mehr im Roster)"
            )
    return "\n".join(lines)


class PanelMemberSearchModal(discord.ui.Modal):
    """Reverse whois via name search: type a Discord name, get the member's
    claimed chars.

    A UserSelect dropdown was useless on a 1000+-member server (only shows
    a handful of cached members initially), so this searches server-side
    via ``guild.query_members`` (username + nickname prefix match, works
    without the privileged members intent).
    """

    def __init__(self, cog: WoWCog) -> None:
        super().__init__(title="Member suchen")
        self.cog = cog
        self.query = discord.ui.TextInput(
            label="Discord-Name",
            placeholder="z. B. Gerrit",
            min_length=2,
            max_length=32,
        )
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        q = str(self.query.value).strip()
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Geht nur im Server, nicht in DMs.", ephemeral=True
            )
            return
        # query_members can take >3s on a cold gateway — defer first.
        await interaction.response.defer(ephemeral=True)
        try:
            matches = await guild.query_members(query=q, limit=10)
        except (discord.HTTPException, asyncio.TimeoutError) as exc:
            logger.warning("[WoWCog] Member-Suche '%s' fehlgeschlagen: %s", q, exc)
            await interaction.followup.send(
                "❌ Member-Suche fehlgeschlagen, bitte nochmal versuchen.",
                ephemeral=True,
            )
            return
        if not matches:
            await interaction.followup.send(
                f"Kein Member gefunden, der mit **{q}** beginnt. "
                "Gesucht wird nach Username und Server-Nickname.",
                ephemeral=True,
            )
            return
        if len(matches) == 1:
            target = matches[0]
            content = await _render_member_claims(
                self.cog, target.id, target.display_name
            )
            await interaction.followup.send(content, ephemeral=True)
            return
        view = _MemberMatchSelectView(self.cog, matches)
        await interaction.followup.send(
            f"**{len(matches)}** Member gefunden — wähle aus:",
            view=view,
            ephemeral=True,
        )


class _MemberMatchSelectView(discord.ui.View):
    """Disambiguation step when the name search returns several members."""

    def __init__(self, cog: WoWCog, matches: list[discord.Member]) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.matches = {str(m.id): m for m in matches}
        select_options = [
            discord.SelectOption(
                label=m.display_name[:100],
                value=str(m.id),
                description=str(m)[:100],
            )
            for m in matches[:25]
        ]
        select = discord.ui.Select(
            placeholder="Member auswählen",
            min_values=1,
            max_values=1,
            options=select_options,
        )
        select.callback = self._on_pick
        self._select = select
        self.add_item(select)
        _add_dismiss_button(self)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        target = self.matches.get(self._select.values[0])
        if target is None:
            await interaction.response.edit_message(
                content="Member nicht mehr verfügbar.", view=None
            )
            return
        content = await _render_member_claims(self.cog, target.id, target.display_name)
        # Keep the select so several members can be checked in a row.
        await interaction.response.edit_message(content=content, view=self)


class PanelWhoisModal(discord.ui.Modal):
    """Free-text wrapper around ``build_whois_view`` — same payload as
    ``/wow whois`` but reachable from the panel hub."""

    def __init__(self, cog: WoWCog) -> None:
        super().__init__(title="Char nachschlagen")
        self.cog = cog
        self.query = discord.ui.TextInput(
            label="Charaktername",
            placeholder="z. B. Voidok",
            min_length=2,
            max_length=32,
        )
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = str(self.query.value).strip()
        view = await self.cog.build_whois_view(name, interaction.user.id)
        if view is None:
            await interaction.response.send_message(
                f"**{name}** ist nicht im aktuellen Roster.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(view=view, ephemeral=True)


class _WhoisLayoutView(discord.ui.LayoutView):
    """Components-V2 profile card for a roster char.

    Layout: a single ``Container`` with title, info, professions, and
    (if claimed) twin links. The twin buttons swap the view in-place
    via ``edit_message`` so the user navigates without spawning more
    ephemerals.
    """

    def __init__(
        self,
        cog: "WoWCog",
        member: RosterMember,
        claim: "CharacterClaim | None",
        gear,
        professions: list[CharacterProfession],
        twins: list[tuple["CharacterClaim", "RosterMember | None"]],
        viewer_id: int,
        viewer_is_owner: bool,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.viewer_id = viewer_id
        self._member_key = member.character_key

        emoji_name = CLASS_EMOJI_NAMES.get(member.class_id, "")
        class_emoji = cog.bot.data.get("emojis", {}).get(emoji_name, "")
        class_name = CLASS_NAMES_DE.get(member.class_id) or "Klasse?"
        race_name = RACE_NAMES_DE.get(member.race_id) or ""
        title_line = (
            f"## {class_emoji} **{member.name}** — "
            f"Level {member.level} {race_name} {class_name}"
        ).strip()

        owner_str = f"<@{claim.discord_user_id}>" if claim else "_Nicht geclaimed_"
        status_str = "🕯️ Tot (Geist)" if member.is_ghost else "🟢 Lebt"
        ilvl_str = cog._format_item_level(gear.average_item_level) if gear else "—"
        rank_str = cog._rank_label(member.guild_rank)
        info_line = (
            f"**Owner:** {owner_str}  ·  **Rang:** {rank_str}  ·  "
            f"**Status:** {status_str}  ·  **Ø iLvl:** {ilvl_str}"
        )

        items: list[discord.ui.Item] = [
            discord.ui.TextDisplay(title_line),
            discord.ui.Separator(),
            discord.ui.TextDisplay(info_line),
        ]

        if professions:
            berufe_text = "**Berufe:**\n" + cog.format_profession_slots_short(
                professions
            )
            items.append(discord.ui.Separator())
            if viewer_is_owner:
                pflegen_btn = discord.ui.Button(
                    label="🛠️ Berufe pflegen",
                    style=discord.ButtonStyle.primary,
                )
                pflegen_btn.callback = self._open_professions
                items.append(
                    discord.ui.Section(
                        discord.ui.TextDisplay(berufe_text),
                        accessory=pflegen_btn,
                    )
                )
            else:
                items.append(discord.ui.TextDisplay(berufe_text))

        if twins:
            items.append(discord.ui.Separator())
            owner_mention = f"<@{claim.discord_user_id}>"
            lines = [f"**Andere Chars von {owner_mention}** (nach Rang sortiert):"]
            for tclaim, tmember in twins:
                if tmember is not None:
                    detail = (
                        f"{cog._rank_label(tmember.guild_rank)}, Level {tmember.level}"
                    )
                    ghost = " 🕯️" if tmember.is_ghost else ""
                else:
                    detail = "nicht mehr im Roster"
                    ghost = ""
                lines.append(f"- **{tclaim.character_name}** — {detail}{ghost}")
            if len(twins) > 5:
                lines.append(f"_(Buttons unten: erste 5 von {len(twins)})_")
            items.append(discord.ui.TextDisplay("\n".join(lines)))

        self.add_item(discord.ui.Container(*items))

        # V2 LayoutView only accepts layout-type items at the top level
        # (ActionRow, Section, Container, TextDisplay, …). Plain Buttons
        # must be wrapped in an ActionRow.
        if twins:
            twin_row = discord.ui.ActionRow()
            for tclaim, _tmember in twins[:5]:
                twin_btn = discord.ui.Button(
                    label=tclaim.character_name[:80],
                    style=discord.ButtonStyle.secondary,
                )
                twin_btn.callback = self._make_twin_callback(tclaim.character_name)
                twin_row.add_item(twin_btn)
            self.add_item(twin_row)

        dismiss_row = discord.ui.ActionRow()
        close_btn = discord.ui.Button(
            label="✖ Schließen", style=discord.ButtonStyle.secondary
        )

        async def _close(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            try:
                await interaction.delete_original_response()
            except discord.NotFound:
                pass

        close_btn.callback = _close
        dismiss_row.add_item(close_btn)
        self.add_item(dismiss_row)

    def _make_twin_callback(self, char_name: str):
        async def cb(interaction: discord.Interaction) -> None:
            new_view = await self.cog.build_whois_view(char_name, self.viewer_id)
            if new_view is None:
                # V2 messages can't carry plain content via edit_message;
                # spawn a fresh ephemeral for the error.
                await interaction.response.send_message(
                    f"**{char_name}** ist nicht im aktuellen Roster.",
                    ephemeral=True,
                )
                return
            await interaction.response.edit_message(view=new_view)

        return cb

    async def _open_professions(self, interaction: discord.Interaction) -> None:
        # PanelMyCharsView is a V1 view with an embed; we can't edit the
        # whois LayoutView message to a V1 payload (Discord locks the V2
        # flag on the message). Open as a fresh ephemeral instead.
        view = PanelMyCharsView(self.cog, interaction.user.id)
        await view._load()
        for c in view.claims:
            if c.character_key == self._member_key:
                view.selected_claim = c
                break
        view._rebuild()
        embed = await view._build_embed()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class GBankRequestModal(discord.ui.Modal):
    """Step 1 of a guild-bank request: free-text 'what do you need?'."""

    def __init__(self, cog: WoWCog) -> None:
        super().__init__(title="Gildenbank-Anfrage")
        self.cog = cog
        self.request = discord.ui.TextInput(
            label="Was wird angefragt?",
            placeholder="z. B. 20x Runenstoff, Mondstoff, ...",
            style=discord.TextStyle.paragraph,
            min_length=2,
            max_length=300,
        )
        self.add_item(self.request)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bank_chars = await self.cog.data.list_bank_characters()
        if not bank_chars:
            await interaction.response.send_message(
                "Aktuell sind keine Gildenbank-Chars eingetragen. Bitte wende "
                "dich an einen Mod.",
                ephemeral=True,
            )
            return
        view = GBankBankCharSelectView(
            self.cog,
            interaction.user.id,
            str(self.request.value).strip(),
            bank_chars,
        )
        await interaction.response.send_message(
            "Auf welchem Bank-Char liegen die Items?",
            view=view,
            ephemeral=True,
        )


class GBankBankCharSelectView(discord.ui.View):
    """Step 2: pick the bank char that holds the requested items."""

    def __init__(
        self,
        cog: WoWCog,
        requester_id: int,
        request_text: str,
        bank_chars: list["BankCharacter"],
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.requester_id = requester_id
        self.request_text = request_text
        self.bank_chars = bank_chars
        self.add_item(GBankBankCharSelect(self))
        _add_dismiss_button(self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "Diese Auswahl gehört nicht dir.", ephemeral=True
        )
        return False


class GBankBankCharSelect(discord.ui.Select):
    def __init__(self, parent: GBankBankCharSelectView) -> None:
        self.parent_view = parent
        options = [
            discord.SelectOption(
                label=bank.character_name[:100], value=bank.character_key
            )
            for bank in parent.bank_chars[:25]
        ]
        super().__init__(
            placeholder="Bank-Char auswählen",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        bank_key = self.values[0]
        bank_name = next(
            (
                bank.character_name
                for bank in self.parent_view.bank_chars
                if bank.character_key == bank_key
            ),
            bank_key,
        )
        claims = await self.parent_view.cog.data.claims_for_user(
            self.parent_view.requester_id
        )
        if len(claims) == 1:
            # Respond first (3s interaction window), then do the slow DM work.
            await interaction.response.edit_message(
                content="✅ Anfrage gesendet.", view=None
            )
            await self.parent_view.cog.submit_gbank_request(
                interaction.user,
                claims[0].character_name,
                bank_key,
                bank_name,
                self.parent_view.request_text,
            )
            return
        view = GBankOwnCharSelectView(
            self.parent_view.cog,
            self.parent_view.requester_id,
            self.parent_view.request_text,
            bank_key,
            bank_name,
            claims,
        )
        await interaction.response.edit_message(
            content="Für welchen deiner Chars ist die Anfrage?", view=view
        )


class GBankOwnCharSelectView(discord.ui.View):
    """Step 3 (only if requester has multiple claims): pick own char."""

    def __init__(
        self,
        cog: WoWCog,
        requester_id: int,
        request_text: str,
        bank_key: str,
        bank_name: str,
        claims: list[CharacterClaim],
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.requester_id = requester_id
        self.request_text = request_text
        self.bank_key = bank_key
        self.bank_name = bank_name
        self.claims = claims
        self.add_item(GBankOwnCharSelect(self))
        _add_dismiss_button(self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "Diese Auswahl gehört nicht dir.", ephemeral=True
        )
        return False


class GBankOwnCharSelect(discord.ui.Select):
    def __init__(self, parent: GBankOwnCharSelectView) -> None:
        self.parent_view = parent
        options = [
            discord.SelectOption(
                label=claim.character_name[:100], value=claim.character_name
            )
            for claim in parent.claims[:25]
        ]
        super().__init__(
            placeholder="Dein Char",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        # Respond first (3s interaction window), then do the slow DM work.
        await interaction.response.edit_message(
            content="✅ Anfrage gesendet.", view=None
        )
        await self.parent_view.cog.submit_gbank_request(
            interaction.user,
            self.values[0],
            self.parent_view.bank_key,
            self.parent_view.bank_name,
            self.parent_view.request_text,
        )


class PanelMyCharsView(discord.ui.View):
    """Ephemeral overview-and-control panel for the user's claimed chars.

    Replaces the old "Meine Chars" plain-text response with an embed +
    char-select + inline action buttons. Consolidates what previously
    required five separate slash commands (``/wow claims mine``,
    ``/wow crafting mine``, ``/wow crafting learned``, ``/wow cooldowns
    mine``, ``/wow claim-release``) into one surface.
    """

    def __init__(self, cog: WoWCog, owner_user_id: int) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_user_id = owner_user_id
        self.claims: list[CharacterClaim] = []
        self.selected_claim: CharacterClaim | None = None
        self._char_select: discord.ui.Select | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_user_id:
            return True
        await interaction.response.send_message(
            "Diese Ansicht gehört nicht dir.", ephemeral=True
        )
        return False

    async def _load(self) -> None:
        self.claims = await self.cog.data.claims_for_user(self.owner_user_id)
        if self.claims:
            # Always re-resolve to the freshly-fetched object by key, even
            # when the previously-selected character is still present -
            # otherwise a DB flag flipped via an action button (e.g. the
            # crafting-notify toggle) would never show up after refresh()
            # since the view kept pointing at the stale pre-flip object.
            key = self.selected_claim.character_key if self.selected_claim else None
            match = next((c for c in self.claims if c.character_key == key), None)
            self.selected_claim = match or self.claims[0]
        else:
            self.selected_claim = None

    def _rebuild(self) -> None:
        """Recreate items based on the current claim list + selection."""
        self.clear_items()
        if len(self.claims) > 1:
            self._char_select = _MyCharsSelect(self, self.claims)
            self.add_item(self._char_select)
        if self.selected_claim is not None:
            self.add_item(_MyCharsActionButton(self, "profession", "🛠️ Berufe pflegen"))
            self.add_item(
                _MyCharsActionButton(self, "addon_import", "📥 Per Addon importieren")
            )
            self.add_item(_MyCharsActionButton(self, "recipes", "📖 Rezepte pflegen"))
            notify_label = (
                "🔕 Crafting-Anfragen aus"
                if self.selected_claim.crafting_notify
                else "🔔 Crafting-Anfragen an"
            )
            self.add_item(_MyCharsActionButton(self, "notify_toggle", notify_label))
            self.add_item(
                _MyCharsActionButton(
                    self,
                    "release",
                    "🗑️ Claim freigeben",
                    style=discord.ButtonStyle.danger,
                )
            )
        self.add_item(
            _MyCharsActionButton(
                self,
                "claim_new",
                "➕ Neuen Char claimen",
                style=discord.ButtonStyle.success,
            )
        )
        _add_dismiss_button(self)

    async def _build_embed(self) -> discord.Embed:
        if not self.claims:
            return discord.Embed(
                title="Deine Chars",
                description=(
                    "Du hast noch keinen Black-Lotus-Char verbunden. "
                    "Klick **Neuen Char claimen** unten."
                ),
                color=0x9B59B6,
            )
        embed = discord.Embed(title="🪷 Deine Chars", color=0x2ECC71)
        for claim in self.claims:
            member = await self.cog.data.find_roster_member_by_name(
                claim.character_name
            )
            lines: list[str] = []
            if member is not None:
                race = RACE_NAMES_DE.get(member.race_id, "")
                klass = CLASS_NAMES_DE.get(member.class_id, "")
                lines.append(f"Level **{member.level}** {race} {klass}".strip())
                if member.is_ghost:
                    lines.append("🕯️ Tot (Geist)")
            lines.append(
                "✅ bestätigt"
                if claim.status == "verified"
                else "🕐 wartet auf Bestätigung"
            )
            lines.append(
                "🔔 Crafting-Anfragen: An"
                if claim.crafting_notify
                else "🔕 Crafting-Anfragen: Aus"
            )
            gear = await self.cog.data.gear_snapshot(claim.character_key)
            if gear is not None:
                lines.append(
                    f"Ø iLvl **{self.cog._format_item_level(gear.average_item_level)}**"
                )
            professions = await self.cog.data.professions_for_character(
                claim.character_key
            )
            if professions:
                prof_lines = [
                    f"{self.cog._profession_name(p.profession_id)} **{p.skill_level}**"
                    for p in professions
                ]
                lines.append("Berufe: " + ", ".join(prof_lines))
            cooldowns = await self.cog.data.cooldowns_for_character(claim.character_key)
            if cooldowns:
                now = datetime.now(timezone.utc)
                cd_lines = []
                for cd in cooldowns:
                    try:
                        ready = datetime.fromisoformat(cd.ready_at)
                    except ValueError:
                        continue
                    if ready.tzinfo is None:
                        ready = ready.replace(tzinfo=timezone.utc)
                    if ready <= now:
                        cd_lines.append(f"⏳ {cd.last_spell_name}: **jetzt bereit**")
                    else:
                        delta = ready - now
                        hours = int(delta.total_seconds() // 3600)
                        minutes = int((delta.total_seconds() % 3600) // 60)
                        cd_lines.append(
                            f"⏳ {cd.last_spell_name}: ready in {hours}h {minutes:02d}m"
                        )
                lines.extend(cd_lines)
            marker = (
                "▶ "
                if (
                    self.selected_claim is not None
                    and claim.character_key == self.selected_claim.character_key
                    and len(self.claims) > 1
                )
                else ""
            )
            embed.add_field(
                name=f"{marker}{claim.character_name}",
                value="\n".join(lines) or "—",
                inline=False,
            )
        if self.selected_claim is not None and len(self.claims) > 1:
            embed.set_footer(
                text=f"Aktionen wirken auf {self.selected_claim.character_name}."
            )
        return embed

    async def send(self, interaction: discord.Interaction) -> None:
        await self._load()
        self._rebuild()
        embed = await self._build_embed()
        await interaction.response.send_message(embed=embed, view=self, ephemeral=True)

    async def refresh(self, interaction: discord.Interaction) -> None:
        await self._load()
        self._rebuild()
        embed = await self._build_embed()
        await interaction.response.edit_message(embed=embed, view=self)


class _MyCharsSelect(discord.ui.Select):
    def __init__(self, parent: PanelMyCharsView, claims: list[CharacterClaim]) -> None:
        self.parent_view = parent
        options = []
        for claim in claims[:25]:
            options.append(
                discord.SelectOption(
                    label=claim.character_name[:100],
                    value=claim.character_key,
                    default=(
                        parent.selected_claim is not None
                        and claim.character_key == parent.selected_claim.character_key
                    ),
                )
            )
        super().__init__(
            placeholder="Char auswählen",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        new_key = self.values[0]
        match = next(
            (c for c in self.parent_view.claims if c.character_key == new_key), None
        )
        if match is None:
            await self.parent_view.refresh(interaction)
            return
        self.parent_view.selected_claim = match
        await self.parent_view.refresh(interaction)


class _MyCharsActionButton(discord.ui.Button):
    def __init__(
        self,
        parent: PanelMyCharsView,
        action: str,
        label: str,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    ) -> None:
        super().__init__(label=label, style=style)
        self.parent_view = parent
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        await getattr(self.parent_view.cog, f"_my_chars_{self.action}")(
            interaction, self.parent_view
        )


DISCORD_MESSAGE_LIMIT = 1900  # Below Discord's 2000-char baseline for safety.
CONTINUATION_SUFFIX = " *(Forts.)*"


def _pack_digest_sections_into_chunks(
    sections: list[tuple[str | None, list[str]]],
    limit: int = DISCORD_MESSAGE_LIMIT,
) -> list[str]:
    """Pack ordered digest sections into Discord-postable message chunks.

    Each section is ``(header, body_lines)``. ``header=None`` marks a
    headerless block (opener, closer) whose body is emitted verbatim.

    Rules:
    * A section is kept together in one chunk if it fits.
    * Multiple small sections may share a chunk, separated by a blank line.
    * A section larger than ``limit`` is split internally — every
      continuation chunk repeats the header with ``" *(Forts.)*"`` appended,
      so readers always know what section they're looking at.
    * The chunker never splits a single line.
    """

    def _section_lines(header: str | None, body: list[str]) -> list[str]:
        return ([header] if header is not None else []) + list(body)

    def _cost(lines: list[str]) -> int:
        # Each joined line contributes len(line) + 1 (the \n separator).
        return sum(len(line) + 1 for line in lines)

    # Pass 1: expand any oversized section into multiple fitting sub-sections,
    # marking continuations with the Forts. suffix.
    expanded: list[tuple[str | None, list[str]]] = []
    for header, body in sections:
        if _cost(_section_lines(header, body)) <= limit:
            expanded.append((header, list(body)))
            continue
        if header is None:
            # Headerless block — break body into runs that each fit.
            current_body: list[str] = []
            current_size = 0
            for line in body:
                line_cost = len(line) + 1
                if current_body and current_size + line_cost > limit:
                    expanded.append((None, current_body))
                    current_body = []
                    current_size = 0
                current_body.append(line)
                current_size += line_cost
            if current_body:
                expanded.append((None, current_body))
            continue
        cont_header = f"{header}{CONTINUATION_SUFFIX}"
        first_part = True
        current_body = []
        current_size = len(header) + 1
        for line in body:
            line_cost = len(line) + 1
            if current_body and current_size + line_cost > limit:
                expanded.append((header if first_part else cont_header, current_body))
                first_part = False
                current_body = []
                current_size = len(cont_header) + 1
            current_body.append(line)
            current_size += line_cost
        if current_body:
            expanded.append((header if first_part else cont_header, current_body))

    # Pass 2: pack expanded sections into chunks, separating with a blank line.
    chunks: list[str] = []
    current_lines: list[str] = []
    current_size = 0
    for header, body in expanded:
        section_lines = _section_lines(header, body)
        prefix = [""] if current_lines else []
        added_cost = _cost(prefix + section_lines)
        if current_size + added_cost <= limit:
            current_lines.extend(prefix + section_lines)
            current_size += added_cost
        else:
            if current_lines:
                chunks.append("\n".join(current_lines))
            current_lines = list(section_lines)
            current_size = _cost(section_lines)
    if current_lines:
        chunks.append("\n".join(current_lines))
    return chunks


def _format_panel_claim_result(result: ClaimResult, requested_name: str) -> str:
    if result.reason == "not_found":
        return (
            f"**{requested_name}** wurde im aktuellen Black-Lotus-Roster "
            "nicht gefunden. Stimmt die Schreibweise?"
        )
    if result.claim is None:
        return "Der Charakter konnte gerade nicht verbunden werden. Versuch es gleich nochmal."
    if result.reason == "taken":
        return (
            f"**{result.claim.character_name}** ist bereits mit einem "
            "anderen Discord-Account verbunden."
        )
    if result.reason == "already_own":
        status = (
            "bestätigt ✅"
            if result.claim.status == "verified"
            else "wartet auf Bestätigung 🕐"
        )
        return f"**{result.claim.character_name}** hast du bereits verbunden — Status: *{status}*"
    warning = (
        "" if result.review_posted else "\nOffi-Review konnte nicht gepostet werden."
    )
    return f"**{result.claim.character_name}** wurde verbunden und wartet auf Bestätigung durch einen Moderator. 🪷{warning}"


def _format_panel_profession_result(cog: WoWCog, result: CraftingProfileResult) -> str:
    if result.reason == "unknown_profession":
        return "Dieser Beruf ist unbekannt."
    if result.reason == "not_claimed":
        return "Dieser Charakter ist nicht verbunden."
    if result.reason == "forbidden":
        return "Du darfst diesen Charakter nicht bearbeiten."
    if result.reason == "invalid_skill":
        return "Skill muss zwischen 1 und 300 liegen."
    if result.reason == "primary_limit":
        return "Dieser Charakter hat bereits zwei Hauptberufe gepflegt."
    return f"Gespeichert: {cog.format_profession(result.profession)}"


class ClaimReviewView(discord.ui.View):
    def __init__(self, cog: WoWCog) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    async def _ensure_mod(self, interaction: discord.Interaction) -> bool:
        permissions = getattr(interaction.user, "guild_permissions", None)
        if permissions and permissions.manage_guild:
            return True
        await interaction.response.send_message(
            "❌ Du hast keine Berechtigung für diese Claim-Prüfung.",
            ephemeral=True,
        )
        return False

    async def _claim_for_message(
        self, interaction: discord.Interaction
    ) -> CharacterClaim | None:
        message = interaction.message
        if message is None:
            return None
        return await self.cog.data.get_claim_by_review_message(message.id)

    @discord.ui.button(
        label="Bestätigen",
        style=discord.ButtonStyle.success,
        custom_id="wow_claim_review:verify",
    )
    async def verify(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        if not await self._ensure_mod(interaction):
            return
        claim = await self._claim_for_message(interaction)
        if claim is None:
            await interaction.response.send_message(
                "❌ Dieser Claim ist nicht mehr aktiv.", ephemeral=True
            )
            return
        await self.cog.data.verify_claim(claim.character_key, interaction.user.id)
        # Verified claim now entitles the owner to the guild role.
        await self.cog.reconcile_guild_role_for(claim.discord_user_id)
        await self.cog.update_claim_review_message(interaction, claim, "verified")

    @discord.ui.button(
        label="Ablehnen",
        style=discord.ButtonStyle.danger,
        custom_id="wow_claim_review:reject",
    )
    async def reject(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        if not await self._ensure_mod(interaction):
            return
        claim = await self._claim_for_message(interaction)
        if claim is None:
            await interaction.response.send_message(
                "❌ Dieser Claim ist nicht mehr aktiv.", ephemeral=True
            )
            return
        await self.cog.data.remove_claim(claim.character_key)
        # Rejecting removes a claim — re-check the owner's role entitlement.
        await self.cog.reconcile_guild_role_for(claim.discord_user_id)
        await self.cog.update_claim_review_message(interaction, claim, "rejected")


def _norm(value: str) -> str:
    return " ".join(str(value).casefold().split())
