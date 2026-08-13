from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from catalog_loader import load_catalog
from attachment_data import ATTACHMENT_BY_NAME, apply_attachments, compatible
from constants import ATTRIBUTES, CLASSES, SKILLS
from talent_data import TALENTS
from medal_data import MEDALS


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
            character_columns = {row["name"] for row in await db.execute_fetchall("PRAGMA table_info(characters)")}
            if "hands" not in character_columns:
                await db.execute("ALTER TABLE characters ADD COLUMN hands INTEGER NOT NULL DEFAULT 2")
                await db.execute("UPDATE characters SET hands=4 WHERE race='Тараканы'")
            if "rat_recovery_at" not in character_columns:
                await db.execute("ALTER TABLE characters ADD COLUMN rat_recovery_at TEXT")
            if "skills_initialized" not in character_columns:
                await db.execute("ALTER TABLE characters ADD COLUMN skills_initialized INTEGER NOT NULL DEFAULT 1")
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
            for medal in MEDALS:
                await db.execute(
                    """INSERT INTO medal_catalog(code,name,description,effect,ribbon,metal)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(code) DO UPDATE SET
                         name=excluded.name,description=excluded.description,effect=excluded.effect,
                         ribbon=excluded.ribbon,metal=excluded.metal""",
                    (
                        medal["code"], medal["name"], medal["description"], medal["effect"],
                        medal.get("ribbon", medal.get("image", "")), medal.get("metal", ""),
                    ),
                )
            await db.commit()
        await self.reload_base_catalog()
        await self.migrate_ammo_packages()
        await self.repair_ammo_package_contents()
        await self.merge_stackable_inventory()
        await self.split_nonstackable_inventory()

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
                """SELECT inventory.character_id,inventory.item_id,
                          MIN(inventory.id) keep_id,SUM(inventory.quantity) total,COUNT(*) rows_count
                   FROM inventory
                   JOIN item_catalog ON item_catalog.id=inventory.item_id
                   WHERE lower(item_catalog.properties) LIKE '%расходник%'
                   GROUP BY inventory.character_id,inventory.item_id
                   HAVING COUNT(*)>1"""
            )
            for group in groups:
                await db.execute(
                    "UPDATE inventory SET quantity=? WHERE id=?",
                    (group["total"], group["keep_id"]),
                )
                cursor = await db.execute(
                    "DELETE FROM inventory WHERE character_id=? AND item_id=? AND id<>?",
                    (group["character_id"], group["item_id"], group["keep_id"]),
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
            await db.execute(
                f"""DELETE FROM item_catalog
                    WHERE guild_id=0 AND source_number IS NOT NULL
                    AND source_number NOT IN ({placeholders})""",
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
                           defense,price,access,properties,conditions,armor_slot,prosthetic_slot,will_cost,icon_file,created_by
                       ) VALUES(0,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
                       ON CONFLICT(guild_id,name) DO UPDATE SET
                           size=excluded.size,category=excluded.category,max_durability=excluded.max_durability,
                           description=excluded.description,use_range=excluded.use_range,ammo_max=excluded.ammo_max,
                           fire_rate=excluded.fire_rate,source_number=excluded.source_number,hands=excluded.hands,
                           gear=excluded.gear,damage=excluded.damage,damage_type=excluded.damage_type,
                           defense=excluded.defense,price=excluded.price,access=excluded.access,
                           properties=excluded.properties,conditions=excluded.conditions,armor_slot=excluded.armor_slot,
                           prosthetic_slot=excluded.prosthetic_slot,will_cost=excluded.will_cost,icon_file=excluded.icon_file""",
                    (
                        item["name"], item["size"], item["category"], item["max_durability"], item["description"],
                        item["use_range"], item["ammo_max"], item["fire_rate"], "{}", "{}",
                        item["source_number"], item["hands"], item["gear"], item["damage"], item["damage_type"],
                        item["defense"], item["price"], item["access"], item["properties"], item["conditions"],
                        item["armor_slot"], item.get("slot"), item.get("will_cost", 0), item.get("icon_file"),
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
            result["attributes"] = {r["name"]: {"current": r["current_value"], "max": r["max_value"]} for r in attrs}
            result["skills"] = {r["name"]: r["value"] for r in skills}
            result["talents"] = {r["name"]: r["description"] for r in talents}
            costs = await db.execute_fetchall(
                """SELECT COALESCE(SUM(item_catalog.will_cost),0) cost FROM inventory
                   JOIN item_catalog ON item_catalog.id=inventory.item_id
                   WHERE inventory.character_id=? AND inventory.equipped=1 AND item_catalog.category='Протезы'""", (result["id"],)
            )
            result["will_base_max"] = int(result["will_max"])
            result["will_max"] = max(0, int(result["will_max"]) - int(costs[0]["cost"]))
            result["will_current"] = min(int(result["will_current"]), result["will_max"])
            return result

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
            after = max(0, min(5, before + delta))
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
                "INSERT INTO injuries(character_id,attribute_name,roll_code,name,description,penalties,duration,expires_at) VALUES(?,?,?,?,?,?,?,?)",
                (character_id, attribute, trauma.code, trauma.name, trauma.description, trauma.penalties, trauma.duration, expires_at),
            )
            if attribute in ("Телосложение", "Ловкость"):
                await db.execute("UPDATE characters SET infection=infection+1 WHERE id=?", (character_id,))
            await db.commit()

    async def add_pending_injury(self, character_id: int, attribute: str, roll_code: int) -> None:
        async with self.connect() as db:
            await db.execute(
                "INSERT INTO injuries(character_id,attribute_name,roll_code,name,description,penalties,duration) VALUES(?,?,?,?,?,?,?)",
                (character_id, attribute, roll_code, f"Травма {attribute} ({roll_code})", "Таблица для этой характеристики ещё не загружена.", "Определяет мастер", "Определяет мастер"),
            )
            if attribute in ("Телосложение", "Ловкость"):
                await db.execute("UPDATE characters SET infection=infection+1 WHERE id=?", (character_id,))
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
                   FROM character_medals
                   JOIN medal_catalog ON medal_catalog.code=character_medals.medal_code
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
                "DELETE FROM character_medals WHERE character_id=? AND medal_code=?",
                (character_id, medal_code),
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
            if "расходник" in str(item.get("properties") or "").casefold():
                rows = await db.execute_fetchall(
                    """SELECT id FROM inventory
                       WHERE character_id=? AND item_id=? AND equipped=0
                       ORDER BY id LIMIT 1""",
                    (character_id, item["id"]),
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
            required_skill = "Защита" if item["category"] == "Протезы" else "Снабжение"
            required_class = "Окопник" if item["category"] == "Протезы" else "Снабженец"
            skills = await db.execute_fetchall("SELECT value FROM skills WHERE character_id=? AND name=?", (character_id, required_skill))
            level = int(skills[0]["value"]) if skills and character["class_name"] == required_class else -99
            if required_supply_level > 0 and level < required_supply_level:
                await db.rollback()
                return False, f"Требуется {required_skill} {required_supply_level} и класс {required_class}.", None
            price = int(item["price"] or 0)
            if price < 1:
                await db.rollback()
                return False, "У предмета не указана цена.", None
            bureaucracy = await db.execute_fetchall(
                "SELECT 1 FROM talents WHERE character_id=? AND lower(name)=lower('Бюрократия') LIMIT 1",
                (character_id,),
            )
            discount = (1 if bureaucracy else 0) + (1 if item["category"] != "Протезы" and level > 6 else 0)
            price = max(1, price - discount)
            balance = int(character["supply_forms"])
            if balance < price:
                await db.rollback()
                return False, f"Недостаточно БС: требуется {price}, доступно {balance}.", balance
            if item["size"] in {"Малый", "Большой"}:
                attribute = "Ловкость" if item["size"] == "Малый" else "Телосложение"
                capacities = await db.execute_fetchall(
                    "SELECT max_value FROM attributes WHERE character_id=? AND name=?",
                    (character_id, attribute),
                )
                occupied_rows = await db.execute_fetchall(
                    """SELECT COALESCE(SUM(inventory.quantity),0) occupied
                       FROM inventory JOIN item_catalog ON item_catalog.id=inventory.item_id
                       WHERE inventory.character_id=? AND item_catalog.size=?""",
                    (character_id, item["size"]),
                )
                capacity = int(capacities[0]["max_value"])
                slot_talent = "Карманный склад" if item["size"] == "Малый" else "Вьючный ремень"
                talent_rows = await db.execute_fetchall(
                    "SELECT 1 FROM talents WHERE character_id=? AND lower(name)=lower(?) LIMIT 1",
                    (character_id, slot_talent),
                )
                capacity += 1 if talent_rows else 0
                occupied = int(occupied_rows[0]["occupied"])
                if occupied >= capacity:
                    await db.rollback()
                    return False, f"Нет свободного слота: {occupied}/{capacity}.", balance
            if "расходник" in str(item.get("properties") or "").casefold():
                stacks = await db.execute_fetchall(
                    """SELECT id FROM inventory
                       WHERE character_id=? AND item_id=? AND equipped=0
                       ORDER BY id LIMIT 1""",
                    (character_id, item_id),
                )
                if stacks:
                    await db.execute(
                        "UPDATE inventory SET quantity=quantity+1 WHERE id=?",
                        (stacks[0]["id"],),
                    )
                else:
                    await db.execute(
                        "INSERT INTO inventory(character_id,item_id,durability,ammo) VALUES(?,?,?,?)",
                        (character_id, item_id, item["max_durability"], item["ammo_max"]),
                    )
            else:
                await db.execute(
                    "INSERT INTO inventory(character_id,item_id,durability,ammo) VALUES(?,?,?,?)",
                    (character_id, item_id, item["max_durability"], item["ammo_max"]),
                )
            balance -= price
            await db.execute(
                "UPDATE characters SET supply_forms=? WHERE id=?",
                (balance, character_id),
            )
            await db.commit()
            return True, f'Приобретено: {item["name"]} за {price} БС.', balance

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
            available = [row for row in rows if not row["equipped"]]
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
            stackable = "расходник" in str(item["properties"] or "").casefold()
            for row in available:
                if remaining <= 0:
                    break
                taken = min(int(row["quantity"]), remaining)
                if stackable:
                    targets = await db.execute_fetchall(
                        "SELECT id FROM inventory WHERE character_id=? AND item_id=? AND equipped=0 LIMIT 1",
                        (recipient_id, row["item_id"]),
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
                    await db.execute(
                        "UPDATE inventory SET quantity=quantity-? WHERE id=?",
                        (taken, row["id"]),
                    )
                remaining -= taken
            await db.commit()
            return True, f'Передано: {item["name"]} ×{quantity}.'

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
        return next(
            (
                item for item in items
                if str(item["name"]).casefold() == name.casefold()
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
