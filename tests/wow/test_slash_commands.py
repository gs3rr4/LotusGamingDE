from lotus_bot.cogs.wow.data import (
    CharacterClaim,
    CharacterKnownRecipe,
    CharacterProfession,
    RosterMember,
)
from lotus_bot.cogs.wow.slash_commands import (
    all_claims_autocomplete,
    claim,
    claim_release,
    claim_remove,
    claim_char_autocomplete,
    claims_list,
    claims_mine,
    crafting_learned,
    crafting_list,
    crafting_mine,
    crafting_recipe_remove,
    crafting_recipes,
    crafting_remove,
    crafting_search,
    crafting_set,
    ghosts,
    recipes_profession_autocomplete,
    roster_char_autocomplete,
    scan,
    setup,
    status,
    user_claim_autocomplete,
)
import pytest

pytestmark = pytest.mark.asyncio


class DummyCog:
    def __init__(self):
        self.channel_id = None
        self.scan_calls = []
        self.claim_results = {}
        self.crafting_set_calls = []
        self.crafting_remove_calls = []
        self.crafting_set_result = None
        self.crafting_remove_result = None
        self.crafting_search_result = None
        self.recipe_selection_result = None
        self.known_recipes_result = None
        self.remove_known_recipe_result = None
        self.panel_publish_calls = []
        self.reconcile_calls = []
        self.data = DummyData()

    async def set_announcement_channel(self, channel_id):
        self.channel_id = channel_id

    async def reconcile_guild_role_for(self, user_id):
        self.reconcile_calls.append(user_id)

    async def status(self):
        return {
            "guild": "Black Lotus",
            "realm": "soulseeker",
            "channel_id": self.channel_id,
            "panel_channel_id": None,
            "panel_message_id": None,
            "last_scan_at": "now",
            "member_count": 12,
            "poll_interval": 10800,
        }

    async def scan(self, *, post=True, persist=True):
        self.scan_calls.append((post, persist))
        return type("Result", (), {"member_count": 12, "milestones": [], "posted": 0})()

    async def claim_character(self, user_id, char):
        return self.claim_results[char]

    async def set_crafting_profile(
        self, user_id, char, profession, skill, specialization, *, is_mod=False
    ):
        self.crafting_set_calls.append(
            (user_id, char, profession, skill, specialization, is_mod)
        )
        return self.crafting_set_result

    async def remove_crafting_profile(self, user_id, char, profession, *, is_mod=False):
        self.crafting_remove_calls.append((user_id, char, profession, is_mod))
        return self.crafting_remove_result

    async def search_crafting(self, item):
        return self.crafting_search_result

    async def prepare_recipe_selection(
        self, user_id, char, profession, search, *, is_mod=False
    ):
        return self.recipe_selection_result

    async def known_recipes_for_character(self, user_id, char, *, is_mod=False):
        return self.known_recipes_result

    async def remove_known_recipe(self, user_id, char, recipe, *, is_mod=False):
        return self.remove_known_recipe_result

    async def publish_panel(self, channel):
        self.panel_publish_calls.append(channel.id)
        return self.panel_publish_result

    def _profession_name(self, profession_id):
        return {"alchemy": "Alchemie"}.get(profession_id, profession_id)

    def _get_static_record(self, table, record_id):
        return {"id": record_id, "type": "primary"} if table == "professions" else {}

    def _is_crafting_profession(self, profession):
        return profession.get("id") not in {"first-aid", "fishing"}

    def _recipe_by_spell_id(self, spell_id):
        return {"spell_id": spell_id, "id": "recipe.test"}

    def _recipe_name(self, recipe, language="de"):
        return {"spell.2335": "Swiftnesstrank"}.get(
            recipe.get("spell_id"), "Testrezept"
        )

    def _recipe_secondary_name(self, recipe, language="de"):
        return ""

    def normalize_recipe_language(self, language):
        return language if language in {"de", "en"} else "de"

    def _spell_for_recipe(self, recipe):
        return {"name": {"de": "Swiftnesstrank herstellen"}}

    def _localized_text(self, value, language="de"):
        if isinstance(value, dict):
            return value.get(language) or value.get("de") or value.get("en") or ""
        return value or ""

    def resolve_profession_id(self, profession):
        if profession in (None, "alchemy", "Alchemie"):
            return "alchemy"
        return None

    def format_profession(self, profile):
        return f"**{profile.character_name}** - Alchemie {profile.skill_level}"

    def format_crafting_search_result(self, result):
        return result.message

    def crafting_search_response_view(self, result, owner_user_id):
        return None

    def _format_roster_line(self, member, level=None):
        lvl = level or member.level
        return f"**{member.name}**, Level **{lvl}**"


