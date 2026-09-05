import discord
from discord import app_commands

from lotus_bot.log_setup import get_logger
from lotus_bot.permissions import moderator_only

from .cog import (
    CraftingProfessionSelectView,
    CraftingRecipeSelectionView,
    WoWCog,
)
from .data import CharacterClaim, CharacterKnownRecipe, CharacterProfession

logger = get_logger(__name__)

wow_group = app_commands.Group(
    name="wow",
    description="WoW Classic Hardcore Befehle",
)


def _format_claim_status(status: str) -> str:
    return "bestätigt" if status == "verified" else "ungeprüft"


def _format_claim_line(claim: CharacterClaim, *, include_user: bool = False) -> str:
    user = f" - <@{claim.discord_user_id}>" if include_user else ""
    return f"**{claim.character_name}** ({_format_claim_status(claim.status)}){user}"


def _is_mod(interaction: discord.Interaction) -> bool:
    permissions = getattr(interaction.user, "guild_permissions", None)
    return bool(permissions and permissions.manage_guild)


def _format_profession_line(
    profile: CharacterProfession, cog: WoWCog, *, include_user: bool = False
) -> str:
    user = f" - <@{profile.discord_user_id}>" if include_user else ""
    specialization = f" ({profile.specialization})" if profile.specialization else ""
    return (
        f"**{profile.character_name}** - "
        f"{cog._profession_name(profile.profession_id)} "
        f"{profile.skill_level}{specialization}{user}"
    )


def _format_known_recipe_line(recipe: CharacterKnownRecipe, cog: WoWCog) -> str:
    static = cog._recipe_by_spell_id(recipe.spell_id)
    recipe_name = cog._recipe_name(static) if static else recipe.spell_id
    return f"- **{recipe_name}** ({cog._profession_name(recipe.profession_id)})"


async def profession_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        return []
    return [
        app_commands.Choice(name=name, value=value)
        for name, value in cog.profession_choices(current)
    ]


def _match_choice_text(name: str, current: str) -> bool:
    return not current or current.casefold() in name.casefold()


async def roster_char_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        return []
    snapshot = await cog.data.get_snapshot()
    names = sorted({member.name for member in snapshot.values()}, key=str.casefold)
    return [
        app_commands.Choice(name=name[:100], value=name)
        for name in names
        if _match_choice_text(name, current)
    ][:25]


async def user_claim_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        return []
    claims = await cog.data.claims_for_user(interaction.user.id)
    return [
        app_commands.Choice(name=claim.character_name[:100], value=claim.character_name)
        for claim in claims
        if _match_choice_text(claim.character_name, current)
    ][:25]


async def all_claims_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        return []
    claims = await cog.data.list_claims("all")
    return [
        app_commands.Choice(name=claim.character_name[:100], value=claim.character_name)
        for claim in claims
        if _match_choice_text(claim.character_name, current)
    ][:25]


async def claim_char_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    if _is_mod(interaction):
        return await all_claims_autocomplete(interaction, current)
    return await user_claim_autocomplete(interaction, current)


async def bank_char_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        return []
    bank_chars = await cog.data.list_bank_characters()
    return [
        app_commands.Choice(name=bank.character_name[:100], value=bank.character_name)
        for bank in bank_chars
        if _match_choice_text(bank.character_name, current)
    ][:25]


async def recipes_profession_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        return []
    char = getattr(interaction.namespace, "char", None)
    if not char:
        return []
    claim = await cog.data.get_claim_by_name(char)
    if not claim:
        return []
    if not _is_mod(interaction) and claim.discord_user_id != interaction.user.id:
        return []
    profiles = await cog.data.professions_for_character(claim.character_key)
    choices = []
    for profile in profiles:
        profession = cog._get_static_record("professions", profile.profession_id)
        if not cog._is_crafting_profession(profession):
            continue
        name = cog._profession_name(profile.profession_id)
        if _match_choice_text(name, current) or _match_choice_text(
            profile.profession_id, current
        ):
            choices.append(
                app_commands.Choice(name=name[:100], value=profile.profession_id)
            )
    return choices[:25]


async def known_recipe_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        return []
    char = getattr(interaction.namespace, "char", None)
    if not char:
        return []
    claim = await cog.data.get_claim_by_name(char)
    if not claim:
        return []
    if not _is_mod(interaction) and claim.discord_user_id != interaction.user.id:
        return []
    needle = current.casefold()
    choices = []
    for recipe in await cog.data.known_recipes_for_character(claim.character_key):
        static = cog._recipe_by_spell_id(recipe.spell_id)
        name = cog._recipe_name(static) if static else recipe.spell_id
        if not needle or needle in name.casefold():
            choices.append(app_commands.Choice(name=name[:100], value=recipe.spell_id))
    return choices[:25]


