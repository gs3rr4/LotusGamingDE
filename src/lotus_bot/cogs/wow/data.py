import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiosqlite

from lotus_bot.log_setup import get_logger

logger = get_logger(__name__)


@dataclass
class RosterMember:
    character_key: str
    character_id: int | None
    name: str
    realm_slug: str
    level: int
    class_id: int | None
    race_id: int | None
    faction: str
    guild_rank: int | None
    is_ghost: bool = False


@dataclass
class CharacterClaim:
    character_key: str
    character_name: str
    realm_slug: str
    discord_user_id: int
    status: str
    claimed_at: str
    verified_at: str | None
    verified_by: int | None
    review_message_id: int | None
    crafting_notify: bool = True


@dataclass
class BankCharacter:
    character_key: str
    character_name: str
    added_by: int
    added_at: str


@dataclass
class CharacterProfession:
    character_key: str
    character_name: str
    realm_slug: str
    discord_user_id: int
    profession_id: str
    skill_level: int
    specialization: str | None
    updated_at: str


@dataclass
class CharacterKnownRecipe:
    character_key: str
    character_name: str
    realm_slug: str
    discord_user_id: int
    spell_id: str
    profession_id: str
    learned_at: str


@dataclass
class RecipeLearningEvent:
    character_key: str
    character_name: str
    realm_slug: str
    discord_user_id: int
    spell_id: str
    profession_id: str
    rarity: str
    points: int
    created_at: str


@dataclass
class CharacterGearSnapshot:
    character_key: str
    average_item_level: float
    item_count: int
    updated_at: str


@dataclass
class GearMilestoneEvent:
    character_key: str
    character_name: str
    realm_slug: str
    discord_user_id: int | None
    average_item_level: float
    threshold: int
    created_at: str
    points: int


@dataclass
class ProfessionSkillEvent:
    character_key: str
    character_name: str
    realm_slug: str
    discord_user_id: int | None
    profession_id: str
    threshold: int
    skill_level: int
    points: int
    created_at: str


@dataclass
class Cooldown:
    character_key: str
    character_name: str
    realm_slug: str
    discord_user_id: int | None
    cooldown_group: str
    last_spell_id: str
    last_spell_name: str
    used_at: str
    ready_at: str


