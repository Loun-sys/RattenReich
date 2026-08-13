from __future__ import annotations

from io import BytesIO
from pathlib import Path
from difflib import get_close_matches

from PIL import Image, ImageDraw, ImageFont, ImageOps

from constants import RANKS


class AugmentationRenderer:
    CIRCLES = {
        # Exact transparent apertures measured from augmentations-rat.png.
        "Голова": [(1008, 186, 1080, 258)], "Глаза": [(1231, 247, 1321, 339)],
        "Рука": [(745, 366, 825, 446), (1229, 395, 1334, 502)],
        "Оружейный модуль": [(681, 544, 773, 640)], "Кожа": [(1261, 550, 1332, 621)],
        "Корпус": [(1325, 663, 1387, 725)], "Нога": [(720, 707, 790, 778), (1287, 772, 1356, 841)],
        "Хвост": [(773, 822, 863, 917)],
    }
    COCKROACH_POSITIONS = {
        "Голова": (1006, 186, 1079, 259), "Глаза": (1156, 241, 1227, 313),
        "Верхняя правая рука": (761, 319, 836, 396), "Верхняя левая рука": (1255, 329, 1330, 405),
        "Нижняя правая рука": (681, 458, 757, 536), "Нижняя левая рука": (1290, 481, 1366, 558),
        "Оружейный модуль": (688, 608, 764, 684), "Кожа": (1261, 593, 1328, 660),
        "Корпус": (1303, 696, 1364, 757), "Правая нога": (724, 763, 792, 831),
        "Левая нога": (1287, 794, 1353, 862),
    }
    RAT_POSITIONS = {
        "Голова": (1008, 186, 1080, 258), "Глаза": (1231, 247, 1321, 339),
        "Правая рука": (745, 366, 825, 446), "Левая рука": (1229, 395, 1334, 502),
        "Оружейный модуль": (681, 544, 773, 640), "Кожа": (1261, 550, 1332, 621),
        "Корпус": (1325, 663, 1387, 725), "Правая нога": (720, 707, 790, 778),
        "Левая нога": (1287, 772, 1356, 841), "Хвост": (773, 822, 863, 917),
    }

    def __init__(self, assets_root: Path):
        self.assets_root = assets_root
        self.background = assets_root / "augmentations-rat.png"
        self.cockroach_background = assets_root / "augmentations-cockroach.png"
        self.icons = assets_root / "prosthetics"

    def _font(self, size: int):
        for path in (Path("C:/Windows/Fonts/courbd.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")):
            if path.is_file():
                return ImageFont.truetype(str(path), size)
        return ImageFont.load_default()

    def _icon(self, item: dict, size: tuple[int, int], mirror: bool) -> Image.Image | None:
        path = self.icons / str(item.get("icon_file") or "")
        if not path.is_file():
            candidates = [candidate.name for candidate in self.icons.glob("*.png")]
            matches = get_close_matches(path.name, candidates, n=1, cutoff=0.52)
            if not matches:
                return None
            path = self.icons / matches[0]
        icon = Image.open(path).convert("RGBA")
        # The source sheets use either white or transparent corners.  A white
        # corner becomes an ugly crescent after a circular crop, so turn only
        # the light area connected to the outer edge into the common dark icon
        # backing.  Bright details inside the implant remain untouched.
        for seed in ((0, 0), (icon.width - 1, 0), (0, icon.height - 1), (icon.width - 1, icon.height - 1)):
            pixel = icon.getpixel(seed)
            if pixel[3] > 0 and min(pixel[:3]) >= 220:
                ImageDraw.floodfill(icon, seed, (20, 19, 17, 255), thresh=28)
        if mirror:
            icon = ImageOps.mirror(icon)
        # Cover the whole transparent aperture. The template, composited later,
        # provides the exact circular clipping and preserves its printed rim.
        return ImageOps.fit(icon, size, Image.Resampling.LANCZOS)

    def render(self, character: dict, items: list[dict]) -> BytesIO:
        cockroach = character.get("race") == "Тараканы"
        template = Image.open(self.cockroach_background if cockroach else self.background).convert("RGBA")
        image = Image.new("RGBA", template.size, (0, 0, 0, 0))
        portrait = Path(str(character.get("photo_path") or ""))
        if portrait.is_file():
            photo = Image.open(portrait).convert("RGBA")
            photo = ImageOps.fit(photo, (492, 611), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
            image.alpha_composite(photo, (98, 177))

        equipped = [item for item in items if item.get("equipped") and item.get("category") == "Протезы"]
        positions = self.COCKROACH_POSITIONS if cockroach else self.RAT_POSITIONS
        used = set()
        defaults = {"Рука": ["Правая рука", "Левая рука"], "Нога": ["Правая нога", "Левая нога"]}
        if cockroach:
            defaults["Рука"] = ["Верхняя правая рука", "Верхняя левая рука", "Нижняя правая рука", "Нижняя левая рука"]
        for item in equipped:
            position = str(item.get("equipped_position") or "")
            if position not in positions:
                candidates = defaults.get(str(item.get("prosthetic_slot")), [str(item.get("prosthetic_slot"))])
                position = next((candidate for candidate in candidates if candidate in positions and candidate not in used), "")
            if not position or position not in positions or position in used:
                continue
            used.add(position)
            left, top, right, bottom = positions[position]
            icon = self._icon(item, (right - left, bottom - top), mirror="правая" in position.casefold())
            if icon:
                image.alpha_composite(icon, (left, top))

        # The supplied PNG is a foreground mask: its transparent holes reveal
        # the portrait and implants placed underneath, while its opaque artwork
        # clips everything exactly to the printed frame and circles.
        image.alpha_composite(template)
        draw = ImageDraw.Draw(image)
        color = (82, 48, 39, 255)
        font = self._font(18)
        draw.text((97, 831), f'{character["surname"]} {character["name"]}', fill=color, font=font)
        draw.text((329, 831), RANKS[int(character["rank_index"])], fill=color, font=font)
        draw.text((97, 903), character["class_name"], fill=color, font=font)
        draw.text((329, 903), character["race"], fill=color, font=font)

        total = 11 if cockroach else 10
        occupied = len(equipped)
        draw.rectangle((1128, 41, 1452, 135), fill=(225, 214, 188, 210))
        draw.text((1138, 54), f"СВОБОДНЫЕ СЛОТЫ: {max(0, total - occupied)}", fill=color, font=self._font(17))
        draw.text((1138, 99), f"ЗАНЯТО: {occupied} / {total}", fill=color, font=self._font(17))
        output = BytesIO()
        image.convert("RGB").save(output, "PNG", optimize=True)
        output.seek(0)
        return output
