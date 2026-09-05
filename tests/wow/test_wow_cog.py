import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import discord
import pytest_asyncio
from datetime import datetime

from lotus_bot.cogs.wow.cog import WoWCog
from lotus_bot.cogs.wow.data import RosterMember, WoWData
import lotus_bot.cogs.wow.cog as wow_cog_mod
import lotus_bot.log_setup as log_setup

CREATED_COGS = []


@pytest_asyncio.fixture(autouse=True)
async def close_created_cogs():
    yield
    for cog in CREATED_COGS:
        await cog.data.close()
    CREATED_COGS.clear()


class DummyChannel:
    def __init__(self, missing_message_ids=None):
        self.sent = []
        self.next_message_id = 555
        # Message ids that fetch_message should report as deleted (NotFound).
        self.missing_message_ids = set(missing_message_ids or ())

    async def send(self, msg=None, **kwargs):
        # Real discord.TextChannel.send() makes content optional — a V2
        # LayoutView is sent without any text body. Mirror that here.
        self.sent.append((msg, kwargs))
        return type("Message", (), {"id": self.next_message_id})()

    async def fetch_message(self, message_id):
        if message_id in self.missing_message_ids:
            response = type("Response", (), {"status": 404, "reason": "Not Found"})()
            raise discord.NotFound(response, {"message": "Unknown Message"})
        return type("Message", (), {"id": message_id})()


class DummyPanelMessage:
    def __init__(self, message_id):
        self.id = message_id
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class DummyPanelChannel(DummyChannel):
    id = 1463577361562992807

    def __init__(self, existing_message=None):
        super().__init__()
        self.existing_message = existing_message
        self.fetches = []

    async def fetch_message(self, message_id):
        self.fetches.append(message_id)
        if self.existing_message is None:
            response = type("Response", (), {"status": 404, "reason": "Not Found"})()
            raise discord.NotFound(response, {"message": "Unknown Message"})
        return self.existing_message


class ForbiddenChannel:
    async def send(self, msg):
        response = type("Response", (), {"status": 403, "reason": "Forbidden"})()
        raise discord.Forbidden(response, {"message": "Missing Access", "code": 50001})


class DummyBot:
    def __init__(self, channel=None):
        self.channel = channel
        self.views = []
        self.data = {}

    def get_channel(self, channel_id):
        return self.channel

    def add_view(self, view):
        self.views.append(view)

    def get_cog(self, name):
        return None


class DungeonsBot(DummyBot):
    """Bot whose get_channel() serves the dungeons forum, and (once a guide
    thread exists) that thread too — mirrors how publish_dungeons_guide looks
    both up via plain bot.get_channel()."""

    def __init__(self, forum=None, thread=None):
        super().__init__()
        self.forum = forum
        self.thread = thread
        self.fetch_channel = AsyncMock(return_value=None)

    def get_channel(self, channel_id):
        if channel_id == wow_cog_mod.DUNGEONS_FORUM_CHANNEL_ID:
            return self.forum
        if self.thread is not None and channel_id == self.thread.id:
            return self.thread
        return None


def make_dungeons_forum(created_thread_id=4242):
    """A ForumChannel fake whose create_thread() returns a Thread fake that
    supports get_partial_message(...).edit(...) and edit(pinned=True)."""
    forum = MagicMock(spec=discord.ForumChannel)
    thread = MagicMock(spec=discord.Thread)
    thread.id = created_thread_id
    thread.edit = AsyncMock()
    partial_message = MagicMock()
    partial_message.edit = AsyncMock()
    thread.get_partial_message = MagicMock(return_value=partial_message)
    forum.create_thread = AsyncMock(return_value=SimpleNamespace(thread=thread))
    return forum, thread


class DummyChampionCog:
    def __init__(self):
        self.updates = []

    async def update_user_score(self, user_id, delta, reason):
        self.updates.append((user_id, delta, reason))
        return delta


class ChampionBot(DummyBot):
    def __init__(self, channel=None, champion=None):
        super().__init__(channel=channel)
        self.champion = champion

    def get_cog(self, name):
        return self.champion if name == "ChampionCog" else None


class MultiChannelBot(DummyBot):
    def __init__(self, public_channel=None, officer_channel=None):
        super().__init__(channel=public_channel)
        self.public_channel = public_channel
        self.officer_channel = officer_channel

    def get_channel(self, channel_id):
        if channel_id == wow_cog_mod.DEFAULT_CLAIM_REVIEW_CHANNEL_ID:
            return self.officer_channel
        return self.public_channel


class DummyDMUser:
    """A user whose .send() either records the DM or raises Forbidden."""

    def __init__(self, uid, raise_forbidden=False):
        self.id = uid
        self.mention = f"<@{uid}>"
        self.sent = []
        self._raise_forbidden = raise_forbidden

    async def send(self, content):
        if self._raise_forbidden:
            response = type("Response", (), {"status": 403, "reason": "Forbidden"})()
            raise discord.Forbidden(
                response,
                {"message": "Cannot send messages to this user", "code": 50007},
            )
        self.sent.append(content)


class GBankBot(MultiChannelBot):
    """Bot with DM support + officer channel for gbank request tests."""

    def __init__(self, officer_channel=None, dm_user=None):
        super().__init__(public_channel=None, officer_channel=officer_channel)
        self.dm_user = dm_user

    def get_user(self, uid):
        return self.dm_user

    async def fetch_user(self, uid):
        return self.dm_user


class DummyPermissions:
    def __init__(self, manage_guild=False):
        self.manage_guild = manage_guild


class DummyUser:
    def __init__(self, uid, manage_guild=False):
        self.id = uid
        self.mention = f"<@{uid}>"
        self.guild_permissions = DummyPermissions(manage_guild)


class DummyReviewResponse:
    def __init__(self):
        self.messages = []
        self.edits = []
        self.modals = []

    async def send_message(self, msg=None, **kwargs):
        # Real interaction.response.send_message takes ``content`` as an
        # optional keyword; embed-only or view-only sends omit it.
        self.messages.append((msg, kwargs))

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)

    async def send_modal(self, modal):
        self.modals.append(modal)


class DummyReviewInteraction:
    def __init__(self, user, message_id=555):
        self.user = user
        self.message = type("Message", (), {"id": message_id})()
        self.response = DummyReviewResponse()


def member(
    key="id:1",
    name="Lyxendra",
    level=1,
    class_id=4,
    race_id=8,
    is_ghost=False,
    guild_rank=1,
):
    return RosterMember(
        character_key=key,
        character_id=1,
        name=name,
        realm_slug="soulseeker",
        level=level,
        class_id=class_id,
        race_id=race_id,
        faction="HORDE",
        guild_rank=guild_rank,
        is_ghost=is_ghost,
    )


def crafting_data():
    return {
        "professions": [
            {
                "id": "alchemy",
                "type": "primary",
                "name": {"de": "Alchemie", "en": "Alchemy"},
            },
            {
                "id": "blacksmithing",
                "type": "primary",
                "name": {"de": "Schmiedekunst", "en": "Blacksmithing"},
            },
            {
                "id": "engineering",
                "type": "primary",
                "name": {"de": "Ingenieurskunst", "en": "Engineering"},
            },
            {
                "id": "cooking",
                "type": "secondary",
                "name": {"de": "Kochkunst", "en": "Cooking"},
            },
            {
                "id": "fishing",
                "type": "secondary",
                "name": {"de": "Angeln", "en": "Fishing"},
            },
            {
                "id": "first-aid",
                "type": "secondary",
                "name": {"de": "Erste Hilfe", "en": "First Aid"},
            },
        ],
        "items": [
            {
                "id": "item.118",
                "name": {"de": "Schwacher Heiltrank", "en": "Minor Healing Potion"},
            },
            {
                "id": "item.2459",
                "name": {"de": "Swiftnesstrank", "en": "Swiftness Potion"},
            },
            {
                "id": "item.999",
                "name": {"de": "Schwacher Manatrank", "en": "Minor Mana Potion"},
            },
        ],
        "spells": [
            {
                "id": "spell.2330",
                "name": {
                    "de": "Schwacher Heiltrank herstellen",
                    "en": "Minor Healing Potion",
                },
            },
            {
                "id": "spell.2335",
                "name": {
                    "de": "Swiftnesstrank herstellen",
                    "en": "Swiftness Potion",
                },
            },
            {
                "id": "spell.2331",
                "name": {
                    "de": "Schwacher Manatrank herstellen",
                    "en": "Minor Mana Potion",
                },
            },
        ],
        "profession_recipes": [
            {
                "id": "recipe.minor_healing_potion",
                "profession_id": "alchemy",
                "spell_id": "spell.2330",
                "creates_item_id": "item.118",
                "required_skill": 1,
                "learned_from": "trainer",
                "hardcore_valid": True,
            },
            {
                "id": "recipe.swiftness_potion",
                "profession_id": "alchemy",
                "spell_id": "spell.2335",
                "creates_item_id": "item.2459",
                "required_skill": 60,
                "learned_from": "recipe",
                "recipe_item_sources": ["drop"],
                "hardcore_valid": True,
            },
            {
                "id": "recipe.minor_mana_potion",
                "profession_id": "alchemy",
                "spell_id": "spell.2331",
                "creates_item_id": "item.999",
                "required_skill": 1,
                "learned_from": "trainer",
                "hardcore_valid": True,
            },
        ],
    }


async def create_cog(tmp_path, patch_logged_task, channel=None):
    patch_logged_task(wow_cog_mod, log_setup)
    cog = WoWCog(DummyBot(channel=channel))
    cog.data = WoWData(str(tmp_path / "wow.db"))
    CREATED_COGS.append(cog)
    return cog


@pytest.mark.asyncio
async def test_panel_publish_creates_and_stores_message(tmp_path, patch_logged_task):
    channel = DummyPanelChannel()
    cog = await create_cog(tmp_path, patch_logged_task)

    result = await cog.publish_panel(channel)

    assert result.created is True
    assert result.channel_id == channel.id
    assert result.message_id == 555
    # V2 hub message has no text content — everything lives inside the
    # LayoutView's TextDisplay items.
    sent_content, sent_kwargs = channel.sent[0]
    assert sent_content is None
    view = sent_kwargs["view"]
    assert isinstance(view, wow_cog_mod.WoWPanelLayoutView)
    # Published view carries the live stats line in its hub header.
    hub_display = view.children[0].children[0]
    assert isinstance(hub_display, discord.ui.TextDisplay)
    assert "Member" in hub_display.content
    assert "Cooldowns laufen" in hub_display.content
    assert await cog.data.get_setting("panel_channel_id") == str(channel.id)
    assert await cog.data.get_setting("panel_message_id") == "555"


@pytest.mark.asyncio
async def test_panel_publish_updates_existing_message(tmp_path, patch_logged_task):
    existing = DummyPanelMessage(777)
    channel = DummyPanelChannel(existing)
    cog = await create_cog(tmp_path, patch_logged_task)
    await cog.data.set_setting("panel_message_id", "777")

    result = await cog.publish_panel(channel)

    assert result.created is False
    assert channel.fetches == [777]
    assert channel.sent == []
    # Edit drops the old content (V1→V2 migration) and swaps in the new
    # LayoutView; that's how Discord lets us upgrade a classic message.
    assert existing.edits[0]["content"] is None
    assert isinstance(existing.edits[0]["view"], wow_cog_mod.WoWPanelLayoutView)


@pytest.mark.asyncio
async def test_publish_dungeons_guide_creates_new_pinned_thread(
    tmp_path, patch_logged_task
):
    forum, thread = make_dungeons_forum(created_thread_id=4242)
    patch_logged_task(wow_cog_mod, log_setup)
    cog = WoWCog(DungeonsBot(forum=forum))
    cog.data = WoWData(str(tmp_path / "wow.db"))
    CREATED_COGS.append(cog)

    await cog.publish_dungeons_guide()

    forum.create_thread.assert_awaited_once()
    _, kwargs = forum.create_thread.await_args
    assert kwargs["content"] == wow_cog_mod.DUNGEONS_GUIDE_TEXT
    assert await cog.data.get_setting("dungeons_guide_thread_id") == "4242"
    thread.edit.assert_awaited_once_with(pinned=True)


@pytest.mark.asyncio
async def test_publish_dungeons_guide_edits_existing_thread(
    tmp_path, patch_logged_task
):
    forum, thread = make_dungeons_forum(created_thread_id=5555)
    patch_logged_task(wow_cog_mod, log_setup)
    cog = WoWCog(DungeonsBot(forum=forum, thread=thread))
    cog.data = WoWData(str(tmp_path / "wow.db"))
    CREATED_COGS.append(cog)
    await cog.data.set_setting("dungeons_guide_thread_id", "5555")

    await cog.publish_dungeons_guide()

    forum.create_thread.assert_not_awaited()
    thread.get_partial_message.assert_called_once_with(5555)
    thread.get_partial_message.return_value.edit.assert_awaited_once_with(
        content=wow_cog_mod.DUNGEONS_GUIDE_TEXT
    )
    thread.edit.assert_awaited_once_with(pinned=True)


@pytest.mark.asyncio
async def test_panel_hub_layout_has_seven_sections(tmp_path, patch_logged_task):
    """V2 hub: one Container with seven Sections (Deine Chars, Suchen,
    Gildenbank, Cooldown, Hilfe, Champion, Raider). Each Section's
    accessory is a clickable Button with a stable custom_id so persistence
    survives bot restarts. (The 'Event erstellen' block is a plain
    TextDisplay with an inline command mention, not a Section.)"""
    cog = await create_cog(tmp_path, patch_logged_task)
    hub = wow_cog_mod.WoWPanelLayoutView(cog)

    assert hub.is_persistent()
    container = hub.children[0]
    assert isinstance(container, discord.ui.Container)
    sections = [c for c in container.children if isinstance(c, discord.ui.Section)]
    assert len(sections) == 7
    custom_ids = {section.accessory.custom_id for section in sections}
    assert custom_ids == {
        "wow_panel_v2:chars",
        "wow_panel_v2:search",
        "wow_panel_v2:gbank",
        "wow_panel_v2:cooldown",
        "wow_panel_v2:help",
        "wow_panel_v2:champion",
        "wow_panel_v2:raider",
    }
    # Horde-red accent stripe on the container.
    assert container.accent_colour == discord.Colour(0xC41E3A)


