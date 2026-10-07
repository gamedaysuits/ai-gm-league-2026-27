"""Article images, drawn with Pillow: the week's cover (1200x630, also the social card), the standings chart, and a
quote card per featured line. Model names come first on every image; the GM's portrait is its Draft Night avatar."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from gmbench.coverage.facts import Line, WeekFacts

HERE = Path(__file__).parent
FONTS, GMS = HERE / "fonts", HERE / "gms"
NAVY, NAVY2, INK, MUTED, GOLD, RED, GREEN = "#0e1b4d", "#16255f", "#ffffff", "#aeb6d3", "#f2c14e", "#ff6b6b", "#5ee6a0"
W, H = 1200, 630


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / f"{name}.ttf"), size)


def bold(size: int) -> ImageFont.FreeTypeFont:
    return _font("Archivo-ExtraBold", size)


def medium(size: int) -> ImageFont.FreeTypeFont:
    return _font("Archivo-Medium", size)


def body(size: int) -> ImageFont.FreeTypeFont:
    return _font("Questrial-Regular", size)


def portrait(team: str, size: int) -> Image.Image:
    path = GMS / f"{team}.png"
    if not path.exists():
        path = GMS / "autodraft.png"
    return Image.open(path).convert("RGBA").resize((size, size), Image.NEAREST)


def _rounded(im: Image.Image, radius: int) -> Image.Image:
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.size[0] - 1, im.size[1] - 1), radius, fill=255)
    out = Image.new("RGBA", im.size, (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    return out


def _base(h: int = H) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    im = Image.new("RGB", (W, h), NAVY)
    d = ImageDraw.Draw(im)
    for y in range(h):  # a quiet vertical gradient
        t = y / max(1, h - 1)
        d.line([(0, y), (W, y)], fill=(int(14 + 8 * t), int(27 + 10 * t), int(77 + 18 * t)))
    return im, d


def _fit(d: ImageDraw.ImageDraw, text: str, font_fn, size: int, max_w: int, min_size: int = 18):
    while size > min_size and d.textlength(text, font=font_fn(size)) > max_w:
        size -= 2
    return font_fn(size)


def _wrap(d: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if cur and d.textlength(trial, font=font) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    return lines + ([cur] if cur else [])


def _block(d: ImageDraw.ImageDraw, text: str, font_fn, size: int, max_w: int, max_h: int, lead: float = 1.25,
           min_size: int = 20) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
    """The biggest size (<= size) at which ``text`` wraps into max_w x max_h. -> (font, lines, line height)."""
    while True:
        font = font_fn(size)
        lines = _wrap(d, text, font, max_w)
        step = int(size * lead)
        if len(lines) * step <= max_h or size <= min_size:
            return font, lines, step
        size -= 2


def _footer(d: ImageDraw.ImageDraw, h: int, right: str) -> None:
    d.text((48, h - 46), "AI GM LEAGUE 2026–27 · GAME DAY SUITS", font=medium(20), fill=MUTED)
    d.text((W - 48, h - 46), right, font=medium(20), fill=MUTED, anchor="ra")


def cover(f: WeekFacts, headline: str, out: Path) -> Path:
    im, d = _base()
    d.text((48, 44), f"WEEK {f.number}", font=bold(96), fill=GOLD)
    span = f"{f.week_start:%b %-d} – {f.week_end:%b %-d, %Y}".upper()
    d.text((52, 150), f"AI FANTASY HOCKEY · {span}", font=medium(24), fill=MUTED)
    font, lines, step = _block(d, headline, bold, 64, 620, 300, lead=1.15)
    y = 200
    for ln in lines:
        d.text((48, y), ln, font=font, fill=INK)
        y += step
    top = f.standings[:3]
    x0 = 720
    for i, s in enumerate(top):
        t = f.teams[s["team"]]
        size = 200 if i == 0 else 150
        px, py = (x0 + 125, 60) if i == 0 else ((x0 + 10, 330) if i == 1 else (x0 + 250, 330))
        card = _rounded(portrait(t.id, size), 18)
        im.paste(card, (px, py), card)
        d.rounded_rectangle((px - 4, py - 4, px + size + 3, py + size + 3), 20, outline=GOLD if i == 0 else MUTED, width=3)
        label = f"{s['rank']}. {t.display}"
        fnt = _fit(d, label, bold, 26 if i == 0 else 22, size + 60)
        d.text((px + size / 2, py + size + 12), label, font=fnt, fill=INK, anchor="ma")
        d.text((px + size / 2, py + size + 42), f"{s['points']} PTS", font=medium(20), fill=GOLD if i == 0 else MUTED, anchor="ma")
    _footer(d, H, "STANDINGS · TRADES · TRASH TALK")
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out, optimize=True)
    return out


def standings_chart(f: WeekFacts, out: Path) -> Path:
    rows = f.standings
    row_h, top = 40, 118
    h = top + row_h * len(rows) + 70
    im, d = _base(h)
    d.text((48, 36), f"STANDINGS AFTER WEEK {f.number - 1}", font=bold(40), fill=INK)
    d.text((48, 84), "Total fantasy points · gold = points last week", font=body(22), fill=MUTED)
    most = max([s["points"] for s in rows] + [1])
    bar_x, bar_w = 470, 600
    for i, s in enumerate(rows):
        t = f.teams[s["team"]]
        y = top + i * row_h
        av = _rounded(portrait(t.id, 32), 6)
        im.paste(av, (48, y + 2), av)
        d.text((92, y + 5), f"{s['rank']}.", font=medium(22), fill=MUTED)
        name = t.display if not t.bot else "Autodraft (robot)"
        d.text((132, y + 5), name, font=_fit(d, name, medium, 22, 320), fill=INK)
        total = int(bar_w * s["points"] / most)
        recent = int(bar_w * max(0, s["week_points"]) / most)
        d.rounded_rectangle((bar_x, y + 6, bar_x + max(total, 4), y + 30), 5, fill=t.primary if t.primary.lower() != NAVY else NAVY2)
        d.rounded_rectangle((bar_x + max(total - recent, 0), y + 6, bar_x + max(total, 4), y + 30), 5, fill=GOLD)
        d.text((bar_x + max(total, 4) + 10, y + 5), f"{s['points']}", font=bold(22), fill=INK)
    _footer(d, h, f"THROUGH {f.week_start - timedelta(days=1):%b %-d}".upper())
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out, optimize=True)
    return out


def quote_card(f: WeekFacts, line: Line, out: Path) -> Path:
    t = f.teams[line.team]
    im, d = _base()
    d.rectangle((0, 0, 14, H), fill=t.primary)
    card = _rounded(portrait(t.id, 360), 24)
    im.paste(card, (60, 110), card)
    d.text((48, 40), f"WEEK {f.number} · {'LEAGUE CHAT' if line.kind == 'chat' else 'PRESSER' if line.kind == 'presser' else 'TRADE TALK'}",
           font=medium(24), fill=GOLD)
    d.text((460, 96), "“", font=bold(140), fill=GOLD)
    font, lines, step = _block(d, line.text, body, 40, 680, 270, min_size=22)
    y = 200
    for ln in lines:
        d.text((470, y), ln, font=font, fill=INK)
        y += step
    d.text((470, 500), t.display, font=_fit(d, t.display, bold, 34, 680), fill=INK)
    sub = f"{t.lab}" + (f" · GM {t.gm}, {t.franchise}" if t.gm and t.franchise else "")
    d.text((470, 542), sub, font=_fit(d, sub, medium, 22, 680), fill=MUTED)
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out, optimize=True)
    return out
