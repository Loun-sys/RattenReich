from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from bot import bot, make_pool
from constants import ATTRIBUTES
from database import Database, SCHEMA
from medal_data import MEDALS, medal_bonus_summary


def test_catalog_and_roll_bonuses() -> None:
    assert len(MEDALS) == 38
    assert bot.tree.get_command("медаль") is not None
    assets = Path(__file__).resolve().parent / "assets" / "medals"
    assert all((assets / item["image"]).is_file() for item in MEDALS)
    class_medals = [item for item in MEDALS if item["effects"].get("class_skill_bonus")]
    assert class_medals
    assert all(item["effect"] == "постоянно дает +1 куб к проверкам классового навыка." for item in class_medals)

    capped = medal_bonus_summary(["iron_crown_grand", "blood_merit", "wound_gold", "grand_cross"])
    assert capped["will_max_bonus"] == 2
    capped = medal_bonus_summary(["krystov", "long_service", "memory_1930"])
    assert capped["infection_max_bonus"] == 2

    character = {
        "race": "Крысы",
        "class_name": "Снабженец",
        "skills": {"Снабжение": 1, "Стрельба": 1},
        "attributes": {name: {"current": 3, "max": 3} for name in ATTRIBUTES},
        "talents": {},
        "injuries": [],
        "active_vehicles": [],
        "luck_percent": 0,
        "medals": ["golden_sky", "imperial_red_cross", "blue_legion"],
    }
    pool = make_pool(character, "Снабжение")
    assert len(pool.skill_dice) == 3  # base 1 + capped medal bonus 2
    assert sum(value for label, value in pool.skill_modifier_details if label.startswith("Награда «")) == 2


async def test_database_limits() -> None:
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
        db = Database(Path(folder) / "medals.sqlite3")
        async with db.connect() as connection:
            await connection.executescript(SCHEMA)
            cursor = await connection.execute(
                """INSERT INTO characters(
                       guild_id,user_id,surname,name,class_name,race,will_current,will_max,infection
                   ) VALUES(1,2,'Тест','Боец','Снабженец','Крысы',12,10,6)"""
            )
            character_id = cursor.lastrowid
            for attribute in ATTRIBUTES:
                await connection.execute(
                    "INSERT INTO attributes VALUES(?,?,3,3)",
                    (character_id, attribute),
                )
            await connection.execute("INSERT INTO skills VALUES(?,?,1)", (character_id, "Снабжение"))
            for item in MEDALS:
                await connection.execute(
                    "INSERT INTO medal_catalog(code,name,image,description,effect) VALUES(?,?,?,?,?)",
                    (item["code"], item["name"], item["image"], item["description"], item["effect"]),
                )
            for code in ("iron_crown_grand", "blood_merit", "krystov", "long_service"):
                await connection.execute(
                    "INSERT INTO character_medals(character_id,medal_code,reason,awarded_by) VALUES(?,?,?,1)",
                    (character_id, code, "Тест"),
                )
            await connection.commit()

        character = await db.character(1, 2)
        assert character is not None
        assert character["will_max"] == 12
        assert character["infection_max"] == 7
        assert await db.adjust_infection(character_id, 10) == (6, 7)

        assert await db.revoke_medal(character_id, "blood_merit")
        assert await db.revoke_medal(character_id, "long_service")
        character = await db.character(1, 2)
        assert character is not None
        assert character["will_max"] == 11
        assert character["will_current"] == 11
        assert character["infection_max"] == 6
        assert character["infection"] == 6


async def test_medal_name_swap_migration() -> None:
    class MigrationDatabase(Database):
        async def reload_base_catalog(self) -> int:
            return 0

        async def migrate_ammo_packages(self) -> int:
            return 0

        async def repair_ammo_package_contents(self) -> int:
            return 0

        async def merge_stackable_inventory(self) -> int:
            return 0

        async def split_nonstackable_inventory(self) -> int:
            return 0

    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
        db = MigrationDatabase(Path(folder) / "migration.sqlite3")
        async with db.connect() as connection:
            await connection.executescript(SCHEMA)
            await connection.execute(
                "INSERT INTO medal_catalog(code,name,image,description,effect) VALUES(?,?,?,?,?)",
                ("memory_1931", "Памятная медаль 1931 года", "old-1.png", "", ""),
            )
            await connection.execute(
                "INSERT INTO medal_catalog(code,name,image,description,effect) VALUES(?,?,?,?,?)",
                ("memory_1930", "Памятная медаль 1930 года", "old-2.png", "", ""),
            )
            await connection.commit()
        await db.initialize()
        async with db.connect() as connection:
            rows = await connection.execute_fetchall(
                "SELECT code,name FROM medal_catalog WHERE code IN ('memory_1930','memory_1931') ORDER BY code"
            )
        assert {row["code"]: row["name"] for row in rows} == {
            "memory_1930": "Памятная медаль 1931 года",
            "memory_1931": "Памятная медаль 1930 года",
        }


if __name__ == "__main__":
    test_catalog_and_roll_bonuses()
    asyncio.run(test_database_limits())
    asyncio.run(test_medal_name_swap_migration())
    print("medal bonus tests: OK")
