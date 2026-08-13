import asyncio
import tempfile
from pathlib import Path

import bot as bot_module
from augmentation_renderer import AugmentationRenderer
from database import Database


async def main():
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


if __name__ == "__main__":
    asyncio.run(main())
