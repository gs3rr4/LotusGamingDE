"""Tests for MarketplaceCog's own logic.

Forum/thread plumbing is faked (mirrors test_duo_cog.py's FakeBot style) so
these focus on the decisions that actually matter: the notify-opt-out filter
(deliberately checked side-by-side against WoWData.find_crafters to prove the
existing /wow crafting search stays untouched) and the race-safe claim
button / poster-only "mark done" checks.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from lotus_bot.cogs.wow.cog import MEMBER_RANK, TWINK_RANK, WoWCog
from lotus_bot.cogs.wow.data import RosterMember, WoWData
from lotus_bot.cogs.wow.marketplace_cog import (
    MarketplaceCog,
    MarketplaceCharacterSelectView,
)
from lotus_bot.cogs.wow.marketplace_data import MarketplaceData

pytestmark = pytest.mark.asyncio


def roster_member(name, key, guild_rank=1):
    return RosterMember(
        character_key=key,
        character_id=1,
        name=name,
        realm_slug="soulseeker",
        level=60,
        class_id=4,
        race_id=8,
        faction="HORDE",
        guild_rank=guild_rank,
    )


def make_real_wow(wow_data: WoWData) -> WoWCog:
    """A real WoWCog instance backed by ``wow_data``, without running its
    heavy __init__ (no bot needed) - character_display_label/_localized_text
    are pure functions of self.data, so this exercises the REAL logic."""
    wow = WoWCog.__new__(WoWCog)
    wow.data = wow_data
    return wow


class FakeCreatedThread:
    def __init__(self, thread):
        self.thread = thread
        self.message = MagicMock()
        self.message.edit = AsyncMock()


class FakeForum:
    def __init__(self):
        self.created: list[dict] = []
        self._next_id = 900

    async def create_thread(self, *, name, content=None, embed=None, applied_tags=None):
        self._next_id += 1
        thread = make_thread(self._next_id)
        self.created.append(
            {"name": name, "content": content, "applied_tags": applied_tags}
        )
        return FakeCreatedThread(thread)


def make_result(item_id="item.1", item_name="Testitem", crafters=None, status="ok"):
    return SimpleNamespace(
        status=status,
        item={"id": item_id, "name": {"de": item_name, "en": item_name}},
        recipe={},
        crafters=crafters or [],
        required_skill=0,
        profession_id=None,
        manual_recipe=False,
    )


class FakeBot:
    def __init__(self, wow=None):
        self._wow = wow
        self.views = []

    def add_view(self, view):
        self.views.append(view)

    def get_cog(self, name):
        return self._wow if name == "WoWCog" else None

    def get_channel(self, channel_id):
        return None

    async def wait_until_ready(self):
        return


async def _make_cog(tmp_path, wow=None):
    cog = MarketplaceCog(FakeBot(wow))
    # Kill the background hub-publish startup task before any DB access.
    for task in list(cog.tasks):
        task.cancel()
    cog.data = MarketplaceData(str(tmp_path / "marketplace.db"))
    return cog


def make_thread(thread_id):
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.send = AsyncMock()
    thread.edit = AsyncMock()
    starter = MagicMock()
    starter.edit = AsyncMock()
    thread.starter_message = starter
    thread.fetch_message = AsyncMock(return_value=starter)
    return thread


def make_interaction(user_id, channel=None):
    interaction = MagicMock()
    interaction.user.id = user_id
    interaction.channel = channel
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


async def test_notify_filter_excludes_opted_out_without_touching_find_crafters(
    tmp_path,
):
    wow_data = WoWData(str(tmp_path / "wow.db"))
    alpha = roster_member("Alpha", "id:1")
    beta = roster_member("Beta", "id:2")
    await wow_data.replace_snapshot([alpha, beta])
    claim_a, _ = await wow_data.create_claim(alpha, 42)
    claim_b, _ = await wow_data.create_claim(beta, 43)
    await wow_data.set_character_profession(claim_a, "enchanting", 300)
    await wow_data.set_character_profession(claim_b, "enchanting", 300)
    await wow_data.set_crafting_notify("id:2", False)

    wow = SimpleNamespace(data=wow_data)
    cog = await _make_cog(tmp_path, wow=wow)

    crafters = await wow_data.find_crafters("enchanting", 1)
    # The EXISTING search must show both - completely unaffected by opt-out.
    assert {c.character_key for c in crafters} == {"id:1", "id:2"}

    filtered = await cog._filter_notify_opt_in(crafters)
    assert {c.character_key for c in filtered} == {"id:1"}

    await wow_data.close()


async def test_claim_button_second_click_gets_already_taken_message(tmp_path):
    cog = await _make_cog(tmp_path)
    thread = make_thread(111)
    await cog.data.create_crafting_request(
        111, requester_discord_user_id=42, item_name="Kreuzritter", item_key="enchant:1"
    )

    first = make_interaction(100, thread)
    await cog.handle_claim(first)
    second = make_interaction(200, thread)
    await cog.handle_claim(second)

    assert "übernimmst" in first.response.send_message.await_args.args[0]
    assert "Schon vergeben" in second.response.send_message.await_args.args[0]
    request = await cog.data.get_crafting_request(111)
    assert request.status == "claimed"
    assert request.claimed_by_discord_user_id == 100  # first click wins, not second
    thread.send.assert_awaited_once()  # only one "übernimmt" announcement


async def test_claim_button_requester_cannot_claim_own_request(tmp_path):
    cog = await _make_cog(tmp_path)
    thread = make_thread(111)
    await cog.data.create_crafting_request(
        111, requester_discord_user_id=42, item_name="X", item_key="item.1"
    )

    interaction = make_interaction(42, thread)
    await cog.handle_claim(interaction)

    assert "eigene Anfrage" in interaction.response.send_message.await_args.args[0]
    assert (await cog.data.get_crafting_request(111)).status == "open"


async def test_mark_listing_done_only_for_poster(tmp_path):
    cog = await _make_cog(tmp_path)
    thread = make_thread(222)
    await cog.data.create_listing(222, poster_discord_user_id=42, tag_kind="biete")

    stranger = make_interaction(999, thread)
    await cog.mark_listing_done(stranger)
    assert (await cog.data.get_listing(222)).status == "open"
    thread.edit.assert_not_awaited()

    poster = make_interaction(42, thread)
    await cog.mark_listing_done(poster)
    assert (await cog.data.get_listing(222)).status == "done"
    thread.edit.assert_awaited_once()


async def test_choose_character_for_listing_rejects_without_any_claim(tmp_path):
    wow_data = WoWData(str(tmp_path / "wow.db"))
    wow = make_real_wow(wow_data)
    cog = await _make_cog(tmp_path, wow=wow)
    cog.forum = lambda: FakeForum()  # never reached - the claim gate blocks first

    interaction = make_interaction(42)
    await cog.choose_character_for_listing(interaction, "biete", "Titel", "")

    assert "geclaimten Char" in interaction.response.send_message.await_args.args[0]
    await wow_data.close()


async def test_choose_character_for_listing_single_claim_shows_relation_label(
    tmp_path,
):
    wow_data = WoWData(str(tmp_path / "wow.db"))
    # A lone Twink with no Member+ owner - relation shown without "von X".
    solo = roster_member("Weedlager", "id:3", guild_rank=TWINK_RANK)
    await wow_data.replace_snapshot([solo])
    await wow_data.create_claim(solo, 99)

    wow = make_real_wow(wow_data)
    cog = await _make_cog(tmp_path, wow=wow)
    fake_forum = FakeForum()
    cog.forum = lambda: fake_forum

    interaction = make_interaction(99)
    await cog.choose_character_for_listing(interaction, "biete", "Ausruestung", "Test")

    assert fake_forum.created[0]["content"].endswith("Angebot von Weedlager (Twink)")
    listing = await cog.data.get_listing(901)
    assert listing is not None
    assert listing.character_key == "id:3"
    interaction.followup.send.assert_awaited_once()
    await wow_data.close()


async def test_choose_character_for_listing_multiple_claims_shows_picker(tmp_path):
    wow_data = WoWData(str(tmp_path / "wow.db"))
    main_char = roster_member("Lyxendra", "id:1", guild_rank=MEMBER_RANK)
    twink = roster_member("Voidok", "id:2", guild_rank=TWINK_RANK)
    await wow_data.replace_snapshot([main_char, twink])
    await wow_data.create_claim(main_char, 42)
    await wow_data.create_claim(twink, 42)

    wow = make_real_wow(wow_data)
    cog = await _make_cog(tmp_path, wow=wow)
    fake_forum = FakeForum()
    cog.forum = lambda: fake_forum

    interaction = make_interaction(42)
    await cog.choose_character_for_listing(interaction, "suche", "Hilfe", "")

    # No thread yet - the picker must appear first.
    assert fake_forum.created == []
    _, kwargs = interaction.response.send_message.await_args
    view = kwargs["view"]
    assert isinstance(view, MarketplaceCharacterSelectView)
    assert {key for key, _ in view.choices} == {"id:1", "id:2"}

    # Simulate picking the Twink: the thread now shows the relation label.
    picker_interaction = make_interaction(42)
    await view.on_selected(picker_interaction, "id:2")

    assert fake_forum.created[0]["content"].endswith(
        "Gesuch von Voidok (Twink von Lyxendra)"
    )
    await wow_data.close()


async def test_finish_crafting_request_stores_character_and_label(tmp_path):
    wow_data = WoWData(str(tmp_path / "wow.db"))
    main_char = roster_member("Lyxendra", "id:1", guild_rank=MEMBER_RANK)
    await wow_data.replace_snapshot([main_char])
    await wow_data.create_claim(main_char, 42)

    wow = make_real_wow(wow_data)
    cog = await _make_cog(tmp_path, wow=wow)
    fake_forum = FakeForum()
    cog.forum = lambda: fake_forum

    interaction = make_interaction(42)
    result = make_result(item_id="item.9", item_name="Wuttrank")
    await cog._finish_crafting_request(
        interaction, result, "bringe Mats mit", wow, "id:1", edit=False
    )

    request = await cog.data.get_crafting_request(901)
    assert request.requester_character_key == "id:1"
    assert "Gesucht von Lyxendra" in fake_forum.created[0]["content"]
    assert "bringe Mats mit" in fake_forum.created[0]["content"]
    interaction.followup.send.assert_awaited_once()
    await wow_data.close()
