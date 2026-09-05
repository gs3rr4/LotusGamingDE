"""SQLite storage for the Marketplace forum feature.

Separate database file from ``WoWData``/``DuoData``, same on-disk directory.
Structure and idioms mirror ``duo_data.py``: lazy connection, WAL journal,
idempotent :meth:`init_db`, ``ON CONFLICT DO UPDATE`` upserts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import aiosqlite

from lotus_bot.log_setup import get_logger

logger = get_logger(__name__)


@dataclass
class MarketplaceListing:
    thread_id: int
    poster_discord_user_id: int
    tag_kind: str  # 'biete' | 'suche'
    status: str  # 'open' | 'done'
    created_at: str


@dataclass
class MarketplaceCraftingRequest:
    thread_id: int
    requester_discord_user_id: int
    item_name: str
    item_key: str  # "item.<id>" or "enchant:<spell_id>"
    status: str  # 'open' | 'claimed'
    claimed_by_discord_user_id: int | None
    claimed_at: str | None
    created_at: str


class MarketplaceData:
    """SQLite storage for Marketplace listings, crafting requests and the
    ping-cooldown that guards the crafting-request auto-ping."""

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
            CREATE TABLE IF NOT EXISTS marketplace_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """)
        # Minimal by design: only enough to let the shared "Als erledigt"
        # button (one view instance for every listing thread) know who the
        # poster was, looked up via interaction.channel.id.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS marketplace_listings (
                thread_id INTEGER PRIMARY KEY,
                poster_discord_user_id INTEGER NOT NULL,
                tag_kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL
            )
            """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS marketplace_crafting_requests (
                thread_id INTEGER PRIMARY KEY,
                requester_discord_user_id INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                item_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                claimed_by_discord_user_id INTEGER,
                claimed_at TEXT,
                created_at TEXT NOT NULL
            )
            """)
        # Keyed on (requester, item) - not just requester - so pinging the
        # SAME crafters again for the SAME item is throttled, without
        # blocking a legitimate follow-up request for a different item.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS marketplace_ping_cooldowns (
                requester_discord_user_id INTEGER NOT NULL,
                item_key TEXT NOT NULL,
                last_request_at TEXT NOT NULL,
                PRIMARY KEY(requester_discord_user_id, item_key)
            )
            """)
        await db.commit()
        self._init_done = True
        logger.info("[MarketplaceData] SQLite database initialized.")

    async def get_setting(self, key: str) -> str | None:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            "SELECT value FROM marketplace_settings WHERE key = ?", (key,)
        )
        row = await cur.fetchone()
        return row[0] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self.init_db()
        db = await self._get_db()
        await db.execute(
            """
            INSERT INTO marketplace_settings(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        await db.commit()

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()
            self.db = None
            self._init_done = False

    # ---- generic listings ----

    async def create_listing(
        self, thread_id: int, poster_discord_user_id: int, tag_kind: str
    ) -> MarketplaceListing:
        await self.init_db()
        db = await self._get_db()
        now = datetime.utcnow().isoformat()
        await db.execute(
            """
            INSERT INTO marketplace_listings(
                thread_id, poster_discord_user_id, tag_kind, status, created_at
            ) VALUES (?, ?, ?, 'open', ?)
            """,
            (thread_id, poster_discord_user_id, tag_kind, now),
        )
        await db.commit()
        listing = await self.get_listing(thread_id)
        if listing is None:  # pragma: no cover - defensive
            raise RuntimeError("Listing creation failed")
        return listing

    async def get_listing(self, thread_id: int) -> MarketplaceListing | None:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            SELECT thread_id, poster_discord_user_id, tag_kind, status, created_at
              FROM marketplace_listings WHERE thread_id = ?
            """,
            (thread_id,),
        )
        row = await cur.fetchone()
        return _listing_from_row(row) if row else None

    async def set_listing_status(self, thread_id: int, status: str) -> None:
        await self.init_db()
        db = await self._get_db()
        await db.execute(
            "UPDATE marketplace_listings SET status = ? WHERE thread_id = ?",
            (status, thread_id),
        )
        await db.commit()

    # ---- crafting requests ----

    async def create_crafting_request(
        self,
        thread_id: int,
        requester_discord_user_id: int,
        item_name: str,
        item_key: str,
    ) -> MarketplaceCraftingRequest:
        await self.init_db()
        db = await self._get_db()
        now = datetime.utcnow().isoformat()
        await db.execute(
            """
            INSERT INTO marketplace_crafting_requests(
                thread_id, requester_discord_user_id, item_name, item_key,
                status, created_at
            ) VALUES (?, ?, ?, ?, 'open', ?)
            """,
            (thread_id, requester_discord_user_id, item_name, item_key, now),
        )
        await db.commit()
        request = await self.get_crafting_request(thread_id)
        if request is None:  # pragma: no cover - defensive
            raise RuntimeError("Crafting request creation failed")
        return request

    async def get_crafting_request(
        self, thread_id: int
    ) -> MarketplaceCraftingRequest | None:
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            SELECT thread_id, requester_discord_user_id, item_name, item_key,
                   status, claimed_by_discord_user_id, claimed_at, created_at
              FROM marketplace_crafting_requests WHERE thread_id = ?
            """,
            (thread_id,),
        )
        row = await cur.fetchone()
        return _crafting_request_from_row(row) if row else None

    async def claim_crafting_request(self, thread_id: int, claimer_id: int) -> bool:
        """Atomically claim an open request. ``True`` iff this call won the
        race - a second concurrent click on the claim button gets ``False``
        because SQLite serializes writes and only one ``UPDATE ... WHERE
        status = 'open'`` can affect the row."""
        await self.init_db()
        db = await self._get_db()
        cur = await db.execute(
            """
            UPDATE marketplace_crafting_requests
               SET status = 'claimed', claimed_by_discord_user_id = ?, claimed_at = ?
             WHERE thread_id = ? AND status = 'open'
            """,
            (claimer_id, datetime.utcnow().isoformat(), thread_id),
        )
        await db.commit()
        return cur.rowcount > 0

    # ---- ping cooldown ----

    async def check_and_record_ping_cooldown(
        self,
        requester_discord_user_id: int,
        item_key: str,
        *,
        cooldown_minutes: int = 30,
    ) -> bool:
        """Returns ``True`` (and records "now") if a ping should be sent for
        this (requester, item) pair; ``False`` if the same requester already
        pinged for the same item within the cooldown window. The thread
        itself is always created regardless of this result - only the
        auto-ping is gated."""
        await self.init_db()
        db = await self._get_db()
        now = datetime.utcnow()
        cur = await db.execute(
            """
            SELECT last_request_at FROM marketplace_ping_cooldowns
             WHERE requester_discord_user_id = ? AND item_key = ?
            """,
            (requester_discord_user_id, item_key),
        )
        row = await cur.fetchone()
        if row is not None:
            last = datetime.fromisoformat(row[0])
            if now - last < timedelta(minutes=cooldown_minutes):
                return False
        await db.execute(
            """
            INSERT INTO marketplace_ping_cooldowns(
                requester_discord_user_id, item_key, last_request_at
            ) VALUES (?, ?, ?)
            ON CONFLICT(requester_discord_user_id, item_key)
            DO UPDATE SET last_request_at = excluded.last_request_at
            """,
            (requester_discord_user_id, item_key, now.isoformat()),
        )
        await db.commit()
        return True


def _listing_from_row(row: tuple[Any, ...]) -> MarketplaceListing:
    return MarketplaceListing(
        thread_id=row[0],
        poster_discord_user_id=row[1],
        tag_kind=row[2],
        status=row[3],
        created_at=row[4],
    )


def _crafting_request_from_row(row: tuple[Any, ...]) -> MarketplaceCraftingRequest:
    return MarketplaceCraftingRequest(
        thread_id=row[0],
        requester_discord_user_id=row[1],
        item_name=row[2],
        item_key=row[3],
        status=row[4],
        claimed_by_discord_user_id=row[5],
        claimed_at=row[6],
        created_at=row[7],
    )
