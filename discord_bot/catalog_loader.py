from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from attachment_data import ATTACHMENTS


def _integer(value: str, default: int = 0) -> int:
    match = re.search(r"\d+", value or "")
    return int(match.group()) if match else default


REMOVED_SOURCE_NUMBERS = {55, 56, 58, 60}

SUPPLY_LEVELS = ("I", "II", "III", "IV", "V")

MEDICAL_CONSUMABLES = {
    "Полевые бинты",
    "Индивидуальный перевязочный пакет",
    "Армейская аптечка",
    "Набор полевого санитара",
    "Нейростимулятор",
    "Успокоительный автоинъектор",
}


MULTI_USE_CONSUMABLES = {
    "Сигареты «Фельдграу»": 3,
    "Сигареты «Штальраух»": 2,
    "Папиросы «Кайзеркроне»": 2,
    "Шнапс «Айзернер Гренадир»": 2,
    "Горькая «Рейхсмарш»": 3,
    "Бренди «Кайзерблут»": 2,
}

_CONSUMABLE_NAMES = {name.casefold() for name in MEDICAL_CONSUMABLES | set(MULTI_USE_CONSUMABLES)}


def is_consumable_item(item: dict[str, Any]) -> bool:
    """Recognize current and legacy consumables even when their stored flag is missing."""
    name = str(item.get("name") or "").strip().casefold()
    if name in _CONSUMABLE_NAMES:
        return True
    text = " ".join(
        str(item.get(key) or "") for key in ("properties", "conditions", "description")
    ).casefold()
    return (
        "расходник" in text
        or "одноразов" in text
        or ("восстанавлива" in text and "пункт" in text)
    )

def catalog_price_increase(price: int) -> int:
    if price <= 0:
        return 0
    return 2 if price >= 10 else 1

REDUCED_BASE_PRICES = {
    "Общедоступное": 4,
    "Снабжение I": 5,
    "Снабжение II": 8,
    "Снабжение III": 11,
    "Снабжение IV": 15,
    "Снабжение V": 23,
}

ACCESS_FLOORS = {
    "Общедоступное": 5,
    "Снабжение I": 7,
    "Снабжение II": 10,
    "Снабжение III": 15,
    "Снабжение IV": 20,
    "Снабжение V": 30,
}


def _effect_rating(item: dict[str, Any]) -> int:
    text = f'{item.get("properties") or ""} {item.get("conditions") or ""}'.casefold()
    bonus = 0
    if any(marker in text for marker in ("игнорирует 1", "игнорирует 2", "+2 к", "добавляет 3 куб", "+3 куб")):
        bonus += 1
    if any(marker in text for marker in ("дополнительный урон", "+1 урон", "каждый незаблокированный успех")):
        bonus += 1
    if "нельзя сломать" in text or "неразрушим" in text:
        bonus += 1
    if any(marker in text for marker in ("требуется действие", "требует действия", "требует двух", "требует упора", "после каждого выстрела")):
        bonus -= 1
    if any(marker in text for marker in ("считается сломанным", "проверка 50% на поломку", "может взорваться")):
        bonus -= 1
    return max(-1, min(1, bonus))