@wow_group.command(name="setup", description="Konfiguriert den WoW-Ankündigungschannel")
@moderator_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(channel="Channel für WoW-Meilensteinmeldungen")
async def setup(interaction: discord.Interaction, channel: discord.TextChannel):
    logger.info(f"/wow setup by {interaction.user} channel={channel.id}")
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        await interaction.response.send_message(
            "❌ WoW-System nicht verfügbar.", ephemeral=True
        )
        return
    await cog.set_announcement_channel(channel.id)
    await interaction.response.send_message(
        f"✅ WoW-Ankündigungen werden in {channel.mention} gepostet.",
        ephemeral=True,
    )


@wow_group.command(name="status", description="Zeigt den WoW-Tracker Status")
async def status(interaction: discord.Interaction):
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        await interaction.response.send_message(
            "❌ WoW-System nicht verfügbar.", ephemeral=True
        )
        return

    info = await cog.status()
    channel_id = info.get("channel_id")
    officer_channel_id = info.get("officer_channel_id")
    channel_text = f"<#{channel_id}>" if channel_id else "nicht konfiguriert"
    officer_channel_text = (
        f"<#{officer_channel_id}>" if officer_channel_id else "nicht konfiguriert"
    )
    panel_channel_id = info.get("panel_channel_id")
    panel_message_id = info.get("panel_message_id")
    panel_text = (
        f"<#{panel_channel_id}> / `{panel_message_id}`"
        if panel_channel_id and panel_message_id
        else "nicht konfiguriert"
    )
    last_scan = info.get("last_scan_at") or "noch nie"
    await interaction.response.send_message(
        "\n".join(
            [
                f"Guild: **{info['guild']}**",
                f"Realm: **{info['realm']}**",
                f"Channel: {channel_text}",
                f"Offi-Channel: {officer_channel_text}",
                f"Panel: {panel_text}",
                f"Letzter Scan: {last_scan}",
                f"Mitglieder im Snapshot: {info['member_count']}",
                f"Recipe-Events: {info.get('recipe_events', 'aktiv')}",
                "Digest: täglich um 09:00 Uhr",
            ]
        ),
        ephemeral=True,
    )


