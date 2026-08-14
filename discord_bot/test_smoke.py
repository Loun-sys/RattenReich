import json
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import bot as bot_module
from card_renderer import CardRenderer
from constants import RANKS
from database import Database
from trauma_data import MENTAL_TRAUMAS, PHYSICAL_TRAUMAS


async def main():
    with tempfile.TemporaryDirectory() as temp:
        db = Database(Path(temp) / "test.sqlite3")
        await db.initialize()
        base_catalog = await db.catalog_items(1, "", 500)
        assert len(base_catalog) == 333 and all(item["conditions"] for item in base_catalog)
        assert len({item["source_number"] for item in base_catalog}) == len(base_catalog)
        spark = next(item for item in base_catalog if item["name"] == "Ранцевый огнемёт «Искра»")
        assert spark["gear"] == 1 and spark["damage"] == 1 and spark["access"] == "Общедоступное"
        assert "игнорирует броню" in spark["conditions"].casefold()
        registration = bot_module.RegistrationFlow()
        assert len(registration.children) == 4
        assert registration.children[2].disabled
        registration.class_name = "Солдат"
        registration.race = "Крысы"
        registration.rebuild()
        assert len(registration.children) == 4
        assert not registration.children[2].disabled
        assert next(option for option in registration.children[0].options if option.label == "Солдат").default
        assert next(option for option in registration.children[1].options if option.label == "Крысы").default
        assert registration.continue_registration.row == 3
        assert not any(item["source_number"] in {55, 56, 58, 60} for item in base_catalog)
        assert not any("самодельн" in item["conditions"].casefold() for item in base_catalog)
        character_id = await db.create_character(1, 2, "Шрам", "Конрад", "Солдат", "Крысы")
        await db.set_attributes(character_id, {"Телосложение": 5, "Ловкость": 4, "Смекалка": 3, "Эмпатия": 2})
        await db.set_skill(character_id, "Драка", 2)
        before, after = await db.damage(character_id, "Телосложение", 5)
        assert (before, after) == (5, 0)
        await db.add_injury(character_id, "Телосложение", PHYSICAL_TRAUMAS[34])
        await db.add_injury(character_id, "Телосложение", PHYSICAL_TRAUMAS[71])
        await db.add_injury(character_id, "Ловкость", PHYSICAL_TRAUMAS[11])
        removed = await db.cleanup_expired_injuries()
        assert removed == 1
        active_injuries = await db.list_rows("injuries", character_id)
        assert len(active_injuries) == 2
        amputation = next(row for row in active_injuries if row["roll_code"] == 71)
        assert amputation["expires_at"] is None
        assert amputation["impairment_key"] == "lost_left_arm"
        assert amputation["compensation_position"] == "Левая рука"
        await db.add_pending_injury(character_id, "Смекалка", 34)
        assert await db.delete_injury_by_code(character_id, 34, "psychological")
        remaining = await db.list_rows("injuries", character_id)
        assert len(remaining) == 1 and remaining[0]["attribute_name"] == "Телосложение"
        character = await db.character(1, 2)
        assert character and character["infection"] == 2 and character["skills"]["Драка"] == 2
        assert len(MENTAL_TRAUMAS) == 36 and set(MENTAL_TRAUMAS) == {
            first * 10 + second for first in range(1, 7) for second in range(1, 7)
        }
        await db.create_character(1, 2, "Шрам", "Конрад", "Санитар", "Крысы")
        character = await db.character(1, 2)
        assert "Лечение" in character["skills"] and "Обращение" not in character["skills"]
        recipient_id = await db.create_character(1, 3, "Клык", "Отто", "Солдат", "Крысы")
        await db.update_character(character_id, "supply_forms", 5)
        sender_balance, recipient_balance = await db.transfer_supply(character_id, recipient_id, 3)
        assert (sender_balance, recipient_balance) == (2, 3)
        cockroach_id = await db.create_character(1, 5, "Жёсткий", "Карл", "Солдат", "Тараканы")
        cockroach = await db.character(1, 5)
        assert cockroach["hands"] == 4
        await db.create_character(1, 5, "Жёсткий", "Карл", "Солдат", "Крысы")
        cockroach = await db.character(1, 5)
        assert cockroach["hands"] == 2
        await db.create_character(1, 5, "Жёсткий", "Карл", "Солдат", "Тараканы")
        cockroach = await db.character(1, 5)
        small_armor = await db.catalog_item(1, "Пехотная каска")
        other_small_armor = await db.catalog_item(1, "Усиленные перчатки")
        large_armor = await db.catalog_item(1, "Стальной нагрудник")
        small_row = await db.give_item(cockroach_id, small_armor)
        other_small_row = await db.give_item(cockroach_id, other_small_armor)
        large_row = await db.give_item(cockroach_id, large_armor)
        assert (await db.set_equipped(cockroach_id, small_row, True))[0]
        assert not (await db.set_equipped(cockroach_id, other_small_row, True))[0]
        assert (await db.set_equipped(cockroach_id, large_row, True))[0]
        multilayer_armor = await db.catalog_item(1, "Экспериментальная многослойная броня")
        assert multilayer_armor["max_durability"] == 8
        assert "неразрушаемый куб защиты" in small_armor["description"]
        assert bot_module.armor_indestructible_dice(
            small_armor,
            {"damage_type": "Огненный", "properties": "Взрывное", "conditions": ""},
            "Ближняя",
        ) == 1
        mental_id = await db.create_character(1, 4, "Тихий", "Макс", "Солдат", "Крысы")
        await db.set_attributes(mental_id, {"Телосложение": 1, "Ловкость": 1, "Смекалка": 1, "Эмпатия": 1})
        bot_module.bot.db = db
        mental_character = await db.character(1, 4)
        with patch("bot.secrets.randbelow", side_effect=[2, 2]):
            await bot_module.apply_damage(mental_character, "Смекалка", 1)
        mental_character = await db.character(1, 4)
        assert mental_character["will_current"] == 9 and mental_character["infection"] == 0
        assert len(await db.list_rows("injuries", mental_id)) == 1

        item_id = await db.create_catalog_item(1, 99, {
            "name": "Карабин", "size": "Большой", "category": "Оружие дальнего боя", "durability": 6,
            "description": "Штатное оружие", "range": "Средняя", "ammo": 5, "fire_rate": 2,
            "attribute_modifiers": {}, "skill_modifiers": {"Стрельба": 1},
        })
        item = await db.catalog_item(1, "Карабин")
        assert item and item["id"] == item_id
        row_id = await db.give_item(character_id, item)
        assert await db.update_inventory_state(row_id, character_id, 4, 3)
        assert await db.adjust_inventory_durability(row_id, character_id, 10) == 6
        assert await db.adjust_inventory_durability(row_id, character_id, -20) == 0
        await db.give_item(character_id, item, 3)
        assert await db.remove_inventory_by_name(character_id, "карабин", 2)
        inventory = await db.inventory(character_id)
        assert sum(row["quantity"] for row in inventory if row["name"] == "Карабин") == 2
        inventory_character = await db.character(1, 2)
        inventory_embed = await bot_module.build_inventory_embed(inventory_character)
        assert "Большие: **2/5**" in inventory_embed.fields[0].value
        assert "#" not in (inventory_embed.description or "")
        assert (inventory_embed.description or "").count("**Карабин —") == 2
        inventory_view = bot_module.InventoryActionsView(inventory_character, inventory)
        assert any(isinstance(child, bot_module.InventoryItemSelect) for child in inventory_view.children)
        admin_inventory_view = bot_module.AdminInventoryActionsView(inventory_character, inventory)
        assert {getattr(child, "label", None) for child in admin_inventory_view.children} >= {
            "Добавить +", "Удалить −",
        }
        cockroach_embed = await bot_module.build_inventory_embed(cockroach, "Малый")
        assert "────────────" in (cockroach_embed.description or "")
        assert "защита " in (cockroach_embed.description or "")
        assert "качества" not in (cockroach_embed.description or "")
        assert "неразрушаемый куб защиты" in (cockroach_embed.description or "")
        seven_items = [dict(inventory[0], id=index, size="Большой") for index in range(1, 8)]
        first_page, page, pages = bot_module.inventory_page_items(seven_items, "Большой", 0)
        second_page, _, _ = bot_module.inventory_page_items(seven_items, "Большой", 1)
        assert (len(first_page), len(second_page), page, pages) == (6, 1, 0, 2)
        active_row_id = next(row["id"] for row in inventory if row["name"] == "Карабин")
        assert await db.update_inventory_state(active_row_id, character_id, 6, 3)
        assert await db.consume_ammo(active_row_id, character_id, 2) == 1
        pool = bot_module.RollPool(
            attribute="Телосложение",
            skill="Драка",
            attribute_dice=[],
            skill_dice=[6],
            negative_dice=[6, 6],
            gear_dice={active_row_id: [1, 4]},
        )
        assert pool.successes == -1
        await bot_module.apply_push_cost(pool, inventory_character)
        pushed_item = next(row for row in await db.inventory(character_id) if row["id"] == active_row_id)
        assert pushed_item["durability"] == 5
        command_names = {command.name for command in bot_module.bot.tree.get_commands()}
        assert "каталог" not in command_names
        assert {
            "ударить", "выстрелить", "перезарядить",
            "крысиное-превозмогание", "заражение",
            "гм-атака", "нпс-создать", "нпс-ударить", "нпс-выстрелить",
            "нпс-список", "нпс-удалить", "защита-нпс", "снижение-урона-нпс",
            "магазин", "предмет-передать", "все-предметы", "редактировать-цены",
            "навыки-завершить", "магазин-навыков", "навык-изменить",
            "талант-выдать", "посмотреть-таланты",
            "магазин-талантов", "разрядить", "дозарядить",
        } <= command_names
        assert "драка" not in command_names

        revolver = await db.catalog_item(1, "Армейский револьвер")
        pistol_ammo = await db.catalog_item(1, "Пистолетные боеприпасы")
        revolver_row = await db.give_item(character_id, revolver)
        await db.give_item(character_id, revolver, 2)
        revolver_instances = [
            row for row in await db.inventory(character_id)
            if row["name"] == "Армейский револьвер"
        ]
        assert len(revolver_instances) == 3
        assert all(row["quantity"] == 1 for row in revolver_instances)
        assert (await db.set_equipped(character_id, revolver_row, True))[0]
        assert await db.consume_ammo(revolver_row, character_id, 2) == revolver["ammo_max"] - 2
        await db.give_item(character_id, pistol_ammo, 1)
        await db.give_item(character_id, pistol_ammo, 1)
        ammo_stacks = [
            row for row in await db.inventory(character_id)
            if row["name"] == "Пистолетные боеприпасы"
        ]
        assert len(ammo_stacks) == 1 and ammo_stacks[0]["quantity"] == 2
        reload_result = await db.reload_weapon(
            revolver_row,
            character_id,
            "Пистолетные боеприпасы",
        )
        assert reload_result == (revolver["ammo_max"] - 2, revolver["ammo_max"])
        reloaded = next(row for row in await db.inventory(character_id) if row["id"] == revolver_row)
        assert reloaded["ammo"] == revolver["ammo_max"]
        assert await db.adjust_inventory_ammo(revolver_row, character_id, -2) == (
            revolver["ammo_max"], revolver["ammo_max"] - 2, revolver["ammo_max"]
        )
        assert await db.adjust_inventory_ammo(revolver_row, character_id, 999) == (
            revolver["ammo_max"] - 2, revolver["ammo_max"], revolver["ammo_max"]
        )
        ammo_left = [
            row for row in await db.inventory(character_id)
            if row["name"] == "Пистолетные боеприпасы"
        ]
        assert sum(row["quantity"] for row in ammo_left) == 1
        assert not bot_module.is_general_roll_gear(revolver)
        assert not bot_module.is_general_roll_gear(pistol_ammo)
        binoculars = await db.catalog_item(1, "Бинокль дозорного")
        assert bot_module.is_general_roll_gear(binoculars)
        sapper_armor = await db.catalog_item(1, "Сапёрный нагрудник")
        shooting_weapon = {"damage_type": "Колющий", "properties": "", "conditions": ""}
        explosion = {"damage_type": "Взрывной", "properties": "Осколочный", "conditions": ""}
        assert bot_module.armor_indestructible_dice(sapper_armor, shooting_weapon, "Средняя") == 0
        assert bot_module.armor_indestructible_dice(sapper_armor, explosion, "Нулевая") == 4
        lamellar = await db.catalog_item(1, "Ламеллярный доспех Империи Солнца")
        lamellar["equipped"] = 1
        lamellar["durability"] = lamellar["max_durability"]
        assert bot_module.physical_armor_reduction([lamellar], "Режущий") == 1
        assert bot_module.physical_armor_reduction([lamellar], "Колющий") == 0
        steel = await db.catalog_item(1, "Стальной нагрудник")
        assert bot_module.equipment_success_modifier([steel], "Стрельба") == -1
        assert bot_module.equipment_success_modifier([steel], "Драка") == 0

        supplier_id = await db.create_character(1, 10, "Кладов", "Эрих", "Снабженец", "Крысы")
        receiver_id = await db.create_character(1, 11, "Рот", "Вальтер", "Солдат", "Крысы")
        await db.set_attributes(supplier_id, {"Телосложение": 5, "Ловкость": 5, "Смекалка": 2, "Эмпатия": 2})
        await db.set_attributes(receiver_id, {"Телосложение": 2, "Ловкость": 2, "Смекалка": 5, "Эмпатия": 5})
        await db.set_skill(supplier_id, "Снабжение", 1)
        await db.update_character(supplier_id, "supply_forms", 50)
        knife = await db.catalog_item(1, "Окопный нож")
        supplier = await db.character(1, 10)
        assert bot_module.can_purchase(supplier, knife, "Оружие ближнего боя")
        visible_level_one = bot_module.visible_store_items(
            supplier, await db.catalog_items(1, "", 500), "Оружие ближнего боя"
        )
        assert knife in visible_level_one
        purchased, _, balance = await db.purchase_item(supplier_id, knife["id"], 0)
        assert purchased and balance == 50 - knife["price"]
        saber = await db.catalog_item(1, "Офицерская сабля")
        assert saber not in visible_level_one
        assert not bot_module.can_purchase(supplier, saber, "Оружие ближнего боя")
        denied, _, _ = await db.purchase_item(supplier_id, saber["id"], 2)
        assert not denied
        await db.set_skill(supplier_id, "Снабжение", 3)
        supplier = await db.character(1, 10)
        assert bot_module.can_purchase(supplier, saber, "Оружие ближнего боя")
        assert saber in bot_module.visible_store_items(
            supplier, await db.catalog_items(1, "", 500), "Оружие ближнего боя"
        )
        transferred, _ = await db.transfer_item(supplier_id, receiver_id, "Окопный нож", 1)
        assert transferred and await db.inventory_item_by_name(receiver_id, "Окопный нож")
        store_items = await db.catalog_items(1, "", 500)
        store_view = bot_module.StoreView(supplier, store_items)
        assert "Купить" not in {getattr(child, "label", None) for child in store_view.children}
        assert len(bot_module.TALENTS) == 194
        purchasable_talents = [talent for talent in bot_module.TALENTS if not talent["starter"]]
        assert all(talent["price"] == 16 for talent in purchasable_talents)
        assert sum(talent["kind"] == "skill" for talent in bot_module.TALENTS) == 57
        assert sum(talent["kind"] == "class_progression" for talent in bot_module.TALENTS) == 64
        for class_name in bot_module.CLASS_TALENTS:
            new_class = [
                talent for talent in bot_module.TALENTS
                if talent["class_name"] == class_name and talent["skill_requirements"]
            ]
            assert len(new_class) >= 10
        assert len(bot_module.available_talents(supplier)) == 3
        bureaucracy = bot_module.TALENT_BY_NAME["бюрократия"]
        assert await db.grant_talent(supplier_id, bureaucracy["name"], bureaucracy["description"])
        supplier = await db.character(1, 10)
        assert bot_module.store_price(supplier, knife) == max(1, knife["price"] - 1)
        assert all(talent["kind"] == "general" for talent in bot_module.available_talents(supplier))
        assert all(talent["rank_required"] == 0 for talent in bot_module.available_talents(supplier))
        assert 1 <= len(bot_module.available_talents(supplier)) <= 12
        gated = bot_module.TALENT_BY_NAME["второе дыхание"]
        assert gated not in bot_module.available_talents(supplier)
        denied_talent, denied_message, _ = await db.purchase_talent(
            supplier_id, gated["name"], gated["description"], gated["price"],
            gated["rank_required"], gated["class_name"], (), gated["skill_requirements"],
        )
        assert not denied_talent and "Требуется навык Выносливость 1" in denied_message
        await db.set_skill(supplier_id, "Выносливость", 1)
        supplier = await db.character(1, 10)
        assert gated in bot_module.available_talents(supplier)
        ammo_for_sale = await db.catalog_item(1, "Пистолетные боеприпасы")
        balance_before = supplier["supply_forms"]
        bought, _, balance_after = await db.purchase_item(supplier_id, ammo_for_sale["id"], 0)
        assert bought and balance_after == balance_before - max(1, ammo_for_sale["price"] - 1)
        first_general = bot_module.available_talents(await db.character(1, 10))[0]
        bought_talent, _, _ = await db.purchase_talent(
            supplier_id, first_general["name"], first_general["description"],
            first_general["price"], first_general["rank_required"],
        )
        assert bought_talent
        dog = dict(supplier, race="Псовые")
        marsupial = dict(supplier, race="Сумчатые")
        assert bot_module.racial_skill_bonus(dog, "Наблюдательность") == 1
        assert bot_module.racial_skill_bonus(marsupial, "Снабжение") == 1
        healthy = bot_module.TALENT_BY_NAME["здоровяк"]
        await db.grant_talent(character_id, healthy["name"], healthy["description"])
        healthy_character = await db.character(1, 2)
        assert len(bot_module.make_pool(healthy_character, "Драка").attribute_dice) == 5
        clear_eyed = bot_module.TALENT_BY_NAME["глазомер"]
        await db.grant_talent(character_id, clear_eyed["name"], clear_eyed["description"])
        healthy_character = await db.character(1, 2)
        assert bot_module.shooting_talent_distance_modifier(healthy_character, -2) == -1
        assert bot_module.shooting_talent_distance_modifier(healthy_character, 0) == 0
        pocket = bot_module.TALENT_BY_NAME["карманный склад"]
        harness = bot_module.TALENT_BY_NAME["вьючный ремень"]
        await db.grant_talent(character_id, pocket["name"], pocket["description"])
        await db.grant_talent(character_id, harness["name"], harness["description"])
        slotted = await db.character(1, 2)
        assert bot_module.inventory_slot_capacities(slotted) == (5, 6)

        # New talent dice are mechanical and shown separately.
        await db.grant_talent(character_id, "Второе дыхание", bot_module.TALENT_BY_NAME["второе дыхание"]["description"])
        await db.set_skill(character_id, "Выносливость", 1)
        bonus_character = await db.character(1, 2)
        bonus_pool = bot_module.make_pool(bonus_character, "Выносливость")
        assert any(name == "Талант «Второе дыхание»" and value == 1 for name, value in bonus_pool.skill_modifier_details)
        assert len(bonus_pool.skill_dice) == 2
        permanent = dict(bonus_character, skills=dict(bonus_character["skills"]))
        permanent["skills"]["Лечение"] = 6
        guaranteed_pool = bot_module.make_pool(permanent, "Лечение", custom_modifier=10)
        assert guaranteed_pool.flat_success_modifier == 1
        assert guaranteed_pool.success_modifier_details == [("Постоянный навык выше 5", 1)]

        # Starting points freeze once, subsequent improvement costs exactly 8 BS.
        progression_id = await db.create_character(1, 20, "Проба", "Навык", "Солдат", "Мыши")
        await db.set_skill(progression_id, "Драка", 5)
        await db.set_skill(progression_id, "Стрельба", 1)
        progression = await db.character(1, 20)
        assert not bot_module.starting_skills_ready(progression)
        await db.update_character(progression_id, "supply_forms", 16)
        ok, _, remaining = await db.purchase_skill(progression_id, "Стрельба", 5, 8)
        assert ok and remaining == 8
        progression = await db.character(1, 20)
        assert bot_module.starting_skills_ready(progression)
        ok, message = await db.finalize_starting_skills(progression_id, 12)
        assert not ok and "уже" in message

        # Cap-7 talents and high Supply discounts are enforced by both UI and DB.
        cap_talent = bot_module.TALENT_BY_NAME["мелкий шрифт"]
        await db.grant_talent(supplier_id, cap_talent["name"], cap_talent["description"])
        await db.set_skill(supplier_id, "Снабжение", 7)
        supplier = await db.character(1, 10)
        assert bot_module.character_skill_cap(supplier, "Снабжение") == 7
        assert bot_module.store_price(supplier, knife) == max(1, knife["price"] - 2)
        await db.update_catalog_price("Окопный нож", 9)
        changed_knife = await db.catalog_item(1, "Окопный нож")
        assert changed_knife["price"] == 9
        await db.reload_base_catalog()
        assert (await db.catalog_item(1, "Окопный нож"))["price"] == 9

        calm = bot_module.TALENT_BY_NAME["спокойный спуск"]
        await db.grant_talent(character_id, calm["name"], calm["description"])
        calm_character = await db.character(1, 2)
        calm_view = bot_module.AttackView(2, None, calm_character, [pool], {"name": "Пистолет", "fire_rate": 1, "damage": 1, "conditions": "", "damage_type": "", "properties": ""}, True)
        assert not calm_view.push_button.disabled
        ordinary_view = bot_module.AttackView(2, None, inventory_character, [pool], {"name": "Автомат", "fire_rate": 2, "damage": 1, "conditions": "", "damage_type": "", "properties": ""}, True)
        assert ordinary_view.push_button.disabled
        npc_push_view = bot_module.AttackView(99, None, {}, [pool], {"name": "НПС", "fire_rate": 3, "damage": 1, "conditions": "", "damage_type": "", "properties": ""}, True, attacker_npc={"id": 1})
        assert not npc_push_view.push_button.disabled

        rat_status, rat_payload = await db.rat_recover(character_id, "Телосложение")
        assert rat_status == "ok" and rat_payload[0:2] == (0, 1)
        cooldown_status, _ = await db.rat_recover(character_id, "Ловкость")
        assert cooldown_status == "cooldown"
        infection_before_after = await db.adjust_infection(character_id, 99)
        assert infection_before_after[1] == 5
        infection_before_after = await db.adjust_infection(character_id, -99)
        assert infection_before_after == (5, 0)

        npc_id = await db.create_npc(
            1, "Манекен", 12, 8, 3, 5, 4, 2, 3,
            "Дробящий", "Колющий", "Учебная цель", 2, 4, '{"Колющий": 2}',
        )
        npc = await db.npc(1, "манекен")
        assert npc["id"] == npc_id and npc["physique"] == 12 and npc["agility"] == 8
        assert (await db.damage_npc(npc_id, 5)) == (12, 7)
        assert (await db.damage_npc_attribute(npc_id, "Ловкость", 3)) == (8, 5)
        assert (await db.heal_npc_attribute(npc_id, "Ловкость", 2)) == (5, 7)
        assert (await db.adjust_npc_defense(npc_id, -2)) == (3, 1)
        assert (await db.adjust_npc_protection(npc_id, "Щит", -1)) == (2, 1)
        assert (await db.adjust_npc_protection(npc_id, "Щит", 50)) == (1, 2)
        assert (await db.adjust_npc_protection(npc_id, "Неразрушимая защита", -3)) == (4, 1)
        assert await db.set_npc_damage_reduction(npc_id, "Режущий", 3)
        reduced_npc = await db.npc(1, "Манекен")
        assert json.loads(reduced_npc["damage_reductions"]) == {"Колющий": 2, "Режущий": 3}
        listed_npc = (await db.npcs(1, "ман"))[0]
        assert listed_npc["physique"] == 7 and listed_npc["agility"] == 7
        assert await db.delete_npc(1, "МАНЕКЕН")
        assert await db.npc(1, "Манекен") is None

        attack_view = bot_module.AttackView(
            2,
            3,
            inventory_character,
            [pool, pool],
            reloaded,
            True,
        )
        assert attack_view.attack_embed().fields[-1].name == "Общий итог очереди"
        assert any(getattr(child, "label", None) == "Защищаться" for child in attack_view.children)
        modified_attack = bot_module.AttackView(
            2, None, inventory_character, [pool], reloaded, False,
            damage_modifier=3,
        )
        assert "модификатор урона: **+3**" in modified_attack.attack_embed().fields[-1].value

        renderer = CardRenderer(Path(__file__).parent / "assets")
        for rank_index, _ in enumerate(RANKS):
            character["rank_index"] = rank_index
            output = renderer.render(character)
            image = Image.open(output)
            assert image.size == (1330, 1548)
    print("smoke test: OK")


if __name__ == "__main__":
    asyncio.run(main())







