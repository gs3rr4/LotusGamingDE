import pytest

from lotus_bot.cogs.wow.data import RosterMember, WoWData, parse_roster_member

pytestmark = pytest.mark.asyncio


def roster_member(name="Lyxendra", key="id:123"):
    return RosterMember(
        character_key=key,
        character_id=123,
        name=name,
        realm_slug="soulseeker",
        level=44,
        class_id=4,
        race_id=8,
        faction="HORDE",
        guild_rank=1,
    )


async def test_parse_roster_member_with_character_id():
    member = parse_roster_member(
        {
            "character": {
                "id": 123,
                "name": "Lyxendra",
                "level": 44,
                "realm": {"slug": "soulseeker"},
                "playable_class": {"id": 4},
                "playable_race": {"id": 8},
                "faction": {"type": "HORDE"},
            },
            "rank": 1,
        }
    )

    assert member.character_key == "id:123"
    assert member.name == "Lyxendra"
    assert member.level == 44
    assert member.class_id == 4
    assert member.race_id == 8
    assert member.faction == "HORDE"
    assert member.guild_rank == 1


async def test_parse_roster_member_fallback_key():
    member = parse_roster_member(
        {
            "character": {
                "name": "Noid",
                "level": 30,
                "realm": {"slug": "soulseeker"},
            }
        }
    )

    assert member.character_key == "realm:soulseeker:name:noid"


async def test_parse_roster_member_rejects_incomplete_entry():
    assert parse_roster_member({"character": {"name": "NoRealm", "level": 1}}) is None


async def test_replace_snapshot_seeds_digest_baseline(tmp_path):
    """The daily scan path keeps the live snapshot and the frozen baseline in sync."""
    data = WoWData(str(tmp_path / "wow.db"))
    await data.replace_snapshot([roster_member(name="Lyxendra", key="id:1")])

    snapshot = await data.get_snapshot()
    baseline = await data.get_digest_baseline()
    assert set(snapshot) == {"id:1"}
    assert set(baseline) == {"id:1"}
    await data.close()


async def test_refresh_live_snapshot_keeps_baseline_frozen(tmp_path):
    """Hourly live refresh adds new chars but must not move the digest baseline."""
    data = WoWData(str(tmp_path / "wow.db"))
    await data.replace_snapshot([roster_member(name="Lyxendra", key="id:1")])

    await data.refresh_live_snapshot(
        [
            roster_member(name="Lyxendra", key="id:1"),
            roster_member(name="Newbie", key="id:2"),
        ]
    )

    # Live snapshot now sees the freshly joined char (claimable within the hour)...
    assert set(await data.get_snapshot()) == {"id:1", "id:2"}
    # ...but the digest baseline still only knows yesterday's roster.
    assert set(await data.get_digest_baseline()) == {"id:1"}
    await data.close()


async def test_refresh_live_snapshot_preserves_known_ghost(tmp_path):
    """The roster endpoint omits is_ghost, so a known death must not be wiped."""
    data = WoWData(str(tmp_path / "wow.db"))
    ghost = roster_member(name="Lyxendra", key="id:1")
    ghost.is_ghost = True
    await data.replace_snapshot([ghost])

    # Hourly fetch carries the same char but without the ghost flag.
    await data.refresh_live_snapshot([roster_member(name="Lyxendra", key="id:1")])

    snapshot = await data.get_snapshot()
    assert snapshot["id:1"].is_ghost is True
    await data.close()


async def test_role_eligible_user_ids_only_counts_verified(tmp_path):
    data = WoWData(str(tmp_path / "wow.db"))
    alice = roster_member(name="AliceChar", key="id:a")
    bob = roster_member(name="BobChar", key="id:b")
    await data.replace_snapshot([alice, bob])
    await data.create_claim(alice, 111)
    await data.create_claim(bob, 222)

    # Both claims start unverified → nobody is entitled yet.
    assert await data.role_eligible_user_ids() == set()

    await data.verify_claim(alice.character_key, reviewer_id=999)
    assert await data.role_eligible_user_ids() == {111}
    await data.close()


