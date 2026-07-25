from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def _integer(value: str, default: int = 0) -> int:
    match = re.search(r"\d+", value or "")
    return int(match.group()) if match else default


REMOVED_SOURCE_NUMBERS = {55, 56, 58, 60}


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
    return _apply_summary_overrides(result, path)
