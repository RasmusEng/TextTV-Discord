"""Hämtar SVT Text-TV-sidor (via texttv.nu:s API) och ritar dem som färgad text
(ANSI + blocktecken) som Discord kan visa i ett ```ansi-kodblock."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import aiohttp
from PIL import Image

API_URL = "https://api.texttv.nu/api/get/{page}"
CHAR_HOST = "https://l.texttv.nu/"  # mosaikgrafiken hämtas bara härifrån
CHAR_CACHE_DIR = Path(os.getenv("DATA_DIR") or Path(__file__).parent) / "cache" / "chars"

COLS, ROWS = 40, 24

# Text-TV:s färger (klassnamn från texttv.nu). "bl" = svart text, "Bl" = svart bakgrund.
COLORS = {
    "W": (255, 255, 255),
    "Y": (255, 255, 0),
    "C": (0, 255, 255),
    "G": (0, 255, 0),
    "M": (255, 0, 255),
    "R": (255, 0, 0),
    "B": (0, 0, 255),
    "bl": (0, 0, 0),
    "Bl": (0, 0, 0),
}
BLACK = {"bl", "Bl"}
DEFAULT_FG, DEFAULT_BG = "W", "Bl"

# Discords ANSI-färger. Textfärgerna finns för alla Text-TV-färger,
# men bakgrundsfärgerna är få och ungefärliga.
FG_ANSI = {"W": 37, "Y": 33, "C": 36, "G": 32, "M": 35, "R": 31, "B": 34, "bl": 30, "Bl": 30}
# Discord saknar ren blå bakgrund: 40 (mörk marinblå) ligger närmast Text-TV:s blå,
# medan 45 (indigo) ser lila/rosa ut. Övriga bakgrundsfärger ritas med █-block.
BG_ANSI = {"B": 40, "R": 41, "W": 47}

# Blocktecken per 2x2-mönster: bit 1 = uppe vänster, 2 = uppe höger, 4 = nere vänster,
# 8 = nere höger. Bara ▀▄▌▐█ används, eftersom de finns i nästan alla typsnitt och
# därför alltid får rätt bredd. Kvadrantblock (▘▚▟ …) saknas ofta och förskjuter raderna.
QUADRANTS = " ▀▀▀▄▌▀█▄▄▐█▄███"

DISCORD_LIMIT = 4096  # max längd på en embed-beskrivning


@dataclass
class Page:
    num: int
    title: str
    subpages: list[str]  # HTML (eller ren text) per undersida
    updated: int  # unix-tid
    next_page: int
    prev_page: int
    section: str = ""  # avdelning, t.ex. "Inrikes" eller "Sport"


@dataclass
class Cell:
    ch: str
    fg: str
    bg: str
    img: str | None  # URL till mosaikgrafik, om cellen är grafik


# --------------------------------------------------------------------------- hämtning

async def fetch_page(session: aiohttp.ClientSession, page: int, app: str) -> Page:
    async with session.get(API_URL.format(page=page), params={"app": app},
                           timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    if not data:
        raise ValueError(f"Sidan {page} finns inte")
    d = data[0]
    crumbs = [c.get("name", "") for c in d.get("breadcrumbs") or [] if isinstance(c, dict)]
    return Page(
        num=int(d.get("num") or page),
        title=(d.get("title") or "").strip(),
        subpages=d.get("content") or [""],
        updated=int(d.get("date_updated_unix") or 0),
        next_page=int(d.get("next_page") or page + 1),
        prev_page=int(d.get("prev_page") or page - 1),
        section=crumbs[1] if len(crumbs) > 1 else "",
    )


# Varje mosaikbild (13x16 px) görs om till ett rutnät av Text-TV-färger.
_char_grids: dict[str, list[list[str]]] = {}


def _nearest_color(rgb: tuple[int, int, int]) -> str:
    return min((k for k in COLORS if k != "bl"),
               key=lambda k: sum((a - b) ** 2 for a, b in zip(rgb, COLORS[k], strict=True)))


async def _load_char(session: aiohttp.ClientSession, url: str) -> None:
    if url in _char_grids or not url.startswith(CHAR_HOST):
        return
    path = CHAR_CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".gif")
    if not path.exists():
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            raw = await resp.read()
        CHAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    with Image.open(path) as im:
        im = im.convert("RGB")
        _char_grids[url] = [[_nearest_color(im.getpixel((x, y))) for x in range(im.width)]
                            for y in range(im.height)]


async def load_chars(session: aiohttp.ClientSession, html: str) -> None:
    urls = {c.img for row, _ in parse(html) for c in row if c.img}
    # En saknad grafikbit ska inte stoppa hela sidan; den ritas som tom cell.
    await asyncio.gather(*(_load_char(session, u) for u in urls), return_exceptions=True)


# --------------------------------------------------------------------------- tolkning

class _LineParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, str, str | None]] = []
        self.cells: list[Cell] = []
        self.double = False

    def _current(self) -> tuple[str, str, str | None]:
        return self.stack[-1] if self.stack else (DEFAULT_FG, DEFAULT_BG, None)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        fg, bg, _ = self._current()
        img = None
        classes = (a.get("class") or "").split()
        if "DH" in classes:
            self.double = True
        for c in classes:
            if c.startswith("bg") and c[2:] in COLORS:
                bg = c[2:]
            elif c in COLORS:
                fg = c
        if "bgImg" in classes:
            m = re.search(r"url\(([^)]+)\)", a.get("style") or "")
            if m:
                img = m.group(1).strip("'\" ")
        self.stack.append((fg, bg, img))

    def handle_endtag(self, tag):
        if self.stack:
            self.stack.pop()

    def handle_data(self, data):
        fg, bg, img = self._current()
        for ch in data:
            if ch not in "\r\n":
                self.cells.append(Cell(ch, fg, bg, img))


def parse(html: str) -> list[tuple[list[Cell], bool]]:
    """Delar upp en sida i 24 rader à 40 celler. Returnerar (celler, dubbelhöjd)."""
    rows = []
    for line in html.split("\n")[:ROWS]:
        p = _LineParser()
        p.feed(line)
        p.close()
        cells = p.cells[:COLS]
        cells += [Cell(" ", DEFAULT_FG, cells[-1].bg if cells else DEFAULT_BG, None)] * (COLS - len(cells))
        rows.append((cells, p.double))
    while len(rows) < ROWS:
        rows.append(([Cell(" ", DEFAULT_FG, DEFAULT_BG, None)] * COLS, False))
    return rows


# --------------------------------------------------------------------------- ritning

def _mosaic(cell: Cell) -> tuple[str, int] | None:
    """Gör om en mosaikbild till (förgrundsfärg, kvadrantmask)."""
    grid = _char_grids.get(cell.img or "")
    if grid is None:
        return None
    bg = "Bl" if cell.bg in BLACK else cell.bg
    counts = Counter(c for row in grid for c in row if c != bg)
    if not counts:
        return cell.fg, 0
    on = counts.most_common(1)[0][0]
    h, w = len(grid), len(grid[0])
    mask = 0
    for bit, (x0, x1, y0, y1) in enumerate([(0, w // 2, 0, h // 2), (w // 2, w, 0, h // 2),
                                            (0, w // 2, h // 2, h), (w // 2, w, h // 2, h)]):
        lit = sum(grid[y][x] == on for y in range(y0, y1) for x in range(x0, x1))
        if lit / ((x1 - x0) * (y1 - y0)) > 0.35:
            mask |= 1 << bit
    return on, mask


def _cell_to_ansi(cell: Cell, use_bg: bool) -> tuple[str, int | None, int | None]:
    """Returnerar (tecken, textfärgskod, bakgrundskod) för en cell."""
    bg = cell.bg
    bg_code = BG_ANSI.get(bg) if use_bg else None

    def solid(color: str) -> tuple[str, int | None, int | None]:
        # En helt ifylld cell i en färg.
        if color in BLACK:
            return " ", None, None
        if bg_code and color == bg:
            return " ", None, bg_code
        return "█", FG_ANSI[color], None

    if cell.img:
        m = _mosaic(cell)
        if m is None:
            return solid(bg)
        on, mask = m
        if mask == 0:
            return solid(bg)
        if mask == 15:
            return solid(on)
        if bg in BLACK:
            return QUADRANTS[mask], FG_ANSI[on], None
        if on in BLACK:  # svart grafik på färgad botten: rita botten i stället
            return QUADRANTS[~mask & 15], FG_ANSI[bg], None
        return QUADRANTS[mask], FG_ANSI[on], bg_code

    ch = "ˋ" if cell.ch == "`" else cell.ch  # backticks skulle bryta kodblocket
    if ch.isspace():
        return solid(bg)
    if bg in BLACK:
        return ch, FG_ANSI[cell.fg], None
    if bg_code:
        return ch, FG_ANSI[cell.fg], bg_code
    # Färgad botten som Discord saknar: visa texten i bottenfärgen om den annars är svart.
    return ch, FG_ANSI[bg if cell.fg in BLACK else cell.fg], None


# --------------------------------------------------------------------------- banners
#
# Rubrikbanners (t.ex. "INRIKES", "SPORT") är ritade med Text-TV-grafik i 2x3-rutor
# per tecken. De blir oläsliga när de pressas ihop, så de ritas i stället om från
# grunden med ett eget pixeltypsnitt. Varje tecken i Discord blir 1 pixel brett och
# 2 pixlar högt (▀ ▄ █), vilket ger stora, skarpa bokstäver.

# Pixeltypsnitt, 5 rader högt. Rad 0 (prickar/ring för ÅÄÖ) läggs till automatiskt.
_FONT_SRC = {
    "A": ".##.|#..#|####|#..#|#..#",   "B": "###.|#..#|###.|#..#|###.",
    "C": ".###|#...|#...|#...|.###",   "D": "###.|#..#|#..#|#..#|###.",
    "E": "####|#...|###.|#...|####",   "F": "####|#...|###.|#...|#...",
    "G": ".###|#...|#.##|#..#|.###",   "H": "#..#|#..#|####|#..#|#..#",
    "I": "###|.#.|.#.|.#.|###",        "J": "..##|...#|...#|#..#|.##.",
    "K": "#..#|#.#.|##..|#.#.|#..#",   "L": "#...|#...|#...|#...|####",
    "M": "#...#|##.##|#.#.#|#...#|#...#", "N": "#..#|##.#|#.##|#..#|#..#",
    "O": ".##.|#..#|#..#|#..#|.##.",   "P": "###.|#..#|###.|#...|#...",
    "Q": ".##.|#..#|#..#|#.#.|.#.#",   "R": "###.|#..#|###.|#.#.|#..#",
    "S": ".###|#...|.##.|...#|###.",   "T": "###|.#.|.#.|.#.|.#.",
    "U": "#..#|#..#|#..#|#..#|.##.",   "V": "#...#|#...#|.#.#.|.#.#.|..#..",
    "W": "#...#|#...#|#.#.#|##.##|#...#", "X": "#..#|#..#|.##.|#..#|#..#",
    "Y": "#.#|#.#|.#.|.#.|.#.",        "Z": "####|..#.|.#..|#...|####",
    "0": ".##.|#..#|#..#|#..#|.##.",   "1": ".#.|##.|.#.|.#.|###",
    "2": "###.|...#|.##.|#...|####",   "3": "###.|...#|.##.|...#|###.",
    "4": "#..#|#..#|####|...#|...#",   "5": "####|#...|###.|...#|###.",
    "6": ".##.|#...|###.|#..#|.##.",   "7": "####|...#|..#.|.#..|.#..",
    "8": ".##.|#..#|.##.|#..#|.##.",   "9": ".##.|#..#|.###|...#|.##.",
    " ": "..|..|..|..|..",             "-": "...|...|###|...|...",
}
_ACCENTS = {"Å": "A", "Ä": "A", "Ö": "O", "Ü": "U"}
# Två pixlar på höjden per tecken: (övre, nedre) -> blocktecken.
_HALF_BLOCKS = {(False, False): " ", (True, False): "▀", (False, True): "▄", (True, True): "█"}


def _label_bitmap(label: str) -> list[str] | None:
    """Ritar en text som 6 pixelrader. None om något tecken saknas i typsnittet."""
    rows = [""] * 6
    for ch in label.upper():
        base = _ACCENTS.get(ch, ch)
        src = _FONT_SRC.get(base)
        if src is None:
            return None
        body = src.split("|")
        w = len(body[0])
        if ch == "Å":
            accent = "." * ((w - 2) // 2) + "##" + "." * (w - 2 - (w - 2) // 2)
        elif ch in _ACCENTS:
            accent = "#" + "." * (w - 2) + "#"
        else:
            accent = "." * w
        for i, part in enumerate([accent] + body):
            rows[i] += part + "."
    return [r[:-1] for r in rows]


def _find_banner(rows: list[tuple[list[Cell], bool]]) -> int:
    """Hittar en grafisk rubrikbanner överst på sidan. Returnerar antal rader (0 = ingen)."""
    counts = []
    for y in range(1, 5):
        n = sum(1 for c in rows[y][0] if c.img)
        if n < 4:
            break
        counts.append(n)
    return len(counts) if sum(n >= 10 for n in counts) >= 2 else 0


def _banner_cells(rows: list[tuple[list[Cell], bool]], height: int, label: str,
                  use_bg: bool) -> list[list[tuple]]:
    """Ritar om bannern med ren pixeltext. Returnerar (tecken, fg, bg) per cell och rad."""
    banner = [rows[y][0] for y in range(1, 1 + height)]
    bg_counts = Counter(c.bg for r in banner for c in r if c.bg not in BLACK)
    bg = bg_counts.most_common(1)[0][0] if bg_counts else "B"
    fg = "B" if bg == "W" else "W"
    bg_code = BG_ANSI.get(bg) if use_bg else None

    bitmap = _label_bitmap(label) if height >= 3 else None
    if bitmap and len(bitmap[0]) > COLS - 2:
        bitmap = None
    x0 = 2 if bitmap and len(bitmap[0]) <= COLS - 4 else 1  # vänstermarginal i tecken

    # Pixelrutnät: 1 pixel per tecken på bredden, 2 på höjden.
    px_h = height * 2
    grid = [[False] * COLS for _ in range(px_h)]
    label_end_col = 0
    if bitmap:
        y0 = (px_h - 6) // 2
        for gy, line in enumerate(bitmap):
            for gx, p in enumerate(line):
                if p == "#":
                    grid[y0 + gy][x0 + gx] = True
        label_end_col = x0 + len(bitmap[0]) + 1

    def plain(ch: str, color: str) -> tuple:
        if bg_code:
            return ch, FG_ANSI[color], bg_code
        return ch, FG_ANSI[bg if color in BLACK else color], None

    def kept_text(cells: list[Cell]) -> dict[int, Cell]:
        """Vanlig text i bannern (t.ex. "SVT Text") som får plats till höger om rubriken.
        Ord som skulle krocka med rubriken flyttas åt höger, eller tas bort om de inte ryms."""
        kept: dict[int, Cell] = {}
        col = 0
        while col < COLS:
            if cells[col].img or cells[col].ch.isspace():
                col += 1
                continue
            start = col
            # En textbit = tecken med högst ett mellanslag emellan.
            while col < COLS and not cells[col].img and not (
                    cells[col].ch.isspace() and (col + 1 >= COLS or cells[col + 1].ch.isspace())):
                col += 1
            run = cells[start:col]
            pos = max(start, label_end_col)
            if pos + len(run) <= COLS:
                kept.update({pos + i: c for i, c in enumerate(run)})
        return kept

    out = []
    for r in range(height):
        line = []
        kept = kept_text(banner[r])
        for col in range(COLS):
            top, bottom = grid[r * 2][col], grid[r * 2 + 1][col]
            orig = kept.get(col)
            if col == 0 and banner[r][0].bg in BLACK and not (top or bottom):
                # Första kolumnen är svart på Text-TV; då linjerar bannern med raderna under.
                line.append((" ", None, None))
            elif not (top or bottom) and orig is not None:
                tfg = orig.fg if orig.fg not in BLACK and orig.fg != bg else fg
                line.append(plain("ˋ" if orig.ch == "`" else orig.ch, tfg))
            elif bg_code:
                ch = _HALF_BLOCKS[(top, bottom)]
                line.append((ch, FG_ANSI[fg], bg_code) if ch != " " else (" ", None, bg_code))
            else:
                # Ingen passande bakgrundsfärg: rita bannern som block och bokstäverna som hål.
                ch = _HALF_BLOCKS[(not top, not bottom)]
                line.append((ch, FG_ANSI[bg], None) if ch != " " else (" ", None, None))
        out.append(line)

    if not bitmap:
        # För låg banner eller tecken som saknas i typsnittet: skriv avdelningen som text.
        text = " " + (" ".join(label.upper()) if len(label) * 2 < COLS - 2 else label.upper())
        row = out[(height - 1) // 2]
        for i, ch in enumerate(text[:COLS]):
            if not ch.isspace():
                row[i] = plain(ch, fg)
    return out


# --------------------------------------------------------------------------- ANSI-text

def _encode_line(line_cells: list[tuple], color: bool) -> str:
    # Ta bort tomma celler i slutet av raden för att spara tecken.
    while line_cells and line_cells[-1][0] == " " and line_cells[-1][2] is None:
        line_cells = line_cells[:-1]
    parts: list[str] = []
    state: tuple[int | None, int | None] = (None, None)
    for ch, fg, bg in line_cells:
        if color:
            wanted = (fg if fg is not None else state[0], bg)
            if ch == " " and bg is None:
                wanted = (state[0], None)
            if wanted != state:
                codes = ["0"] + [str(c) for c in wanted if c is not None]
                parts.append(f"\x1b[{';'.join(codes)}m")
                state = wanted
        parts.append(ch)
    if color and state != (None, None):
        parts.append("\x1b[0m")
    return "".join(parts)


def _graphic_fill(line: list[tuple], cells: list[Cell]) -> list[tuple]:
    """Färgade ytor som hör till en bild (t.ex. Norrland på väderkartan) ritas som block
    i textfärgen, så att de får samma nyans som bildens kanter. Med Discords
    bakgrundsfärger skulle blått bli nästan svart, vitt krämfärgat och rött orange.
    En yta räknas som bild om den saknar text och gränsar till grafik. Färgade rader med
    text (rubrikrader) behåller bakgrunden så att texten syns."""
    line = list(line)
    x = 0
    while x < COLS:
        bg = cells[x].bg
        if bg in BLACK:
            x += 1
            continue
        start = x
        while x < COLS and cells[x].bg == bg:
            x += 1
        run = cells[start:x]
        has_text = any(not c.img and not c.ch.isspace() for c in run)
        touches_graphic = any(c.img for c in run) or (start > 0 and cells[start - 1].img) or (
            x < COLS and cells[x].img)
        if has_text or not touches_graphic:
            continue
        for i in range(start, x):
            if not cells[i].img:
                line[i] = ("█", FG_ANSI[bg], None)
    return line


def _build(rows: list[tuple[list[Cell], bool]], label: str, *, color: bool, use_bg: bool) -> str:
    out = ["```ansi"]
    banner_h = _find_banner(rows) if label else 0
    y = 0
    while y < ROWS:
        if banner_h and y == 1:
            out.extend(_encode_line(line, color) for line in _banner_cells(rows, banner_h, label, use_bg))
            y += banner_h
            continue
        cells, double = rows[y]
        out.append(_encode_line(_graphic_fill([_cell_to_ansi(c, use_bg) for c in cells], cells), color))
        # Dubbel höjd går inte i text; raden under (som täcks av den) blir tom.
        if double and y + 1 < ROWS:
            out.append("")
            y += 2
        else:
            y += 1
    out.append("```")
    return "\n".join(out)


def render_ansi(html: str, limit: int = DISCORD_LIMIT, label: str = "") -> str:
    """Ritar en sida som ett ```ansi-kodblock. Anropa load_chars() först.

    label är texten som ritas i sidans rubrikbanner (t.ex. "Sport") om sidan har en.
    Blir texten för lång för Discord tas först bakgrundsfärgerna bort och sist alla färger."""
    rows = parse(html)
    text = ""
    for opts in ({"color": True, "use_bg": True}, {"color": True, "use_bg": False},
                 {"color": False, "use_bg": False}):
        text = _build(rows, label, **opts)
        if len(text) <= limit:
            return text
    return text[: limit - 4] + "\n```"


async def render_page(session: aiohttp.ClientSession, html: str, limit: int = DISCORD_LIMIT,
                      label: str = "") -> str:
    await load_chars(session, html)
    return render_ansi(html, limit, label)
