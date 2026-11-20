"""Discord-bot som visar SVT Text-TV som ett meddelande som uppdateras hela tiden."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import timezone
from datetime import time as dt_time
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import texttv

load_dotenv()
TOKEN = os.environ.get("DISCORD_TOKEN", "")
UPDATE_MINUTES = int(os.getenv("UPDATE_MINUTES", "15"))
if UPDATE_MINUTES < 1 or 60 % UPDATE_MINUTES:
    raise SystemExit("UPDATE_MINUTES måste gå jämnt upp i 60, till exempel 5, 10, 15, 20, 30 eller 60.")
# Uppdatera på jämna klockslag: :00, :15, :30, :45 (vid 15 minuter).
UPDATE_TIMES = [dt_time(h, m, tzinfo=timezone.utc) for h in range(24) for m in range(0, 60, UPDATE_MINUTES)]
API_APP_NAME = os.getenv("TEXTTV_APP_NAME", "discord-texttv-bot")
# DATA_DIR låter Docker spara panelerna i en volym. Standard: mappen där bot.py ligger.
DATA_DIR = Path(os.getenv("DATA_DIR") or Path(__file__).parent)
PANELS_FILE = DATA_DIR / "panels.json"
PAGE_CACHE_SECONDS = 20

log = logging.getLogger("texttv-bot")

# message_id -> {"channel_id": int, "page": int, "sub": int, "key": str}
panels: dict[int, dict] = {}
panel_locks: dict[int, asyncio.Lock] = {}
page_cache: dict[int, tuple[float, texttv.Page]] = {}


def load_panels() -> None:
    if PANELS_FILE.exists():
        raw = json.loads(PANELS_FILE.read_text(encoding="utf-8"))
        panels.update({int(k): v for k, v in raw.items()})


def save_panels() -> None:
    PANELS_FILE.write_text(json.dumps(panels, indent=2), encoding="utf-8")


def next_update_unix() -> int:
    step = UPDATE_MINUTES * 60
    return int((time.time() // step + 1) * step)


def clamp_page(n: int) -> int:
    if n < 100:
        return 899
    if n > 899:
        return 100
    return n


# Felkoder som betyder att panelen verkligen är borta (Unknown Channel / Unknown Message).
GONE_CODES = {10003, 10008}


def handle_panel_error(message_id: int, error: discord.HTTPException) -> None:
    """Tar bara bort panelen om meddelandet eller kanalen faktiskt är borta."""
    if isinstance(error, discord.NotFound) and error.code in GONE_CODES:
        log.info("Panel %s är borttagen (kod %s), slutar uppdatera", message_id, error.code)
        panels.pop(message_id, None)
        save_panels()
    else:
        # Saknad behörighet eller tillfälligt fel: behåll panelen och försök igen nästa gång.
        log.warning("Panel %s kunde inte uppdateras (HTTP %s, kod %s: %s), försöker igen senare",
                    message_id, error.status, error.code, error.text)


class TextTVBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.session: aiohttp.ClientSession | None = None

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession(headers={"User-Agent": API_APP_NAME})
        load_panels()
        self.add_view(PanelView())  # knapparna fungerar även efter omstart
        await self.tree.sync()
        refresh_loop.start()

    async def close(self) -> None:
        refresh_loop.cancel()
        if self.session:
            await self.session.close()
        await super().close()

    async def get_page(self, num: int) -> texttv.Page:
        cached = page_cache.get(num)
        if cached and time.monotonic() - cached[0] < PAGE_CACHE_SECONDS:
            return cached[1]
        page = await texttv.fetch_page(self.session, num, API_APP_NAME)
        page_cache[num] = (time.monotonic(), page)
        return page

    async def build_message(self, panel: dict) -> tuple[dict, str]:
        """Skapar meddelandetexten för en panel. Returnerar (kwargs till edit/send, nyckel)."""
        page = await self.get_page(panel["page"])
        sub = panel.get("sub", 0) % len(page.subpages)
        key = f"{page.num}:{page.updated}:{sub}"

        header = f"📺 **SVT Text-TV · sida {page.num}**"
        if len(page.subpages) > 1:
            header += f" · undersida {sub + 1}/{len(page.subpages)}"
        # <t:…> visas i varje läsares egen tidszon och räknar ner automatiskt.
        footer = f"-# Uppdateras var {UPDATE_MINUTES}:e minut · nästa <t:{next_update_unix()}:R>"
        if page.updated:
            footer += f" · sidan ändrad <t:{page.updated}:t>"
        footer += " · SVT Text via texttv.nu"

        # Sidan ritas som färgad text (ANSI) och får plats med resten inom Discords 2000 tecken.
        body = await texttv.render_page(self.session, page.subpages[sub],
                                        limit=2000 - len(header) - len(footer) - 2,
                                        label=page.section or "SVT Text")
        kwargs = {
            "content": f"{header}\n{body}\n{footer}",
            "view": PanelView(show_stop=not panel.get("pinned", False)),
        }
        return kwargs, key


client = TextTVBot()


async def update_panel(message_id: int, *, force: bool = False,
                       interaction: discord.Interaction | None = None) -> None:
    """Ritar om en panel om sidan har ändrats (eller alltid om force=True)."""
    panel = panels.get(message_id)
    if panel is None:
        return
    lock = panel_locks.setdefault(message_id, asyncio.Lock())
    async with lock:
        kwargs, key = await client.build_message(panel)
        if not force and key == panel.get("key"):
            return
        if interaction is not None:
            await interaction.edit_original_response(**kwargs)
        else:
            channel = client.get_channel(panel["channel_id"]) or await client.fetch_channel(panel["channel_id"])
            await channel.get_partial_message(message_id).edit(**kwargs)
        panel["key"] = key
        save_panels()


@tasks.loop(time=UPDATE_TIMES)
async def refresh_loop() -> None:
    for message_id in list(panels):
        panel = panels.get(message_id)
        if panel is None:
            continue
        try:
            # Sidor med flera undersidor bläddras automatiskt, precis som på TV:n.
            cached = page_cache.get(panel["page"])
            if cached and len(cached[1].subpages) > 1:
                panel["sub"] = panel.get("sub", 0) + 1
            # Alltid omritning så att nedräkningen till nästa uppdatering stämmer.
            await update_panel(message_id, force=True)
        except discord.HTTPException as e:
            handle_panel_error(message_id, e)
        except Exception:
            log.exception("Kunde inte uppdatera panel %s", message_id)


@refresh_loop.before_loop
async def before_refresh() -> None:
    await client.wait_until_ready()


# --------------------------------------------------------------------------- knappar

async def change_page(interaction: discord.Interaction, new_page: int) -> None:
    panel = panels.get(interaction.message.id)
    if panel is None:
        await interaction.response.send_message("Den här panelen är inte längre aktiv.", ephemeral=True)
        return
    await interaction.response.defer()
    panel["page"], panel["sub"] = clamp_page(new_page), 0
    try:
        await update_panel(interaction.message.id, force=True, interaction=interaction)
    except Exception:
        log.exception("Kunde inte byta sida")
        await interaction.followup.send(f"Kunde inte hämta sida {new_page} just nu.", ephemeral=True)


class GotoModal(discord.ui.Modal, title="Gå till sida"):
    page = discord.ui.TextInput(label="Sidnummer (100–899)", min_length=3, max_length=3,
                                placeholder="t.ex. 377")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value = self.page.value.strip()
        if not value.isdigit() or not 100 <= int(value) <= 899:
            await interaction.response.send_message("Skriv ett sidnummer mellan 100 och 899.", ephemeral=True)
            return
        await change_page(interaction, int(value))


class PanelView(discord.ui.View):
    def __init__(self, show_stop: bool = True) -> None:
        super().__init__(timeout=None)
        if not show_stop:
            # Fästa paneler är tänkta att vara kvar; ta bort fästningen för att få tillbaka Stoppa.
            self.remove_item(self.stop_panel)

    def _current(self, interaction: discord.Interaction) -> int:
        return panels.get(interaction.message.id, {}).get("page", 100)

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.secondary, custom_id="texttv:prev")
    async def prev(self, interaction: discord.Interaction, _):
        cached = page_cache.get(self._current(interaction))
        target = cached[1].prev_page if cached else self._current(interaction) - 1
        await change_page(interaction, target)

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.secondary, custom_id="texttv:next")
    async def next(self, interaction: discord.Interaction, _):
        cached = page_cache.get(self._current(interaction))
        target = cached[1].next_page if cached else self._current(interaction) + 1
        await change_page(interaction, target)

    @discord.ui.button(label="Gå till sida", emoji="🔢", style=discord.ButtonStyle.primary, custom_id="texttv:goto")
    async def goto(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(GotoModal())

    @discord.ui.button(label="100", emoji="🏠", style=discord.ButtonStyle.secondary, custom_id="texttv:home")
    async def home(self, interaction: discord.Interaction, _):
        await change_page(interaction, 100)

    @discord.ui.button(label="Stoppa", emoji="⏹", style=discord.ButtonStyle.danger, custom_id="texttv:stop")
    async def stop_panel(self, interaction: discord.Interaction, _):
        perms = interaction.permissions
        if not (perms.manage_messages or perms.administrator):
            await interaction.response.send_message("Bara de som får hantera meddelanden kan stoppa panelen.",
                                                    ephemeral=True)
            return
        panels.pop(interaction.message.id, None)
        save_panels()
        await interaction.response.edit_message(view=None)
        await interaction.followup.send("Panelen är stoppad och uppdateras inte längre.", ephemeral=True)


# --------------------------------------------------------------------------- kommandon

@client.tree.command(name="texttv", description="Starta en Text-TV-panel som uppdateras automatiskt i den här kanalen")
@app_commands.describe(sida="Sidnummer att börja på (100–899)")
@app_commands.default_permissions(manage_messages=True)
@app_commands.guild_only()
async def texttv_command(interaction: discord.Interaction, sida: app_commands.Range[int, 100, 899] = 100):
    await interaction.response.defer(ephemeral=True, thinking=True)
    panel = {"channel_id": interaction.channel_id, "page": sida, "sub": 0}
    try:
        kwargs, key = await client.build_message(panel)
    except Exception:
        log.exception("Kunde inte hämta första sidan")
        await interaction.followup.send("Kunde inte hämta Text-TV just nu, försök igen om en stund.")
        return
    try:
        message = await interaction.channel.send(**kwargs)
    except discord.Forbidden:
        await interaction.followup.send(
            "Jag saknar behörighet här. Jag behöver *Skicka meddelanden* och *Läsa meddelandehistorik*.")
        return
    panel["key"] = key
    panels[message.id] = panel
    save_panels()
    try:
        await message.pin(reason="Text-TV-panel")
        pin_note = "Meddelandet är fäst i kanalen."
        await set_pinned(message.id, True)
    except discord.HTTPException:
        pin_note = "Jag kunde inte fästa meddelandet. Ge mig behörigheten *Fäst meddelanden* eller fäst det själv."
    await interaction.followup.send(
        f"Text-TV-panelen är igång och uppdateras var {UPDATE_MINUTES}:e minut. {pin_note}")


async def set_pinned(message_id: int, pinned: bool) -> None:
    """Visar eller döljer Stoppa-knappen beroende på om panelen är fäst."""
    panel = panels.get(message_id)
    if panel is None or panel.get("pinned", False) == pinned:
        return
    panel["pinned"] = pinned
    save_panels()
    lock = panel_locks.setdefault(message_id, asyncio.Lock())
    async with lock:
        channel = client.get_channel(panel["channel_id"]) or await client.fetch_channel(panel["channel_id"])
        await channel.get_partial_message(message_id).edit(view=PanelView(show_stop=not pinned))


async def sync_pinned(message_id: int) -> None:
    """Läser av om panelens meddelande är fäst just nu."""
    panel = panels[message_id]
    channel = client.get_channel(panel["channel_id"]) or await client.fetch_channel(panel["channel_id"])
    message = await channel.fetch_message(message_id)
    await set_pinned(message_id, message.pinned)


@client.event
async def on_guild_channel_pins_update(channel, last_pin) -> None:
    # Discord säger bara att något fästes eller lossades i kanalen, så kolla kanalens paneler.
    for message_id, panel in list(panels.items()):
        if panel["channel_id"] == channel.id:
            try:
                await sync_pinned(message_id)
            except discord.HTTPException as e:
                handle_panel_error(message_id, e)


@client.event
async def on_ready() -> None:
    log.info("Inloggad som %s, %d aktiva paneler", client.user, len(panels))
    # Rita om alla paneler direkt, så att de visar senaste sidan och utseendet efter en omstart.
    for message_id in list(panels):
        try:
            await sync_pinned(message_id)
        except discord.HTTPException as e:
            # Kräver Läsa meddelandehistorik; utan den behålls senast kända fästning.
            handle_panel_error(message_id, e)
        try:
            await update_panel(message_id, force=True)
        except discord.HTTPException as e:
            handle_panel_error(message_id, e)
        except Exception:
            log.exception("Kunde inte uppdatera panel %s", message_id)


def main() -> None:
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN saknas. Kopiera .env.example till .env och fyll i din token.")
    discord.utils.setup_logging(level=logging.INFO)
    client.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