def roster_member(name="Lyxendra"):
    return RosterMember(
        character_key="id:1",
        character_id=1,
        name=name,
        realm_slug="soulseeker",
        level=44,
        class_id=4,
        race_id=8,
        faction="HORDE",
        guild_rank=1,
    )


def character_claim(name="Lyxendra", user_id=42, status="unverified", key="id:1"):
    return CharacterClaim(
        character_key=key,
        character_name=name,
        realm_slug="soulseeker",
        discord_user_id=user_id,
        status=status,
        claimed_at="now",
        verified_at=None,
        verified_by=None,
        review_message_id=None,
    )


def character_profession(
    name="Lyxendra", user_id=42, profession_id="alchemy", key="id:1"
):
    return CharacterProfession(
        character_key=key,
        character_name=name,
        realm_slug="soulseeker",
        discord_user_id=user_id,
        profession_id=profession_id,
        skill_level=250,
        specialization="Elixiere",
        updated_at="now",
    )


def known_recipe(name="Lyxendra", user_id=42, spell_id="spell.2335"):
    return CharacterKnownRecipe(
        character_key="id:1",
        character_name=name,
        realm_slug="soulseeker",
        discord_user_id=user_id,
        spell_id=spell_id,
        profession_id="alchemy",
        learned_at="now",
    )


class DummyData:
    def __init__(self):
        self.member = roster_member()
        self.snapshot = {
            "id:1": roster_member("Voidok"),
            "id:2": roster_member("Voljin"),
            "id:3": roster_member("Lyxendra"),
        }
        self.claim = None
        self.claims = []
        self.professions = []
        self.removed = []
        self.ghosts: list = []

    async def get_snapshot(self):
        return self.snapshot

    async def ghost_members(self):
        return list(self.ghosts)

    async def find_roster_member_by_name(self, char):
        if char.lower() == self.member.name.lower():
            return self.member
        return None

    async def release_claim(self, character_key, user_id):
        if self.claim and self.claim.discord_user_id == user_id:
            self.claim = None
            return True
        return False

    async def get_claim(self, character_key):
        return self.claim

    async def get_claim_by_name(self, char):
        for existing_claim in self.claims:
            if char.lower() == existing_claim.character_name.lower():
                return existing_claim
        if self.claim and char.lower() == self.claim.character_name.lower():
            return self.claim
        return None

    async def remove_claim(self, character_key):
        self.removed.append(character_key)
        self.claim = None

    async def claims_for_user(self, user_id):
        if self.claims:
            return [
                existing_claim
                for existing_claim in self.claims
                if existing_claim.discord_user_id == user_id
            ]
        return (
            [self.claim] if self.claim and self.claim.discord_user_id == user_id else []
        )

    async def list_claims(self, status="all"):
        if self.claims:
            return [
                existing_claim
                for existing_claim in self.claims
                if status == "all" or existing_claim.status == status
            ]
        if not self.claim:
            return []
        if status != "all" and self.claim.status != status:
            return []
        return [self.claim]

    async def professions_for_user(self, user_id):
        return [
            profession
            for profession in self.professions
            if profession.discord_user_id == user_id
        ]

    async def list_professions(self, profession_id=None):
        if profession_id:
            return [
                profession
                for profession in self.professions
                if profession.profession_id == profession_id
            ]
        return self.professions

    async def professions_for_character(self, character_key):
        return [
            profession
            for profession in self.professions
            if profession.character_key == character_key
        ]


