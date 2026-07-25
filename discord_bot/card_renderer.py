from __future__ import annotations

import os
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from constants import ATTRIBUTES, RANK_FILES, RANKS


@lru_cache(maxsize=64)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    configured = os.getenv("RATTEN_FONT_BOLD" if bold else "RATTEN_FONT_REGULAR", "").strip()
    filename = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    candidates = [
        Path(configured) if configured else None,
        Path(__file__).resolve().parent / "assets" / "fonts" / filename,
        Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return ImageFont.truetype(str(candidate), size=size)
    raise RuntimeError(
        "Не найден шрифт с поддержкой кириллицы. "
        "Установите fonts-dejavu-core или задайте RATTEN_FONT_REGULAR и RATTEN_FONT_BOLD."
    )


def _fit_text(draw: ImageDraw.ImageDraw, text: str, box_width: int, start_size: int, bold: bool = False):
    for size in range(start_size, 10, -1):
        font = _font(size, bold)
        if draw.textbbox((0, 0), text, font=font)[2] <= box_width:
            return font
    return _font(10, bold)


def _rotated_centered_text(image: Image.Image, text: str, center: tuple[int, int], width: int, angle: float, size: int, fill, bold: bool = False):
    measure = ImageDraw.Draw(image)
    font = _fit_text(measure, text, width, size, bold)
    box = measure.textbbox((0, 0), text, font=font)
    layer = Image.new("RGBA", (width + 30, max(60, box[3] - box[1] + 24)), (0, 0, 0, 0))
    layer_draw = ImageDraw.Draw(layer)
    x = (layer.width - (box[2] - box[0])) // 2
    y = (layer.height - (box[3] - box[1])) // 2 - box[1]
    layer_draw.text((x, y), text, fill=fill, font=font)
    rotated = layer.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC)
    image.alpha_composite(rotated, (center[0] - rotated.width // 2, center[1] - rotated.height // 2))


def _wrapped_lines(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in (text or "").splitlines() or [""]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if draw.textbbox((0, 0), candidate, font=font)[2] <= width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def _draw_notes(draw: ImageDraw.ImageDraw, text: str, box: tuple[int, int, int, int], fill, scale: int = 1) -> None:
    left, top, right, bottom = box
    for size in range(12, 6, -1):
        font = _font(size * scale)
        lines = _wrapped_lines(draw, text, font, right - left)
        spacing = max(2, size // 4) * scale
        line_height = draw.textbbox((0, 0), "Ау", font=font)[3] + spacing
        if len(lines) * line_height <= bottom - top:
            for index, line in enumerate(lines):
                draw.text((left, top + index * line_height), line, fill=fill, font=font)
            return

    font = _font(7 * scale)
    lines = _wrapped_lines(draw, text, font, right - left)
    line_height = draw.textbbox((0, 0), "Ау", font=font)[3] + 2 * scale
    max_lines = max(1, (bottom - top) // line_height)
    for index, line in enumerate(lines[:max_lines]):
        if index == max_lines - 1 and len(lines) > max_lines:
            line = line.rstrip(". ") + "…"
        draw.text((left, top + index * line_height), line, fill=fill, font=font)


class CardRenderer:
    def __init__(self, assets_dir: Path):
        self.assets_dir = assets_dir

    def render(self, character: dict) -> BytesIO:
        scale = 2
        s = lambda value: int(round(value * scale))
        point = lambda x, y: (s(x), s(y))
        rect = lambda left, top, right, bottom: (s(left), s(top), s(right), s(bottom))

        rank = RANKS[max(0, min(len(RANKS) - 1, int(character["rank_index"])))]
        template = self.assets_dir / "ranks" / RANK_FILES[rank]
        image = Image.open(template).convert("RGBA")
        image = image.resize((image.width * scale, image.height * scale), Image.Resampling.LANCZOS)
        draw = ImageDraw.Draw(image)
        ink = (45, 43, 38, 255)
        muted = (80, 74, 63, 255)

        full_name = f'{character["surname"]} {character["name"]}'
        _rotated_centered_text(image, full_name, center=point(260, 103), width=s(330), angle=3.5, size=s(24), fill=ink, bold=True)
        second = f'{character["class_name"]}  •  {character["race"]}  •  {rank}'
        draw.text(point(68, 303), second, fill=muted, font=_fit_text(draw, second, s(320), s(16), True))
        supply = f'БС: {character["supply_forms"]}'
        supply_font = _fit_text(draw, supply, s(145), s(14), True)
        supply_width = draw.textbbox((0, 0), supply, font=supply_font)[2]
        draw.text((s(456) - supply_width, s(303)), supply, fill=ink, font=supply_font)

        draw.text(point(67, 353), "СОСТОЯНИЕ БОЙЦА", fill=muted, font=_font(s(13), True))
        positions = [point(68, 382), point(267, 382), point(68, 423), point(267, 423)]
        for attribute, pos in zip(ATTRIBUTES, positions):
            values = character["attributes"][attribute]
            label = f'{attribute}: {values["current"]}/{values["max"]}'
            draw.text(pos, label, fill=ink, font=_fit_text(draw, label, s(185), s(16), True))

        status_accent = (105, 70, 47, 255)
        draw.text(point(68, 470), "ВОЛЯ", fill=muted, font=_font(s(10), True))
        draw.text(point(68, 486), f'{character["will_current"]} / {character["will_max"]}', fill=status_accent, font=_font(s(20), True))
        draw.text(point(267, 470), "ЗАРАЖЕНИЕ", fill=muted, font=_font(s(10), True))
        draw.text(point(267, 486), f'{character["infection"]} / 5', fill=status_accent, font=_font(s(20), True))
        draw.line(rect(68, 526, 350, 526), fill=(125, 113, 91, 180), width=scale)
        draw.text(point(68, 545), "ПОЛЕВЫЕ ЗАМЕТКИ", fill=muted, font=_font(s(11), True))
        notes = (character.get("notes") or "").strip()
        if notes:
            _draw_notes(draw, notes, rect(68, 565, 346, 686), ink, scale)
        else:
            draw.text(point(68, 565), "Нет записей", fill=(105, 98, 84, 190), font=_font(s(11)))

        photo_path = character.get("photo_path")
        if photo_path and Path(photo_path).exists():
            portrait = Image.open(photo_path).convert("RGBA")
            portrait = ImageOps.fit(portrait, point(238, 168), method=Image.Resampling.LANCZOS)
            edge_mask = Image.new("L", portrait.size, 0)
            ImageDraw.Draw(edge_mask).rounded_rectangle(rect(6, 6, 231, 161), radius=s(5), fill=255)
            edge_mask = edge_mask.filter(ImageFilter.GaussianBlur(s(4)))
            portrait.putalpha(edge_mask)
            tilted = portrait.rotate(20, expand=True, resample=Image.Resampling.BICUBIC, fillcolor=(0, 0, 0, 0))
            image.alpha_composite(tilted, point(358, 454))
        else:
            _rotated_centered_text(image, "ФОТО ОТСУТСТВУЕТ", center=point(500, 578), width=s(185), angle=20, size=s(12), fill=(105, 98, 84, 210), bold=True)

        output = BytesIO()
        image.convert("RGB").save(output, "PNG", optimize=True)
        output.seek(0)
        return output
