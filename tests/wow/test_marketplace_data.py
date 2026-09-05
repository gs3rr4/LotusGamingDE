import pytest

from lotus_bot.cogs.wow.marketplace_data import MarketplaceData


@pytest.mark.asyncio
async def test_listing_lifecycle(tmp_path):
    data = MarketplaceData(str(tmp_path / "marketplace.db"))
    listing = await data.create_listing(
        111, poster_discord_user_id=42, tag_kind="biete"
    )

    assert listing.status == "open"
    fetched = await data.get_listing(111)
    assert fetched == listing

    await data.set_listing_status(111, "done")
    assert (await data.get_listing(111)).status == "done"
    assert await data.get_listing(999) is None
    await data.close()


@pytest.mark.asyncio
async def test_claim_crafting_request_is_race_safe_only_first_wins(tmp_path):
    data = MarketplaceData(str(tmp_path / "marketplace.db"))
    await data.create_crafting_request(111, 42, "Kreuzritter", "enchant:20line")

    first = await data.claim_crafting_request(111, claimer_id=100)
    second = await data.claim_crafting_request(111, claimer_id=200)

    assert first is True
    assert second is False
    request = await data.get_crafting_request(111)
    assert request.status == "claimed"
    assert request.claimed_by_discord_user_id == 100  # the first claimer wins
    await data.close()


@pytest.mark.asyncio
async def test_claim_crafting_request_missing_thread_returns_false(tmp_path):
    data = MarketplaceData(str(tmp_path / "marketplace.db"))
    assert await data.claim_crafting_request(999, claimer_id=1) is False
    await data.close()


@pytest.mark.asyncio
async def test_ping_cooldown_blocks_same_item_allows_different_item(tmp_path):
    data = MarketplaceData(str(tmp_path / "marketplace.db"))

    first = await data.check_and_record_ping_cooldown(42, "item.1", cooldown_minutes=30)
    second = await data.check_and_record_ping_cooldown(
        42, "item.1", cooldown_minutes=30
    )
    other_item = await data.check_and_record_ping_cooldown(
        42, "item.2", cooldown_minutes=30
    )
    other_requester = await data.check_and_record_ping_cooldown(
        99, "item.1", cooldown_minutes=30
    )

    assert first is True
    assert second is False  # same requester, same item, within window
    assert other_item is True  # same requester, different item - not blocked
    assert other_requester is True  # different requester - not blocked
    await data.close()


@pytest.mark.asyncio
async def test_ping_cooldown_allows_again_after_window_elapsed(tmp_path):
    data = MarketplaceData(str(tmp_path / "marketplace.db"))
    assert await data.check_and_record_ping_cooldown(
        42, "item.1", cooldown_minutes=0
    ) is (True)
    # cooldown_minutes=0 means "always expired immediately" - a second call
    # should be allowed right away.
    assert (
        await data.check_and_record_ping_cooldown(42, "item.1", cooldown_minutes=0)
        is True
    )
    await data.close()