class DummyBot:
    def __init__(self, cog):
        self.cog = cog

    def get_cog(self, name):
        return self.cog if name == "WoWCog" else None


class DummyResponse:
    def __init__(self):
        self.messages = []
        self.deferred = False

    async def send_message(self, msg, **kwargs):
        self.messages.append((msg, kwargs))

    async def defer(self, **kwargs):
        self.deferred = True


class DummyFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, msg, **kwargs):
        self.messages.append((msg, kwargs))


class DummyInteraction:
    def __init__(self, cog, *, manage_guild=False):
        self.client = DummyBot(cog)
        self.response = DummyResponse()
        self.followup = DummyFollowup()
        permissions = type("Permissions", (), {"manage_guild": manage_guild})()
        self.user = type(
            "User",
            (),
            {
                "id": 42,
                "guild_permissions": permissions,
                "__str__": lambda self: "tester",
            },
        )()
        self.namespace = type("Namespace", (), {})()


class DummyChannel:
    id = 123
    mention = "<#123>"


async def test_setup_command_persists_channel():
    cog = DummyCog()
    inter = DummyInteraction(cog)
    await setup.callback(inter, DummyChannel())

    assert cog.channel_id == 123
    assert inter.response.messages


async def test_status_command_reports_state():
    cog = DummyCog()
    cog.channel_id = 123
    inter = DummyInteraction(cog)
    await status.callback(inter)

    msg, kwargs = inter.response.messages[0]
    assert "Black Lotus" in msg
    assert "<#123>" in msg
    assert kwargs.get("ephemeral")


async def test_scan_command_dry_run_does_not_persist():
    cog = DummyCog()
    inter = DummyInteraction(cog)
    await scan.callback(inter, False)

    assert cog.scan_calls == [(False, False)]
    assert inter.response.deferred
    assert inter.followup.messages


async def test_claim_command_creates_claim():
    cog = DummyCog()
    cog.claim_results["Lyxendra"] = type(
        "Result",
        (),
        {
            "reason": "created",
            "claim": character_claim(),
            "created": True,
            "review_posted": True,
        },
    )()
    inter = DummyInteraction(cog)
    await claim.callback(inter, "Lyxendra")

    msg, kwargs = inter.response.messages[0]
    assert "Lyxendra" in msg
    assert "geclaimed" in msg
    assert kwargs.get("ephemeral")


async def test_claim_command_rejects_unknown_character():
    cog = DummyCog()
    cog.claim_results["Unknown"] = type(
        "Result", (), {"reason": "not_found", "claim": None}
    )()
    inter = DummyInteraction(cog)
    await claim.callback(inter, "Unknown")

    msg, kwargs = inter.response.messages[0]
    assert "nicht gefunden" in msg
    assert kwargs.get("ephemeral")


async def test_claim_command_reports_taken_character():
    cog = DummyCog()
    cog.claim_results["Lyxendra"] = type(
        "Result",
        (),
        {"reason": "taken", "claim": character_claim(user_id=99)},
    )()
    inter = DummyInteraction(cog)
    await claim.callback(inter, "Lyxendra")

    assert "bereits geclaimed" in inter.response.messages[0][0]


async def test_roster_char_autocomplete_shows_claimed_and_unclaimed_matches():
    cog = DummyCog()
    cog.data.claims = [character_claim(name="Voidok", user_id=99)]
    inter = DummyInteraction(cog)

    choices = await roster_char_autocomplete(inter, "Vo")

    assert [choice.value for choice in choices] == ["Voidok", "Voljin"]