def _weapon_access(item: dict[str, Any]) -> str:
    damage = max(0, int(item.get("damage") or 0))
    gear = max(0, int(item.get("gear") or 0))
    hands = max(0, int(item.get("hands") or 0))
    effect = _effect_rating(item)
    if str(item.get("category") or "") == "Оружие дальнего боя":
        fire_rate = max(1, int(item.get("fire_rate") or 1))
        ammo = max(0, int(item.get("ammo_max") or 0))
        use_range = str(item.get("use_range") or "")
        fire_score = 0 if fire_rate == 1 else 1 if fire_rate == 2 else 2 if fire_rate == 3 else 3 if fire_rate <= 5 else 4
        ammo_score = -1 if ammo <= 1 else 0 if ammo <= 3 else 1 if ammo <= 5 else 2 if ammo <= 11 else 3
        range_score = {"Нулевая": -1, "Ближняя": 0, "Средняя": 1, "Дальняя": 2}.get(use_range, 0)
        score = damage * 2 + gear + fire_score + ammo_score + range_score - (1 if hands >= 2 else 0) + effect
        exceptional = (
            damage >= 3 and gear >= 3 and fire_rate >= 3
        ) or (
            damage >= 2 and gear >= 3 and fire_rate >= 4 and ammo >= 4
        )
        if exceptional and score >= 12:
            return "Снабжение V"
        if score >= 11 or damage >= 4:
            return "Снабжение IV"
        if score >= 9:
            return "Снабжение III"
        if damage >= 3:
            return "Снабжение II"
        if score >= 7:
            return "Снабжение II"
        if score >= 5 or damage >= 2 or gear >= 3:
            return "Снабжение I"
        return "Общедоступное"
    score = damage * 2 + gear + (1 if item.get("use_range") == "Ближняя" else 0) - (1 if hands >= 2 else 0) + effect
    if damage >= 3 and gear >= 3 and score >= 8:
        return "Снабжение V"
    if score >= 8 or (damage >= 3 and gear >= 2):
        return "Снабжение IV"
    if score >= 6:
        return "Снабжение III"
    if score >= 5:
        return "Снабжение II"
    if score >= 4:
        return "Снабжение I"
    return "Общедоступное"

def _weapon_price(item: dict[str, Any]) -> int:
    base = REDUCED_BASE_PRICES[str(item["access"])]
    damage = int(item.get("damage") or 0)
    gear = int(item.get("gear") or 0)
    hands = int(item.get("hands") or 0)
    adjustment = damage - gear + (1 if damage >= 4 else 0)
    if hands >= 2:
        adjustment -= 1
    if str(item.get("category") or "") == "Оружие дальнего боя":
        use_range = str(item.get("use_range") or "")
        adjustment += {"Нулевая": -1, "Средняя": 1, "Дальняя": 2}.get(use_range, 0)
        fire_rate = int(item.get("fire_rate") or 0)
        ammo = int(item.get("ammo_max") or 0)
        adjustment += 1 if fire_rate >= 4 else 0
        adjustment += 1 if ammo >= 6 else (-1 if ammo == 1 else 0)
    raw_price = base + max(-3, min(3, adjustment))
    bounds = {
        "Общедоступное": (3, 4),
        "Снабжение I": (5, 6),
        "Снабжение II": (7, 9),
        "Снабжение III": (10, 13),
        "Снабжение IV": (14, 20),
        "Снабжение V": (22, 30),
    }
    minimum, maximum = bounds[str(item["access"])]
    return max(minimum, min(maximum, raw_price))


def _protection_price(item: dict[str, Any]) -> int:
    base = REDUCED_BASE_PRICES[str(item["access"])]
    protection = int(item.get("defense") or item.get("gear") or 0)
    is_large = str(item.get("armor_slot") or item.get("size") or "") == "Большой"
    adjustment = (1 if is_large else 0) + max(0, protection - 3)
    if str(item.get("category") or "") == "Щит" and int(item.get("hands") or 0) >= 2:
        adjustment -= 1
    return max(3, base + max(-2, min(4, adjustment)))


def _balance_item(item: dict[str, Any]) -> dict[str, Any]:
    category = str(item.get("category") or "")
    uses = MULTI_USE_CONSUMABLES.get(str(item.get("name") or ""))
    if uses:
        item["max_durability"] = uses
        usage_text = f"Использований: {uses}."
        if usage_text not in str(item.get("conditions") or ""):
            item["conditions"] = f'{usage_text} {item.get("conditions") or ""}'.strip()
            item["description"] = item["conditions"]
    if category.startswith("Оружие "):
        item["access"] = _weapon_access(item)
    elif category == "Броня":
        protection = max(0, int(item.get("defense") or 0))
        if str(item.get("armor_slot") or item.get("size") or "") == "Большой":
            tier = min(5, max(2, protection + 1))
        else:
            tier = min(5, max(0, protection - 1))
        item["access"] = "Общедоступное" if tier == 0 else f"Снабжение {SUPPLY_LEVELS[tier - 1]}"
    elif category == "Щит":
        protection = max(0, int(item.get("defense") or item.get("gear") or 0))
        tier = min(5, max(0, protection - 1))
        item["access"] = "Общедоступное" if tier == 0 else f"Снабжение {SUPPLY_LEVELS[tier - 1]}"
    if category.startswith("Оружие "):
        item["price"] = _weapon_price(item)
    elif category in {"Броня", "Щит"}:
        item["price"] = _protection_price(item)
    if str(item.get("name") or "") not in MEDICAL_CONSUMABLES:
        item["price"] = int(item.get("price") or 0) + catalog_price_increase(int(item.get("price") or 0))
    return item


