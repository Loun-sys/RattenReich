import asyncio
import tempfile
from pathlib import Path

import bot as bot_module
from database import Database
from trauma_data import PHYSICAL_TRAUMAS


async def main() -> None:
    with tempfile.TemporaryDirectory() as temp:
        db = Database(Path(temp) / "impairments.sqlite3")
        await db.initialize()
        bot_module.bot.db = db
        character_id = await db.create_character(1, 100, "Тест", "Увечий", "Окопник", "Крысы")
        await db.set_attributes(character_id, {name: 5 for name in bot_module.ATTRIBUTES})

        await db.add_injury(character_id, "Телосложение", PHYSICAL_TRAUMAS[71])
        character = await db.character(1, 100)
        pool = bot_module.make_pool(character, "Драка")
        assert len(pool.negative_dice) == 8  # базовый навык −3 и увечье −5
        assert ("Увечье «Потеря левой руки»", -5) in pool.skill_modifier_details

        catalog = await db.catalog_items(1, "", 1000)
        two_handed = next(item for item in catalog if int(item.get("hands") or 0) >= 2)
        weapon_row = await db.give_item(character_id, two_handed)
        ok, message = await db.set_equipped(character_id, weapon_row, True)
        assert not ok and "потеря руки" in message.casefold()

        arm = next(item for item in catalog if item.get("category") == "Протезы" and item.get("prosthetic_slot") == "Рука")
        arm_row = await db.give_item(character_id, arm)
        assert (await db.set_equipped(character_id, arm_row, True, "Левая рука"))[0]
        assert (await db.set_equipped(character_id, weapon_row, True))[0]
        compensated = await db.character(1, 100)
        assert not bot_module.has_uncompensated_impairment(compensated, "lost_left_arm")
        assert not bot_module.uncompensated_impairment_attribute_modifiers(compensated, "Телосложение")

        await db.add_injury(character_id, "Эмпатия", PHYSICAL_TRAUMAS[76])
        skin_character = await db.character(1, 100)
        empathy_pool = bot_module.make_pool(skin_character, "Влияние")
        assert ("Увечье «Обширная потеря кожи»", -5) in empathy_pool.skill_modifier_details
        before_physique = skin_character["attributes"]["Телосложение"]["current"]
        before_agility = skin_character["attributes"]["Ловкость"]["current"]
        messages = await bot_module.apply_post_roll_impairment_cost(skin_character, "Ловкость")
        after = await db.character(1, 100)
        assert messages
        assert after["attributes"]["Телосложение"]["current"] == before_physique - 1
        assert after["attributes"]["Ловкость"]["current"] == before_agility - 1

    print("impairment test: OK")


if __name__ == "__main__":
    asyncio.run(main())