async def test_role_eligible_user_ids_drops_chars_no_longer_in_roster(tmp_path):
    """A verified claim only counts while its char is still in the roster.

    Mirrors the real bug: someone keeps the guild role after their claimed
    char left the guild. The claim row stays (so a rejoin restores the role),
    but eligibility drops as soon as the char is gone from the snapshot.
    """
    data = WoWData(str(tmp_path / "wow.db"))
    leaver = roster_member(name="Leaver", key="id:l")
    await data.replace_snapshot([leaver])
    await data.create_claim(leaver, 111)
    await data.verify_claim(leaver.character_key, reviewer_id=999)
    assert await data.role_eligible_user_ids() == {111}

    # Char leaves the guild → next live roster refresh no longer lists it.
    await data.refresh_live_snapshot([])
    assert await data.role_eligible_user_ids() == set()
    # The claim itself is untouched, so a rejoin restores eligibility.
    await data.refresh_live_snapshot([leaver])
    assert await data.role_eligible_user_ids() == {111}
    await data.close()


async def test_role_eligible_user_ids_excludes_ghosts(tmp_path):
    data = WoWData(str(tmp_path / "wow.db"))
    dead = roster_member(name="Dead", key="id:d")
    dead.is_ghost = True
    await data.replace_snapshot([dead])
    await data.create_claim(dead, 111)
    await data.verify_claim(dead.character_key, reviewer_id=999)
    assert await data.role_eligible_user_ids() == set()
    await data.close()


async def test_init_db_seeds_baseline_from_legacy_snapshot(tmp_path):
    """A DB that predates the baseline table is seeded from the live snapshot.

    Without this, the first daily scan after the upgrade would diff against an
    empty baseline and announce the whole guild as new.
    """
    db_path = str(tmp_path / "wow.db")
    data = WoWData(db_path)
    await data.replace_snapshot([roster_member(name="Lyxendra", key="id:1")])

    # Simulate the legacy state: baseline empty, then re-run init.
    db = await data._get_db()
    await db.execute("DELETE FROM roster_digest_baseline")
    await db.commit()
    data._init_done = False

    await data.init_db()

    assert set(await data.get_digest_baseline()) == {"id:1"}
    await data.close()


async def test_ghost_members_filters_and_orders_by_level(tmp_path):
    data = WoWData(str(tmp_path / "wow.db"))
    alive = roster_member(name="Alive", key="id:alive")
    ghost_high = RosterMember(
        character_key="id:gh",
        character_id=2,
        name="HighGhost",
        realm_slug="soulseeker",
        level=42,
        class_id=4,
        race_id=8,
        faction="HORDE",
        guild_rank=1,
        is_ghost=True,
    )
    ghost_low = RosterMember(
        character_key="id:gl",
        character_id=3,
        name="LowGhost",
        realm_slug="soulseeker",
        level=12,
        class_id=4,
        race_id=8,
        faction="HORDE",
        guild_rank=1,
        is_ghost=True,
    )
    await data.replace_snapshot([alive, ghost_low, ghost_high])

    ghosts_list = await data.ghost_members()

    # Only dead chars, sorted by level descending.
    assert [g.name for g in ghosts_list] == ["HighGhost", "LowGhost"]
    assert all(g.is_ghost for g in ghosts_list)
    await data.close()


async def test_first_seen_at_is_idempotent_across_replace_snapshot(tmp_path):
    """first_seen_at must be set on first appearance and preserved across
    subsequent replace_snapshot calls — even when the member is briefly
    absent (left guild + rejoined)."""
    data = WoWData(str(tmp_path / "wow.db"))
    voidok = roster_member(name="Voidok", key="id:1")

    # First scan records voidok.
    await data.replace_snapshot([voidok])
    first = await data.first_seen_at("id:1")
    assert first is not None

    # Wait a moment; second scan with the same member — date stays.
    import asyncio

    await asyncio.sleep(0.01)
    await data.replace_snapshot([voidok])
    assert await data.first_seen_at("id:1") == first

    # Voidok leaves the guild (not in snapshot) and then comes back.
    await data.replace_snapshot([])
    await asyncio.sleep(0.01)
    await data.replace_snapshot([voidok])
    # Original date persists — INSERT OR IGNORE.
    assert await data.first_seen_at("id:1") == first

    # Unknown chars return None.
    assert await data.first_seen_at("id:nope") is None
    await data.close()