def _apply_summary_overrides(items: list[dict[str, Any]], source_path: Path) -> list[dict[str, Any]]:
    summary_path = source_path.parent / "discord_bot" / "ITEM_CATALOG_SUMMARY.md"
    if not summary_path.exists():
        summary_path = source_path.parent / "ITEM_CATALOG_SUMMARY.md"
    if not summary_path.exists():
        return items
    overrides: dict[int, dict[str, Any]] = {}
    category = ""
    for raw_line in summary_path.read_text(encoding="utf-8").splitlines():
        if raw_line.startswith("## "):
            category = re.sub(r"\s+—\s+\d+\s*$", "", raw_line[3:].strip())
            continue
        match = re.match(r"^- \*\*(\d+)\.\s+(.+?)\*\*\s+—\s+(.+)$", raw_line.strip())
        if not match:
            continue
        number, name, payload = int(match.group(1)), match.group(2).strip(), match.group(3).strip()
        effect_match = re.match(r"^(.*?)\.(?:\s+|(?=[А-ЯA-Z]))(.*)$", payload)
        stats, effect = (effect_match.group(1), effect_match.group(2)) if effect_match else (payload, "")
        override: dict[str, Any] = {"name": name, "category": category, "conditions": effect.strip()}
        size = re.match(r"^(Безделушка|Малый|Большой)", stats)
        if size:
            override["size"] = size.group(1)
        for key, pattern in (
            ("defense", r"защита\s+(\d+)"),
            ("gear", r"качество/гир\s+(\d+)"),
            ("damage", r"урон\s+(\d+)"),
            ("ammo_max", r"боезапас\s+(\d+)"),
            ("fire_rate", r"СКР\s+(\d+)"),
        ):
            value = re.search(pattern, stats, re.IGNORECASE)
            if value:
                override[key] = int(value.group(1))
        damage_type = re.search(r"урон\s+\d+\s+\(([^)]+)\)", stats, re.IGNORECASE)
        if damage_type:
            override["damage_type"] = damage_type.group(1)
        use_range = re.search(r"дистанция\s+([^,.]+)", stats, re.IGNORECASE)
        if use_range:
            override["use_range"] = use_range.group(1).strip()
        overrides[number] = override

    merged = []
    for item in items:
        number = int(item["source_number"])
        if number in REMOVED_SOURCE_NUMBERS:
            continue
        override = overrides.get(number)
        if override:
            item.update({key: value for key, value in override.items() if value not in ("", None)})
            item["description"] = item["conditions"]
            if item["category"] == "Броня":
                item["max_durability"] = max(1, int(item.get("defense") or 0))
                item["gear"] = int(item.get("defense") or 0)
            elif item["category"] == "Щит":
                item["gear"] = max(1, int(item.get("gear") or 1))
                item["defense"] = item["gear"]
                item["max_durability"] = item["gear"]
            elif "gear" in override:
                item["max_durability"] = max(1, int(item["gear"]))
        item["properties"] = re.sub(
            r"(?:,\s*)?Самодельное", "", str(item.get("properties") or ""), flags=re.IGNORECASE
        ).strip(" ,;")
        item["conditions"] = re.sub(
            r"(?:,\s*)?Самодельное", "", str(item.get("conditions") or ""), flags=re.IGNORECASE
        ).strip(" ,;")
        if re.search(r"\b(?:одноразов\w*|расходник)\b", item["conditions"], re.IGNORECASE):
            properties = str(item.get("properties") or "")
            if "расходник" not in properties.casefold():
                item["properties"] = ", ".join(part for part in (properties, "Расходник") if part)
        merged.append(item)
    return merged


