import discord
from discord import app_commands

from lotus_bot.log_setup import get_logger
from lotus_bot.permissions import moderator_only

logger = get_logger(__name__)

marketplace_group = app_commands.Group(
    name="marketplace",
    description="Marktplatz – Angebote, Gesuche & Crafting",
)


@marketplace_group.command(
    name="publish",
    description="Veröffentlicht/aktualisiert den Marktplatz-Hub (nur Mods)",
)
@moderator_only()
@app_commands.default_permissions(manage_guild=True)
async def marketplace_publish(interaction: discord.Interaction):
    logger.info(f"/marketplace publish by {interaction.user}")
    cog = interaction.client.get_cog("MarketplaceCog")
    if cog is None:
        await interaction.response.send_message(
            "❌ Marktplatz-System nicht verfügbar.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        await cog.publish_hub()
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(
            "[MarketplaceCommands] Hub-Publish fehlgeschlagen: %s", exc, exc_info=True
        )
        await interaction.followup.send("❌ Hub-Publish fehlgeschlagen.")
        return
    await interaction.followup.send("✅ Marktplatz-Hub veröffentlicht/aktualisiert.")