async def test_user_claim_autocomplete_only_shows_own_claims():
    cog = DummyCog()
    cog.data.claims = [
        character_claim(name="Voidok", user_id=42),
        character_claim(name="Voljin", user_id=99),
    ]
    inter = DummyInteraction(cog)

    choices = await user_claim_autocomplete(inter, "Vo")

    assert [choice.value for choice in choices] == ["Voidok"]


async def test_all_claims_autocomplete_shows_all_claims_for_mod_context():
    cog = DummyCog()
    cog.data.claims = [
        character_claim(name="Voidok", user_id=42),
        character_claim(name="Voljin", user_id=99),
    ]
    inter = DummyInteraction(cog, manage_guild=True)

    choices = await all_claims_autocomplete(inter, "Vo")

    assert [choice.value for choice in choices] == ["Voidok", "Voljin"]


async def test_claim_char_autocomplete_uses_mod_scope():
    cog = DummyCog()
    cog.data.claims = [
        character_claim(name="Voidok", user_id=42),
        character_claim(name="Voljin", user_id=99),
    ]

    user_choices = await claim_char_autocomplete(DummyInteraction(cog), "Vo")
    mod_choices = await claim_char_autocomplete(
        DummyInteraction(cog, manage_guild=True), "Vo"
    )

    assert [choice.value for choice in user_choices] == ["Voidok"]
    assert [choice.value for choice in mod_choices] == ["Voidok", "Voljin"]


async def test_claims_mine_shows_user_claims():
    cog = DummyCog()
    cog.data.claim = character_claim()
    inter = DummyInteraction(cog)
    await claims_mine.callback(inter)

    assert "Lyxendra" in inter.response.messages[0][0]
    assert "ungeprüft" in inter.response.messages[0][0]


async def test_claims_list_filters_claims():
    cog = DummyCog()
    cog.data.claim = character_claim(user_id=99, status="verified")
    inter = DummyInteraction(cog)
    await claims_list.callback(inter, "verified")

    msg, kwargs = inter.response.messages[0]
    assert "<@99>" in msg
    assert "bestätigt" in msg
    assert kwargs.get("ephemeral")


async def test_claim_release_removes_own_claim():
    cog = DummyCog()
    cog.data.claim = character_claim()
    inter = DummyInteraction(cog)
    await claim_release.callback(inter, "Lyxendra")

    assert cog.data.claim is None
    assert "freigegeben" in inter.response.messages[0][0]


async def test_claim_remove_removes_claim():
    cog = DummyCog()
    cog.data.claim = character_claim()
    inter = DummyInteraction(cog)
    await claim_remove.callback(inter, "Lyxendra")

    assert cog.data.removed == ["id:1"]
    assert "entfernt" in inter.response.messages[0][0]


async def test_ghosts_empty_reports_clean_roster():
    cog = DummyCog()
    cog.data.ghosts = []
    inter = DummyInteraction(cog, manage_guild=True)
    await ghosts.callback(inter)

    msg, kwargs = inter.response.messages[0]
    assert "keine toten" in msg
    assert kwargs.get("ephemeral") is True


async def test_ghosts_chunks_when_list_exceeds_message_limit():
    """A large guild can have many ghosts — output must respect Discord's
    2000-char limit by sending follow-up messages."""
    cog = DummyCog()
    # Forge ~120 ghosts; each line is ~50 chars → ~6000 chars total → 4+ chunks.
    ghost_list = []
    for i in range(120):
        g = roster_member(name=f"Geist{i:03d}")
        g.character_key = f"id:g{i:03d}"
        g.is_ghost = True
        ghost_list.append(g)
    cog.data.ghosts = ghost_list
    inter = DummyInteraction(cog, manage_guild=True)

    await ghosts.callback(inter)

    # First message via response, the rest via followup.
    assert len(inter.response.messages) == 1
    assert inter.followup.messages, "expected followup chunks"
    # All chunks stay under Discord's 2000-char cap.
    first = inter.response.messages[0][0]
    assert len(first) <= 2000
    for msg, _ in inter.followup.messages:
        assert len(msg) <= 2000
    # Every ghost appears somewhere across all chunks.
    blob = first + "\n" + "\n".join(m for m, _ in inter.followup.messages)
    assert "Geist000" in blob
    assert "Geist119" in blob


