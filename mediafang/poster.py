"""Generate a 2:3 show poster from an episode thumbnail, with the show name on it.

Every show from one creator would otherwise get the same poster (the creator's
avatar), which makes them impossible to tell apart in Plex.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

W, H = 1000, 1500
MARGIN = 70

_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Debian, fonts-dejavu-core
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",  # macOS
    "/Library/Fonts/Arial Bold.ttf",
]


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONTS:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for word in text.split():
        if lines and draw.textlength(f"{lines[-1]} {word}", font=font) <= max_width:
            lines[-1] += f" {word}"
        else:
            lines.append(word)
    return lines


def _fit_title(draw: ImageDraw.ImageDraw, text: str, max_width: int, max_lines: int = 4):
    """Largest font size where the title fits in max_lines without overflowing."""
    for size in range(150, 40, -6):
        font = _font(size)
        lines = _wrap(draw, text, font, max_width)
        if len(lines) <= max_lines and all(draw.textlength(line, font=font) <= max_width for line in lines):
            return font, lines
    font = _font(40)
    return font, _wrap(draw, text, font, max_width)


def make_poster(thumbnail: Path, title: str, subtitle: str, out: Path) -> None:
    src = Image.open(thumbnail).convert("RGB")

    # Blurred, darkened full-bleed background
    bg = ImageOps.fit(src, (W, H), method=Image.Resampling.LANCZOS)
    bg = bg.filter(ImageFilter.GaussianBlur(40))
    bg = ImageEnhance.Brightness(bg).enhance(0.45)

    # Sharp frame from the episode in the upper part
    frame = ImageOps.contain(src, (W - 2 * MARGIN, 700), method=Image.Resampling.LANCZOS)
    bg.paste(frame, ((W - frame.width) // 2, 150))

    draw = ImageDraw.Draw(bg)
    font, lines = _fit_title(draw, title, W - 2 * MARGIN)
    line_h = int(font.size * 1.12)
    sub_font = _font(46)
    block_h = line_h * len(lines) + 30 + sub_font.size
    y = 150 + frame.height + (H - 150 - frame.height - block_h) // 2
    for line in lines:
        x = (W - draw.textlength(line, font=font)) // 2
        draw.text((x, y), line, font=font, fill="white", stroke_width=3, stroke_fill="black")
        y += line_h
    y += 30
    x = (W - draw.textlength(subtitle, font=sub_font)) // 2
    draw.text((x, y), subtitle, font=sub_font, fill=(230, 200, 120), stroke_width=2, stroke_fill="black")

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.jpg")
    bg.save(tmp, "JPEG", quality=90)
    tmp.replace(out)