@wow_group.command(name="scan", description="Prüft den WoW-Roster sofort")
@moderator_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(post="Meilensteine posten, falls welche gefunden werden")
async def scan(interaction: discord.Interaction, post: bool = True):
    logger.info(f"/wow scan by {interaction.user} post={post}")
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        await interaction.response.send_message(
            "❌ WoW-System nicht verfügbar.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        result = await cog.scan(post=post, persist=post)
    except Exception as exc:
        logger.error("[WoWCommands] Scan failed: %s", exc, exc_info=True)
        await interaction.followup.send("❌ WoW-Scan fehlgeschlagen.")
        return

    mode = "gepostet" if post else "Dry-Run"
    await interaction.followup.send(
        f"{mode}: {result.member_count} Mitglieder geprüft, "
        f"{len(result.milestones)} Meilensteine gefunden, {result.posted} gepostet."
    )


@wow_group.command(
    name="role-sync",
    description="Gleicht die Gildenrolle mit den verifizierten Claims ab (nur Mods)",
)
@moderator_only()
@app_commands.default_permissions(manage_guild=True)
async def role_sync(interaction: discord.Interaction):
    logger.info(f"/wow role-sync by {interaction.user}")
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        await interaction.response.send_message(
            "❌ WoW-System nicht verfügbar.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        result = await cog.sync_guild_role()
    except Exception as exc:
        logger.error("[WoWCommands] Role-Sync failed: %s", exc, exc_info=True)
        await interaction.followup.send("❌ Rollen-Abgleich fehlgeschlagen.")
        return

    if not result.available:
        await interaction.followup.send(
            "❌ Gilde oder Rolle nicht gefunden — Abgleich nicht möglich."
        )
        return

    await interaction.followup.send(
        f"✅ Rollen-Abgleich fertig: **{result.eligible}** berechtigt, "
        f"**{result.granted}** vergeben, **{result.removed}** entzogen."
    )


@wow_group.command(
    name="sync-report",
    description="Zeigt Abweichungen zwischen Claims und In-Game-Rängen (nur Mods)",
)
@moderator_only()
@app_commands.default_permissions(manage_guild=True)
async def sync_report(interaction: discord.Interaction):
    logger.info(f"/wow sync-report by {interaction.user}")
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        await interaction.response.send_message(
            "❌ WoW-System nicht verfügbar.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        members = list((await cog.data.get_snapshot()).values())
        chunks = await cog.sync_report_chunks(members)
    except Exception as exc:
        logger.error("[WoWCommands] Sync-Report failed: %s", exc, exc_info=True)
        await interaction.followup.send("❌ Sync-Report fehlgeschlagen.")
        return

    if not chunks:
        await interaction.followup.send("✅ Alles synchron — keine Abweichungen.")
        return
    for chunk in chunks:
        await interaction.followup.send(chunk)


@wow_group.command(
    name="whois",
    description="Zeigt was wir über einen Char wissen (privat).",
)
@app_commands.describe(char="Charaktername aus dem Roster")
@app_commands.autocomplete(char=roster_char_autocomplete)
async def whois(interaction: discord.Interaction, char: str):
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        await interaction.response.send_message(
            "❌ WoW-System nicht verfügbar.", ephemeral=True
        )
        return
    view = await cog.build_whois_view(char, interaction.user.id)
    if view is None:
        await interaction.response.send_message(
            f"**{char}** ist nicht im aktuellen Roster.", ephemeral=True
        )
        return
    await interaction.response.send_message(view=view, ephemeral=True)


@wow_group.command(name="claim", description="Claimt einen Black-Lotus-Charakter")
@app_commands.describe(char="Name des Charakters im Black-Lotus-Roster")
@app_commands.autocomplete(char=roster_char_autocomplete)
async def claim(interaction: discord.Interaction, char: str):
    logger.info(f"/wow claim by {interaction.user} char={char}")
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        await interaction.response.send_message(
            "❌ WoW-System nicht verfügbar.", ephemeral=True
        )
        return

    result = await cog.claim_character(interaction.user.id, char)
    if result.reason == "not_found":
        await interaction.response.send_message(
            f"❌ **{char}** wurde im aktuellen Black-Lotus-Roster nicht gefunden.",
            ephemeral=True,
        )
        return
    if result.claim is None:
        await interaction.response.send_message(
            "❌ Claim konnte nicht erstellt werden.", ephemeral=True
        )
        return
    if result.reason == "taken":
        await interaction.response.send_message(
            f"❌ **{result.claim.character_name}** ist bereits geclaimed.",
            ephemeral=True,
        )
        return
    if result.reason == "already_own":
        await interaction.response.send_message(
            f"ℹ️ Du hast **{result.claim.character_name}** bereits geclaimed "
            f"({_format_claim_status(result.claim.status)}).",
            ephemeral=True,
        )
        return

    warning = (
        "" if result.review_posted else "\n⚠️ Offi-Review konnte nicht gepostet werden."
    )
    await interaction.response.send_message(
        f"✅ Du hast **{result.claim.character_name}** geclaimed "
        f"({_format_claim_status(result.claim.status)}).{warning}",
        ephemeral=True,
    )


@wow_group.command(
    name="claim-release", description="Gibt deinen eigenen WoW-Claim frei"
)
@app_commands.describe(char="Name des geclaimten Charakters")
@app_commands.autocomplete(char=user_claim_autocomplete)
async def claim_release(interaction: discord.Interaction, char: str):
    logger.info(f"/wow claim-release by {interaction.user} char={char}")
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        await interaction.response.send_message(
            "❌ WoW-System nicht verfügbar.", ephemeral=True
        )
        return

    existing = await cog.data.get_claim_by_name(char)
    if not existing or existing.discord_user_id != interaction.user.id:
        await interaction.response.send_message(
            f"❌ Du hast **{char}** nicht geclaimed.", ephemeral=True
        )
        return
    await cog.data.remove_claim(existing.character_key)
    await cog.reconcile_guild_role_for(existing.discord_user_id)
    await interaction.response.send_message(
        f"✅ Claim für **{existing.character_name}** wurde freigegeben.", ephemeral=True
    )


@wow_group.command(name="claim-remove", description="Entfernt einen WoW-Claim")
@moderator_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(char="Name des geclaimten Charakters")
@app_commands.autocomplete(char=all_claims_autocomplete)
async def claim_remove(interaction: discord.Interaction, char: str):
    logger.info(f"/wow claim-remove by {interaction.user} char={char}")
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        await interaction.response.send_message(
            "❌ WoW-System nicht verfügbar.", ephemeral=True
        )
        return

    existing = await cog.data.get_claim_by_name(char)
    if not existing:
        await interaction.response.send_message(
            f"ℹ️ **{char}** ist aktuell nicht geclaimed.", ephemeral=True
        )
        return
    await cog.data.remove_claim(existing.character_key)
    await cog.reconcile_guild_role_for(existing.discord_user_id)
    await interaction.response.send_message(
        f"✅ Claim für **{existing.character_name}** wurde entfernt.", ephemeral=True
    )


@wow_group.command(
    name="ghosts",
    description="Listet tote Chars, die noch in der Gilde sind (nur Mods)",
)
@moderator_only()
@app_commands.default_permissions(manage_guild=True)
async def ghosts(interaction: discord.Interaction):
    """List dead characters that are still members of the guild.

    Mod-only roster cleanup helper: each entry shows level/race/class and
    the claim owner (if any), so officers can kick them in-game.
    """
    logger.info(f"/wow ghosts by {interaction.user}")
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        await interaction.response.send_message(
            "❌ WoW-System nicht verfügbar.", ephemeral=True
        )
        return

    ghost_members = await cog.data.ghost_members()
    if not ghost_members:
        await interaction.response.send_message(
            "✅ Aktuell sind keine toten Chars mehr im Roster.", ephemeral=True
        )
        return

    lines = [f"🕯️ **Geister im Roster** ({len(ghost_members)})"]
    for member in ghost_members:
        claim = await cog.data.get_claim(member.character_key)
        owner = f" — <@{claim.discord_user_id}>" if claim else " — _nicht geclaimed_"
        lines.append(f"- {cog._format_roster_line(member)}{owner}")

    # Discord caps message content at 2000 chars. Chunk the list so a guild
    # with many ghosts doesn't blow past the limit.
    chunks = _pack_lines_into_chunks(lines)
    await interaction.response.send_message(chunks[0], ephemeral=True)
    for extra in chunks[1:]:
        await interaction.followup.send(extra, ephemeral=True)


def _pack_lines_into_chunks(lines: list[str], limit: int = 1900) -> list[str]:
    """Pack ``lines`` into messages of at most ``limit`` characters each.

    Joins lines with newlines greedily; a single oversized line is sent on
    its own (Discord will truncate it). Default ``limit`` 1900 leaves some
    headroom under Discord's 2000-char content cap.
    """
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        added = len(line) + (1 if current else 0)  # account for "\n" join
        if current and current_len + added > limit:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
            added = len(line)
        current.append(line)
        current_len += added
    if current:
        chunks.append("\n".join(current))
    return chunks


gbank_group = app_commands.Group(
    name="gbank",
    description="Verwaltet Gildenbank-Chars",
    parent=wow_group,
)


@gbank_group.command(name="add", description="Markiert einen Char als Gildenbank-Char")
@moderator_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(char="Name des Chars im Black-Lotus-Roster")
@app_commands.autocomplete(char=roster_char_autocomplete)
async def gbank_add(interaction: discord.Interaction, char: str):
    logger.info(f"/wow gbank add by {interaction.user} char={char}")
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        await interaction.response.send_message(
            "❌ WoW-System nicht verfügbar.", ephemeral=True
        )
        return

    member = await cog.data.find_roster_member_by_name(char)
    if member is None:
        await interaction.response.send_message(
            f"❌ **{char}** wurde im aktuellen Black-Lotus-Roster nicht gefunden.",
            ephemeral=True,
        )
        return

    await cog.data.add_bank_character(
        member.character_key, member.name, interaction.user.id
    )
    claim = await cog.data.get_claim(member.character_key)
    warning = (
        ""
        if claim
        else (
            "\n⚠️ Dieser Char ist noch nicht geclaimed — Anfragen landen "
            "vorerst nur im Officer-Channel, bis ihn jemand claimed."
        )
    )
    await interaction.response.send_message(
        f"✅ **{member.name}** ist jetzt ein Gildenbank-Char.{warning}",
        ephemeral=True,
    )


@gbank_group.command(
    name="remove", description="Entfernt einen Char aus den Gildenbank-Chars"
)
@moderator_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(char="Name des Gildenbank-Chars")
@app_commands.autocomplete(char=bank_char_autocomplete)
async def gbank_remove(interaction: discord.Interaction, char: str):
    logger.info(f"/wow gbank remove by {interaction.user} char={char}")
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        await interaction.response.send_message(
            "❌ WoW-System nicht verfügbar.", ephemeral=True
        )
        return

    member = await cog.data.find_roster_member_by_name(char)
    character_key = member.character_key if member else None
    if character_key is None:
        # Fall back to matching by name among registered bank chars.
        for bank in await cog.data.list_bank_characters():
            if bank.character_name.casefold() == char.casefold():
                character_key = bank.character_key
                break

    removed = (
        await cog.data.remove_bank_character(character_key) if character_key else False
    )
    if not removed:
        await interaction.response.send_message(
            f"ℹ️ **{char}** ist aktuell kein Gildenbank-Char.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        f"✅ **{char}** ist kein Gildenbank-Char mehr.", ephemeral=True
    )


@gbank_group.command(name="list", description="Listet alle Gildenbank-Chars")
@moderator_only()
@app_commands.default_permissions(manage_guild=True)
async def gbank_list(interaction: discord.Interaction):
    logger.info(f"/wow gbank list by {interaction.user}")
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        await interaction.response.send_message(
            "❌ WoW-System nicht verfügbar.", ephemeral=True
        )
        return

    bank_chars = await cog.data.list_bank_characters()
    if not bank_chars:
        await interaction.response.send_message(
            "📭 Es sind keine Gildenbank-Chars eingetragen.", ephemeral=True
        )
        return

    lines = []
    for bank in bank_chars:
        claim = await cog.data.get_claim(bank.character_key)
        owner = f"<@{claim.discord_user_id}>" if claim else "_nicht geclaimed_"
        lines.append(f"- **{bank.character_name}** — {owner}")
    await interaction.response.send_message(
        "🏦 **Gildenbank-Chars:**\n" + "\n".join(lines), ephemeral=True
    )


claims_group = app_commands.Group(
    name="claims",
    description="WoW Character Claims",
    parent=wow_group,
)


@claims_group.command(name="mine", description="Zeigt deine geclaimten WoW-Charaktere")
async def claims_mine(interaction: discord.Interaction):
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        await interaction.response.send_message(
            "❌ WoW-System nicht verfügbar.", ephemeral=True
        )
        return

    claims = await cog.data.claims_for_user(interaction.user.id)
    if not claims:
        await interaction.response.send_message(
            "Du hast noch keine WoW-Charaktere geclaimed.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        "\n".join(_format_claim_line(claim) for claim in claims),
        ephemeral=True,
    )


@claims_group.command(name="list", description="Listet WoW Character Claims")
@moderator_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.choices(
    status=[
        app_commands.Choice(name="all", value="all"),
        app_commands.Choice(name="unverified", value="unverified"),
        app_commands.Choice(name="verified", value="verified"),
    ]
)
async def claims_list(interaction: discord.Interaction, status: str = "all"):
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        await interaction.response.send_message(
            "❌ WoW-System nicht verfügbar.", ephemeral=True
        )
        return

    claims = await cog.data.list_claims(status)
    if not claims:
        await interaction.response.send_message(
            "Keine Claims gefunden.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        "\n".join(_format_claim_line(claim, include_user=True) for claim in claims),
        ephemeral=True,
    )


@claims_group.command(
    name="repost",
    description="Postet fehlende Claim-Review-Karten erneut (nur Mods)",
)
@moderator_only()
@app_commands.default_permissions(manage_guild=True)
async def claims_repost(interaction: discord.Interaction):
    logger.info(f"/wow claims repost by {interaction.user}")
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        await interaction.response.send_message(
            "❌ WoW-System nicht verfügbar.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    reposted, failed = await cog.repost_missing_claim_reviews()
    if reposted == 0 and failed == 0:
        await interaction.followup.send(
            "✅ Alle offenen Claims haben bereits eine Review-Karte — nichts zu tun."
        )
        return
    msg = f"✅ {reposted} Review-Karte(n) neu gepostet."
    if failed:
        msg += (
            f"\n⚠️ {failed} fehlgeschlagen — prüfe die Schreibrechte des Bots "
            "im Offi-Channel."
        )
    await interaction.followup.send(msg)


cooldowns_group = app_commands.Group(
    name="cooldowns",
    description="WoW Craft-Cooldowns (Transmute, Mooncloth, ...)",
    parent=wow_group,
)


@cooldowns_group.command(
    name="mine",
    description="Zeigt deine aktiven Craft-Cooldowns.",
)
async def cooldowns_mine(interaction: discord.Interaction):
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        await interaction.response.send_message(
            "❌ WoW-System nicht verfügbar.", ephemeral=True
        )
        return
    cooldowns = await cog.data.cooldowns_for_user(interaction.user.id)
    if not cooldowns:
        await interaction.response.send_message(
            "Du hast aktuell keine Cooldowns eingetragen. "
            "Trag einen über das Panel → **⏳ Cooldown loggen** ein, sobald "
            "du z.B. eine Transmutation oder Mondstoff produziert hast.",
            ephemeral=True,
        )
        return
    lines = []
    for cd in cooldowns:
        ready_label = cog._format_cooldown_ready_label(cd.ready_at)
        lines.append(f"- **{cd.character_name}** ({cd.last_spell_name}): {ready_label}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


crafting_group = app_commands.Group(
    name="crafting",
    description="WoW Crafting Profile und Suche",
    parent=wow_group,
)


@crafting_group.command(name="set", description="Setzt Beruf und Skill eines Chars")
@app_commands.describe(
    char="Name des geclaimten Charakters",
    profession="Beruf",
    skill="Berufsskill 1-300",
    specialization="Optionale Spezialisierung",
)
@app_commands.autocomplete(
    char=claim_char_autocomplete, profession=profession_autocomplete
)
async def crafting_set(
    interaction: discord.Interaction,
    char: str,
    profession: str,
    skill: app_commands.Range[int, 1, 300],
    specialization: str | None = None,
):
    logger.info(
        f"/wow crafting set by {interaction.user} char={char} profession={profession}"
    )
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        await interaction.response.send_message(
            "WoW-System nicht verfuegbar.", ephemeral=True
        )
        return

    result = await cog.set_crafting_profile(
        interaction.user.id,
        char,
        profession,
        int(skill),
        specialization,
        is_mod=_is_mod(interaction),
    )
    if result.reason == "unknown_profession":
        await interaction.response.send_message(
            f"Beruf **{profession}** ist unbekannt.", ephemeral=True
        )
        return
    if result.reason == "not_claimed":
        await interaction.response.send_message(
            f"**{char}** ist nicht geclaimed.", ephemeral=True
        )
        return
    if result.reason == "forbidden":
        await interaction.response.send_message(
            f"Du darfst **{char}** nicht bearbeiten.", ephemeral=True
        )
        return
    if result.reason == "invalid_skill":
        await interaction.response.send_message(
            "Skill muss zwischen 1 und 300 liegen.", ephemeral=True
        )
        return
    if result.reason == "primary_limit":
        await interaction.response.send_message(
            f"**{char}** hat bereits zwei Hauptberufe gepflegt.", ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"Gespeichert: {cog.format_profession(result.profession)}",
        ephemeral=True,
    )


@crafting_group.command(name="remove", description="Entfernt einen Beruf vom Char")
@app_commands.describe(
    char="Name des geclaimten Charakters",
    profession="Beruf",
)
@app_commands.autocomplete(
    char=claim_char_autocomplete, profession=profession_autocomplete
)
async def crafting_remove(
    interaction: discord.Interaction,
    char: str,
    profession: str,
):
    logger.info(
        f"/wow crafting remove by {interaction.user} char={char} profession={profession}"
    )
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        await interaction.response.send_message(
            "WoW-System nicht verfuegbar.", ephemeral=True
        )
        return

    result = await cog.remove_crafting_profile(
        interaction.user.id,
        char,
        profession,
        is_mod=_is_mod(interaction),
    )
    if result.reason == "unknown_profession":
        await interaction.response.send_message(
            f"Beruf **{profession}** ist unbekannt.", ephemeral=True
        )
        return
    if result.reason == "not_claimed":
        await interaction.response.send_message(
            f"**{char}** ist nicht geclaimed.", ephemeral=True
        )
        return
    if result.reason == "forbidden":
        await interaction.response.send_message(
            f"Du darfst **{char}** nicht bearbeiten.", ephemeral=True
        )
        return
    if result.reason == "not_set":
        await interaction.response.send_message(
            f"Fuer **{char}** ist dieser Beruf nicht gepflegt.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"Beruf fuer **{result.claim.character_name}** entfernt.",
        ephemeral=True,
    )


@crafting_group.command(name="mine", description="Zeigt deine Crafting-Profile")
async def crafting_mine(interaction: discord.Interaction):
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        await interaction.response.send_message(
            "WoW-System nicht verfuegbar.", ephemeral=True
        )
        return

    profiles = await cog.data.professions_for_user(interaction.user.id)
    if not profiles:
        await interaction.response.send_message(
            "Du hast noch keine Crafting-Profile gepflegt.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        "\n".join(_format_profession_line(profile, cog) for profile in profiles),
        ephemeral=True,
    )


@crafting_group.command(name="list", description="Listet Crafting-Profile")
@moderator_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(profession="Optionaler Beruf-Filter")
@app_commands.autocomplete(profession=profession_autocomplete)
async def crafting_list(
    interaction: discord.Interaction, profession: str | None = None
):
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        await interaction.response.send_message(
            "WoW-System nicht verfuegbar.", ephemeral=True
        )
        return

    profession_id = cog.resolve_profession_id(profession) if profession else None
    if profession and not profession_id:
        await interaction.response.send_message(
            f"Beruf **{profession}** ist unbekannt.", ephemeral=True
        )
        return
    profiles = await cog.data.list_professions(profession_id)
    if not profiles:
        await interaction.response.send_message(
            "Keine Crafting-Profile gefunden.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        "\n".join(
            _format_profession_line(profile, cog, include_user=True)
            for profile in profiles
        ),
        ephemeral=True,
    )


@crafting_group.command(name="recipes", description="Pflegt Spezialrezepte eines Chars")
@app_commands.describe(
    char="Name des geclaimten Charakters",
    profession="Optionaler Beruf-Filter",
    search="Optionaler Suchbegriff fuer Rezept oder Item",
)
@app_commands.autocomplete(
    char=claim_char_autocomplete,
    profession=recipes_profession_autocomplete,
)
async def crafting_recipes(
    interaction: discord.Interaction,
    char: str,
    profession: str | None = None,
    search: str | None = None,
):
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        await interaction.response.send_message(
            "WoW-System nicht verfuegbar.", ephemeral=True
        )
        return

    result = await cog.prepare_recipe_selection(
        interaction.user.id,
        char,
        profession,
        search,
        is_mod=_is_mod(interaction),
    )
    if result.status == "unknown_profession":
        await interaction.response.send_message(
            f"Beruf **{profession}** ist unbekannt.", ephemeral=True
        )
        return
    if result.status == "not_claimed":
        await interaction.response.send_message(
            f"**{char}** ist nicht geclaimed.", ephemeral=True
        )
        return
    if result.status == "forbidden":
        await interaction.response.send_message(
            f"Du darfst **{char}** nicht bearbeiten.", ephemeral=True
        )
        return
    if result.status == "no_professions":
        await interaction.response.send_message(
            f"Fuer **{char}** sind noch keine Berufe gepflegt.", ephemeral=True
        )
        return
    if result.status == "profession_not_set":
        await interaction.response.send_message(
            f"Fuer **{char}** ist dieser Beruf nicht gepflegt.", ephemeral=True
        )
        return
    if result.status == "choose_profession":
        view = CraftingProfessionSelectView(
            cog,
            interaction.user.id,
            result.claim,
            result.profiles,
            search,
        )
        await interaction.response.send_message(
            f"Bitte Beruf fuer **{result.claim.character_name}** auswaehlen.",
            view=view,
            ephemeral=True,
        )
        return

    recipes = result.recipes or []
    if not recipes:
        await interaction.response.send_message(
            f"Fuer **{result.claim.character_name}** gibt es aktuell keine "
            "offenen Spezialrezepte.",
            ephemeral=True,
        )
        return

    view = CraftingRecipeSelectionView(
        cog,
        interaction.user.id,
        result.claim,
        result.profile,
        recipes,
    )
    await interaction.response.send_message(
        view.content(),
        view=view,
        ephemeral=True,
    )


@crafting_group.command(
    name="import",
    description="Importiert Berufe/Rezepte aus dem LotusDiscordBridge-Addon-Code",
)
@app_commands.describe(
    code="Export-Code aus dem Addon (/ldx im Spiel, Button 'Exportieren')"
)
async def crafting_import(interaction: discord.Interaction, code: str):
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        await interaction.response.send_message(
            "WoW-System nicht verfuegbar.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    result = await cog.import_profession_export(
        interaction.user.id, code, is_mod=_is_mod(interaction)
    )

    if result.status == "invalid":
        await interaction.followup.send(
            "Der Code konnte nicht gelesen werden - bitte frisch per `/ldx` im "
            "Spiel exportieren und komplett kopieren.",
            ephemeral=True,
        )
        return
    if result.status == "not_claimed":
        await interaction.followup.send(
            f"**{result.character_name or '?'}** ist nicht geclaimed — erst "
            "`/wow claim` benutzen.",
            ephemeral=True,
        )
        return
    if result.status == "forbidden":
        await interaction.followup.send(
            f"**{result.claim.character_name}** gehoert nicht dir.", ephemeral=True
        )
        return
    if result.status == "no_professions":
        await interaction.followup.send(
            "Im Export waren keine Berufe enthalten.", ephemeral=True
        )
        return

    lines = [
        f"✅ **{result.character_name}**: {result.professions_imported} Beruf(e) "
        f"aktualisiert, {result.recipes_seen} Rezepte geprueft."
    ]
    if result.special_recipes_learned:
        lines.append(
            f"🌟 {result.special_recipes_learned} davon neu als Spezialrezept gewertet."
        )
    if result.unmatched:
        lines.append(
            f"⚠️ {len(result.unmatched)} Rezept(e) nicht in unserer Datenbank "
            "gefunden — siehe Bot-Logs fuer Details."
        )
    await interaction.followup.send("\n".join(lines), ephemeral=True)


@crafting_group.command(name="learned", description="Zeigt gepflegte Spezialrezepte")
@app_commands.describe(char="Name des geclaimten Charakters")
@app_commands.autocomplete(char=claim_char_autocomplete)
async def crafting_learned(interaction: discord.Interaction, char: str):
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        await interaction.response.send_message(
            "WoW-System nicht verfuegbar.", ephemeral=True
        )
        return

    result = await cog.known_recipes_for_character(
        interaction.user.id,
        char,
        is_mod=_is_mod(interaction),
    )
    if result.status == "not_claimed":
        await interaction.response.send_message(
            f"**{char}** ist nicht geclaimed.", ephemeral=True
        )
        return
    if result.status == "forbidden":
        await interaction.response.send_message(
            f"Du darfst **{char}** nicht ansehen.", ephemeral=True
        )
        return

    recipes = result.recipes or []
    if not recipes:
        await interaction.response.send_message(
            f"Fuer **{result.claim.character_name}** sind keine Spezialrezepte gepflegt.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        "\n".join(_format_known_recipe_line(recipe, cog) for recipe in recipes),
        ephemeral=True,
    )


@crafting_group.command(
    name="recipe-remove", description="Entfernt ein gepflegtes Spezialrezept"
)
@app_commands.describe(
    char="Name des geclaimten Charakters",
    recipe="Rezeptname oder gespeicherte Spell-ID",
)
@app_commands.autocomplete(
    char=claim_char_autocomplete, recipe=known_recipe_autocomplete
)
async def crafting_recipe_remove(
    interaction: discord.Interaction,
    char: str,
    recipe: str,
):
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        await interaction.response.send_message(
            "WoW-System nicht verfuegbar.", ephemeral=True
        )
        return

    result = await cog.remove_known_recipe(
        interaction.user.id,
        char,
        recipe,
        is_mod=_is_mod(interaction),
    )
    if result.status == "not_claimed":
        await interaction.response.send_message(
            f"**{char}** ist nicht geclaimed.", ephemeral=True
        )
        return
    if result.status == "forbidden":
        await interaction.response.send_message(
            f"Du darfst **{char}** nicht bearbeiten.", ephemeral=True
        )
        return
    if result.status == "recipe_not_found":
        await interaction.response.send_message(
            f"**{recipe}** ist fuer **{char}** nicht gepflegt.", ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"Spezialrezept fuer **{result.claim.character_name}** entfernt.",
        ephemeral=True,
    )


@crafting_group.command(name="search", description="Sucht Crafter fuer ein Item")
@app_commands.describe(item="Deutscher oder englischer Itemname")
async def crafting_search(interaction: discord.Interaction, item: str):
    cog: WoWCog | None = interaction.client.get_cog("WoWCog")
    if cog is None:
        await interaction.response.send_message(
            "WoW-System nicht verfuegbar.", ephemeral=True
        )
        return

    result = await cog.search_crafting(item)
    view = cog.crafting_search_response_view(result, interaction.user.id)
    content = cog.format_crafting_search_result(result)
    if view is not None:
        await interaction.response.send_message(content, view=view, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)