class WoWData:
    """SQLite storage for WoW guild settings, snapshots, and milestones."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.db: aiosqlite.Connection | None = None
        self._init_done = False

    async def _get_db(self) -> aiosqlite.Connection:
        if self.db is None:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            self.db = await aiosqlite.connect(self.db_path)
            await self.db.execute("PRAGMA journal_mode=WAL")
        return self.db

    async def init_db(self) -> None:
        if self._init_done:
            return
        db = await self._get_db()
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS roster_snapshot (
                character_key TEXT PRIMARY KEY,
                character_id INTEGER,
                name TEXT NOT NULL,
                realm_slug TEXT NOT NULL,
                level INTEGER NOT NULL,
                class_id INTEGER,
                race_id INTEGER,
                faction TEXT,
                guild_rank INTEGER,
                is_ghost INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """)
        # Frozen day-over-day baseline for the daily digest diff. ``roster_snapshot``
        # is now refreshed hourly (so freshly-joined chars become claimable within
        # the hour), which would otherwise destroy the digest's "yesterday vs today"
        # comparison. The digest reads this table instead; it is only rewritten by
        # the daily scan. Same columns as ``roster_snapshot``.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS roster_digest_baseline (
                character_key TEXT PRIMARY KEY,
                character_id INTEGER,
                name TEXT NOT NULL,
                realm_slug TEXT NOT NULL,
                level INTEGER NOT NULL,
                class_id INTEGER,
                race_id INTEGER,
                faction TEXT,
                guild_rank INTEGER,
                is_ghost INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS milestone_events (
                character_key TEXT NOT NULL,
                level INTEGER NOT NULL,
                announced_at TEXT NOT NULL,
                PRIMARY KEY(character_key, level)
            )
            """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS death_events (
                character_key TEXT PRIMARY KEY,
                recorded_at TEXT NOT NULL
            )
            """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS officer_note_events (
                character_key TEXT PRIMARY KEY,
                recorded_at TEXT NOT NULL
            )
            """)
        # Tracks the first time we ever saw a character_key in any roster
        # snapshot. INSERT OR IGNORE on each replace_snapshot — never
        # overwritten, so chars who leave + rejoin keep their original date.
        # Limitation: for chars present at first bot scan, this equals the
        # bot-tracking-start date, not their actual guild-join date.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS roster_first_seen (
                character_key TEXT PRIMARY KEY,
                first_seen_at TEXT NOT NULL
            )
            """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS character_claims (
                character_key TEXT PRIMARY KEY,
                character_name TEXT NOT NULL,
                realm_slug TEXT NOT NULL,
                discord_user_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                claimed_at TEXT NOT NULL,
                verified_at TEXT,
                verified_by INTEGER,
                review_message_id INTEGER
            )
            """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS character_professions (
                character_key TEXT NOT NULL,
                profession_id TEXT NOT NULL,
                skill_level INTEGER NOT NULL,
                specialization TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(character_key, profession_id)
            )
            """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS character_known_recipes (
                character_key TEXT NOT NULL,
                spell_id TEXT NOT NULL,
                profession_id TEXT NOT NULL,
                learned_at TEXT NOT NULL,
                PRIMARY KEY(character_key, spell_id)
            )
            """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS recipe_learning_events (
                character_key TEXT NOT NULL,
                spell_id TEXT NOT NULL,
                profession_id TEXT NOT NULL,
                rarity TEXT NOT NULL,
                points INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                announced_at TEXT,
                awarded_at TEXT,
                PRIMARY KEY(character_key, spell_id)
            )
            """)
        await db.execute("DROP TABLE IF EXISTS character_reputation_snapshot")
        await db.execute("DROP TABLE IF EXISTS reputation_events")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS character_gear_snapshot (
                character_key TEXT PRIMARY KEY,
                average_item_level REAL NOT NULL,
                item_count INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS gear_milestone_events (
                character_key TEXT NOT NULL,
                threshold INTEGER NOT NULL,
                average_item_level REAL NOT NULL,
                points INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                announced_at TEXT,
                awarded_at TEXT,
                PRIMARY KEY(character_key, threshold)
            )
            """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS profession_skill_milestone_events (
                character_key TEXT NOT NULL,
                profession_id TEXT NOT NULL,
                threshold INTEGER NOT NULL,
                skill_level INTEGER NOT NULL,
                points INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                announced_at TEXT,
                awarded_at TEXT,
                PRIMARY KEY(character_key, profession_id, threshold)
            )
            """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS profession_cooldowns (
                character_key TEXT NOT NULL,
                cooldown_group TEXT NOT NULL,
                last_spell_id TEXT NOT NULL,
                last_spell_name TEXT NOT NULL,
                used_at TEXT NOT NULL,
                ready_at TEXT NOT NULL,
                PRIMARY KEY(character_key, cooldown_group)
            )
            """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bank_characters (
                character_key TEXT PRIMARY KEY,
                character_name TEXT NOT NULL,
                added_by INTEGER NOT NULL,
                added_at TEXT NOT NULL
            )
            """)
        await self._ensure_column(
            "gear_milestone_events", "points", "INTEGER NOT NULL DEFAULT 0"
        )
        await self._ensure_column("gear_milestone_events", "awarded_at", "TEXT")
        await self._ensure_column(
            "roster_snapshot", "is_ghost", "INTEGER NOT NULL DEFAULT 0"
        )
        # Default ON (opt-out): the guild lead wants max discoverability for
        # the Marketplace crafting-ping feature - SQLite backfills existing
        # rows to 1 automatically, no separate migration needed.
        await self._ensure_column(
            "character_claims", "crafting_notify", "INTEGER NOT NULL DEFAULT 1"
        )
        # Seed the digest baseline from the existing live snapshot exactly once,
        # on upgrade of a database that predates the baseline table. Without this
        # the first daily scan after deploy would diff against an empty baseline
        # and announce the entire guild as "new". Skips on a fresh install (both
        # tables empty) and never re-seeds once the baseline holds rows.
        cur = await db.execute("SELECT COUNT(*) FROM roster_digest_baseline")
        baseline_count = (await cur.fetchone())[0]
        if baseline_count == 0:
            await db.execute("""
                INSERT INTO roster_digest_baseline (
                    character_key, character_id, name, realm_slug, level, class_id,
                    race_id, faction, guild_rank, is_ghost, updated_at
                )
                SELECT character_key, character_id, name, realm_slug, level, class_id,
                       race_id, faction, guild_rank, is_ghost, updated_at
                  FROM roster_snapshot
                """)
        await db.commit()
        self._init_done = True
        logger.info("[WoWData] SQLite database initialized.")

    async def _ensure_column(self, table: str, column: str, definition: str) -> None:
        db = await self._get_db()
        cur = await db.execute(f"PRAGMA table_info({table})")
        columns = {row[1] for row in await cur.fetchall()}
        if column not in columns:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()
            self.db = None
            self._init_done = False

    async def get_setting(self, key: str) -> str | None:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self.init_db()
        db = await self._get_db()
        await db.execute(
            """
            INSERT INTO settings(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        await db.commit()

    async def get_snapshot(self) -> dict[str, RosterMember]:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute("""
            SELECT character_key, character_id, name, realm_slug, level, class_id,
                   race_id, faction, guild_rank, is_ghost
              FROM roster_snapshot
            """)
        rows = await cur.fetchall()
        return {
            row[0]: RosterMember(
                character_key=row[0],
                character_id=row[1],
                name=row[2],
                realm_slug=row[3],
                level=row[4],
                class_id=row[5],
                race_id=row[6],
                faction=row[7] or "",
                guild_rank=row[8],
                is_ghost=bool(row[9]),
            )
            for row in rows
        }

    async def ghost_members(self) -> list[RosterMember]:
        """Return roster members currently flagged as dead/ghost.

        Sorted by level descending then name. Officers can use this to
        clean up dead characters that are still in the guild roster.
        """
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute("""
            SELECT character_key, character_id, name, realm_slug, level, class_id,
                   race_id, faction, guild_rank, is_ghost
              FROM roster_snapshot
             WHERE is_ghost = 1
             ORDER BY level DESC, lower(name)
            """)
        rows = await cur.fetchall()
        return [
            RosterMember(
                character_key=row[0],
                character_id=row[1],
                name=row[2],
                realm_slug=row[3],
                level=row[4],
                class_id=row[5],
                race_id=row[6],
                faction=row[7] or "",
                guild_rank=row[8],
                is_ghost=bool(row[9]),
            )
            for row in rows
        ]

    @staticmethod
    async def _write_roster_table(
        db: aiosqlite.Connection,
        table: str,
        members: list[RosterMember],
        now: str,
    ) -> None:
        """Replace all rows of a roster-shaped table with ``members``."""
        await db.execute(f"DELETE FROM {table}")
        for member in members:
            await db.execute(
                f"""
                INSERT INTO {table}(
                    character_key, character_id, name, realm_slug, level, class_id,
                    race_id, faction, guild_rank, is_ghost, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    member.character_key,
                    member.character_id,
                    member.name,
                    member.realm_slug,
                    member.level,
                    member.class_id,
                    member.race_id,
                    member.faction,
                    member.guild_rank,
                    int(member.is_ghost),
                    now,
                ),
            )

    @staticmethod
    async def _record_first_seen(
        db: aiosqlite.Connection, members: list[RosterMember], now: str
    ) -> None:
        for member in members:
            # Record first-seen timestamp once per character_key. Subsequent
            # writes are no-ops — the original date persists across guild
            # leaves and re-joins.
            await db.execute(
                "INSERT OR IGNORE INTO roster_first_seen("
                "character_key, first_seen_at) VALUES (?, ?)",
                (member.character_key, now),
            )

    async def replace_snapshot(self, members: list[RosterMember]) -> None:
        """Write the daily scan result to the live snapshot AND the digest baseline.

        The daily scan is the only caller that should move the frozen digest
        baseline forward, so both tables are updated here in lockstep. The
        hourly live refresh uses :meth:`refresh_live_snapshot`, which leaves
        the baseline untouched.
        """
        await self.init_db()
        db = await self._get_db()
        now = datetime.utcnow().isoformat()
        await self._write_roster_table(db, "roster_snapshot", members, now)
        await self._write_roster_table(db, "roster_digest_baseline", members, now)
        await self._record_first_seen(db, members, now)
        await db.commit()

    async def refresh_live_snapshot(self, members: list[RosterMember]) -> None:
        """Refresh only the live snapshot (hourly), preserving known ghost state.

        The hourly roster pull skips the per-character profile endpoint, so the
        fetched members never carry a fresh ``is_ghost`` flag. To avoid wiping
        the death state set by the last daily profile refresh, ghosts already
        recorded in the snapshot keep their flag. The digest baseline is NOT
        touched here.
        """
        await self.init_db()
        db = await self._get_db()
        now = datetime.utcnow().isoformat()
        cur = await db.execute(
            "SELECT character_key FROM roster_snapshot WHERE is_ghost = 1"
        )
        known_ghosts = {row[0] for row in await cur.fetchall()}
        for member in members:
            if member.character_key in known_ghosts:
                member.is_ghost = True
        await self._write_roster_table(db, "roster_snapshot", members, now)
        await self._record_first_seen(db, members, now)
        await db.commit()

    async def get_digest_baseline(self) -> dict[str, RosterMember]:
        """Return the frozen day-over-day baseline used by the daily digest."""
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute("""
            SELECT character_key, character_id, name, realm_slug, level, class_id,
                   race_id, faction, guild_rank, is_ghost
              FROM roster_digest_baseline
            """)
        rows = await cur.fetchall()
        return {
            row[0]: RosterMember(
                character_key=row[0],
                character_id=row[1],
                name=row[2],
                realm_slug=row[3],
                level=row[4],
                class_id=row[5],
                race_id=row[6],
                faction=row[7] or "",
                guild_rank=row[8],
                is_ghost=bool(row[9]),
            )
            for row in rows
        }

    async def first_seen_at(self, character_key: str) -> str | None:
        """Return the ISO timestamp of when this character was first seen
        in any roster snapshot, or ``None`` if never recorded."""
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            "SELECT first_seen_at FROM roster_first_seen WHERE character_key = ?",
            (character_key,),
        )
        row = await cur.fetchone()
        return row[0] if row else None

    async def milestone_exists(self, character_key: str, level: int) -> bool:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            "SELECT 1 FROM milestone_events WHERE character_key = ? AND level = ?",
            (character_key, level),
        )
        return await cur.fetchone() is not None

    async def record_milestone(self, character_key: str, level: int) -> None:
        await self.init_db()
        db = await self._get_db()
        await db.execute(
            """
            INSERT OR IGNORE INTO milestone_events(character_key, level, announced_at)
            VALUES (?, ?, ?)
            """,
            (character_key, level, datetime.utcnow().isoformat()),
        )
        await db.commit()

    async def death_exists(self, character_key: str) -> bool:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            "SELECT 1 FROM death_events WHERE character_key = ?",
            (character_key,),
        )
        return await cur.fetchone() is not None

    async def record_death(self, character_key: str) -> None:
        await self.init_db()
        db = await self._get_db()
        await db.execute(
            """
            INSERT OR IGNORE INTO death_events(character_key, recorded_at)
            VALUES (?, ?)
            """,
            (character_key, datetime.utcnow().isoformat()),
        )
        await db.commit()

    async def officer_note_exists(self, character_key: str) -> bool:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            "SELECT 1 FROM officer_note_events WHERE character_key = ?",
            (character_key,),
        )
        return await cur.fetchone() is not None

    async def record_officer_note(self, character_key: str) -> None:
        await self.init_db()
        db = await self._get_db()
        await db.execute(
            """
            INSERT OR IGNORE INTO officer_note_events(character_key, recorded_at)
            VALUES (?, ?)
            """,
            (character_key, datetime.utcnow().isoformat()),
        )
        await db.commit()

    async def member_count(self) -> int:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute("SELECT COUNT(*) FROM roster_snapshot")
        row = await cur.fetchone()
        return row[0] if row else 0

    async def last_scan_at(self) -> str | None:
        return await self.get_setting("last_scan_at")

    async def mark_scanned(self) -> None:
        await self.set_setting("last_scan_at", datetime.utcnow().isoformat())

    async def find_roster_member_by_name(self, name: str) -> RosterMember | None:
        """Lookup by name, preferring living characters over ghosts.

        After a HC death + reroll, both an old (ghost) and a new (alive)
        character with the same name can sit in the snapshot. The
        ``ORDER BY is_ghost ASC`` sorts the alive one to the top so
        ``/wow whois`` and the claim flow always pick the active char.
        """
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            SELECT character_key, character_id, name, realm_slug, level, class_id,
                   race_id, faction, guild_rank, is_ghost
              FROM roster_snapshot
             WHERE lower(name) = lower(?)
             ORDER BY is_ghost ASC, character_id DESC
             LIMIT 1
            """,
            (name.strip(),),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return RosterMember(
            character_key=row[0],
            character_id=row[1],
            name=row[2],
            realm_slug=row[3],
            level=row[4],
            class_id=row[5],
            race_id=row[6],
            faction=row[7] or "",
            guild_rank=row[8],
            is_ghost=bool(row[9]),
        )

    async def unclaimed_roster_members(self) -> list[RosterMember]:
        """Snapshot members without a claim, alphabetically sorted."""
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute("""
            SELECT s.character_key, s.character_id, s.name, s.realm_slug, s.level,
                   s.class_id, s.race_id, s.faction, s.guild_rank, s.is_ghost
              FROM roster_snapshot s
              LEFT JOIN character_claims c ON c.character_key = s.character_key
             WHERE c.character_key IS NULL
             ORDER BY lower(s.name)
            """)
        rows = await cur.fetchall()
        return [
            RosterMember(
                character_key=row[0],
                character_id=row[1],
                name=row[2],
                realm_slug=row[3],
                level=row[4],
                class_id=row[5],
                race_id=row[6],
                faction=row[7] or "",
                guild_rank=row[8],
                is_ghost=bool(row[9]),
            )
            for row in rows
        ]

    async def get_claim(self, character_key: str) -> CharacterClaim | None:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            SELECT character_key, character_name, realm_slug, discord_user_id, status,
                   claimed_at, verified_at, verified_by, review_message_id,
                   crafting_notify
              FROM character_claims
             WHERE character_key = ?
            """,
            (character_key,),
        )
        row = await cur.fetchone()
        return _claim_from_row(row) if row else None

    async def get_claim_by_name(self, name: str) -> CharacterClaim | None:
        """Lookup by name, preferring claims for currently-alive characters.

        After HC death + reroll, two claims with the same name can exist
        (old key for the dead char, new key for the reroll). We rank the
        claims so the alive one wins:
            1. char has no ``death_events`` entry AND is not ghost in roster
            2. fallback: most recently claimed
        """
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            SELECT c.character_key, c.character_name, c.realm_slug, c.discord_user_id,
                   c.status, c.claimed_at, c.verified_at, c.verified_by,
                   c.review_message_id, c.crafting_notify
              FROM character_claims c
              LEFT JOIN roster_snapshot rs ON rs.character_key = c.character_key
              LEFT JOIN death_events d ON d.character_key = c.character_key
             WHERE lower(c.character_name) = lower(?)
             ORDER BY (d.character_key IS NULL
                       AND COALESCE(rs.is_ghost, 0) = 0) DESC,
                      c.claimed_at DESC
             LIMIT 1
            """,
            (name.strip(),),
        )
        row = await cur.fetchone()
        return _claim_from_row(row) if row else None

    async def create_claim(
        self, member: RosterMember, discord_user_id: int
    ) -> tuple[CharacterClaim, bool]:
        await self.init_db()
        existing = await self.get_claim(member.character_key)
        if existing:
            return existing, False

        db = await self._get_db()
        now = datetime.utcnow().isoformat()
        await db.execute(
            """
            INSERT INTO character_claims(
                character_key, character_name, realm_slug, discord_user_id, status,
                claimed_at, verified_at, verified_by, review_message_id
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
            """,
            (
                member.character_key,
                member.name,
                member.realm_slug,
                discord_user_id,
                "unverified",
                now,
            ),
        )
        await db.commit()
        claim = await self.get_claim(member.character_key)
        if claim is None:  # pragma: no cover - defensive
            raise RuntimeError("Claim creation failed")
        return claim, True

    async def set_claim_review_message(
        self, character_key: str, review_message_id: int
    ) -> None:
        await self.init_db()
        db = await self._get_db()
        await db.execute(
            """
            UPDATE character_claims
               SET review_message_id = ?
             WHERE character_key = ?
            """,
            (review_message_id, character_key),
        )
        await db.commit()

    async def verify_claim(self, character_key: str, reviewer_id: int) -> None:
        await self.init_db()
        db = await self._get_db()
        await db.execute(
            """
            UPDATE character_claims
               SET status = 'verified', verified_at = ?, verified_by = ?
             WHERE character_key = ?
            """,
            (datetime.utcnow().isoformat(), reviewer_id, character_key),
        )
        await db.commit()

    async def set_crafting_notify(self, character_key: str, enabled: bool) -> None:
        await self.init_db()
        db = await self._get_db()
        await db.execute(
            "UPDATE character_claims SET crafting_notify = ? WHERE character_key = ?",
            (int(bool(enabled)), character_key),
        )
        await db.commit()

    async def crafting_notify_map(self, character_keys: list[str]) -> dict[str, bool]:
        """Batch lookup for the Marketplace ping filter - one query, not N+1.

        Keys not present in ``character_claims`` (shouldn't normally happen
        for a claimed char, but be defensive) are simply absent from the
        result; callers should default to ``True`` (opt-out semantics) via
        ``.get(key, True)``.
        """
        if not character_keys:
            return {}
        await self.init_db()
        db = await self._get_db()
        placeholders = ",".join("?" for _ in character_keys)
        cur = await db.execute(
            f"SELECT character_key, crafting_notify FROM character_claims "
            f"WHERE character_key IN ({placeholders})",
            character_keys,
        )
        rows = await cur.fetchall()
        return {row[0]: bool(row[1]) for row in rows}

    async def get_claim_by_review_message(
        self, review_message_id: int
    ) -> CharacterClaim | None:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            SELECT character_key, character_name, realm_slug, discord_user_id, status,
                   claimed_at, verified_at, verified_by, review_message_id,
                   crafting_notify
              FROM character_claims
             WHERE review_message_id = ?
            """,
            (review_message_id,),
        )
        row = await cur.fetchone()
        return _claim_from_row(row) if row else None

    async def remove_claim(self, character_key: str) -> None:
        await self.init_db()
        db = await self._get_db()
        await db.execute(
            "DELETE FROM character_claims WHERE character_key = ?",
            (character_key,),
        )
        await db.commit()

    async def release_claim(self, character_key: str, discord_user_id: int) -> bool:
        claim = await self.get_claim(character_key)
        if not claim or claim.discord_user_id != discord_user_id:
            return False
        await self.remove_claim(character_key)
        return True

    async def claims_for_user(self, discord_user_id: int) -> list[CharacterClaim]:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            SELECT character_key, character_name, realm_slug, discord_user_id, status,
                   claimed_at, verified_at, verified_by, review_message_id,
                   crafting_notify
              FROM character_claims
             WHERE discord_user_id = ?
             ORDER BY lower(character_name)
            """,
            (discord_user_id,),
        )
        rows = await cur.fetchall()
        return [_claim_from_row(row) for row in rows]

    async def list_claims(self, status: str = "all") -> list[CharacterClaim]:
        await self.init_db()
        db = await self._get_db()
        query = """
            SELECT character_key, character_name, realm_slug, discord_user_id, status,
                   claimed_at, verified_at, verified_by, review_message_id,
                   crafting_notify
              FROM character_claims
        """
        params: tuple[str, ...] = ()
        if status != "all":
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY lower(character_name)"
        cur = await db.execute(query, params)
        rows = await cur.fetchall()
        return [_claim_from_row(row) for row in rows]

    async def role_eligible_user_ids(self) -> set[int]:
        """Discord user IDs entitled to the bot-managed guild role.

        A user qualifies when they own at least one verified claim for a
        character that is *currently in the guild roster* and alive. Claims
        for characters that have left the guild (or died) therefore stop
        granting the role on the next reconcile — without the claim row having
        to be deleted, so a character that rejoins restores the role
        automatically. Unverified (pending-review) claims never count.
        """
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute("""
            SELECT DISTINCT c.discord_user_id
              FROM character_claims c
              JOIN roster_snapshot rs ON rs.character_key = c.character_key
             WHERE c.status = 'verified' AND rs.is_ghost = 0
            """)
        rows = await cur.fetchall()
        return {int(row[0]) for row in rows}

    async def add_bank_character(
        self, character_key: str, character_name: str, added_by: int
    ) -> None:
        await self.init_db()
        db = await self._get_db()
        await db.execute(
            """
            INSERT INTO bank_characters (character_key, character_name, added_by, added_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(character_key) DO UPDATE SET
                character_name = excluded.character_name
            """,
            (character_key, character_name, added_by, datetime.utcnow().isoformat()),
        )
        await db.commit()

    async def remove_bank_character(self, character_key: str) -> bool:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            "DELETE FROM bank_characters WHERE character_key = ?",
            (character_key,),
        )
        await db.commit()
        return cur.rowcount > 0

    async def list_bank_characters(self) -> list[BankCharacter]:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute("""
            SELECT character_key, character_name, added_by, added_at
              FROM bank_characters
             ORDER BY lower(character_name)
            """)
        rows = await cur.fetchall()
        return [
            BankCharacter(
                character_key=row[0],
                character_name=row[1],
                added_by=row[2],
                added_at=row[3],
            )
            for row in rows
        ]

    async def is_bank_character(self, character_key: str) -> bool:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            "SELECT 1 FROM bank_characters WHERE character_key = ?",
            (character_key,),
        )
        return await cur.fetchone() is not None

    async def set_character_profession(
        self,
        claim: CharacterClaim,
        profession_id: str,
        skill_level: int,
        specialization: str | None = None,
    ) -> CharacterProfession:
        if skill_level < 1 or skill_level > 300:
            raise ValueError("skill_level must be between 1 and 300")
        await self.init_db()
        db = await self._get_db()
        now = datetime.utcnow().isoformat()
        cleaned_specialization = (
            specialization.strip()
            if specialization and specialization.strip()
            else None
        )
        await db.execute(
            """
            INSERT INTO character_professions(
                character_key, profession_id, skill_level, specialization, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(character_key, profession_id)
            DO UPDATE SET
                skill_level = excluded.skill_level,
                specialization = excluded.specialization,
                updated_at = excluded.updated_at
            """,
            (
                claim.character_key,
                profession_id,
                skill_level,
                cleaned_specialization,
                now,
            ),
        )
        await db.commit()
        profession = await self.get_character_profession(
            claim.character_key, profession_id
        )
        if profession is None:  # pragma: no cover - defensive
            raise RuntimeError("Profession update failed")
        return profession

    async def get_character_profession(
        self, character_key: str, profession_id: str
    ) -> CharacterProfession | None:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            SELECT c.character_key, c.character_name, c.realm_slug, c.discord_user_id,
                   p.profession_id, p.skill_level, p.specialization, p.updated_at
              FROM character_professions p
              JOIN character_claims c ON c.character_key = p.character_key
             WHERE p.character_key = ? AND p.profession_id = ?
            """,
            (character_key, profession_id),
        )
        row = await cur.fetchone()
        return _profession_from_row(row) if row else None

    async def remove_character_profession(
        self, character_key: str, profession_id: str
    ) -> bool:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            DELETE FROM character_professions
             WHERE character_key = ? AND profession_id = ?
            """,
            (character_key, profession_id),
        )
        await db.commit()
        return cur.rowcount > 0

    async def professions_for_user(
        self, discord_user_id: int
    ) -> list[CharacterProfession]:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            SELECT c.character_key, c.character_name, c.realm_slug, c.discord_user_id,
                   p.profession_id, p.skill_level, p.specialization, p.updated_at
              FROM character_professions p
              JOIN character_claims c ON c.character_key = p.character_key
             WHERE c.discord_user_id = ?
             ORDER BY lower(c.character_name), p.profession_id
            """,
            (discord_user_id,),
        )
        rows = await cur.fetchall()
        return [_profession_from_row(row) for row in rows]

    async def list_professions(
        self, profession_id: str | None = None
    ) -> list[CharacterProfession]:
        await self.init_db()
        db = await self._get_db()
        query = """
            SELECT c.character_key, c.character_name, c.realm_slug, c.discord_user_id,
                   p.profession_id, p.skill_level, p.specialization, p.updated_at
              FROM character_professions p
              JOIN character_claims c ON c.character_key = p.character_key
        """
        params: tuple[str, ...] = ()
        if profession_id:
            query += " WHERE p.profession_id = ?"
            params = (profession_id,)
        query += (
            " ORDER BY p.profession_id, p.skill_level DESC, lower(c.character_name)"
        )
        cur = await db.execute(query, params)
        rows = await cur.fetchall()
        return [_profession_from_row(row) for row in rows]

    async def professions_for_character(
        self, character_key: str
    ) -> list[CharacterProfession]:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            SELECT c.character_key, c.character_name, c.realm_slug, c.discord_user_id,
                   p.profession_id, p.skill_level, p.specialization, p.updated_at
              FROM character_professions p
              JOIN character_claims c ON c.character_key = p.character_key
             WHERE p.character_key = ?
             ORDER BY p.profession_id
            """,
            (character_key,),
        )
        rows = await cur.fetchall()
        return [_profession_from_row(row) for row in rows]

    async def find_crafters(
        self, profession_id: str, minimum_skill: int
    ) -> list[CharacterProfession]:
        """Active crafters for the given profession with at least the skill.

        Excludes characters that are flagged ghost in the current roster
        snapshot OR have a row in ``death_events`` — those are dead and
        can't actually craft anything. Professions stay isolated by
        ``character_key`` so a reroll with the same name starts fresh.
        """
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            SELECT c.character_key, c.character_name, c.realm_slug, c.discord_user_id,
                   p.profession_id, p.skill_level, p.specialization, p.updated_at
              FROM character_professions p
              JOIN character_claims c ON c.character_key = p.character_key
              JOIN roster_snapshot rs ON rs.character_key = p.character_key
              LEFT JOIN death_events d ON d.character_key = p.character_key
             WHERE p.profession_id = ? AND p.skill_level >= ?
               AND rs.is_ghost = 0
               AND d.character_key IS NULL
             ORDER BY p.skill_level DESC, lower(c.character_name)
            """,
            (profession_id, minimum_skill),
        )
        rows = await cur.fetchall()
        return [_profession_from_row(row) for row in rows]

    async def add_known_recipes(
        self,
        character_key: str,
        profession_id: str,
        spell_ids: list[str],
    ) -> int:
        return len(
            await self.add_known_recipes_returning_inserted(
                character_key, profession_id, spell_ids
            )
        )

    async def add_known_recipes_returning_inserted(
        self,
        character_key: str,
        profession_id: str,
        spell_ids: list[str],
    ) -> list[str]:
        if not spell_ids:
            return []
        await self.init_db()
        db = await self._get_db()
        now = datetime.utcnow().isoformat()
        inserted: list[str] = []
        for spell_id in spell_ids:
            cur = await db.execute(
                """
                INSERT OR IGNORE INTO character_known_recipes(
                    character_key, spell_id, profession_id, learned_at
                ) VALUES (?, ?, ?, ?)
                """,
                (character_key, spell_id, profession_id, now),
            )
            if cur.rowcount > 0:
                inserted.append(spell_id)
        await db.commit()
        return inserted

    async def record_recipe_learning_event(
        self,
        character_key: str,
        spell_id: str,
        profession_id: str,
        rarity: str,
        points: int,
    ) -> bool:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            INSERT OR IGNORE INTO recipe_learning_events(
                character_key, spell_id, profession_id, rarity, points, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                character_key,
                spell_id,
                profession_id,
                rarity,
                points,
                datetime.utcnow().isoformat(),
            ),
        )
        await db.commit()
        return cur.rowcount > 0

    async def pending_recipe_learning_events(self) -> list[RecipeLearningEvent]:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute("""
            SELECT c.character_key, c.character_name, c.realm_slug, c.discord_user_id,
                   e.spell_id, e.profession_id, e.rarity, e.points, e.created_at
              FROM recipe_learning_events e
              JOIN character_claims c ON c.character_key = e.character_key
             WHERE e.announced_at IS NULL
             ORDER BY e.points DESC, e.created_at, lower(c.character_name)
            """)
        rows = await cur.fetchall()
        return [_recipe_event_from_row(row) for row in rows]

    async def pending_award_retries_recipe_learning(
        self,
    ) -> list[RecipeLearningEvent]:
        """Recipe events that were announced but never awarded.

        Used by the scan's retry loop so a single ChampionCog hiccup doesn't
        cost a user their points permanently.
        """
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute("""
            SELECT c.character_key, c.character_name, c.realm_slug, c.discord_user_id,
                   e.spell_id, e.profession_id, e.rarity, e.points, e.created_at
              FROM recipe_learning_events e
              JOIN character_claims c ON c.character_key = e.character_key
             WHERE e.announced_at IS NOT NULL
               AND e.awarded_at IS NULL
               AND e.points > 0
             ORDER BY e.created_at
            """)
        rows = await cur.fetchall()
        return [_recipe_event_from_row(row) for row in rows]

    async def mark_recipe_learning_announced(
        self, character_key: str, spell_id: str
    ) -> None:
        await self.init_db()
        db = await self._get_db()
        now = datetime.utcnow().isoformat()
        await db.execute(
            """
            UPDATE recipe_learning_events
               SET announced_at = COALESCE(announced_at, ?)
             WHERE character_key = ? AND spell_id = ?
            """,
            (now, character_key, spell_id),
        )
        await db.commit()

    async def mark_recipe_learning_awarded(
        self, character_key: str, spell_id: str
    ) -> bool:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            UPDATE recipe_learning_events
               SET awarded_at = ?
             WHERE character_key = ? AND spell_id = ? AND awarded_at IS NULL
            """,
            (datetime.utcnow().isoformat(), character_key, spell_id),
        )
        await db.commit()
        return cur.rowcount > 0

    async def unmark_recipe_learning_awarded(
        self, character_key: str, spell_id: str
    ) -> None:
        """Roll back awarded_at so a failed ChampionCog call can be retried.

        Pairs with :meth:`mark_recipe_learning_awarded` — the cog reserves
        the slot via CAS-mark, attempts the Champion-update, and unmarks
        on failure so the next scan's retry loop picks the row up again.
        """
        await self.init_db()
        db = await self._get_db()
        await db.execute(
            """
            UPDATE recipe_learning_events
               SET awarded_at = NULL
             WHERE character_key = ? AND spell_id = ?
            """,
            (character_key, spell_id),
        )
        await db.commit()

    async def gear_snapshot(self, character_key: str) -> CharacterGearSnapshot | None:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            SELECT character_key, average_item_level, item_count, updated_at
              FROM character_gear_snapshot
             WHERE character_key = ?
            """,
            (character_key,),
        )
        row = await cur.fetchone()
        return _gear_snapshot_from_row(row) if row else None

    async def set_gear_snapshot(
        self, character_key: str, average_item_level: float, item_count: int
    ) -> None:
        await self.init_db()
        db = await self._get_db()
        await db.execute(
            """
            INSERT INTO character_gear_snapshot(
                character_key, average_item_level, item_count, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(character_key)
            DO UPDATE SET
                average_item_level = excluded.average_item_level,
                item_count = excluded.item_count,
                updated_at = excluded.updated_at
            """,
            (
                character_key,
                float(average_item_level),
                int(item_count),
                datetime.utcnow().isoformat(),
            ),
        )
        await db.commit()

    async def gear_milestone_exists(self, character_key: str, threshold: int) -> bool:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            SELECT 1 FROM gear_milestone_events
             WHERE character_key = ? AND threshold = ?
            """,
            (character_key, threshold),
        )
        return await cur.fetchone() is not None

    async def record_gear_milestone(
        self,
        character_key: str,
        threshold: int,
        average_item_level: float,
        points: int,
    ) -> bool:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            INSERT OR IGNORE INTO gear_milestone_events(
                character_key, threshold, average_item_level, points, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                character_key,
                int(threshold),
                float(average_item_level),
                int(points),
                datetime.utcnow().isoformat(),
            ),
        )
        await db.commit()
        return cur.rowcount > 0

    async def pending_gear_milestone_events(self) -> list[GearMilestoneEvent]:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute("""
            SELECT e.character_key,
                   COALESCE(c.character_name, s.name, e.character_key),
                   COALESCE(c.realm_slug, s.realm_slug, ''),
                   c.discord_user_id,
                   e.average_item_level,
                   e.threshold,
                   e.created_at,
                   e.points
              FROM gear_milestone_events e
              LEFT JOIN character_claims c ON c.character_key = e.character_key
              LEFT JOIN roster_snapshot s ON s.character_key = e.character_key
             WHERE e.announced_at IS NULL
             ORDER BY e.threshold DESC, e.created_at
            """)
        rows = await cur.fetchall()
        return [_gear_event_from_row(row) for row in rows]

    async def pending_award_retries_gear_milestone(
        self,
    ) -> list[GearMilestoneEvent]:
        """Gear-milestone events that were announced but never awarded."""
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute("""
            SELECT e.character_key,
                   COALESCE(c.character_name, s.name, e.character_key),
                   COALESCE(c.realm_slug, s.realm_slug, ''),
                   c.discord_user_id,
                   e.average_item_level,
                   e.threshold,
                   e.created_at,
                   e.points
              FROM gear_milestone_events e
              LEFT JOIN character_claims c ON c.character_key = e.character_key
              LEFT JOIN roster_snapshot s ON s.character_key = e.character_key
             WHERE e.announced_at IS NOT NULL
               AND e.awarded_at IS NULL
               AND e.points > 0
               AND c.discord_user_id IS NOT NULL
             ORDER BY e.created_at
            """)
        rows = await cur.fetchall()
        return [_gear_event_from_row(row) for row in rows]

    async def mark_gear_milestone_announced(
        self, character_key: str, threshold: int
    ) -> None:
        await self.init_db()
        db = await self._get_db()
        await db.execute(
            """
            UPDATE gear_milestone_events
               SET announced_at = COALESCE(announced_at, ?)
             WHERE character_key = ? AND threshold = ?
            """,
            (datetime.utcnow().isoformat(), character_key, threshold),
        )
        await db.commit()

    async def mark_gear_milestone_awarded(
        self, character_key: str, threshold: int
    ) -> bool:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            UPDATE gear_milestone_events
               SET awarded_at = ?
             WHERE character_key = ?
               AND threshold = ?
               AND awarded_at IS NULL
            """,
            (datetime.utcnow().isoformat(), character_key, threshold),
        )
        await db.commit()
        return cur.rowcount > 0

    async def unmark_gear_milestone_awarded(
        self, character_key: str, threshold: int
    ) -> None:
        """Roll back awarded_at so a failed ChampionCog call can be retried."""
        await self.init_db()
        db = await self._get_db()
        await db.execute(
            """
            UPDATE gear_milestone_events
               SET awarded_at = NULL
             WHERE character_key = ? AND threshold = ?
            """,
            (character_key, threshold),
        )
        await db.commit()

    # ---- profession-skill milestones ----

    async def skill_milestone_exists(
        self, character_key: str, profession_id: str, threshold: int
    ) -> bool:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            SELECT 1 FROM profession_skill_milestone_events
             WHERE character_key = ? AND profession_id = ? AND threshold = ?
            """,
            (character_key, profession_id, int(threshold)),
        )
        return await cur.fetchone() is not None

    async def record_skill_milestone(
        self,
        character_key: str,
        profession_id: str,
        threshold: int,
        skill_level: int,
        points: int,
    ) -> bool:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            INSERT OR IGNORE INTO profession_skill_milestone_events(
                character_key, profession_id, threshold, skill_level, points, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                character_key,
                profession_id,
                int(threshold),
                int(skill_level),
                int(points),
                datetime.utcnow().isoformat(),
            ),
        )
        await db.commit()
        return cur.rowcount > 0

    async def pending_skill_milestone_events(self) -> list[ProfessionSkillEvent]:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute("""
            SELECT e.character_key,
                   COALESCE(c.character_name, s.name, e.character_key),
                   COALESCE(c.realm_slug, s.realm_slug, ''),
                   c.discord_user_id,
                   e.profession_id,
                   e.threshold,
                   e.skill_level,
                   e.points,
                   e.created_at
              FROM profession_skill_milestone_events e
              LEFT JOIN character_claims c ON c.character_key = e.character_key
              LEFT JOIN roster_snapshot s ON s.character_key = e.character_key
             WHERE e.announced_at IS NULL
             ORDER BY e.threshold DESC, e.created_at
            """)
        rows = await cur.fetchall()
        return [_skill_event_from_row(row) for row in rows]

    async def pending_award_retries_skill_milestone(
        self,
    ) -> list[ProfessionSkillEvent]:
        """Skill-milestone events that were announced but never awarded."""
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute("""
            SELECT e.character_key,
                   COALESCE(c.character_name, s.name, e.character_key),
                   COALESCE(c.realm_slug, s.realm_slug, ''),
                   c.discord_user_id,
                   e.profession_id,
                   e.threshold,
                   e.skill_level,
                   e.points,
                   e.created_at
              FROM profession_skill_milestone_events e
              LEFT JOIN character_claims c ON c.character_key = e.character_key
              LEFT JOIN roster_snapshot s ON s.character_key = e.character_key
             WHERE e.announced_at IS NOT NULL
               AND e.awarded_at IS NULL
               AND e.points > 0
               AND c.discord_user_id IS NOT NULL
             ORDER BY e.created_at
            """)
        rows = await cur.fetchall()
        return [_skill_event_from_row(row) for row in rows]

    async def mark_skill_milestone_announced(
        self, character_key: str, profession_id: str, threshold: int
    ) -> None:
        await self.init_db()
        db = await self._get_db()
        await db.execute(
            """
            UPDATE profession_skill_milestone_events
               SET announced_at = COALESCE(announced_at, ?)
             WHERE character_key = ? AND profession_id = ? AND threshold = ?
            """,
            (
                datetime.utcnow().isoformat(),
                character_key,
                profession_id,
                int(threshold),
            ),
        )
        await db.commit()

    async def mark_skill_milestone_awarded(
        self, character_key: str, profession_id: str, threshold: int
    ) -> bool:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            UPDATE profession_skill_milestone_events
               SET awarded_at = ?
             WHERE character_key = ?
               AND profession_id = ?
               AND threshold = ?
               AND awarded_at IS NULL
            """,
            (
                datetime.utcnow().isoformat(),
                character_key,
                profession_id,
                int(threshold),
            ),
        )
        await db.commit()
        return cur.rowcount > 0

    async def unmark_skill_milestone_awarded(
        self, character_key: str, profession_id: str, threshold: int
    ) -> None:
        """Roll back awarded_at so a failed ChampionCog call can be retried."""
        await self.init_db()
        db = await self._get_db()
        await db.execute(
            """
            UPDATE profession_skill_milestone_events
               SET awarded_at = NULL
             WHERE character_key = ?
               AND profession_id = ?
               AND threshold = ?
            """,
            (character_key, profession_id, int(threshold)),
        )
        await db.commit()

    # ---- profession cooldowns ----

    async def set_cooldown(
        self,
        character_key: str,
        cooldown_group: str,
        last_spell_id: str,
        last_spell_name: str,
        used_at: str,
        ready_at: str,
    ) -> None:
        await self.init_db()
        db = await self._get_db()
        await db.execute(
            """
            INSERT INTO profession_cooldowns(
                character_key, cooldown_group, last_spell_id, last_spell_name,
                used_at, ready_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(character_key, cooldown_group)
            DO UPDATE SET
                last_spell_id = excluded.last_spell_id,
                last_spell_name = excluded.last_spell_name,
                used_at = excluded.used_at,
                ready_at = excluded.ready_at
            """,
            (
                character_key,
                cooldown_group,
                last_spell_id,
                last_spell_name,
                used_at,
                ready_at,
            ),
        )
        await db.commit()

    async def cooldowns_for_character(self, character_key: str) -> list[Cooldown]:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            SELECT cd.character_key,
                   COALESCE(c.character_name, s.name, cd.character_key),
                   COALESCE(c.realm_slug, s.realm_slug, ''),
                   c.discord_user_id,
                   cd.cooldown_group,
                   cd.last_spell_id,
                   cd.last_spell_name,
                   cd.used_at,
                   cd.ready_at
              FROM profession_cooldowns cd
              LEFT JOIN character_claims c ON c.character_key = cd.character_key
              LEFT JOIN roster_snapshot s ON s.character_key = cd.character_key
             WHERE cd.character_key = ?
             ORDER BY cd.ready_at
            """,
            (character_key,),
        )
        rows = await cur.fetchall()
        return [_cooldown_from_row(row) for row in rows]

    async def cooldowns_for_user(self, discord_user_id: int) -> list[Cooldown]:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            SELECT cd.character_key,
                   c.character_name,
                   c.realm_slug,
                   c.discord_user_id,
                   cd.cooldown_group,
                   cd.last_spell_id,
                   cd.last_spell_name,
                   cd.used_at,
                   cd.ready_at
              FROM profession_cooldowns cd
              JOIN character_claims c ON c.character_key = cd.character_key
             WHERE c.discord_user_id = ?
             ORDER BY cd.ready_at
            """,
            (int(discord_user_id),),
        )
        rows = await cur.fetchall()
        return [_cooldown_from_row(row) for row in rows]

    async def cooldowns_ready_in_window(
        self, start_iso: str, end_iso: str
    ) -> list[Cooldown]:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            SELECT cd.character_key,
                   COALESCE(c.character_name, s.name, cd.character_key),
                   COALESCE(c.realm_slug, s.realm_slug, ''),
                   c.discord_user_id,
                   cd.cooldown_group,
                   cd.last_spell_id,
                   cd.last_spell_name,
                   cd.used_at,
                   cd.ready_at
              FROM profession_cooldowns cd
              LEFT JOIN character_claims c ON c.character_key = cd.character_key
              LEFT JOIN roster_snapshot s ON s.character_key = cd.character_key
             WHERE cd.ready_at >= ? AND cd.ready_at < ?
             ORDER BY cd.ready_at
            """,
            (start_iso, end_iso),
        )
        rows = await cur.fetchall()
        return [_cooldown_from_row(row) for row in rows]

    async def active_cooldown_count(self) -> int:
        """Count cooldowns that are still running (ready_at in the future)."""
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            "SELECT COUNT(*) FROM profession_cooldowns WHERE ready_at > ?",
            (datetime.utcnow().isoformat(),),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def remove_known_recipe(self, character_key: str, spell_id: str) -> bool:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            DELETE FROM character_known_recipes
             WHERE character_key = ? AND spell_id = ?
            """,
            (character_key, spell_id),
        )
        await db.commit()
        return cur.rowcount > 0

    async def known_recipe_spell_ids(self, character_key: str) -> set[str]:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            SELECT spell_id
              FROM character_known_recipes
             WHERE character_key = ?
            """,
            (character_key,),
        )
        rows = await cur.fetchall()
        return {str(row[0]) for row in rows}

    async def known_recipes_for_character(
        self, character_key: str
    ) -> list[CharacterKnownRecipe]:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            SELECT c.character_key, c.character_name, c.realm_slug, c.discord_user_id,
                   r.spell_id, r.profession_id, r.learned_at
              FROM character_known_recipes r
              JOIN character_claims c ON c.character_key = r.character_key
             WHERE r.character_key = ?
             ORDER BY r.profession_id, r.spell_id
            """,
            (character_key,),
        )
        rows = await cur.fetchall()
        return [_known_recipe_from_row(row) for row in rows]

    async def find_crafters_with_known_recipe(
        self,
        profession_id: str,
        minimum_skill: int,
        spell_id: str,
    ) -> list[CharacterProfession]:
        """Active crafters who explicitly learned ``spell_id``.

        Same death/ghost filter as :meth:`find_crafters` — dead chars
        don't show up even if their old data still references the recipe.
        """
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            SELECT c.character_key, c.character_name, c.realm_slug, c.discord_user_id,
                   p.profession_id, p.skill_level, p.specialization, p.updated_at
              FROM character_professions p
              JOIN character_claims c ON c.character_key = p.character_key
              JOIN character_known_recipes r
                ON r.character_key = p.character_key
               AND r.spell_id = ?
              JOIN roster_snapshot rs ON rs.character_key = p.character_key
              LEFT JOIN death_events d ON d.character_key = p.character_key
             WHERE p.profession_id = ? AND p.skill_level >= ?
               AND rs.is_ghost = 0
               AND d.character_key IS NULL
             ORDER BY p.skill_level DESC, lower(c.character_name)
            """,
            (spell_id, profession_id, minimum_skill),
        )
        rows = await cur.fetchall()
        return [_profession_from_row(row) for row in rows]


def _claim_from_row(row: tuple[Any, ...]) -> CharacterClaim:
    return CharacterClaim(
        character_key=row[0],
        character_name=row[1],
        realm_slug=row[2],
        discord_user_id=row[3],
        status=row[4],
        claimed_at=row[5],
        verified_at=row[6],
        verified_by=row[7],
        crafting_notify=bool(row[9]) if len(row) > 9 else True,
        review_message_id=row[8],
    )


def _profession_from_row(row: tuple[Any, ...]) -> CharacterProfession:
    return CharacterProfession(
        character_key=row[0],
        character_name=row[1],
        realm_slug=row[2],
        discord_user_id=row[3],
        profession_id=row[4],
        skill_level=row[5],
        specialization=row[6],
        updated_at=row[7],
    )


def _known_recipe_from_row(row: tuple[Any, ...]) -> CharacterKnownRecipe:
    return CharacterKnownRecipe(
        character_key=row[0],
        character_name=row[1],
        realm_slug=row[2],
        discord_user_id=row[3],
        spell_id=row[4],
        profession_id=row[5],
        learned_at=row[6],
    )


def _recipe_event_from_row(row: tuple[Any, ...]) -> RecipeLearningEvent:
    return RecipeLearningEvent(
        character_key=row[0],
        character_name=row[1],
        realm_slug=row[2],
        discord_user_id=row[3],
        spell_id=row[4],
        profession_id=row[5],
        rarity=row[6],
        points=row[7],
        created_at=row[8],
    )


def _gear_snapshot_from_row(row: tuple[Any, ...]) -> CharacterGearSnapshot:
    return CharacterGearSnapshot(
        character_key=row[0],
        average_item_level=float(row[1]),
        item_count=int(row[2]),
        updated_at=row[3],
    )


def _gear_event_from_row(row: tuple[Any, ...]) -> GearMilestoneEvent:
    return GearMilestoneEvent(
        character_key=row[0],
        character_name=row[1],
        realm_slug=row[2],
        discord_user_id=row[3],
        average_item_level=float(row[4]),
        threshold=int(row[5]),
        created_at=row[6],
        points=int(row[7]),
    )


def _skill_event_from_row(row: tuple[Any, ...]) -> ProfessionSkillEvent:
    return ProfessionSkillEvent(
        character_key=row[0],
        character_name=row[1],
        realm_slug=row[2],
        discord_user_id=row[3],
        profession_id=row[4],
        threshold=int(row[5]),
        skill_level=int(row[6]),
        points=int(row[7]),
        created_at=row[8],
    )


def _cooldown_from_row(row: tuple[Any, ...]) -> Cooldown:
    return Cooldown(
        character_key=row[0],
        character_name=row[1],
        realm_slug=row[2],
        discord_user_id=row[3],
        cooldown_group=row[4],
        last_spell_id=row[5],
        last_spell_name=row[6],
        used_at=row[7],
        ready_at=row[8],
    )


def parse_roster_member(raw: dict[str, Any]) -> RosterMember | None:
    """Convert a Battle.net roster entry into a stable local record."""
    character = raw.get("character") or {}
    name = character.get("name")
    realm_slug = (character.get("realm") or {}).get("slug")
    level = character.get("level")
    if not name or not realm_slug or level is None:
        return None

    character_id = character.get("id")
    is_ghost = bool(character.get("is_ghost") or raw.get("is_ghost"))
    character_key = (
        f"id:{character_id}"
        if character_id is not None
        else f"realm:{realm_slug}:name:{str(name).lower()}"
    )

    return RosterMember(
        character_key=character_key,
        character_id=character_id,
        name=str(name),
        realm_slug=str(realm_slug),
        level=int(level),
        class_id=(character.get("playable_class") or {}).get("id"),
        race_id=(character.get("playable_race") or {}).get("id"),
        faction=(character.get("faction") or {}).get("type") or "",
        guild_rank=raw.get("rank"),
        is_ghost=is_ghost,
    )
