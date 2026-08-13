from __future__ import annotations

from io import BytesIO
from pathlib import Path
from difflib import get_close_matches

from PIL import Image, ImageDraw, ImageFont, ImageOps

from constants import RANKS


class AugmentationRenderer:
    CIRCLES = {
        "Голова": [(1044, 219, 37)], "Глаза": [(1275, 292, 43)],
        "Рука": [(784, 405, 39), (1297, 449, 49)],
        "Оружейный модуль": [(727, 592, 47)], "Кожа": [(1297, 585, 36)],
        "Корпус": [(1355, 691, 32)], "Нога": [(755, 742, 36), (1322, 807, 34)],
        "Хвост": [(815, 869, 47)],
    }

    def __init__(self, assets_root: Path):
        self.assets_root = assets_root
        self.background = assets_root / "augmentations-rat.png"
        self.icons = assets_root / "prosthetics"

    def _font(self, size: int):
        for path in (Path("C:/Windows/Fonts/courbd.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")):
            if path.is_file():
                return ImageFont.truetype(str(path), size)
        return ImageFont.load_default()

    def _icon(self, item: dict, diameter: int, mirror: bool) -> Image.Image | None:
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
        padding = max(3, diameter // 24)
        inner = diameter - padding * 2
        icon = ImageOps.fit(icon, (inner, inner), Image.Resampling.LANCZOS)
        framed = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
        circle = Image.new("RGBA", (diameter, diameter), (20, 19, 17, 255))
        framed.alpha_composite(circle)
        framed.alpha_composite(icon, (padding, padding))
        mask = Image.new("L", (diameter, diameter), 0)
        ImageDraw.Draw(mask).ellipse((2, 2, diameter - 3, diameter - 3), fill=255)
        framed.putalpha(mask)
        return framed

    def render(self, character: dict, items: list[dict]) -> BytesIO:
        image = Image.open(self.background).convert("RGBA")
        draw = ImageDraw.Draw(image)
        portrait = Path(str(character.get("photo_path") or ""))
        if portrait.is_file():
            photo = Image.open(portrait).convert("RGBA")
            # Fill the complete portrait window. Cropping is preferable here to
            # letterboxing: the dossier frame itself already defines the crop.
            photo = ImageOps.fit(
                photo,
                (492, 604),
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
            image.alpha_composite(photo, (97, 180))

        color = (82, 48, 39, 255)
        font = self._font(18)
        draw.text((97, 831), f'{character["surname"]} {character["name"]}', fill=color, font=font)
        draw.text((329, 831), RANKS[int(character["rank_index"])], fill=color, font=font)
        draw.text((97, 903), character["class_name"], fill=color, font=font)
        draw.text((329, 903), character["race"], fill=color, font=font)

        equipped = [item for item in items if item.get("equipped") and item.get("category") == "Протезы"]
        by_slot = {}
        for item in equipped:
            by_slot.setdefault(item.get("prosthetic_slot"), []).append(item)
        for slot, positions in self.CIRCLES.items():
            for index, item in enumerate(by_slot.get(slot, [])[:len(positions)]):
                x, y, radius = positions[index]
                # The left-side circles on the sheet describe the character's
                # right limbs.  Their source art needs the mirrored variant.
                icon = self._icon(item, radius * 2, mirror=slot in {"Рука", "Нога"} and index == 0)
                if icon:
                    image.alpha_composite(icon, (x - radius, y - radius))

        total = 11 if character.get("race") == "Тараканы" else 10
        occupied = len(equipped)
        draw.rectangle((1128, 41, 1452, 135), fill=(225, 214, 188, 210))
        draw.text((1138, 54), f"СВОБОДНЫЕ СЛОТЫ: {max(0, total - occupied)}", fill=color, font=self._font(17))
        draw.text((1138, 99), f"ЗАНЯТО: {occupied} / {total}", fill=color, font=self._font(17))
        output = BytesIO()
        image.convert("RGB").save(output, "PNG", optimize=True)
        output.seek(0)
        return output