async def test_ghosts_lists_dead_members_with_claim_owner():
    cog = DummyCog()
    g1 = roster_member("Eisprinz")
    g1.character_key = "id:g1"
    g1.is_ghost = True
    g2 = roster_member("Ohkaputt")
    g2.character_key = "id:g2"
    g2.is_ghost = True
    cog.data.ghosts = [g1, g2]
    cog.data.claim = character_claim(name="Eisprinz", key="id:g1", user_id=123)
    inter = DummyInteraction(cog, manage_guild=True)

    await ghosts.callback(inter)

    msg, kwargs = inter.response.messages[0]
    assert "Geister im Roster" in msg
    assert "(2)" in msg
    assert "Eisprinz" in msg
    assert "Ohkaputt" in msg
    # DummyData.get_claim returns the single stored claim for both — that's
    # OK for this happy-path test; we just want to verify the owner line
    # renders.
    assert "<@123>" in msg
    assert kwargs.get("ephemeral") is True


async def test_crafting_set_saves_profile():
    cog = DummyCog()
    cog.crafting_set_result = type(
        "Result",
        (),
        {
            "reason": "saved",
            "profession": character_profession(),
            "claim": character_claim(),
        },
    )()
    inter = DummyInteraction(cog)
    await crafting_set.callback(inter, "Lyxendra", "alchemy", 250, "Elixiere")

    msg, kwargs = inter.response.messages[0]
    assert cog.crafting_set_calls == [
        (42, "Lyxendra", "alchemy", 250, "Elixiere", False)
    ]
    assert "Gespeichert" in msg
    assert kwargs.get("ephemeral")


async def test_crafting_set_rejects_foreign_claim():
    cog = DummyCog()
    cog.crafting_set_result = type(
        "Result", (), {"reason": "forbidden", "profession": None}
    )()
    inter = DummyInteraction(cog)
    await crafting_set.callback(inter, "Voidok", "alchemy", 250, None)

    assert "nicht bearbeiten" in inter.response.messages[0][0]


async def test_crafting_remove_removes_profile_as_mod():
    cog = DummyCog()
    cog.crafting_remove_result = type(
        "Result", (), {"reason": "removed", "claim": character_claim()}
    )()
    inter = DummyInteraction(cog, manage_guild=True)
    await crafting_remove.callback(inter, "Lyxendra", "alchemy")

    assert cog.crafting_remove_calls == [(42, "Lyxendra", "alchemy", True)]
    assert "entfernt" in inter.response.messages[0][0]


async def test_crafting_mine_shows_profiles():
    cog = DummyCog()
    cog.data.professions = [character_profession()]
    inter = DummyInteraction(cog)
    await crafting_mine.callback(inter)

    assert "Lyxendra" in inter.response.messages[0][0]
    assert "Alchemie" in inter.response.messages[0][0]


async def test_crafting_list_filters_profiles():
    cog = DummyCog()
    cog.data.professions = [character_profession(user_id=99)]
    inter = DummyInteraction(cog, manage_guild=True)
    await crafting_list.callback(inter, "alchemy")

    msg, kwargs = inter.response.messages[0]
    assert "<@99>" in msg
    assert kwargs.get("ephemeral")


async def test_crafting_recipes_reports_no_open_recipes():
    cog = DummyCog()
    cog.recipe_selection_result = type(
        "Result",
        (),
        {
            "status": "ok",
            "claim": character_claim(name="Voidok"),
            "profile": character_profession(name="Voidok"),
            "profiles": None,
            "recipes": [],
        },
    )()
    inter = DummyInteraction(cog)
    await crafting_recipes.callback(inter, "Voidok", "alchemy", None)

    msg, kwargs = inter.response.messages[0]
    assert "keine offenen Spezialrezepte" in msg
    assert kwargs.get("ephemeral")


