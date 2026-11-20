"""Snabba tester för renderingen som inte behöver nätverk."""
import texttv

SAMPLE = (
    '<div class="root"><span class="line toprow"> 100 SVT Text   Fredag 20 nov 2026</span>\n'
    '<span class="line"><span class="Y">Inrikes nyheter</span></span>\n'
    '<span class="line"><span class="W bgB">Vitt på blått</span></span></div>'
)


def test_parse_gives_full_grid():
    rows = texttv.parse(SAMPLE)
    assert len(rows) == texttv.ROWS
    assert all(len(cells) == texttv.COLS for cells, _ in rows)


def test_render_is_ansi_block_within_limit():
    out = texttv.render_ansi(SAMPLE, limit=2000)
    assert out.startswith("```ansi")
    assert out.rstrip().endswith("```")
    assert len(out) <= 2000
    assert "Inrikes nyheter" in out


def test_render_falls_back_when_limit_is_small():
    out = texttv.render_ansi(SAMPLE, limit=400)
    assert len(out) <= 400


def test_label_bitmap():
    bitmap = texttv._label_bitmap("SPORT")
    assert bitmap is not None and len(bitmap) == 6
    assert texttv._label_bitmap("€") is None
