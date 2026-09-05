"""Marktplatz-Forum: allgemeine Angebote/Gesuche + smarte Crafting-Gesuche.

Konzept
-------
* Ein angepinnter Hub-Post mit Buttons (Crafting-Gesuch erstellen / Sonstiges
  anbieten-suchen / Hilfe).
* "Sonstiges" ist bewusst leichtgewichtig: Biete/Suche wählen, kurzes
  Formular ausfüllen, fertig ist ein normaler Forum-Post mit einem
  "Als erledigt markieren"-Button. Keine Matching-Logik.
* "Crafting-Gesuch" nutzt die bestehende Crafting-Suche von WoWCog
  (search_crafting/search_crafting_by_item_id) wieder, erstellt einen
  Forum-Post und pingt automatisch jeden bekannten Crafter direkt im Thread
  — die automatisierte Version von "im Gildenchat fragen, wer X kann".

Kopplung
--------
Liest Claims/Crafting-Daten über ``WoWCog.data`` via desselben
get_cog+hasattr-Musters, das ``DuoCog`` für die Kopplung an ``WoWCog``
verwendet — keine Import-Zeit-Kopplung.

Bewusst NICHT verändert: ``WoWCog.search_crafting``/``search_crafting_by_item_id``,
``WoWData.find_crafters``/``find_crafters_with_known_recipe`` — die bestehende
``/wow crafting search`` bleibt exakt wie sie ist. Der Crafting-Notify-Filter
(Panel-Toggle) wird ausschließlich hier, beim Ping, angewendet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Awaitable, Callable

import discord
from discord.ext import commands

from lotus_bot.log_setup import get_logger
from lotus_bot.utils.managed_cog import ManagedTaskCog

from .marketplace_data import MarketplaceData

if TYPE_CHECKING:  # pragma: no cover
    from .cog import CharacterProfession, CraftingSearchResult, WoWCog

logger = get_logger(__name__)

MARKETPLACE_FORUM_CHANNEL_ID = 1545796864019013792

# Forum tag names the bot manages. Created on startup if missing (needs
# Manage-Channels); everything degrades gracefully to "no tag" if it can't.
TAG_BIETE = "🛒 Biete"
TAG_SUCHE = "🔍 Suche"
TAG_CRAFTING_GESUCH = "🛠️ Crafting-Gesuch"
TAG_ERLEDIGT = "✅ Erledigt"

# How long a requester must wait before the SAME item auto-pings the SAME
# crafters again. Gates only the ping, never the thread creation itself.
PING_COOLDOWN_MINUTES = 30

HUB_TEXT = (
    "# 🛒 Marktplatz\n"
    "Biete oder suche Ausrüstung, Reagenzien, Dienstleistungen — was auch "
    "immer. Für Crafting-Gesuche (Items **und** Verzauberungen) gibt's "
    "extra Unterstützung: der Bot sucht automatisch passende Crafter in der "
    "Gilde und pingt sie direkt hier im Thread.\n\n"
    "**So geht's:**\n"
    "1. **🛠️ Crafting-Gesuch erstellen** — Item oder Verzauberung eingeben, "
    "der Bot findet passende Crafter und pingt sie.\n"
    "2. **📝 Sonstiges anbieten/suchen** — für alles andere.\n\n"
    "Nur für Black-Lotus-Member. 🪷"
)
HUB_HELP_TEXT = (
    "**❓ Hilfe – Marktplatz**\n\n"
    "**🛠️ Crafting-Gesuch erstellen** — gib das Item oder die Verzauberung "
    'ein, die du brauchst (z.B. "Wuttrank" oder "Kreuzritter"). Der Bot '
    "sucht in der Gilde nach bekannten Craftern (Beruf + Skill oder "
    "gepflegtes Spezialrezept) und pingt sie automatisch im neuen Thread. "
    "Auch ohne bekannten Crafter wird der Thread erstellt — vielleicht "
    "meldet sich trotzdem wer. Wer zuerst auf **✅ Ich übernehme das** "
    "klickt, hat die Anfrage übernommen.\n\n"
    "**📝 Sonstiges anbieten/suchen** — für alles, was kein Crafting ist: "
    "Titel + kurze Beschreibung, fertig. **✅ Als erledigt markieren** "
    "schließt deinen Post wieder.\n\n"
    "Deine eigene Pingbarkeit für Crafting-Anfragen kannst du im "
    "🪷-Panel unter **Deine Chars** je Char an-/ausschalten."
)


class MarketplaceCog(ManagedTaskCog):
    """Marktplatz-Forum: allgemeine Angebote/Gesuche + smarte Crafting-Gesuche."""

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot
        self.data = MarketplaceData("data/pers/wow/marketplace.db")
        self.forum_channel_id = MARKETPLACE_FORUM_CHANNEL_ID
        self._tags: dict[str, discord.ForumTag] = {}
        self.create_task(self._startup())
        if hasattr(self.bot, "add_view"):
            self.bot.add_view(MarketplaceHubView(self))
            self.bot.add_view(MarketplaceListingPostView(self))
            self.bot.add_view(MarketplaceCraftingPostView(self))

    # ---- infrastructure ----

    @property
    def wow(self) -> "WoWCog | None":
        get_cog = getattr(self.bot, "get_cog", None)
        return get_cog("WoWCog") if get_cog else None

    def forum(self) -> discord.ForumChannel | None:
        channel = self.bot.get_channel(self.forum_channel_id)
        return channel if isinstance(channel, discord.ForumChannel) else None

    async def _startup(self) -> None:
        await self.bot.wait_until_ready()
        try:
            await self.publish_hub()
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(
                "[MarketplaceCog] Hub-Publish beim Start fehlgeschlagen: %s", exc
            )

    async def _ensure_tags(self, forum: discord.ForumChannel) -> None:
        """Resolve (and create if missing) the managed forum tags."""
        wanted = [TAG_BIETE, TAG_SUCHE, TAG_CRAFTING_GESUCH, TAG_ERLEDIGT]
        by_name = {tag.name: tag for tag in forum.available_tags}
        self._tags = {name: by_name[name] for name in wanted if name in by_name}
        missing = [name for name in wanted if name not in by_name]
        for name in missing:
            try:
                tag = await forum.create_tag(name=name)
                self._tags[name] = tag
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.info(
                    "[MarketplaceCog] Forum-Tag '%s' nicht anlegbar: %s", name, exc
                )

    def _tag(self, name: str) -> list[discord.ForumTag]:
        tag = self._tags.get(name)
        return [tag] if tag else []

    async def publish_hub(self) -> None:
        """Create or refresh the pinned hub post in the forum."""
        forum = self.forum()
        if forum is None:
            logger.warning(
                "[MarketplaceCog] Forum-Channel %s nicht gefunden.",
                self.forum_channel_id,
            )
            return
        await self._ensure_tags(forum)
        view = MarketplaceHubView(self)
        hub_id = await self.data.get_setting("hub_thread_id")
        if hub_id:
            try:
                thread = self.bot.get_channel(
                    int(hub_id)
                ) or await self.bot.fetch_channel(int(hub_id))
                if isinstance(thread, discord.Thread):
                    await thread.get_partial_message(int(hub_id)).edit(
                        content=HUB_TEXT, view=view
                    )
                    await self._try_pin(thread)
                    return
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.info(
                    "[MarketplaceCog] Bestehender Hub-Post nicht editierbar — neu."
                )
        created = await forum.create_thread(
            name="🛒 Marktplatz – Start hier", content=HUB_TEXT
        )
        # create_thread can't attach a view directly (discord.py 2.7); the
        # persistent buttons go onto the starter message afterwards.
        await created.message.edit(view=view)
        await self.data.set_setting("hub_thread_id", str(created.thread.id))
        await self._try_pin(created.thread)

    async def _try_pin(self, thread: discord.Thread) -> None:
        try:
            await thread.edit(pinned=True)
        except (discord.Forbidden, discord.HTTPException, TypeError):
            pass

    # ---- hub entry points ----

    async def open_crafting_request(self, interaction: discord.Interaction) -> None:
        wow = self.wow
        claims = await wow.data.claims_for_user(interaction.user.id) if wow else []
        if not claims:
            await interaction.response.send_message(
                "Du brauchst mindestens einen geclaimten Char, um hier "
                "mitzumachen. Nutze zuerst `/wow claim`.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(MarketplaceCraftingItemModal(self))

    async def open_generic_listing(self, interaction: discord.Interaction) -> None:
        wow = self.wow
        claims = await wow.data.claims_for_user(interaction.user.id) if wow else []
        if not claims:
            await interaction.response.send_message(
                "Du brauchst mindestens einen geclaimten Char, um hier "
                "mitzumachen. Nutze zuerst `/wow claim`.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Was möchtest du posten?",
            view=MarketplaceKindChooseView(self),
            ephemeral=True,
        )

    async def open_help(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(HUB_HELP_TEXT, ephemeral=True)

    # ---- generic listing flow ----

    async def choose_character_for_listing(
        self,
        interaction: discord.Interaction,
        kind: str,
        title: str,
        description: str,
    ) -> None:
        """After the title/description modal: pick WHICH claimed char this
        post is about (skipped automatically if the user only has one) —
        the thread then shows e.g. "Angebot von Voidok (Twink von Lyxendra)"
        instead of an anonymous Discord mention, and picking one character
        also gates out anyone without a single claimed char at all."""
        wow = self.wow
        claims = await wow.data.claims_for_user(interaction.user.id) if wow else []
        if not claims:
            await interaction.response.send_message(
                "Du brauchst mindestens einen geclaimten Char, um hier "
                "mitzumachen. Nutze zuerst `/wow claim`.",
                ephemeral=True,
            )
            return
        if len(claims) == 1:
            await self.publish_listing(
                interaction,
                kind,
                title,
                description,
                claims[0].character_key,
                edit=False,
            )
            return

        async def on_selected(
            picker_interaction: discord.Interaction, character_key: str
        ) -> None:
            await self.publish_listing(
                picker_interaction, kind, title, description, character_key, edit=True
            )

        choices = [(c.character_key, c.character_name) for c in claims]
        view = MarketplaceCharacterSelectView(interaction.user.id, choices, on_selected)
        await interaction.response.send_message(
            "Welcher Char bietet/sucht das?", view=view, ephemeral=True
        )

    async def publish_listing(
        self,
        interaction: discord.Interaction,
        kind: str,
        title: str,
        description: str,
        character_key: str,
        *,
        edit: bool,
    ) -> None:
        """``edit=True`` when called from the character-picker's own select
        callback (edits that ephemeral message in place); ``edit=False``
        when called directly off the title/description modal's own
        interaction (that modal's "origin message" is the shared hub post,
        so it must get a brand-new ephemeral response, not an edit)."""
        forum = self.forum()
        if forum is None:
            msg = "❌ Marktplatz aktuell nicht verfügbar."
            if edit:
                await interaction.response.edit_message(content=msg, view=None)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return
        wow = self.wow
        label = (
            await wow.character_display_label(character_key)
            if wow is not None
            else f"<@{interaction.user.id}>"
        )
        prefix = "🛒 " if kind == "biete" else "🔍 "
        thread_name = (prefix + title.strip())[:100]
        kind_label = "Angebot" if kind == "biete" else "Gesuch"
        body_lines = [description.strip()] if description.strip() else []
        body_lines.append(f"\n{kind_label} von <@{interaction.user.id}> – {label}")
        tag_name = TAG_BIETE if kind == "biete" else TAG_SUCHE

        if edit:
            await interaction.response.edit_message(
                content="⏳ Erstelle Marktplatz-Post ...", view=None
            )
        else:
            await interaction.response.defer(ephemeral=True)
        created = await forum.create_thread(
            name=thread_name,
            content="\n".join(body_lines),
            applied_tags=self._tag(tag_name),
        )
        await created.message.edit(view=MarketplaceListingPostView(self))
        await self.data.create_listing(
            created.thread.id, interaction.user.id, kind, character_key
        )
        reply = f"✅ Veröffentlicht: {created.thread.mention}"
        if edit:
            await interaction.edit_original_response(content=reply)
        else:
            await interaction.followup.send(reply, ephemeral=True)

    async def mark_listing_done(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        listing = (
            await self.data.get_listing(channel.id)
            if isinstance(channel, discord.Thread)
            else None
        )
        if listing is None:
            await interaction.response.send_message(
                "Dieser Post ist kein aktiver Marktplatz-Eintrag.", ephemeral=True
            )
            return
        if listing.poster_discord_user_id != interaction.user.id:
            await interaction.response.send_message(
                "Nur der Ersteller kann diesen Post als erledigt markieren.",
                ephemeral=True,
            )
            return
        await self.data.set_listing_status(channel.id, "done")
        await interaction.response.send_message(
            "✅ Als erledigt markiert.", ephemeral=True
        )
        try:
            await channel.edit(applied_tags=self._tag(TAG_ERLEDIGT), archived=True)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.info(
                "[MarketplaceCog] Listing-Thread %s archivieren fehlgeschlagen: %s",
                channel.id,
                exc,
            )

    # ---- crafting-gesuch flow ----

    async def handle_crafting_item_search(
        self, interaction: discord.Interaction, item_name: str, note: str | None
    ) -> None:
        wow = self.wow
        if wow is None:
            await interaction.response.send_message(
                "❌ System aktuell nicht verfügbar.", ephemeral=True
            )
            return
        result = await wow.search_crafting(item_name)
        await self._route_search_result(interaction, result, note, edit=False)

    async def continue_crafting_request_by_item_id(
        self, interaction: discord.Interaction, item_id: str, note: str | None
    ) -> None:
        wow = self.wow
        if wow is None:
            await interaction.response.edit_message(
                content="❌ System aktuell nicht verfügbar.", view=None
            )
            return
        result = await wow.search_crafting_by_item_id(item_id)
        await self._route_search_result(interaction, result, note, edit=True)

    async def begin_crafting_request_from_result(
        self,
        interaction: discord.Interaction,
        result: "CraftingSearchResult",
        note: str | None,
    ) -> None:
        """Entry point from the EXISTING /wow crafting search's own result
        (via the "Marktplatz-Anfrage erstellen" button) - the item is
        already resolved, so this skips straight to picking the requesting
        character. Only ever called for an already-"ok"/"no_crafter"/
        "manual_recipe" result (see WoWCog.crafting_search_response_view)."""
        wow = self.wow
        if wow is None:
            await interaction.response.edit_message(
                content="❌ System aktuell nicht verfügbar.", view=None
            )
            return
        # edit=True: the button's own interaction originates from the
        # existing ephemeral search-result message (a private message
        # already owned by this user) - edit it in place rather than
        # stacking a new ephemeral message on top of it.
        await self._pick_requesting_character_and_finish(
            interaction, result, note, wow, edit=True
        )

    async def _route_search_result(
        self,
        interaction: discord.Interaction,
        result: "CraftingSearchResult",
        note: str | None,
        *,
        edit: bool,
    ) -> None:
        if result.status == "item_not_found":
            content = "Dieses Item wurde in den WoW-Daten nicht gefunden."
            if edit:
                await interaction.response.edit_message(content=content, view=None)
            else:
                await interaction.response.send_message(content, ephemeral=True)
            return
        if result.status == "ambiguous_item":
            view = MarketplaceCraftingSuggestionView(
                self, interaction.user.id, result.candidates or [], note
            )
            content = "Mehrere Items gefunden — bitte aus dem Menü wählen."
            if edit:
                await interaction.response.edit_message(content=content, view=view)
            else:
                await interaction.response.send_message(
                    content, view=view, ephemeral=True
                )
            return
        if result.status == "recipe_not_found":
            content = (
                "Für dieses Item wurde kein Crafting-Rezept gefunden — "
                "kein Marktplatz-Post möglich."
            )
            if edit:
                await interaction.response.edit_message(content=content, view=None)
            else:
                await interaction.response.send_message(content, ephemeral=True)
            return

        # "ok" / "no_crafter" / "manual_recipe" all create the post. The
        # latter two mean "nobody KNOWN can do it right now" - exactly the
        # case this feature exists for (maybe someone unlisted steps up).
        wow = self.wow
        if wow is None:  # pragma: no cover - defensive, checked by callers already
            return
        await self._pick_requesting_character_and_finish(
            interaction, result, note, wow, edit=edit
        )

    async def _pick_requesting_character_and_finish(
        self,
        interaction: discord.Interaction,
        result: "CraftingSearchResult",
        note: str | None,
        wow: "WoWCog",
        *,
        edit: bool,
    ) -> None:
        """Which claimed char needs this, before actually creating the post
        — skipped automatically if the user only has one claim. Also gates
        out anyone without a single claimed char at all."""
        claims = await wow.data.claims_for_user(interaction.user.id)
        if not claims:
            msg = (
                "Du brauchst mindestens einen geclaimten Char, um hier "
                "mitzumachen. Nutze zuerst `/wow claim`."
            )
            if edit:
                await interaction.response.edit_message(content=msg, view=None)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return
        if len(claims) == 1:
            if edit:
                await interaction.response.edit_message(
                    content="⏳ Erstelle Marktplatz-Post ...", view=None
                )
            else:
                await interaction.response.defer(ephemeral=True)
            await self._finish_crafting_request(
                interaction, result, note, wow, claims[0].character_key, edit=edit
            )
            return

        async def on_selected(
            picker_interaction: discord.Interaction, character_key: str
        ) -> None:
            await picker_interaction.response.edit_message(
                content="⏳ Erstelle Marktplatz-Post ...", view=None
            )
            await self._finish_crafting_request(
                picker_interaction, result, note, wow, character_key, edit=True
            )

        choices = [(c.character_key, c.character_name) for c in claims]
        view = MarketplaceCharacterSelectView(interaction.user.id, choices, on_selected)
        content = "Welcher Char sucht das?"
        if edit:
            await interaction.response.edit_message(content=content, view=view)
        else:
            await interaction.response.send_message(content, view=view, ephemeral=True)

    async def _finish_crafting_request(
        self,
        interaction: discord.Interaction,
        result: "CraftingSearchResult",
        note: str | None,
        wow: "WoWCog",
        character_key: str,
        *,
        edit: bool,
    ) -> None:
        forum = self.forum()
        if forum is None:
            msg = "❌ Marktplatz aktuell nicht verfügbar."
            if edit:
                await interaction.edit_original_response(content=msg)
            else:
                await interaction.followup.send(msg, ephemeral=True)
            return

        item_name = wow._localized_text((result.item or {}).get("name")) or "?"
        item_key = self._item_key_for_result(result)
        label = await wow.character_display_label(character_key)
        practitioner = wow.profession_practitioner_title(result.profession_id)
        thread_name = (
            f"🛠️ {practitioner} gesucht für {item_name}"
            if practitioner
            else f"🛠️ {item_name} gesucht"
        )[:100]
        body_lines = [f"**Item:** {item_name}"]
        if note:
            body_lines.append(f"**Notiz:** {note}")
        body_lines.append(f"\nGesucht von <@{interaction.user.id}> – {label}")

        created = await forum.create_thread(
            name=thread_name,
            content="\n".join(body_lines),
            applied_tags=self._tag(TAG_CRAFTING_GESUCH),
        )
        await created.message.edit(view=MarketplaceCraftingPostView(self))
        await self.data.create_crafting_request(
            created.thread.id,
            interaction.user.id,
            item_name,
            item_key,
            character_key,
        )

        crafters = await self._filter_notify_opt_in(result.crafters or [])
        if crafters:
            can_ping = await self.data.check_and_record_ping_cooldown(
                interaction.user.id, item_key, cooldown_minutes=PING_COOLDOWN_MINUTES
            )
            if can_ping:
                mentions = " ".join(f"<@{c.discord_user_id}>" for c in crafters)
                await created.thread.send(f"👀 {mentions} — kannst du das herstellen?")
            else:
                await created.thread.send(
                    "ℹ️ Kürzlich schon für dieses Item angefragt — kein erneuter "
                    "Ping, der Thread bleibt aber offen."
                )

        reply = f"✅ Dein Crafting-Gesuch ist online: {created.thread.mention}"
        if edit:
            await interaction.edit_original_response(content=reply)
        else:
            await interaction.followup.send(reply, ephemeral=True)

    async def handle_claim(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        request = (
            await self.data.get_crafting_request(channel.id)
            if isinstance(channel, discord.Thread)
            else None
        )
        if request is None:
            await interaction.response.send_message(
                "Diese Anfrage ist nicht mehr aktiv.", ephemeral=True
            )
            return
        if request.requester_discord_user_id == interaction.user.id:
            await interaction.response.send_message(
                "Das ist deine eigene Anfrage. 🙂", ephemeral=True
            )
            return
        won = await self.data.claim_crafting_request(channel.id, interaction.user.id)
        if not won:
            await interaction.response.send_message(
                "Schon vergeben — ein anderer Crafter war schneller.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"✅ Du übernimmst **{request.item_name}**!", ephemeral=True
        )
        try:
            await channel.send(
                f"✅ <@{interaction.user.id}> übernimmt **{request.item_name}** für "
                f"<@{request.requester_discord_user_id}>!"
            )
            await channel.edit(applied_tags=self._tag(TAG_ERLEDIGT))
            starter = channel.starter_message or await channel.fetch_message(channel.id)
            disabled_view = MarketplaceCraftingPostView(self)
            for item in disabled_view.children:
                item.disabled = True
            await starter.edit(view=disabled_view)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.info("[MarketplaceCog] Claim-Folgeaktionen fehlgeschlagen: %s", exc)

    async def _filter_notify_opt_in(
        self, crafters: list["CharacterProfession"]
    ) -> list["CharacterProfession"]:
        """Excludes anyone who opted out via the panel's crafting-notify
        toggle. Deliberately the ONLY place this flag is checked — the
        existing /wow crafting search (find_crafters /
        find_crafters_with_known_recipe) stays completely unaffected."""
        wow = self.wow
        if wow is None or not crafters:
            return crafters
        notify_map = await wow.data.crafting_notify_map(
            [c.character_key for c in crafters]
        )
        return [c for c in crafters if notify_map.get(c.character_key, True)]

    @staticmethod
    def _item_key_for_result(result: "CraftingSearchResult") -> str:
        if result.item and result.item.get("id"):
            return str(result.item["id"])
        if result.recipe and result.recipe.get("spell_id"):
            return f"enchant:{result.recipe['spell_id']}"
        return "unknown"


# --------------------------------------------------------------------------- #
#                                   Views                                      #
# --------------------------------------------------------------------------- #


class MarketplaceCharacterSelect(discord.ui.Select):
    def __init__(self, parent: "MarketplaceCharacterSelectView") -> None:
        self.parent_view = parent
        options = [
            discord.SelectOption(label=label[:100], value=key)
            for key, label in parent.choices[:25]
        ]
        super().__init__(
            placeholder="Char auswählen", min_values=1, max_values=1, options=options
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.on_selected(interaction, self.values[0])


class MarketplaceCharacterSelectView(discord.ui.View):
    """Reusable 'which of my claimed chars' picker, shared by the generic
    listing flow and the crafting-gesuch flow. ``on_selected`` is an async
    callable ``(interaction, character_key) -> None`` supplied by the caller
    - each flow closes over whatever else it still needs (title/description,
    or the resolved search result) rather than this view knowing about it."""

    def __init__(
        self,
        owner_user_id: int,
        choices: list[tuple[str, str]],
        on_selected: Callable[[discord.Interaction, str], Awaitable[None]],
    ) -> None:
        super().__init__(timeout=180)
        self.owner_user_id = owner_user_id
        self.choices = choices
        self.on_selected = on_selected
        self.add_item(MarketplaceCharacterSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_user_id:
            return True
        await interaction.response.send_message(
            "Diese Auswahl gehört nicht dir.", ephemeral=True
        )
        return False


class MarketplaceHubView(discord.ui.View):
    """Persistent buttons on the pinned hub post."""

    def __init__(self, cog: MarketplaceCog) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Crafting-Gesuch erstellen",
        style=discord.ButtonStyle.success,
        emoji="🛠️",
        custom_id="market_hub:crafting",
    )
    async def crafting(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self.cog.open_crafting_request(interaction)

    @discord.ui.button(
        label="Sonstiges anbieten/suchen",
        style=discord.ButtonStyle.secondary,
        emoji="📝",
        custom_id="market_hub:listing",
    )
    async def listing(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self.cog.open_generic_listing(interaction)

    @discord.ui.button(
        label="Hilfe",
        style=discord.ButtonStyle.secondary,
        custom_id="market_hub:help",
    )
    async def help(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self.cog.open_help(interaction)


class MarketplaceKindChooseView(discord.ui.View):
    """Ephemeral 'Biete oder Suche' pre-step before the listing modal."""

    def __init__(self, cog: MarketplaceCog) -> None:
        super().__init__(timeout=120)
        self.cog = cog

    @discord.ui.button(label="Biete", style=discord.ButtonStyle.success, emoji="🛒")
    async def biete(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            MarketplaceListingModal(self.cog, "biete")
        )

    @discord.ui.button(label="Suche", style=discord.ButtonStyle.primary, emoji="🔍")
    async def suche(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            MarketplaceListingModal(self.cog, "suche")
        )


class MarketplaceListingModal(discord.ui.Modal):
    def __init__(self, cog: MarketplaceCog, kind: str) -> None:
        super().__init__(
            title="Angebot erstellen" if kind == "biete" else "Gesuch erstellen"
        )
        self.cog = cog
        self.kind = kind
        self.title_input = discord.ui.TextInput(label="Titel", max_length=80)
        self.description_input = discord.ui.TextInput(
            label="Beschreibung (optional)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1000,
        )
        self.add_item(self.title_input)
        self.add_item(self.description_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.choose_character_for_listing(
            interaction,
            self.kind,
            str(self.title_input.value),
            str(self.description_input.value),
        )


class MarketplaceListingPostView(discord.ui.View):
    """Persistent 'Als erledigt markieren' button on a generic listing post."""

    def __init__(self, cog: MarketplaceCog) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Als erledigt markieren",
        style=discord.ButtonStyle.secondary,
        emoji="✅",
        custom_id="market_listing:done",
    )
    async def done(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self.cog.mark_listing_done(interaction)


class MarketplaceCraftingItemModal(discord.ui.Modal):
    def __init__(self, cog: MarketplaceCog) -> None:
        super().__init__(title="Crafting-Gesuch erstellen")
        self.cog = cog
        self.item = discord.ui.TextInput(
            label="Item oder Verzauberung?",
            placeholder="z. B. Wuttrank, Kreuzritter ...",
            min_length=2,
            max_length=80,
        )
        self.note = discord.ui.TextInput(
            label="Notiz (optional)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=200,
            placeholder="z.B. Material bringe ich mit",
        )
        self.add_item(self.item)
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        note = str(self.note.value).strip() or None
        await self.cog.handle_crafting_item_search(
            interaction, str(self.item.value).strip(), note
        )


class MarketplaceCraftingSuggestionSelect(discord.ui.Select):
    def __init__(self, parent: "MarketplaceCraftingSuggestionView") -> None:
        self.parent_view = parent
        wow = parent.cog.wow
        options = []
        for item in parent.items[:25]:
            label = str(item.get("id"))
            description = ""
            if wow is not None:
                label = wow._localized_text(item.get("name"), "de")[:100] or label
                description = wow._localized_text(item.get("name"), "en")[:100]
            options.append(
                discord.SelectOption(
                    label=label, value=str(item.get("id")), description=description
                )
            )
        super().__init__(
            placeholder="Item auswählen", min_values=1, max_values=1, options=options
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.cog.continue_crafting_request_by_item_id(
            interaction, self.values[0], self.parent_view.note
        )


class MarketplaceCraftingSuggestionView(discord.ui.View):
    def __init__(
        self,
        cog: MarketplaceCog,
        owner_user_id: int,
        items: list[dict],
        note: str | None,
    ) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.owner_user_id = owner_user_id
        self.items = items
        self.note = note
        self.add_item(MarketplaceCraftingSuggestionSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_user_id:
            return True
        await interaction.response.send_message(
            "Diese Auswahl gehört nicht dir.", ephemeral=True
        )
        return False


class MarketplaceCraftingPostView(discord.ui.View):
    """Persistent 'Ich übernehme das' claim button on a crafting-gesuch post."""

    def __init__(self, cog: MarketplaceCog) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Ich übernehme das",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="market_crafting:claim",
    )
    async def claim(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self.cog.handle_claim(interaction)
