from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from catalog_loader import MEDICAL_CONSUMABLES, MULTI_USE_CONSUMABLES, catalog_price_increase, is_consumable_item, load_catalog
from attachment_data import ATTACHMENT_BY_NAME, apply_attachments, compatible
from constants import ATTRIBUTES, CLASSES, INVENTORY_CAPACITY_ITEMS, SKILLS
from talent_data import TALENTS
from medal_data import MEDALS, medal_bonus_summary
from trauma_data import PHYSICAL_TRAUMAS


AMMO_PACKAGE_MIGRATIONS = {
    151: (502, "Средняя упаковка пистолетных боеприпасов (6)"),
    152: (505, "Средняя упаковка винтовочных боеприпасов (6)"),
    153: (508, "Средняя упаковка дробовых боеприпасов (6)"),
    234: (511, "Средняя упаковка сигнальных ракет (6)"),
    235: (514, "Средняя упаковка гарпунов (6)"),
    236: (517, "Средняя упаковка закалённых игл (6)"),
    245: (520, "Средняя упаковка огнесмеси (6)"),
}


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS characters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    surname TEXT NOT NULL,
    name TEXT NOT NULL,
    class_name TEXT NOT NULL,
    race TEXT NOT NULL,
    rank_index INTEGER NOT NULL DEFAULT 0,
    will_current INTEGER NOT NULL DEFAULT 10,
    will_max INTEGER NOT NULL DEFAULT 10,
    supply_forms INTEGER NOT NULL DEFAULT 0,
    infection INTEGER NOT NULL DEFAULT 0,
    rat_recovery_at TEXT,
    hands INTEGER NOT NULL DEFAULT 2,
    photo_path TEXT,
    notes TEXT NOT NULL DEFAULT '',
    skills_initialized INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS attributes (
    character_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    current_value INTEGER NOT NULL,
    max_value INTEGER NOT NULL,
    PRIMARY KEY(character_id, name)
);
CREATE TABLE IF NOT EXISTS skills (
    character_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT -3,
    PRIMARY KEY(character_id, name)
);
CREATE TABLE IF NOT EXISTS talents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS injuries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    attribute_name TEXT NOT NULL,
    roll_code INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    penalties TEXT NOT NULL DEFAULT '',
    duration TEXT NOT NULL DEFAULT '',
    impairment_key TEXT NOT NULL DEFAULT '',
    compensation_position TEXT NOT NULL DEFAULT '',
    expires_at TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS item_catalog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    size TEXT NOT NULL,
    category TEXT NOT NULL,
    max_durability INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    use_range TEXT,
    ammo_max INTEGER,
    fire_rate INTEGER,
    attribute_modifiers TEXT NOT NULL DEFAULT '{}',
    skill_modifiers TEXT NOT NULL DEFAULT '{}',
    source_number INTEGER,
    hands INTEGER NOT NULL DEFAULT 0,
    gear INTEGER NOT NULL DEFAULT 0,
    damage INTEGER NOT NULL DEFAULT 0,
    damage_type TEXT NOT NULL DEFAULT '',
    defense INTEGER NOT NULL DEFAULT 0,
    price INTEGER NOT NULL DEFAULT 0,
    access TEXT NOT NULL DEFAULT 'Общедоступное',
    properties TEXT NOT NULL DEFAULT '',
    conditions TEXT NOT NULL DEFAULT '',
    armor_slot TEXT,
    prosthetic_slot TEXT,
    will_cost INTEGER NOT NULL DEFAULT 0,
    icon_file TEXT,
    created_by INTEGER NOT NULL,
    UNIQUE(guild_id, name)
);
CREATE TABLE IF NOT EXISTS item_price_overrides (
    name TEXT PRIMARY KEY,
    price INTEGER NOT NULL CHECK(price >= 0)
);
CREATE TABLE IF NOT EXISTS purchase_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    character_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL,
    item_name TEXT NOT NULL,
    paid_price INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    ordered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TEXT,
    reviewed_by INTEGER
);
CREATE INDEX IF NOT EXISTS idx_purchase_orders_guild_status ON purchase_orders(guild_id,status,ordered_at);
CREATE TABLE IF NOT EXISTS luck_modifiers (
    user_id INTEGER PRIMARY KEY,
    percent INTEGER NOT NULL DEFAULT 0 CHECK(percent BETWEEN -100 AND 100),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS supply_warehouse (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL REFERENCES item_catalog(id) ON DELETE CASCADE,
    durability INTEGER NOT NULL DEFAULT 0,
    ammo INTEGER,
    quantity INTEGER NOT NULL DEFAULT 1,
    deposited_by INTEGER,
    deposited_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_supply_warehouse_guild ON supply_warehouse(guild_id);
CREATE TABLE IF NOT EXISTS motor_pool (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL REFERENCES item_catalog(id) ON DELETE RESTRICT,
    quantity INTEGER NOT NULL DEFAULT 1,
    purchased_by INTEGER,
    added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(guild_id,item_id)
);
CREATE INDEX IF NOT EXISTS idx_motor_pool_guild ON motor_pool(guild_id);
CREATE TABLE IF NOT EXISTS motor_pool_funds (
    guild_id INTEGER PRIMARY KEY,
    balance INTEGER NOT NULL DEFAULT 0,
    maintenance_active INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS app_migrations (
    key TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES item_catalog(id) ON DELETE CASCADE,
    durability INTEGER NOT NULL DEFAULT 0,
    ammo INTEGER,
    quantity INTEGER NOT NULL DEFAULT 1,
    equipped INTEGER NOT NULL DEFAULT 0,
    equipped_position TEXT,
    notes TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_inventory_character ON inventory(character_id);
CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS weapon_attachments (
    weapon_inventory_id INTEGER NOT NULL REFERENCES inventory(id) ON DELETE CASCADE,
    attachment_inventory_id INTEGER NOT NULL UNIQUE REFERENCES inventory(id) ON DELETE CASCADE,
    slot TEXT NOT NULL,
    PRIMARY KEY(weapon_inventory_id,slot)
);
CREATE INDEX IF NOT EXISTS idx_injuries_character ON injuries(character_id, active);
CREATE TABLE IF NOT EXISTS character_effects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    source_name TEXT NOT NULL,
    attribute_name TEXT,
    modifier INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_effects_character ON character_effects(character_id, expires_at);
CREATE TABLE IF NOT EXISTS medal_catalog (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    image TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    effect TEXT NOT NULL DEFAULT '',
    ribbon TEXT NOT NULL DEFAULT '7A1F24',
    metal TEXT NOT NULL DEFAULT 'C9A54A'
);
CREATE TABLE IF NOT EXISTS character_medals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    medal_code TEXT NOT NULL REFERENCES medal_catalog(code) ON DELETE RESTRICT,
    reason TEXT NOT NULL,
    awarded_by INTEGER NOT NULL,
    awarded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(character_id, medal_code)
);
CREATE INDEX IF NOT EXISTS idx_character_medals_character ON character_medals(character_id, awarded_at);
CREATE TABLE IF NOT EXISTS npcs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    health INTEGER NOT NULL,
    max_health INTEGER NOT NULL,
    defense INTEGER NOT NULL DEFAULT 0,
    attack_dice INTEGER NOT NULL DEFAULT 1,
    damage INTEGER NOT NULL DEFAULT 1,
    description TEXT NOT NULL DEFAULT '',
    physique INTEGER NOT NULL DEFAULT 1,
    physique_max INTEGER NOT NULL DEFAULT 1,
    agility INTEGER NOT NULL DEFAULT 1,
    agility_max INTEGER NOT NULL DEFAULT 1,
    defense_max INTEGER NOT NULL DEFAULT 0,
    shield INTEGER NOT NULL DEFAULT 0,
    shield_max INTEGER NOT NULL DEFAULT 0,
    indestructible_defense INTEGER NOT NULL DEFAULT 0,
    indestructible_defense_max INTEGER NOT NULL DEFAULT 0,
    damage_reductions TEXT NOT NULL DEFAULT '{}',
    fight_skill INTEGER NOT NULL DEFAULT 0,
    shooting_skill INTEGER NOT NULL DEFAULT 0,
    melee_damage INTEGER NOT NULL DEFAULT 1,
    ranged_damage INTEGER NOT NULL DEFAULT 1,
    melee_damage_type TEXT NOT NULL DEFAULT 'Дробящий',
    ranged_damage_type TEXT NOT NULL DEFAULT 'Колющий',
    UNIQUE(guild_id,name)
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    @asynccontextmanager
    async def connect(self):
        db = await aiosqlite.connect(self.path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        try:
            yield db
        finally:
            await db.close()

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self.connect() as db:
            await db.executescript(SCHEMA)
            columns = await db.execute_fetchall("PRAGMA table_info(injuries)")
            if "expires_at" not in {row["name"] for row in columns}:
                await db.execute("ALTER TABLE injuries ADD COLUMN expires_at TEXT")
                await db.execute(
                    "UPDATE injuries SET expires_at=datetime(created_at, '+' || CAST(substr(duration,1,instr(duration,' ')-1) AS INTEGER) || ' hours') WHERE duration LIKE '% ч.'"
                )
                await db.execute("UPDATE injuries SET expires_at=created_at WHERE duration='Мгновенное'")
            injury_columns = {row["name"] for row in await db.execute_fetchall("PRAGMA table_info(injuries)")}
            if "impairment_key" not in injury_columns:
                await db.execute("ALTER TABLE injuries ADD COLUMN impairment_key TEXT NOT NULL DEFAULT ''")
            if "compensation_position" not in injury_columns:
                await db.execute("ALTER TABLE injuries ADD COLUMN compensation_position TEXT NOT NULL DEFAULT ''")
            for roll_code in range(71, 77):
                trauma = PHYSICAL_TRAUMAS[roll_code]
                await db.execute(
                    """UPDATE injuries
                       SET name=?,description=?,penalties=?,duration=?,impairment_key=?,compensation_position=?,expires_at=NULL
                       WHERE roll_code=?""",
                    (
                        trauma.name, trauma.description, trauma.penalties, trauma.duration,
                        trauma.impairment_key, trauma.compensation_position, roll_code,
                    ),
                )
            character_columns = {row["name"] for row in await db.execute_fetchall("PRAGMA table_info(characters)")}
            if "hands" not in character_columns:
                await db.execute("ALTER TABLE characters ADD COLUMN hands INTEGER NOT NULL DEFAULT 2")
                await db.execute("UPDATE characters SET hands=4 WHERE race='Тараканы'")
            if "rat_recovery_at" not in character_columns:
                await db.execute("ALTER TABLE characters ADD COLUMN rat_recovery_at TEXT")
            if "skills_initialized" not in character_columns:
                await db.execute("ALTER TABLE characters ADD COLUMN skills_initialized INTEGER NOT NULL DEFAULT 1")
            medal_columns = {row["name"] for row in await db.execute_fetchall("PRAGMA table_info(medal_catalog)")}
            if "image" not in medal_columns:
                await db.execute("ALTER TABLE medal_catalog ADD COLUMN image TEXT NOT NULL DEFAULT ''")
            inventory_columns = {row["name"] for row in await db.execute_fetchall("PRAGMA table_info(inventory)")}
            if "equipped" not in inventory_columns:
                await db.execute("ALTER TABLE inventory ADD COLUMN equipped INTEGER NOT NULL DEFAULT 0")
            if "equipped_position" not in inventory_columns:
                await db.execute("ALTER TABLE inventory ADD COLUMN equipped_position TEXT")
            catalog_columns = {row["name"] for row in await db.execute_fetchall("PRAGMA table_info(item_catalog)")}
            catalog_migrations = {
                "source_number": "INTEGER",
                "hands": "INTEGER NOT NULL DEFAULT 0",
                "gear": "INTEGER NOT NULL DEFAULT 0",
                "damage": "INTEGER NOT NULL DEFAULT 0",
                "damage_type": "TEXT NOT NULL DEFAULT ''",
                "defense": "INTEGER NOT NULL DEFAULT 0",
                "price": "INTEGER NOT NULL DEFAULT 0",
                "access": "TEXT NOT NULL DEFAULT 'Общедоступное'",
                "properties": "TEXT NOT NULL DEFAULT ''",
                "conditions": "TEXT NOT NULL DEFAULT ''",
                "armor_slot": "TEXT",
                "prosthetic_slot": "TEXT",
                "will_cost": "INTEGER NOT NULL DEFAULT 0",
                "icon_file": "TEXT",
            }
            for name, definition in catalog_migrations.items():
                if name not in catalog_columns:
                    await db.execute(f"ALTER TABLE item_catalog ADD COLUMN {name} {definition}")
            motor_fund_columns = {
                row["name"] for row in await db.execute_fetchall("PRAGMA table_info(motor_pool_funds)")
            }
            if "maintenance_active" not in motor_fund_columns:
                await db.execute(
                    "ALTER TABLE motor_pool_funds ADD COLUMN maintenance_active INTEGER NOT NULL DEFAULT 0"
                )
            npc_columns = {row["name"] for row in await db.execute_fetchall("PRAGMA table_info(npcs)")}
            npc_migrations = {
                "physique": "INTEGER NOT NULL DEFAULT 1",
                "physique_max": "INTEGER NOT NULL DEFAULT 1",
                "agility": "INTEGER NOT NULL DEFAULT 1",
                "agility_max": "INTEGER NOT NULL DEFAULT 1",
                "defense_max": "INTEGER NOT NULL DEFAULT 0",
                "shield": "INTEGER NOT NULL DEFAULT 0",
                "shield_max": "INTEGER NOT NULL DEFAULT 0",
                "indestructible_defense": "INTEGER NOT NULL DEFAULT 0",
                "indestructible_defense_max": "INTEGER NOT NULL DEFAULT 0",
                "damage_reductions": "TEXT NOT NULL DEFAULT '{}'",
                "fight_skill": "INTEGER NOT NULL DEFAULT 0",
                "shooting_skill": "INTEGER NOT NULL DEFAULT 0",
                "melee_damage": "INTEGER NOT NULL DEFAULT 1",
                "ranged_damage": "INTEGER NOT NULL DEFAULT 1",
                "melee_damage_type": "TEXT NOT NULL DEFAULT 'Дробящий'",
                "ranged_damage_type": "TEXT NOT NULL DEFAULT 'Колющий'",
            }
            added_npc_columns = set()
            for name, definition in npc_migrations.items():
                if name not in npc_columns:
                    await db.execute(f"ALTER TABLE npcs ADD COLUMN {name} {definition}")
                    added_npc_columns.add(name)
            if "physique" in added_npc_columns:
                await db.execute("UPDATE npcs SET physique=health,physique_max=max_health")
            if "defense_max" in added_npc_columns:
                await db.execute("UPDATE npcs SET defense_max=defense")
            await db.execute(
                "UPDATE talents SET name='Солдат удачи' WHERE name='Проверка магазина'"
            )
            await db.execute("UPDATE talents SET name='Пересчитать стволы' WHERE name='Считать стволы'")
            attachment_renames = {
                "Открытый траншейный прицел": "Траншейный прицел",
                "Ремень быстрого хвата": "Ремень для быстрого хвата",
                "Чок полного сужения": "Сужающий чок",
                "Раструб траншейной зачистки": "Раструб",
            }
            for old_name, new_name in attachment_renames.items():
                await db.execute("UPDATE item_catalog SET name=? WHERE name=?", (new_name, old_name))
            await db.execute("UPDATE talents SET name='Дедовщина' WHERE name='Надавить званием'")
            for old_number, (new_number, new_name) in AMMO_PACKAGE_MIGRATIONS.items():
                await db.execute(
                    "UPDATE item_catalog SET source_number=?,name=? WHERE source_number=?",
                    (new_number, new_name, old_number),
                )
            for talent in TALENTS:
                await db.execute(
                    "UPDATE talents SET description=? WHERE lower(name)=lower(?)",
                    (talent["description"], talent["name"]),
                )
            medal_codes = tuple(medal["code"] for medal in MEDALS)
            medal_placeholders = ",".join("?" for _ in medal_codes)
            await db.execute(
                f"DELETE FROM character_medals WHERE medal_code NOT IN ({medal_placeholders})",
                medal_codes,
            )
            await db.execute(
                f"DELETE FROM medal_catalog WHERE code NOT IN ({medal_placeholders})",
                medal_codes,
            )
            # Names are UNIQUE. Neutralize existing rows before catalog sync so two
            # retained medals can safely exchange names in the same migration.
            for medal in MEDALS:
                await db.execute(
                    "UPDATE medal_catalog SET name=? WHERE code=? AND name<>?",
                    (f'__rr_medal_sync_{medal["code"]}', medal["code"], medal["name"]),
                )
            for medal in MEDALS:
                await db.execute(
                    """INSERT INTO medal_catalog(code,name,image,description,effect)
                       VALUES(?,?,?,?,?)
                       ON CONFLICT(code) DO UPDATE SET
                         name=excluded.name,image=excluded.image,
                         description=excluded.description,effect=excluded.effect""",
                    (medal["code"], medal["name"], medal["image"], medal["description"], medal["effect"]),
                )
            medical_price_migration = "medical_consumable_prices_2026_07_31"
            applied = await db.execute_fetchall(
                "SELECT 1 FROM app_migrations WHERE key=?",
                (medical_price_migration,),
            )
            if not applied:
                medical_prices = {
                    "Полевые бинты": 4,
                    "Индивидуальный перевязочный пакет": 6,
                    "Армейская аптечка": 8,
                    "Набор полевого санитара": 12,
                    "Нейростимулятор": 8,
                    "Успокоительный автоинъектор": 8,
                }
                for item_name, price in medical_prices.items():
                    await db.execute(
                        """INSERT INTO item_price_overrides(name,price) VALUES(?,?)
                           ON CONFLICT(name) DO UPDATE SET price=excluded.price""",
                        (item_name, price),
                    )
                await db.execute(
                    "INSERT INTO app_migrations(key) VALUES(?)",
                    (medical_price_migration,),
                )
            equipment_price_migration = "equipment_prices_plus_1_2_2026_07_31"
            equipment_prices_applied = await db.execute_fetchall(
                "SELECT 1 FROM app_migrations WHERE key=?",
                (equipment_price_migration,),
            )
            if not equipment_prices_applied:
                overrides = await db.execute_fetchall(
                    "SELECT name,price FROM item_price_overrides WHERE price>0"
                )
                for override in overrides:
                    if override["name"] in MEDICAL_CONSUMABLES:
                        continue
                    price = int(override["price"])
                    await db.execute(
                        "UPDATE item_price_overrides SET price=? WHERE name=?",
                        (price + catalog_price_increase(price), override["name"]),
                    )
                await db.execute(
                    "INSERT INTO app_migrations(key) VALUES(?)",
                    (equipment_price_migration,),
                )
            multi_use_migration = "multi_use_consumables_2026_07_31"
            multi_use_applied = await db.execute_fetchall(
                "SELECT 1 FROM app_migrations WHERE key=?",
                (multi_use_migration,),
            )
            if not multi_use_applied:
                for item_name, uses in MULTI_USE_CONSUMABLES.items():
                    await db.execute(
                        """UPDATE inventory SET durability=? WHERE item_id IN
                           (SELECT id FROM item_catalog WHERE name=?)""",
                        (uses, item_name),
                    )
                    await db.execute(
                        """UPDATE supply_warehouse SET durability=? WHERE item_id IN
                           (SELECT id FROM item_catalog WHERE name=?)""",
                        (uses, item_name),
                    )
                await db.execute(
                    "INSERT INTO app_migrations(key) VALUES(?)",
                    (multi_use_migration,),
                )
            filter_flask_migration = "filter_flask_two_uses_2026_08_14"
            filter_flask_applied = await db.execute_fetchall(
                "SELECT 1 FROM app_migrations WHERE key=?",
                (filter_flask_migration,),
            )
            if not filter_flask_applied:
                filter_flask_name = "\u0424\u043b\u044f\u0433\u0430 \u0441 \u0444\u0438\u043b\u044c\u0442\u0440\u043e\u043c"
                filter_flask_uses = MULTI_USE_CONSUMABLES[filter_flask_name]
                await db.execute(
                    """UPDATE inventory SET durability=? WHERE item_id IN
                       (SELECT id FROM item_catalog WHERE name=?)""",
                    (filter_flask_uses, filter_flask_name),
                )
                await db.execute(
                    """UPDATE supply_warehouse SET durability=? WHERE item_id IN
                       (SELECT id FROM item_catalog WHERE name=?)""",
                    (filter_flask_uses, filter_flask_name),
                )
                await db.execute(
                    "INSERT INTO app_migrations(key) VALUES(?)",
                    (filter_flask_migration,),
                )
            await db.commit()
        await self.reload_base_catalog()
        await self.migrate_legacy_optical_sights()
        await self.migrate_ammo_packages()
        await self.repair_ammo_package_contents()
        await self.repair_regressed_flame_ammo()
        await self.merge_stackable_inventory()
        await self.split_nonstackable_inventory()

    async def migrate_legacy_optical_sights(self) -> int:
        """Переносит ошибочный предмет №65 в настоящую винтовочную насадку."""
        migration_key = "legacy_optical_sight_to_attachment_v1"
        async with self.connect() as db:
            applied = await db.execute_fetchall(
                "SELECT 1 FROM app_migrations WHERE key=?",
                (migration_key,),
            )
            if applied:
                return 0
            target_rows = await db.execute_fetchall(
                """SELECT id FROM item_catalog
                   WHERE guild_id=0 AND name='Оптический прицел X4' AND category='Насадка'"""
            )
            legacy_rows = await db.execute_fetchall(
                "SELECT id FROM item_catalog WHERE guild_id=0 AND source_number=65"
            )
            if not target_rows or not legacy_rows:
                return 0
            target_id = int(target_rows[0]["id"])
            legacy_id = int(legacy_rows[0]["id"])
            cursor = await db.execute(
                "UPDATE inventory SET item_id=?,equipped=0 WHERE item_id=?",
                (target_id, legacy_id),
            )
            await db.execute(
                "UPDATE supply_warehouse SET item_id=? WHERE item_id=?",
                (target_id, legacy_id),
            )
            await db.execute(
                """UPDATE purchase_orders SET item_id=?,item_name='Оптический прицел X4'
                   WHERE item_id=?
                      OR lower(item_name)=lower('Оптический прицел')
                      OR lower(item_name)=lower('Оптический прицел ×2')""",
                (target_id, legacy_id),
            )
            await db.execute("INSERT INTO app_migrations(key) VALUES(?)", (migration_key,))
            await db.commit()
            return cursor.rowcount

    async def repair_regressed_flame_ammo(self) -> int:
        """Восстанавливает заряды баллонов, повторно появившихся из старого каталога."""
        repaired = 0
        async with self.connect() as db:
            done = await db.execute_fetchall(
                "SELECT value FROM app_meta WHERE key='repair_flame_ammo_catalog_regression_v1'"
            )
            if done:
                return 0
            rows = await db.execute_fetchall(
                """SELECT inventory.id,inventory.ammo,item_catalog.ammo_max
                   FROM inventory JOIN item_catalog ON item_catalog.id=inventory.item_id
                   WHERE item_catalog.source_number=520
                     AND (inventory.ammo IS NULL OR inventory.ammo<=0)"""
            )
            for row in rows:
                # The removed legacy balloon contained three charges. Preserve that
                # amount when converting it into the six-charge medium package.
                restored = min(3, max(1, int(row["ammo_max"] or 6)))
                await db.execute("UPDATE inventory SET ammo=? WHERE id=?", (restored, row["id"]))
                repaired += 1
            await db.execute(
                "INSERT OR REPLACE INTO app_meta(key,value) VALUES('repair_flame_ammo_catalog_regression_v1','done')"
            )
            await db.commit()
        return repaired

    async def repair_ammo_package_contents(self) -> int:
        """Одноразово наполняет упаковки, созданные из старых комплектов."""
        repaired = 0
        async with self.connect() as db:
            done = await db.execute_fetchall(
                "SELECT value FROM app_meta WHERE key='ammo_packages_v3_contents'"
            )
            if done:
                return 0
            rows = await db.execute_fetchall(
                """SELECT inventory.id,inventory.character_id,inventory.item_id,
                          inventory.quantity,inventory.ammo,item_catalog.ammo_max
                   FROM inventory JOIN item_catalog ON item_catalog.id=inventory.item_id
                   WHERE lower(item_catalog.properties) LIKE '%упаковка боеприпасов%'
                     AND item_catalog.ammo_max IS NOT NULL"""
            )
            for row in rows:
                quantity = max(1, int(row["quantity"] or 1))
                maximum = max(1, int(row["ammo_max"] or 1))
                current = int(row["ammo"] or 0)
                if quantity > 1:
                    await db.execute(
                        "UPDATE inventory SET quantity=1,ammo=? WHERE id=?",
                        (maximum if current <= 0 else min(maximum, current), row["id"]),
                    )
                    for _ in range(quantity - 1):
                        await db.execute(
                            "INSERT INTO inventory(character_id,item_id,quantity,durability,ammo,equipped) VALUES(?,?,1,1,?,0)",
                            (row["character_id"], row["item_id"], maximum),
                        )
                    repaired += quantity
                elif current <= 0:
                    await db.execute("UPDATE inventory SET ammo=? WHERE id=?", (maximum, row["id"]))
                    repaired += 1
            await db.execute(
                "INSERT OR REPLACE INTO app_meta(key,value) VALUES('ammo_packages_v3_contents','done')"
            )
            await db.commit()
        return repaired

    async def migrate_ammo_packages(self) -> int:
        """Переносит старые комплекты/единицы в отдельные средние упаковки."""
        migrated = 0
        async with self.connect() as db:
            done = await db.execute_fetchall(
                "SELECT value FROM app_meta WHERE key='ammo_packages_v2'"
            )
            if done:
                return 0
            units_marker = await db.execute_fetchall(
                "SELECT value FROM app_meta WHERE key='ammo_units_v1'"
            )
            medium_numbers = tuple(new for new, _ in AMMO_PACKAGE_MIGRATIONS.values())
            placeholders = ",".join("?" for _ in medium_numbers)
            rows = await db.execute_fetchall(
                f"""SELECT inventory.*,item_catalog.ammo_max
                    FROM inventory JOIN item_catalog ON item_catalog.id=inventory.item_id
                    WHERE item_catalog.source_number IN ({placeholders})""",
                medium_numbers,
            )
            for row in rows:
                bullets = int(row["quantity"]) if units_marker else int(row["quantity"]) * 3
                capacity = int(row["ammo_max"] or 6)
                first = min(capacity, bullets)
                await db.execute(
                    "UPDATE inventory SET quantity=1,ammo=? WHERE id=?",
                    (first, row["id"]),
                )
                bullets -= first
                while bullets > 0:
                    stored = min(capacity, bullets)
                    await db.execute(
                        """INSERT INTO inventory(
                               character_id,item_id,durability,ammo,quantity,equipped,notes
                           ) VALUES(?,?,?,?,1,0,?)""",
                        (row["character_id"], row["item_id"], row["durability"], stored, row["notes"]),
                    )
                    bullets -= stored
                migrated += 1
            await db.execute(
                "INSERT INTO app_meta(key,value) VALUES('ammo_packages_v2','done')"
            )
            await db.commit()
        return migrated

    async def split_nonstackable_inventory(self) -> int:
        """Разделяет старые стопки постоянных предметов на отдельные экземпляры."""
        created = 0
        async with self.connect() as db:
            rows = await db.execute_fetchall(
                """SELECT inventory.*,item_catalog.properties
                   FROM inventory
                   JOIN item_catalog ON item_catalog.id=inventory.item_id
                   WHERE inventory.quantity>1
                   AND lower(item_catalog.properties) NOT LIKE '%расходник%'"""
            )
            for row in rows:
                quantity = int(row["quantity"])
                await db.execute("UPDATE inventory SET quantity=1 WHERE id=?", (row["id"],))
                for _ in range(quantity - 1):
                    await db.execute(
                        """INSERT INTO inventory(
                               character_id,item_id,durability,ammo,quantity,equipped,notes
                           ) VALUES(?,?,?,?,1,0,?)""",
                        (
                            row["character_id"],
                            row["item_id"],
                            row["durability"],
                            row["ammo"],
                            row["notes"],
                        ),
                    )
                    created += 1
            await db.commit()
        return created

    async def merge_stackable_inventory(self) -> int:
        merged = 0
        async with self.connect() as db:
            groups = await db.execute_fetchall(
                """SELECT inventory.character_id,inventory.item_id,inventory.durability,inventory.ammo,
                          MIN(inventory.id) keep_id,SUM(inventory.quantity) total,COUNT(*) rows_count
                   FROM inventory
                   JOIN item_catalog ON item_catalog.id=inventory.item_id
                   WHERE lower(item_catalog.properties) LIKE '%расходник%'
                   GROUP BY inventory.character_id,inventory.item_id,inventory.durability,inventory.ammo
                   HAVING COUNT(*)>1"""
            )
            for group in groups:
                await db.execute(
                    "UPDATE inventory SET quantity=? WHERE id=?",
                    (group["total"], group["keep_id"]),
                )
                cursor = await db.execute(
                    """DELETE FROM inventory WHERE character_id=? AND item_id=?
                       AND durability=? AND ammo IS ? AND id<>?""",
                    (group["character_id"], group["item_id"], group["durability"], group["ammo"], group["keep_id"]),
                )
                merged += cursor.rowcount
            await db.commit()
        return merged

    async def reload_base_catalog(self) -> int:
        source = Path(__file__).resolve().parent / "ITEM_CATALOG_SOURCE.md"
        if not source.exists():
            source = Path(__file__).resolve().parent.parent / "Каталог снабжения — оружие и снаряжение.md"
        items = load_catalog(source)
        async with self.connect() as db:
            active_numbers = [int(item["source_number"]) for item in items]
            placeholders = ",".join("?" for _ in active_numbers)
            # Never cascade-delete issued or warehoused items during catalog sync.
            # Missing source entries remain usable by their owners and can be restored to the catalog later.
            await db.execute(
                f"""DELETE FROM item_catalog
                    WHERE guild_id=0 AND source_number IS NOT NULL
                    AND source_number NOT IN ({placeholders})
                    AND NOT EXISTS (SELECT 1 FROM inventory i WHERE i.item_id=item_catalog.id)
                    AND NOT EXISTS (SELECT 1 FROM supply_warehouse sw WHERE sw.item_id=item_catalog.id)
                    AND NOT EXISTS (SELECT 1 FROM motor_pool mp WHERE mp.item_id=item_catalog.id)""",
                active_numbers,
            )
            for item in items:
                rows = await db.execute_fetchall(
                    "SELECT id,name FROM item_catalog WHERE guild_id=0 AND source_number=?",
                    (item["source_number"],),
                )
                if rows and rows[0]["name"] != item["name"]:
                    await db.execute(
                        "UPDATE item_catalog SET name=? WHERE id=?",
                        (f'__rr_catalog_sync_{item["source_number"]}_{rows[0]["id"]}', rows[0]["id"]),
                    )
            for item in items:
                numbered_rows = await db.execute_fetchall(
                    "SELECT id,name FROM item_catalog WHERE guild_id=0 AND source_number=?",
                    (item["source_number"],),
                )
                if numbered_rows and numbered_rows[0]["name"] != item["name"]:
                    await db.execute(
                        "UPDATE item_catalog SET name=? WHERE id=?",
                        (item["name"], numbered_rows[0]["id"]),
                    )
                previous_rows = await db.execute_fetchall(
                    "SELECT id,max_durability FROM item_catalog WHERE guild_id=0 AND name=?",
                    (item["name"],),
                )
                previous_max = int(previous_rows[0]["max_durability"]) if previous_rows else None
                previous_id = int(previous_rows[0]["id"]) if previous_rows else None
                await db.execute(
                    """INSERT INTO item_catalog(
                           guild_id,name,size,category,max_durability,description,use_range,ammo_max,fire_rate,
                           attribute_modifiers,skill_modifiers,source_number,hands,gear,damage,damage_type,
                           defense,price,access,properties,conditions,armor_slot,
                           prosthetic_slot,will_cost,icon_file,created_by
                       ) VALUES(0,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
                       ON CONFLICT(guild_id,name) DO UPDATE SET
                           size=excluded.size,category=excluded.category,max_durability=excluded.max_durability,
                           description=excluded.description,use_range=excluded.use_range,ammo_max=excluded.ammo_max,
                           fire_rate=excluded.fire_rate,source_number=excluded.source_number,hands=excluded.hands,
                           gear=excluded.gear,damage=excluded.damage,damage_type=excluded.damage_type,
                           defense=excluded.defense,price=excluded.price,access=excluded.access,
                           properties=excluded.properties,conditions=excluded.conditions,armor_slot=excluded.armor_slot,
                           prosthetic_slot=excluded.prosthetic_slot,will_cost=excluded.will_cost,
                           icon_file=excluded.icon_file""",
                    (
                        item["name"], item["size"], item["category"], item["max_durability"], item["description"],
                        item["use_range"], item["ammo_max"], item["fire_rate"], "{}", "{}",
                        item["source_number"], item["hands"], item["gear"], item["damage"], item["damage_type"],
                        item["defense"], item["price"], item["access"], item["properties"], item["conditions"],
                        item["armor_slot"], item.get("prosthetic_slot") or item.get("slot"), int(item.get("will_cost") or 0),
                        item.get("icon_file"),
                    ),
                )
                if item["category"] in {"Броня", "Щит"} and previous_id is not None and previous_max != item["max_durability"]:
                    issued_rows = await db.execute_fetchall(
                        "SELECT id,durability FROM inventory WHERE item_id=?",
                        (previous_id,),
                    )
                    for issued in issued_rows:
                        damage_taken = max(0, previous_max - int(issued["durability"]))
                        migrated = max(0, int(item["max_durability"]) - damage_taken)
                        await db.execute(
                            "UPDATE inventory SET durability=? WHERE id=?",
                            (migrated, issued["id"]),
                        )
            overrides = await db.execute_fetchall("SELECT name,price FROM item_price_overrides")
            for override in overrides:
                await db.execute(
                    "UPDATE item_catalog SET price=? WHERE lower(name)=lower(?)",
                    (override["price"], override["name"]),
                )

            # Гарнитура №334 was temporarily omitted from the source catalog. The old
            # hard-delete removed its inventory rows through ON DELETE CASCADE. Relink
            # old orders and restore purchased copies once, without duplicating items.
            headset_rows = await db.execute_fetchall(
                "SELECT id,max_durability FROM item_catalog WHERE guild_id=0 AND source_number=334"
            )
            if headset_rows:
                headset_id = int(headset_rows[0]["id"])
                headset_durability = int(headset_rows[0]["max_durability"])
                await db.execute(
                    "UPDATE purchase_orders SET item_id=? WHERE lower(item_name)=lower('Гарнитура')",
                    (headset_id,),
                )
                recovery_done = await db.execute_fetchall(
                    "SELECT value FROM app_meta WHERE key='restore_headset_334_v1'"
                )
                if not recovery_done:
                    purchased = await db.execute_fetchall(
                        """SELECT character_id,COUNT(*) AS quantity
                           FROM purchase_orders
                           WHERE lower(item_name)=lower('Гарнитура') AND status='approved'
                           GROUP BY character_id"""
                    )
                    for purchase in purchased:
                        owned = await db.execute_fetchall(
                            "SELECT COALESCE(SUM(quantity),0) AS quantity FROM inventory WHERE character_id=? AND item_id=?",
                            (purchase["character_id"], headset_id),
                        )
                        missing = max(0, int(purchase["quantity"]) - int(owned[0]["quantity"]))
                        if missing:
                            await db.execute(
                                """INSERT INTO inventory(character_id,item_id,durability,ammo,quantity,equipped)
                                   VALUES(?,?,?,NULL,?,0)""",
                                (purchase["character_id"], headset_id, headset_durability, missing),
                            )
                    await db.execute(
                        "INSERT OR REPLACE INTO app_meta(key,value) VALUES('restore_headset_334_v1','done')"
                    )
            await db.commit()
        return len(items)

    async def create_character(self, guild_id: int, user_id: int, surname: str, name: str, class_name: str, race: str) -> int:
        async with self.connect() as db:
            cursor = await db.execute(
                "INSERT INTO characters(guild_id,user_id,surname,name,class_name,race,hands,skills_initialized) VALUES(?,?,?,?,?,?,?,0) "
                "ON CONFLICT(guild_id,user_id) DO UPDATE SET surname=excluded.surname,name=excluded.name,class_name=excluded.class_name,race=excluded.race,hands=excluded.hands,skills_initialized=0",
                (guild_id, user_id, surname, name, class_name, race, 4 if race == "Тараканы" else 2),
            )
            await db.commit()
            row = await db.execute_fetchall("SELECT id FROM characters WHERE guild_id=? AND user_id=?", (guild_id, user_id))
            character_id = int(row[0]["id"])
            await db.execute("DELETE FROM skills WHERE character_id=?", (character_id,))
            class_skills = tuple(CLASSES.values())
            placeholders = ",".join("?" for _ in class_skills)
            await db.execute(
                f"DELETE FROM skills WHERE character_id=? AND name IN ({placeholders}) AND name<>?",
                (character_id, *class_skills, CLASSES[class_name]),
            )
            for attribute in ATTRIBUTES:
                await db.execute("INSERT OR IGNORE INTO attributes VALUES(?,?,1,1)", (character_id, attribute))
            for skill in (*SKILLS, CLASSES[class_name]):
                await db.execute("INSERT OR IGNORE INTO skills VALUES(?,?,-3)", (character_id, skill))
            await db.commit()
            return character_id

    async def character(self, guild_id: int, user_id: int) -> dict[str, Any] | None:
        async with self.connect() as db:
            rows = await db.execute_fetchall("SELECT * FROM characters WHERE guild_id=? AND user_id=?", (guild_id, user_id))
            if not rows:
                return None
            result = dict(rows[0])
            attrs = await db.execute_fetchall("SELECT * FROM attributes WHERE character_id=?", (result["id"],))
            skills = await db.execute_fetchall("SELECT * FROM skills WHERE character_id=? ORDER BY name", (result["id"],))
            talents = await db.execute_fetchall("SELECT name,description FROM talents WHERE character_id=? ORDER BY name", (result["id"],))
            medal_rows = await db.execute_fetchall(
                "SELECT medal_code FROM character_medals WHERE character_id=? ORDER BY awarded_at,id",
                (result["id"],),
            )
            injuries = await db.execute_fetchall(
                "SELECT * FROM injuries WHERE character_id=? AND active=1 AND (expires_at IS NULL OR expires_at>CURRENT_TIMESTAMP) ORDER BY id",
                (result["id"],),
            )
            prosthetics = await db.execute_fetchall(
                """SELECT item_catalog.prosthetic_slot,inventory.equipped_position,item_catalog.name
                   FROM inventory JOIN item_catalog ON item_catalog.id=inventory.item_id
                   WHERE inventory.character_id=? AND inventory.equipped=1 AND item_catalog.category='Протезы'""",
                (result["id"],),
            )
            result["attributes"] = {r["name"]: {"current": r["current_value"], "max": r["max_value"]} for r in attrs}
            result["skills"] = {r["name"]: r["value"] for r in skills}
            result["talents"] = {r["name"]: r["description"] for r in talents}
            result["injuries"] = [dict(r) for r in injuries]
            result["equipped_prosthetics"] = [dict(r) for r in prosthetics]
            costs = await db.execute_fetchall(
                """SELECT COALESCE(SUM(item_catalog.will_cost),0) cost FROM inventory
                   JOIN item_catalog ON item_catalog.id=inventory.item_id
                   WHERE inventory.character_id=? AND inventory.equipped=1 AND item_catalog.category='Протезы'""", (result["id"],)
            )
            result["medals"] = [str(row["medal_code"]) for row in medal_rows]
            medal_bonuses = medal_bonus_summary(result["medals"])
            result["medal_bonuses"] = medal_bonuses
            result["will_max_base"] = int(result["will_max"]) + int(medal_bonuses["will_max_bonus"])
            result["will_max"] = max(0, result["will_max_base"] - int(costs[0]["cost"]))
            result["will_current"] = min(int(result["will_current"]), result["will_max"])
            result["infection_max"] = 5 + int(medal_bonuses["infection_max_bonus"])
            luck = await db.execute_fetchall("SELECT percent FROM luck_modifiers WHERE user_id=?", (user_id,))
            result["luck_percent"] = int(luck[0]["percent"]) if luck else 0
            fleet = await db.execute_fetchall(
                """SELECT ic.name FROM motor_pool mp
                   JOIN item_catalog ic ON ic.id=mp.item_id
                   JOIN motor_pool_funds mf ON mf.guild_id=mp.guild_id
                   WHERE mp.guild_id=? AND mf.maintenance_active=1""",
                (guild_id,),
            )
            result["active_vehicles"] = [row["name"] for row in fleet]
            capacity_rows = await db.execute_fetchall(
                """SELECT ic.name,COALESCE(SUM(i.quantity),0) AS quantity
                   FROM inventory i JOIN item_catalog ic ON ic.id=i.item_id
                   WHERE i.character_id=? GROUP BY ic.name""",
                (result["id"],),
            )
            small_slots = 0
            large_slots = 0
            for row in capacity_rows:
                bonuses = INVENTORY_CAPACITY_ITEMS.get(str(row["name"]))
                if not bonuses:
                    continue
                quantity = int(row["quantity"])
                small_slots += bonuses[0] * quantity
                large_slots += bonuses[1] * quantity
            result["inventory_capacity_bonus"] = {
                "small": small_slots,
                "large": large_slots,
            }
            return result

    async def set_luck_modifier(self, user_id: int, percent: int) -> None:
        percent = max(-100, min(100, int(percent)))
        async with self.connect() as db:
            if percent:
                await db.execute(
                    """INSERT INTO luck_modifiers(user_id,percent,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)
                       ON CONFLICT(user_id) DO UPDATE SET percent=excluded.percent,updated_at=CURRENT_TIMESTAMP""",
                    (user_id, percent),
                )
            else:
                await db.execute("DELETE FROM luck_modifiers WHERE user_id=?", (user_id,))
            await db.commit()

    async def get_luck_modifier(self, user_id: int) -> int:
        async with self.connect() as db:
            rows = await db.execute_fetchall("SELECT percent FROM luck_modifiers WHERE user_id=?", (user_id,))
            return int(rows[0]["percent"]) if rows else 0

    async def delete_character(self, guild_id: int, user_id: int) -> str | None:
        async with self.connect() as db:
            rows = await db.execute_fetchall(
                "SELECT photo_path FROM characters WHERE guild_id=? AND user_id=?", (guild_id, user_id)
            )
            if not rows:
                return None
            photo_path = rows[0]["photo_path"]
            await db.execute("DELETE FROM characters WHERE guild_id=? AND user_id=?", (guild_id, user_id))
            await db.commit()
            return str(photo_path) if photo_path else ""

    async def set_attributes(self, character_id: int, values: dict[str, int]) -> None:
        async with self.connect() as db:
            for name, value in values.items():
                await db.execute("UPDATE attributes SET current_value=?,max_value=? WHERE character_id=? AND name=?", (value, value, character_id, name))
            await db.commit()

    async def set_skill(self, character_id: int, name: str, value: int) -> None:
        async with self.connect() as db:
            await db.execute("INSERT INTO skills VALUES(?,?,?) ON CONFLICT(character_id,name) DO UPDATE SET value=excluded.value", (character_id, name, value))
            await db.commit()

    async def finalize_starting_skills(self, character_id: int, budget: int) -> tuple[bool, str]:
        async with self.connect() as db:
            character = await db.execute_fetchall("SELECT skills_initialized FROM characters WHERE id=?", (character_id,))
            if not character:
                return False, "Персонаж не найден."
            if int(character[0]["skills_initialized"]):
                return False, "Стартовые навыки уже зафиксированы."
            rows = await db.execute_fetchall("SELECT name,value FROM skills WHERE character_id=?", (character_id,))
            values = [int(row["value"]) for row in rows]
            if any(value < -5 or value > 5 for value in values):
                return False, "Стартовые навыки должны быть от −5 до +5."
            used = sum(value + 3 for value in values)
            if used != budget:
                return False, f"Нужно распределить ровно {budget} очков; сейчас распределено {used}."
            await db.execute("UPDATE characters SET skills_initialized=1 WHERE id=?", (character_id,))
            await db.commit()
            return True, f"Распределение зафиксировано: {used}/{budget}."

    async def purchase_skill(self, character_id: int, name: str, cap: int, price: int = 8) -> tuple[bool, str, int | None]:
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            chars = await db.execute_fetchall("SELECT supply_forms,skills_initialized,race FROM characters WHERE id=?", (character_id,))
            rows = await db.execute_fetchall("SELECT value FROM skills WHERE character_id=? AND name=?", (character_id, name))
            if not chars or not rows:
                await db.rollback(); return False, "Персонаж или навык не найден.", None
            if not int(chars[0]["skills_initialized"]):
                all_skills = await db.execute_fetchall(
                    "SELECT value FROM skills WHERE character_id=?", (character_id,)
                )
                values = [int(row["value"]) for row in all_skills]
                budget = 12 if chars[0]["race"] == "Мыши" else 8 if chars[0]["race"] == "Тараканы" else 10
                used = sum(value + 3 for value in values)
                if any(value < -5 or value > 5 for value in values) or used != budget:
                    await db.rollback()
                    return False, f"Сначала завершите стартовое распределение: нужно {budget} очков, распределено {used}.", None
                await db.execute("UPDATE characters SET skills_initialized=1 WHERE id=?", (character_id,))
            value = int(rows[0]["value"])
            if value >= cap:
                await db.rollback(); return False, f"Предел навыка — {cap}.", int(chars[0]["supply_forms"])
            balance = int(chars[0]["supply_forms"])
            if balance < price:
                await db.rollback(); return False, f"Недостаточно БС: требуется {price}, доступно {balance}.", balance
            await db.execute("UPDATE skills SET value=value+1 WHERE character_id=? AND name=?", (character_id, name))
            await db.execute("UPDATE characters SET supply_forms=supply_forms-? WHERE id=?", (price, character_id))
            await db.commit()
            return True, f"Навык «{name}» повышен: {value} → {value+1} за {price} БС.", balance-price

    async def adjust_skill(self, character_id: int, name: str, delta: int) -> tuple[int, int]:
        async with self.connect() as db:
            rows = await db.execute_fetchall("SELECT value FROM skills WHERE character_id=? AND name=?", (character_id, name))
            if not rows: raise ValueError("Навык не найден")
            before = int(rows[0]["value"]); after = max(-5, min(7, before + delta))
            await db.execute("UPDATE skills SET value=? WHERE character_id=? AND name=?", (after, character_id, name))
            await db.commit(); return before, after

    async def update_catalog_price(self, name: str, price: int) -> None:
        if price < 0: raise ValueError("Цена не может быть отрицательной")
        async with self.connect() as db:
            await db.execute("INSERT INTO item_price_overrides(name,price) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET price=excluded.price", (name, price))
            await db.execute("UPDATE item_catalog SET price=? WHERE lower(name)=lower(?)", (price, name))
            await db.commit()

    async def update_identity(self, character_id: int, surname: str, name: str) -> None:
        async with self.connect() as db:
            await db.execute("UPDATE characters SET surname=?,name=? WHERE id=?", (surname, name, character_id))
            await db.commit()

    async def damage(self, character_id: int, attribute: str, amount: int) -> tuple[int, int]:
        async with self.connect() as db:
            rows = await db.execute_fetchall("SELECT current_value FROM attributes WHERE character_id=? AND name=?", (character_id, attribute))
            if not rows:
                raise ValueError("Неизвестная характеристика")
            before = int(rows[0]["current_value"])
            after = max(0, before - amount)
            await db.execute("UPDATE attributes SET current_value=? WHERE character_id=? AND name=?", (after, character_id, attribute))
            await db.commit()
            return before, after

    async def heal(self, character_id: int, attribute: str, amount: int) -> int:
        async with self.connect() as db:
            rows = await db.execute_fetchall("SELECT current_value,max_value FROM attributes WHERE character_id=? AND name=?", (character_id, attribute))
            current, maximum = int(rows[0][0]), int(rows[0][1])
            value = min(maximum, current + amount)
            await db.execute("UPDATE attributes SET current_value=? WHERE character_id=? AND name=?", (value, character_id, attribute))
            await db.commit()
            return value

    async def rat_recover(self, character_id: int, attribute: str) -> tuple[str, Any]:
        now = datetime.now(UTC)
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            character_rows = await db.execute_fetchall(
                "SELECT race,rat_recovery_at FROM characters WHERE id=?",
                (character_id,),
            )
            if not character_rows or character_rows[0]["race"] != "Крысы":
                await db.rollback()
                return "wrong_race", None
            cooldown = character_rows[0]["rat_recovery_at"]
            if cooldown:
                ready_at = datetime.fromisoformat(str(cooldown))
                if ready_at > now:
                    await db.rollback()
                    return "cooldown", ready_at
            attribute_rows = await db.execute_fetchall(
                """SELECT current_value,max_value FROM attributes
                   WHERE character_id=? AND name=?""",
                (character_id, attribute),
            )
            if not attribute_rows:
                await db.rollback()
                return "unknown_attribute", None
            current = int(attribute_rows[0]["current_value"])
            maximum = int(attribute_rows[0]["max_value"])
            if current >= maximum:
                await db.rollback()
                return "full", (current, maximum)
            restored = current + 1
            ready_at = now + timedelta(hours=24)
            await db.execute(
                "UPDATE attributes SET current_value=? WHERE character_id=? AND name=?",
                (restored, character_id, attribute),
            )
            await db.execute(
                "UPDATE characters SET rat_recovery_at=? WHERE id=?",
                (ready_at.isoformat(), character_id),
            )
            await db.commit()
            return "ok", (current, restored, ready_at)

    async def adjust_infection(self, character_id: int, delta: int) -> tuple[int, int] | None:
        async with self.connect() as db:
            rows = await db.execute_fetchall(
                "SELECT infection FROM characters WHERE id=?",
                (character_id,),
            )
            if not rows:
                return None
            before = int(rows[0]["infection"])
            medal_rows = await db.execute_fetchall(
                "SELECT medal_code FROM character_medals WHERE character_id=?",
                (character_id,),
            )
            infection_max = 5 + int(
                medal_bonus_summary(str(row["medal_code"]) for row in medal_rows)["infection_max_bonus"]
            )
            after = max(0, min(infection_max, before + delta))
            await db.execute(
                "UPDATE characters SET infection=? WHERE id=?",
                (after, character_id),
            )
            await db.commit()
            return before, after

    async def create_npc(
        self,
        guild_id: int,
        name: str,
        physique: int,
        agility: int,
        defense: int,
        fight_skill: int,
        shooting_skill: int,
        melee_damage: int,
        ranged_damage: int,
        melee_damage_type: str,
        ranged_damage_type: str,
        description: str,
        shield: int = 0,
        indestructible_defense: int = 0,
        damage_reductions: str = '{}',
    ) -> int:
        async with self.connect() as db:
            await db.execute(
                """INSERT INTO npcs(
                     guild_id,name,health,max_health,defense,attack_dice,damage,description,
                     physique,physique_max,agility,agility_max,defense_max,
                     shield,shield_max,indestructible_defense,indestructible_defense_max,damage_reductions,
                     fight_skill,shooting_skill,melee_damage,ranged_damage,
                     melee_damage_type,ranged_damage_type
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(guild_id,name) DO UPDATE SET
                     health=excluded.health,max_health=excluded.max_health,
                     defense=excluded.defense,description=excluded.description,
                     physique=excluded.physique,physique_max=excluded.physique_max,
                     agility=excluded.agility,agility_max=excluded.agility_max,
                     defense_max=excluded.defense_max,shield=excluded.shield,shield_max=excluded.shield_max,
                     indestructible_defense=excluded.indestructible_defense,
                     indestructible_defense_max=excluded.indestructible_defense_max,
                     damage_reductions=excluded.damage_reductions,fight_skill=excluded.fight_skill,
                     shooting_skill=excluded.shooting_skill,melee_damage=excluded.melee_damage,
                     ranged_damage=excluded.ranged_damage,
                     melee_damage_type=excluded.melee_damage_type,
                     ranged_damage_type=excluded.ranged_damage_type""",
                (
                    guild_id, name, physique, physique, defense, fight_skill, melee_damage, description,
                    physique, physique, agility, agility, defense,
                    shield, shield, indestructible_defense, indestructible_defense, damage_reductions,
                    fight_skill, shooting_skill, melee_damage, ranged_damage,
                    melee_damage_type, ranged_damage_type,
                    ),
                )
            await db.commit()
            rows = await db.execute_fetchall(
                "SELECT id FROM npcs WHERE guild_id=? AND lower(name)=lower(?)",
                (guild_id, name),
            )
            return int(rows[0]["id"])

    async def npc(self, guild_id: int, name: str) -> dict[str, Any] | None:
        async with self.connect() as db:
            rows = await db.execute_fetchall(
                "SELECT * FROM npcs WHERE guild_id=? ORDER BY name",
                (guild_id,),
            )
            return next(
                (dict(row) for row in rows if str(row["name"]).casefold() == name.casefold()),
                None,
            )

    async def npcs(self, guild_id: int, query: str = "") -> list[dict[str, Any]]:
        async with self.connect() as db:
            rows = await db.execute_fetchall(
                "SELECT * FROM npcs WHERE guild_id=? ORDER BY name",
                (guild_id,),
            )
            return [
                dict(row) for row in rows
                if not query or query.casefold() in str(row["name"]).casefold()
            ]

    async def damage_npc(self, npc_id: int, amount: int) -> tuple[int, int] | None:
        return await self.damage_npc_attribute(npc_id, "Телосложение", amount)

    async def damage_npc_attribute(
        self,
        npc_id: int,
        attribute: str,
        amount: int,
    ) -> tuple[int, int] | None:
        column = {"Телосложение": "physique", "Ловкость": "agility"}.get(attribute)
        if not column:
            return None
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            rows = await db.execute_fetchall(
                f"SELECT {column} value FROM npcs WHERE id=?",
                (npc_id,),
            )
            if not rows:
                await db.rollback()
                return None
            before = int(rows[0]["value"])
            after = max(0, before - max(0, amount))
            await db.execute(f"UPDATE npcs SET {column}=? WHERE id=?", (after, npc_id))
            if column == "physique":
                await db.execute("UPDATE npcs SET health=? WHERE id=?", (after, npc_id))
            await db.commit()
            return before, after

    async def heal_npc_attribute(
        self,
        npc_id: int,
        attribute: str,
        amount: int,
    ) -> tuple[int, int] | None:
        columns = {
            "Телосложение": ("physique", "physique_max"),
            "Ловкость": ("agility", "agility_max"),
        }
        selected = columns.get(attribute)
        if not selected:
            return None
        current_column, maximum_column = selected
        async with self.connect() as db:
            rows = await db.execute_fetchall(
                f"SELECT {current_column} current_value,{maximum_column} max_value FROM npcs WHERE id=?",
                (npc_id,),
            )
            if not rows:
                return None
            before = int(rows[0]["current_value"])
            after = min(int(rows[0]["max_value"]), before + max(0, amount))
            await db.execute(f"UPDATE npcs SET {current_column}=? WHERE id=?", (after, npc_id))
            if current_column == "physique":
                await db.execute("UPDATE npcs SET health=? WHERE id=?", (after, npc_id))
            await db.commit()
            return before, after

    async def adjust_npc_protection(
        self, npc_id: int, protection: str, delta: int
    ) -> tuple[int, int] | None:
        columns = {
            "Броня": ("defense", "defense_max"),
            "Щит": ("shield", "shield_max"),
            "Неразрушимая защита": ("indestructible_defense", "indestructible_defense_max"),
        }
        selected = columns.get(protection)
        if not selected:
            return None
        current_column, maximum_column = selected
        async with self.connect() as db:
            rows = await db.execute_fetchall(
                f"SELECT {current_column} current_value,{maximum_column} max_value FROM npcs WHERE id=?",
                (npc_id,),
            )
            if not rows:
                return None
            before = int(rows[0]["current_value"])
            after = max(0, min(int(rows[0]["max_value"]), before + delta))
            await db.execute(f"UPDATE npcs SET {current_column}=? WHERE id=?", (after, npc_id))
            await db.commit()
            return before, after

    async def adjust_npc_defense(self, npc_id: int, delta: int) -> tuple[int, int] | None:
        return await self.adjust_npc_protection(npc_id, "Броня", delta)

    async def set_npc_damage_reduction(
        self, npc_id: int, damage_type: str, amount: int
    ) -> bool:
        import json
        async with self.connect() as db:
            rows = await db.execute_fetchall("SELECT damage_reductions FROM npcs WHERE id=?", (npc_id,))
            if not rows:
                return False
            reductions = json.loads(rows[0]["damage_reductions"] or "{}")
            if amount > 0:
                reductions[damage_type] = amount
            else:
                reductions.pop(damage_type, None)
            await db.execute(
                "UPDATE npcs SET damage_reductions=? WHERE id=?",
                (json.dumps(reductions, ensure_ascii=False), npc_id),
            )
            await db.commit()
            return True

    async def delete_npc(self, guild_id: int, name: str) -> bool:
        async with self.connect() as db:
            rows = await db.execute_fetchall(
                "SELECT id,name FROM npcs WHERE guild_id=?",
                (guild_id,),
            )
            row = next(
                (candidate for candidate in rows if str(candidate["name"]).casefold() == name.casefold()),
                None,
            )
            if not row:
                return False
            cursor = await db.execute(
                "DELETE FROM npcs WHERE id=?",
                (row["id"],),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def add_injury(self, character_id: int, attribute: str, trauma: Any) -> None:
        expires_at = None
        if trauma.duration == "Мгновенное":
            expires_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        elif trauma.duration.endswith(" ч."):
            hours = int(trauma.duration.split()[0])
            expires_at = (datetime.now(UTC) + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        async with self.connect() as db:
            await db.execute(
                """INSERT INTO injuries(
                       character_id,attribute_name,roll_code,name,description,penalties,duration,
                       impairment_key,compensation_position,expires_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    character_id, attribute, trauma.code, trauma.name, trauma.description,
                    trauma.penalties, trauma.duration, getattr(trauma, "impairment_key", ""),
                    getattr(trauma, "compensation_position", ""), expires_at,
                ),
            )
            if attribute in ("Телосложение", "Ловкость"):
                medal_rows = await db.execute_fetchall(
                    "SELECT medal_code FROM character_medals WHERE character_id=?",
                    (character_id,),
                )
                infection_max = 5 + int(
                    medal_bonus_summary(str(row["medal_code"]) for row in medal_rows)["infection_max_bonus"]
                )
                await db.execute(
                    "UPDATE characters SET infection=MIN(?,infection+1) WHERE id=?",
                    (infection_max, character_id),
                )
            await db.commit()

    async def add_pending_injury(self, character_id: int, attribute: str, roll_code: int) -> None:
        async with self.connect() as db:
            await db.execute(
                "INSERT INTO injuries(character_id,attribute_name,roll_code,name,description,penalties,duration) VALUES(?,?,?,?,?,?,?)",
                (character_id, attribute, roll_code, f"Травма {attribute} ({roll_code})", "Таблица для этой характеристики ещё не загружена.", "Определяет мастер", "Определяет мастер"),
            )
            if attribute in ("Телосложение", "Ловкость"):
                medal_rows = await db.execute_fetchall(
                    "SELECT medal_code FROM character_medals WHERE character_id=?",
                    (character_id,),
                )
                infection_max = 5 + int(
                    medal_bonus_summary(str(row["medal_code"]) for row in medal_rows)["infection_max_bonus"]
                )
                await db.execute(
                    "UPDATE characters SET infection=MIN(?,infection+1) WHERE id=?",
                    (infection_max, character_id),
                )
            await db.commit()

    async def list_rows(self, table: str, character_id: int) -> list[dict[str, Any]]:
        if table not in {"talents", "injuries", "inventory"}:
            raise ValueError("Недопустимая таблица")
        if table == "injuries":
            await self.cleanup_expired_injuries()
        async with self.connect() as db:
            rows = await db.execute_fetchall(f"SELECT * FROM {table} WHERE character_id=? ORDER BY id", (character_id,))
            return [dict(row) for row in rows]

    async def cleanup_expired_injuries(self) -> int:
        async with self.connect() as db:
            cursor = await db.execute("DELETE FROM injuries WHERE expires_at IS NOT NULL AND expires_at<=CURRENT_TIMESTAMP")
            await db.commit()
            return cursor.rowcount

    async def referenced_photo_paths(self) -> set[str]:
        async with self.connect() as db:
            rows = await db.execute_fetchall("SELECT photo_path FROM characters WHERE photo_path IS NOT NULL AND photo_path<>''")
            return {str(row["photo_path"]) for row in rows}

    async def vacuum(self) -> None:
        async with self.connect() as db:
            await db.execute("VACUUM")

    async def add_talent(self, character_id: int, name: str, description: str) -> None:
        async with self.connect() as db:
            await db.execute("INSERT INTO talents(character_id,name,description) VALUES(?,?,?)", (character_id, name, description))
            await db.commit()

    async def delete_owned_row(self, table: str, row_id: int, character_id: int) -> bool:
        if table not in {"talents", "injuries", "inventory"}:
            raise ValueError("Недопустимая таблица")
        async with self.connect() as db:
            cursor = await db.execute(f"DELETE FROM {table} WHERE id=? AND character_id=?", (row_id, character_id))
            await db.commit()
            return cursor.rowcount > 0

    async def delete_injury_by_code(self, character_id: int, roll_code: int, category: str) -> bool:
        attributes = ("Телосложение", "Ловкость") if category == "physical" else ("Смекалка", "Эмпатия")
        async with self.connect() as db:
            cursor = await db.execute(
                """DELETE FROM injuries
                   WHERE id=(
                       SELECT id FROM injuries
                       WHERE character_id=? AND roll_code=? AND attribute_name IN (?,?)
                       ORDER BY id LIMIT 1
                   )""",
                (character_id, roll_code, *attributes),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def medals(self, character_id: int) -> list[dict[str, Any]]:
        async with self.connect() as db:
            rows = await db.execute_fetchall(
                """SELECT character_medals.id,character_medals.reason,character_medals.awarded_by,
                          character_medals.awarded_at,medal_catalog.*
                   FROM character_medals JOIN medal_catalog ON medal_catalog.code=character_medals.medal_code
                   WHERE character_medals.character_id=?
                   ORDER BY character_medals.awarded_at,character_medals.id""",
                (character_id,),
            )
            return [dict(row) for row in rows]

    async def award_medal(self, character_id: int, medal_code: str, reason: str, awarded_by: int) -> tuple[bool, str]:
        async with self.connect() as db:
            medal = await db.execute_fetchall("SELECT name FROM medal_catalog WHERE code=?", (medal_code,))
            if not medal:
                return False, "Неизвестная медаль."
            try:
                await db.execute(
                    "INSERT INTO character_medals(character_id,medal_code,reason,awarded_by) VALUES(?,?,?,?)",
                    (character_id, medal_code, reason.strip(), awarded_by),
                )
                await db.commit()
            except aiosqlite.IntegrityError:
                return False, "У персонажа уже есть эта медаль."
            return True, str(medal[0]["name"])

    async def revoke_medal(self, character_id: int, medal_code: str) -> bool:
        async with self.connect() as db:
            cursor = await db.execute(
                "DELETE FROM character_medals WHERE character_id=? AND medal_code=?", (character_id, medal_code)
            )
            if cursor.rowcount:
                remaining = await db.execute_fetchall(
                    "SELECT medal_code FROM character_medals WHERE character_id=?",
                    (character_id,),
                )
                bonuses = medal_bonus_summary(str(row["medal_code"]) for row in remaining)
                await db.execute(
                    """UPDATE characters
                       SET will_current=MIN(will_current,will_max+?),
                           infection=MIN(infection,5+?)
                       WHERE id=?""",
                    (bonuses["will_max_bonus"], bonuses["infection_max_bonus"], character_id),
                )
            await db.commit()
            return cursor.rowcount > 0

    async def transfer_supply(self, sender_id: int, recipient_id: int, amount: int) -> tuple[int, int]:
        if amount < 1:
            raise ValueError("Количество должно быть положительным")
        if sender_id == recipient_id:
            raise ValueError("Нельзя передать бланки самому себе")
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            rows = await db.execute_fetchall(
                "SELECT id,supply_forms FROM characters WHERE id IN (?,?)", (sender_id, recipient_id)
            )
            balances = {int(row["id"]): int(row["supply_forms"]) for row in rows}
            if sender_id not in balances or recipient_id not in balances:
                raise ValueError("Один из персонажей не найден")
            if balances[sender_id] < amount:
                raise ValueError("Недостаточно бланков снабжения")
            await db.execute("UPDATE characters SET supply_forms=supply_forms-? WHERE id=?", (amount, sender_id))
            await db.execute("UPDATE characters SET supply_forms=supply_forms+? WHERE id=?", (amount, recipient_id))
            await db.commit()
            return balances[sender_id] - amount, balances[recipient_id] + amount

    async def update_character(self, character_id: int, field: str, value: Any) -> None:
        allowed = {"rank_index", "will_current", "supply_forms", "photo_path", "notes", "infection", "hands"}
        if field not in allowed:
            raise ValueError("Недопустимое поле")
        async with self.connect() as db:
            await db.execute(f"UPDATE characters SET {field}=? WHERE id=?", (value, character_id))
            await db.commit()

    async def create_catalog_item(self, guild_id: int, user_id: int, data: dict[str, Any]) -> int:
        async with self.connect() as db:
            cursor = await db.execute(
                """INSERT INTO item_catalog(
                       guild_id,name,size,category,max_durability,description,use_range,ammo_max,fire_rate,
                       attribute_modifiers,skill_modifiers,gear,conditions,created_by
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (guild_id, data["name"], data["size"], data["category"], data["durability"], data.get("description", ""),
                 data.get("range"), data.get("ammo"), data.get("fire_rate"), json.dumps(data.get("attribute_modifiers", {}), ensure_ascii=False),
                 json.dumps(data.get("skill_modifiers", {}), ensure_ascii=False), data["durability"],
                 data.get("description", "") or "Особых условий использования нет.", user_id),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def catalog_item(self, guild_id: int, name: str) -> dict[str, Any] | None:
        async with self.connect() as db:
            rows = await db.execute_fetchall(
                "SELECT * FROM item_catalog WHERE guild_id IN (0,?) ORDER BY guild_id DESC", (guild_id,)
            )
            return next((dict(row) for row in rows if str(row["name"]).casefold() == name.casefold()), None)

    async def base_catalog_item_by_number(self, source_number: int) -> dict[str, Any] | None:
        async with self.connect() as db:
            rows = await db.execute_fetchall(
                "SELECT * FROM item_catalog WHERE guild_id=0 AND source_number=? LIMIT 1",
                (source_number,),
            )
            return dict(rows[0]) if rows else None

    async def characters_by_user(self, user_id: int) -> list[dict[str, Any]]:
        async with self.connect() as db:
            rows = await db.execute_fetchall(
                """SELECT id,guild_id,user_id,surname,name,class_name,race
                   FROM characters WHERE user_id=? ORDER BY guild_id""",
                (user_id,),
            )
            return [dict(row) for row in rows]

    async def catalog_items(self, guild_id: int, query: str = "", limit: int = 25) -> list[dict[str, Any]]:
        async with self.connect() as db:
            rows = await db.execute_fetchall(
                "SELECT * FROM item_catalog WHERE guild_id IN (0,?) ORDER BY source_number IS NULL,name", (guild_id,)
            )
            seen: set[str] = set()
            result = []
            for row in rows:
                item = dict(row)
                key = str(item["name"]).casefold()
                if key in seen or (query and query.casefold() not in key):
                    continue
                seen.add(key)
                result.append(item)
                if len(result) >= limit:
                    break
            return result

    async def give_item(self, character_id: int, item: dict[str, Any], quantity: int = 1) -> int:
        async with self.connect() as db:
            if is_consumable_item(item):
                rows = await db.execute_fetchall(
                    """SELECT id FROM inventory
                       WHERE character_id=? AND item_id=? AND durability=? AND ammo IS ? AND equipped=0
                       ORDER BY id LIMIT 1""",
                    (character_id, item["id"], item["max_durability"], item["ammo_max"]),
                )
                if rows:
                    row_id = int(rows[0]["id"])
                    await db.execute(
                        "UPDATE inventory SET quantity=quantity+? WHERE id=?",
                        (quantity, row_id),
                    )
                    await db.commit()
                    return row_id
            first_row_id = 0
            for _ in range(max(1, quantity)):
                cursor = await db.execute(
                    "INSERT INTO inventory(character_id,item_id,durability,ammo,quantity) VALUES(?,?,?,?,1)",
                    (character_id, item["id"], item["max_durability"], item["ammo_max"]),
                )
                if not first_row_id:
                    first_row_id = int(cursor.lastrowid)
            await db.commit()
            return first_row_id

    async def purchase_item(
        self,
        character_id: int,
        item_id: int,
        required_supply_level: int,
    ) -> tuple[bool, str, int | None]:
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            characters = await db.execute_fetchall(
                "SELECT class_name,supply_forms FROM characters WHERE id=?",
                (character_id,),
            )
            items = await db.execute_fetchall(
                "SELECT * FROM item_catalog WHERE id=?",
                (item_id,),
            )
            if not characters or not items:
                await db.rollback()
                return False, "Персонаж или предмет не найден.", None
            character, item = characters[0], dict(items[0])
            if item["category"] == "Разное" or item["access"] == "Не продаётся":
                await db.rollback()
                return False, "Этот предмет нельзя приобрести в магазине.", None
            is_vehicle = item["category"] == "Транспорт"
            is_prosthetic = item["category"] == "Протезы"
            required_skill = "Обращение" if is_vehicle else "Защита" if is_prosthetic else "Снабжение"
            required_class = "Солдат" if is_vehicle else "Окопник" if is_prosthetic else "Снабженец"
            skills = await db.execute_fetchall(
                "SELECT value FROM skills WHERE character_id=? AND name=?",
                (character_id, required_skill),
            )
            level = int(skills[0]["value"]) if skills and character["class_name"] == required_class else -99
            if required_supply_level > 0 and level < required_supply_level:
                await db.rollback()
                return False, f"Требуется {required_skill} {required_supply_level} и класс {required_class}.", None
            price = int(item["price"] or 0)
            if price < 1:
                await db.rollback()
                return False, "У предмета не указана цена.", None
            bureaucracy = [] if is_vehicle or is_prosthetic else await db.execute_fetchall(
                "SELECT 1 FROM talents WHERE character_id=? AND lower(name)=lower('Бюрократия') LIMIT 1",
                (character_id,),
            )
            discount = (1 if bureaucracy else 0) + (1 if not is_vehicle and not is_prosthetic and level > 6 else 0)
            if item["name"] in MEDICAL_CONSUMABLES:
                ambulance = await db.execute_fetchall(
                    """SELECT 1 FROM motor_pool mp
                       JOIN item_catalog ic ON ic.id=mp.item_id
                       JOIN motor_pool_funds mf ON mf.guild_id=mp.guild_id
                       WHERE mp.guild_id=(SELECT guild_id FROM characters WHERE id=?)
                         AND mf.maintenance_active=1
                         AND ic.name='Санитарная мотокарета «Белый хвост»' LIMIT 1""",
                    (character_id,),
                )
                discount += 1 if ambulance else 0
            price = max(1, price - discount)
            balance = int(character["supply_forms"])
            if balance < price:
                await db.rollback()
                return False, f"Недостаточно БС: требуется {price}, доступно {balance}.", balance
            balance -= price
            requires_approval = "одобрение" in str(item["access"] or "").casefold()
            if is_vehicle and not requires_approval:
                guild_rows = await db.execute_fetchall(
                    "SELECT guild_id,user_id FROM characters WHERE id=?", (character_id,)
                )
                await db.execute(
                    """INSERT INTO motor_pool(guild_id,item_id,quantity,purchased_by)
                       VALUES(?,?,1,?) ON CONFLICT(guild_id,item_id)
                       DO UPDATE SET quantity=quantity+1""",
                    (guild_rows[0]["guild_id"], item_id, guild_rows[0]["user_id"]),
                )
            else:
                await db.execute(
                    """INSERT INTO purchase_orders(
                           guild_id,character_id,item_id,item_name,paid_price
                       ) VALUES((SELECT guild_id FROM characters WHERE id=?),?,?,?,?)""",
                    (character_id, character_id, item_id, item["name"], price),
                )
            await db.execute(
                "UPDATE characters SET supply_forms=? WHERE id=?",
                (balance, character_id),
            )
            await db.commit()
            if is_vehicle and not requires_approval:
                return True, f'{item["name"]} приобретён и добавлен в автопарк за {price} БС.', balance
            return True, f'Заявка на {item["name"]} создана. Зарезервировано {price} БС.', balance

    async def pending_purchase_orders(self, guild_id: int) -> list[dict[str, Any]]:
        async with self.connect() as db:
            rows = await db.execute_fetchall(
                """SELECT po.*,c.user_id,c.surname,c.name,c.supply_forms
                   FROM purchase_orders po
                   JOIN characters c ON c.id=po.character_id
                   WHERE po.guild_id=? AND po.status='pending'
                   ORDER BY po.ordered_at,po.id""",
                (guild_id,),
            )
            return [dict(row) for row in rows]

    async def resolve_purchase_order(
        self, guild_id: int, order_id: int, reviewer_id: int, approve: bool,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            rows = await db.execute_fetchall(
                """SELECT po.*,c.user_id FROM purchase_orders po
                   JOIN characters c ON c.id=po.character_id
                   WHERE po.id=? AND po.guild_id=?""",
                (order_id, guild_id),
            )
            if not rows:
                await db.rollback()
                return False, "Заявка не найдена.", None
            order = dict(rows[0])
            if order["status"] != "pending":
                await db.rollback()
                return False, "Эта заявка уже обработана.", order
            if approve:
                item_rows = await db.execute_fetchall("SELECT * FROM item_catalog WHERE id=?", (order["item_id"],))
                if not item_rows:
                    await db.rollback()
                    return False, "Предмет больше не существует в каталоге. Отклоните заявку для возврата БС.", order
                item = dict(item_rows[0])
                if item["category"] == "Транспорт":
                    await db.execute(
                        """INSERT INTO motor_pool(guild_id,item_id,quantity,purchased_by)
                           VALUES(?,?,1,?) ON CONFLICT(guild_id,item_id)
                           DO UPDATE SET quantity=quantity+1""",
                        (guild_id, order["item_id"], order["user_id"]),
                    )
                elif is_consumable_item(item):
                    stacks = await db.execute_fetchall(
                        """SELECT id FROM supply_warehouse
                           WHERE guild_id=? AND item_id=? AND durability=? AND ammo IS ?
                           ORDER BY id LIMIT 1""",
                        (guild_id, order["item_id"], item["max_durability"], item["ammo_max"]),
                    )
                    if stacks:
                        await db.execute("UPDATE supply_warehouse SET quantity=quantity+1 WHERE id=?", (stacks[0]["id"],))
                    else:
                        await db.execute(
                            "INSERT INTO supply_warehouse(guild_id,item_id,durability,ammo,deposited_by) VALUES(?,?,?,?,?)",
                            (guild_id, order["item_id"], item["max_durability"], item["ammo_max"], reviewer_id),
                        )
                else:
                    await db.execute(
                        "INSERT INTO supply_warehouse(guild_id,item_id,durability,ammo,deposited_by) VALUES(?,?,?,?,?)",
                        (guild_id, order["item_id"], item["max_durability"], item["ammo_max"], reviewer_id),
                    )
                status = "approved"
                destination = "в автопарк" if item["category"] == "Транспорт" else "на склад снабжения"
                message = f'Заявка одобрена: {order["item_name"]} добавлен {destination}.'
                order["destination"] = destination
            else:
                await db.execute(
                    "UPDATE characters SET supply_forms=supply_forms+? WHERE id=?",
                    (order["paid_price"], order["character_id"]),
                )
                status = "rejected"
                message = f'Заявка отклонена: возвращено {order["paid_price"]} БС.'
            await db.execute(
                """UPDATE purchase_orders
                   SET status=?,reviewed_at=CURRENT_TIMESTAMP,reviewed_by=?
                   WHERE id=? AND status='pending'""",
                (status, reviewer_id, order_id),
            )
            await db.commit()
            order["status"] = status
            return True, message, order

    async def supply_warehouse_items(self, guild_id: int) -> list[dict[str, Any]]:
        async with self.connect() as db:
            rows = await db.execute_fetchall(
                """SELECT sw.*,ic.name,ic.size,ic.category,ic.max_durability,ic.ammo_max,
                          ic.properties,ic.conditions
                   FROM supply_warehouse sw JOIN item_catalog ic ON ic.id=sw.item_id
                   WHERE sw.guild_id=? ORDER BY ic.category,ic.name,sw.id""",
                (guild_id,),
            )
            return [dict(row) for row in rows]

    async def motor_pool(self, guild_id: int) -> dict[str, Any]:
        async with self.connect() as db:
            rows = await db.execute_fetchall(
                """SELECT mp.*,ic.name,ic.description,ic.properties,ic.conditions,
                          ic.max_durability,ic.access
                   FROM motor_pool mp JOIN item_catalog ic ON ic.id=mp.item_id
                   WHERE mp.guild_id=? ORDER BY ic.name""",
                (guild_id,),
            )
            funds = await db.execute_fetchall(
                "SELECT balance,maintenance_active FROM motor_pool_funds WHERE guild_id=?", (guild_id,)
            )
            return {
                "items": [dict(row) for row in rows],
                "balance": int(funds[0]["balance"]) if funds else 0,
                "maintenance_active": bool(funds and funds[0]["maintenance_active"]),
            }

    async def deposit_motor_pool_funds(
        self, guild_id: int, character_id: int, amount: int,
    ) -> tuple[bool, str, int, int]:
        amount = max(1, int(amount))
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            rows = await db.execute_fetchall(
                "SELECT supply_forms FROM characters WHERE id=? AND guild_id=?",
                (character_id, guild_id),
            )
            if not rows:
                await db.rollback()
                return False, "Персонаж не найден.", 0, 0
            character_balance = int(rows[0]["supply_forms"])
            if character_balance < amount:
                await db.rollback()
                return False, f"Недостаточно БС: доступно {character_balance}.", character_balance, 0
            await db.execute(
                "UPDATE characters SET supply_forms=supply_forms-? WHERE id=?",
                (amount, character_id),
            )
            await db.execute(
                """INSERT INTO motor_pool_funds(guild_id,balance) VALUES(?,?)
                   ON CONFLICT(guild_id) DO UPDATE SET balance=balance+excluded.balance""",
                (guild_id, amount),
            )
            funds = await db.execute_fetchall(
                "SELECT balance FROM motor_pool_funds WHERE guild_id=?", (guild_id,)
            )
            await db.commit()
            return True, f"В автопарк внесено {amount} БС.", character_balance - amount, int(funds[0]["balance"])

    async def charge_motor_pool_maintenance(self, guild_id: int) -> dict[str, Any]:
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            rows = await db.execute_fetchall(
                """SELECT ic.name,mp.quantity,ic.conditions,ic.properties,ic.description
                   FROM motor_pool mp JOIN item_catalog ic ON ic.id=mp.item_id
                   WHERE mp.guild_id=? ORDER BY ic.name""",
                (guild_id,),
            )
            breakdown = []
            total = 0
            for row in rows:
                text = " ".join(str(row[key] or "") for key in ("conditions", "properties", "description"))
                match = re.search(r"Обслуживание:\s*(\d+)\s*БС", text, re.IGNORECASE)
                upkeep = int(match.group(1)) if match else 0
                subtotal = upkeep * int(row["quantity"])
                total += subtotal
                breakdown.append({"name": row["name"], "quantity": int(row["quantity"]), "upkeep": upkeep, "subtotal": subtotal})
            funds = await db.execute_fetchall(
                "SELECT balance FROM motor_pool_funds WHERE guild_id=?", (guild_id,)
            )
            balance = int(funds[0]["balance"]) if funds else 0
            paid = min(balance, total)
            remaining = balance - paid
            active = 1 if total > 0 and paid == total else 0
            await db.execute(
                """INSERT INTO motor_pool_funds(guild_id,balance,maintenance_active) VALUES(?,?,?)
                   ON CONFLICT(guild_id) DO UPDATE SET
                       balance=excluded.balance,maintenance_active=excluded.maintenance_active""",
                (guild_id, remaining, active),
            )
            await db.commit()
            return {
                "items": breakdown, "total": total, "paid": paid,
                "shortfall": total - paid, "balance": remaining,
            }

    async def take_from_supply_warehouse(
        self, guild_id: int, character_id: int, warehouse_id: int,
    ) -> tuple[bool, str]:
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            chars = await db.execute_fetchall("SELECT guild_id FROM characters WHERE id=?", (character_id,))
            rows = await db.execute_fetchall(
                """SELECT sw.*,ic.name,ic.size,ic.max_durability,ic.properties
                   FROM supply_warehouse sw JOIN item_catalog ic ON ic.id=sw.item_id
                   WHERE sw.id=? AND sw.guild_id=?""",
                (warehouse_id, guild_id),
            )
            if not chars or int(chars[0]["guild_id"]) != guild_id or not rows:
                await db.rollback()
                return False, "Предмет на складе больше не найден."
            item = dict(rows[0])
            if item["size"] in {"Малый", "Большой"}:
                attribute = "Ловкость" if item["size"] == "Малый" else "Телосложение"
                capacities = await db.execute_fetchall(
                    "SELECT max_value FROM attributes WHERE character_id=? AND name=?",
                    (character_id, attribute),
                )
                occupied_rows = await db.execute_fetchall(
                    """SELECT COALESCE(SUM(i.quantity),0) occupied FROM inventory i
                       JOIN item_catalog ic ON ic.id=i.item_id
                       WHERE i.character_id=? AND ic.size=?""",
                    (character_id, item["size"]),
                )
                capacity = int(capacities[0]["max_value"])
                slot_talent = "Карманный склад" if item["size"] == "Малый" else "Вьючный ремень"
                talent = await db.execute_fetchall(
                    "SELECT 1 FROM talents WHERE character_id=? AND lower(name)=lower(?) LIMIT 1",
                    (character_id, slot_talent),
                )
                capacity += 1 if talent else 0
                occupied = int(occupied_rows[0]["occupied"])
                if occupied >= capacity:
                    await db.rollback()
                    return False, f"Нет свободного слота: {occupied}/{capacity}."
            consumable = is_consumable_item(item)
            stacks = await db.execute_fetchall(
                "SELECT id FROM inventory WHERE character_id=? AND item_id=? AND durability=? AND ammo IS ? AND equipped=0 ORDER BY id LIMIT 1",
                (character_id, item["item_id"], item["durability"], item["ammo"]),
            ) if consumable else []
            if stacks:
                await db.execute("UPDATE inventory SET quantity=quantity+1 WHERE id=?", (stacks[0]["id"],))
            else:
                await db.execute(
                    "INSERT INTO inventory(character_id,item_id,durability,ammo,quantity,equipped) VALUES(?,?,?,?,1,0)",
                    (character_id, item["item_id"], item["durability"], item["ammo"]),
                )
            if int(item["quantity"]) > 1:
                await db.execute("UPDATE supply_warehouse SET quantity=quantity-1 WHERE id=?", (warehouse_id,))
            else:
                await db.execute("DELETE FROM supply_warehouse WHERE id=?", (warehouse_id,))
            await db.commit()
            return True, f'«{item["name"]}» взят со склада снабжения.'

    async def deposit_to_supply_warehouse(
        self, guild_id: int, character_id: int, inventory_id: int, user_id: int,
    ) -> tuple[bool, str]:
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            rows = await db.execute_fetchall(
                """SELECT i.*,c.guild_id,ic.name,ic.properties FROM inventory i
                   JOIN characters c ON c.id=i.character_id
                   JOIN item_catalog ic ON ic.id=i.item_id
                   WHERE i.id=? AND i.character_id=?""",
                (inventory_id, character_id),
            )
            if not rows or int(rows[0]["guild_id"]) != guild_id:
                await db.rollback()
                return False, "Предмет в инвентаре не найден."
            item = dict(rows[0])
            if int(item["equipped"]):
                await db.rollback()
                return False, "Сначала снимите предмет."
            links = await db.execute_fetchall(
                "SELECT 1 FROM weapon_attachments WHERE weapon_inventory_id=? OR attachment_inventory_id=? LIMIT 1",
                (inventory_id, inventory_id),
            )
            if links:
                await db.rollback()
                return False, "Сначала снимите все насадки с предмета или оружия."
            consumable = is_consumable_item(item)
            stacks = await db.execute_fetchall(
                """SELECT id FROM supply_warehouse WHERE guild_id=? AND item_id=?
                   AND durability=? AND ammo IS ? ORDER BY id LIMIT 1""",
                (guild_id, item["item_id"], item["durability"], item["ammo"]),
            ) if consumable else []
            if stacks:
                await db.execute("UPDATE supply_warehouse SET quantity=quantity+1 WHERE id=?", (stacks[0]["id"],))
            else:
                await db.execute(
                    """INSERT INTO supply_warehouse(guild_id,item_id,durability,ammo,quantity,deposited_by)
                       VALUES(?,?,?,?,1,?)""",
                    (guild_id, item["item_id"], item["durability"], item["ammo"], user_id),
                )
            if int(item["quantity"]) > 1:
                if item["name"] in MULTI_USE_CONSUMABLES:
                    await db.execute(
                        "UPDATE inventory SET quantity=quantity-1,durability=? WHERE id=?",
                        (MULTI_USE_CONSUMABLES[item["name"]], inventory_id),
                    )
                else:
                    await db.execute("UPDATE inventory SET quantity=quantity-1 WHERE id=?", (inventory_id,))
            else:
                await db.execute("DELETE FROM inventory WHERE id=?", (inventory_id,))
            await db.commit()
            return True, f'«{item["name"]}» помещён на склад снабжения.'

    async def talent_names(self, character_id: int) -> set[str]:
        async with self.connect() as db:
            rows = await db.execute_fetchall(
                "SELECT name FROM talents WHERE character_id=?",
                (character_id,),
            )
            return {str(row["name"]) for row in rows}

    async def grant_talent(self, character_id: int, name: str, description: str) -> bool:
        async with self.connect() as db:
            exists = await db.execute_fetchall(
                "SELECT 1 FROM talents WHERE character_id=? AND lower(name)=lower(?) LIMIT 1",
                (character_id, name),
            )
            if exists:
                return False
            await db.execute(
                "INSERT INTO talents(character_id,name,description) VALUES(?,?,?)",
                (character_id, name, description),
            )
            await db.commit()
            return True

    async def purchase_talent(
        self,
        character_id: int,
        name: str,
        description: str,
        price: int,
        rank_required: int,
        class_name: str | None = None,
        starter_names: tuple[str, ...] = (),
        skill_requirements: dict[str, int] | None = None,
        talent_requirements: tuple[str, ...] = (),
    ) -> tuple[bool, str, int | None]:
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            rows = await db.execute_fetchall(
                "SELECT class_name,rank_index,supply_forms FROM characters WHERE id=?",
                (character_id,),
            )
            if not rows:
                await db.rollback()
                return False, "Персонаж не найден.", None
            character = rows[0]
            if class_name and character["class_name"] != class_name:
                await db.rollback()
                return False, "Этот талант принадлежит другому классу.", None
            if int(character["rank_index"]) < rank_required:
                await db.rollback()
                return False, "Текущее звание недостаточно для этого таланта.", None
            for skill, required_level in (skill_requirements or {}).items():
                rows = await db.execute_fetchall(
                    "SELECT value FROM skills WHERE character_id=? AND name=?",
                    (character_id, skill),
                )
                current_level = int(rows[0]["value"]) if rows else -3
                if current_level < int(required_level):
                    await db.rollback()
                    return False, f"Требуется навык {skill} {required_level}; сейчас {current_level}.", None
            for required_talent in talent_requirements:
                rows = await db.execute_fetchall(
                    "SELECT 1 FROM talents WHERE character_id=? AND lower(name)=lower(?) LIMIT 1",
                    (character_id, required_talent),
                )
                if not rows:
                    await db.rollback()
                    return False, f'Сначала требуется получить талант «{required_talent}».', None
            exists = await db.execute_fetchall(
                "SELECT 1 FROM talents WHERE character_id=? AND lower(name)=lower(?) LIMIT 1",
                (character_id, name),
            )
            if exists:
                await db.rollback()
                return False, "Этот талант уже получен.", None
            if starter_names:
                placeholders = ",".join("?" for _ in starter_names)
                chosen = await db.execute_fetchall(
                    f"SELECT 1 FROM talents WHERE character_id=? AND name IN ({placeholders}) LIMIT 1",
                    (character_id, *starter_names),
                )
                if chosen:
                    await db.rollback()
                    return False, "Стартовый классовый талант уже выбран.", None
            balance = int(character["supply_forms"])
            if balance < price:
                await db.rollback()
                return False, f"Недостаточно БС: требуется {price}, доступно {balance}.", balance
            await db.execute(
                "INSERT INTO talents(character_id,name,description) VALUES(?,?,?)",
                (character_id, name, description),
            )
            balance -= price
            await db.execute(
                "UPDATE characters SET supply_forms=? WHERE id=?",
                (balance, character_id),
            )
            await db.commit()
            return True, f"Получен талант «{name}» за {price} БС.", balance

    async def transfer_item(
        self,
        sender_id: int,
        recipient_id: int,
        name: str,
        quantity: int,
    ) -> tuple[bool, str]:
        if sender_id == recipient_id or quantity < 1:
            return False, "Некорректная передача."
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            rows = await db.execute_fetchall(
                """SELECT inventory.*,item_catalog.name,item_catalog.size,item_catalog.properties
                   FROM inventory JOIN item_catalog ON item_catalog.id=inventory.item_id
                   WHERE inventory.character_id=? AND lower(item_catalog.name)=lower(?)
                   ORDER BY inventory.equipped,inventory.id""",
                (sender_id, name),
            )
            # catalog_loader expects a normal mapping with .get(); aiosqlite.Row
            # only supports subscription and used to crash every item transfer.
            available = [dict(row) for row in rows if not row["equipped"]]
            if sum(int(row["quantity"]) for row in available) < quantity:
                await db.rollback()
                return False, "Предмета нет в нужном количестве или он экипирован."
            item = available[0]
            if item["size"] in {"Малый", "Большой"}:
                attribute = "Ловкость" if item["size"] == "Малый" else "Телосложение"
                capacities = await db.execute_fetchall(
                    "SELECT max_value FROM attributes WHERE character_id=? AND name=?",
                    (recipient_id, attribute),
                )
                if not capacities:
                    await db.rollback()
                    return False, "У получателя нет персонажа."
                occupied_rows = await db.execute_fetchall(
                    """SELECT COALESCE(SUM(inventory.quantity),0) occupied
                       FROM inventory JOIN item_catalog ON item_catalog.id=inventory.item_id
                       WHERE inventory.character_id=? AND item_catalog.size=?""",
                    (recipient_id, item["size"]),
                )
                occupied, capacity = int(occupied_rows[0]["occupied"]), int(capacities[0]["max_value"])
                slot_talent = "Карманный склад" if item["size"] == "Малый" else "Вьючный ремень"
                talent_rows = await db.execute_fetchall(
                    "SELECT 1 FROM talents WHERE character_id=? AND lower(name)=lower(?) LIMIT 1",
                    (recipient_id, slot_talent),
                )
                capacity += 1 if talent_rows else 0
                if occupied + quantity > capacity:
                    await db.rollback()
                    return False, f"У получателя недостаточно слотов: {occupied}/{capacity}."
            remaining = quantity
            stackable = is_consumable_item(item)
            for row in available:
                if remaining <= 0:
                    break
                taken = min(int(row["quantity"]), remaining)
                if stackable:
                    targets = await db.execute_fetchall(
                        "SELECT id FROM inventory WHERE character_id=? AND item_id=? AND durability=? AND ammo IS ? AND equipped=0 LIMIT 1",
                        (recipient_id, row["item_id"], row["durability"], row["ammo"]),
                    )
                    if targets:
                        await db.execute(
                            "UPDATE inventory SET quantity=quantity+? WHERE id=?",
                            (taken, targets[0]["id"]),
                        )
                    else:
                        await db.execute(
                            """INSERT INTO inventory(
                                   character_id,item_id,durability,ammo,quantity,equipped,notes
                               ) VALUES(?,?,?,?,?,0,?)""",
                            (recipient_id, row["item_id"], row["durability"], row["ammo"], taken, row["notes"]),
                        )
                else:
                    await db.execute(
                        """INSERT INTO inventory(
                               character_id,item_id,durability,ammo,quantity,equipped,notes
                           ) VALUES(?,?,?,?,?,0,?)""",
                        (recipient_id, row["item_id"], row["durability"], row["ammo"], taken, row["notes"]),
                    )
                if taken == int(row["quantity"]):
                    await db.execute("DELETE FROM inventory WHERE id=?", (row["id"],))
                else:
                    if row["name"] in MULTI_USE_CONSUMABLES:
                        await db.execute(
                            "UPDATE inventory SET quantity=quantity-?,durability=? WHERE id=?",
                            (taken, MULTI_USE_CONSUMABLES[row["name"]], row["id"]),
                        )
                    else:
                        await db.execute(
                            "UPDATE inventory SET quantity=quantity-? WHERE id=?",
                            (taken, row["id"]),
                        )
                remaining -= taken
            await db.commit()
            return True, f'Передано: {item["name"]} ×{quantity}.'

    async def consume_multi_use_item(
        self, character_id: int, inventory_id: int, item_name: str,
    ) -> dict[str, int] | None:
        max_uses = MULTI_USE_CONSUMABLES.get(item_name)
        if not max_uses:
            return None
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            rows = await db.execute_fetchall(
                """SELECT inventory.id,inventory.quantity,inventory.durability,item_catalog.name
                   FROM inventory JOIN item_catalog ON item_catalog.id=inventory.item_id
                   WHERE inventory.id=? AND inventory.character_id=?""",
                (inventory_id, character_id),
            )
            if not rows or rows[0]["name"] != item_name:
                await db.rollback()
                return None
            row = rows[0]
            quantity = int(row["quantity"])
            remaining = int(row["durability"]) - 1
            finished_item = remaining <= 0
            if not finished_item:
                await db.execute(
                    "UPDATE inventory SET durability=? WHERE id=?",
                    (remaining, inventory_id),
                )
            elif quantity > 1:
                quantity -= 1
                remaining = max_uses
                await db.execute(
                    "UPDATE inventory SET quantity=?,durability=? WHERE id=?",
                    (quantity, remaining, inventory_id),
                )
            else:
                quantity = 0
                remaining = 0
                await db.execute("DELETE FROM inventory WHERE id=?", (inventory_id,))
            await db.commit()
            return {
                "remaining_uses": remaining,
                "max_uses": max_uses,
                "quantity": quantity,
                "finished_item": int(finished_item),
            }

    async def remove_inventory_by_name(self, character_id: int, name: str, quantity: int) -> bool:
        if quantity < 1:
            return False
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            rows = await db.execute_fetchall(
                """SELECT inventory.id,inventory.quantity,item_catalog.name
                   FROM inventory
                   JOIN item_catalog ON item_catalog.id=inventory.item_id
                   WHERE inventory.character_id=?
                   ORDER BY inventory.id""",
                (character_id,),
            )
            rows = [row for row in rows if str(row["name"]).casefold() == name.casefold()]
            if sum(int(row["quantity"]) for row in rows) < quantity:
                await db.rollback()
                return False
            remaining = quantity
            for row in rows:
                if remaining <= 0:
                    break
                row_quantity = int(row["quantity"])
                taken = min(row_quantity, remaining)
                if taken == row_quantity:
                    await db.execute("DELETE FROM inventory WHERE id=?", (row["id"],))
                else:
                    if row["name"] in MULTI_USE_CONSUMABLES:
                        await db.execute(
                            "UPDATE inventory SET quantity=quantity-?,durability=? WHERE id=?",
                            (taken, MULTI_USE_CONSUMABLES[row["name"]], row["id"]),
                        )
                    else:
                        await db.execute(
                            "UPDATE inventory SET quantity=quantity-? WHERE id=?",
                            (taken, row["id"]),
                        )
                remaining -= taken
            await db.commit()
            return True

    async def inventory(self, character_id: int) -> list[dict[str, Any]]:
        async with self.connect() as db:
            rows = await db.execute_fetchall(
                """SELECT inventory.*,item_catalog.name,item_catalog.size,item_catalog.category,
                          item_catalog.description,item_catalog.max_durability,item_catalog.use_range,
                          item_catalog.ammo_max,item_catalog.fire_rate,item_catalog.attribute_modifiers,
                          item_catalog.skill_modifiers,item_catalog.hands,item_catalog.gear,item_catalog.damage,
                          item_catalog.damage_type,item_catalog.defense,item_catalog.properties,
                          item_catalog.conditions,item_catalog.armor_slot,item_catalog.price,item_catalog.access,
                          item_catalog.prosthetic_slot,item_catalog.will_cost,item_catalog.icon_file
                   FROM inventory JOIN item_catalog ON item_catalog.id=inventory.item_id
                   WHERE character_id=? ORDER BY inventory.equipped DESC,item_catalog.size,item_catalog.name""",
                (character_id,),
            )
            items = [dict(row) for row in rows]
            links = await db.execute_fetchall(
                """SELECT wa.weapon_inventory_id,ic.name FROM weapon_attachments wa
                   JOIN inventory ai ON ai.id=wa.attachment_inventory_id
                   JOIN item_catalog ic ON ic.id=ai.item_id
                   JOIN inventory wi ON wi.id=wa.weapon_inventory_id
                   WHERE wi.character_id=?""", (character_id,)
            )
            attached = {}
            for link in links:
                attached.setdefault(int(link["weapon_inventory_id"]), []).append(ATTACHMENT_BY_NAME[link["name"]])
            return [apply_attachments(item, attached.get(int(item["id"]), [])) for item in items]

    async def weapon_attachments(self, character_id: int, weapon_id: int | None = None) -> list[dict[str, Any]]:
        async with self.connect() as db:
            query = """SELECT wa.slot,wa.weapon_inventory_id,wa.attachment_inventory_id,ic.name
                       FROM weapon_attachments wa JOIN inventory ai ON ai.id=wa.attachment_inventory_id
                       JOIN item_catalog ic ON ic.id=ai.item_id JOIN inventory wi ON wi.id=wa.weapon_inventory_id
                       WHERE wi.character_id=?"""
            params = [character_id]
            if weapon_id is not None:
                query += " AND wa.weapon_inventory_id=?"
                params.append(weapon_id)
            return [dict(row) for row in await db.execute_fetchall(query, params)]

    async def install_attachment(self, character_id: int, weapon_id: int, attachment_id: int) -> tuple[bool, str]:
        async with self.connect() as db:
            rows = await db.execute_fetchall(
                """SELECT i.id,ic.name,ic.category,ic.conditions,ic.fire_rate FROM inventory i
                   JOIN item_catalog ic ON ic.id=i.item_id WHERE i.character_id=? AND i.id IN (?,?)""",
                (character_id, weapon_id, attachment_id),
            )
            weapon = next((dict(r) for r in rows if r["id"] == weapon_id and r["category"] == "Оружие дальнего боя"), None)
            attachment = next((dict(r) for r in rows if r["id"] == attachment_id and r["category"] == "Насадка"), None)
            if not weapon or not attachment:
                return False, "Выберите своё огнестрельное оружие и насадку."
            spec = ATTACHMENT_BY_NAME.get(attachment["name"])
            if not spec or not compatible(spec, weapon):
                return False, "Эта насадка несовместима с выбранным оружием."
            if await db.execute_fetchall("SELECT 1 FROM weapon_attachments WHERE weapon_inventory_id=? AND slot=?", (weapon_id, spec["slot"])):
                return False, f'Слот «{spec["slot"]}» уже занят.'
            try:
                await db.execute("INSERT INTO weapon_attachments VALUES(?,?,?)", (weapon_id, attachment_id, spec["slot"]))
            except Exception:
                return False, "Эта насадка уже установлена на другое оружие."
            await db.commit()
            return True, f'{attachment["name"]} установлена на {weapon["name"]}.'

    async def remove_attachment(self, character_id: int, weapon_id: int, attachment_id: int) -> tuple[bool, str]:
        async with self.connect() as db:
            cursor = await db.execute(
                """DELETE FROM weapon_attachments WHERE weapon_inventory_id=? AND attachment_inventory_id=?
                   AND EXISTS(SELECT 1 FROM inventory WHERE id=? AND character_id=?)""",
                (weapon_id, attachment_id, weapon_id, character_id),
            )
            await db.commit()
            return cursor.rowcount > 0, ("Насадка снята." if cursor.rowcount else "Такая насадка не установлена.")

    async def inventory_item_by_name(self, character_id: int, name: str, equipped_only: bool = False) -> dict[str, Any] | None:
        items = await self.inventory(character_id)
        normalized_name = name.strip().casefold()
        return next(
            (
                item for item in items
                if str(item["name"]).strip().casefold() == normalized_name
                and (not equipped_only or bool(item["equipped"]))
            ),
            None,
        )

    async def set_equipped(self, character_id: int, row_id: int, equipped: bool, position: str | None = None) -> tuple[bool, str]:
        async with self.connect() as db:
            rows = await db.execute_fetchall(
                """SELECT inventory.*,item_catalog.name,item_catalog.size,item_catalog.category,
                          item_catalog.hands,item_catalog.armor_slot,item_catalog.max_durability,
                          item_catalog.prosthetic_slot,item_catalog.will_cost
                   FROM inventory JOIN item_catalog ON item_catalog.id=inventory.item_id
                   WHERE inventory.id=? AND inventory.character_id=?""",
                (row_id, character_id),
            )
            if not rows:
                return False, "Предмет не найден"
            item = dict(rows[0])
            if not equipped:
                await db.execute("UPDATE inventory SET equipped=0,equipped_position=NULL WHERE id=?", (row_id,))
                await db.commit()
                return True, f'Снято: {item["name"]}'
            if int(item["durability"]) <= 0:
                return False, "Сломанный предмет нельзя экипировать"
            is_trinket = item["size"] == "Безделушка"
            if item["category"] not in {"Броня", "Щит", "Оружие ближнего боя", "Оружие дальнего боя", "Протезы"} and not is_trinket:
                return False, "Этот предмет не требует экипировки"
            equipped_rows = await db.execute_fetchall(
                """SELECT inventory.id,inventory.equipped_position,item_catalog.size,item_catalog.category,item_catalog.hands,item_catalog.prosthetic_slot,item_catalog.will_cost
                   FROM inventory JOIN item_catalog ON item_catalog.id=inventory.item_id
                   WHERE inventory.character_id=? AND inventory.equipped=1""",
                (character_id,),
            )
            if int(item["hands"] or 0) >= 2:
                injuries = await db.execute_fetchall(
                    """SELECT impairment_key,compensation_position FROM injuries
                       WHERE character_id=? AND active=1
                         AND (expires_at IS NULL OR expires_at>CURRENT_TIMESTAMP)
                         AND impairment_key IN ('lost_left_arm','lost_right_arm')""",
                    (character_id,),
                )
                prosthetic_positions = {
                    str(row["equipped_position"] or row["prosthetic_slot"] or "").casefold()
                    for row in equipped_rows if row["category"] == "Протезы"
                }
                for injury in injuries:
                    required = str(injury["compensation_position"] or "").casefold()
                    compensated = any(
                        actual == required or (
                            required in {"левая рука", "правая рука"} and actual.endswith(required)
                        )
                        for actual in prosthetic_positions
                    )
                    if not compensated:
                        return False, "Нельзя экипировать двуручное оружие: потеря руки не компенсирована подходящим протезом"
            if item["category"] == "Протезы":
                character_rows = await db.execute_fetchall("SELECT race FROM characters WHERE id=?", (character_id,))
                race = str(character_rows[0]["race"])
                if race == "Тараканы" and item["prosthetic_slot"] == "Хвост":
                    return False, "Тараканам нельзя устанавливать протез хвоста"
                position_options = {
                    "Рука": (["Верхняя правая рука", "Верхняя левая рука", "Нижняя правая рука", "Нижняя левая рука"] if race == "Тараканы" else ["Правая рука", "Левая рука"]),
                    "Нога": ["Правая нога", "Левая нога"],
                }
                allowed_positions = position_options.get(item["prosthetic_slot"])
                if allowed_positions:
                    if position not in allowed_positions:
                        return False, "Выберите конкретную позицию установки"
                    occupied_positions = {
                        str(row["equipped_position"])
                        for row in equipped_rows
                        if row["category"] == "Протезы" and int(row["id"]) != row_id
                    }
                    if position in occupied_positions:
                        return False, f'Позиция «{position}» уже занята'
                else:
                    position = str(item["prosthetic_slot"])
                limits = {"Рука": 4 if race == "Тараканы" else 2, "Нога": 2, "Глаза": 1, "Голова": 1, "Кожа": 1, "Корпус": 1, "Оружейный модуль": 1, "Хвост": 0 if race == "Тараканы" else 1}
                occupied = sum(row["category"] == "Протезы" and row["prosthetic_slot"] == item["prosthetic_slot"] and int(row["id"]) != row_id for row in equipped_rows)
                if occupied >= limits.get(item["prosthetic_slot"], 1):
                    return False, f'Все места слота «{item["prosthetic_slot"]}» уже заняты'
            elif item["category"] == "Броня" and any(
                row["category"] == "Броня" and row["size"] == item["size"] and int(row["id"]) != row_id
                for row in equipped_rows
            ):
                return False, f'Уже экипирована броня размера «{item["size"]}»'
            if is_trinket:
                trinkets = sum(
                    row["size"] == "Безделушка" and int(row["id"]) != row_id
                    for row in equipped_rows
                )
                if trinkets >= 4:
                    return False, "Можно экипировать не более четырёх безделушек"
            elif item["category"] not in {"Броня", "Протезы"}:
                character_rows = await db.execute_fetchall("SELECT hands FROM characters WHERE id=?", (character_id,))
                hand_limit = int(character_rows[0]["hands"])
                occupied = sum(
                    int(row["hands"] or 0)
                    for row in equipped_rows
                    if row["category"] != "Броня" and int(row["id"]) != row_id
                )
                if occupied + int(item["hands"] or 0) > hand_limit:
                    return False, f"Недостаточно свободных рук: занято {occupied}/{hand_limit}"
            await db.execute("UPDATE inventory SET equipped=1,equipped_position=? WHERE id=?", (position if item["category"] == "Протезы" else None, row_id))
            if item["category"] == "Протезы":
                total = sum(int(row["will_cost"] or 0) for row in equipped_rows if row["category"] == "Протезы" and int(row["id"]) != row_id) + int(item["will_cost"] or 0)
                await db.execute("UPDATE characters SET will_current=MIN(will_current,MAX(0,will_max-?)) WHERE id=?", (total, character_id))
            await db.commit()
            return True, f'Экипировано: {item["name"]}'

    async def add_timed_effect(
        self,
        character_id: int,
        source_name: str,
        attribute_name: str,
        modifier: int,
        hours: int,
        description: str,
    ) -> None:
        expires_at = (datetime.now(UTC) + timedelta(hours=hours)).isoformat()
        async with self.connect() as db:
            await db.execute(
                """INSERT INTO character_effects(
                       character_id,source_name,attribute_name,modifier,expires_at,description
                   ) VALUES(?,?,?,?,?,?)""",
                (character_id, source_name, attribute_name, modifier, expires_at, description),
            )
            await db.commit()

    async def active_effects(self, character_id: int) -> list[dict[str, Any]]:
        now = datetime.now(UTC).isoformat()
        async with self.connect() as db:
            await db.execute(
                "DELETE FROM character_effects WHERE character_id=? AND expires_at IS NOT NULL AND expires_at<=?",
                (character_id, now),
            )
            rows = await db.execute_fetchall(
                "SELECT * FROM character_effects WHERE character_id=? ORDER BY id",
                (character_id,),
            )
            await db.commit()
            return [dict(row) for row in rows]

    async def consume_will_guard(self, character_id: int) -> int:
        async with self.connect() as db:
            rows = await db.execute_fetchall(
                """SELECT id,modifier FROM character_effects
                   WHERE character_id=? AND attribute_name='Воля'
                   ORDER BY id LIMIT 1""",
                (character_id,),
            )
            if not rows:
                return 0
            await db.execute("DELETE FROM character_effects WHERE id=?", (rows[0]["id"],))
            await db.commit()
            return max(0, int(rows[0]["modifier"]))

    async def consume_ammo(self, row_id: int, character_id: int, amount: int) -> int | None:
        async with self.connect() as db:
            rows = await db.execute_fetchall(
                "SELECT ammo FROM inventory WHERE id=? AND character_id=?", (row_id, character_id)
            )
            if not rows or rows[0]["ammo"] is None or int(rows[0]["ammo"]) < amount:
                return None
            value = int(rows[0]["ammo"]) - amount
            await db.execute("UPDATE inventory SET ammo=? WHERE id=?", (value, row_id))
            await db.commit()
            return value

    async def adjust_inventory_ammo(
        self,
        row_id: int,
        character_id: int,
        delta: int,
    ) -> tuple[int, int, int] | None:
        async with self.connect() as db:
            rows = await db.execute_fetchall(
                """SELECT inventory.ammo,item_catalog.ammo_max
                   FROM inventory JOIN item_catalog ON item_catalog.id=inventory.item_id
                   WHERE inventory.id=? AND inventory.character_id=?""",
                (row_id, character_id),
            )
            if not rows or rows[0]["ammo_max"] is None:
                return None
            before = int(rows[0]["ammo"] or 0)
            maximum = int(rows[0]["ammo_max"])
            after = max(0, min(maximum, before + delta))
            await db.execute("UPDATE inventory SET ammo=? WHERE id=?", (after, row_id))
            await db.commit()
            return before, after, maximum

    async def reload_weapon(
        self,
        row_id: int,
        character_id: int,
        package_names: tuple[str, ...],
        amount: int | None = None,
    ) -> tuple[int, int, int, int, int] | None:
        """Заряжает оружие конкретными патронами из совместимых упаковок."""
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            weapon_rows = await db.execute_fetchall(
                """SELECT inventory.ammo,item_catalog.ammo_max
                   FROM inventory JOIN item_catalog ON item_catalog.id=inventory.item_id
                   WHERE inventory.id=? AND inventory.character_id=?""",
                (row_id, character_id),
            )
            if not weapon_rows or weapon_rows[0]["ammo_max"] is None:
                await db.rollback()
                return None
            current = int(weapon_rows[0]["ammo"] or 0)
            maximum = int(weapon_rows[0]["ammo_max"])
            attachment_rows = await db.execute_fetchall(
                """SELECT ic.name FROM weapon_attachments wa
                   JOIN inventory ai ON ai.id=wa.attachment_inventory_id
                   JOIN item_catalog ic ON ic.id=ai.item_id WHERE wa.weapon_inventory_id=?""",
                (row_id,),
            )
            maximum += sum(
                int(ATTACHMENT_BY_NAME[row["name"]].get("ammo") or 0)
                for row in attachment_rows
            )
            placeholders = ",".join("?" for _ in package_names)
            packages = await db.execute_fetchall(
                f"""SELECT inventory.id,inventory.ammo,item_catalog.ammo_max
                    FROM inventory JOIN item_catalog ON item_catalog.id=inventory.item_id
                    WHERE inventory.character_id=?
                    AND item_catalog.name IN ({placeholders})
                    ORDER BY COALESCE(inventory.ammo,0),inventory.id""",
                (character_id, *package_names),
            )
            available = sum(int(row["ammo"] or 0) for row in packages)
            requested = maximum - current if amount is None else max(0, int(amount))
            loaded = min(maximum - current, available, requested)
            if loaded <= 0:
                await db.rollback()
                return current, current, maximum, 0, available
            left = loaded
            for package in packages:
                if left <= 0:
                    break
                stored = int(package["ammo"] or 0)
                taken = min(stored, left)
                if taken:
                    await db.execute(
                        "UPDATE inventory SET ammo=? WHERE id=?",
                        (stored - taken, package["id"]),
                    )
                    left -= taken
            after = current + loaded
            await db.execute(
                "UPDATE inventory SET ammo=? WHERE id=? AND character_id=?",
                (after, row_id, character_id),
            )
            await db.commit()
            return current, after, maximum, loaded, available - loaded

    async def unload_weapon(
        self,
        row_id: int,
        character_id: int,
        package_names: tuple[str, ...],
        medium_package_name: str,
        amount: int | None = None,
    ) -> tuple[int, int, int, int, int] | None:
        """Возвращает патроны в неполные упаковки и создаёт средние при нехватке места."""
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            weapon_rows = await db.execute_fetchall(
                """SELECT inventory.ammo,item_catalog.ammo_max
                   FROM inventory JOIN item_catalog ON item_catalog.id=inventory.item_id
                   WHERE inventory.id=? AND inventory.character_id=?""",
                (row_id, character_id),
            )
            if not weapon_rows or weapon_rows[0]["ammo_max"] is None:
                await db.rollback()
                return None
            current = int(weapon_rows[0]["ammo"] or 0)
            maximum = int(weapon_rows[0]["ammo_max"])
            requested = current if amount is None else min(current, max(0, int(amount)))
            if requested <= 0:
                await db.rollback()
                return current, current, maximum, 0, 0
            placeholders = ",".join("?" for _ in package_names)
            packages = await db.execute_fetchall(
                f"""SELECT inventory.id,inventory.ammo,item_catalog.ammo_max
                    FROM inventory JOIN item_catalog ON item_catalog.id=inventory.item_id
                    WHERE inventory.character_id=?
                    AND item_catalog.name IN ({placeholders})
                    ORDER BY COALESCE(inventory.ammo,0) DESC,inventory.id""",
                (character_id, *package_names),
            )
            left = requested
            for package in packages:
                if left <= 0:
                    break
                stored = int(package["ammo"] or 0)
                capacity = int(package["ammo_max"] or 0)
                added = min(max(0, capacity - stored), left)
                if added:
                    await db.execute(
                        "UPDATE inventory SET ammo=? WHERE id=?",
                        (stored + added, package["id"]),
                    )
                    left -= added
            medium_rows = await db.execute_fetchall(
                """SELECT id,max_durability,ammo_max FROM item_catalog
                   WHERE lower(name)=lower(?) AND guild_id IN (
                       0,(SELECT guild_id FROM characters WHERE id=?)
                   ) ORDER BY guild_id DESC LIMIT 1""",
                (medium_package_name, character_id),
            )
            if left > 0 and not medium_rows:
                await db.rollback()
                return None
            if left > 0:
                medium = medium_rows[0]
                capacity = int(medium["ammo_max"] or 6)
                while left > 0:
                    stored = min(capacity, left)
                    await db.execute(
                        """INSERT INTO inventory(character_id,item_id,durability,ammo,quantity)
                           VALUES(?,?,?,?,1)""",
                        (character_id, medium["id"], medium["max_durability"], stored),
                    )
                    left -= stored
            unloaded = requested
            after = current - unloaded
            await db.execute(
                "UPDATE inventory SET ammo=? WHERE id=? AND character_id=?",
                (after, row_id, character_id),
            )
            totals = await db.execute_fetchall(
                f"""SELECT COALESCE(SUM(inventory.ammo),0) total
                    FROM inventory JOIN item_catalog ON item_catalog.id=inventory.item_id
                    WHERE inventory.character_id=?
                    AND item_catalog.name IN ({placeholders})""",
                (character_id, *package_names),
            )
            inventory_after = int(totals[0]["total"] or 0)
            await db.commit()
            return current, after, maximum, unloaded, inventory_after

    async def update_inventory_state(self, row_id: int, character_id: int, durability: int | None, ammo: int | None) -> bool:
        async with self.connect() as db:
            rows = await db.execute_fetchall(
                "SELECT inventory.id,item_catalog.max_durability,item_catalog.ammo_max FROM inventory JOIN item_catalog ON item_catalog.id=inventory.item_id WHERE inventory.id=? AND character_id=?",
                (row_id, character_id),
            )
            if not rows:
                return False
            row = rows[0]
            if durability is not None:
                await db.execute("UPDATE inventory SET durability=? WHERE id=?", (max(0, min(int(row["max_durability"]), durability)), row_id))
            if ammo is not None and row["ammo_max"] is not None:
                await db.execute("UPDATE inventory SET ammo=? WHERE id=?", (max(0, min(int(row["ammo_max"]), ammo)), row_id))
            await db.commit()
            return True

    async def adjust_inventory_durability(self, row_id: int, character_id: int, delta: int) -> int | None:
        async with self.connect() as db:
            rows = await db.execute_fetchall(
                """SELECT inventory.durability,item_catalog.max_durability
                   FROM inventory
                   JOIN item_catalog ON item_catalog.id=inventory.item_id
                   WHERE inventory.id=? AND inventory.character_id=?""",
                (row_id, character_id),
            )
            if not rows:
                return None
            current = int(rows[0]["durability"])
            maximum = int(rows[0]["max_durability"])
            value = max(0, min(maximum, current + delta))
            await db.execute(
                "UPDATE inventory SET durability=? WHERE id=? AND character_id=?",
                (value, row_id, character_id),
            )
            await db.commit()
            return value