def load_catalog(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    glossary = {}
    for line in lines:
        match = re.match(r"^\*\*(.+?)\.\*\*\s*(.+)$", line.strip())
        if match:
            glossary[match.group(1).strip()] = match.group(2).strip()
    section = ""
    subsection = ""
    result: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if line.startswith("## "):
            section = line[3:].strip()
            subsection = ""
        elif line.startswith("### "):
            subsection = line[4:].strip()
        if not line.startswith("| № |"):
            index += 1
            continue

        headers = [cell.strip() for cell in line.strip("|").split("|")]
        index += 2
        while index < len(lines) and lines[index].strip().startswith("|"):
            cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
            index += 1
            if len(cells) != len(headers) or not cells[0].isdigit():
                continue
            row = dict(zip(headers, cells))
            name = row.get("Название", "")
            if not name:
                continue

            heading = f"{section} {subsection}".casefold()
            if "брон" in heading or "Защита" in row:
                category = "Броня"
            elif "щит" in heading:
                category = "Щит"
            elif "оруж" in heading and ("Боезапас" in row or "Скорострельность" in row):
                category = "Оружие дальнего боя"
            elif "оруж" in heading and ("Тип урона" in row or "Хват" in row):
                category = "Оружие ближнего боя"
            elif "безделуш" in heading:
                category = "Разное"
            else:
                category = "Снаряжение"

            size = row.get("Размер") or ("Безделушка" if "безделуш" in heading else "Малый")
            hands = 2 if ":twohand:" in row.get("Хват", "") else 1 if ":onehand:" in row.get("Хват", "") else 0
            gear = _integer(row.get(":gears:", ""), 1)
            damage = _integer(row.get(":damage:", ""), 0)
            properties = row.get("Свойства") or row.get("Свойство") or ""
            effect = row.get("Игровой эффект") or row.get("Эффект") or ""
            expanded_effect = glossary.get(effect.rstrip("."), effect)
            defense = _integer(row.get("Защита", ""), 0)
            if category == "Щит":
                defense = gear
            compatibility = row.get("Совместимое оружие") or ""
            ammo_type = row.get("Боеприпас") or ""
            access = row.get("Допуск") or "Общедоступное"
            conditions = "; ".join(
                part for part in (
                    expanded_effect,
                    properties,
                    f"Совместимо: {compatibility}" if compatibility else "",
                    f"Боеприпас: {ammo_type}" if ammo_type else "",
                    f"Допуск: {access}",
                )
                if part
            )
            if not conditions:
                conditions = "Для использования предмет должен находиться в инвентаре персонажа."

            result.append({
                "source_number": int(row["№"]),
                "name": name,
                "size": size,
                "category": category,
                "max_durability": max(1, defense if category == "Броня" else gear),
                "gear": defense if category == "Броня" else gear,
                "hands": hands,
                "damage": damage,
                "damage_type": row.get("Тип урона") or "",
                "defense": defense,
                "use_range": row.get("Дистанция") or None,
                "ammo_max": _integer(row.get("Боезапас", ""), 0) or None,
                "fire_rate": _integer(row.get("Скорострельность", ""), 0) or None,
                "price": _integer(row.get("Цена", ""), 0),
                "access": access,
                "properties": properties,
                "conditions": conditions,
                "description": expanded_effect or conditions,
                "armor_slot": size if category == "Броня" else None,
            })
    result = _apply_summary_overrides(result, path)
    approved_path = path.parent / "APPROVED_ITEMS.json"
    if approved_path.exists():
        approved = json.loads(approved_path.read_text(encoding="utf-8"))
        result.extend(approved.get("items", []))
    for index, attachment in enumerate(ATTACHMENTS, 600):
        result.append({
            "source_number": index, "name": attachment["name"], "size": "Безделушка",
            "category": "Насадка", "max_durability": 1, "gear": 0, "hands": 0,
            "damage": 0, "damage_type": "", "defense": 0, "use_range": None,
            "ammo_max": None, "fire_rate": None, "price": attachment["price"],
            "access": attachment["access"], "properties": f'Слот: {attachment["slot"]}; Совместимость: {attachment["kinds"]}',
            "conditions": attachment["effect"], "description": attachment["effect"], "armor_slot": None,
        })
    return [_balance_item(item) for item in result]
