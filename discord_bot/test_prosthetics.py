import asyncio
import json
import tempfile
from collections import Counter
from pathlib import Path

import bot as bot_module
from augmentation_renderer import AugmentationRenderer
from database import Database
from prosthetic_balance import PROSTHETIC_ACCESS_TIERS, balance_prosthetic, validate_prosthetic_balance


async def main():
    source = json.loads((Path(__file__).parent / "prosthetic_data.json").read_text(encoding="utf-8"))["items"]
    validate_prosthetic_balance(source)
    balanced = [balance_prosthetic(item) for item in source]
    distribution = Counter((item["slot"], item["access"]) for item in balanced)
    for slot in PROSTHETIC_ACCESS_TIERS:
        for access in ("Общедоступное", "Защита 1", "Защита 2", "Защита 3", "Защита 4"):
            assert distribution[(slot, access)] >= 2
    assert min(item["price"] for item in balanced) >= 10
    assert min(item["price"] for item in balanced if item["access"] == "Защита 4") >= 32

    with tempfile.TemporaryDirectory() as temp:
        db = Database(Path(temp) / "test.sqlite3")
        await db.initialize()
        items = await db.catalog_items(1, "", 1000)
        prosthetics = [item for item in items if item["category"] == "Протезы"]
        assert len(prosthetics) >= 80
        assert all(item["prosthetic_slot"] and item["will_cost"] in {1, 2, 3} for item in prosthetics)
        assert all("Максимум Воли" in item["properties"] for item in prosthetics)

        trench_id = await db.create_character(1, 101, "Шрам", "Ганс", "Окопник", "Крысы")
        await db.set_attributes(trench_id, {"Телосложение": 5, "Ловкость": 5, "Смекалка": 4, "Эмпатия": 4})
        await db.set_skill(trench_id, "Защита", 5)
        await db.update_character(trench_id, "supply_forms", 200)
        trench = await db.character(1, 101)
        first = prosthetics[0]
        assert bot_module.can_purchase(trench, first, "Протезы")
        purchased, _, _ = await db.purchase_item(trench_id, first["id"], bot_module.required_protection_level(first) or 0)
        assert purchased
        # Покупка создаёт заявку снабжения; отдельную выданную копию используем
        # для проверки установки и расхода максимальной Воли.
        await db.give_item(trench_id, first)
        row = await db.inventory_item_by_name(trench_id, first["name"])
        assert (await db.set_equipped(trench_id, row["id"], True))[0]
        equipped = await db.character(1, 101)
        assert equipped["will_max"] == 10 - first["will_cost"]

        soldier_id = await db.create_character(1, 102, "Клык", "Отто", "Солдат", "Крысы")
        soldier = await db.character(1, 102)
        assert not bot_module.can_purchase(soldier, first, "Протезы")

        inventory = await db.inventory(trench_id)
        rendered = AugmentationRenderer(Path(__file__).parent / "assets").render(equipped, inventory)
        assert len(rendered.getvalue()) > 10000

        cockroach_id = await db.create_character(1, 103, "Жук", "Карл", "Окопник", "Тараканы")
        await db.set_attributes(cockroach_id, {"Телосложение": 5, "Ловкость": 5, "Смекалка": 4, "Эмпатия": 4})
        hand_item = next(item for item in prosthetics if item["prosthetic_slot"] == "Рука")
        await db.give_item(cockroach_id, hand_item, 2)
        hand_rows = [item for item in await db.inventory(cockroach_id) if item["prosthetic_slot"] == "Рука"]
        assert (await db.set_equipped(cockroach_id, hand_rows[0]["id"], True, "Верхняя правая рука"))[0]
        assert not (await db.set_equipped(cockroach_id, hand_rows[1]["id"], True, "Верхняя правая рука"))[0]
        assert (await db.set_equipped(cockroach_id, hand_rows[1]["id"], True, "Нижняя левая рука"))[0]
        tail_item = next(item for item in prosthetics if item["prosthetic_slot"] == "Хвост")
        await db.give_item(cockroach_id, tail_item, 1)
        tail_row = await db.inventory_item_by_name(cockroach_id, tail_item["name"])
        assert not (await db.set_equipped(cockroach_id, tail_row["id"], True))[0]
        cockroach = await db.character(1, 103)
        cockroach_inventory = await db.inventory(cockroach_id)
        assert len(AugmentationRenderer(Path(__file__).parent / "assets").render(cockroach, cockroach_inventory).getvalue()) > 10000

        renderer = AugmentationRenderer(Path(__file__).parent / "assets")
        assert renderer._template_for_race("Псовые")[0].name == "augmentations-canine-marsupial.png"
        assert renderer._template_for_race("Сумчатые")[0].name == "augmentations-canine-marsupial.png"
        assert renderer._template_for_race("Вараны")[0].name == "augmentations-monitor-agama.png"
        assert renderer._template_for_race("Агамы")[0].name == "augmentations-monitor-agama.png"
        assert renderer._template_for_race("Мыши")[0].name == "augmentations-rat.png"
        assert renderer._template_for_race("Тараканы")[1]


if __name__ == "__main__":
    asyncio.run(main())
