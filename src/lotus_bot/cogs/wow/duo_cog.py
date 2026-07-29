"""WoW-Duo: Level-Partner-Matchmaking im Forum-Channel ``wow-duo``.

Konzept
-------
* Ein angepinnter Hub-Post mit Buttons (Partner suchen / Mein Status / Hilfe).
* Wer sucht, bekommt einen **öffentlichen Forum-Post** ("🔍 … sucht
  Level-Partner") mit einem *Zusammen leveln*-Button — das Schwarze Brett.
* Klickt jemand den Button, geht eine Anfrage an den Sucher; nimmt der an,
  entsteht ein **Team-Post** ("🤝 Team Phoenix"), beide werden hinzugefügt und
  die beiden Such-Posts verschwinden.
* Der Team-Post ist die gemeinsame Reise: automatische Level-Meilenstein-Posts,
  Champion-Bonus wenn beide einen Meilenstein erreichen, und — Hardcore — ein
  Memorial wenn ein Partner fällt, mit der Option weiterzumachen oder das Team
  ehrenvoll aufzulösen.

Kopplung
--------
Der Cog liest Claims/Roster über ``WoWCog.data`` und wird von ``WoWCog`` bei
Tod/Meilenstein über :meth:`on_character_death` / :meth:`on_character_milestone`
aufgerufen — dasselbe ``get_cog``-Muster, mit dem ``WoWCog`` den ``ChampionCog``
anspricht.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from lotus_bot.log_setup import get_logger
from lotus_bot.utils.managed_cog import ManagedTaskCog

from .cog import CLASS_NAMES_DE, HORDE_RED, RACE_NAMES_DE
from .duo_data import DuoData, DuoSignup, DuoTeam
from .duo_logic import (
    INTENSITY,
    PLAY_TAGS,
    SELF_FOUND_TAG,
    TIME_WINDOWS,
    decode_prefs,
    decode_windows,
    encode_prefs,
    encode_windows,
    format_prefs,
    format_windows,
    intensity_label,
    overlap_keys,
    pick_team_name,
    rank_candidates,
)

if TYPE_CHECKING:  # pragma: no cover
    from .cog import WoWCog

logger = get_logger(__name__)

DUO_FORUM_CHANNEL_ID = 1526376290129678406

# Forum tag names the bot manages. Created on startup if missing (needs
# Manage-Channels); everything degrades gracefully to "no tag" if it can't.
TAG_SEARCHING = "🔍 Sucht Partner"
TAG_ACTIVE = "🟢 Aktiv"
TAG_MOURNING = "🕯️ In Trauer"
TAG_DISBANDED = "🕯️ Aufgelöst"

# Bonus Champion points to BOTH partners when the team reaches a milestone
# together. On top of the solo milestone points the WoW cog already awards.
DUO_MILESTONE_BONUS = {30: 3, 40: 6, 50: 12, 60: 30}

# Sentinel char-select value for "let's roll a brand-new char together".
REROLL_VALUE = "__new__"
# Open searches auto-expire after this many days so the board stays credible.
SEARCH_STALE_DAYS = 14

HUB_TEXT = (
    "# 🤝 Zusammen leveln\n"
    "Niemand muss allein leveln. Such dir einen Partner — mehr Spaß, viel "
    "sicherer, und ihr schreibt eure Hardcore-Geschichte gemeinsam.\n\n"
    "**So geht's:**\n"
    "1. **Partner suchen** — Char (oder neu rollen) + Zeiten + Pensum wählen. "
    "Dein Suchpost erscheint hier im Forum.\n"
    "2. Findest du unten jemand Passenden, klick **🤝 Zusammen leveln** in "
    "seinem Post.\n"
    "3. Nimmt er an, macht der Bot euch ein **Team** mit eigenem Thread — "
    "Termine, Screenshots, eure Reise.\n\n"
    "Nur für Black-Lotus-Member. Horde, deutsch, Hardcore. 🪷"
)
HUB_HELP_TEXT = (
    "**❓ Hilfe – Zusammen leveln**\n\n"
    "**Partner suchen** — wähle einen Char (oder **✨ Neuer Char**, um mit "
    "jemandem zusammen frisch zu rollen), deine Spielzeiten und dein **Pensum** "
    "(1–2 h / 2–4 h / 5 h+). Optional: **Self-Found** & Vorlieben, plus eine "
    "kurze Notiz. Der Bot stellt dich aufs Board und pingt passende Sucher.\n\n"
    "**Zusammen leveln** — in einem fremden Suchpost startest du damit eine "
    "Anfrage. Der andere muss zustimmen, dann entsteht euer Team.\n\n"
    "**Mein Status** — zeigt alle deine Teams und offenen Suchen. Pro Char "
    "geht ein Partner — du kannst also mit mehreren Twinks parallel suchen. "
    "Eine Suche ziehst du im Such-Post zurück, ein Team löst/benennst du im "
    "Team-Post.\n\n"
    "**Im Team** — der Bot postet automatisch, wenn ihr Meilensteine "
    "erreicht (Level 30/40/50/60) und vergibt Bonus-Champion-Punkte, wenn "
    "**beide** dort ankommen. Fällt ein Partner, bekommt ihr ein Memorial "
    "und könnt entscheiden, ob ihr weitermacht.\n\n"
    "**Meinen Char wechseln** — im Team-Thread, falls du rerollst oder nach "
    "einem Tod mit einem neuen Char weiterziehst."
)


def _class_name(class_id: int | None) -> str:
    return CLASS_NAMES_DE.get(class_id or 0, "?")


def _race_name(race_id: int | None) -> str:
    return RACE_NAMES_DE.get(race_id or 0, "")


class DuoCog(ManagedTaskCog):
    """Level-Partner-Matchmaking für Black Lotus."""

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot
        self.data = DuoData("data/pers/wow/duo.db")
        self.forum_channel_id = DUO_FORUM_CHANNEL_ID
        self._tags: dict[str, discord.ForumTag] = {}
        self.create_task(self._startup())
        if hasattr(self.bot, "add_view"):
            self.bot.add_view(DuoHubView(self))
            self.bot.add_view(DuoSearchPostView(self))
            self.bot.add_view(DuoTeamPostView(self))

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
            logger.error("[DuoCog] Hub-Publish beim Start fehlgeschlagen: %s", exc)

    async def _ensure_tags(self, forum: discord.ForumChannel) -> None:
        """Resolve (and create if missing) the managed forum tags."""
        wanted = [TAG_SEARCHING, TAG_ACTIVE, TAG_MOURNING, TAG_DISBANDED]
        by_name = {tag.name: tag for tag in forum.available_tags}
        self._tags = {name: by_name[name] for name in wanted if name in by_name}
        missing = [name for name in wanted if name not in by_name]
        for name in missing:
            try:
                tag = await forum.create_tag(name=name)
                self._tags[name] = tag
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.info("[DuoCog] Forum-Tag '%s' nicht anlegbar: %s", name, exc)

    def _tag(self, name: str) -> list[discord.ForumTag]:
        tag = self._tags.get(name)
        return [tag] if tag else []

    async def publish_hub(self) -> None:
        """Create or refresh the pinned hub post in the forum."""
        forum = self.forum()
        if forum is None:
            logger.warning(
                "[DuoCog] Forum-Channel %s nicht gefunden.", self.forum_channel_id
            )
            return
        await self._ensure_tags(forum)
        view = DuoHubView(self)
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
                logger.info("[DuoCog] Bestehender Hub-Post nicht editierbar — neu.")
        created = await forum.create_thread(
            name="📖 Zusammen leveln – Start hier", content=HUB_TEXT
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

    # ---- character helpers ----

    async def _living_claim_chars(
        self, user_id: int
    ) -> list[tuple[str, str, int, int | None]]:
        """Return ``(character_key, name, level, class_id)`` for the user's
        currently-alive, in-roster claimed characters."""
        wow = self.wow
        if wow is None:
            return []
        claims = await wow.data.claims_for_user(user_id)
        snapshot = await wow.data.get_snapshot()
        result: list[tuple[str, str, int, int | None]] = []
        for claim in claims:
            member = snapshot.get(claim.character_key)
            if member is None or member.is_ghost:
                continue
            result.append(
                (
                    claim.character_key,
                    claim.character_name,
                    member.level,
                    member.class_id,
                )
            )
        result.sort(key=lambda item: item[1].casefold())
        return result

    async def _char_descriptor(self, character_key: str, fallback_name: str) -> str:
        """Human line for a character from the live roster snapshot."""
        wow = self.wow
        if wow is None:
            return f"**{fallback_name}**"
        snapshot = await wow.data.get_snapshot()
        member = snapshot.get(character_key)
        if member is None:
            return f"**{fallback_name}**"
        bits = [f"Level {member.level}"]
        race = _race_name(member.race_id)
        klass = _class_name(member.class_id)
        if race:
            bits.append(race)
        if klass != "?":
            bits.append(klass)
        return f"**{member.name}** ({', '.join(bits)})"

    async def _char_level(self, character_key: str) -> int:
        wow = self.wow
        if wow is None:
            return 0
        snapshot = await wow.data.get_snapshot()
        member = snapshot.get(character_key)
        return member.level if member else 0

    async def _class_name_for(self, character_key: str) -> str | None:
        """German class name from the snapshot, or ``None`` if unknown."""
        wow = self.wow
        if wow is None:
            return None
        snapshot = await wow.data.get_snapshot()
        member = snapshot.get(character_key)
        if member is None:
            return None
        klass = _class_name(member.class_id)
        return klass if klass != "?" else None

    @staticmethod
    def _is_reroll(signup: DuoSignup) -> bool:
        return signup.kind == "reroll"

    async def _search_title(self, signup: DuoSignup) -> str:
        """Forum title for a search post — carries class + level at a glance."""
        if self._is_reroll(signup):
            return f"🔍 {signup.character_name} · neuer Char ab 1 – Partner gesucht"[
                :100
            ]
        klass = await self._class_name_for(signup.character_key)
        level = await self._char_level(signup.character_key)
        klass_part = f"{klass} " if klass else ""
        return f"🔍 {signup.character_name} · {klass_part}{level} sucht Partner"[:100]

    async def _signup_descriptor(self, signup: DuoSignup) -> str:
        if self._is_reroll(signup):
            return "🆕 **Neuer Char** (rollt frisch von 1 an)"
        return await self._char_descriptor(signup.character_key, signup.character_name)

    async def _search_body(self, signup: DuoSignup) -> str:
        """Body text for a search post, rebuilt on refresh so level stays live."""
        prefs = decode_prefs(signup.prefs)
        lines = [
            f"**{signup.character_name} sucht einen Level-Partner!**",
            "",
            f"• {await self._signup_descriptor(signup)}",
            f"• **Zeiten:** {format_windows(decode_windows(signup.time_windows))}",
            f"• **Pensum:** {intensity_label(signup.intensity)}",
        ]
        if signup.self_found:
            lines.append("• **Self-Found** 🛡️")
        style = [p for p in prefs if p != SELF_FOUND_TAG]
        if style:
            lines.append(f"• **Stil:** {format_prefs(style)}")
        if signup.note:
            lines.append(f"• 📝 {signup.note}")
        lines.append(f"• Gesucht von <@{signup.discord_user_id}>")
        lines.append("")
        lines.append(
            "Passt zu dir? Klick **🤝 Zusammen leveln** — der Rest geht von allein."
        )
        return "\n".join(lines)

    async def _team_title(self, name: str, member_keys: list[str]) -> str:
        """Team forum title with both classes (level intentionally omitted)."""
        classes = [await self._class_name_for(k) for k in member_keys]
        classes = [c for c in classes if c]
        suffix = f" · {' + '.join(classes)}" if classes else ""
        return f"🤝 {name}{suffix}"[:100]

    async def _team_embed(
        self, name: str, members: list[tuple[int, str, str]]
    ) -> discord.Embed:
        """Live journey panel for a team post.

        Rebuilt from the current snapshot so levels/classes stay up to date on
        every refresh. ``members`` items are ``(user_id, char_key, char_name)``.
        """
        embed = discord.Embed(
            title=f"🤝 {name}",
            description=(
                "Euer gemeinsamer Weg. Viel Erfolg — und passt aufeinander " "auf. 🪷"
            ),
            colour=HORDE_RED,
        )
        levels: list[int] = []
        for idx, (user_id, char_key, char_name) in enumerate(members, start=1):
            descriptor = await self._char_descriptor(char_key, char_name)
            embed.add_field(
                name=f"Partner {idx}",
                value=f"<@{user_id}>\n{descriptor}",
                inline=True,
            )
            levels.append(await self._char_level(char_key))
        reached = [lvl for lvl in levels if lvl > 0]
        if reached:
            embed.add_field(
                name="Gemeinsam",
                value="Level " + " + ".join(str(lvl) for lvl in reached),
                inline=False,
            )
        embed.set_footer(text="Level & Klasse aktualisieren sich automatisch.")
        return embed

    async def _chars_in_active_team(self, user_id: int) -> set[str]:
        """Character keys of the user that already sit in an active team."""
        in_team: set[str] = set()
        for team in await self.data.active_teams_for_user(user_id):
            for member in await self.data.team_members(team.team_id):
                if member.discord_user_id == user_id:
                    in_team.add(member.character_key)
        return in_team

    async def _available_chars(
        self, user_id: int, *, exclude_searching: bool
    ) -> list[tuple[str, str, int, int | None]]:
        """Living claimed chars a player can still dedicate to a partner.

        Always excludes chars already in an active team. When
        ``exclude_searching`` is set (creating a new search), also excludes
        chars that already have an open search post, so a char can't be
        double-listed on the board.
        """
        chars = await self._living_claim_chars(user_id)
        blocked = await self._chars_in_active_team(user_id)
        if exclude_searching:
            blocked |= {
                s.character_key for s in await self.data.signups_for_user(user_id)
            }
        return [c for c in chars if c[0] not in blocked]

    # ---- hub button entry points ----

    async def open_search(self, interaction: discord.Interaction) -> None:
        # Reroll ("neuen Char zusammen anfangen") is always possible, so even a
        # player with every char already paired can still open the flow.
        chars = await self._available_chars(interaction.user.id, exclude_searching=True)
        view = DuoSearchComposeView(self, interaction.user.id, chars)
        await interaction.response.send_message(
            "**Partner-Suche erstellen** — wähle den Char (oder **✨ Neuer Char**, "
            "um zusammen frisch zu rollen), deine Spielzeiten und dein Pensum, "
            "dann **Auf's Board**.",
            view=view,
            ephemeral=True,
        )

    async def open_status(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            **await self._status_payload(interaction.user.id), ephemeral=True
        )

    async def open_help(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(HUB_HELP_TEXT, ephemeral=True)

    async def _status_payload(self, user_id: int) -> dict:
        """Informational overview of ALL of a player's teams and searches.

        Mutating actions live on the posts themselves (disband/rename/swap on
        the team post, withdraw on the search post) so this scales cleanly to
        several alts.
        """
        teams = await self.data.active_teams_for_user(user_id)
        searches = await self.data.signups_for_user(user_id)
        lines: list[str] = []
        if teams:
            lines.append("🤝 **Deine Teams:**")
            for team in teams:
                members = await self.data.team_members(team.team_id)
                mine = next((m for m in members if m.discord_user_id == user_id), None)
                partners = [m for m in members if m.discord_user_id != user_id]
                mine_txt = (
                    await self._char_descriptor(mine.character_key, mine.character_name)
                    if mine
                    else "—"
                )
                partner_txt = (
                    ", ".join(
                        f"<@{p.discord_user_id}> ({p.character_name})" for p in partners
                    )
                    or "—"
                )
                lines.append(
                    f"• **{team.name}** ({_thread_mention(team.thread_id)}) — "
                    f"dein Char: {mine_txt}; Partner: {partner_txt}"
                )
        if searches:
            lines.append("🔍 **Deine offenen Suchen:**")
            for signup in searches:
                windows = format_windows(decode_windows(signup.time_windows))
                descriptor = await self._char_descriptor(
                    signup.character_key, signup.character_name
                )
                lines.append(
                    f"• {descriptor} — {windows} ({_thread_mention(signup.post_id)})"
                )
        if not lines:
            lines.append("Du suchst gerade nicht und bist in keinem Team.")
        lines.append(
            "\n_Suche zurückziehen: im Such-Post · Team auflösen/umbenennen: "
            "im Team-Post._"
        )
        return {"content": "\n".join(lines), "view": DuoStatusView(self)}

    # ---- search publishing ----

    async def publish_search(
        self,
        interaction: discord.Interaction,
        character_key: str,
        windows: list[str],
        *,
        intensity: str,
        prefs: list[str],
        note: str | None,
    ) -> None:
        wow = self.wow
        forum = self.forum()
        if wow is None or forum is None:
            await interaction.response.edit_message(
                content="❌ System aktuell nicht verfügbar.", view=None
            )
            return

        is_reroll = character_key == REROLL_VALUE
        if is_reroll:
            character_key = (
                f"reroll:{interaction.user.id}:{int(datetime.utcnow().timestamp())}"
            )
            char_name = "Neuer Char"
            realm_slug = ""
        else:
            claim = await wow.data.get_claim(character_key)
            if claim is None or claim.discord_user_id != interaction.user.id:
                await interaction.response.edit_message(
                    content="❌ Dieser Char gehört dir nicht (mehr).", view=None
                )
                return
            # Race guard: the char may have joined a team since compose opened.
            if await self.data.active_team_by_character(character_key) is not None:
                await interaction.response.edit_message(
                    content=f"❌ **{claim.character_name}** ist bereits in einem Team.",
                    view=None,
                )
                return
            char_name = claim.character_name
            realm_slug = claim.realm_slug

        encoded_windows = encode_windows(windows)
        encoded_prefs = encode_prefs(prefs)
        self_found = SELF_FOUND_TAG in prefs

        # Replace this character's previous open search + its forum post.
        old = await self.data.get_signup(character_key)
        if old is not None and old.post_id:
            await self._delete_post(old.post_id)

        signup = await self.data.upsert_signup(
            interaction.user.id,
            character_key,
            char_name,
            realm_slug,
            encoded_windows,
            (note or None),
            kind="reroll" if is_reroll else "char",
            self_found=self_found,
            prefs=encoded_prefs,
            intensity=intensity,
        )
        await interaction.response.edit_message(
            content="✅ Deine Suche wird erstellt …", view=None
        )

        created = await forum.create_thread(
            name=await self._search_title(signup),
            content=await self._search_body(signup),
            applied_tags=self._tag(TAG_SEARCHING),
        )
        await created.message.edit(view=DuoSearchPostView(self))
        await self.data.set_signup_post(character_key, created.thread.id)

        await self._ping_matches(created.thread, signup)
        try:
            await interaction.edit_original_response(
                content=f"✅ Deine Suche ist online: {created.thread.mention}"
            )
        except discord.HTTPException:
            pass

    async def _ranked_candidates(self, signup: DuoSignup):
        """Rank all OTHER open signups against ``signup``."""
        my_level = await self._char_level(signup.character_key)
        others = []
        for other in await self.data.list_signups(
            exclude_user_id=signup.discord_user_id
        ):
            others.append(
                (
                    other.discord_user_id,
                    other.character_name,
                    await self._char_level(other.character_key),
                    decode_windows(other.time_windows),
                    bool(other.self_found),
                    other.intensity,
                )
            )
        return rank_candidates(
            decode_windows(signup.time_windows),
            my_level,
            others,
            my_self_found=bool(signup.self_found),
            my_intensity=signup.intensity,
        )

    async def _ping_matches(self, thread: discord.Thread, signup: DuoSignup) -> None:
        """Reverse-match: ping the best-fitting existing searchers in the post.

        Turns a passive board into active matchmaking — the *potential
        partners* get notified, not just the person who searched.
        """
        ranked = [c for c in await self._ranked_candidates(signup) if c.overlap_count]
        if not ranked:
            return
        top = ranked[:3]
        mentions = " ".join(f"<@{c.discord_user_id}>" for c in top)
        lines = [f"👀 {mentions} — das könnte zu euch passen!"]
        for cand in top:
            lines.append(
                f"• **{cand.character_name}** (Level {cand.level}) · "
                f"gemeinsam: {format_windows(cand.overlap)}"
            )
        try:
            await thread.send("\n".join(lines))
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ---- join / request / team creation ----

    async def handle_join(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        signup = (
            await self.data.get_signup_by_post(channel.id)
            if isinstance(channel, discord.Thread)
            else None
        )
        if signup is None:
            await interaction.response.send_message(
                "Diese Suche ist nicht mehr aktiv.", ephemeral=True
            )
            return
        if signup.discord_user_id == interaction.user.id:
            await interaction.response.send_message(
                "Das ist deine eigene Suche. 🙂", ephemeral=True
            )
            return
        # Chars already in a team can't join another; a char that is itself
        # searching may join (its search post is cleaned up on match). A reroll
        # option is always available so two people can start fresh together.
        chars = await self._available_chars(
            interaction.user.id, exclude_searching=False
        )
        view = DuoJoinCharSelectView(self, signup, chars)
        await interaction.response.send_message(
            f"Mit welchem Char willst du zu **{signup.character_name}**? "
            "(**✨ Neuer Char** = ihr rollt zusammen frisch)",
            view=view,
            ephemeral=True,
        )

    async def _send_request(
        self,
        interaction: discord.Interaction,
        owner_signup: DuoSignup,
        requester_char_key: str,
        requester_char_name: str,
    ) -> None:
        is_reroll = requester_char_key == REROLL_VALUE
        my_level = 0 if is_reroll else await self._char_level(requester_char_key)
        req_signup = (
            None if is_reroll else await self.data.get_signup(requester_char_key)
        )
        shared = overlap_keys(
            decode_windows(owner_signup.time_windows),
            decode_windows(req_signup.time_windows) if req_signup else [],
        )
        shared_txt = (
            f"\nGemeinsame Zeiten: **{format_windows(shared)}**" if shared else ""
        )
        who = (
            "🆕 Neuer Char (frisch)"
            if is_reroll
            else f"**{requester_char_name}**, Level {my_level}"
        )
        view = DuoJoinRequestView(
            self,
            owner_id=owner_signup.discord_user_id,
            owner_char_key=owner_signup.character_key,
            owner_char_name=owner_signup.character_name,
            requester_id=interaction.user.id,
            requester_char_key=requester_char_key,
            requester_char_name=requester_char_name,
        )
        try:
            await interaction.channel.send(
                content=(
                    f"<@{owner_signup.discord_user_id}> — <@{interaction.user.id}> "
                    f"will mit dir leveln! ({who}){shared_txt}\n"
                    "Nimm an, dann macht der Bot euch ein Team."
                ),
                view=view,
            )
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message(
                "❌ Anfrage konnte nicht gesendet werden.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"✅ Anfrage an <@{owner_signup.discord_user_id}> gesendet.",
            ephemeral=True,
        )

    async def accept_request(
        self,
        interaction: discord.Interaction,
        owner_id: int,
        owner_char_key: str,
        owner_char_name: str,
        requester_id: int,
        requester_char_key: str,
        requester_char_name: str,
    ) -> None:
        owner_signup = await self.data.get_signup(owner_char_key)
        if owner_signup is None:
            await interaction.response.edit_message(
                content="Diese Suche ist nicht mehr aktiv.", view=None
            )
            return
        # Per-character guard: either char may have been paired meanwhile.
        if await self.data.active_team_by_character(requester_char_key) is not None:
            await interaction.response.edit_message(
                content=f"**{requester_char_name}** ist bereits in einem Team.",
                view=None,
            )
            return
        if await self.data.active_team_by_character(owner_char_key) is not None:
            await interaction.response.edit_message(
                content=f"**{owner_char_name}** ist bereits in einem Team.", view=None
            )
            return
        await interaction.response.edit_message(
            content="✅ Angenommen! Euer Team wird erstellt …", view=None
        )
        await self._create_team(
            owner_id,
            owner_char_key,
            owner_char_name,
            requester_id,
            requester_char_key,
            requester_char_name,
        )

    async def _create_team(
        self,
        owner_id: int,
        owner_char_key: str,
        owner_char_name: str,
        requester_id: int,
        requester_char_key: str,
        requester_char_name: str,
    ) -> None:
        forum = self.forum()
        if forum is None:
            return

        # A requester joining as a fresh reroll has no signup yet — mint a
        # synthetic character key for them (owner rerolls already carry one).
        owner_signup = await self.data.get_signup(owner_char_key)
        if requester_char_key == REROLL_VALUE:
            requester_char_key = (
                f"reroll:{requester_id}:{int(datetime.utcnow().timestamp())}"
            )
            requester_char_name = "Neuer Char"
            req_signup = None
        else:
            req_signup = await self.data.get_signup(requester_char_key)

        name = pick_team_name(await self.data.used_team_names())
        members = [
            (owner_id, owner_char_key, owner_char_name),
            (requester_id, requester_char_key, requester_char_name),
        ]
        created = await forum.create_thread(
            name=await self._team_title(name, [owner_char_key, requester_char_key]),
            embed=await self._team_embed(name, members),
            applied_tags=self._tag(TAG_ACTIVE),
        )
        await created.message.edit(view=DuoTeamPostView(self))
        thread = created.thread
        await self.data.create_team(name, thread.id, members)
        for user_id in (owner_id, requester_id):
            try:
                await thread.add_user(discord.Object(id=user_id))
            except (discord.Forbidden, discord.HTTPException):
                pass
        try:
            pin_msg = await thread.send(
                "📌 **Haltet hier eure Termine fest** — wann geht's weiter? "
                "Postet ruhig Screenshots, das ist eure Reise."
            )
            await pin_msg.pin()
        except (discord.Forbidden, discord.HTTPException):
            pass

        # Both dedicated chars are now paired: drop their board posts + signups.
        for signup in (owner_signup, req_signup):
            if signup is None:
                continue
            if signup.post_id:
                await self._delete_post(signup.post_id)
            await self.data.remove_signup(signup.character_key)

    # ---- status actions (edit / withdraw / disband / swap) ----

    async def handle_withdraw(self, interaction: discord.Interaction) -> None:
        """Owner-only 'Zurückziehen' button on a search post."""
        channel = interaction.channel
        signup = (
            await self.data.get_signup_by_post(channel.id)
            if isinstance(channel, discord.Thread)
            else None
        )
        if signup is None:
            await interaction.response.send_message(
                "Diese Suche ist nicht mehr aktiv.", ephemeral=True
            )
            return
        if signup.discord_user_id != interaction.user.id:
            await interaction.response.send_message(
                "Nur wer die Suche erstellt hat, kann sie zurückziehen.",
                ephemeral=True,
            )
            return
        await self.data.remove_signup(signup.character_key)
        await interaction.response.send_message(
            f"✅ Suche für **{signup.character_name}** zurückgezogen.", ephemeral=True
        )
        if signup.post_id:
            await self._delete_post(signup.post_id)

    async def disband_team_for(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        team = (
            await self.data.get_team_by_thread(channel.id)
            if isinstance(channel, discord.Thread)
            else None
        )
        if team is None or team.status == "disbanded":
            await interaction.response.send_message(
                "Dieses Team ist nicht mehr aktiv.", ephemeral=True
            )
            return
        members = await self.data.team_members(team.team_id)
        if interaction.user.id not in {m.discord_user_id for m in members}:
            await interaction.response.send_message(
                "Nur Team-Mitglieder können das Team auflösen.", ephemeral=True
            )
            return
        await self._disband(team)
        await interaction.response.send_message(
            f"✅ **{team.name}** wurde aufgelöst.", ephemeral=True
        )

    async def _disband(self, team: DuoTeam) -> None:
        await self.data.disband_team(team.team_id)
        thread = await self._fetch_thread(team.thread_id)
        if thread is not None:
            try:
                await thread.send(
                    "🕯️ Dieses Team wurde aufgelöst. Danke für die Reise."
                )
                await thread.edit(applied_tags=self._tag(TAG_DISBANDED), archived=True)
            except (discord.Forbidden, discord.HTTPException):
                pass

    async def start_char_swap(self, interaction: discord.Interaction) -> None:
        """Team member picks a new dedicated char (reroll / after a death)."""
        channel = interaction.channel
        team = (
            await self.data.get_team_by_thread(channel.id)
            if isinstance(channel, discord.Thread)
            else None
        )
        if team is None or team.status == "disbanded":
            await interaction.response.send_message(
                "Dieses Team ist nicht mehr aktiv.", ephemeral=True
            )
            return
        members = await self.data.team_members(team.team_id)
        if interaction.user.id not in {m.discord_user_id for m in members}:
            await interaction.response.send_message(
                "Nur Team-Mitglieder können das.", ephemeral=True
            )
            return
        chars = await self._living_claim_chars(interaction.user.id)
        if not chars:
            await interaction.response.send_message(
                "Du hast keinen lebenden, geclaimten Char zum Wechseln.",
                ephemeral=True,
            )
            return
        view = DuoSwapCharSelectView(self, team, chars)
        await interaction.response.send_message(
            "Welchen Char bringst du jetzt ins Team?", view=view, ephemeral=True
        )

    async def apply_char_swap(
        self, interaction: discord.Interaction, team: DuoTeam, character_key: str
    ) -> None:
        wow = self.wow
        if wow is None:
            await interaction.response.edit_message(content="❌ Fehler.", view=None)
            return
        claim = await wow.data.get_claim(character_key)
        if claim is None or claim.discord_user_id != interaction.user.id:
            await interaction.response.edit_message(
                content="❌ Dieser Char gehört dir nicht.", view=None
            )
            return
        await self.data.swap_member_character(
            team.team_id, interaction.user.id, character_key, claim.character_name
        )
        await self.data.set_team_status(team.team_id, "active")
        await interaction.response.edit_message(
            content=f"✅ Du ziehst jetzt mit **{claim.character_name}** weiter.",
            view=None,
        )
        thread = await self._fetch_thread(team.thread_id)
        if thread is not None:
            descriptor = await self._char_descriptor(
                character_key, claim.character_name
            )
            try:
                await thread.send(
                    f"🔄 <@{interaction.user.id}> zieht jetzt mit {descriptor} "
                    f"weiter. **{team.name}** macht weiter! 💪"
                )
                await thread.edit(applied_tags=self._tag(TAG_ACTIVE))
            except (discord.Forbidden, discord.HTTPException):
                pass

    async def start_rename(self, interaction: discord.Interaction) -> None:
        """Open the rename modal for a team member."""
        channel = interaction.channel
        team = (
            await self.data.get_team_by_thread(channel.id)
            if isinstance(channel, discord.Thread)
            else None
        )
        if team is None or team.status == "disbanded":
            await interaction.response.send_message(
                "Dieses Team ist nicht mehr aktiv.", ephemeral=True
            )
            return
        members = await self.data.team_members(team.team_id)
        if interaction.user.id not in {m.discord_user_id for m in members}:
            await interaction.response.send_message(
                "Nur Team-Mitglieder können den Namen ändern.", ephemeral=True
            )
            return
        await interaction.response.send_modal(DuoTeamRenameModal(self, team))

    async def apply_rename(
        self, interaction: discord.Interaction, team: DuoTeam, raw_name: str
    ) -> None:
        name = " ".join(raw_name.split()).strip()
        if not name:
            await interaction.response.send_message(
                "Der Name darf nicht leer sein.", ephemeral=True
            )
            return
        # Discord caps thread titles at 100 chars; leave room for the emoji.
        name = name[:80]
        current = await self.data.get_team(team.team_id)
        if current is None or current.status == "disbanded":
            await interaction.response.send_message(
                "Dieses Team ist nicht mehr aktiv.", ephemeral=True
            )
            return
        await self.data.set_team_name(team.team_id, name)
        await interaction.response.send_message(
            f"✅ Team heißt jetzt **{name}**.", ephemeral=True
        )
        thread = await self._fetch_thread(team.thread_id)
        if thread is not None:
            members = await self.data.team_members(team.team_id)
            title = await self._team_title(name, [m.character_key for m in members])
            try:
                await thread.edit(name=title)
                await thread.send(f"✏️ Das Team heißt ab jetzt **{name}**.")
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("[DuoCog] Team-Umbenennung fehlgeschlagen: %s", exc)

    # ---- hourly upkeep (called by WoWCog.refresh_live_roster) ----

    async def refresh_open_posts(self) -> None:
        """Keep search posts' level/class live, expire stale searches, and
        update the hub board counter. Each step is isolated so one failure
        doesn't skip the rest."""
        if self.forum() is None:
            return
        for step in (
            self._expire_stale_searches,
            self._refresh_search_posts,
            self._refresh_team_posts,
            self._refresh_hub_counter,
        ):
            try:
                await step()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("[DuoCog] %s fehlgeschlagen: %s", step.__name__, exc)

    async def _expire_stale_searches(self) -> None:
        cutoff = (datetime.utcnow() - timedelta(days=SEARCH_STALE_DAYS)).isoformat()
        for signup in await self.data.stale_signups(cutoff):
            if signup.post_id:
                await self._delete_post(signup.post_id)
            await self.data.remove_signup(signup.character_key)
            logger.info(
                "[DuoCog] Abgelaufene Suche entfernt: %s (%s).",
                signup.character_name,
                signup.character_key,
            )

    async def _refresh_search_posts(self) -> None:
        for signup in await self.data.list_signups():
            if not signup.post_id:
                continue
            thread = await self._fetch_thread(signup.post_id)
            if thread is None:
                continue
            try:
                await thread.get_partial_message(signup.post_id).edit(
                    content=await self._search_body(signup)
                )
                new_title = await self._search_title(signup)
                if thread.name != new_title:  # rename only on change (rate-limit)
                    await thread.edit(name=new_title)
            except (discord.Forbidden, discord.HTTPException):
                pass

    async def _refresh_team_posts(self) -> None:
        """Keep team posts' embed (live levels/classes) and title current."""
        for team in await self.data.active_teams():
            if not team.thread_id:
                continue
            thread = await self._fetch_thread(team.thread_id)
            if thread is None:
                continue
            members = await self.data.team_members(team.team_id)
            member_tuples = [
                (m.discord_user_id, m.character_key, m.character_name) for m in members
            ]
            try:
                await thread.get_partial_message(team.thread_id).edit(
                    embed=await self._team_embed(team.name, member_tuples)
                )
                title = await self._team_title(
                    team.name, [m.character_key for m in members]
                )
                if thread.name != title:  # rename only on change (rate-limit)
                    await thread.edit(name=title)
            except (discord.Forbidden, discord.HTTPException):
                pass

    async def _refresh_hub_counter(self) -> None:
        hub_id = await self.data.get_setting("hub_thread_id")
        if not hub_id:
            return
        thread = await self._fetch_thread(int(hub_id))
        if thread is None:
            return
        count = await self.data.signup_count()
        counter = f"\n\n🔎 **Gerade auf Partnersuche:** {count}" if count else ""
        try:
            await thread.get_partial_message(int(hub_id)).edit(
                content=HUB_TEXT + counter
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ---- hooks called by WoWCog ----

    async def on_character_death(self, character_key: str, level: int) -> None:
        """Post a memorial in the team thread when a partner falls (HC)."""
        try:
            team = await self.data.active_team_by_character(character_key)
            if team is None:
                return
            members = await self.data.team_members(team.team_id)
            fallen = next(
                (m for m in members if m.character_key == character_key), None
            )
            if fallen is None:
                return
            thread = await self._fetch_thread(team.thread_id)
            if thread is None:
                return
            await self.data.set_team_status(team.team_id, "mourning")
            survivors = [
                m for m in members if m.discord_user_id != fallen.discord_user_id
            ]
            ping = " ".join(f"<@{m.discord_user_id}>" for m in members)
            survivor_ping = (
                " ".join(f"<@{m.discord_user_id}>" for m in survivors) or "Team"
            )
            embed = discord.Embed(
                title="🕯️ Ein Partner ist gefallen",
                description=(
                    f"**{fallen.character_name}** ist auf Level **{level}** "
                    f"gefallen. Hardcore vergisst nichts.\n\n"
                    f"{survivor_ping} — ihr könnt euer Team fortführen: "
                    f"<@{fallen.discord_user_id}> wählt über **🔄 Meinen Char "
                    "wechseln** einen neuen Char. Oder löst **{team}** ehrenvoll "
                    "auf.".replace("{team}", team.name)
                ),
                colour=HORDE_RED,
            )
            await thread.send(content=ping, embed=embed)
            try:
                await thread.edit(applied_tags=self._tag(TAG_MOURNING))
            except (discord.Forbidden, discord.HTTPException):
                pass
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("[DuoCog] on_character_death fehlgeschlagen: %s", exc)

    async def on_character_milestone(self, character_key: str, level: int) -> None:
        """Celebrate a level milestone in the team thread; duo bonus if both."""
        try:
            if level not in DUO_MILESTONE_BONUS:
                return
            team = await self.data.active_team_by_character(character_key)
            if team is None:
                return
            thread = await self._fetch_thread(team.thread_id)
            if thread is None:
                return
            members = await self.data.team_members(team.team_id)
            reached = [
                await self._reached_milestone(m.character_key, level) for m in members
            ]
            both_reached = len(members) >= 2 and all(reached)
            if both_reached and await self.data.record_duo_milestone(
                team.team_id, level
            ):
                bonus = DUO_MILESTONE_BONUS[level]
                ping = " ".join(f"<@{m.discord_user_id}>" for m in members)
                await thread.send(
                    f"🎉 {ping} — **{team.name}** ist gemeinsam auf **Level "
                    f"{level}**! Bonus: **+{bonus}** Champion-Punkte für beide. 🪷"
                )
                for member in members:
                    await self._award_champion(
                        member.discord_user_id,
                        bonus,
                        f"Duo-Meilenstein {team.name}: gemeinsam Level {level}",
                    )
            elif not both_reached:
                fallen_behind = [m for m in members if m.character_key != character_key]
                waiting = (
                    " ".join(f"<@{m.discord_user_id}>" for m in fallen_behind) or ""
                )
                mover = next(
                    (m for m in members if m.character_key == character_key), None
                )
                mover_name = mover.character_name if mover else "Ein Partner"
                await thread.send(
                    f"🏅 **{mover_name}** hat Level **{level}** erreicht! "
                    f"{waiting} zieh nach — dann gibt's Bonus für beide. 💪"
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("[DuoCog] on_character_milestone fehlgeschlagen: %s", exc)

    async def _reached_milestone(self, character_key: str, level: int) -> bool:
        """Whether a character has reached ``level``, timing-independent.

        Prefers WoWData's recorded milestone event (authoritative, unaffected
        by the fact that the live snapshot is only rewritten *after* the scan
        posts). Falls back to the current snapshot level so characters that
        were already past the milestone when they joined the team still count.
        """
        wow = self.wow
        if wow is None:
            return False
        try:
            if await wow.data.milestone_exists(character_key, level):
                return True
        except Exception:  # pragma: no cover - defensive
            pass
        return await self._char_level(character_key) >= level

    async def _award_champion(self, user_id: int, points: int, reason: str) -> None:
        get_cog = getattr(self.bot, "get_cog", None)
        champion = get_cog("ChampionCog") if get_cog else None
        if champion is None or not hasattr(champion, "update_user_score"):
            logger.info("[DuoCog] ChampionCog nicht verfügbar für Duo-Bonus.")
            return
        try:
            await champion.update_user_score(user_id, points, reason)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("[DuoCog] Champion-Bonus fehlgeschlagen: %s", exc)

    # ---- thread/post utilities ----

    async def _fetch_thread(self, thread_id: int | None) -> discord.Thread | None:
        if not thread_id:
            return None
        channel = self.bot.get_channel(thread_id)
        if isinstance(channel, discord.Thread):
            return channel
        try:
            fetched = await self.bot.fetch_channel(thread_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        return fetched if isinstance(fetched, discord.Thread) else None

    async def _delete_post(self, thread_id: int) -> None:
        thread = await self._fetch_thread(thread_id)
        if thread is None:
            logger.warning(
                "[DuoCog] Such-Post %s zum Löschen nicht auffindbar.", thread_id
            )
            return
        try:
            await thread.delete()
            return
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning(
                "[DuoCog] Such-Post %s löschen fehlgeschlagen (%s) — archiviere.",
                thread_id,
                exc,
            )
        # Fall back to archiving + locking if we can't delete.
        try:
            await thread.edit(archived=True, locked=True)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning(
                "[DuoCog] Such-Post %s archivieren fehlgeschlagen: %s", thread_id, exc
            )


def _thread_mention(thread_id: int | None) -> str:
    return f"<#{thread_id}>" if thread_id else "—"


# --------------------------------------------------------------------------- #
#                                   Views                                      #
# --------------------------------------------------------------------------- #


class DuoHubView(discord.ui.View):
    """Persistent buttons on the pinned hub post."""

    def __init__(self, cog: DuoCog) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Partner suchen",
        style=discord.ButtonStyle.success,
        emoji="🤝",
        custom_id="duo_hub:search",
    )
    async def search(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self.cog.open_search(interaction)

    @discord.ui.button(
        label="Mein Status",
        style=discord.ButtonStyle.secondary,
        custom_id="duo_hub:status",
    )
    async def status(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self.cog.open_status(interaction)

    @discord.ui.button(
        label="Hilfe",
        style=discord.ButtonStyle.secondary,
        custom_id="duo_hub:help",
    )
    async def help(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self.cog.open_help(interaction)


class DuoSearchPostView(discord.ui.View):
    """Persistent 'Zusammen leveln' button on a public search post."""

    def __init__(self, cog: DuoCog) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Zusammen leveln",
        style=discord.ButtonStyle.success,
        emoji="🤝",
        custom_id="duo_post:join",
    )
    async def join(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self.cog.handle_join(interaction)

    @discord.ui.button(
        label="Zurückziehen",
        style=discord.ButtonStyle.secondary,
        emoji="🗑️",
        custom_id="duo_post:withdraw",
    )
    async def withdraw(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self.cog.handle_withdraw(interaction)


class DuoTeamPostView(discord.ui.View):
    """Persistent buttons on a team post."""

    def __init__(self, cog: DuoCog) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Team umbenennen",
        style=discord.ButtonStyle.secondary,
        emoji="✏️",
        custom_id="duo_team:rename",
    )
    async def rename(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self.cog.start_rename(interaction)

    @discord.ui.button(
        label="Meinen Char wechseln",
        style=discord.ButtonStyle.secondary,
        emoji="🔄",
        custom_id="duo_team:swap",
    )
    async def swap(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self.cog.start_char_swap(interaction)

    @discord.ui.button(
        label="Team auflösen",
        style=discord.ButtonStyle.danger,
        emoji="🕯️",
        custom_id="duo_team:disband",
    )
    async def disband(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self.cog.disband_team_for(interaction)


class DuoTeamRenameModal(discord.ui.Modal, title="Team umbenennen"):
    """Lets a team member set a custom team name."""

    new_name: discord.ui.TextInput = discord.ui.TextInput(
        label="Neuer Teamname",
        placeholder="z.B. Team Aschefaust",
        min_length=1,
        max_length=80,
        required=True,
    )

    def __init__(self, cog: DuoCog, team: DuoTeam) -> None:
        super().__init__()
        self.cog = cog
        self.team = team

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.apply_rename(interaction, self.team, str(self.new_name.value))


class _OwnerCheckMixin(discord.ui.View):
    """Restrict a transient view to a single user."""

    allowed_user_id: int

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.allowed_user_id:
            await interaction.response.send_message(
                "Das ist nicht deine Auswahl.", ephemeral=True
            )
            return False
        return True


def _char_select_options(
    chars: list[tuple[str, str, int, int | None]],
    *,
    encode_name: bool = False,
) -> list[discord.SelectOption]:
    """Build char-select options with the reroll option always on top.

    ``encode_name`` packs ``key||name`` into the value (used where the callback
    only sees the value, e.g. the join flow); otherwise the value is the key.
    """
    reroll_value = f"{REROLL_VALUE}||Neuer Char" if encode_name else REROLL_VALUE
    options = [
        discord.SelectOption(
            label="✨ Neuer Char (zusammen rollen)",
            value=reroll_value,
            description="Ihr fangt beide frisch von Level 1 an",
        )
    ]
    for key, name, level, class_id in chars[:24]:
        value = f"{key}||{name}" if encode_name else key
        options.append(
            discord.SelectOption(
                label=name[:100],
                value=value,
                description=f"Level {level} · {_class_name(class_id)}"[:100],
            )
        )
    return options


class DuoSearchComposeView(_OwnerCheckMixin):
    """Ephemeral compose flow: char/reroll + windows + intensity (+ optional
    play-style tags and a free-text note), then publish."""

    def __init__(
        self,
        cog: DuoCog,
        user_id: int,
        chars: list[tuple[str, str, int, int | None]],
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.allowed_user_id = user_id
        self.char_key: str | None = None
        self.windows: list[str] = []
        self.intensity: str | None = None
        self.prefs: list[str] = []
        self.note: str | None = None

        self.char_select = discord.ui.Select(
            placeholder="Welchen Char? (oder neu rollen)",
            min_values=1,
            max_values=1,
            options=_char_select_options(chars),
            row=0,
        )
        self.char_select.callback = self._on_char
        self.add_item(self.char_select)

        self.win_select = discord.ui.Select(
            placeholder="Wann spielst du meist? (mehrere möglich)",
            min_values=1,
            max_values=len(TIME_WINDOWS),
            options=[
                discord.SelectOption(label=label, value=key)
                for key, label in TIME_WINDOWS.items()
            ],
            row=1,
        )
        self.win_select.callback = self._on_win
        self.add_item(self.win_select)

        self.intensity_select = discord.ui.Select(
            placeholder="Wie viel Zeit pro Session?",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=label, value=key)
                for key, label in INTENSITY.items()
            ],
            row=2,
        )
        self.intensity_select.callback = self._on_intensity
        self.add_item(self.intensity_select)

        self.prefs_select = discord.ui.Select(
            placeholder="Vorlieben (optional, mehrere)",
            min_values=0,
            max_values=len(PLAY_TAGS),
            options=[
                discord.SelectOption(label=label, value=key)
                for key, label in PLAY_TAGS.items()
            ],
            row=3,
        )
        self.prefs_select.callback = self._on_prefs
        self.add_item(self.prefs_select)

        publish_btn = discord.ui.Button(
            label="Auf's Board", style=discord.ButtonStyle.success, row=4
        )
        publish_btn.callback = self._publish
        self.add_item(publish_btn)

        note_btn = discord.ui.Button(
            label="Notiz", emoji="✏️", style=discord.ButtonStyle.secondary, row=4
        )
        note_btn.callback = self._open_note
        self.add_item(note_btn)

    async def _on_char(self, interaction: discord.Interaction) -> None:
        self.char_key = self.char_select.values[0]
        await interaction.response.defer()

    async def _on_win(self, interaction: discord.Interaction) -> None:
        self.windows = list(self.win_select.values)
        await interaction.response.defer()

    async def _on_intensity(self, interaction: discord.Interaction) -> None:
        self.intensity = self.intensity_select.values[0]
        await interaction.response.defer()

    async def _on_prefs(self, interaction: discord.Interaction) -> None:
        self.prefs = list(self.prefs_select.values)
        await interaction.response.defer()

    async def _open_note(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(DuoNoteModal(self))

    async def _publish(self, interaction: discord.Interaction) -> None:
        if not self.char_key or not self.windows or not self.intensity:
            await interaction.response.send_message(
                "Bitte Char, **Zeiten** und **Pensum** wählen.", ephemeral=True
            )
            return
        await self.cog.publish_search(
            interaction,
            self.char_key,
            self.windows,
            intensity=self.intensity,
            prefs=self.prefs,
            note=self.note,
        )


class DuoNoteModal(discord.ui.Modal, title="Notiz zur Suche"):
    """Optional free-text note to capture things the selects can't."""

    note_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Notiz (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=200,
        placeholder="z.B. meist mittwochs · suche ruhigen Schurken-Partner",
    )

    def __init__(self, compose_view: "DuoSearchComposeView") -> None:
        super().__init__()
        self.compose_view = compose_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.compose_view.note = str(self.note_input.value).strip() or None
        msg = (
            "📝 Notiz gespeichert. Jetzt **Auf's Board**."
            if self.compose_view.note
            else "Notiz geleert."
        )
        await interaction.response.send_message(msg, ephemeral=True)


class DuoJoinCharSelectView(_OwnerCheckMixin):
    """Requester picks which char to bring (or a fresh reroll)."""

    def __init__(
        self,
        cog: DuoCog,
        owner_signup: DuoSignup,
        chars: list[tuple[str, str, int, int | None]],
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_signup = owner_signup
        self.allowed_user_id = 0  # set below to the requester
        # The requester is whoever opened this; captured on first interaction.
        self.select = discord.ui.Select(
            placeholder="Dein Char (oder neu rollen)",
            min_values=1,
            max_values=1,
            options=_char_select_options(chars, encode_name=True),
        )
        self.select.callback = self._on_pick
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Bind to the first (and only intended) user.
        if self.allowed_user_id == 0:
            self.allowed_user_id = interaction.user.id
        return await super().interaction_check(interaction)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        key, _, name = self.select.values[0].partition("||")
        await self.cog._send_request(interaction, self.owner_signup, key, name)


class DuoSwapCharSelectView(_OwnerCheckMixin):
    """Team member picks a new dedicated character."""

    def __init__(
        self,
        cog: DuoCog,
        team: DuoTeam,
        chars: list[tuple[str, str, int, int | None]],
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.team = team
        self.allowed_user_id = 0
        self.select = discord.ui.Select(
            placeholder="Neuer Char",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=name[:100],
                    value=key,
                    description=f"Level {level} · {_class_name(class_id)}"[:100],
                )
                for key, name, level, class_id in chars[:25]
            ],
        )
        self.select.callback = self._on_pick
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.allowed_user_id == 0:
            self.allowed_user_id = interaction.user.id
        return await super().interaction_check(interaction)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        await self.cog.apply_char_swap(interaction, self.team, self.select.values[0])


class DuoJoinRequestView(discord.ui.View):
    """Transient accept/decline shown to a search-post owner."""

    def __init__(
        self,
        cog: DuoCog,
        owner_id: int,
        owner_char_key: str,
        owner_char_name: str,
        requester_id: int,
        requester_char_key: str,
        requester_char_name: str,
    ) -> None:
        super().__init__(timeout=86400)
        self.cog = cog
        self.owner_id = owner_id
        self.owner_char_key = owner_char_key
        self.owner_char_name = owner_char_name
        self.requester_id = requester_id
        self.requester_char_key = requester_char_key
        self.requester_char_name = requester_char_name

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Nur die gesuchte Person kann hier entscheiden.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Annehmen", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self.cog.accept_request(
            interaction,
            self.owner_id,
            self.owner_char_key,
            self.owner_char_name,
            self.requester_id,
            self.requester_char_key,
            self.requester_char_name,
        )

    @discord.ui.button(
        label="Ablehnen", style=discord.ButtonStyle.secondary, emoji="✖️"
    )
    async def decline(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(
            content=f"<@{self.requester_id}> — Anfrage abgelehnt.", view=None
        )


class DuoStatusView(discord.ui.View):
    """Ephemeral status overview — the only action is starting a new search.

    Withdrawing a search and disbanding/renaming a team happen on the
    respective forum posts, so this stays valid no matter how many teams or
    searches a player has.
    """

    def __init__(self, cog: DuoCog) -> None:
        super().__init__(timeout=300)
        self.cog = cog

    @discord.ui.button(
        label="Weiteren Partner suchen",
        style=discord.ButtonStyle.success,
        emoji="🤝",
    )
    async def search(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self.cog.open_search(interaction)
