import asyncio
import tempfile
from pathlib import Path

import bot as bot_module
from database import Database


async def main() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "restored.sqlite3"
        db = Database(path)
        await db.initialize()
        character_id = await db.create_character(1, 501, "Проверка", "Тыла", "Окопник", "Крысы")
        await db.set_skill(character_id, "Защита", 3)
        await db.set_skill(character_id, "Снабжение", -5)
        await db.update_character(character_id, "supply_forms", 100)
        items = await db.catalog_items(1, "", 1000)
        prosthetic = next(item for item in items if item["category"] == "Протезы" and bot_module.required_protection_level(item) <= 3)
        character = await db.character(1, 501)
        assert bot_module.can_purchase(character, prosthetic)

        ordinary = next(item for item in items if item["category"] == "Снаряжение")
        vehicle = next(item for item in items if item["category"] == "Транспорт")
        async with db.connect() as connection:
            await connection.execute(
                "INSERT INTO supply_warehouse(guild_id,item_id,durability,quantity,deposited_by) VALUES(?,?,?,?,?)",
                (1, ordinary["id"], ordinary["max_durability"], 2, 501),
            )
            await connection.execute(
                "INSERT INTO purchase_orders(guild_id,character_id,item_id,item_name,paid_price) VALUES(?,?,?,?,?)",
                (1, character_id, ordinary["id"], ordinary["name"], 7),
            )
            await connection.execute(
                "INSERT INTO motor_pool(guild_id,item_id,quantity,purchased_by) VALUES(?,?,?,?)",
                (1, vehicle["id"], 1, 501),
            )
            await connection.commit()

        await db.initialize()
        assert len(await db.supply_warehouse_items(1)) == 1
        assert len(await db.pending_purchase_orders(1)) == 1
        assert len((await db.motor_pool(1))["items"]) == 1

        command_names = {command.name for command in bot_module.bot.tree.get_commands()}
        assert {"склад-снабжения", "снабжение", "автопарк", "магазин", "дозарядить"} <= command_names
        skill_view = bot_module.SkillRollView(501, character, bot_module.make_pool(character, "Драка"), "", True)
        assert {getattr(child, "label", "") for child in skill_view.children} >= {"Пуш", "Завершить бросок"}

    print("restored features test: OK")


if __name__ == "__main__":
    asyncio.run(main())
