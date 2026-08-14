import asyncio
import tempfile
from pathlib import Path

from database import Database
from trauma_data import PHYSICAL_TRAUMAS


async def main():
    with tempfile.TemporaryDirectory() as temp:
        db = Database(Path(temp) / "test.sqlite3")
        await db.initialize()
        character_id = await db.create_character(1, 9001, "Тест", "Увечий", "Окопник", "Крысы")
        await db.add_injury(character_id, "Телосложение", PHYSICAL_TRAUMAS[71])
        character = await db.character(1, 9001)
        injury = character["injuries"][0]
        assert injury["roll_code"] == 71
        assert injury["expires_at"] is None
        assert injury["impairment_key"] == "lost_left_arm"
        assert injury["compensation_position"] == "Левая рука"


if __name__ == "__main__":
    asyncio.run(main())