async def test_active_cooldown_count_ignores_expired(tmp_path):
    data = WoWData(str(tmp_path / "wow.db"))
    import datetime as dt

    now = dt.datetime.utcnow()
    # Expired cooldown — ready two hours ago.
    await data.set_cooldown(
        "id:1",
        "alchemy_transmute",
        "spell.17187",
        "Transmute: Arkanit",
        (now - dt.timedelta(hours=50)).isoformat(),
        (now - dt.timedelta(hours=2)).isoformat(),
    )
    # Running cooldown — ready in 90 hours.
    await data.set_cooldown(
        "id:2",
        "tailoring_mooncloth",
        "spell.18560",
        "Mondstoff",
        now.isoformat(),
        (now + dt.timedelta(hours=90)).isoformat(),
    )

    assert await data.active_cooldown_count() == 1
    await data.close()


async def test_claim_lifecycle_and_case_insensitive_lookup(tmp_path):
    data = WoWData(str(tmp_path / "wow.db"))
    member = roster_member()
    await data.replace_snapshot([member])

    found = await data.find_roster_member_by_name("lyxendra")
    assert found == member

    claim, created = await data.create_claim(member, 42)
    assert created is True
    assert claim.character_name == "Lyxendra"
    assert claim.status == "unverified"

    same_claim, created = await data.create_claim(member, 43)
    assert created is False
    assert same_claim.discord_user_id == 42

    await data.verify_claim(member.character_key, 99)
    verified = await data.get_claim(member.character_key)
    assert verified.status == "verified"
    assert verified.verified_by == 99

    assert await data.release_claim(member.character_key, 43) is False
    assert await data.release_claim(member.character_key, 42) is True
    assert await data.get_claim(member.character_key) is None
    await data.close()


async def test_crafting_notify_defaults_on_and_can_be_toggled(tmp_path):
    data = WoWData(str(tmp_path / "wow.db"))
    member = roster_member()
    await data.replace_snapshot([member])
    claim, _ = await data.create_claim(member, 42)

    # Default ON (opt-out), per the guild lead's decision - a fresh claim
    # is pingable for crafting requests without any action needed.
    assert claim.crafting_notify is True
    assert (await data.get_claim(member.character_key)).crafting_notify is True

    await data.set_crafting_notify(member.character_key, False)
    assert (await data.get_claim(member.character_key)).crafting_notify is False

    await data.set_crafting_notify(member.character_key, True)
    assert (await data.get_claim(member.character_key)).crafting_notify is True
    await data.close()


async def test_crafting_notify_map_batches_multiple_keys_in_one_query(tmp_path):
    data = WoWData(str(tmp_path / "wow.db"))
    alpha = roster_member(name="Alpha", key="id:1")
    beta = roster_member(name="Beta", key="id:2")
    await data.replace_snapshot([alpha, beta])
    await data.create_claim(alpha, 42)
    await data.create_claim(beta, 43)
    await data.set_crafting_notify("id:2", False)

    result = await data.crafting_notify_map(["id:1", "id:2", "id:unknown"])

    assert result == {"id:1": True, "id:2": False}
    assert await data.crafting_notify_map([]) == {}
    await data.close()


async def test_claim_listing_and_review_message_lookup(tmp_path):
    data = WoWData(str(tmp_path / "wow.db"))
    member = roster_member()
    await data.replace_snapshot([member])
    claim, _ = await data.create_claim(member, 42)
    await data.set_claim_review_message(claim.character_key, 555)

    by_message = await data.get_claim_by_review_message(555)
    assert by_message.character_key == claim.character_key
    by_name = await data.get_claim_by_name("lyxendra")
    assert by_name == by_message
    assert await data.claims_for_user(42) == [by_message]
    assert await data.list_claims("unverified") == [by_message]
    assert await data.list_claims("verified") == []

    await data.remove_claim(claim.character_key)
    assert await data.list_claims() == []
    await data.close()


