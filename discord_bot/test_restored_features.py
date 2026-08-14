import asyncio
import tempfile
from pathlib import Path

import bot as bot_module
from database import Database


async def main() -> None:
    assert bot_module.attack_damage(0, 3) == 0
    assert bot_module.attack_damage(1, 3) == 3
    assert bot_module.attack_damage(2, 3) == 4
    assert bot_module.attack_damage(3, 3) == 5
    assert bot_module.attack_damage(2, 3, 1) == 5
    equipped_crowbar = {
        "category": "Оружие ближнего боя", "equipped": 1,
        "properties": "Разрушающее, Тяжёлое", "attachment_melee_damage": 0,
    }
    assert bot_module.is_skill_roll_gear(equipped_crowbar, "Драка")
    assert not bot_module.is_skill_roll_gear(equipped_crowbar, "Стрельба")
    assert not bot_module.is_skill_roll_gear({**equipped_crowbar, "equipped": 0}, "Драка")
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "restored.sqlite3"
        db = Database(path)
        await db.initialize()
        character_id = await db.create_character(1, 501, "Проверка", "Тыла", "Окопник", "Крысы")
        await db.set_skill(character_id, "Защита", 3)
        await db.set_skill(character_id, "Снабжение", -5)
        await db.update_character(character_id, "supply_forms", 100)
        items = await db.catalog_items(1, "", 1000)
        filter_flask = next(item for item in items if item["name"] == "Фляга с фильтром")
        assert filter_flask["max_durability"] == 2
        flask_row_id = await db.give_item(character_id, filter_flask)
        first_use = await db.consume_multi_use_item(character_id, flask_row_id, filter_flask["name"])
        assert first_use and first_use["remaining_uses"] == 1 and first_use["quantity"] == 1
        second_use = await db.consume_multi_use_item(character_id, flask_row_id, filter_flask["name"])
        assert second_use and second_use["remaining_uses"] == 0 and second_use["quantity"] == 0
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
        assert {"склад-снабжения", "снабжение", "автопарк", "магазин", "дозарядить", "выдать-травму", "инициатива"} <= command_names
        skill_view = bot_module.SkillRollView(501, character, bot_module.make_pool(character, "Драка"), "", True)
        assert {getattr(child, "label", "") for child in skill_view.children} >= {"Пуш", "Завершить бросок"}

    print("restored features test: OK")


if __name__ == "__main__":
    asyncio.run(main())
