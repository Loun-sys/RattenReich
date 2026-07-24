from collections import defaultdict
from pathlib import Path

from catalog_loader import load_catalog


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parent / "Каталог снабжения — оружие и снаряжение.md"
OUTPUT = ROOT / "ITEM_CATALOG_SUMMARY.md"


def main() -> None:
    groups = defaultdict(list)
    for item in load_catalog(SOURCE):
        groups[item["category"]].append(item)

    lines = [
        "# Итоговый перечень предметов Ratten Reich",
        "",
        f"Всего предметов: **{sum(map(len, groups.values()))}**.",
        "",
        "Формат: размер; качество/защита; урон и тип; дистанция; эффект и условия.",
        "",
    ]
    for category in sorted(groups):
        items = sorted(groups[category], key=lambda row: (row["source_number"], row["name"]))
        lines.extend((f"## {category} — {len(items)}", ""))
        for item in items:
            stats = [item["size"]]
            if item["hands"]:
                stats.append("одноручное" if int(item["hands"]) == 1 else "двуручное")
            if item["category"] == "Броня":
                stats.append(f'защита {item["defense"]}')
            elif item["gear"]:
                stats.append(f'качество/гир {item["gear"]}')
            if item["damage"]:
                stats.append(f'урон {item["damage"]} ({item["damage_type"] or "без типа"})')
            if item["use_range"]:
                stats.append(f'дистанция {item["use_range"]}')
            if item["ammo_max"]:
                stats.append(f'боезапас {item["ammo_max"]}, СКР {item["fire_rate"] or 1}')
            lines.append(
                f'- **{item["source_number"]}. {item["name"]}** — '
                f'{", ".join(stats)}. {item["conditions"]}'
            )
        lines.append("")
    OUTPUT.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
