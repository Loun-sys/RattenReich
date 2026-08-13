from pathlib import Path

from card_renderer import CardRenderer

sample = {
    "surname": "ШРАМ",
    "name": "Конрад",
    "class_name": "Солдат",
    "race": "Крысы",
    "rank_index": 2,
    "will_current": 8,
    "will_max": 10,
    "infection": 1,
    "supply_forms": 4,
    "photo_path": None,
    "attributes": {
        "Телосложение": {"current": 4, "max": 5},
        "Ловкость": {"current": 3, "max": 4},
        "Смекалка": {"current": 3, "max": 3},
        "Эмпатия": {"current": 2, "max": 2},
    },
}

output = Path(__file__).parent / "data" / "card-preview.png"
output.write_bytes(CardRenderer(Path(__file__).parent / "assets").render(sample).getvalue())
print(output)