async def test_character_profession_lifecycle(tmp_path):
    data = WoWData(str(tmp_path / "wow.db"))
    member = roster_member()
    await data.replace_snapshot([member])
    claim, _ = await data.create_claim(member, 42)

    profile = await data.set_character_profession(claim, "alchemy", 250, "TrÃ¤nke")
    assert profile.character_name == "Lyxendra"
    assert profile.profession_id == "alchemy"
    assert profile.skill_level == 250
    assert profile.specialization == "TrÃ¤nke"

    updated = await data.set_character_profession(claim, "alchemy", 300, None)
    assert updated.skill_level == 300
    assert updated.specialization is None

    assert await data.professions_for_user(42) == [updated]
    assert await data.list_professions("alchemy") == [updated]
    assert await data.find_crafters("alchemy", 275) == [updated]
    assert await data.find_crafters("alchemy", 301) == []

    assert await data.remove_character_profession(member.character_key, "alchemy")
    assert await data.professions_for_user(42) == []
    await data.close()


async def test_character_profession_rejects_invalid_skill(tmp_path):
    data = WoWData(str(tmp_path / "wow.db"))
    member = roster_member()
    await data.replace_snapshot([member])
    claim, _ = await data.create_claim(member, 42)

    with pytest.raises(ValueError):
        await data.set_character_profession(claim, "alchemy", 0)
    with pytest.raises(ValueError):
        await data.set_character_profession(claim, "alchemy", 301)
    await data.close()


async def test_character_profession_hidden_after_claim_remove(tmp_path):
    data = WoWData(str(tmp_path / "wow.db"))
    member = roster_member()
    await data.replace_snapshot([member])
    claim, _ = await data.create_claim(member, 42)
    await data.set_character_profession(claim, "alchemy", 250)

    await data.remove_claim(member.character_key)

    assert await data.list_professions() == []
    assert await data.find_crafters("alchemy", 1) == []
    await data.close()


async def test_known_recipe_lifecycle_and_duplicate_blocking(tmp_path):
    data = WoWData(str(tmp_path / "wow.db"))
    member = roster_member()
    await data.replace_snapshot([member])
    claim, _ = await data.create_claim(member, 42)
    await data.set_character_profession(claim, "alchemy", 250)

    assert await data.add_known_recipes(member.character_key, "alchemy", ["spell.2335"])
    assert (
        await data.add_known_recipes(member.character_key, "alchemy", ["spell.2335"])
    ) == 0
    assert await data.known_recipe_spell_ids(member.character_key) == {"spell.2335"}

    recipes = await data.known_recipes_for_character(member.character_key)
    assert len(recipes) == 1
    assert recipes[0].spell_id == "spell.2335"
    assert recipes[0].profession_id == "alchemy"

    crafters = await data.find_crafters_with_known_recipe("alchemy", 200, "spell.2335")
    assert crafters[0].character_name == "Lyxendra"

    assert await data.remove_known_recipe(member.character_key, "spell.2335")
    assert not await data.remove_known_recipe(member.character_key, "spell.2335")
    await data.close()


async def test_known_recipe_empty_insert_is_noop(tmp_path):
    data = WoWData(str(tmp_path / "wow.db"))

    assert await data.add_known_recipes("id:missing", "alchemy", []) == 0
    assert await data.last_scan_at() is None
    await data.close()


async def test_known_recipes_survive_claim_remove_but_are_hidden(tmp_path):
    data = WoWData(str(tmp_path / "wow.db"))
    member = roster_member()
    await data.replace_snapshot([member])
    claim, _ = await data.create_claim(member, 42)
    await data.set_character_profession(claim, "alchemy", 250)
    await data.add_known_recipes(member.character_key, "alchemy", ["spell.2335"])

    await data.remove_claim(member.character_key)

    assert await data.known_recipe_spell_ids(member.character_key) == {"spell.2335"}
    assert await data.find_crafters_with_known_recipe("alchemy", 1, "spell.2335") == []
    await data.close()