async def test_crafting_recipes_asks_for_profession_selection():
    cog = DummyCog()
    cog.recipe_selection_result = type(
        "Result",
        (),
        {
            "status": "choose_profession",
            "claim": character_claim(name="Voidok"),
            "profiles": [
                character_profession(name="Voidok", profession_id="alchemy"),
                character_profession(name="Voidok", profession_id="blacksmithing"),
            ],
            "profile": None,
            "recipes": None,
        },
    )()
    inter = DummyInteraction(cog)
    await crafting_recipes.callback(inter, "Voidok", None, None)

    msg, kwargs = inter.response.messages[0]
    assert "Bitte Beruf" in msg
    assert kwargs.get("view") is not None
    assert kwargs.get("ephemeral")


async def test_crafting_recipes_with_open_recipes_creates_selection_view():
    cog = DummyCog()
    cog.recipe_selection_result = type(
        "Result",
        (),
        {
            "status": "ok",
            "claim": character_claim(name="Voidok"),
            "profile": character_profession(name="Voidok"),
            "profiles": None,
            "recipes": [
                {
                    "id": "recipe.swiftness_potion",
                    "spell_id": "spell.2335",
                    "required_skill": 60,
                }
            ],
        },
    )()
    inter = DummyInteraction(cog)
    await crafting_recipes.callback(inter, "Voidok", "alchemy", None)

    msg, kwargs = inter.response.messages[0]
    assert "Voidok" in msg
    assert kwargs.get("view") is not None
    assert kwargs.get("ephemeral")


async def test_recipes_profession_autocomplete_shows_professions_for_selected_char():
    cog = DummyCog()
    cog.data.claims = [character_claim(name="Voidok", user_id=42)]
    cog.data.professions = [
        character_profession(name="Voidok", profession_id="alchemy", key="id:1"),
        character_profession(name="Voidok", profession_id="blacksmithing", key="id:1"),
        character_profession(name="Voljin", profession_id="tailoring", key="id:2"),
    ]
    inter = DummyInteraction(cog)
    inter.namespace.char = "Voidok"

    choices = await recipes_profession_autocomplete(inter, "")

    assert [choice.value for choice in choices] == ["alchemy", "blacksmithing"]


async def test_recipes_profession_autocomplete_rejects_foreign_claim():
    cog = DummyCog()
    cog.data.claims = [character_claim(name="Voljin", user_id=99)]
    cog.data.professions = [
        character_profession(name="Voljin", profession_id="alchemy")
    ]
    inter = DummyInteraction(cog)
    inter.namespace.char = "Voljin"

    assert await recipes_profession_autocomplete(inter, "") == []


async def test_crafting_learned_shows_known_recipes():
    cog = DummyCog()
    cog.known_recipes_result = type(
        "Result",
        (),
        {
            "status": "ok",
            "claim": character_claim(name="Voidok"),
            "recipes": [known_recipe(name="Voidok")],
        },
    )()
    inter = DummyInteraction(cog)
    await crafting_learned.callback(inter, "Voidok")

    assert "Swiftnesstrank" in inter.response.messages[0][0]


async def test_crafting_recipe_remove_removes_recipe_as_mod():
    cog = DummyCog()
    cog.remove_known_recipe_result = type(
        "Result",
        (),
        {"status": "removed", "claim": character_claim(name="Voidok")},
    )()
    inter = DummyInteraction(cog, manage_guild=True)
    await crafting_recipe_remove.callback(inter, "Voidok", "spell.2335")

    assert "Spezialrezept" in inter.response.messages[0][0]
    assert "entfernt" in inter.response.messages[0][0]


async def test_crafting_search_reports_result():
    cog = DummyCog()
    cog.crafting_search_result = type("Result", (), {"message": "Voidok kann das"})()
    inter = DummyInteraction(cog)
    await crafting_search.callback(inter, "Schwacher Heiltrank")

    assert "Voidok" in inter.response.messages[0][0]