@pytest.mark.asyncio
async def test_build_panel_stats_line_renders_counts(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    alive = member(key="id:1", name="Alive")
    ghost = member(key="id:2", name="Ghosty", is_ghost=True)
    await cog.data.replace_snapshot([alive, ghost])
    await cog.data.create_claim(alive, 42)
    import datetime as dt

    now = dt.datetime.utcnow()
    await cog.data.set_cooldown(
        "id:1",
        "alchemy_transmute",
        "spell.17187",
        "Transmute: Arkanit",
        now.isoformat(),
        (now + dt.timedelta(hours=48)).isoformat(),
    )

    line = await cog.build_panel_stats_line()

    assert "**2** Member" in line
    assert "**1** Chars geclaimt" in line
    assert "**1** Geister" in line
    assert "**1** Cooldowns laufen" in line


class _DummyLookupUser:
    def __init__(self, uid, name="Tester", username=None):
        self.id = uid
        self.display_name = name
        self._username = username or name.lower()

    def __str__(self):
        return self._username


class _DummyQueryGuild:
    def __init__(self, matches):
        self._matches = matches
        self.queries = []

    async def query_members(self, *, query, limit):
        self.queries.append((query, limit))
        return list(self._matches)


@pytest.mark.asyncio
async def test_member_search_single_match_lists_claims(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    voidok = member(key="id:1", name="Voidok", level=48, class_id=5, race_id=5)
    await cog.data.replace_snapshot([voidok])
    await cog.data.create_claim(voidok, 99)

    modal = wow_cog_mod.PanelMemberSearchModal(cog)
    modal.query._value = "Gerrit"
    gerrit = _DummyLookupUser(99, "Gerrit")
    interaction = _DummyRaiderInteraction(
        _DummyLookupUser(42), _DummyQueryGuild([gerrit])
    )

    await modal.on_submit(interaction)

    assert interaction.response.deferred is True
    content, _ = interaction.followup.messages[0]
    assert "Chars von Gerrit" in content
    assert "Voidok" in content
    assert "Level **48**" in content
    assert "ungeprüft" in content


@pytest.mark.asyncio
async def test_member_claims_shows_rank_and_sorts_main_first(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)
    main = ranked("id:1", "Mainchar", 4, level=60)  # Member
    twink = ranked("id:2", "Twinkerl", 6, level=41)  # Twink
    await cog.data.replace_snapshot([main, twink])
    await cog.data.create_claim(main, 99)
    await cog.data.create_claim(twink, 99)

    content = await wow_cog_mod._render_member_claims(cog, 99, "Gerrit")

    assert "Member" in content and "Twink" in content
    assert "Level **60**" in content and "Level **41**" in content
    # Better in-game rank (Member) is listed above the Twink → likely the main.
    assert content.index("Mainchar") < content.index("Twinkerl")


@pytest.mark.asyncio
async def test_member_search_no_match(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    modal = wow_cog_mod.PanelMemberSearchModal(cog)
    modal.query._value = "Niemando"
    interaction = _DummyRaiderInteraction(_DummyLookupUser(42), _DummyQueryGuild([]))

    await modal.on_submit(interaction)

    content, _ = interaction.followup.messages[0]
    assert "Kein Member gefunden" in content
    assert "Niemando" in content


@pytest.mark.asyncio
async def test_member_search_multiple_matches_shows_select(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    modal = wow_cog_mod.PanelMemberSearchModal(cog)
    modal.query._value = "Ger"
    matches = [_DummyLookupUser(1, "Gerrit"), _DummyLookupUser(2, "Gerald")]
    interaction = _DummyRaiderInteraction(
        _DummyLookupUser(42), _DummyQueryGuild(matches)
    )

    await modal.on_submit(interaction)

    content, kwargs = interaction.followup.messages[0]
    assert "**2** Member gefunden" in content
    assert isinstance(kwargs["view"], wow_cog_mod._MemberMatchSelectView)


@pytest.mark.asyncio
async def test_member_match_select_renders_claims(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    voidok = member(key="id:1", name="Voidok", level=48, class_id=5, race_id=5)
    await cog.data.replace_snapshot([voidok])
    await cog.data.create_claim(voidok, 99)

    matches = [_DummyLookupUser(99, "Gerrit"), _DummyLookupUser(2, "Gerald")]
    view = wow_cog_mod._MemberMatchSelectView(cog, matches)
    view._select._values = ["99"]
    interaction = DummyReviewInteraction(DummyUser(42))

    await view._on_pick(interaction)

    content = interaction.response.edits[0]["content"]
    assert "Chars von Gerrit" in content
    assert "Voidok" in content
    # Select stays attached for follow-up lookups.
    assert interaction.response.edits[0]["view"] is view


@pytest.mark.asyncio
async def test_scan_schedules_panel_refresh(tmp_path, patch_logged_task, monkeypatch):
    """A persisting scan fires _auto_publish_panel so the dashboard stats
    in the hub header stay current."""
    cog = await create_cog(tmp_path, patch_logged_task)
    members = [member(key=f"id:{i}", name=f"M{i}", level=10) for i in range(6)]
    await cog.data.replace_snapshot(members)

    async def fake_roster(session=None):
        return members

    async def fake_profile(*args, **kwargs):
        return {"is_ghost": False}

    monkeypatch.setattr(wow_cog_mod, "fetch_character_profile", fake_profile)
    cog.fetch_roster = fake_roster

    tracked = []

    def spy_track(coro):
        tracked.append(getattr(coro, "__name__", repr(coro)))
        coro.close()

    cog._track_task = spy_track

    await cog.scan(post=False, persist=True)

    assert "_auto_publish_panel" in tracked


class _DummyRaiderMember:
    def __init__(self, roles=None):
        self.id = 42
        self.roles = list(roles or [])
        self.added = []
        self.removed = []

    async def add_roles(self, role, *, reason=None):
        self.added.append(role)
        self.roles.append(role)

    async def remove_roles(self, role, *, reason=None):
        self.removed.append(role)
        self.roles = [r for r in self.roles if r is not role]


class _DummyRaiderGuild:
    def __init__(self, role, member):
        self._role = role
        self._member = member

    def get_role(self, rid):
        return self._role if rid == wow_cog_mod.RAIDER_ROLE_ID else None

    def get_member(self, uid):
        return self._member


class _DummyRaiderResponse:
    def __init__(self):
        self.deferred = False
        self.messages = []

    async def defer(self, *, ephemeral=False):
        self.deferred = True

    async def send_message(self, msg=None, **kwargs):
        self.messages.append((msg, kwargs))


class _DummyRaiderFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, msg=None, **kwargs):
        self.messages.append((msg, kwargs))


class _DummyRaiderInteraction:
    def __init__(self, member, guild):
        self.user = member
        self.guild = guild
        self.response = _DummyRaiderResponse()
        self.followup = _DummyRaiderFollowup()


@pytest.mark.asyncio
async def test_raider_toggle_adds_role_when_missing(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    hub = wow_cog_mod.WoWPanelLayoutView(cog)
    role = type("Role", (), {"id": wow_cog_mod.RAIDER_ROLE_ID, "name": "Raider"})()
    member = _DummyRaiderMember(roles=[])
    interaction = _DummyRaiderInteraction(member, _DummyRaiderGuild(role, member))

    await hub._toggle_raider_role(interaction)

    assert interaction.response.deferred is True
    assert member.added == [role]
    assert member.removed == []
    msg, _ = interaction.followup.messages[0]
    assert "hinzugefügt" in msg


@pytest.mark.asyncio
async def test_raider_toggle_removes_role_when_present(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    hub = wow_cog_mod.WoWPanelLayoutView(cog)
    role = type("Role", (), {"id": wow_cog_mod.RAIDER_ROLE_ID, "name": "Raider"})()
    member = _DummyRaiderMember(roles=[role])
    interaction = _DummyRaiderInteraction(member, _DummyRaiderGuild(role, member))

    await hub._toggle_raider_role(interaction)

    assert member.removed == [role]
    assert member.added == []
    msg, _ = interaction.followup.messages[0]
    assert "entfernt" in msg


@pytest.mark.asyncio
async def test_raider_toggle_handles_missing_role(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    hub = wow_cog_mod.WoWPanelLayoutView(cog)
    member = _DummyRaiderMember(roles=[])
    # Guild returns None for the role lookup.
    guild = _DummyRaiderGuild(role=None, member=member)
    interaction = _DummyRaiderInteraction(member, guild)

    await hub._toggle_raider_role(interaction)

    assert member.added == [] and member.removed == []
    msg, _ = interaction.followup.messages[0]
    assert "nicht gefunden" in msg


@pytest.mark.asyncio
async def test_raider_toggle_handles_forbidden(tmp_path, patch_logged_task):
    """Reviewer-flagged branch coverage: bot lacks Manage Roles permission."""
    cog = await create_cog(tmp_path, patch_logged_task)
    hub = wow_cog_mod.WoWPanelLayoutView(cog)
    role = type("Role", (), {"id": wow_cog_mod.RAIDER_ROLE_ID, "name": "Raider"})()

    class ForbiddenMember(_DummyRaiderMember):
        async def add_roles(self, role, *, reason=None):
            response = type("R", (), {"status": 403, "reason": "Forbidden"})()
            raise discord.Forbidden(response, {"message": "Missing Permissions"})

    member = ForbiddenMember(roles=[])
    interaction = _DummyRaiderInteraction(member, _DummyRaiderGuild(role, member))

    await hub._toggle_raider_role(interaction)

    msg, _ = interaction.followup.messages[0]
    assert "Berechtigung" in msg
    assert "Manage Roles" in msg


@pytest.mark.asyncio
async def test_bank_character_crud(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    await cog.data.add_bank_character("id:1", "Lotusrocks", 42)

    assert await cog.data.is_bank_character("id:1") is True
    banks = await cog.data.list_bank_characters()
    assert [b.character_name for b in banks] == ["Lotusrocks"]
    assert banks[0].added_by == 42

    # Re-adding updates the name, stays a single row.
    await cog.data.add_bank_character("id:1", "Lotusbank", 7)
    banks = await cog.data.list_bank_characters()
    assert [b.character_name for b in banks] == ["Lotusbank"]

    assert await cog.data.remove_bank_character("id:1") is True
    assert await cog.data.is_bank_character("id:1") is False
    assert await cog.data.remove_bank_character("id:1") is False


async def _seed_bank_request(cog, *, bank_owner_id=None, requester_ids=None):
    """Snapshot a bank char + optional requester chars, mark the bank char."""
    members = [member(key="id:1", name="Lotusrocks")]
    requester_ids = requester_ids or []
    for idx, _ in enumerate(requester_ids, start=2):
        members.append(member(key=f"id:{idx}", name=f"Reqchar{idx}"))
    await cog.data.replace_snapshot(members)
    if bank_owner_id is not None:
        await cog.data.create_claim(
            member(key="id:1", name="Lotusrocks"), bank_owner_id
        )
    for idx, uid in enumerate(requester_ids, start=2):
        await cog.data.create_claim(member(key=f"id:{idx}", name=f"Reqchar{idx}"), uid)
    await cog.data.add_bank_character("id:1", "Lotusrocks", 1)


@pytest.mark.asyncio
async def test_gbank_request_dms_owner(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    owner = DummyDMUser(99)
    cog.bot = GBankBot(officer_channel=DummyChannel(), dm_user=owner)
    await _seed_bank_request(cog, bank_owner_id=99)

    requester = DummyDMUser(42)
    await cog.submit_gbank_request(
        requester, "Voidok", "id:1", "Lotusrocks", "20x Runenstoff"
    )

    assert len(owner.sent) == 1
    assert "Runenstoff" in owner.sent[0]
    assert "Lotusrocks" in owner.sent[0]
    assert "Voidok" in owner.sent[0]
    # No officer-channel fallback when the DM succeeds.
    assert cog.bot.officer_channel.sent == []


@pytest.mark.asyncio
async def test_gbank_request_falls_back_to_officer_on_dm_failure(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)
    owner = DummyDMUser(99, raise_forbidden=True)
    officer = DummyChannel()
    cog.bot = GBankBot(officer_channel=officer, dm_user=owner)
    await _seed_bank_request(cog, bank_owner_id=99)

    requester = DummyDMUser(42)
    await cog.submit_gbank_request(
        requester, "Voidok", "id:1", "Lotusrocks", "Mondstoff"
    )

    assert owner.sent == []
    assert len(officer.sent) == 1
    assert "fehlgeschlagen" in officer.sent[0][0]
    assert "Mondstoff" in officer.sent[0][0]


@pytest.mark.asyncio
async def test_gbank_request_unclaimed_goes_to_officer(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    officer = DummyChannel()
    cog.bot = GBankBot(officer_channel=officer, dm_user=None)
    await _seed_bank_request(cog, bank_owner_id=None)

    requester = DummyDMUser(42)
    await cog.submit_gbank_request(
        requester, "Voidok", "id:1", "Lotusrocks", "Runenstoff"
    )

    assert len(officer.sent) == 1
    assert "nicht geclaimed" in officer.sent[0][0]


@pytest.mark.asyncio
async def test_open_gbank_request_requires_a_claim(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    hub = wow_cog_mod.WoWPanelLayoutView(cog)
    interaction = DummyReviewInteraction(DummyUser(42))

    await hub._open_gbank_request(interaction)

    # No modal — user is told to claim a char first.
    assert interaction.response.modals == []
    msg, _ = interaction.response.messages[0]
    assert "claimen" in msg


@pytest.mark.asyncio
async def test_open_gbank_request_opens_modal_with_claim(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    await _seed_bank_request(cog, requester_ids=[42])
    hub = wow_cog_mod.WoWPanelLayoutView(cog)
    interaction = DummyReviewInteraction(DummyUser(42))

    await hub._open_gbank_request(interaction)

    assert isinstance(interaction.response.modals[0], wow_cog_mod.GBankRequestModal)


@pytest.mark.asyncio
async def test_gbank_bank_select_single_claim_finalizes(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    owner = DummyDMUser(99)
    cog.bot = GBankBot(officer_channel=DummyChannel(), dm_user=owner)
    # Bank char owned by 99; requester 42 has exactly one char (Reqchar2).
    await _seed_bank_request(cog, bank_owner_id=99, requester_ids=[42])

    bank_chars = await cog.data.list_bank_characters()
    view = wow_cog_mod.GBankBankCharSelectView(cog, 42, "Runenstoff", bank_chars)
    select = view.children[0]
    select._values = ["id:1"]
    interaction = DummyReviewInteraction(DummyUser(42))

    await select.callback(interaction)

    assert len(owner.sent) == 1
    assert "Reqchar2" in owner.sent[0]
    assert interaction.response.edits[0]["content"] == "✅ Anfrage gesendet."


@pytest.mark.asyncio
async def test_gbank_bank_select_multi_claim_shows_own_char(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot = GBankBot(officer_channel=DummyChannel(), dm_user=DummyDMUser(99))
    # Requester 42 has two chars → must pick one.
    await _seed_bank_request(cog, bank_owner_id=99, requester_ids=[42, 42])

    bank_chars = await cog.data.list_bank_characters()
    view = wow_cog_mod.GBankBankCharSelectView(cog, 42, "Runenstoff", bank_chars)
    select = view.children[0]
    select._values = ["id:1"]
    interaction = DummyReviewInteraction(DummyUser(42))

    await select.callback(interaction)

    assert isinstance(
        interaction.response.edits[0]["view"], wow_cog_mod.GBankOwnCharSelectView
    )


@pytest.mark.asyncio
async def test_gbank_own_char_select_finalizes(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    owner = DummyDMUser(99)
    cog.bot = GBankBot(officer_channel=DummyChannel(), dm_user=owner)
    await _seed_bank_request(cog, bank_owner_id=99, requester_ids=[42, 42])

    claims = await cog.data.claims_for_user(42)
    view = wow_cog_mod.GBankOwnCharSelectView(
        cog, 42, "Runenstoff", "id:1", "Lotusrocks", claims
    )
    select = view.children[0]
    select._values = [claims[0].character_name]
    interaction = DummyReviewInteraction(DummyUser(42))

    await select.callback(interaction)

    assert len(owner.sent) == 1
    assert claims[0].character_name in owner.sent[0]
    assert interaction.response.edits[0]["content"] == "✅ Anfrage gesendet."


@pytest.mark.asyncio
async def test_panel_hub_help_button_sends_overview(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    hub = wow_cog_mod.WoWPanelLayoutView(cog)
    interaction = DummyReviewInteraction(DummyUser(42))

    await hub._open_help(interaction)

    text = interaction.response.messages[0][0]
    assert "Deine Chars" in text
    assert "Cooldown" in text
    assert "Daily Digest" in text
    assert interaction.response.messages[0][1]["ephemeral"] is True


@pytest.mark.asyncio
async def test_my_chars_view_empty_state(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    view = wow_cog_mod.PanelMyCharsView(cog, owner_user_id=42)
    interaction = DummyReviewInteraction(DummyUser(42))

    await view.send(interaction)

    msg, kwargs = interaction.response.messages[0]
    assert kwargs["ephemeral"] is True
    embed = kwargs["embed"]
    assert "noch keinen" in embed.description
    # Empty state: "Neuen Char claimen" + global "✖ Schließen" dismiss button.
    labels = [c.label for c in view.children if isinstance(c, discord.ui.Button)]
    assert labels == ["➕ Neuen Char claimen", "✖ Schließen"]


@pytest.mark.asyncio
async def test_my_chars_view_renders_claim_details(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    voidok = member(name="Voidok", level=60)
    await cog.data.replace_snapshot([voidok])
    claim, _ = await cog.data.create_claim(voidok, 42)
    await cog.data.set_character_profession(claim, "alchemy", 270)
    view = wow_cog_mod.PanelMyCharsView(cog, owner_user_id=42)
    interaction = DummyReviewInteraction(DummyUser(42))

    await view.send(interaction)

    embed = interaction.response.messages[0][1]["embed"]
    field = next(f for f in embed.fields if "Voidok" in f.name)
    assert "Level **60**" in field.value
    assert "Alchemie" in field.value
    # With a claim active, per-char action buttons appear plus the
    # global "claim new" button. Cooldown logging moved to the hub.
    labels = [c.label for c in view.children if isinstance(c, discord.ui.Button)]
    assert "🛠️ Berufe pflegen" in labels
    assert "📥 Per Addon importieren" in labels
    assert "📖 Rezepte pflegen" in labels
    assert "🗑️ Claim freigeben" in labels
    assert "➕ Neuen Char claimen" in labels
    assert "⏳ Cooldown loggen" not in labels


@pytest.mark.asyncio
async def test_my_chars_release_drops_claim_and_refreshes(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    voidok = member(name="Voidok")
    await cog.data.replace_snapshot([voidok])
    await cog.data.create_claim(voidok, 42)
    view = wow_cog_mod.PanelMyCharsView(cog, owner_user_id=42)
    interaction = DummyReviewInteraction(DummyUser(42))
    await view.send(interaction)
    interaction.response.edits.clear()

    await cog._my_chars_release(interaction, view)

    assert await cog.data.claims_for_user(42) == []
    # Refresh edited the message back to the empty state.
    assert interaction.response.edits, "release should refresh the view"
    refreshed_embed = interaction.response.edits[0]["embed"]
    assert "noch keinen" in refreshed_embed.description


@pytest.mark.asyncio
async def test_my_chars_notify_toggle_flips_flag_and_button_label(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)
    voidok = member(name="Voidok")
    await cog.data.replace_snapshot([voidok])
    claim, _ = await cog.data.create_claim(voidok, 42)
    assert claim.crafting_notify is True  # default ON (opt-out)

    view = wow_cog_mod.PanelMyCharsView(cog, owner_user_id=42)
    interaction = DummyReviewInteraction(DummyUser(42))
    await view.send(interaction)
    labels = [c.label for c in view.children if isinstance(c, discord.ui.Button)]
    assert "🔕 Crafting-Anfragen aus" in labels

    await cog._my_chars_notify_toggle(interaction, view)

    assert (await cog.data.get_claim(claim.character_key)).crafting_notify is False
    refreshed_labels = [
        c.label for c in view.children if isinstance(c, discord.ui.Button)
    ]
    # The relies-on-fresh-object _load() fix: without it, this button would
    # still read "aus" here since the view would keep the stale pre-toggle
    # claim object even though the DB flag flipped.
    assert "🔔 Crafting-Anfragen an" in refreshed_labels

    await cog._my_chars_notify_toggle(interaction, view)
    assert (await cog.data.get_claim(claim.character_key)).crafting_notify is True


@pytest.mark.asyncio
async def test_panel_search_submenu_routes_to_correct_modal(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)
    sub = wow_cog_mod.PanelSearchSubView(cog)

    crafter_interaction = DummyReviewInteraction(DummyUser(42))
    await sub.children[0].callback(crafter_interaction)
    assert isinstance(
        crafter_interaction.response.modals[0],
        wow_cog_mod.PanelCraftingSearchModal,
    )

    whois_interaction = DummyReviewInteraction(DummyUser(42))
    await sub.children[1].callback(whois_interaction)
    assert isinstance(whois_interaction.response.modals[0], wow_cog_mod.PanelWhoisModal)


@pytest.mark.asyncio
async def test_panel_whois_modal_renders_existing_char(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    await cog.data.replace_snapshot([member(name="Voidok", level=42)])
    modal = wow_cog_mod.PanelWhoisModal(cog)
    modal.query._value = "Voidok"
    interaction = DummyReviewInteraction(DummyUser(42))

    await modal.on_submit(interaction)

    _, kwargs = interaction.response.messages[0]
    assert kwargs["ephemeral"] is True
    view = kwargs["view"]
    assert isinstance(view, wow_cog_mod._WhoisLayoutView)
    container = view.children[0]
    title = container.children[0].content
    assert "Voidok" in title
    assert "Level 42" in title


@pytest.mark.asyncio
async def test_panel_whois_modal_handles_missing_char(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    modal = wow_cog_mod.PanelWhoisModal(cog)
    modal.query._value = "Ghostly"
    interaction = DummyReviewInteraction(DummyUser(42))

    await modal.on_submit(interaction)

    msg, _ = interaction.response.messages[0]
    assert "nicht im aktuellen Roster" in msg


@pytest.mark.asyncio
async def test_panel_character_search_limits_matches(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    await cog.data.replace_snapshot(
        [member(key=f"id:{idx}", name=f"Vochar{idx}") for idx in range(30)]
    )
    modal = wow_cog_mod.PanelCharacterSearchModal(cog)
    modal.query._value = "Vo"
    interaction = DummyReviewInteraction(DummyUser(42))

    await modal.on_submit(interaction)

    msg, kwargs = interaction.response.messages[0]
    assert "aus" in msg
    assert kwargs["ephemeral"] is True
    assert len(kwargs["view"].children[0].options) == 25


@pytest.mark.asyncio
async def test_panel_character_search_reports_no_match(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    modal = wow_cog_mod.PanelCharacterSearchModal(cog)
    modal.query._value = "Voidok"
    interaction = DummyReviewInteraction(DummyUser(42))

    await modal.on_submit(interaction)

    assert "Kein Black-Lotus-Charakter" in interaction.response.messages[0][0]


@pytest.mark.asyncio
async def test_panel_roster_select_claims_character(tmp_path, patch_logged_task):
    channel = DummyChannel()
    cog = await create_cog(tmp_path, patch_logged_task, channel=channel)
    await cog.data.replace_snapshot([member(name="Voidok")])
    view = wow_cog_mod.PanelRosterCharacterSelectView(cog, 42, [member(name="Voidok")])
    select = view.children[0]
    select._values = ["Voidok"]
    interaction = DummyReviewInteraction(DummyUser(42))

    await select.callback(interaction)

    assert "verbunden" in interaction.response.edits[0]["content"]
    assert await cog.data.get_claim_by_name("Voidok")


@pytest.mark.asyncio
async def test_panel_profession_flow_saves_profile(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    claim, _ = await cog.data.create_claim(member(name="Voidok"), 42)

    char_view = wow_cog_mod.PanelOwnedCharacterSelectView(
        cog, 42, [claim], "profession"
    )
    char_select = char_view.children[0]
    char_select._values = ["Voidok"]
    char_interaction = DummyReviewInteraction(DummyUser(42))
    await char_select.callback(char_interaction)
    assert isinstance(
        char_interaction.response.edits[0]["view"],
        wow_cog_mod.PanelProfessionSelectView,
    )
    assert "Hauptberuf 1: frei" in char_interaction.response.edits[0]["content"]
    assert "Kochen: frei" in char_interaction.response.edits[0]["content"]

    profession_view = char_interaction.response.edits[0]["view"]
    profession_select = profession_view.children[0]
    profession_select._values = ["alchemy"]
    modal_interaction = DummyReviewInteraction(DummyUser(42))
    await profession_select.callback(modal_interaction)
    modal = modal_interaction.response.modals[0]
    modal.skill._value = "250"
    modal.specialization._value = "Elixiere"
    save_interaction = DummyReviewInteraction(DummyUser(42))
    await modal.on_submit(save_interaction)

    assert "Gespeichert" in save_interaction.response.messages[0][0]
    profiles = await cog.data.professions_for_user(42)
    assert profiles[0].profession_id == "alchemy"


@pytest.mark.asyncio
async def test_panel_profession_modal_rejects_invalid_skill(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    claim, _ = await cog.data.create_claim(member(name="Voidok"), 42)
    modal = wow_cog_mod.PanelProfessionEditModal(cog, claim, "alchemy")
    modal.skill._value = "abc"
    modal.specialization._value = ""
    interaction = DummyReviewInteraction(DummyUser(42))

    await modal.on_submit(interaction)

    assert "Skill muss" in interaction.response.messages[0][0]


@pytest.mark.asyncio
async def test_panel_recipes_flow_opens_recipe_selection(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    claim, _ = await cog.data.create_claim(member(name="Voidok"), 42)
    await cog.data.set_character_profession(claim, "alchemy", 75)

    char_view = wow_cog_mod.PanelOwnedCharacterSelectView(cog, 42, [claim], "recipes")
    char_select = char_view.children[0]
    char_select._values = ["Voidok"]
    char_interaction = DummyReviewInteraction(DummyUser(42))
    await char_select.callback(char_interaction)
    recipe_profession_view = char_interaction.response.edits[0]["view"]
    assert isinstance(
        recipe_profession_view, wow_cog_mod.PanelRecipeProfessionSelectView
    )

    profession_select = recipe_profession_view.children[0]
    profession_select._values = ["alchemy"]
    recipe_interaction = DummyReviewInteraction(DummyUser(42))
    await profession_select.callback(recipe_interaction)

    assert "Offene Spezialrezepte" in recipe_interaction.response.edits[0]["content"]
    assert isinstance(
        recipe_interaction.response.edits[0]["view"],
        wow_cog_mod.CraftingRecipeSelectionView,
    )


@pytest.mark.asyncio
async def test_panel_recipes_requires_profession(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    claim, _ = await cog.data.create_claim(member(name="Voidok"), 42)
    view = wow_cog_mod.PanelOwnedCharacterSelectView(cog, 42, [claim], "recipes")
    select = view.children[0]
    select._values = ["Voidok"]
    interaction = DummyReviewInteraction(DummyUser(42))

    await select.callback(interaction)

    assert "noch keine Berufe" in interaction.response.edits[0]["content"]


@pytest.mark.asyncio
async def test_panel_crafting_search_modal_uses_search(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    modal = wow_cog_mod.PanelCraftingSearchModal(cog)
    modal.item._value = "Swiftnesstrank"
    interaction = DummyReviewInteraction(DummyUser(42))

    await modal.on_submit(interaction)

    assert "Spezialrezept" in interaction.response.messages[0][0]


@pytest.mark.asyncio
async def test_character_display_label_plain_for_main(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    main_char = member(key="id:1", name="Lyxendra", guild_rank=wow_cog_mod.MEMBER_RANK)
    await cog.data.replace_snapshot([main_char])
    await cog.data.create_claim(main_char, 42)

    assert await cog.character_display_label("id:1") == "Lyxendra"


@pytest.mark.asyncio
async def test_character_display_label_shows_relation_for_twink_and_bank(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)
    main_char = member(key="id:1", name="Lyxendra", guild_rank=wow_cog_mod.MEMBER_RANK)
    twink = member(key="id:2", name="Voidok", guild_rank=wow_cog_mod.TWINK_RANK)
    bank = member(key="id:3", name="Weedlager", guild_rank=wow_cog_mod.BANK_RANK)
    await cog.data.replace_snapshot([main_char, twink, bank])
    await cog.data.create_claim(main_char, 42)
    await cog.data.create_claim(twink, 42)
    await cog.data.create_claim(bank, 42)

    assert await cog.character_display_label("id:2") == "Voidok (Twink von Lyxendra)"
    assert await cog.character_display_label("id:3") == "Weedlager (Bank von Lyxendra)"


@pytest.mark.asyncio
async def test_character_display_label_omits_relation_when_no_main_found(
    tmp_path, patch_logged_task
):
    """A Twink whose owner has no Member+ char (the 'no_main_users' case) -
    show the plain relation without a dangling 'von' clause."""
    cog = await create_cog(tmp_path, patch_logged_task)
    twink = member(key="id:2", name="Voidok", guild_rank=wow_cog_mod.TWINK_RANK)
    await cog.data.replace_snapshot([twink])
    await cog.data.create_claim(twink, 42)

    assert await cog.character_display_label("id:2") == "Voidok (Twink)"


@pytest.mark.asyncio
async def test_character_display_label_officer_twink_not_treated_as_main(
    tmp_path, patch_logged_task
):
    """Officer Twink is a sanctioned SECOND high-rank char, not a 'Main' -
    a Bank/Twink char's relation must not attribute to it."""
    cog = await create_cog(tmp_path, patch_logged_task)
    officer_twink = member(
        key="id:1", name="Zweitoffi", guild_rank=wow_cog_mod.OFFICER_TWINK_RANK
    )
    bank = member(key="id:2", name="Weedlager", guild_rank=wow_cog_mod.BANK_RANK)
    await cog.data.replace_snapshot([officer_twink, bank])
    await cog.data.create_claim(officer_twink, 42)
    await cog.data.create_claim(bank, 42)

    assert await cog.character_display_label("id:2") == "Weedlager (Bank)"


@pytest.mark.asyncio
async def test_profession_practitioner_title_maps_known_professions(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)

    assert cog.profession_practitioner_title("blacksmithing") == "Schmied"
    assert cog.profession_practitioner_title("enchanting") == "Verzauberer"
    assert cog.profession_practitioner_title(None) is None
    # No mapped title and no static record (empty test data) -> falls back
    # to _profession_name, which itself falls back to the raw id.
    assert cog.profession_practitioner_title("mining") == "mining"


def test_crafting_search_response_view_maps_statuses():
    cog = wow_cog_mod.WoWCog.__new__(wow_cog_mod.WoWCog)  # no __init__ needed here
    ambiguous = type("R", (), {"status": "ambiguous_item", "candidates": []})()
    ok = type("R", (), {"status": "ok"})()
    no_crafter = type("R", (), {"status": "no_crafter"})()
    not_found = type("R", (), {"status": "item_not_found"})()

    assert isinstance(
        cog.crafting_search_response_view(ambiguous, 42),
        wow_cog_mod.CraftingSearchSuggestionView,
    )
    assert isinstance(
        cog.crafting_search_response_view(ok, 42),
        wow_cog_mod.CraftingSearchMarketplaceActionsView,
    )
    assert isinstance(
        cog.crafting_search_response_view(no_crafter, 42),
        wow_cog_mod.CraftingSearchMarketplaceActionsView,
    )
    assert cog.crafting_search_response_view(not_found, 42) is None


@pytest.mark.asyncio
async def test_crafting_search_suggestion_select_runs_selected_search(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    voidok = member(name="Voidok")
    # find_crafters now requires the char to be in the live roster (not
    # ghost, not in death_events) — so seed the snapshot here too.
    await cog.data.replace_snapshot([voidok])
    claim, _ = await cog.data.create_claim(voidok, 42)
    await cog.data.set_character_profession(claim, "alchemy", 75)
    result = await cog.search_crafting("Schwacher")
    view = wow_cog_mod.CraftingSearchSuggestionView(cog, 42, result.candidates)
    select = view.children[0]
    select._values = ["item.118"]
    interaction = DummyReviewInteraction(DummyUser(42))

    await select.callback(interaction)

    assert "kann gecraftet werden" in interaction.response.edits[0]["content"]
    # A resolved ("ok") result now carries the Marktplatz-bridge button
    # instead of no view - it's no longer just informational text.
    assert isinstance(
        interaction.response.edits[0]["view"],
        wow_cog_mod.CraftingSearchMarketplaceActionsView,
    )


@pytest.mark.asyncio
async def test_daily_digest_sleep_targets_next_9am(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)

    before_9 = datetime(2026, 5, 12, 8, 30, tzinfo=wow_cog_mod.DIGEST_TIMEZONE)
    after_9 = datetime(2026, 5, 12, 9, 30, tzinfo=wow_cog_mod.DIGEST_TIMEZONE)

    assert cog._seconds_until_next_digest(before_9) == 30 * 60
    assert cog._seconds_until_next_digest(after_9) == 23.5 * 60 * 60


@pytest.mark.asyncio
async def test_daily_digest_due_catches_up_after_9am(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    now = datetime(2026, 5, 12, 9, 40, tzinfo=wow_cog_mod.DIGEST_TIMEZONE)

    assert await cog._scheduled_digest_due(now)

    await cog.data.set_setting("last_scan_at", "2026-05-12T07:05:00")
    assert not await cog._scheduled_digest_due(now)


@pytest.mark.asyncio
async def test_daily_digest_not_due_before_9am(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    now = datetime(2026, 5, 12, 8, 40, tzinfo=wow_cog_mod.DIGEST_TIMEZONE)

    assert not await cog._scheduled_digest_due(now)


@pytest.mark.asyncio
async def test_new_character_does_not_create_milestone(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    milestones = await cog._detect_milestones({}, [member(level=60)])
    assert milestones == []


@pytest.mark.asyncio
async def test_level_increase_below_milestone_is_ignored(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    previous = {"id:1": member(level=30)}
    milestones = await cog._detect_milestones(previous, [member(level=31)])
    assert milestones == []


@pytest.mark.asyncio
async def test_level_increase_crossing_milestones(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    previous = {"id:1": member(level=49)}
    milestones = await cog._detect_milestones(previous, [member(level=52)])
    assert [m.level for m in milestones] == [50]


@pytest.mark.asyncio
async def test_duplicate_milestone_is_ignored(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    await cog.data.record_milestone("id:1", 50)
    previous = {"id:1": member(level=49)}
    milestones = await cog._detect_milestones(previous, [member(level=50)])
    assert milestones == []


@pytest.mark.asyncio
async def test_missing_channel_does_not_crash(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task, channel=None)
    await cog.set_announcement_channel(123)
    posted = await cog._post_milestones([wow_cog_mod.Milestone(member(), 30)])
    assert posted == 0


@pytest.mark.asyncio
async def test_scan_dry_run_does_not_post_or_persist(tmp_path, patch_logged_task):
    channel = DummyChannel()
    cog = await create_cog(tmp_path, patch_logged_task, channel=channel)
    await cog.data.replace_snapshot([member(level=49)])

    async def fake_roster(session=None):
        return [member(level=50)]

    cog.fetch_roster = fake_roster
    result = await cog.scan(post=False, persist=False)

    assert len(result.milestones) == 1
    assert result.posted == 0
    assert channel.sent == []
    snapshot = await cog.data.get_snapshot()
    assert snapshot["id:1"].level == 49


@pytest.mark.asyncio
async def test_activity_detects_new_character(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    activity = await cog._detect_activity({}, [member(name="Voidok", level=23)])

    assert [m.name for m in activity.new_members] == ["Voidok"]
    assert activity.milestones == []
    assert activity.public_count == 1


@pytest.mark.asyncio
async def test_level_31_no_longer_announces(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    previous = {"id:1": member(level=30)}

    milestones = await cog._detect_milestones(previous, [member(level=31)])

    assert milestones == []


@pytest.mark.asyncio
async def test_digest_posts_one_message_for_multiple_events(
    tmp_path, patch_logged_task, monkeypatch
):
    channel = DummyChannel()
    cog = await create_cog(tmp_path, patch_logged_task, channel=channel)
    # Announcement and officer channels are distinct in reality — the sync
    # report posts to the officer channel, so it must not land on the
    # announcement channel this test counts.
    cog.bot = MultiChannelBot(public_channel=channel, officer_channel=DummyChannel())
    await cog.set_announcement_channel(123)
    await cog.data.replace_snapshot([member(level=49)])

    async def fake_roster(session=None):
        return [
            member(level=50),
            member(key="id:2", name="Voidok", level=23, class_id=1, race_id=8),
        ]

    monkeypatch.setattr(wow_cog_mod.random, "choice", lambda seq: seq[0])
    cog.fetch_roster = fake_roster
    result = await cog.scan(post=True, persist=True)

    assert result.posted == 1
    assert len(channel.sent) == 1
    msg = channel.sent[0][0]
    assert "**Voidok**, Level **23**, Troll, Krieger" in msg
    assert "**Lyxendra**, Level **50**, Troll, Schurke" in msg


@pytest.mark.asyncio
async def test_digest_mentions_claimed_character(
    tmp_path, patch_logged_task, monkeypatch
):
    channel = DummyChannel()
    cog = await create_cog(tmp_path, patch_logged_task, channel=channel)
    claimed = member(level=39, class_id=1, race_id=8)
    await cog.data.replace_snapshot([claimed])
    await cog.data.create_claim(claimed, 42)
    activity = wow_cog_mod.ActivityDiff(
        new_members=[],
        milestones=[wow_cog_mod.Milestone(member(level=40, class_id=1, race_id=8), 40)],
        deaths=[],
        officer_notes=[],
    )
    monkeypatch.setattr(wow_cog_mod.random, "choice", lambda seq: seq[0])

    msg = await cog.format_activity_digest(activity)

    assert "**Lyxendra**, Level **40**, Troll, Krieger - <@42>" in msg
    assert "+10 Champion-Punkte" in msg


@pytest.mark.asyncio
async def test_level_60_new_member_digest_includes_item_level(
    tmp_path, patch_logged_task, monkeypatch
):
    cog = await create_cog(tmp_path, patch_logged_task)
    newcomer = member(name="Voidok", level=60, class_id=1, race_id=8)
    await cog.data.set_gear_snapshot(newcomer.character_key, 63.4, 15)
    activity = wow_cog_mod.ActivityDiff(
        new_members=[newcomer],
        milestones=[],
        deaths=[],
        officer_notes=[],
    )
    monkeypatch.setattr(wow_cog_mod.random, "choice", lambda seq: seq[0])

    msg = await cog.format_activity_digest(activity)

    assert "**Voidok**, Level **60**, Troll, Krieger, Ø iLvl **63.4**" in msg


@pytest.mark.asyncio
async def test_gear_refresh_records_item_level_milestone(
    tmp_path, patch_logged_task, monkeypatch
):
    cog = await create_cog(tmp_path, patch_logged_task)
    lvl_60 = member(name="Voidok", level=60)
    await cog.data.set_gear_snapshot(lvl_60.character_key, 59.8, 0)

    async def fake_profile(*args, **kwargs):
        return {"is_ghost": False, "equipped_item_level": 61}

    monkeypatch.setattr(wow_cog_mod, "fetch_character_profile", fake_profile)

    await cog._refresh_member_profiles([lvl_60])
    events = await cog.data.pending_gear_milestone_events()

    assert len(events) == 1
    assert events[0].threshold == 60
    assert events[0].average_item_level == 61.0
    assert events[0].points == 5


@pytest.mark.asyncio
async def test_record_public_events_awards_gear_milestone_points(
    tmp_path, patch_logged_task
):
    champion = DummyChampionCog()
    patch_logged_task(wow_cog_mod, log_setup)
    cog = WoWCog(ChampionBot(champion=champion))
    cog.data = WoWData(str(tmp_path / "wow.db"))
    CREATED_COGS.append(cog)
    claimed = member(name="Voidok", level=60)
    await cog.data.replace_snapshot([claimed])
    await cog.data.create_claim(claimed, 42)
    await cog.data.record_gear_milestone(claimed.character_key, 65, 66.4, 3)
    activity = wow_cog_mod.ActivityDiff(
        new_members=[],
        milestones=[],
        deaths=[],
        officer_notes=[],
        gear_events=await cog.data.pending_gear_milestone_events(),
    )
    msg = await cog.format_activity_digest(activity)

    await cog._record_public_events(activity)
    await cog._record_public_events(activity)

    assert "+3 Champion-Punkte" in msg
    assert champion.updates == [(42, 3, "WoW-iLvl-Meilenstein: Voidok Ø iLvl 65")]


@pytest.mark.asyncio
async def test_roster_ghost_death_uses_refreshed_item_level(
    tmp_path, patch_logged_task, monkeypatch
):
    channel = DummyChannel()
    cog = await create_cog(tmp_path, patch_logged_task, channel=channel)
    await cog.set_announcement_channel(123)
    await cog.data.replace_snapshot([member(level=60, is_ghost=False)])

    async def fake_roster(session=None):
        return [member(level=60, is_ghost=True)]

    async def fake_profile(*args, **kwargs):
        return {"is_ghost": True, "equipped_item_level": 68}

    monkeypatch.setattr(wow_cog_mod, "fetch_character_profile", fake_profile)
    monkeypatch.setattr(wow_cog_mod.random, "choice", lambda seq: seq[0])
    cog.fetch_roster = fake_roster

    result = await cog.scan(post=True, persist=True)

    assert len(result.deaths) == 1
    assert "Ø iLvl **68.0**" in channel.sent[0][0]


@pytest.mark.asyncio
async def test_record_public_events_awards_claimed_milestone_points(
    tmp_path, patch_logged_task
):
    channel = DummyChannel()
    champion = DummyChampionCog()
    patch_logged_task(wow_cog_mod, log_setup)
    cog = WoWCog(ChampionBot(channel=channel, champion=champion))
    cog.data = WoWData(str(tmp_path / "wow.db"))
    CREATED_COGS.append(cog)
    claimed = member(level=59, class_id=1, race_id=8)
    await cog.data.create_claim(claimed, 42)
    activity = wow_cog_mod.ActivityDiff(
        new_members=[],
        milestones=[wow_cog_mod.Milestone(member(level=60, class_id=1, race_id=8), 60)],
        deaths=[],
        officer_notes=[],
    )

    await cog._record_public_events(activity)

    assert champion.updates == [(42, 50, "WoW-Meilenstein: Lyxendra Level 60")]


@pytest.mark.asyncio
async def test_disappeared_dead_profile_creates_public_death(
    tmp_path, patch_logged_task, monkeypatch
):
    cog = await create_cog(tmp_path, patch_logged_task)
    previous = {"id:1": member(name="Voidok", level=37)}

    async def fake_profile(*args, **kwargs):
        return {"is_ghost": True}

    monkeypatch.setattr(wow_cog_mod, "fetch_character_profile", fake_profile)
    activity = await cog._detect_activity(previous, [])

    assert [d.member.name for d in activity.deaths] == ["Voidok"]
    assert activity.deaths[0].confirmed is True
    assert activity.officer_notes == []
    assert "Level **37**" in cog._format_death_line(activity.deaths[0])


@pytest.mark.asyncio
async def test_disappeared_alive_profile_creates_officer_note(
    tmp_path, patch_logged_task, monkeypatch
):
    cog = await create_cog(tmp_path, patch_logged_task)
    previous = {"id:1": member(name="Voidok")}

    async def fake_profile(*args, **kwargs):
        return {"is_ghost": False}

    monkeypatch.setattr(wow_cog_mod, "fetch_character_profile", fake_profile)
    activity = await cog._detect_activity(previous, [])

    assert activity.deaths == []
    assert "nicht mehr Teil" in activity.officer_notes[0].message


@pytest.mark.asyncio
async def test_disappeared_unknown_profile_creates_presumed_death(
    tmp_path, patch_logged_task, monkeypatch
):
    cog = await create_cog(tmp_path, patch_logged_task)
    previous = {"id:1": member(name="Voidok", level=42)}

    async def fake_profile(*args, **kwargs):
        raise wow_cog_mod.WoWAPIError("not found", status=404)

    monkeypatch.setattr(wow_cog_mod, "fetch_character_profile", fake_profile)
    activity = await cog._detect_activity(previous, [])

    assert activity.officer_notes == []
    assert [d.member.name for d in activity.deaths] == ["Voidok"]
    assert activity.deaths[0].confirmed is False
    death_line = cog._format_death_line(activity.deaths[0])
    assert "Level **42**" in death_line
    # Confirmed and unconfirmed cases are reported uniformly as "gefallen" —
    # no more hedging language.
    assert "wahrscheinlich" not in death_line
    assert "verschwunden" not in death_line
    assert "gefallen" in death_line


@pytest.mark.asyncio
async def test_disappeared_alive_officer_note_includes_claim_and_new_guild(
    tmp_path, patch_logged_task, monkeypatch
):
    """The officer note for a left-the-guild char should expose level/class,
    the claim owner (if any) and the character's current guild status."""
    cog = await create_cog(tmp_path, patch_logged_task)
    target = member(name="Voidok", level=42, class_id=5, race_id=5)
    previous = {"id:1": target}
    # Seed roster_first_seen by snapshotting target before they "leave".
    await cog.data.replace_snapshot([target])
    claim, _ = await cog.data.create_claim(target, 1234)

    async def fake_profile(*args, **kwargs):
        return {"is_ghost": False, "guild": {"name": "Random Pugs"}}

    monkeypatch.setattr(wow_cog_mod, "fetch_character_profile", fake_profile)
    activity = await cog._detect_activity(previous, [])

    assert len(activity.officer_notes) == 1
    msg = activity.officer_notes[0].message
    # Header substring preserved for downstream tests.
    assert "nicht mehr Teil" in msg
    assert "Voidok" in msg
    # Identity line: level + race + class.
    assert "Level **42**" in msg
    assert "Priester" in msg
    # Claim and new guild are surfaced.
    assert "<@1234>" in msg
    assert "Random Pugs" in msg
    # First-seen line is present (formatted as YYYY-MM-DD). Wording is
    # "Bei uns gelistet seit" — honest about the fact that for pre-bot
    # chars this is only the bot-tracking start, not the actual join.
    assert "Bei uns gelistet seit:" in msg
    # Date format is YYYY-MM-DD (10 chars), matched by digit pattern check.
    import re

    assert re.search(r"Bei uns gelistet seit: \*\*\d{4}-\d{2}-\d{2}\*\*", msg)


@pytest.mark.asyncio
async def test_disappeared_alive_officer_note_guildless_and_unclaimed(
    tmp_path, patch_logged_task, monkeypatch
):
    cog = await create_cog(tmp_path, patch_logged_task)
    previous = {"id:1": member(name="Voidok")}

    async def fake_profile(*args, **kwargs):
        # No 'guild' key → guildless.
        return {"is_ghost": False}

    monkeypatch.setattr(wow_cog_mod, "fetch_character_profile", fake_profile)
    activity = await cog._detect_activity(previous, [])

    msg = activity.officer_notes[0].message
    assert "nicht mehr Teil" in msg
    assert "_nicht geclaimed_" in msg
    assert "_gildenlos_" in msg


@pytest.mark.asyncio
async def test_public_death_from_roster_ghost_is_deduplicated(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)
    previous = {"id:1": member(is_ghost=False)}
    current = [member(is_ghost=True)]

    activity = await cog._detect_activity(previous, current)
    assert len(activity.deaths) == 1
    await cog._record_public_events(activity)

    activity = await cog._detect_activity(previous, current)
    assert activity.deaths == []


@pytest.mark.asyncio
async def test_only_officer_notes_persist_without_public_channel(
    tmp_path, patch_logged_task, monkeypatch
):
    officer = DummyChannel()
    bot = MultiChannelBot(public_channel=None, officer_channel=officer)
    patch_logged_task(wow_cog_mod, log_setup)
    cog = WoWCog(bot)
    cog.data = WoWData(str(tmp_path / "wow.db"))
    CREATED_COGS.append(cog)
    await cog.data.replace_snapshot([member(name="Voidok")])

    async def fake_roster(session=None):
        return []

    async def fake_profile(*args, **kwargs):
        return {"is_ghost": False}

    monkeypatch.setattr(wow_cog_mod, "fetch_character_profile", fake_profile)
    cog.fetch_roster = fake_roster
    result = await cog.scan(post=True, persist=True)

    assert result.posted == 0
    assert "nicht mehr Teil" in officer.sent[0][0]
    assert await cog.data.member_count() == 0


@pytest.mark.asyncio
async def test_claim_character_creates_unverified_claim_and_review(
    tmp_path, patch_logged_task
):
    channel = DummyChannel()
    cog = await create_cog(tmp_path, patch_logged_task, channel=channel)
    await cog.data.replace_snapshot([member(name="Lyxendra")])

    result = await cog.claim_character(42, "lyxendra")

    assert result.created is True
    assert result.reason == "created"
    assert result.review_posted is True
    claim = await cog.data.get_claim("id:1")
    assert claim.discord_user_id == 42
    assert claim.status == "unverified"
    assert claim.review_message_id == 555
    assert "<@42>" in channel.sent[0][0]
    assert channel.sent[0][1]["view"] is not None


@pytest.mark.asyncio
async def test_claim_character_rejects_unknown_and_taken(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task, channel=DummyChannel())
    await cog.data.replace_snapshot([member(name="Lyxendra")])

    missing = await cog.claim_character(42, "Unknown")
    assert missing.reason == "not_found"

    created = await cog.claim_character(42, "Lyxendra")
    assert created.reason == "created"
    own_repeat = await cog.claim_character(42, "Lyxendra")
    assert own_repeat.reason == "already_own"
    taken = await cog.claim_character(43, "Lyxendra")
    assert taken.reason == "taken"


@pytest.mark.asyncio
async def test_claim_review_verify_button_marks_claim_verified(
    tmp_path, patch_logged_task
):
    channel = DummyChannel()
    cog = await create_cog(tmp_path, patch_logged_task, channel=channel)
    await cog.data.replace_snapshot([member(name="Lyxendra")])
    await cog.claim_character(42, "Lyxendra")
    view = wow_cog_mod.ClaimReviewView(cog)

    interaction = DummyReviewInteraction(DummyUser(99, manage_guild=True))
    await view.children[0].callback(interaction)

    claim = await cog.data.get_claim("id:1")
    assert claim.status == "verified"
    assert claim.verified_by == 99
    assert interaction.response.edits[0]["view"] is None
    assert "Bestätigt" in interaction.response.edits[0]["content"]


@pytest.mark.asyncio
async def test_claim_review_reject_button_removes_claim(tmp_path, patch_logged_task):
    channel = DummyChannel()
    cog = await create_cog(tmp_path, patch_logged_task, channel=channel)
    await cog.data.replace_snapshot([member(name="Lyxendra")])
    await cog.claim_character(42, "Lyxendra")
    view = wow_cog_mod.ClaimReviewView(cog)

    interaction = DummyReviewInteraction(DummyUser(99, manage_guild=True))
    await view.children[1].callback(interaction)

    assert await cog.data.get_claim("id:1") is None
    assert interaction.response.edits[0]["view"] is None
    assert "Abgelehnt" in interaction.response.edits[0]["content"]


@pytest.mark.asyncio
async def test_claim_review_button_rejects_non_mod(tmp_path, patch_logged_task):
    channel = DummyChannel()
    cog = await create_cog(tmp_path, patch_logged_task, channel=channel)
    await cog.data.replace_snapshot([member(name="Lyxendra")])
    await cog.claim_character(42, "Lyxendra")
    view = wow_cog_mod.ClaimReviewView(cog)

    interaction = DummyReviewInteraction(DummyUser(77, manage_guild=False))
    await view.children[0].callback(interaction)

    claim = await cog.data.get_claim("id:1")
    assert claim.status == "unverified"
    assert interaction.response.messages[0][1]["ephemeral"] is True


@pytest.mark.asyncio
async def test_crafting_set_profile_requires_own_claim_or_mod(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    await cog.data.replace_snapshot([member(name="Voidok")])
    await cog.claim_character(42, "Voidok")

    own = await cog.set_crafting_profile(42, "Voidok", "Alchemie", 250, "Elixiere")
    assert own.reason == "saved"
    assert own.profession.skill_level == 250

    forbidden = await cog.set_crafting_profile(43, "Voidok", "Alchemie", 260, None)
    assert forbidden.reason == "forbidden"

    mod = await cog.set_crafting_profile(
        43, "Voidok", "Alchemy", 260, None, is_mod=True
    )
    assert mod.reason == "saved"
    assert mod.profession.skill_level == 260


@pytest.mark.asyncio
async def test_crafting_set_limits_primary_professions_but_allows_cooking(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    claim, _ = await cog.data.create_claim(member(name="Voidok"), 42)
    await cog.data.set_character_profession(claim, "alchemy", 250)
    await cog.data.set_character_profession(claim, "blacksmithing", 200)

    third_primary = await cog.set_crafting_profile(
        42, "Voidok", "engineering", 100, None
    )
    cooking = await cog.set_crafting_profile(42, "Voidok", "cooking", 100, None)

    assert third_primary.reason == "primary_limit"
    assert cooking.reason == "saved"
    assert cooking.profession.profession_id == "cooking"


@pytest.mark.asyncio
async def test_profession_choices_exclude_unwanted_secondaries_and_slot_limit(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    claim, _ = await cog.data.create_claim(member(name="Voidok"), 42)
    await cog.data.set_character_profession(claim, "alchemy", 250)
    await cog.data.set_character_profession(claim, "blacksmithing", 200)
    profiles = await cog.data.professions_for_character(claim.character_key)

    all_choices = {value for _, value in cog.profession_choices("")}
    char_choices = {value for _, value, _ in cog.profession_choices_for_claim(profiles)}

    assert "first-aid" not in all_choices
    assert "fishing" not in all_choices
    assert "engineering" not in char_choices
    assert {"alchemy", "blacksmithing", "cooking"} <= char_choices


@pytest.mark.asyncio
async def test_crafting_search_finds_claimed_trainer_recipe(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    await cog.data.replace_snapshot([member(name="Voidok")])
    claim, _ = await cog.data.create_claim(member(name="Voidok"), 42)
    await cog.data.set_character_profession(claim, "alchemy", 75)

    result = await cog.search_crafting("Schwacher Heiltrank")

    assert result.status == "ok"
    assert result.crafters[0].character_name == "Voidok"
    assert "Voidok" in cog.format_crafting_search_result(result)


@pytest.mark.asyncio
async def test_crafting_search_tolerates_typos(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    await cog.data.replace_snapshot([member(name="Voidok")])
    claim, _ = await cog.data.create_claim(member(name="Voidok"), 42)
    await cog.data.set_character_profession(claim, "alchemy", 75)

    result = await cog.search_crafting("Schwacher Heiltrnak")

    assert result.status == "ok"
    assert result.item["id"] == "item.118"


@pytest.mark.asyncio
async def test_crafting_search_ignores_unclaimed_crafters(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    await cog.data.replace_snapshot([member(name="Voidok")])
    claim, _ = await cog.data.create_claim(member(name="Voidok"), 42)
    await cog.data.set_character_profession(claim, "alchemy", 75)
    await cog.data.remove_claim(claim.character_key)

    result = await cog.search_crafting("Minor Healing Potion")

    assert result.status == "no_crafter"


@pytest.mark.asyncio
async def test_crafting_search_reports_manual_recipe_and_ambiguity(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}

    manual = await cog.search_crafting("Swiftnesstrank")
    ambiguous = await cog.search_crafting("Schwacher")

    assert manual.status == "manual_recipe"
    assert "Spezialrezept" in cog.format_crafting_search_result(manual)
    assert ambiguous.status == "ambiguous_item"


@pytest.mark.asyncio
async def test_crafting_search_finds_saved_manual_recipe(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    await cog.data.replace_snapshot([member(name="Voidok")])
    claim, _ = await cog.data.create_claim(member(name="Voidok"), 42)
    await cog.data.set_character_profession(claim, "alchemy", 75)
    await cog.data.add_known_recipes(claim.character_key, "alchemy", ["spell.2335"])

    result = await cog.search_crafting("Swiftnesstrank")

    assert result.status == "ok"
    assert result.manual_recipe is True
    assert result.crafters[0].character_name == "Voidok"
    assert "Spezialrezept gepflegt" in cog.format_crafting_search_result(result)


@pytest.mark.asyncio
async def test_crafting_search_finds_enchant_by_name_de_en(tmp_path, patch_logged_task):
    """Crusader is a weapon enchant (no item output). Searching the German
    OR English name must find an enchanter who maintained the formula."""
    from lotus_bot.bot import load_wow_data

    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": load_wow_data("data/wow/classic_hc")}
    enchanter = member(name="Zappy")
    await cog.data.replace_snapshot([enchanter])
    claim, _ = await cog.data.create_claim(enchanter, 42)
    await cog.data.set_character_profession(claim, "enchanting", 300)
    await cog.data.add_known_recipes(claim.character_key, "enchanting", ["spell.20034"])

    for term in ("Kreuzfahrer", "Crusader"):
        result = await cog.search_crafting(term)
        assert result.status == "ok", f"{term} → {result.status}"
        assert result.crafters[0].character_name == "Zappy"
        assert "Kreuzfahrer" in cog.format_crafting_search_result(result)


@pytest.mark.asyncio
async def test_crafting_search_enchant_manual_needs_learned(
    tmp_path, patch_logged_task
):
    """A drop-formula enchant (Crusader) with an enchanter who has the skill
    but did NOT maintain the formula → manual_recipe (nobody pinned it)."""
    from lotus_bot.bot import load_wow_data

    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": load_wow_data("data/wow/classic_hc")}
    enchanter = member(name="Zappy")
    await cog.data.replace_snapshot([enchanter])
    claim, _ = await cog.data.create_claim(enchanter, 42)
    await cog.data.set_character_profession(claim, "enchanting", 300)
    # No add_known_recipes — formula not maintained.

    result = await cog.search_crafting("Crusader")
    assert result.status == "manual_recipe"


@pytest.mark.asyncio
async def test_crafting_search_trainer_enchant_any_enchanter(
    tmp_path, patch_logged_task
):
    """Trainer enchants have no formula item at all. Any enchanter with
    enough skill counts — covers the 62 trainer enchants the item-only
    search could never reach. (spell.7420 = Enchant Chest - Minor Health,
    trainer, skill 15.)"""
    from lotus_bot.bot import load_wow_data

    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": load_wow_data("data/wow/classic_hc")}
    enchanter = member(name="Zappy")
    await cog.data.replace_snapshot([enchanter])
    claim, _ = await cog.data.create_claim(enchanter, 42)
    await cog.data.set_character_profession(claim, "enchanting", 100)

    # Resolve directly by id to bypass any name-ambiguity in the matcher.
    result = await cog.search_crafting_by_item_id("enchant:spell.7420")
    assert result.status == "ok"
    assert result.manual_recipe is False
    assert result.crafters[0].character_name == "Zappy"


@pytest.mark.asyncio
async def test_search_crafting_by_item_id_resolves_enchant(tmp_path, patch_logged_task):
    """The ambiguous-dropdown path: a click sends enchant:<spell_id>."""
    from lotus_bot.bot import load_wow_data

    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": load_wow_data("data/wow/classic_hc")}
    enchanter = member(name="Zappy")
    await cog.data.replace_snapshot([enchanter])
    claim, _ = await cog.data.create_claim(enchanter, 42)
    await cog.data.set_character_profession(claim, "enchanting", 300)
    await cog.data.add_known_recipes(claim.character_key, "enchanting", ["spell.20034"])

    result = await cog.search_crafting_by_item_id("enchant:spell.20034")
    assert result.status == "ok"
    assert result.crafters[0].character_name == "Zappy"
    # Unknown enchant id degrades gracefully.
    miss = await cog.search_crafting_by_item_id("enchant:spell.nonexistent")
    assert miss.status == "item_not_found"


@pytest.mark.asyncio
async def test_recipe_selection_filters_known_and_low_skill(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    await cog.data.replace_snapshot([member(name="Voidok")])
    claim, _ = await cog.data.create_claim(member(name="Voidok"), 42)
    await cog.data.set_character_profession(claim, "alchemy", 50)

    low_skill = await cog.prepare_recipe_selection(42, "Voidok", "alchemy", None)
    assert low_skill.status == "ok"
    assert low_skill.recipes == []

    await cog.data.set_character_profession(claim, "alchemy", 75)
    available = await cog.prepare_recipe_selection(42, "Voidok", "alchemy", None)
    assert [recipe["spell_id"] for recipe in available.recipes] == ["spell.2335"]

    saved = await cog.save_known_recipes(claim, "alchemy", ["spell.2335"])
    assert saved == 1
    available = await cog.prepare_recipe_selection(42, "Voidok", "alchemy", None)
    assert available.recipes == []


@pytest.mark.asyncio
async def test_recipe_selection_view_saves_multiple_recipes(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    claim, _ = await cog.data.create_claim(member(name="Voidok"), 42)
    profile = await cog.data.set_character_profession(claim, "alchemy", 75)
    recipe = crafting_data()["profession_recipes"][1]
    view = wow_cog_mod.CraftingRecipeSelectionView(cog, 42, claim, profile, [recipe])
    view.selected_spell_ids = {"spell.2335"}

    interaction = DummyReviewInteraction(DummyUser(42))
    await view.children[-1].callback(interaction)

    assert await cog.data.known_recipe_spell_ids(claim.character_key) == {"spell.2335"}
    assert "1 Rezepte" in interaction.response.edits[0]["content"]


def _build_addon_export(character: str, professions: list[dict]) -> str:
    """Build a LotusDiscordBridge-shaped export code, mirroring the addon's
    own JSON+base64 envelope, for exercising import_profession_export()."""
    payload = {
        "character": character,
        "version": 1,
        "addon": "LotusDiscordBridge",
        "exportedAt": 1,
        "realm": "Soulseeker",
        "data": {"professions": professions},
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


@pytest.mark.asyncio
async def test_import_profession_export_success(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    await cog.data.replace_snapshot([member(name="Voidok")])
    claim, _ = await cog.data.create_claim(member(name="Voidok"), 42)

    profession = {
        "professionId": "alchemy",
        "name": "Alchemy",
        "skillLevel": 75,
        "maxSkillLevel": 300,
        "recipes": [
            # Trainer recipes - only update character_professions.skill_level
            # (existing table), never character_known_recipes: the crafter
            # search already finds these dynamically via required_skill.
            {"name": "Minor Healing Potion", "itemId": 118, "idSource": "item"},
            {"name": "Minor Mana Potion", "itemId": 999, "idSource": "item"},
            # Non-trainer, hardcore_valid - IS points-eligible, goes into
            # the existing character_known_recipes (same table manual
            # curation writes to).
            {"name": "Swiftness Potion", "itemId": 2459, "idSource": "item"},
            # Not in our Wowhead data at all - must be logged as unmatched,
            # never silently dropped.
            {"name": "Totally Unknown Thing", "itemId": 424242, "idSource": "item"},
        ],
    }
    code = _build_addon_export("Voidok", [profession])

    result = await cog.import_profession_export(42, code)

    assert result.status == "ok"
    assert result.professions_imported == 1
    assert result.recipes_seen == 4
    assert result.special_recipes_learned == 1
    assert result.unmatched == [
        "alchemy: Totally Unknown Thing (item=424242, spell=None)"
    ]

    profile = await cog.data.get_character_profession(claim.character_key, "alchemy")
    assert profile.skill_level == 75

    # Only the non-trainer recipe feeds the existing points system.
    assert await cog.data.known_recipe_spell_ids(claim.character_key) == {"spell.2335"}

    # No new/redundant storage: the EXISTING crafter search must find this
    # character for both a trainer recipe (via skill_level) and the special
    # recipe (via character_known_recipes), completely unchanged.
    await cog.data.verify_claim(claim.character_key, 99)
    trainer_search = await cog.search_crafting_by_item_id("item.118")
    assert trainer_search.status == "ok"
    assert trainer_search.crafters[0].character_name == "Voidok"

    special_search = await cog.search_crafting_by_item_id("item.2459")
    assert special_search.status == "ok"
    assert special_search.crafters[0].character_name == "Voidok"


@pytest.mark.asyncio
async def test_import_profession_export_is_idempotent(tmp_path, patch_logged_task):
    """Re-importing the same export twice must not duplicate points/events -
    character_known_recipes already de-dupes via INSERT OR IGNORE."""
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    claim, _ = await cog.data.create_claim(member(name="Voidok"), 42)

    profession = {
        "professionId": "alchemy",
        "skillLevel": 75,
        "recipes": [{"name": "Swiftness Potion", "itemId": 2459}],
    }
    code = _build_addon_export("Voidok", [profession])

    first = await cog.import_profession_export(42, code)
    second = await cog.import_profession_export(42, code)

    assert first.special_recipes_learned == 1
    assert second.special_recipes_learned == 0  # already known, not re-awarded
    assert await cog.data.known_recipe_spell_ids(claim.character_key) == {"spell.2335"}

    # A follow-up import with a higher skill level updates it in place - the
    # existing character_professions upsert, unchanged.
    profession["skillLevel"] = 150
    await cog.import_profession_export(42, _build_addon_export("Voidok", [profession]))
    profile = await cog.data.get_character_profession(claim.character_key, "alchemy")
    assert profile.skill_level == 150


@pytest.mark.asyncio
async def test_import_profession_export_ownership_and_validation(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}

    # Malformed / non-addon codes are rejected, not raised as exceptions.
    assert (await cog.import_profession_export(42, "not-valid-base64!!")).status == (
        "invalid"
    )
    other_addon = base64.b64encode(json.dumps({"addon": "Other"}).encode()).decode()
    assert (await cog.import_profession_export(42, other_addon)).status == "invalid"

    profession = {"professionId": "alchemy", "skillLevel": 75, "recipes": []}
    code = _build_addon_export("Ghostface", [profession])
    assert (await cog.import_profession_export(42, code)).status == "not_claimed"

    await cog.data.create_claim(member(name="Voidok"), 42)
    code = _build_addon_export("Voidok", [])
    assert (await cog.import_profession_export(42, code)).status == "no_professions"

    code = _build_addon_export("Voidok", [profession])
    forbidden = await cog.import_profession_export(999, code)
    assert forbidden.status == "forbidden"
    # A moderator may import on behalf of someone else's character.
    as_mod = await cog.import_profession_export(999, code, is_mod=True)
    assert as_mod.status == "ok"


def test_format_profession_import_result_covers_all_statuses():
    cog = wow_cog_mod.WoWCog.__new__(wow_cog_mod.WoWCog)

    invalid = wow_cog_mod.ProfessionImportResult("invalid")
    assert "nicht gelesen werden" in cog.format_profession_import_result(invalid)

    not_claimed = wow_cog_mod.ProfessionImportResult(
        "not_claimed", character_name="Ghostface"
    )
    msg = cog.format_profession_import_result(not_claimed)
    assert "Ghostface" in msg and "nicht geclaimed" in msg

    claim = SimpleNamespace(character_name="Voidok")
    forbidden = wow_cog_mod.ProfessionImportResult("forbidden", claim=claim)
    assert "Voidok" in cog.format_profession_import_result(forbidden)
    assert "gehört nicht dir" in cog.format_profession_import_result(forbidden)

    no_professions = wow_cog_mod.ProfessionImportResult("no_professions")
    assert "keine Berufe" in cog.format_profession_import_result(no_professions)

    ok = wow_cog_mod.ProfessionImportResult(
        "ok",
        character_name="Voidok",
        professions_imported=1,
        recipes_seen=4,
        special_recipes_learned=1,
        unmatched=["alchemy: Foo"],
    )
    text = cog.format_profession_import_result(ok)
    assert "1 Beruf(e)" in text
    assert "🌟" in text and "1 davon" in text
    assert "⚠️ 1 Rezept(e)" in text


@pytest.mark.asyncio
async def test_panel_addon_import_modal_reports_formatted_result(
    tmp_path, patch_logged_task
):
    """End-to-end through the Panel's own modal (not the slash command) -
    proves the wiring, not just import_profession_export() itself."""
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    await cog.data.create_claim(member(name="Voidok"), 42)

    profession = {"professionId": "alchemy", "skillLevel": 75, "recipes": []}
    code = _build_addon_export("Voidok", [profession])

    modal = wow_cog_mod.PanelAddonImportModal(cog)
    modal.code._value = code
    interaction = _DummyRaiderInteraction(_DummyLookupUser(42), guild=None)

    await modal.on_submit(interaction)

    assert interaction.response.deferred is True
    content, kwargs = interaction.followup.messages[0]
    assert "Voidok" in content and "1 Beruf(e)" in content
    assert kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_my_chars_addon_import_opens_modal(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    voidok = member(name="Voidok")
    await cog.data.replace_snapshot([voidok])
    await cog.data.create_claim(voidok, 42)
    view = wow_cog_mod.PanelMyCharsView(cog, owner_user_id=42)
    interaction = DummyReviewInteraction(DummyUser(42))
    await view.send(interaction)

    await cog._my_chars_addon_import(interaction, view)

    assert isinstance(interaction.response.modals[0], wow_cog_mod.PanelAddonImportModal)


@pytest.mark.asyncio
async def test_rare_recipe_learning_creates_digest_event_and_points(
    tmp_path, patch_logged_task
):
    champion = DummyChampionCog()
    patch_logged_task(wow_cog_mod, log_setup)
    cog = WoWCog(ChampionBot(champion=champion))
    cog.data = WoWData(str(tmp_path / "wow.db"))
    CREATED_COGS.append(cog)
    cog.bot.data = {"wow": crafting_data()}
    claim, _ = await cog.data.create_claim(member(name="Voidok"), 42)
    await cog.data.verify_claim(claim.character_key, 99)
    claim = await cog.data.get_claim(claim.character_key)

    saved = await cog.save_known_recipes(claim, "alchemy", ["spell.2335"])
    activity = wow_cog_mod.ActivityDiff(
        new_members=[],
        milestones=[],
        deaths=[],
        officer_notes=[],
        recipe_events=await cog.data.pending_recipe_learning_events(),
    )
    msg = await cog.format_activity_digest(activity)
    await cog._record_public_events(activity)
    await cog._record_public_events(activity)

    assert saved == 1
    assert "Swiftnesstrank" in msg
    # Swiftness Potion: drop source (base 2) × skill 60 (multiplier 1.0) = 2 points.
    assert champion.updates == [(42, 2, "WoW-Rezept: Voidok lernt Swiftnesstrank")]


@pytest.mark.asyncio
async def test_vendor_recipe_learning_does_not_create_reward_event(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)
    data = crafting_data()
    data["profession_recipes"][1]["recipe_item_sources"] = ["vendor"]
    cog.bot.data = {"wow": data}
    claim, _ = await cog.data.create_claim(member(name="Voidok"), 42)
    await cog.data.verify_claim(claim.character_key, 99)
    claim = await cog.data.get_claim(claim.character_key)

    assert await cog.save_known_recipes(claim, "alchemy", ["spell.2335"]) == 1
    assert await cog.data.pending_recipe_learning_events() == []


@pytest.mark.asyncio
async def test_scan_missing_access_does_not_crash_or_persist(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task, channel=ForbiddenChannel())
    await cog.set_announcement_channel(123)
    await cog.data.replace_snapshot([member(level=49)])

    async def fake_roster(session=None):
        return [member(level=50)]

    cog.fetch_roster = fake_roster
    result = await cog.scan(post=True, persist=True)

    assert len(result.milestones) == 1
    assert result.posted == 0
    snapshot = await cog.data.get_snapshot()
    assert snapshot["id:1"].level == 49


@pytest.mark.asyncio
async def test_ghost_refresh_marks_dead_members_from_profile(
    tmp_path, patch_logged_task, monkeypatch
):
    cog = await create_cog(tmp_path, patch_logged_task)
    alive = member(key="id:1", name="Alive", level=60)
    dead = member(key="id:2", name="Dead", level=60)

    async def fake_profile(realm, name, **kwargs):
        return {"is_ghost": name.casefold() == "dead"}

    monkeypatch.setattr(wow_cog_mod, "fetch_character_profile", fake_profile)

    await cog._refresh_member_profiles([alive, dead])

    assert alive.is_ghost is False
    assert dead.is_ghost is True


@pytest.mark.asyncio
async def test_ghost_refresh_tolerates_api_failure(
    tmp_path, patch_logged_task, monkeypatch
):
    cog = await create_cog(tmp_path, patch_logged_task)
    target = member(name="Voidok", level=60)

    async def boom(*args, **kwargs):
        raise wow_cog_mod.WoWAPIError("forbidden", status=403)

    monkeypatch.setattr(wow_cog_mod, "fetch_character_profile", boom)

    await cog._refresh_member_profiles([target])

    assert target.is_ghost is False


@pytest.mark.asyncio
async def test_scan_detects_ghost_in_roster_via_profile(
    tmp_path, patch_logged_task, monkeypatch
):
    channel = DummyChannel()
    cog = await create_cog(tmp_path, patch_logged_task, channel=channel)
    await cog.set_announcement_channel(123)
    await cog.data.replace_snapshot([member(name="Gorokhan", level=60)])

    async def fake_roster(session=None):
        return [member(name="Gorokhan", level=60)]

    async def fake_profile(realm, name, **kwargs):
        return {"is_ghost": True}

    monkeypatch.setattr(wow_cog_mod, "fetch_character_profile", fake_profile)
    monkeypatch.setattr(wow_cog_mod.random, "choice", lambda seq: seq[0])
    cog.fetch_roster = fake_roster

    result = await cog.scan(post=True, persist=True)

    assert [d.member.name for d in result.deaths] == ["Gorokhan"]
    assert result.deaths[0].confirmed is True


@pytest.mark.asyncio
async def test_recipe_digest_includes_user_mention(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    claim, _ = await cog.data.create_claim(member(name="Voidok"), 42)
    await cog.data.verify_claim(claim.character_key, 99)
    claim = await cog.data.get_claim(claim.character_key)
    await cog.save_known_recipes(claim, "alchemy", ["spell.2335"])
    activity = wow_cog_mod.ActivityDiff(
        new_members=[],
        milestones=[],
        deaths=[],
        officer_notes=[],
        recipe_events=await cog.data.pending_recipe_learning_events(),
    )

    msg = await cog.format_activity_digest(activity)

    assert "<@42>" in msg
    assert "Swiftnesstrank" in msg
    # Swiftness Potion: drop, skill 60 → 2 points.
    assert "+2 Champion-Punkte" in msg
    assert "ab Skill 60" in msg


@pytest.mark.asyncio
async def test_unclaimed_roster_members_excludes_claimed(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    a = member(key="id:1", name="Alpha", level=60)
    b = member(key="id:2", name="Beta", level=60)
    c = member(key="id:3", name="Charlie", level=60)
    await cog.data.replace_snapshot([a, b, c])
    await cog.data.create_claim(b, 42)

    unclaimed = await cog.data.unclaimed_roster_members()

    assert [m.name for m in unclaimed] == ["Alpha", "Charlie"]


@pytest.mark.asyncio
async def test_roster_response_unsafe_refuses_huge_drop():
    previous = {f"id:{i}": member(key=f"id:{i}") for i in range(40)}
    assert WoWCog._roster_response_unsafe(previous, []) is True
    assert WoWCog._roster_response_unsafe(previous, [member(key="id:0")] * 5) is True
    assert WoWCog._roster_response_unsafe(previous, [member()] * 30) is False


@pytest.mark.asyncio
async def test_roster_response_unsafe_ignores_small_guilds():
    previous = {"id:1": member()}
    assert WoWCog._roster_response_unsafe(previous, []) is False


@pytest.mark.asyncio
async def test_whois_view_renders_claim_ilvl_and_professions(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    target = member(name="Voidok", level=60, class_id=5, race_id=5)
    await cog.data.replace_snapshot([target])
    claim, _ = await cog.data.create_claim(target, 42)
    await cog.data.set_gear_snapshot(target.character_key, 68.0, 0)
    claim = await cog.data.get_claim(target.character_key)
    await cog.data.set_character_profession(claim, "alchemy", 225, None)

    view = await cog.build_whois_view("Voidok", viewer_id=42)

    assert view is not None
    container = view.children[0]
    texts = [
        c.content for c in container.children if isinstance(c, discord.ui.TextDisplay)
    ]
    # Sections (e.g. Berufe when viewer is owner) wrap a TextDisplay
    for child in container.children:
        if isinstance(child, discord.ui.Section):
            for inner in child.children:
                if isinstance(inner, discord.ui.TextDisplay):
                    texts.append(inner.content)
    blob = "\n".join(texts)
    assert "Voidok" in blob
    assert "Level 60" in blob
    assert "<@42>" in blob
    assert "Lebt" in blob
    assert "68" in blob
    assert "Alchemie" in blob


@pytest.mark.asyncio
async def test_whois_view_returns_none_for_unknown_char(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    assert await cog.build_whois_view("Ghostly", viewer_id=42) is None


def test_pack_sections_keeps_short_digest_in_one_chunk():
    sections = [
        (None, ["🪷 Tagesbericht"]),
        ("**Meilensteine** 🏆", ["- Voidok Level 40"]),
        (None, ["Glückwunsch."]),
    ]
    chunks = wow_cog_mod._pack_digest_sections_into_chunks(sections, limit=1900)
    assert len(chunks) == 1
    assert "🪷 Tagesbericht" in chunks[0]
    assert "**Meilensteine** 🏆" in chunks[0]
    assert "Glückwunsch." in chunks[0]


def test_pack_sections_groups_small_sections_into_one_chunk():
    sections = [
        (None, ["Opener"]),
        ("**A**", ["- a1", "- a2"]),
        ("**B**", ["- b1", "- b2"]),
        (None, ["Closer"]),
    ]
    chunks = wow_cog_mod._pack_digest_sections_into_chunks(sections, limit=1900)
    assert len(chunks) == 1
    # Sections are separated by a blank line.
    assert "\n\n**A**\n" in chunks[0]
    assert "\n\n**B**\n" in chunks[0]


def test_pack_sections_starts_new_chunk_when_full():
    big_body = [f"- line {i:03d} {'x' * 40}" for i in range(20)]
    sections = [
        ("**Section A** 🏆", big_body),
        ("**Section B** 📖", big_body),
    ]
    chunks = wow_cog_mod._pack_digest_sections_into_chunks(sections, limit=400)
    # Two separate chunks, each starting with its own header.
    assert len(chunks) >= 2
    assert chunks[0].startswith("**Section A** 🏆")
    # No chunk contains both headers — sections aren't sliced together
    # halfway through.
    for chunk in chunks:
        assert not ("**Section A** 🏆" in chunk and "**Section B** 📖" in chunk)


def test_pack_sections_splits_oversized_section_with_forts_marker():
    body = [f"- line {i:03d} {'x' * 30}" for i in range(40)]
    sections = [("**Meilensteine** 🏆", body)]
    chunks = wow_cog_mod._pack_digest_sections_into_chunks(sections, limit=300)
    assert len(chunks) >= 2
    # First chunk has the original header.
    assert chunks[0].startswith("**Meilensteine** 🏆\n")
    # Continuation chunks repeat the header with the Forts. suffix.
    for chunk in chunks[1:]:
        assert chunk.startswith("**Meilensteine** 🏆 *(Forts.)*\n")
    # All body lines are accounted for and no chunk exceeds the limit.
    rejoined = "\n".join(chunk for chunk in chunks)
    for line in body:
        assert line in rejoined
    assert all(len(chunk) <= 300 for chunk in chunks)


@pytest.mark.asyncio
async def test_digest_post_chunks_long_digest_across_sections(
    tmp_path, patch_logged_task, monkeypatch
):
    channel = DummyChannel()
    cog = await create_cog(tmp_path, patch_logged_task, channel=channel)
    await cog.set_announcement_channel(123)

    async def synthetic_sections(_activity):
        body_a = [f"- entry A{i:03d} {'x' * 60}" for i in range(40)]
        body_b = [f"- entry B{i:03d} {'x' * 60}" for i in range(40)]
        return [
            (None, ["🪷 Tagesbericht"]),
            ("**Section A** 🏆", body_a),
            ("**Section B** 📖", body_b),
            (None, ["Glückwunsch."]),
        ]

    monkeypatch.setattr(cog, "_digest_sections", synthetic_sections)

    activity = wow_cog_mod.ActivityDiff(
        new_members=[member()], milestones=[], deaths=[], officer_notes=[]
    )
    posted = await cog._post_activity_digest(activity)

    assert posted >= 2
    sent_messages = [msg for msg, _ in channel.sent]
    assert all(len(msg) <= 2000 for msg in sent_messages)
    # No section header appears split mid-line.
    for msg in sent_messages:
        for header in ("**Section A** 🏆", "**Section B** 📖"):
            if header in msg:
                # Header must appear at the start of a line (not glued to
                # the previous line's content).
                idx = msg.find(header)
                assert idx == 0 or msg[idx - 1] == "\n"


@pytest.mark.asyncio
async def test_officer_note_does_not_refire_after_persist_failure(
    tmp_path, patch_logged_task, monkeypatch
):
    """Reproduces the bug seen in prod: digest fails → persist=False → snapshot
    stays old → officer note for departed member re-fires on next scan."""
    officer = DummyChannel()
    bot = MultiChannelBot(public_channel=ForbiddenChannel(), officer_channel=officer)
    patch_logged_task(wow_cog_mod, log_setup)
    cog = WoWCog(bot)
    cog.data = WoWData(str(tmp_path / "wow.db"))
    CREATED_COGS.append(cog)
    await cog.set_announcement_channel(123)
    # Seed: 6 members so the empty-roster guard doesn't trip; then 5 of them
    # leave so we get exactly one officer note (Frostj) plus a milestone for
    # Voidok that forces public_count > 0 (which triggers persist=False on
    # failed post).
    members = [member(key=f"id:{i}", name=f"M{i}", level=10) for i in range(5)]
    members.append(member(key="id:frostj", name="Frostj", level=10))
    members.append(member(key="id:voidok", name="Voidok", level=39))
    await cog.data.replace_snapshot(members)

    async def fake_roster(session=None):
        # Frostj is gone, Voidok hit 40, the rest stay.
        return [
            *members[:5],
            member(key="id:voidok", name="Voidok", level=40),
        ]

    async def fake_profile(realm, name, **kwargs):
        return {"is_ghost": False}

    monkeypatch.setattr(wow_cog_mod, "fetch_character_profile", fake_profile)
    cog.fetch_roster = fake_roster

    await cog.scan(post=True, persist=True)
    await cog.scan(post=True, persist=True)

    # Only one Frostj note total despite two scans where the public digest
    # failed and snapshot was not replaced.
    frostj_notes = [
        msg for msg, _ in officer.sent if "Frostj" in msg and "nicht mehr Teil" in msg
    ]
    assert len(frostj_notes) == 1


def test_recipe_reward_trainer_returns_zero():
    cog = WoWCog.__new__(WoWCog)
    assert cog.recipe_learning_reward(
        {"learned_from": "trainer", "required_skill": 250}
    ) == ("common", 0)


def test_recipe_reward_vendor_only_returns_zero():
    cog = WoWCog.__new__(WoWCog)
    assert cog.recipe_learning_reward(
        {
            "learned_from": "recipe",
            "required_skill": 200,
            "recipe_item_sources": ["vendor"],
        }
    ) == ("common", 0)


def test_recipe_reward_scales_with_skill_bracket():
    cog = WoWCog.__new__(WoWCog)
    # drop base = 2; brackets: skill 50 → ×1, 150 → ×1.5, 250 → ×2, 290 → ×3
    cases = [
        (50, 2),
        (150, 3),
        (250, 4),
        (290, 6),
    ]
    for skill, expected in cases:
        rarity, points = cog.recipe_learning_reward(
            {
                "learned_from": "recipe",
                "required_skill": skill,
                "recipe_item_sources": ["drop"],
            }
        )
        assert (rarity, points) == ("rare", expected), f"skill={skill}"


def test_recipe_reward_world_drop_beats_regular_drop():
    cog = WoWCog.__new__(WoWCog)
    # world_drop base = 4, drop base = 2; the best source wins.
    _, points = cog.recipe_learning_reward(
        {
            "learned_from": "recipe",
            "required_skill": 200,
            "recipe_item_sources": ["drop", "world_drop"],
        }
    )
    # 4 × 1.5 = 6
    assert points == 6


def test_recipe_reward_epic_spell_id_scales_with_skill():
    cog = WoWCog.__new__(WoWCog)
    epic_spell = next(iter(wow_cog_mod.EPIC_RECIPE_SPELL_IDS))
    rarity, points = cog.recipe_learning_reward(
        {
            "spell_id": epic_spell,
            "learned_from": "recipe",
            "required_skill": 290,
            "recipe_item_sources": ["drop"],
        }
    )
    # Epic base 10 × bracket 3.0 = 30
    assert rarity == "epic"
    assert points == 30


@pytest.mark.asyncio
async def test_new_member_digest_mentions_claimed_owner(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    newcomer = member(key="id:99", name="Lyra", level=12)
    await cog.data.replace_snapshot([newcomer])
    await cog.data.create_claim(newcomer, 777)
    activity = wow_cog_mod.ActivityDiff(
        new_members=[newcomer], milestones=[], deaths=[], officer_notes=[]
    )
    msg = await cog.format_activity_digest(activity)
    assert "<@777>" in msg


@pytest.mark.asyncio
async def test_death_digest_mentions_claimed_owner(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    fallen = member(key="id:42", name="Gorokhan", level=60)
    await cog.data.replace_snapshot([fallen])
    await cog.data.create_claim(fallen, 1234)
    activity = wow_cog_mod.ActivityDiff(
        new_members=[],
        milestones=[],
        deaths=[wow_cog_mod.DeathEvent(fallen, confirmed=True)],
        officer_notes=[],
    )
    msg = await cog.format_activity_digest(activity)
    assert "Gorokhan" in msg
    assert "<@1234>" in msg


@pytest.mark.asyncio
async def test_gear_event_omits_points_when_not_claimed(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    target = member(key="id:1", name="Naked60", level=60)
    await cog.data.replace_snapshot([target])
    # Pretend a gear milestone fired for an unclaimed char (discord_user_id None).
    activity = wow_cog_mod.ActivityDiff(
        new_members=[],
        milestones=[],
        deaths=[],
        officer_notes=[],
        gear_events=[
            wow_cog_mod.GearMilestoneEvent(
                character_key=target.character_key,
                character_name=target.name,
                realm_slug=target.realm_slug,
                discord_user_id=None,
                average_item_level=65.0,
                threshold=65,
                created_at="2026-01-01T00:00:00",
                points=8,
            )
        ],
    )
    msg = await cog.format_activity_digest(activity)
    assert "Naked60" in msg
    assert "Champion-Punkte" not in msg
    assert "<@" not in msg


@pytest.mark.asyncio
async def test_gear_event_mentions_claimed_owner_and_shows_points(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)
    target = member(key="id:1", name="Owned60", level=60)
    await cog.data.replace_snapshot([target])
    activity = wow_cog_mod.ActivityDiff(
        new_members=[],
        milestones=[],
        deaths=[],
        officer_notes=[],
        gear_events=[
            wow_cog_mod.GearMilestoneEvent(
                character_key=target.character_key,
                character_name=target.name,
                realm_slug=target.realm_slug,
                discord_user_id=555,
                average_item_level=65.0,
                threshold=65,
                created_at="2026-01-01T00:00:00",
                points=8,
            )
        ],
    )
    msg = await cog.format_activity_digest(activity)
    assert "<@555>" in msg
    assert "+8 Champion-Punkte" in msg


# ---- Profession skill milestones ----


@pytest.mark.asyncio
async def test_skill_milestone_awarded_on_single_threshold_cross(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    target = member(name="Voidok")
    claim, _ = await cog.data.create_claim(target, 42)

    await cog._record_skill_milestones(claim.character_key, "alchemy", 50, 80)

    events = await cog.data.pending_skill_milestone_events()
    assert [(e.threshold, e.points) for e in events] == [(75, 3)]
    assert events[0].profession_id == "alchemy"
    assert events[0].skill_level == 80


@pytest.mark.asyncio
async def test_skill_milestone_cumulative_on_big_jump(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    target = member(name="Voidok")
    claim, _ = await cog.data.create_claim(target, 42)

    await cog._record_skill_milestones(claim.character_key, "alchemy", 0, 300)

    events = await cog.data.pending_skill_milestone_events()
    crossed = sorted((e.threshold, e.points) for e in events)
    assert crossed == [(75, 3), (150, 6), (225, 12), (300, 25)]


@pytest.mark.asyncio
async def test_skill_milestone_idempotent_on_resave(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    target = member(name="Voidok")
    claim, _ = await cog.data.create_claim(target, 42)

    await cog._record_skill_milestones(claim.character_key, "alchemy", 0, 80)
    await cog._record_skill_milestones(claim.character_key, "alchemy", 0, 80)

    events = await cog.data.pending_skill_milestone_events()
    assert len(events) == 1


@pytest.mark.asyncio
async def test_skill_milestone_digest_section_renders(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    target = member(name="Voidok")
    claim, _ = await cog.data.create_claim(target, 42)
    await cog._record_skill_milestones(claim.character_key, "alchemy", 0, 160)

    activity = wow_cog_mod.ActivityDiff(
        new_members=[],
        milestones=[],
        deaths=[],
        officer_notes=[],
        skill_events=await cog.data.pending_skill_milestone_events(),
    )

    msg = await cog.format_activity_digest(activity)
    assert "Berufsskill-Meilensteine" in msg
    assert "Alchemie" in msg
    # Only the highest threshold is shown (collapsed), with points summed.
    assert "Skill **150**" in msg
    assert "Skill **75**" not in msg
    assert "<@42>" in msg
    assert "+9 Champion-Punkte" in msg


@pytest.mark.asyncio
async def test_skill_milestone_digest_collapses_per_profession(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    target = member(name="Laenalia")
    claim, _ = await cog.data.create_claim(target, 42)
    # Two professions, each crossing all four thresholds (75/150/225/300).
    await cog._record_skill_milestones(claim.character_key, "alchemy", 0, 300)
    await cog._record_skill_milestones(claim.character_key, "cooking", 0, 300)

    activity = wow_cog_mod.ActivityDiff(
        new_members=[],
        milestones=[],
        deaths=[],
        officer_notes=[],
        skill_events=await cog.data.pending_skill_milestone_events(),
    )

    msg = await cog.format_activity_digest(activity)
    # Exactly one collapsed line per profession, only the top threshold.
    assert msg.count("hat Skill **300** erreicht") == 2
    assert "Skill **75**" not in msg
    assert "Skill **150**" not in msg
    assert "Skill **225**" not in msg
    # 3 + 6 + 12 + 25 = 46 points summed per profession.
    assert msg.count("+46 Champion-Punkte") == 2


@pytest.mark.asyncio
async def test_skill_milestone_awards_via_champion_cog(tmp_path, patch_logged_task):
    champion = DummyChampionCog()
    bot = ChampionBot(channel=DummyChannel(), champion=champion)
    patch_logged_task(wow_cog_mod, log_setup)
    cog = WoWCog(bot)
    cog.data = WoWData(str(tmp_path / "wow.db"))
    CREATED_COGS.append(cog)
    cog.bot.data = {"wow": crafting_data()}
    target = member(name="Voidok")
    claim, _ = await cog.data.create_claim(target, 42)
    await cog._record_skill_milestones(claim.character_key, "alchemy", 200, 230)

    activity = wow_cog_mod.ActivityDiff(
        new_members=[],
        milestones=[],
        deaths=[],
        officer_notes=[],
        skill_events=await cog.data.pending_skill_milestone_events(),
    )
    await cog._record_public_events(activity)

    assert champion.updates == [
        (42, 12, "WoW-Berufsskill: Voidok (Alchemie) Skill 225"),
    ]


# ---- Cooldown tracking ----


@pytest.mark.asyncio
async def test_cooldown_log_propagates_to_group(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    target = member(name="Voidok")
    claim, _ = await cog.data.create_claim(target, 42)
    # User must have learned a transmute spell for the log to succeed.
    await cog.data.add_known_recipes(claim.character_key, "alchemy", ["spell.17187"])

    status, cooldown = await cog.log_cooldown(42, claim.character_key, "spell.17187")
    assert status == "ok"
    assert cooldown is not None
    assert cooldown.cooldown_group == "alchemy_transmute"

    # Logging Iron-to-Gold (same group) overwrites the existing row.
    await cog.data.add_known_recipes(claim.character_key, "alchemy", ["spell.11479"])
    status2, _ = await cog.log_cooldown(42, claim.character_key, "spell.11479")
    assert status2 == "ok"
    rows = await cog.data.cooldowns_for_character(claim.character_key)
    # Still one row — group is shared.
    assert len(rows) == 1
    assert rows[0].last_spell_id == "spell.11479"


@pytest.mark.asyncio
async def test_cooldown_log_requires_known_recipe(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    target = member(name="Voidok")
    claim, _ = await cog.data.create_claim(target, 42)
    # No recipe learned — must fail.

    status, cooldown = await cog.log_cooldown(42, claim.character_key, "spell.17187")
    assert status == "recipe_missing"
    assert cooldown is None
    assert await cog.data.cooldowns_for_character(claim.character_key) == []


@pytest.mark.asyncio
async def test_cooldown_ready_window_filter(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    target = member(name="Voidok")
    claim, _ = await cog.data.create_claim(target, 42)
    now = datetime(2026, 5, 14, 12, 0, 0, tzinfo=__import__("datetime").timezone.utc)
    in_12h = now + __import__("datetime").timedelta(hours=12)
    in_30h = now + __import__("datetime").timedelta(hours=30)
    await cog.data.set_cooldown(
        claim.character_key,
        "alchemy_transmute",
        "spell.17187",
        "Transmute: Arcanite",
        now.isoformat(),
        in_12h.isoformat(),
    )
    other = member(key="id:2", name="Other")
    other_claim, _ = await cog.data.create_claim(other, 99)
    await cog.data.set_cooldown(
        other_claim.character_key,
        "tailoring_mooncloth",
        "spell.18560",
        "Mooncloth",
        now.isoformat(),
        in_30h.isoformat(),
    )

    in_window = await cog.data.cooldowns_ready_in_window(
        now.isoformat(),
        (now + __import__("datetime").timedelta(hours=24)).isoformat(),
    )
    assert [cd.character_name for cd in in_window] == ["Voidok"]


@pytest.mark.asyncio
async def test_cooldown_digest_section_renders_with_mention(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)
    target = member(name="Voidok")
    claim, _ = await cog.data.create_claim(target, 42)
    in_two_hours = datetime.now(__import__("datetime").timezone.utc) + __import__(
        "datetime"
    ).timedelta(hours=2)
    cd = wow_cog_mod.Cooldown(
        character_key=claim.character_key,
        character_name="Voidok",
        realm_slug="soulseeker",
        discord_user_id=42,
        cooldown_group="alchemy_transmute",
        last_spell_id="spell.17187",
        last_spell_name="Transmute: Arcanite",
        used_at=datetime.now(__import__("datetime").timezone.utc).isoformat(),
        ready_at=in_two_hours.isoformat(),
    )
    activity = wow_cog_mod.ActivityDiff(
        new_members=[],
        milestones=[],
        deaths=[],
        officer_notes=[],
        cooldowns_ready=[cd],
    )

    msg = await cog.format_activity_digest(activity)
    assert "Cooldowns bereit in den nächsten 24h" in msg
    assert "Voidok" in msg
    assert "Transmutationen" in msg
    assert "Transmute: Arcanite" in msg
    assert "<@42>" in msg


@pytest.mark.asyncio
async def test_cooldown_eligible_options_filters_by_known_recipes(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    target = member(name="Voidok")
    claim, _ = await cog.data.create_claim(target, 42)
    other_char = member(key="id:2", name="Otherchar")
    other_claim, _ = await cog.data.create_claim(other_char, 42)

    # Voidok learns Arcanite; Otherchar learns nothing.
    await cog.data.add_known_recipes(claim.character_key, "alchemy", ["spell.17187"])

    eligible = await cog.cooldown_eligible_options(42)

    assert [(c.character_name, sid) for c, sid, _, _ in eligible] == [
        ("Voidok", "spell.17187")
    ]


@pytest.mark.asyncio
async def test_salt_shaker_appears_as_cooldown_when_marked_learned(
    tmp_path, patch_logged_task
):
    """Salt Shaker (spell.19566) is registered in COOLDOWN_GROUPS and shows
    up in cooldown_eligible_options once a Leatherworking char has marked
    the recipe as learned."""
    cog = await create_cog(tmp_path, patch_logged_task)
    # Use live wow data (which now contains the Salt Shaker recipe).
    from lotus_bot.bot import load_wow_data

    cog.bot.data = {"wow": load_wow_data("data/wow/classic_hc")}
    target = member(name="Voidok")
    claim, _ = await cog.data.create_claim(target, 42)
    await cog.data.add_known_recipes(
        claim.character_key, "leatherworking", ["spell.19566"]
    )

    eligible = await cog.cooldown_eligible_options(42)
    salt_shaker_entries = [
        (c.character_name, sid, label)
        for c, sid, _name, label in eligible
        if sid == "spell.19566"
    ]
    assert salt_shaker_entries == [("Voidok", "spell.19566", "Salt Shaker")]


# ---- Award retry on ChampionCog failure ----


class FlakyChampionCog:
    """ChampionCog stub that fails the next ``fail_next`` calls.

    Used to simulate a transient DB-lock / network hiccup so we can verify
    the cog's retry path picks the event up on the following scan.
    """

    def __init__(self, fail_next: int = 0) -> None:
        self.updates: list[tuple[int, int, str]] = []
        self.fail_next = fail_next

    async def update_user_score(self, user_id, delta, reason):
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("Simulated transient ChampionCog failure")
        self.updates.append((user_id, delta, reason))
        return delta


async def _recipe_retry_setup(tmp_path, patch_logged_task, fail_next):
    champion = FlakyChampionCog(fail_next=fail_next)
    bot = ChampionBot(channel=DummyChannel(), champion=champion)
    patch_logged_task(wow_cog_mod, log_setup)
    cog = WoWCog(bot)
    cog.data = WoWData(str(tmp_path / "wow.db"))
    CREATED_COGS.append(cog)
    cog.bot.data = {"wow": crafting_data()}
    claim, _ = await cog.data.create_claim(member(name="Voidok"), 42)
    await cog.data.verify_claim(claim.character_key, 99)
    claim = await cog.data.get_claim(claim.character_key)
    await cog.save_known_recipes(claim, "alchemy", ["spell.2335"])
    return cog, champion


@pytest.mark.asyncio
async def test_recipe_award_retry_after_failed_champion_call(
    tmp_path, patch_logged_task
):
    """ChampionCog fails once → row stays in retry pool → next scan funds it."""
    # fail_next=2: normal call + same-scan retry both throw → next scan recovers.
    cog, champion = await _recipe_retry_setup(tmp_path, patch_logged_task, fail_next=2)

    activity = wow_cog_mod.ActivityDiff(
        new_members=[],
        milestones=[],
        deaths=[],
        officer_notes=[],
        recipe_events=await cog.data.pending_recipe_learning_events(),
    )
    await cog._record_public_events(activity)

    # First pass: champion threw, retry loop with the still-broken champion
    # also threw, awarded_at was rolled back both times. Row stays queued.
    assert champion.updates == []
    retries = await cog.data.pending_award_retries_recipe_learning()
    assert len(retries) == 1

    # Second pass with a healthy champion drains the pool.
    await cog._retry_unawarded_pending_events()
    assert champion.updates == [(42, 2, "WoW-Rezept: Voidok lernt Swiftnesstrank")]
    assert await cog.data.pending_award_retries_recipe_learning() == []


@pytest.mark.asyncio
async def test_recipe_award_does_not_double_fire(tmp_path, patch_logged_task):
    """Repeat _record_public_events with the same activity → exactly one award."""
    cog, champion = await _recipe_retry_setup(tmp_path, patch_logged_task, fail_next=0)

    activity = wow_cog_mod.ActivityDiff(
        new_members=[],
        milestones=[],
        deaths=[],
        officer_notes=[],
        recipe_events=await cog.data.pending_recipe_learning_events(),
    )
    await cog._record_public_events(activity)
    await cog._record_public_events(activity)

    assert len(champion.updates) == 1
    assert await cog.data.pending_award_retries_recipe_learning() == []


@pytest.mark.asyncio
async def test_gear_award_retry_after_failed_champion_call(tmp_path, patch_logged_task):
    # fail_next=2: the normal-flow call AND the same-scan retry both fail.
    # The "second scan" (separate _retry_unawarded_pending_events call) then
    # succeeds with a fresh fail_next=0.
    champion = FlakyChampionCog(fail_next=2)
    bot = ChampionBot(channel=DummyChannel(), champion=champion)
    patch_logged_task(wow_cog_mod, log_setup)
    cog = WoWCog(bot)
    cog.data = WoWData(str(tmp_path / "wow.db"))
    CREATED_COGS.append(cog)
    target = member(name="Voidok", level=60)
    await cog.data.replace_snapshot([target])
    claim, _ = await cog.data.create_claim(target, 42)
    await cog.data.verify_claim(claim.character_key, 99)
    await cog.data.record_gear_milestone(claim.character_key, 65, 65.0, 8)

    activity = wow_cog_mod.ActivityDiff(
        new_members=[],
        milestones=[],
        deaths=[],
        officer_notes=[],
        gear_events=await cog.data.pending_gear_milestone_events(),
    )
    await cog._record_public_events(activity)

    assert champion.updates == []
    assert len(await cog.data.pending_award_retries_gear_milestone()) == 1

    await cog._retry_unawarded_pending_events()
    assert len(champion.updates) == 1
    assert await cog.data.pending_award_retries_gear_milestone() == []


@pytest.mark.asyncio
async def test_gear_award_success_clears_retry_pool(tmp_path, patch_logged_task):
    champion = FlakyChampionCog(fail_next=0)
    bot = ChampionBot(channel=DummyChannel(), champion=champion)
    patch_logged_task(wow_cog_mod, log_setup)
    cog = WoWCog(bot)
    cog.data = WoWData(str(tmp_path / "wow.db"))
    CREATED_COGS.append(cog)
    target = member(name="Voidok", level=60)
    await cog.data.replace_snapshot([target])
    claim, _ = await cog.data.create_claim(target, 42)
    await cog.data.verify_claim(claim.character_key, 99)
    await cog.data.record_gear_milestone(claim.character_key, 65, 65.0, 8)

    activity = wow_cog_mod.ActivityDiff(
        new_members=[],
        milestones=[],
        deaths=[],
        officer_notes=[],
        gear_events=await cog.data.pending_gear_milestone_events(),
    )
    await cog._record_public_events(activity)

    assert len(champion.updates) == 1
    assert await cog.data.pending_award_retries_gear_milestone() == []


@pytest.mark.asyncio
async def test_skill_award_retry_after_failed_champion_call(
    tmp_path, patch_logged_task
):
    # fail_next=2: the normal-flow call AND the same-scan retry both fail.
    # The "second scan" (separate _retry_unawarded_pending_events call) then
    # succeeds with a fresh fail_next=0.
    champion = FlakyChampionCog(fail_next=2)
    bot = ChampionBot(channel=DummyChannel(), champion=champion)
    patch_logged_task(wow_cog_mod, log_setup)
    cog = WoWCog(bot)
    cog.data = WoWData(str(tmp_path / "wow.db"))
    CREATED_COGS.append(cog)
    cog.bot.data = {"wow": crafting_data()}
    target = member(name="Voidok")
    claim, _ = await cog.data.create_claim(target, 42)
    await cog._record_skill_milestones(claim.character_key, "alchemy", 0, 80)

    activity = wow_cog_mod.ActivityDiff(
        new_members=[],
        milestones=[],
        deaths=[],
        officer_notes=[],
        skill_events=await cog.data.pending_skill_milestone_events(),
    )
    await cog._record_public_events(activity)

    assert champion.updates == []
    assert len(await cog.data.pending_award_retries_skill_milestone()) == 1

    await cog._retry_unawarded_pending_events()
    assert len(champion.updates) == 1
    assert await cog.data.pending_award_retries_skill_milestone() == []


@pytest.mark.asyncio
async def test_skill_award_success_clears_retry_pool(tmp_path, patch_logged_task):
    champion = FlakyChampionCog(fail_next=0)
    bot = ChampionBot(channel=DummyChannel(), champion=champion)
    patch_logged_task(wow_cog_mod, log_setup)
    cog = WoWCog(bot)
    cog.data = WoWData(str(tmp_path / "wow.db"))
    CREATED_COGS.append(cog)
    cog.bot.data = {"wow": crafting_data()}
    target = member(name="Voidok")
    claim, _ = await cog.data.create_claim(target, 42)
    await cog._record_skill_milestones(claim.character_key, "alchemy", 0, 80)

    activity = wow_cog_mod.ActivityDiff(
        new_members=[],
        milestones=[],
        deaths=[],
        officer_notes=[],
        skill_events=await cog.data.pending_skill_milestone_events(),
    )
    await cog._record_public_events(activity)

    assert len(champion.updates) == 1
    assert await cog.data.pending_award_retries_skill_milestone() == []


# ---- Death + reroll with same name ----


@pytest.mark.asyncio
async def test_death_auto_releases_claim(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task, channel=DummyChannel())
    target = member(key="id:1", name="Zêah", level=60)
    await cog.data.replace_snapshot([target])
    claim, _ = await cog.data.create_claim(target, 42)
    assert await cog.data.get_claim(target.character_key) is not None

    activity = wow_cog_mod.ActivityDiff(
        new_members=[],
        milestones=[],
        deaths=[wow_cog_mod.DeathEvent(member=target, confirmed=True)],
        officer_notes=[],
    )
    await cog._record_public_events(activity)

    # Claim is gone after the death is recorded.
    assert await cog.data.get_claim(target.character_key) is None
    # User's "my chars" list no longer references the dead char.
    assert await cog.data.claims_for_user(42) == []


@pytest.mark.asyncio
async def test_death_without_claim_does_not_crash(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task, channel=DummyChannel())
    target = member(key="id:5", name="Solo", level=20)
    activity = wow_cog_mod.ActivityDiff(
        new_members=[],
        milestones=[],
        deaths=[wow_cog_mod.DeathEvent(member=target, confirmed=False)],
        officer_notes=[],
    )
    # Should not raise even though no claim exists for the dead char.
    await cog._record_public_events(activity)
    assert await cog.data.death_exists(target.character_key)


@pytest.mark.asyncio
async def test_find_roster_member_by_name_prefers_alive(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    old_zeah = member(key="id:1", name="Zêah", level=60, is_ghost=True)
    old_zeah.character_id = 1
    new_zeah = member(key="id:2", name="Zêah", level=8, is_ghost=False)
    new_zeah.character_id = 2
    await cog.data.replace_snapshot([old_zeah, new_zeah])

    found = await cog.data.find_roster_member_by_name("Zêah")

    assert found is not None
    assert found.character_key == "id:2"
    assert found.is_ghost is False


@pytest.mark.asyncio
async def test_get_claim_by_name_prefers_alive(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    old_zeah = member(key="id:1", name="Zêah", level=60, is_ghost=True)
    new_zeah = member(key="id:2", name="Zêah", level=8, is_ghost=False)
    await cog.data.replace_snapshot([old_zeah, new_zeah])
    await cog.data.create_claim(old_zeah, 42)
    await cog.data.create_claim(new_zeah, 42)

    found = await cog.data.get_claim_by_name("Zêah")

    assert found is not None
    assert found.character_key == "id:2"


@pytest.mark.asyncio
async def test_find_crafters_excludes_dead_and_ghost(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    alive = member(key="id:1", name="Alive", level=60, is_ghost=False)
    ghost = member(key="id:2", name="Ghosting", level=60, is_ghost=True)
    dead = member(key="id:3", name="Buried", level=60, is_ghost=False)
    await cog.data.replace_snapshot([alive, ghost, dead])
    for char in (alive, ghost, dead):
        claim, _ = await cog.data.create_claim(char, 42)
        await cog.data.set_character_profession(claim, "tailoring", 290)
    await cog.data.record_death("id:3")

    crafters = await cog.data.find_crafters("tailoring", 290)

    assert [c.character_name for c in crafters] == ["Alive"]


@pytest.mark.asyncio
async def test_find_crafters_with_known_recipe_excludes_dead(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)
    alive = member(key="id:1", name="Alive", level=60)
    dead = member(key="id:3", name="Buried", level=60)
    await cog.data.replace_snapshot([alive, dead])
    for char in (alive, dead):
        claim, _ = await cog.data.create_claim(char, 42)
        await cog.data.set_character_profession(claim, "tailoring", 290)
        await cog.data.add_known_recipes(
            char.character_key, "tailoring", ["spell.18560"]
        )
    await cog.data.record_death("id:3")

    crafters = await cog.data.find_crafters_with_known_recipe(
        "tailoring", 290, "spell.18560"
    )

    assert [c.character_name for c in crafters] == ["Alive"]


@pytest.mark.asyncio
async def test_reroll_after_death_can_claim_same_name(tmp_path, patch_logged_task):
    """End-to-end: char dies → claim released → reroll appears → new claim works."""
    cog = await create_cog(tmp_path, patch_logged_task, channel=DummyChannel())
    old_zeah = member(key="id:1", name="Zêah", level=60)
    await cog.data.replace_snapshot([old_zeah])
    await cog.data.create_claim(old_zeah, 42)

    # Death event records the death and auto-releases the claim.
    await cog._record_public_events(
        wow_cog_mod.ActivityDiff(
            new_members=[],
            milestones=[],
            deaths=[wow_cog_mod.DeathEvent(member=old_zeah, confirmed=True)],
            officer_notes=[],
        )
    )
    assert await cog.data.claims_for_user(42) == []

    # New Zêah is added to the roster (alongside the still-ghost old one).
    new_zeah = member(key="id:2", name="Zêah", level=8, is_ghost=False)
    old_zeah.is_ghost = True
    await cog.data.replace_snapshot([old_zeah, new_zeah])

    # User claims again — find_roster_member_by_name returns the alive one,
    # create_claim creates a fresh claim against the new character_key.
    result = await cog.claim_character(42, "Zêah")
    assert result.created is True
    assert result.claim is not None
    assert result.claim.character_key == "id:2"


# ---- PanelCraftingSearchModal: never pass view=None to send_message ----


@pytest.mark.asyncio
async def test_panel_crafting_search_modal_handles_unambiguous_match(
    tmp_path, patch_logged_task
):
    """An unambiguous match now carries the "Marktplatz-Anfrage erstellen"
    bridge button (not plain informational text anymore)."""
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    voidok = member(name="Voidok")
    await cog.data.replace_snapshot([voidok])
    claim, _ = await cog.data.create_claim(voidok, 42)
    await cog.data.set_character_profession(claim, "alchemy", 75)

    modal = wow_cog_mod.PanelCraftingSearchModal(cog)
    # Unambiguous: "Swiftnesstrank" matches exactly item.2459.
    modal.item._value = "Swiftnesstrank"
    interaction = DummyReviewInteraction(DummyUser(42))

    await modal.on_submit(interaction)

    assert len(interaction.response.messages) == 1
    msg, kwargs = interaction.response.messages[0]
    assert "Swiftnesstrank" in msg
    assert isinstance(
        kwargs.get("view"), wow_cog_mod.CraftingSearchMarketplaceActionsView
    )
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_panel_crafting_search_modal_omits_view_when_none(
    tmp_path, patch_logged_task
):
    """Regression: send_message(..., view=None) raises AttributeError inside
    discord.py — that's the production crash we hit ~6× the week of 15. May.
    For statuses with no follow-up view (e.g. item not found) we must omit
    ``view`` entirely rather than pass an explicit ``None``.
    """
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}

    modal = wow_cog_mod.PanelCraftingSearchModal(cog)
    modal.item._value = "Voidobsoluteradnonsenseitem"
    interaction = DummyReviewInteraction(DummyUser(42))

    # Would have raised AttributeError before the fix.
    await modal.on_submit(interaction)

    assert len(interaction.response.messages) == 1
    _, kwargs = interaction.response.messages[0]
    assert "view" not in kwargs
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_panel_crafting_search_modal_offers_view_for_ambiguous_match(
    tmp_path, patch_logged_task
):
    """Ambiguous matches still get the suggestion view — fix must not break."""
    cog = await create_cog(tmp_path, patch_logged_task)
    cog.bot.data = {"wow": crafting_data()}
    voidok = member(name="Voidok")
    await cog.data.replace_snapshot([voidok])
    claim, _ = await cog.data.create_claim(voidok, 42)
    await cog.data.set_character_profession(claim, "alchemy", 75)

    modal = wow_cog_mod.PanelCraftingSearchModal(cog)
    # "Schwacher" matches both Schwacher Heiltrank and Schwacher Manatrank.
    modal.item._value = "Schwacher"
    interaction = DummyReviewInteraction(DummyUser(42))

    await modal.on_submit(interaction)

    assert len(interaction.response.messages) == 1
    _, kwargs = interaction.response.messages[0]
    assert isinstance(kwargs.get("view"), wow_cog_mod.CraftingSearchSuggestionView)
    assert kwargs.get("ephemeral") is True


# ---- guild-role reconcile ----


class FakeRole:
    def __init__(self, rid):
        self.id = rid
        self.members = []


class FakeMember:
    def __init__(self, uid, roles=None):
        self.id = uid
        self.display_name = f"User{uid}"
        self.roles = list(roles or [])
        self.added = []
        self.removed = []

    async def add_roles(self, role, reason=None):
        self.roles.append(role)
        self.added.append(role.id)

    async def remove_roles(self, role, reason=None):
        self.roles = [r for r in self.roles if r.id != role.id]
        self.removed.append(role.id)


class FakeGuild:
    def __init__(self, role, members):
        self._role = role
        self._members = {m.id: m for m in members}

    def get_role(self, rid):
        return self._role if self._role and rid == self._role.id else None

    def get_member(self, uid):
        return self._members.get(uid)

    async def fetch_member(self, uid):
        member_obj = self._members.get(uid)
        if member_obj is None:
            response = type("Response", (), {"status": 404, "reason": "Not Found"})()
            raise discord.NotFound(response, {"message": "Unknown Member"})
        return member_obj


async def _verified_claim(cog, key, uid):
    char = member(key=key, name=f"Char{uid}")
    char.character_id = uid
    await cog.data.replace_snapshot([char])
    claim, _ = await cog.data.create_claim(char, uid)
    await cog.data.verify_claim(claim.character_key, reviewer_id=999)
    return char


@pytest.mark.asyncio
async def test_sync_guild_role_grants_and_strips(
    tmp_path, patch_logged_task, monkeypatch
):
    monkeypatch.setattr(wow_cog_mod.discord, "Guild", FakeGuild)
    cog = await create_cog(tmp_path, patch_logged_task)

    # User 111 owns a verified claim but lacks the role; 222 holds the role
    # without any claim (a split-off member to clean up).
    await _verified_claim(cog, "id:111", 111)
    role = FakeRole(wow_cog_mod.GUILD_ROLE_ID)
    entitled = FakeMember(111, roles=[])
    leftover = FakeMember(222, roles=[role])
    role.members = [leftover]
    cog.bot.main_guild = FakeGuild(role, [entitled, leftover])

    result = await cog.sync_guild_role()

    assert result.available is True
    assert result.eligible == 1
    assert result.granted == 1
    assert result.removed == 1
    assert entitled.added == [wow_cog_mod.GUILD_ROLE_ID]
    assert leftover.removed == [wow_cog_mod.GUILD_ROLE_ID]


@pytest.mark.asyncio
async def test_sync_guild_role_no_guild_is_safe(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    # DummyBot has no main_guild → reconcile is a no-op, not a crash.
    result = await cog.sync_guild_role()
    assert result.available is False


@pytest.mark.asyncio
async def test_reconcile_guild_role_for_removes_when_no_claim(
    tmp_path, patch_logged_task, monkeypatch
):
    monkeypatch.setattr(wow_cog_mod.discord, "Guild", FakeGuild)
    cog = await create_cog(tmp_path, patch_logged_task)
    role = FakeRole(wow_cog_mod.GUILD_ROLE_ID)
    holder = FakeMember(333, roles=[role])
    cog.bot.main_guild = FakeGuild(role, [holder])

    # No verified claim for 333 → the role must be stripped.
    await cog.reconcile_guild_role_for(333)

    assert holder.removed == [wow_cog_mod.GUILD_ROLE_ID]


@pytest.mark.asyncio
async def test_reconcile_guild_role_for_grants_when_verified(
    tmp_path, patch_logged_task, monkeypatch
):
    monkeypatch.setattr(wow_cog_mod.discord, "Guild", FakeGuild)
    cog = await create_cog(tmp_path, patch_logged_task)
    await _verified_claim(cog, "id:444", 444)
    role = FakeRole(wow_cog_mod.GUILD_ROLE_ID)
    newcomer = FakeMember(444, roles=[])
    cog.bot.main_guild = FakeGuild(role, [newcomer])

    await cog.reconcile_guild_role_for(444)

    assert newcomer.added == [wow_cog_mod.GUILD_ROLE_ID]


@pytest.mark.asyncio
async def test_sync_guild_role_strips_when_claimed_char_left_guild(
    tmp_path, patch_logged_task, monkeypatch
):
    """The actual reported bug: role lingers after the claimed char leaves."""
    monkeypatch.setattr(wow_cog_mod.discord, "Guild", FakeGuild)
    cog = await create_cog(tmp_path, patch_logged_task)

    left = ranked("id:left", "Leaver", 6)
    still_here = ranked("id:other", "Stillhere", 3)
    await cog.data.create_claim(left, 555)
    await cog.data.verify_claim(left.character_key, 999)
    # Roster no longer contains the claimed char, but is NOT empty.
    await cog.data.replace_snapshot([still_here])

    role = FakeRole(wow_cog_mod.GUILD_ROLE_ID)
    holder = FakeMember(555, roles=[role])
    role.members = [holder]
    cog.bot.main_guild = FakeGuild(role, [holder])

    result = await cog.sync_guild_role()

    assert holder.removed == [wow_cog_mod.GUILD_ROLE_ID]
    assert result.removed == 1
    assert result.eligible == 0


@pytest.mark.asyncio
async def test_sync_guild_role_skips_strip_when_roster_empty(
    tmp_path, patch_logged_task, monkeypatch
):
    """Empty roster snapshot must not trigger a mass role-strip."""
    monkeypatch.setattr(wow_cog_mod.discord, "Guild", FakeGuild)
    cog = await create_cog(tmp_path, patch_logged_task)
    # No replace_snapshot → roster table empty.

    role = FakeRole(wow_cog_mod.GUILD_ROLE_ID)
    holder = FakeMember(777, roles=[role])
    role.members = [holder]
    cog.bot.main_guild = FakeGuild(role, [holder])

    result = await cog.sync_guild_role()

    assert holder.removed == []
    assert result.removed == 0


# ---- Offi-Sync-Report (claim ⇄ in-game rank reconcile) ----


def ranked(key, name, rank, level=60, is_ghost=False):
    return RosterMember(
        character_key=key,
        character_id=int(key.split(":")[-1]) if key.split(":")[-1].isdigit() else 0,
        name=name,
        realm_slug="soulseeker",
        level=level,
        class_id=4,
        race_id=8,
        faction="HORDE",
        guild_rank=rank,
        is_ghost=is_ghost,
    )


async def _verify(cog, char, uid):
    await cog.data.create_claim(char, uid)
    await cog.data.verify_claim(char.character_key, reviewer_id=999)


@pytest.mark.asyncio
async def test_sync_report_flags_all_categories(tmp_path, patch_logged_task):
    officer = DummyChannel()
    bot = MultiChannelBot(public_channel=None, officer_channel=officer)
    patch_logged_task(wow_cog_mod, log_setup)
    cog = WoWCog(bot)
    cog.data = WoWData(str(tmp_path / "wow.db"))
    CREATED_COGS.append(cog)

    main_a = ranked("id:1", "Maina", 4)  # Member, verified (user 100's main)
    main_a2 = ranked("id:2", "Zweitmain", 3)  # Veteran, verified (100 → 2nd Member+)
    initiate_claimed = ranked("id:3", "Neulor", 7)  # verified but still Initiate
    twink = ranked("id:4", "Twinkus", 6)  # verified twink → fine
    pending = ranked("id:5", "Pendlor", 7)  # UNVERIFIED claim → protected from kick
    legacy_member = ranked("id:6", "Altmember", 4)  # Member, no claim
    bank_noclaim = ranked("id:7", "Bankus", 5)  # Bank rank, no claim → Twink+ section
    gbank = ranked("id:8", "Tresor", 5)  # Bank rank, registered gbank → excluded
    kickable = ranked("id:9", "Kickmich", 7)  # Initiate, no claim → kick list
    roster = [
        main_a,
        main_a2,
        initiate_claimed,
        twink,
        pending,
        legacy_member,
        bank_noclaim,
        gbank,
        kickable,
    ]
    await cog.data.replace_snapshot(roster)
    await _verify(cog, main_a, 100)
    await _verify(cog, main_a2, 100)
    await _verify(cog, initiate_claimed, 200)
    await _verify(cog, twink, 200)
    await cog.data.create_claim(pending, 300)  # left unverified → still "claimed"
    await cog.data.add_bank_character(gbank.character_key, gbank.name, 999)

    posted = await cog._post_sync_report(roster)

    assert posted >= 1
    msg = "\n".join(sent[0] for sent in officer.sent)
    assert "Offi-Sync-Report" in msg
    # Claimed-but-Initiate → promote reminder (explicitly NOT a kick candidate).
    assert "Neulor" in msg and "<@200>" in msg
    # Initiates are new/probation and must NEVER be listed for kicking.
    assert "Kickmich" not in msg
    assert "Pendlor" not in msg
    # Twink+ without claim includes Bank rank (Bankus) and Member (Altmember).
    assert "Altmember" in msg
    assert "Bankus" in msg
    # Registered guild-bank char is never flagged.
    assert "Tresor" not in msg
    # >1 Member+ per user.
    assert "<@100>" in msg
    assert "Maina" in msg and "Zweitmain" in msg


@pytest.mark.asyncio
async def test_sync_report_empty_when_everything_matches(tmp_path, patch_logged_task):
    officer = DummyChannel()
    bot = MultiChannelBot(public_channel=None, officer_channel=officer)
    patch_logged_task(wow_cog_mod, log_setup)
    cog = WoWCog(bot)
    cog.data = WoWData(str(tmp_path / "wow.db"))
    CREATED_COGS.append(cog)

    main = ranked("id:1", "Main", 4)  # Member, verified
    twink = ranked("id:2", "Twink", 6)  # Twink, verified (same user, fine)
    bank = ranked("id:3", "Bank", 5)  # Bank rank, registered gbank → excluded
    await cog.data.replace_snapshot([main, twink, bank])
    await _verify(cog, main, 100)
    await _verify(cog, twink, 100)
    await cog.data.add_bank_character(bank.character_key, bank.name, 999)

    assert await cog.sync_report_chunks([main, twink, bank]) == []
    assert await cog._post_sync_report([main, twink, bank]) == 0
    assert officer.sent == []


@pytest.mark.asyncio
async def test_sync_report_flags_twink_bank_without_main(tmp_path, patch_logged_task):
    officer = DummyChannel()
    bot = MultiChannelBot(public_channel=None, officer_channel=officer)
    patch_logged_task(wow_cog_mod, log_setup)
    cog = WoWCog(bot)
    cog.data = WoWData(str(tmp_path / "wow.db"))
    CREATED_COGS.append(cog)

    # User 700: Twink + Bank char, NO Member → flagged, highest-level suggested.
    twink = ranked("id:1", "Twinki", 6, level=58)
    bankc = ranked("id:2", "Lager", 5, level=30)
    # User 800: has a real Member main → must NOT be flagged.
    main = ranked("id:3", "Hauptmann", 4, level=60)
    alt = ranked("id:4", "Nebenchar", 6, level=40)
    await cog.data.replace_snapshot([twink, bankc, main, alt])
    await _verify(cog, twink, 700)
    await _verify(cog, bankc, 700)
    await _verify(cog, main, 800)
    await _verify(cog, alt, 800)

    posted = await cog._post_sync_report([twink, bankc, main, alt])

    assert posted >= 1
    msg = "\n".join(sent[0] for sent in officer.sent)
    assert "Twink/Bank ohne Main" in msg
    assert "<@700>" in msg and "Twinki" in msg and "Lager" in msg
    # Highest level (Twinki 58) listed before the bank char (Lager 30).
    assert msg.index("Twinki") < msg.index("Lager")
    # The user who still has a Member main is not flagged as main-less.
    assert "<@800>" not in msg


@pytest.mark.asyncio
async def test_officer_twink_not_counted_as_second_main(tmp_path, patch_logged_task):
    officer = DummyChannel()
    bot = MultiChannelBot(public_channel=None, officer_channel=officer)
    patch_logged_task(wow_cog_mod, log_setup)
    cog = WoWCog(bot)
    cog.data = WoWData(str(tmp_path / "wow.db"))
    CREATED_COGS.append(cog)

    # User 100: Offizier main + sanctioned Officer-Twink alt → must NOT be flagged.
    off_main = ranked("id:1", "Chefe", 1)  # Offizier
    off_twink = ranked("id:2", "Chefezwo", 2)  # Officer Twink
    # User 200: Offizier + a real Member → two mains → flagged.
    boss = ranked("id:3", "Bossman", 1)  # Offizier
    second = ranked("id:4", "Zweitmain", 4)  # Member
    await cog.data.replace_snapshot([off_main, off_twink, boss, second])
    await _verify(cog, off_main, 100)
    await _verify(cog, off_twink, 100)
    await _verify(cog, boss, 200)
    await _verify(cog, second, 200)

    await cog._post_sync_report([off_main, off_twink, boss, second])

    msg = "\n".join(sent[0] for sent in officer.sent)
    assert "Mehr als ein Main-Char" in msg
    assert "<@200>" in msg  # two real mains → flagged
    assert "<@100>" not in msg  # Officer Twink is the sanctioned exception


@pytest.mark.asyncio
async def test_repost_missing_claim_reviews(tmp_path, patch_logged_task):
    officer = DummyChannel()
    bot = MultiChannelBot(public_channel=None, officer_channel=officer)
    patch_logged_task(wow_cog_mod, log_setup)
    cog = WoWCog(bot)
    cog.data = WoWData(str(tmp_path / "wow.db"))
    CREATED_COGS.append(cog)

    alpha = member(key="id:1", name="Alpha")
    beta = member(key="id:2", name="Beta")
    await cog.data.replace_snapshot([alpha, beta])
    # id:1 = claimed while the bot lacked channel perms → no review card yet.
    await cog.data.create_claim(alpha, 100)
    # id:2 = already has a review card and must NOT be reposted.
    await cog.data.create_claim(beta, 200)
    await cog.data.set_claim_review_message("id:2", 555)

    reposted, failed = await cog.repost_missing_claim_reviews()

    assert (reposted, failed) == (1, 0)
    assert (await cog.data.get_claim("id:1")).review_message_id is not None
    assert (await cog.data.get_claim("id:2")).review_message_id == 555
    assert officer.sent  # a card was actually posted


async def test_repost_reposts_manually_deleted_card(tmp_path, patch_logged_task):
    # id:2 has a stored review_message_id (200) but its Discord message was
    # manually deleted → fetch_message reports it gone → it must be reposted.
    officer = DummyChannel(missing_message_ids={200})
    bot = MultiChannelBot(public_channel=None, officer_channel=officer)
    patch_logged_task(wow_cog_mod, log_setup)
    cog = WoWCog(bot)
    cog.data = WoWData(str(tmp_path / "wow.db"))
    CREATED_COGS.append(cog)

    beta = member(key="id:2", name="Beta")
    await cog.data.replace_snapshot([beta])
    await cog.data.create_claim(beta, 200)
    await cog.data.set_claim_review_message("id:2", 200)

    reposted, failed = await cog.repost_missing_claim_reviews()

    assert (reposted, failed) == (1, 0)
    # A fresh card was posted and the stored id points at the new message.
    assert officer.sent
    assert (await cog.data.get_claim("id:2")).review_message_id == 555


@pytest.mark.asyncio
async def test_prune_departed_claims_removes_absent_users(
    tmp_path, patch_logged_task, monkeypatch
):
    monkeypatch.setattr(wow_cog_mod.discord, "Guild", FakeGuild)
    officer = DummyChannel()
    bot = MultiChannelBot(public_channel=None, officer_channel=officer)
    patch_logged_task(wow_cog_mod, log_setup)
    cog = WoWCog(bot)
    cog.data = WoWData(str(tmp_path / "wow.db"))
    CREATED_COGS.append(cog)

    stays = ranked("id:1", "Bleibtchar", 4)  # owner 100 still on Discord
    left = ranked("id:2", "Wegchar", 4)  # owner 500 left Discord
    await cog.data.replace_snapshot([stays, left])
    await _verify(cog, stays, 100)
    await _verify(cog, left, 500)

    role = FakeRole(wow_cog_mod.GUILD_ROLE_ID)
    cog.bot.main_guild = FakeGuild(role, [FakeMember(100)])  # 500 is gone

    removed = await cog.prune_departed_claims()

    assert removed == 1
    assert await cog.data.claims_for_user(500) == []  # departed → pruned
    assert len(await cog.data.claims_for_user(100)) == 1  # present → kept
    # Officer got a one-time note naming the freed char and its rank.
    msg = "\n".join(sent[0] for sent in officer.sent)
    assert "Discord verlassen" in msg
    assert "Wegchar" in msg and "<@500>" in msg


@pytest.mark.asyncio
async def test_prune_departed_claims_no_guild_is_safe(tmp_path, patch_logged_task):
    cog = await create_cog(tmp_path, patch_logged_task)
    solo = ranked("id:1", "Soloist", 4)
    await cog.data.replace_snapshot([solo])
    await _verify(cog, solo, 100)

    # No main_guild resolvable → nothing pruned, no crash.
    assert await cog.prune_departed_claims() == 0
    assert len(await cog.data.claims_for_user(100)) == 1


@pytest.mark.asyncio
async def test_on_member_remove_drops_claims(tmp_path, patch_logged_task, monkeypatch):
    monkeypatch.setattr(wow_cog_mod.discord, "Guild", FakeGuild)
    officer = DummyChannel()
    bot = MultiChannelBot(public_channel=None, officer_channel=officer)
    patch_logged_task(wow_cog_mod, log_setup)
    cog = WoWCog(bot)
    cog.data = WoWData(str(tmp_path / "wow.db"))
    CREATED_COGS.append(cog)

    char = ranked("id:1", "Verlassen", 4)
    await cog.data.replace_snapshot([char])
    await _verify(cog, char, 500)

    role = FakeRole(wow_cog_mod.GUILD_ROLE_ID)
    cog.bot.main_guild = FakeGuild(role, [])
    cog.bot.main_guild_id = 4242
    leaving = type("M", (), {"id": 500, "guild": type("G", (), {"id": 4242})()})()

    await cog.on_member_remove(leaving)

    assert await cog.data.claims_for_user(500) == []
    assert "Verlassen" in "\n".join(sent[0] for sent in officer.sent)


async def _main_died_cog(tmp_path, patch_logged_task):
    officer = DummyChannel()
    bot = MultiChannelBot(public_channel=None, officer_channel=officer)
    patch_logged_task(wow_cog_mod, log_setup)
    cog = WoWCog(bot)
    cog.data = WoWData(str(tmp_path / "wow.db"))
    CREATED_COGS.append(cog)
    return cog, officer


@pytest.mark.asyncio
async def test_notify_main_died_suggests_promotion(tmp_path, patch_logged_task):
    cog, officer = await _main_died_cog(tmp_path, patch_logged_task)
    twink = ranked("id:2", "Twinki", 6, level=58)  # only a twink survives
    await cog.data.replace_snapshot([twink])
    await _verify(cog, twink, 100)

    dead_main = ranked("id:1", "Deadmain", 4, level=60)
    await cog._notify_main_died(100, dead_main)

    msg = "\n".join(sent[0] for sent in officer.sent)
    assert "Main gefallen" in msg and "<@100>" in msg
    assert "Twinki" in msg and "Vorschlag" in msg


@pytest.mark.asyncio
async def test_notify_main_died_skips_when_another_main_remains(
    tmp_path, patch_logged_task
):
    cog, officer = await _main_died_cog(tmp_path, patch_logged_task)
    second_main = ranked("id:2", "Zweitmain", 4, level=60)  # still a Member
    await cog.data.replace_snapshot([second_main])
    await _verify(cog, second_main, 100)

    await cog._notify_main_died(100, ranked("id:1", "Deadmain", 4))

    assert officer.sent == []


@pytest.mark.asyncio
async def test_notify_main_died_skips_initiate_only_survivors(
    tmp_path, patch_logged_task
):
    cog, officer = await _main_died_cog(tmp_path, patch_logged_task)
    initiate = ranked("id:2", "Neuling", 7, level=20)  # only a probation char
    await cog.data.replace_snapshot([initiate])
    await _verify(cog, initiate, 100)

    await cog._notify_main_died(100, ranked("id:1", "Deadmain", 4))

    assert officer.sent == []


@pytest.mark.asyncio
async def test_claim_review_message_hints_twink_when_user_has_member(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)
    main = ranked("id:1", "Hauptmann", 4)  # existing Member char
    twink = ranked("id:2", "Zwergnase", 7)  # the newly claimed char (Initiate)
    await cog.data.replace_snapshot([main, twink])
    await _verify(cog, main, 100)
    new_claim, _ = await cog.data.create_claim(twink, 100)

    msg = await cog.format_claim_review_message(new_claim)

    assert "Hauptmann" in msg  # existing claim shown
    assert "Member" in msg  # its rank label
    assert "Twink" in msg  # recommendation


@pytest.mark.asyncio
async def test_claim_review_message_recommends_member_for_first_claim(
    tmp_path, patch_logged_task
):
    cog = await create_cog(tmp_path, patch_logged_task)
    first = ranked("id:1", "Erstchar", 7)
    await cog.data.replace_snapshot([first])
    claim, _ = await cog.data.create_claim(first, 100)

    msg = await cog.format_claim_review_message(claim)

    assert "Bereits geclaimt" not in msg
    assert "Member" in msg
