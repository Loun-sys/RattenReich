from __future__ import annotations

import json
import ipaddress
import logging
import os
import re
import secrets
from datetime import UTC, datetime
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from attachment_data import ATTACHMENT_BY_NAME, compatible
from card_renderer import CardRenderer
from constants import ATTRIBUTES, CLASSES, ITEM_CATEGORIES, ITEM_SIZES, RACES, RANGES, RANKS, SKILLS
from database import Database
from trauma_data import MENTAL_TRAUMAS, PHYSICAL_TRAUMAS, SOCIAL_TRAUMAS
from talent_data import CLASS_TALENTS, TALENTS, TALENT_BY_NAME

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
DATA_ROOT = Path(os.getenv("RATTEN_DATA_DIR", str(ROOT / "data"))).expanduser().resolve()
PHOTOS_ROOT = DATA_ROOT / "photos"


def normalize(value: str, options) -> str | None:
    return next((option for option in options if option.casefold() == value.strip().casefold()), None)


def parse_modifiers(raw: str | None, allowed: tuple[str, ...]) -> dict[str, int]:
    if not raw:
        return {}
    result: dict[str, int] = {}
    for chunk in raw.split(","):
        if ":" not in chunk:
            raise ValueError("Модификаторы указываются как Название:+1, Другое:-2")
        name, value = (part.strip() for part in chunk.split(":", 1))
        canonical = normalize(name, allowed)
        if not canonical:
            raise ValueError(f"Неизвестный параметр: {name}")
        result[canonical] = int(value)
    return result


def short(text: str, limit: int = 1024) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class RattenBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!rr ", intents=intents)
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        self.db = Database(DATA_ROOT / "rattenreich.sqlite3")
        self.renderer = CardRenderer(ROOT / "assets")

    async def setup_hook(self):
        await self.db.initialize()
        self.trauma_cleanup.start()
        self.add_view(CharacterPanel(self))
        configured_ids = {
            value.strip()
            for value in (
                os.getenv("DISCORD_GUILD_IDS", "")
                + ","
                + os.getenv("DISCORD_GUILD_ID", "")
            ).split(",")
            if value.strip()
        }
        configured_ids.update({"1529631776602062978", "980168851473985596"})
        if configured_ids:
            for guild_id in sorted(configured_ids):
                guild = discord.Object(id=int(guild_id))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                logging.info("Команды синхронизированы с сервером %s", guild_id)
        else:
            await self.tree.sync()
            logging.info("Глобальные команды синхронизированы")

    @tasks.loop(minutes=1)
    async def trauma_cleanup(self):
        removed = await self.db.cleanup_expired_injuries()
        if removed:
            logging.info("Удалено истёкших травм: %s", removed)

    @trauma_cleanup.before_loop
    async def before_trauma_cleanup(self):
        await self.wait_until_ready()


bot = RattenBot()
PENDING_ATTACKS: dict[tuple[int, int], "AttackView"] = {}
MASTER_ROLE_IDS = frozenset({
    980168851658506269,
    980168851683696660,
})


class MasterAccessRequired(app_commands.CheckFailure):
    """Пользователь не является управляющим сервером и не имеет мастерской роли."""


def has_master_access(interaction: discord.Interaction) -> bool:
    user = interaction.user
    if not isinstance(user, discord.Member):
        return False
    if user.guild_permissions.manage_guild or user.guild_permissions.administrator:
        return True
    return any(role.id in MASTER_ROLE_IDS for role in user.roles)


async def require_master_access(interaction: discord.Interaction) -> bool:
    if has_master_access(interaction):
        return True
    raise MasterAccessRequired


MASTER_ACCESS_ERROR = (
    "Для этой команды требуется право управления сервером "
    "или одна из разрешённых мастерских ролей."
)


async def get_character(interaction: discord.Interaction) -> dict | None:
    if not interaction.guild_id:
        await interaction.response.send_message("Бот работает только на сервере.", ephemeral=True)
        return None
    character = await bot.db.character(interaction.guild_id, interaction.user.id)
    if not character:
        await interaction.response.send_message("Сначала зарегистрируйте персонажа командой `/регистрация`.", ephemeral=True)
    return character


def profile_embed(character: dict) -> discord.Embed:
    rank = RANKS[character["rank_index"]]
    embed = discord.Embed(title=f'Личное дело: {character["surname"]} {character["name"]}', color=0x9B6A2F)
    embed.description = f'**{rank}** · {character["race"]} · {character["class_name"]}'
    stats = "\n".join(f'**{name}:** {value["current"]}/{value["max"]}' for name, value in character["attributes"].items())
    embed.add_field(name="Характеристики", value=stats, inline=True)
    embed.add_field(name="Состояние", value=f'**Воля:** {character["will_current"]}/{character["will_max"]}\n**Заражение:** {character["infection"]}/5\n**Бланки:** {character["supply_forms"]}', inline=True)
    embed.set_footer(text="Ratten Reich · полевой архив")
    return embed


def injuries_embed(character: dict, injuries: list[dict] | None = None) -> discord.Embed:
    injuries = character.get("injuries", []) if injuries is None else injuries
    lines = []
    for injury in injuries:
        expiry = f' · до {injury["expires_at"]} UTC' if injury.get("expires_at") else ""
        lines.append(
            f'**ID {injury["id"]} · №{injury["roll_code"]} {injury["name"]}** '
            f'({injury["attribute_name"]})\n'
            f'{injury["description"]}\n'
            f'Штрафы: {injury["penalties"]} · {injury["duration"]}{expiry}'
        )
    embed = discord.Embed(
        title=f'Травмы · {character["surname"]} {character["name"]}',
        description=short("\n────────────\n".join(lines) or "Активных травм нет.", 4000),
        color=0x7A342E,
    )
    embed.set_footer(text=f'Активных травм: {len(injuries)}')
    return embed


async def apply_damage(character: dict, attribute: str, amount: int) -> str:
    before, after = await bot.db.damage(character["id"], attribute, amount)
    message = f'**{attribute}:** {before} → {after}'
    if before > 0 and after == 0:
        first, second = secrets.randbelow(6) + 1, secrets.randbelow(6) + 1
        code = first * 10 + second
        if attribute in ("Телосложение", "Ловкость"):
            trauma = PHYSICAL_TRAUMAS[code]
            await bot.db.add_injury(character["id"], attribute, trauma)
            message += f'\nПолучена травма **№{code}: {trauma.name}**\n{trauma.description}\nСрок действия: {trauma.duration}. Заражение увеличено на 1.'
        else:
            pool = MENTAL_TRAUMAS if attribute == "Смекалка" else SOCIAL_TRAUMAS
            if code in pool:
                trauma = pool[code]
                await bot.db.add_injury(character["id"], attribute, trauma)
                will_before = character["will_current"]
                guard = await bot.db.consume_will_guard(character["id"])
                will_after = max(-10, will_before - max(0, 1 - guard))
                await bot.db.update_character(character["id"], "will_current", will_after)
                message += (
                    f'\nПолучена психологическая травма **№{code}: {trauma.name}**'
                    f'\n{trauma.description}'
                    f'\nШтрафы: {trauma.penalties}.'
                    f'\nСрок действия: {trauma.duration}.'
                    f'\n**Воля:** {will_before} → {will_after}'
                )
            else:
                await bot.db.add_pending_injury(character["id"], attribute, code)
                message += f'\nПолучена психологическая травма **№{code}**. Результат сохранён для мастера.'
    return message


SKILL_ATTRIBUTES = {
    "Выносливость": "Телосложение", "Сила": "Телосложение", "Драка": "Телосложение",
    "Скрытность": "Ловкость", "Проворство": "Ловкость", "Стрельба": "Ловкость",
    "Наблюдательность": "Смекалка", "Анализ": "Смекалка", "Знания": "Смекалка",
    "Проницательность": "Эмпатия", "Влияние": "Эмпатия", "Воодушевление": "Эмпатия",
    "Снабжение": "Смекалка", "Лечение": "Эмпатия", "Обращение": "Ловкость", "Защита": "Телосложение",
}


def racial_skill_bonus(character: dict, skill: str) -> int:
    bonuses = {
        ("Агамы", "Стрельба"): 1,
        ("Вараны", "Драка"): 1,
        ("Псовые", "Наблюдательность"): 1,
        ("Парнокопытные", "Воодушевление"): 1,
        ("Непарнокопытные", "Выносливость"): 1,
        ("Рукокрылые", "Проворство"): 1,
        ("Однопроходные", "Скрытность"): 1,
    }
    bonus = bonuses.get((character["race"], skill), 0)
    if character["race"] == "Сумчатые" and skill == CLASSES.get(character["class_name"]):
        bonus += 1
    return bonus


def has_talent(character: dict, name: str) -> bool:
    return name in character.get("talents", {})


def talent_skill_bonus_details(character: dict, skill: str) -> list[tuple[str, int]]:
    details = []
    for name in character.get("talents", {}):
        talent = TALENT_BY_NAME.get(name.casefold())
        value = (talent or {}).get("effects", {}).get("skill_bonus", {}).get(skill, 0)
        if value:
            details.append((f'\u0422\u0430\u043b\u0430\u043d\u0442 \u00ab{name}\u00bb', int(value)))
    return details


def character_skill_cap(character: dict, skill: str) -> int:
    caps = talent_effect(character, "skill_cap", {}) or {}
    return max(5, int(caps.get(skill, 5)))


def starting_skill_budget(race: str) -> int:
    return 12 if race == "\u041c\u044b\u0448\u0438" else 8 if race == "\u0422\u0430\u0440\u0430\u043a\u0430\u043d\u044b" else 10



def starting_skills_ready(character: dict) -> bool:
    return bool(int(character.get("skills_initialized", 1)))


async def reject_unfinished_skills(interaction: discord.Interaction, character: dict) -> bool:
    if starting_skills_ready(character):
        return False
    await interaction.response.send_message(
        "Сначала распределите стартовые очки навыков и зафиксируйте их командой `/навыки-завершить`.",
        ephemeral=True,
    )
    return True


def talent_effect(character: dict, key: str, default=None):
    values = []
    for name in character.get("talents", {}):
        talent = TALENT_BY_NAME.get(name.casefold())
        if talent and key in talent.get("effects", {}):
            values.append(talent["effects"][key])
    if not values:
        return default
    if all(isinstance(value, dict) for value in values):
        merged = {}
        for value in values:
            merged.update(value)
        return merged
    if all(isinstance(value, bool) for value in values):
        return any(values)
    if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values):
        return sum(values)
    return values[-1]


INJURY_SKILL_STEMS = {
    "Выносливость": "вынослив",
    "Сила": "сил",
    "Драка": "драк",
    "Скрытность": "скрытност",
    "Проворство": "проворств",
    "Стрельба": "стрельб",
    "Наблюдательность": "наблюдательност",
    "Анализ": "анализ",
    "Знания": "знани",
    "Проницательность": "проницательност",
    "Влияние": "влияни",
    "Воодушевление": "воодушевлен",
    "Снабжение": "снабжен",
    "Лечение": "лечен",
    "Обращение": "обращен",
    "Защита": "защит",
}


def normalized_injury_text(injury: dict) -> str:
    return " ".join(
        str(injury.get(key) or "") for key in ("description", "penalties")
    ).casefold().replace("−", "-").replace("–", "-")


def normalized_injury_penalties(injury: dict) -> str:
    return str(injury.get("penalties") or "").casefold().replace("−", "-").replace("–", "-")


def injury_skill_modifier_details(character: dict, skill: str) -> list[tuple[str, int]]:
    stem = INJURY_SKILL_STEMS.get(skill, skill.casefold())
    details = []
    for injury in character.get("injuries", []):
        text = normalized_injury_penalties(injury)
        modifier = 0
        all_match = re.search(r"-(\d+)\s+ко всем навыкам", text)
        if all_match:
            modifier -= int(all_match.group(1))
        for match in re.finditer(r"([+-]\d+)\s+к\s+([^;]+)", text):
            targets = match.group(2)
            if "всем навыкам" not in targets and stem in targets:
                modifier += int(match.group(1))
        if modifier:
            details.append((f'Травма «{injury["name"]}»', modifier))
    return details


def injury_blocks_skill(character: dict, skill: str) -> str | None:
    stem = INJURY_SKILL_STEMS.get(skill, skill.casefold())
    for injury in character.get("injuries", []):
        text = normalized_injury_text(injury)
        if "нельзя совершать проверки навыков" in text:
            return injury["name"]
        match = re.search(r"нельзя использовать ([^;]+)", text)
        if match and "совместно" not in match.group(1) and stem in match.group(1):
            return injury["name"]
    return None


def injury_blocks_two_handed(character: dict) -> str | None:
    for injury in character.get("injuries", []):
        text = normalized_injury_text(injury)
        if "нельзя" in text and "двуручн" in text:
            return injury["name"]
    return None


def injury_attribute_damage(character: dict, skill: str) -> int:
    damage = 0
    for injury in character.get("injuries", []):
        text = normalized_injury_text(injury)
        match = re.search(r"-(\d+)\s+телосложен", text)
        if not match:
            continue
        affects_all = "использован" in text and "любого навыка" in text
        affects_physical = "за проверку" in text and skill in {"Сила", "Проворство", "Драка"}
        if affects_all or affects_physical:
            damage += int(match.group(1))
    return damage


async def apply_injury_roll_damage(character: dict, skill: str) -> str:
    amount = injury_attribute_damage(character, skill)
    if amount <= 0:
        return ""
    return await apply_damage(character, "Телосложение", amount)


def equipment_skill_modifier(items: list[dict], skill: str) -> int:
    total = 0
    pattern = re.compile(rf"([+−-]\d+)\s+к\s+{re.escape(skill)}", re.IGNORECASE)
    attribute = SKILL_ATTRIBUTES.get(skill, "")
    for item in items:
        try:
            structured_skills = json.loads(item.get("skill_modifiers") or "{}")
            structured_attributes = json.loads(item.get("attribute_modifiers") or "{}")
        except (TypeError, json.JSONDecodeError):
            structured_skills, structured_attributes = {}, {}
        total += int(structured_skills.get(skill, 0))
        total += int(structured_attributes.get(attribute, 0))
        text = item.get("conditions") or ""
        for value in pattern.findall(text):
            total += int(value.replace("−", "-"))
    return total


def equipment_success_modifier(items: list[dict], skill: str) -> int:
    attribute = SKILL_ATTRIBUTES.get(skill, "")
    attribute_stem = {
        "Телосложение": "телосложен",
        "Ловкость": "ловкост",
        "Смекалка": "смекалк",
        "Эмпатия": "эмпати",
    }.get(attribute, attribute.casefold())
    skill_stems = {
        "Выносливость": "вынослив",
        "Сила": "сил",
        "Драка": "драк",
        "Скрытность": "скрытност",
        "Проворство": "проворств",
        "Стрельба": "стрельб",
        "Наблюдательность": "наблюдательност",
        "Анализ": "анализ",
        "Знания": "знани",
        "Проницательность": "проницательност",
        "Влияние": "влияни",
        "Воодушевление": "воодушевлен",
        "Снабжение": "снабжен",
        "Лечение": "лечен",
        "Обращение": "обращен",
        "Защита": "защит",
    }
    total = 0
    for item in items:
        for sentence in re.split(r"[.;]", item.get("conditions") or ""):
            if attribute and attribute_stem not in sentence.casefold():
                continue
            difficulty = re.search(
                r"на\s+(\d+)\s+успех\w*\s+(больше|меньше)",
                sentence,
                re.IGNORECASE,
            )
            if difficulty:
                amount = int(difficulty.group(1))
                total += -amount if difficulty.group(2).casefold() == "больше" else amount
            direct = re.search(r"([+−-]\d+)\s+успех", sentence, re.IGNORECASE)
            if direct and skill_stems.get(skill, "") in sentence.casefold():
                total += int(direct.group(1).replace("−", "-"))
    return total


def talent_equipment_success_modifier(character: dict, items: list[dict], skill: str) -> int:
    if not has_talent(character, "Подвижная крепость"):
        return 0
    if SKILL_ATTRIBUTES.get(skill) != "Ловкость":
        return 0
    for item in items:
        if (
            item.get("equipped")
            and item.get("category") == "Броня"
            and item.get("size") == "Большой"
            and re.search(r"Ловкост\w*\s+требу\w*\s+на\s+\d+\s+успех\w*\s+больше", item.get("conditions") or "", re.IGNORECASE)
        ):
            return 1
    return 0


def active_success_modifier(effects: list[dict], attribute: str) -> int:
    return sum(
        int(effect["modifier"])
        for effect in effects
        if effect.get("attribute_name") == attribute
    )


def physical_armor_reduction(items: list[dict], damage_type: str) -> int:
    damage_text = (damage_type or "").casefold()
    markers = {
        "дробящ": "дробящ",
        "колющ": "колющ",
        "режущ": "режущ",
        "взрыв": "взрыв",
        "огнен": "огн",
    }
    active_marker = next((marker for source, marker in markers.items() if source in damage_text), "")
    if not active_marker:
        return 0
    total = 0
    for item in items:
        if not item.get("equipped") or int(item.get("durability") or 0) <= 0:
            continue
        for sentence in re.split(r"[.;]", item.get("conditions") or ""):
            lowered = sentence.casefold()
            if "снижа" not in lowered or active_marker not in lowered:
                continue
            match = re.search(r"(?:на\s+)(\d+)", lowered)
            if match:
                total += int(match.group(1))
    return total


def is_general_roll_gear(item: dict) -> bool:
    return (
        item.get("category") not in {"Оружие ближнего боя", "Оружие дальнего боя", "Броня"}
        and "расходник" not in str(item.get("properties") or "").casefold()
    )


def d6(count: int) -> list[int]:
    return [secrets.randbelow(6) + 1 for _ in range(max(0, count))]


@dataclass
class RollPool:
    attribute: str
    skill: str
    attribute_dice: list[int]
    skill_dice: list[int]
    negative_dice: list[int]
    gear_dice: dict[int, list[int]] = field(default_factory=dict)
    push_count: int = 0
    charged_attribute_ones: int = 0
    charged_gear_ones: dict[int, int] = field(default_factory=dict)
    flat_success_modifier: int = 0
    minimum_successes: int = 0
    skill_modifier_details: list[tuple[str, int]] = field(default_factory=list)
    success_modifier_details: list[tuple[str, int]] = field(default_factory=list)

    @property
    def successes(self) -> int:
        positive = sum(value == 6 for value in self.attribute_dice + self.skill_dice)
        positive += sum(value == 6 for values in self.gear_dice.values() for value in values)
        result = positive - sum(value == 6 for value in self.negative_dice) + self.flat_success_modifier
        return max(self.minimum_successes, result) if self.minimum_successes > 0 else result

    def push(self) -> None:
        reroll_positive = lambda values: [value if value in (1, 6) else d6(1)[0] for value in values]
        self.attribute_dice = reroll_positive(self.attribute_dice)
        self.skill_dice = reroll_positive(self.skill_dice)
        self.gear_dice = {item_id: reroll_positive(values) for item_id, values in self.gear_dice.items()}
        self.negative_dice = [value if value == 6 else d6(1)[0] for value in self.negative_dice]
        self.push_count += 1


def make_pool(
    character: dict,
    skill: str,
    custom_modifier: int = 0,
    gear: dict[int, int] | None = None,
    success_modifier: int = 0,
    attribute_override: str | None = None,
    use_max_attribute: bool = False,
) -> RollPool:
    attribute_map = talent_effect(character, "skill_attribute", {}) or {}
    attribute = attribute_override or attribute_map.get(skill) or SKILL_ATTRIBUTES[skill]
    permanent_skill = int(character["skills"].get(skill, -3))
    race_bonus = racial_skill_bonus(character, skill)
    talent_details = talent_skill_bonus_details(character, skill)
    talent_bonus = sum(value for _, value in talent_details)
    injury_details = injury_skill_modifier_details(character, skill)
    injury_modifier = sum(value for _, value in injury_details)
    skill_total = permanent_skill + race_bonus + talent_bonus + injury_modifier + custom_modifier
    guaranteed = max(0, permanent_skill - 5) if skill in {"\u041b\u0435\u0447\u0435\u043d\u0438\u0435", "\u041e\u0431\u0440\u0430\u0449\u0435\u043d\u0438\u0435", "\u0417\u0430\u0449\u0438\u0442\u0430"} else 0
    modifier_details = []
    if race_bonus:
        modifier_details.append((f'\u0420\u0430\u0441\u0430 \u00ab{character["race"]}\u00bb', race_bonus))
    modifier_details.extend(talent_details)
    modifier_details.extend(injury_details)
    if custom_modifier:
        modifier_details.append(("\u041f\u0440\u043e\u0447\u0438\u0435 \u043c\u043e\u0434\u0438\u0444\u0438\u043a\u0430\u0442\u043e\u0440\u044b", custom_modifier))
    maximum_rules = talent_effect(character, "max_attribute_for", {}) or {}
    attribute_value = character["attributes"][attribute]["max" if use_max_attribute or maximum_rules.get(skill) == attribute else "current"]
    minimum_rules = talent_effect(character, "minimum_success", {}) or {}
    return RollPool(
        attribute=attribute,
        skill=skill,
        attribute_dice=d6(int(attribute_value)),
        skill_dice=d6(max(0, skill_total)),
        negative_dice=d6(max(0, -skill_total)),
        gear_dice={item_id: d6(count) for item_id, count in (gear or {}).items()},
        flat_success_modifier=success_modifier + guaranteed,
        minimum_successes=int(minimum_rules.get(skill, 0)),
        skill_modifier_details=modifier_details,
        success_modifier_details=([("\u041f\u043e\u0441\u0442\u043e\u044f\u043d\u043d\u044b\u0439 \u043d\u0430\u0432\u044b\u043a \u0432\u044b\u0448\u0435 5", guaranteed)] if guaranteed else []),
    )


def dice_text(values: list[int]) -> str:
    return " ".join(f"`{value}`" for value in values) or "—"


DIE_FACES = {1: "⚀", 2: "⚁", 3: "⚂", 4: "⚃", 5: "⚄", 6: "⚅"}
DIE_COLORS = {"attribute": "🟨", "skill": "🟩", "negative": "🟥", "gear": "⬛"}
DIE_EMOJI_NAMES = {
    "attribute": "yellow",
    "skill": "green",
    "negative": "red",
    "gear": "black",
}


def dice_emoji(color: str, value: int) -> discord.Emoji | None:
    guild_id = os.getenv("DICE_EMOJI_GUILD_ID")
    if guild_id and guild_id.isdigit():
        guild = bot.get_guild(int(guild_id))
        emojis = guild.emojis if guild else ()
    else:
        emojis = bot.emojis
    name = f'rr_{DIE_EMOJI_NAMES[color]}_{value}'
    return discord.utils.get(emojis, name=name)


def colored_dice(values: list[int], color: str) -> str:
    rendered = []
    for value in values:
        emoji = dice_emoji(color, value)
        # Число остаётся видимым даже без доступа к техническому серверу или
        # при отсутствии одного из пользовательских эмодзи.
        rendered.append(str(emoji) if emoji else f'{DIE_COLORS[color]}`{value}`')
    return " ".join(rendered) or "—"


AMMO_CODES = {
    "пистолетные": "П",
    "винтовочные": "В",
    "дробовые": "Д",
    "сигнальные ракеты": "СР",
    "гарпуны": "Г",
    "иглы": "И",
    "огнесмесь": "О",
}
AMMO_ITEM_NAMES = {
    "П": "Пистолетные боеприпасы",
    "В": "Винтовочные боеприпасы",
    "Д": "Дробовые боеприпасы",
    "СР": "Сигнальные ракеты",
    "Г": "Гарпуны с тросом",
    "И": "Пачка закалённых игл",
    "О": "Баллон огнесмеси",
}


def ammo_code(conditions: str) -> str:
    match = re.search(r"Боеприпас:\s*([^.;]+)", conditions or "", re.IGNORECASE)
    if not match:
        return ""
    ammo = match.group(1).strip()
    folded = ammo.casefold()
    return next((code for name, code in AMMO_CODES.items() if name in folded), ammo[:3].upper())


def compact_conditions(conditions: str) -> str:
    result = re.sub(r"(?:^|[.;]\s*)Боеприпас:\s*[^.;]+[.;]?", "", conditions or "", flags=re.IGNORECASE)
    return result.strip(" .;") or "особых условий нет"


def pool_embed(pool: RollPool, title: str, conditions: str = "") -> discord.Embed:
    embed = discord.Embed(title=title, color=0x6E654F)
    embed.add_field(name=f"{pool.attribute}", value=colored_dice(pool.attribute_dice, "attribute"), inline=False)
    embed.add_field(name=f"{pool.skill}", value=colored_dice(pool.skill_dice, "skill"), inline=False)
    if pool.negative_dice:
        embed.add_field(name="Отрицательные кубы", value=colored_dice(pool.negative_dice, "negative"), inline=False)
    if pool.gear_dice:
        embed.add_field(
            name="Снаряжение",
            value="\n".join(colored_dice(values, "gear") for values in pool.gear_dice.values()),
            inline=False,
        )
    modifier_lines = [
        f'{name}: **{value:+d}**' for name, value in pool.skill_modifier_details if value
    ]
    modifier_lines.extend(
        f'{name}: **{value:+d} успеха**' for name, value in pool.success_modifier_details if value
    )
    if modifier_lines:
        embed.add_field(name="Модификаторы", value="\n".join(modifier_lines), inline=False)
    modifier = (
        f" · модификатор успехов: **{pool.flat_success_modifier:+d}**"
        if pool.flat_success_modifier else ""
    )
    embed.add_field(
        name="Итог",
        value=f"Успехов: **{pool.successes}**{modifier} · пушей: **{pool.push_count}**",
        inline=False,
    )
    if conditions:
        embed.add_field(name="Условия использования", value=short(conditions), inline=False)
    return embed


class RegistrationModal(discord.ui.Modal, title="Регистрация личного дела"):
    full_name = discord.ui.TextInput(label="ФИО", max_length=80, placeholder="Шрам Конрад", required=True)

    def __init__(self, class_name: str, race: str, talent_name: str):
        super().__init__()
        self.class_name = class_name
        self.race = race
        self.talent_name = talent_name

    async def on_submit(self, interaction: discord.Interaction):
        parts = self.full_name.value.strip().split(maxsplit=1)
        if len(parts) < 2:
            await interaction.response.send_message("Укажите как минимум фамилию и имя через пробел, например: `Шрам Конрад`.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        surname, name = parts
        character_id = await bot.db.create_character(
            interaction.guild_id, interaction.user.id, surname, name, self.class_name, self.race
        )
        talent = TALENT_BY_NAME[self.talent_name.casefold()]
        await bot.db.grant_talent(character_id, talent["name"], talent["description"])
        character = await bot.db.character(interaction.guild_id, interaction.user.id)
        await interaction.followup.send(
            "Личное дело зарегистрировано. Теперь распределите характеристики и навыки.",
            embed=profile_embed(character),
            view=CharacterPanel(bot, interaction.user.id),
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logging.exception("Ошибка регистрации", exc_info=error)
        message = "Не удалось зарегистрировать персонажа. Проверьте данные и попробуйте ещё раз."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class RegistrationClassSelect(discord.ui.Select):
    def __init__(self, flow: "RegistrationFlow"):
        self.flow = flow
        super().__init__(
            placeholder="1. Выберите класс",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=name,
                    description=f"Классовый навык: {skill}",
                    default=name == flow.class_name,
                )
                for name, skill in CLASSES.items()
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        self.flow.class_name = self.values[0]
        self.flow.talent_name = None
        self.flow.rebuild()
        await interaction.response.edit_message(view=self.flow)


class RegistrationRaceSelect(discord.ui.Select):
    def __init__(self, flow: "RegistrationFlow"):
        self.flow = flow
        super().__init__(
            placeholder="2. Выберите расу",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=name, default=name == flow.race)
                for name in RACES
            ],
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        self.flow.race = self.values[0]
        await interaction.response.defer()


class RegistrationTalentSelect(discord.ui.Select):
    def __init__(self, flow: "RegistrationFlow"):
        self.flow = flow
        talents = CLASS_TALENTS.get(flow.class_name or "", ())
        if not talents:
            super().__init__(
                placeholder="3. Сначала выберите класс",
                min_values=1,
                max_values=1,
                options=[discord.SelectOption(label="Сначала выберите класс", value="__locked__")],
                disabled=True,
                row=2,
            )
            return
        super().__init__(
            placeholder="3. Выберите стартовый талант",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=talent["name"],
                    description=talent["description"][:100],
                    default=talent["name"] == flow.talent_name,
                )
                for talent in talents
            ],
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        self.flow.talent_name = self.values[0]
        await interaction.response.defer()


class RegistrationFlow(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.class_name: str | None = None
        self.race: str | None = None
        self.talent_name: str | None = None
        self.rebuild()

    def rebuild(self):
        self.clear_items()
        self.add_item(RegistrationClassSelect(self))
        self.add_item(RegistrationRaceSelect(self))
        self.add_item(RegistrationTalentSelect(self))
        self.add_item(self.continue_registration)

    @discord.ui.button(label="Продолжить регистрацию", emoji="📁", style=discord.ButtonStyle.primary, row=3)
    async def continue_registration(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self.class_name or not self.race or not self.talent_name:
            await interaction.response.send_message(
                "Сначала выберите класс, расу и стартовый талант.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            RegistrationModal(self.class_name, self.race, self.talent_name)
        )


class StatsModal(discord.ui.Modal, title="Распределение характеристик"):
    physique = discord.ui.TextInput(label="Телосложение (1–5)", default="1", max_length=1)
    agility = discord.ui.TextInput(label="Ловкость (1–5)", default="1", max_length=1)
    wits = discord.ui.TextInput(label="Смекалка (1–5)", default="1", max_length=1)
    empathy = discord.ui.TextInput(label="Эмпатия (1–5)", default="1", max_length=1)

    def __init__(self, character: dict):
        super().__init__()
        for field, name in zip((self.physique, self.agility, self.wits, self.empathy), ATTRIBUTES):
            field.default = str(character["attributes"][name]["max"])
        self.character_id = character["id"]

    async def on_submit(self, interaction: discord.Interaction):
        try:
            values = dict(zip(ATTRIBUTES, map(int, (self.physique.value, self.agility.value, self.wits.value, self.empathy.value))))
        except ValueError:
            await interaction.response.send_message("Все значения должны быть целыми числами.", ephemeral=True)
            return
        if any(value < 1 or value > 5 for value in values.values()):
            await interaction.response.send_message("Каждая характеристика должна быть от 1 до 5.", ephemeral=True)
            return
        await bot.db.set_attributes(self.character_id, values)
        await interaction.response.send_message("Характеристики сохранены.", ephemeral=True)


class IdentityModal(discord.ui.Modal, title="Изменение ФИО"):
    full_name = discord.ui.TextInput(label="Фамилия и имя", max_length=80, placeholder="Шрам Конрад")

    def __init__(self, character: dict):
        super().__init__()
        self.character_id = character["id"]
        self.full_name.default = f'{character["surname"]} {character["name"]}'

    async def on_submit(self, interaction: discord.Interaction):
        parts = self.full_name.value.strip().split(maxsplit=1)
        if len(parts) < 2:
            await interaction.response.send_message(
                "Укажите фамилию и имя через пробел, например: `Шрам Конрад`.",
                ephemeral=True,
            )
            return
        surname, name = parts
        await bot.db.update_identity(self.character_id, surname, name)
        await interaction.response.send_message(
            "ФИО изменено. Нажмите «Личное дело», чтобы получить обновлённый бланк.",
            ephemeral=True,
        )


class HandsModal(discord.ui.Modal, title="Количество рук"):
    hands = discord.ui.TextInput(label="Руки (от 1 до 4)", max_length=1)

    def __init__(self, character: dict):
        super().__init__()
        self.character_id = character["id"]
        self.hands.default = str(character.get("hands", 2))

    async def on_submit(self, interaction: discord.Interaction):
        try:
            value = int(self.hands.value)
        except ValueError:
            value = 0
        if value < 1 or value > 4:
            await interaction.response.send_message("Количество рук должно быть от 1 до 4.", ephemeral=True)
            return
        equipped = [
            item for item in await bot.db.inventory(self.character_id)
            if item["equipped"] and item["category"] != "Броня"
        ]
        occupied = sum(int(item["hands"] or 0) for item in equipped)
        if occupied > value:
            await interaction.response.send_message(
                f"Сначала снимите часть предметов: сейчас занято рук — {occupied}.",
                ephemeral=True,
            )
            return
        await bot.db.update_character(self.character_id, "hands", value)
        await interaction.response.send_message(f"Количество рук: **{value}**.", ephemeral=True)


class NotesModal(discord.ui.Modal, title="Полевые заметки"):
    notes = discord.ui.TextInput(label="Заметки", style=discord.TextStyle.paragraph, required=False, max_length=700)

    def __init__(self, character: dict):
        super().__init__()
        self.character_id = character["id"]
        self.notes.default = character["notes"][:700]

    async def on_submit(self, interaction: discord.Interaction):
        await bot.db.update_character(self.character_id, "notes", self.notes.value)
        await interaction.response.send_message("Заметки сохранены.", ephemeral=True)


SKILL_GROUPS = {
    "Телосложение": ("Выносливость", "Сила", "Драка"),
    "Ловкость": ("Скрытность", "Проворство", "Стрельба"),
    "Смекалка": ("Наблюдательность", "Анализ", "Знания"),
    "Эмпатия": ("Проницательность", "Влияние", "Воодушевление"),
}


class SkillEditModal(discord.ui.Modal, title="Редактирование навыков"):
    def __init__(self, character: dict, group: str):
        super().__init__()
        self.character = character
        self.group = group
        names = (CLASSES[character["class_name"]],) if group == "Классовый навык" else SKILL_GROUPS[group]
        self.fields: dict[str, discord.ui.TextInput] = {}
        for name in names:
            field = discord.ui.TextInput(
                label=name,
                default=str(character["skills"].get(name, -3)),
                placeholder="От −5 до +5",
                max_length=2,
            )
            self.fields[name] = field
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction):
        current = await bot.db.character(interaction.guild_id, interaction.user.id)
        if not current or int(current.get("skills_initialized", 1)):
            await interaction.response.send_message("Стартовое распределение уже завершено; далее навыки покупаются за БС.", ephemeral=True)
            return
        try:
            values = {name: int(field.value) for name, field in self.fields.items()}
        except ValueError:
            await interaction.response.send_message("Значения навыков должны быть целыми числами.", ephemeral=True)
            return
        if any(value < -5 or value > 5 for value in values.values()):
            await interaction.response.send_message("Каждый навык должен быть от −5 до +5.", ephemeral=True)
            return
        for name, value in values.items():
            await bot.db.set_skill(self.character["id"], name, value)
        summary = "\n".join(f"**{name}:** {value:+d}" for name, value in values.items())
        await interaction.response.send_message(f"Навыки сохранены:\n{summary}", ephemeral=True)


class SkillGroupActionsView(discord.ui.View):
    def __init__(self, character: dict, group: str):
        super().__init__(timeout=180)
        self.character = character
        self.group = group
        if int(character.get("skills_initialized", 1)):
            self.remove_item(self.edit)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.character["user_id"]:
            await interaction.response.send_message("Редактировать навыки может только владелец персонажа.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Редактировать", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: discord.Interaction, _: discord.ui.Button):
        current = await bot.db.character(interaction.guild_id, interaction.user.id)
        if current and not int(current.get("skills_initialized", 1)):
            await interaction.response.send_modal(SkillEditModal(current, self.group))
        else:
            await interaction.response.send_message("Стартовое распределение уже завершено; используйте /магазин-навыков.", ephemeral=True)


class SkillCategoriesView(discord.ui.View):
    def __init__(self, character: dict):
        super().__init__(timeout=180)
        self.character = character

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.character["user_id"]:
            await interaction.response.send_message("Это меню навыков другого персонажа.", ephemeral=True)
            return False
        return True

    async def show_group(self, interaction: discord.Interaction, group: str):
        if group == "Классовый навык":
            names = (CLASSES[self.character["class_name"]],)
            title = f'Классовый навык · {self.character["class_name"]}'
        else:
            names = SKILL_GROUPS[group]
            title = f'Навыки · {group}'
        lines = [f'**{name}:** {self.character["skills"].get(name, -3):+d}' for name in names]
        embed = discord.Embed(title=title, description="\n".join(lines), color=0x6E654F)
        await interaction.response.send_message(
            embed=embed,
            view=SkillGroupActionsView(self.character, group),
            ephemeral=True,
        )

    @discord.ui.button(label="Телосложение", style=discord.ButtonStyle.secondary, row=0)
    async def physique(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.show_group(interaction, "Телосложение")

    @discord.ui.button(label="Ловкость", style=discord.ButtonStyle.secondary, row=0)
    async def agility(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.show_group(interaction, "Ловкость")

    @discord.ui.button(label="Смекалка", style=discord.ButtonStyle.secondary, row=0)
    async def wits(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.show_group(interaction, "Смекалка")

    @discord.ui.button(label="Эмпатия", style=discord.ButtonStyle.secondary, row=0)
    async def empathy(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.show_group(interaction, "Эмпатия")

    @discord.ui.button(label="Классовый навык", style=discord.ButtonStyle.primary, row=1)
    async def class_skill(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.show_group(interaction, "Классовый навык")


class PhotoUrlModal(discord.ui.Modal, title="Фотография персонажа"):
    url = discord.ui.TextInput(
        label="Ссылка Discord на фото или сообщение",
        placeholder="Вставьте ссылку на вложение или сообщение с фото",
        max_length=2000,
    )

    def __init__(self, character: dict):
        super().__init__()
        self.character = character

    async def on_submit(self, interaction: discord.Interaction):
        source_url = self.url.value.strip().strip("<>")
        parsed = urlparse(source_url)
        if parsed.scheme != "https" or not parsed.hostname:
            await interaction.response.send_message("Нужна HTTPS-ссылка на изображение или сообщение Discord с изображением.", ephemeral=True)
            return
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if parsed.hostname in {"localhost", "localhost.localdomain"} or (address and (address.is_private or address.is_loopback or address.is_link_local)):
            await interaction.response.send_message("Локальные адреса использовать нельзя.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        path: Path | None = None
        saved = False
        try:
            # Разрешаем вставить обычную ссылку на сообщение Discord: бот сам
            # найдёт в нём первое прикреплённое изображение.
            parts = [part for part in parsed.path.split("/") if part]
            if parsed.hostname.casefold() in {"discord.com", "www.discord.com", "ptb.discord.com", "canary.discord.com"} and len(parts) >= 4 and parts[0] == "channels":
                channel_id, message_id = int(parts[-2]), int(parts[-1])
                channel = interaction.client.get_channel(channel_id) or await interaction.client.fetch_channel(channel_id)
                message = await channel.fetch_message(message_id)
                attachment = next(
                    (
                        item for item in message.attachments
                        if (item.content_type or "").startswith("image/")
                        or item.filename.casefold().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
                    ),
                    None,
                )
                if attachment is None:
                    raise ValueError("в сообщении Discord нет изображения")
                source_url = attachment.url

            timeout = aiohttp.ClientTimeout(total=20)
            headers = {"User-Agent": "RattenReichBot/1.0 (Discord character sheet)"}
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(source_url, allow_redirects=True) as response:
                    if response.status != 200:
                        raise ValueError(f"Discord вернул ошибку HTTP {response.status}")
                    if int(response.headers.get("Content-Length", "0") or 0) > 8 * 1024 * 1024:
                        raise ValueError("Изображение больше 8 МБ")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        if total > 8 * 1024 * 1024:
                            raise ValueError("Изображение больше 8 МБ")
                        chunks.append(chunk)
                    payload = b"".join(chunks)
                    if not payload:
                        raise ValueError("Discord вернул пустой файл")
            photos = PHOTOS_ROOT
            photos.mkdir(parents=True, exist_ok=True)
            path = photos / f'{interaction.guild_id}_{self.character["user_id"]}.png'
            # Discord CDN иногда отдаёт application/octet-stream, поэтому
            # проверяем сами байты через Pillow, а не доверяем Content-Type.
            from PIL import Image, ImageOps
            with Image.open(BytesIO(payload)) as source:
                source.load()
                normalized = ImageOps.exif_transpose(source).convert("RGB")
                normalized.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
                normalized.save(path, "PNG", optimize=True)
            previous = self.character.get("photo_path")
            await bot.db.update_character(self.character["id"], "photo_path", str(path))
            saved = True
            if previous and Path(previous) != path:
                old_path = Path(previous)
                try:
                    if old_path.parent.resolve() == photos.resolve() and old_path.is_file():
                        old_path.unlink()
                except OSError:
                    logging.warning("Не удалось удалить предыдущий портрет: %s", old_path)
        except Exception as error:
            if not saved and path and path.is_file():
                path.unlink()
            logging.warning("Не удалось загрузить фото по URL: %s", error)
            await interaction.followup.send(f"Не удалось загрузить фото: {error}", ephemeral=True)
            return
        await interaction.followup.send("Фотография сохранена. Нажмите «Обновить бланк».", ephemeral=True)


INVENTORY_CATEGORIES = ("Большой", "Малый", "Безделушка")
INVENTORY_PAGE_SIZE = 6


def inventory_slot_capacities(character: dict) -> tuple[int, int]:
    small = int(character["attributes"]["Ловкость"]["max"])
    large = int(character["attributes"]["Телосложение"]["max"])
    small += int(talent_effect(character, "small_slots", 0) or 0)
    large += int(talent_effect(character, "large_slots", 0) or 0)
    return small, large


async def build_inventory_embed(
    character: dict,
    category: str = "Большой",
    page: int = 0,
) -> discord.Embed:
    items = await bot.db.inventory(character["id"])
    small = sum(item["quantity"] for item in items if item["size"] == "Малый")
    large = sum(item["quantity"] for item in items if item["size"] == "Большой")
    small_cap, large_cap = inventory_slot_capacities(character)
    category_items = [item for item in items if item["size"] == category]
    page_count = max(1, (len(category_items) + INVENTORY_PAGE_SIZE - 1) // INVENTORY_PAGE_SIZE)
    page = max(0, min(page, page_count - 1))
    visible_items = category_items[page * INVENTORY_PAGE_SIZE:(page + 1) * INVENTORY_PAGE_SIZE]
    lines = []
    for item in visible_items:
        details = []
        if item["category"] in {"Броня", "Щит"}:
            details.append(f'защита {item["durability"]}/{item["max_durability"]}')
        else:
            details.append(f'{item["durability"]} качества')
        if item["damage"]:
            details.append(f'{item["damage"]} урона')
        if item["defense"] and item["category"] not in {"Броня", "Щит"}:
            details.append(f'{item["defense"]} куб. защиты')
        hand_label = {1: "одноручное", 2: "двуручное"}.get(int(item["hands"] or 0))
        if hand_label:
            details.append(hand_label)
        if item["damage_type"]:
            details.append(str(item["damage_type"]).casefold())
        if item["use_range"]:
            details.append(str(item["use_range"]).casefold())
        if item["ammo_max"] is not None:
            details.append(f'боезапас {item["ammo"]}/{item["ammo_max"]}')
        if item["fire_rate"]:
            details.append(f'СКР {item["fire_rate"]}')
        if item.get("attachments"):
            details.append("насадки: " + ", ".join(item["attachments"]))
        if item["equipped"]:
            details.append("экипировано")
        quantity = f' ×{item["quantity"]}' if int(item["quantity"]) > 1 else ""
        effect = str(item.get("description") or "").strip()
        entry = f'**{item["name"]}{quantity} — {", ".join(details)}.**'
        if effect and effect != "Для использования предмет должен находиться в инвентаре персонажа.":
            entry += f'\n└─ *{effect}*'
        lines.append(entry)
    embed = discord.Embed(
        title=f'Инвентарь · {character["surname"]} {character["name"]} · {category}',
        description=short("\n────────────\n".join(lines) or "В этом разделе пусто", 4000),
        color=0x6E654F,
    )
    embed.add_field(
        name="Слоты",
        value=(
            f'Малые: **{small}/{small_cap}**\nБольшие: **{large}/{large_cap}**\n'
            f'Безделушки: без ограничений'
        ),
    )
    embed.set_footer(text=f"Страница {page + 1}/{page_count} · предметов в разделе: {len(category_items)}")
    return embed


class InventoryAddModal(discord.ui.Modal, title="Добавить предмет"):
    item_name = discord.ui.TextInput(label="Название предмета из каталога", max_length=100)
    quantity = discord.ui.TextInput(label="Количество", default="1", max_length=2)

    def __init__(self, character: dict):
        super().__init__()
        self.character = character

    async def on_submit(self, interaction: discord.Interaction):
        try:
            quantity = int(self.quantity.value)
        except ValueError:
            await interaction.response.send_message("Количество должно быть целым числом.", ephemeral=True)
            return
        if quantity < 1 or quantity > 20:
            await interaction.response.send_message("Количество должно быть от 1 до 20.", ephemeral=True)
            return
        character = await bot.db.character(interaction.guild_id, self.character["user_id"])
        item = await bot.db.catalog_item(interaction.guild_id, self.item_name.value.strip())
        if not character or not item:
            await interaction.response.send_message("Такого предмета нет в каталоге сервера.", ephemeral=True)
            return
        current = await bot.db.inventory(character["id"])
        small_capacity, large_capacity = inventory_slot_capacities(character)
        if item["size"] == "Малый":
            occupied = sum(row["quantity"] for row in current if row["size"] == "Малый")
            capacity = small_capacity
        elif item["size"] == "Большой":
            occupied = sum(row["quantity"] for row in current if row["size"] == "Большой")
            capacity = large_capacity
        else:
            occupied, capacity = 0, 10**9
        if occupied + quantity > capacity:
            await interaction.response.send_message(
                f"Недостаточно слотов: занято {occupied}/{capacity}, требуется ещё {quantity}.",
                ephemeral=True,
            )
            return
        await bot.db.give_item(character["id"], item, quantity)
        await interaction.response.send_message(
            f'**{item["name"]}** ×{quantity} добавлен в инвентарь.',
            ephemeral=True,
        )


class InventoryItemSelect(discord.ui.Select):
    def __init__(self, view: "InventoryActionsView", items: list[dict]):
        self.inventory_view = view
        options = [
            discord.SelectOption(
                label=item["name"][:100],
                value=str(item["id"]),
                description=(
                    f'{item["category"]} · {item["durability"]}/{item["max_durability"]}'
                    + (" · экипировано" if item["equipped"] else "")
                )[:100],
            )
            for item in items[:25]
        ]
        super().__init__(
            placeholder="Сначала выберите предмет",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        self.inventory_view.selected_id = int(self.values[0])
        await interaction.response.defer()


def inventory_page_items(items: list[dict], category: str, page: int) -> tuple[list[dict], int, int]:
    filtered = [item for item in items if item["size"] == category]
    page_count = max(1, (len(filtered) + INVENTORY_PAGE_SIZE - 1) // INVENTORY_PAGE_SIZE)
    page = max(0, min(page, page_count - 1))
    start = page * INVENTORY_PAGE_SIZE
    return filtered[start:start + INVENTORY_PAGE_SIZE], page, page_count


class PageSelect(discord.ui.Select):
    def __init__(self, owner, current_page: int, page_count: int, row: int):
        self.owner = owner
        first = max(0, min(current_page - 12, max(0, page_count - 25)))
        last = min(page_count, first + 25)
        super().__init__(
            placeholder=f"Выбрать страницу · {current_page + 1}/{page_count}",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=f"Страница {index + 1}",
                    value=str(index),
                    default=index == current_page,
                )
                for index in range(first, last)
            ],
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.owner.refresh(interaction, page=int(self.values[0]))


class InventoryActionsView(discord.ui.View):
    def __init__(self, character: dict, items: list[dict], category: str = "Большой", page: int = 0):
        super().__init__(timeout=180)
        self.character = character
        self.category = category
        self.selected_id: int | None = None
        self.items = {int(item["id"]): item for item in items}
        visible, self.page, self.page_count = inventory_page_items(items, category, page)
        if visible:
            super().add_item(InventoryItemSelect(self, visible))
        super().add_item(PageSelect(self, self.page, self.page_count, row=4))
        self.first_page.disabled = self.page == 0
        self.previous_page.disabled = self.page == 0
        self.next_page.disabled = self.page >= self.page_count - 1
        self.last_page.disabled = self.page >= self.page_count - 1

    def selected_item(self) -> dict | None:
        return self.items.get(self.selected_id)

    async def refresh(
        self,
        interaction: discord.Interaction,
        category: str | None = None,
        page: int | None = None,
    ):
        character = await bot.db.character(interaction.guild_id, self.character["user_id"])
        items = await bot.db.inventory(character["id"])
        category = category or self.category
        page = self.page if page is None else page
        await interaction.response.edit_message(
            embed=await build_inventory_embed(character, category, page),
            view=InventoryActionsView(character, items, category, page),
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.character["user_id"]:
            await interaction.response.send_message("Управлять инвентарём может только владелец.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Большие", style=discord.ButtonStyle.secondary, row=1)
    async def large_category(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, "Большой", 0)

    @discord.ui.button(label="Малые", style=discord.ButtonStyle.secondary, row=1)
    async def small_category(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, "Малый", 0)

    @discord.ui.button(label="Безделушки", style=discord.ButtonStyle.secondary, row=1)
    async def trinket_category(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, "Безделушка", 0)

    @discord.ui.button(label="←", style=discord.ButtonStyle.secondary, row=2)
    async def previous_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, page=self.page - 1)

    @discord.ui.button(label="→", style=discord.ButtonStyle.secondary, row=2)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, page=self.page + 1)

    @discord.ui.button(label="В начало", style=discord.ButtonStyle.secondary, row=2)
    async def first_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, page=0)

    @discord.ui.button(label="В конец", style=discord.ButtonStyle.secondary, row=2)
    async def last_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, page=self.page_count - 1)

    @discord.ui.button(label="Добавить +", style=discord.ButtonStyle.success, row=3)
    async def add_item(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(InventoryAddModal(self.character))

    @discord.ui.button(label="Удалить −", style=discord.ButtonStyle.danger, row=3)
    async def remove_item(self, interaction: discord.Interaction, _: discord.ui.Button):
        item = self.selected_item()
        if not item:
            await interaction.response.send_message("Сначала выберите предмет в списке.", ephemeral=True)
            return
        await bot.db.remove_inventory_by_name(self.character["id"], item["name"], 1)
        await self.refresh(interaction)

    @discord.ui.button(label="Экипировать", style=discord.ButtonStyle.primary, row=3)
    async def equip_item(self, interaction: discord.Interaction, _: discord.ui.Button):
        item = self.selected_item()
        if not item:
            await interaction.response.send_message("Сначала выберите предмет в списке.", ephemeral=True)
            return
        success, message = await bot.db.set_equipped(self.character["id"], item["id"], True)
        if not success:
            await interaction.response.send_message(message, ephemeral=True)
            return
        await self.refresh(interaction)

    @discord.ui.button(label="Снять", style=discord.ButtonStyle.secondary, row=3)
    async def unequip_item(self, interaction: discord.Interaction, _: discord.ui.Button):
        item = self.selected_item()
        if not item:
            await interaction.response.send_message("Сначала выберите предмет в списке.", ephemeral=True)
            return
        success, message = await bot.db.set_equipped(self.character["id"], item["id"], False)
        if not success:
            await interaction.response.send_message(message, ephemeral=True)
            return
        await self.refresh(interaction)


class AdminInventoryActionsView(discord.ui.View):
    def __init__(self, character: dict, items: list[dict], category: str = "Большой", page: int = 0):
        super().__init__(timeout=180)
        self.character = character
        self.category = category
        self.selected_id: int | None = None
        self.items = {int(item["id"]): item for item in items}
        visible, self.page, self.page_count = inventory_page_items(items, category, page)
        if visible:
            super().add_item(InventoryItemSelect(self, visible))
        super().add_item(PageSelect(self, self.page, self.page_count, row=4))
        self.first_page.disabled = self.page == 0
        self.previous_page.disabled = self.page == 0
        self.next_page.disabled = self.page >= self.page_count - 1
        self.last_page.disabled = self.page >= self.page_count - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not has_master_access(interaction):
            await interaction.response.send_message(MASTER_ACCESS_ERROR, ephemeral=True)
            return False
        return True

    async def refresh(
        self,
        interaction: discord.Interaction,
        category: str | None = None,
        page: int | None = None,
    ):
        character = await bot.db.character(interaction.guild_id, self.character["user_id"])
        items = await bot.db.inventory(character["id"])
        category = category or self.category
        page = self.page if page is None else page
        await interaction.response.edit_message(
            embed=await build_inventory_embed(character, category, page),
            view=AdminInventoryActionsView(character, items, category, page),
        )

    @discord.ui.button(label="Большие", style=discord.ButtonStyle.secondary, row=1)
    async def large_category(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, "Большой", 0)

    @discord.ui.button(label="Малые", style=discord.ButtonStyle.secondary, row=1)
    async def small_category(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, "Малый", 0)

    @discord.ui.button(label="Безделушки", style=discord.ButtonStyle.secondary, row=1)
    async def trinket_category(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, "Безделушка", 0)

    @discord.ui.button(label="←", style=discord.ButtonStyle.secondary, row=2)
    async def previous_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, page=self.page - 1)

    @discord.ui.button(label="→", style=discord.ButtonStyle.secondary, row=2)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, page=self.page + 1)

    @discord.ui.button(label="В начало", style=discord.ButtonStyle.secondary, row=2)
    async def first_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, page=0)

    @discord.ui.button(label="В конец", style=discord.ButtonStyle.secondary, row=2)
    async def last_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, page=self.page_count - 1)

    @discord.ui.button(label="Добавить +", style=discord.ButtonStyle.success, row=3)
    async def add_item(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(InventoryAddModal(self.character))

    @discord.ui.button(label="Удалить −", style=discord.ButtonStyle.danger, row=3)
    async def remove_item(self, interaction: discord.Interaction, _: discord.ui.Button):
        item = self.items.get(self.selected_id)
        if not item:
            await interaction.response.send_message("Сначала выберите предмет в списке.", ephemeral=True)
            return
        await bot.db.remove_inventory_by_name(self.character["id"], item["name"], 1)
        await self.refresh(interaction)


STORE_CATEGORIES = (
    "Снаряжение",
    "Оружие дальнего боя",
    "Оружие ближнего боя",
    "Броня",
    "Разное",
)
STORE_PAGE_SIZE = 5
ROMAN_LEVELS = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5}


def store_category(item: dict) -> str:
    return "Броня" if item["category"] in {"Броня", "Щит"} else item["category"]


def required_supply_level(item: dict) -> int | None:
    access = str(item.get("access") or "")
    if access.casefold() == "общедоступное":
        return 0
    match = re.search(r"Снабжение\s+(I{1,3}|IV|V)\b", access, re.IGNORECASE)
    return ROMAN_LEVELS.get(match.group(1).upper()) if match else None


def character_supply_level(character: dict) -> int:
    if character.get("class_name") != "Снабженец":
        return -99
    return int(character.get("skills", {}).get("Снабжение", -99))


def can_purchase(character: dict, item: dict, category: str) -> bool:
    required = required_supply_level(item)
    return (
        category != "Разное"
        and int(item.get("price") or 0) > 0
        and required is not None
        and (required == 0 or character_supply_level(character) >= required)
    )


def store_price(character: dict, item: dict) -> int:
    price = int(item.get("price") or 0)
    if price <= 0:
        return price
    discount = 1 if "\u0411\u044e\u0440\u043e\u043a\u0440\u0430\u0442\u0438\u044f" in character.get("talents", {}) else 0
    if character_supply_level(character) > 6:
        discount += 1
    return max(1, price - discount)


def visible_store_items(character: dict, items: list[dict], category: str) -> list[dict]:
    level = character_supply_level(character)
    visible = []
    for item in items:
        if store_category(item) != category:
            continue
        if category == "Разное":
            visible.append(item)
            continue
        required = required_supply_level(item)
        if required is not None and (required == 0 or level >= required):
            visible.append(item)
    return visible


def visible_purchasable_items(character: dict, items: list[dict]) -> list[dict]:
    return [
        item
        for item in items
        if can_purchase(character, item, store_category(item))
    ]


def store_page_items(character: dict, items: list[dict], category: str, page: int) -> tuple[list[dict], int, int]:
    filtered = visible_store_items(character, items, category)
    pages = max(1, (len(filtered) + STORE_PAGE_SIZE - 1) // STORE_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * STORE_PAGE_SIZE
    return filtered[start:start + STORE_PAGE_SIZE], page, pages


def build_store_embed(character: dict, items: list[dict], category: str, page: int) -> discord.Embed:
    visible, page, pages = store_page_items(character, items, category, page)
    lines = []
    for item in visible:
        required = required_supply_level(item)
        if category == "Разное":
            status = "только просмотр"
        elif required is None:
            status = "не продаётся"
        elif required and character_supply_level(character) < required:
            status = f'требуется Снабжение {required}'
        else:
            status = "доступно"
        effective_price = store_price(character, item)
        price_text = f"{effective_price} БС" if effective_price else "без цены"
        if effective_price and effective_price != int(item["price"]):
            price_text += f' (обычно {item["price"]})'
        details = [price_text, item["access"], status]
        hand = {1: "одноручное", 2: "двуручное"}.get(int(item.get("hands") or 0))
        if hand:
            details.append(hand)
        lines.append(
            f'**{item["name"]}**\n'
            f'└─ {" · ".join(details)}\n'
            f'└─ {short(str(item.get("description") or "Без описания"), 300)}'
        )
    embed = discord.Embed(
        title=f"Магазин снабжения · {category}",
        description=short("\n────────────\n".join(lines) or "В этой категории пока пусто.", 4000),
        color=0x745B38,
    )
    supply_skill = (
        f'{character["skills"].get("Снабжение", -3):+d}'
        if character["class_name"] == "Снабженец" else "нет"
    )
    embed.add_field(
        name="Лицевой счёт",
        value=f'БС: **{character["supply_forms"]}** · Снабжение: **{supply_skill}**',
        inline=False,
    )
    embed.set_footer(text=f"Страница {page + 1}/{pages} · выберите предмет в списке")
    return embed


class StoreItemSelect(discord.ui.Select):
    def __init__(self, store_view: "StoreView", items: list[dict]):
        self.store_view = store_view
        super().__init__(
            placeholder="Выберите предмет",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=item["name"][:100],
                    value=str(item["id"]),
                    description=f'{store_price(store_view.character, item)} БС · {item["access"]}'[:100],
                )
                for item in items
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        self.store_view.selected_id = int(self.values[0])
        item = self.store_view.items[self.store_view.selected_id]
        if can_purchase(self.store_view.character, item, self.store_view.category):
            if self.store_view.buy_button not in self.store_view.children:
                self.store_view.add_item(self.store_view.buy_button)
        elif self.store_view.buy_button in self.store_view.children:
            self.store_view.remove_item(self.store_view.buy_button)
        await interaction.response.edit_message(view=self.store_view)


class StoreView(discord.ui.View):
    def __init__(self, character: dict, items: list[dict], category: str = "Снаряжение", page: int = 0):
        super().__init__(timeout=300)
        self.character = character
        self.items = {int(item["id"]): item for item in items}
        self.category = category
        self.selected_id: int | None = None
        visible, self.page, self.page_count = store_page_items(character, items, category, page)
        if visible:
            self.add_item(StoreItemSelect(self, visible))
        self.add_item(PageSelect(self, self.page, self.page_count, row=3))
        self.first_page.disabled = self.page == 0
        self.previous_page.disabled = self.page == 0
        self.next_page.disabled = self.page >= self.page_count - 1
        self.last_page.disabled = self.page >= self.page_count - 1
        self.remove_item(self.buy_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.character["user_id"]:
            await interaction.response.send_message("Этим магазином может пользоваться только его владелец.", ephemeral=True)
            return False
        return True

    async def refresh(self, interaction: discord.Interaction, category: str | None = None, page: int | None = None):
        character = await bot.db.character(interaction.guild_id, self.character["user_id"])
        items = await bot.db.catalog_items(interaction.guild_id, "", 500)
        category = category or self.category
        page = self.page if page is None else page
        await interaction.response.edit_message(
            embed=build_store_embed(character, items, category, page),
            view=StoreView(character, items, category, page),
        )

    @discord.ui.button(label="Снаряжение", style=discord.ButtonStyle.secondary, row=1)
    async def equipment(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, "Снаряжение", 0)

    @discord.ui.button(label="Дальний бой", style=discord.ButtonStyle.secondary, row=1)
    async def ranged(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, "Оружие дальнего боя", 0)

    @discord.ui.button(label="Ближний бой", style=discord.ButtonStyle.secondary, row=1)
    async def melee(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, "Оружие ближнего боя", 0)

    @discord.ui.button(label="Броня", style=discord.ButtonStyle.secondary, row=1)
    async def armor(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, "Броня", 0)

    @discord.ui.button(label="Разное", style=discord.ButtonStyle.secondary, row=4)
    async def misc(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, "Разное", 0)

    @discord.ui.button(label="Насадки", style=discord.ButtonStyle.primary, row=1)
    async def attachments(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, "Насадка", 0)

    @discord.ui.button(label="←", style=discord.ButtonStyle.secondary, row=2)
    async def previous_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, page=self.page - 1)

    @discord.ui.button(label="→", style=discord.ButtonStyle.secondary, row=2)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, page=self.page + 1)

    @discord.ui.button(label="В начало", style=discord.ButtonStyle.secondary, row=2)
    async def first_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, page=0)

    @discord.ui.button(label="В конец", style=discord.ButtonStyle.secondary, row=2)
    async def last_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, page=self.page_count - 1)

    @discord.ui.button(label="Купить", style=discord.ButtonStyle.success, row=2)
    async def buy_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        item = self.items.get(self.selected_id)
        if not item or not can_purchase(self.character, item, self.category):
            await interaction.response.send_message("Этот предмет вам недоступен.", ephemeral=True)
            return
        required = required_supply_level(item) or 0
        success, message, _ = await bot.db.purchase_item(self.character["id"], item["id"], required)
        if not success:
            await interaction.response.send_message(message, ephemeral=True)
            return
        base_price = int(item.get("price") or 0)
        paid = store_price(self.character, item)
        discount = max(0, base_price - paid)
        await self.refresh(interaction)
        await interaction.followup.send(
            f'{interaction.user.mention} \u0437\u0430\u043a\u0430\u0437\u044b\u0432\u0430\u0435\u0442 **{item["name"]}** \u0437\u0430 **{paid} \u0411\u0421**. \u0421\u043a\u0438\u0434\u043a\u0430: **{discount} \u0411\u0421**. \u0417\u0430\u044f\u0432\u043a\u0430 \u043e\u0436\u0438\u0434\u0430\u0435\u0442 \u0440\u0435\u0448\u0435\u043d\u0438\u044f \u0441\u043d\u0430\u0431\u0436\u0435\u043d\u0438\u044f.',
            ephemeral=False,
        )


SUPPLY_ORDER_PAGE_SIZE = 5


def build_supply_orders_embed(orders: list[dict], page: int) -> discord.Embed:
    pages = max(1, (len(orders) + SUPPLY_ORDER_PAGE_SIZE - 1) // SUPPLY_ORDER_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    visible = orders[page * SUPPLY_ORDER_PAGE_SIZE:(page + 1) * SUPPLY_ORDER_PAGE_SIZE]
    lines = [
        f'**#{order["id"]} · <@{order["user_id"]}> · {order["surname"]} {order["name"]}**\n'
        f'└─ {order["item_name"]} · уплачено **{order["paid_price"]} БС**\n'
        f'└─ заказ: **{order["ordered_at"]} UTC**'
        for order in visible
    ]
    embed = discord.Embed(
        title="Заявки снабжения",
        description="\n────────────\n".join(lines) or "Ожидающих заявок нет.",
        color=0x745B38,
    )
    embed.set_footer(text=f"Страница {page + 1}/{pages} · ожидает: {len(orders)}")
    return embed


class SupplyOrderSelect(discord.ui.Select):
    def __init__(self, owner_view: "SupplyOrdersView", orders: list[dict]):
        self.owner_view = owner_view
        super().__init__(
            placeholder="Выберите заявку",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=f'#{order["id"]} · {order["item_name"]}'[:100],
                    value=str(order["id"]),
                    description=f'{order["surname"]} {order["name"]} · {order["paid_price"]} БС'[:100],
                )
                for order in orders
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        self.owner_view.selected_id = int(self.values[0])
        await interaction.response.edit_message(view=self.owner_view)


class SupplyOrdersView(discord.ui.View):
    def __init__(self, admin_id: int, guild_id: int, orders: list[dict], page: int = 0):
        super().__init__(timeout=600)
        self.admin_id = admin_id
        self.guild_id = guild_id
        self.orders = orders
        self.pages = max(1, (len(orders) + SUPPLY_ORDER_PAGE_SIZE - 1) // SUPPLY_ORDER_PAGE_SIZE)
        self.page = max(0, min(page, self.pages - 1))
        self.selected_id: int | None = None
        visible = orders[self.page * SUPPLY_ORDER_PAGE_SIZE:(self.page + 1) * SUPPLY_ORDER_PAGE_SIZE]
        if visible:
            self.add_item(SupplyOrderSelect(self, visible))
        self.previous.disabled = self.page == 0
        self.next.disabled = self.page >= self.pages - 1
        self.approve.disabled = not visible
        self.reject.disabled = not visible

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.admin_id or not has_master_access(interaction):
            await interaction.response.send_message(MASTER_ACCESS_ERROR, ephemeral=True)
            return False
        return True

    async def refresh(self, interaction: discord.Interaction, page: int | None = None):
        orders = await bot.db.pending_purchase_orders(self.guild_id)
        target_page = self.page if page is None else page
        view = SupplyOrdersView(self.admin_id, self.guild_id, orders, target_page)
        await interaction.response.edit_message(
            embed=build_supply_orders_embed(orders, view.page),
            view=view,
        )

    async def resolve(self, interaction: discord.Interaction, approve: bool):
        if self.selected_id is None:
            await interaction.response.send_message("Сначала выберите заявку.", ephemeral=True)
            return
        success, message, order = await bot.db.resolve_purchase_order(
            self.guild_id, self.selected_id, interaction.user.id, approve,
        )
        if not success:
            await interaction.response.send_message(message, ephemeral=True)
            return
        orders = await bot.db.pending_purchase_orders(self.guild_id)
        view = SupplyOrdersView(self.admin_id, self.guild_id, orders, self.page)
        await interaction.response.edit_message(
            embed=build_supply_orders_embed(orders, view.page),
            view=view,
        )
        await interaction.followup.send(message, ephemeral=True)
        if order:
            member = interaction.guild.get_member(int(order["user_id"])) if interaction.guild else None
            if member:
                try:
                    result = "одобрена — предмет добавлен в инвентарь" if approve else f'отклонена — возвращено {order["paid_price"]} БС'
                    await member.send(f'Ваша заявка на **{order["item_name"]}** {result}.')
                except discord.HTTPException:
                    pass

    @discord.ui.button(label="Одобрить", style=discord.ButtonStyle.success, row=1)
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.resolve(interaction, True)

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.danger, row=1)
    async def reject(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.resolve(interaction, False)

    @discord.ui.button(label="←", style=discord.ButtonStyle.secondary, row=2)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, self.page - 1)

    @discord.ui.button(label="→", style=discord.ButtonStyle.secondary, row=2)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, self.page + 1)


TALENT_PAGE_SIZE = 5


def class_starter_names(class_name: str) -> tuple[str, ...]:
    return tuple(talent["name"] for talent in CLASS_TALENTS[class_name])


def talent_requirements_met(character: dict, talent: dict) -> bool:
    if talent.get("class_name") and talent["class_name"] != character["class_name"]:
        return False
    if int(talent.get("rank_required", 0)) > int(character["rank_index"]):
        return False
    skills = character.get("skills", {})
    return all(int(skills.get(skill, -3)) >= int(level) for skill, level in talent.get("skill_requirements", {}).items())


def talent_requirement_text(talent: dict) -> str:
    parts = [f'звание {RANKS[int(talent.get("rank_required", 0))]}']
    if talent.get("class_name"):
        parts.append(f'класс {talent["class_name"]}')
    parts.extend(f"{skill} {level}" for skill, level in talent.get("skill_requirements", {}).items())
    return " · ".join(parts)


def available_talents(character: dict):
    owned = {name.casefold() for name in character.get("talents", {})}
    starters = class_starter_names(character["class_name"])
    if not any(name.casefold() in owned for name in starters):
        return [
            TALENT_BY_NAME[name.casefold()]
            for name in starters
            if name.casefold() not in owned
        ]
    return [
        talent for talent in TALENTS
        if talent["kind"] in {"general", "class_progression", "skill"}
        and talent_requirements_met(character, talent)
        and talent["name"].casefold() not in owned

    ]


def talent_page(character: dict, mode: str, page: int) -> tuple[list[dict], int, int]:
    if mode == "Мои":
        talents = [
            TALENT_BY_NAME.get(name.casefold(), {
                "name": name, "description": description, "price": 0,
                "rank_required": 0, "kind": "granted",
            })
            for name, description in character.get("talents", {}).items()
        ]
    else:
        talents = available_talents(character)
    pages = max(1, (len(talents) + TALENT_PAGE_SIZE - 1) // TALENT_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * TALENT_PAGE_SIZE
    return talents[start:start + TALENT_PAGE_SIZE], page, pages


def build_talent_embed(character: dict, mode: str, page: int) -> discord.Embed:
    visible, page, pages = talent_page(character, mode, page)
    lines = []
    for talent in visible:
        rank = RANKS[int(talent["rank_required"])]
        price = f'{talent["price"]} БС' if talent["price"] else "стартовый"
        lines.append(
            f'**{talent["name"]}** · {price}\n'
            f'Требования: {talent_requirement_text(talent)}\n'
            f'└─ {talent["description"]}'
        )
    embed = discord.Embed(
        title=f"Таланты · {mode}",
        description=short("\n────────────\n".join(lines) or "В этом разделе пока пусто.", 4000),
        color=0x6E654F,
    )
    embed.add_field(
        name="Персонаж",
        value=f'{character["surname"]} {character["name"]} · {RANKS[character["rank_index"]]} · БС {character["supply_forms"]}',
        inline=False,
    )
    embed.set_footer(text=f"Страница {page + 1}/{pages}")
    return embed


class TalentSelect(discord.ui.Select):
    def __init__(self, talent_view: "TalentView", talents: list[dict]):
        self.talent_view = talent_view
        super().__init__(
            placeholder="Выберите талант",
            options=[
                discord.SelectOption(
                    label=talent["name"][:100],
                    value=talent["name"],
                    description=talent["description"][:100],
                )
                for talent in talents
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        self.talent_view.selected_name = self.values[0]
        if self.talent_view.mode != "Мои" and self.talent_view.buy_button not in self.talent_view.children:
            self.talent_view.add_item(self.talent_view.buy_button)
        await interaction.response.edit_message(view=self.talent_view)


class TalentView(discord.ui.View):
    def __init__(self, character: dict, mode: str = "Доступные", page: int = 0):
        super().__init__(timeout=300)
        self.character = character
        self.mode = mode
        self.selected_name: str | None = None
        visible, self.page, self.page_count = talent_page(character, mode, page)
        if visible:
            self.add_item(TalentSelect(self, visible))
        self.add_item(PageSelect(self, self.page, self.page_count, row=3))
        self.first_page.disabled = self.page == 0
        self.previous_page.disabled = self.page == 0
        self.next_page.disabled = self.page >= self.page_count - 1
        self.last_page.disabled = self.page >= self.page_count - 1
        self.remove_item(self.buy_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.character["user_id"]:
            await interaction.response.send_message("Управлять талантами может только владелец.", ephemeral=True)
            return False
        return True

    async def refresh(self, interaction: discord.Interaction, mode: str | None = None, page: int | None = None):
        character = await bot.db.character(interaction.guild_id, self.character["user_id"])
        mode = mode or self.mode
        page = self.page if page is None else page
        await interaction.response.edit_message(
            embed=build_talent_embed(character, mode, page),
            view=TalentView(character, mode, page),
        )

    @discord.ui.button(label="Доступные", style=discord.ButtonStyle.primary, row=1)
    async def available(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, "Доступные", 0)

    @discord.ui.button(label="Мои таланты", style=discord.ButtonStyle.secondary, row=1)
    async def owned(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, "Мои", 0)

    @discord.ui.button(label="←", style=discord.ButtonStyle.secondary, row=2)
    async def previous_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, page=self.page - 1)

    @discord.ui.button(label="→", style=discord.ButtonStyle.secondary, row=2)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, page=self.page + 1)

    @discord.ui.button(label="В начало", style=discord.ButtonStyle.secondary, row=2)
    async def first_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, page=0)

    @discord.ui.button(label="В конец", style=discord.ButtonStyle.secondary, row=2)
    async def last_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, page=self.page_count - 1)

    @discord.ui.button(label="Приобрести", style=discord.ButtonStyle.success, row=2)
    async def buy_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        talent = TALENT_BY_NAME.get((self.selected_name or "").casefold())
        if not talent or talent not in available_talents(self.character):
            await interaction.response.send_message("Этот талант сейчас недоступен.", ephemeral=True)
            return
        starters = class_starter_names(self.character["class_name"]) if talent["starter"] else ()
        success, message, _ = await bot.db.purchase_talent(
            self.character["id"],
            talent["name"],
            talent["description"],
            int(talent["price"]),
            int(talent["rank_required"]),
            talent["class_name"],
            starters,
            talent.get("skill_requirements", {}),
        )
        if not success:
            await interaction.response.send_message(message, ephemeral=True)
            return
        await self.refresh(interaction, "Доступные", 0)


async def weapon_modification_embed(character: dict, weapon_id: int | None) -> discord.Embed:
    items = await bot.db.inventory(character["id"])
    weapon = next((item for item in items if int(item["id"]) == weapon_id), None)
    if not weapon:
        return discord.Embed(
            title="Модификация оружия",
            description="Выберите конкретный экземпляр оружия. Затем выберите совместимую насадку и установите либо снимите её.",
            color=0x6E654F,
        )
    installed = await bot.db.weapon_attachments(character["id"], weapon_id)
    lines = [f'**{row["slot"]}:** {row["name"]}' for row in installed] or ["Насадки не установлены."]
    lines += [
        "",
        f'Урон **{weapon["damage"]}** · качество **{max(0, int(weapon["durability"]) + int(weapon.get("attachment_gear_modifier") or 0))}**',
        f'СКР **{weapon["fire_rate"]}** · БК **{weapon["ammo"]}/{weapon["ammo_max"]}**',
        f'Дистанция **{weapon["use_range"]}** · рук **{weapon["hands"]}**',
        f'Бонус Стрельбы **{int(weapon.get("attachment_skill_bonus") or 0):+d}**',
    ]
    return discord.Embed(title=f'Модификация · {weapon["name"]} #{weapon_id}', description="\n".join(lines), color=0x6E654F)


class WeaponModificationSelect(discord.ui.Select):
    def __init__(self, owner, items):
        self.owner_view = owner
        super().__init__(
            placeholder="1. Выберите оружие",
            options=[
                discord.SelectOption(label=f'{item["name"]} #{item["id"]}'[:100], value=str(item["id"]))
                for item in items[:25]
            ],
            row=0,
        )
    async def callback(self, interaction):
        self.owner_view.weapon_id = int(self.values[0])
        await self.owner_view.refresh(interaction)


class AttachmentModificationSelect(discord.ui.Select):
    def __init__(self, owner, options):
        self.owner_view = owner
        super().__init__(placeholder="2. Выберите насадку", options=options[:25], row=1)
    async def callback(self, interaction):
        self.owner_view.attachment_id = int(self.values[0])
        await interaction.response.defer()


class WeaponModificationView(discord.ui.View):
    def __init__(self, character: dict, items: list[dict], installed: list[dict], weapon_id: int | None = None):
        super().__init__(timeout=240)
        self.character = character
        self.weapon_id = weapon_id
        self.attachment_id = None
        weapons = [item for item in items if item["category"] == "Оружие дальнего боя"]
        if weapons:
            self.add_item(WeaponModificationSelect(self, weapons))
        weapon = next((item for item in weapons if int(item["id"]) == weapon_id), None)
        if weapon:
            installed_ids = {int(row["attachment_inventory_id"]) for row in installed}
            occupied_elsewhere = {
                int(row["attachment_inventory_id"]) for row in installed if int(row["weapon_inventory_id"]) != weapon_id
            }
            options = []
            for item in items:
                if item["category"] != "Насадка" or int(item["id"]) in occupied_elsewhere:
                    continue
                spec = ATTACHMENT_BY_NAME.get(item["name"])
                if int(item["id"]) in installed_ids or (spec and compatible(spec, weapon)):
                    options.append(discord.SelectOption(
                        label=item["name"][:100], value=str(item["id"]),
                        description=("установлено · " if int(item["id"]) in installed_ids else "") + str(spec["slot"]),
                    ))
            if options:
                self.add_item(AttachmentModificationSelect(self, options))

    async def refresh(self, interaction):
        items = await bot.db.inventory(self.character["id"])
        installed = await bot.db.weapon_attachments(self.character["id"])
        await interaction.response.edit_message(
            embed=await weapon_modification_embed(self.character, self.weapon_id),
            view=WeaponModificationView(self.character, items, installed, self.weapon_id),
        )

    @discord.ui.button(label="Установить", style=discord.ButtonStyle.success, row=2)
    async def install(self, interaction, _):
        if not self.weapon_id or not self.attachment_id:
            await interaction.response.send_message("Выберите оружие и насадку.", ephemeral=True)
            return
        success, message = await bot.db.install_attachment(self.character["id"], self.weapon_id, self.attachment_id)
        if not success:
            await interaction.response.send_message(message, ephemeral=True)
            return
        await self.refresh(interaction)

    @discord.ui.button(label="Снять", style=discord.ButtonStyle.danger, row=2)
    async def remove(self, interaction, _):
        if not self.weapon_id or not self.attachment_id:
            await interaction.response.send_message("Выберите установленную насадку.", ephemeral=True)
            return
        success, message = await bot.db.remove_attachment(self.character["id"], self.weapon_id, self.attachment_id)
        if not success:
            await interaction.response.send_message(message, ephemeral=True)
            return
        await self.refresh(interaction)


class CharacterPanel(discord.ui.View):
    def __init__(self, client: RattenBot, owner_id: int | None = None):
        super().__init__(timeout=None)
        self.client = client
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.owner_id is not None and interaction.user.id != self.owner_id:
            await interaction.response.send_message("Этой панелью может управлять только владелец персонажа.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Личное дело", style=discord.ButtonStyle.primary, custom_id="rr:card", row=0)
    async def card(self, interaction: discord.Interaction, _: discord.ui.Button):
        character = await get_character(interaction)
        if not character:
            return
        await interaction.response.defer(thinking=True)
        image = self.client.renderer.render(character)
        await interaction.followup.send(file=discord.File(image, filename="личное-дело.png"))

    @discord.ui.button(label="Характеристики", style=discord.ButtonStyle.secondary, custom_id="rr:stats", row=1)
    async def stats(self, interaction: discord.Interaction, _: discord.ui.Button):
        character = await get_character(interaction)
        if character:
            await interaction.response.send_modal(StatsModal(character))

    @discord.ui.button(label="Сменить портрет", style=discord.ButtonStyle.secondary, custom_id="rr:photo_url", row=0)
    async def photo_url(self, interaction: discord.Interaction, _: discord.ui.Button):
        character = await get_character(interaction)
        if character:
            await interaction.response.send_modal(PhotoUrlModal(character))

    @discord.ui.button(label="ФИО", style=discord.ButtonStyle.secondary, custom_id="rr:identity", row=0)
    async def identity(self, interaction: discord.Interaction, _: discord.ui.Button):
        character = await get_character(interaction)
        if character:
            await interaction.response.send_modal(IdentityModal(character))

    @discord.ui.button(label="Навыки", style=discord.ButtonStyle.secondary, custom_id="rr:skills", row=1)
    async def skills(self, interaction: discord.Interaction, _: discord.ui.Button):
        character = await get_character(interaction)
        if character:
            embed = discord.Embed(title="Раздел навыков", description="Выберите связанную характеристику или откройте классовый навык.", color=0x6E654F)
            await interaction.response.send_message(embed=embed, view=SkillCategoriesView(character), ephemeral=True)

    @discord.ui.button(label="Инвентарь", style=discord.ButtonStyle.secondary, custom_id="rr:inventory", row=1)
    async def inventory(self, interaction: discord.Interaction, _: discord.ui.Button):
        character = await get_character(interaction)
        if not character:
            return
        embed = await build_inventory_embed(character)
        items = await bot.db.inventory(character["id"])
        await interaction.response.send_message(
            embed=embed,
            view=InventoryActionsView(character, items),
            ephemeral=True,
        )

    @discord.ui.button(label="Таланты", style=discord.ButtonStyle.secondary, custom_id="rr:talents", row=2)
    async def talents(self, interaction: discord.Interaction, _: discord.ui.Button):
        character = await get_character(interaction)
        if not character:
            return
        await interaction.response.send_message(
            embed=build_talent_embed(character, "Доступные", 0),
            view=TalentView(character),
            ephemeral=True,
        )

    @discord.ui.button(label="Травмы", style=discord.ButtonStyle.secondary, custom_id="rr:injuries", row=2)
    async def injuries(self, interaction: discord.Interaction, _: discord.ui.Button):
        character = await get_character(interaction)
        if character:
            await interaction.response.send_message(embed=injuries_embed(character))

    @discord.ui.button(label="Заметки", style=discord.ButtonStyle.secondary, custom_id="rr:notes", row=2)
    async def notes(self, interaction: discord.Interaction, _: discord.ui.Button):
        character = await get_character(interaction)
        if character:
            await interaction.response.send_modal(NotesModal(character))


    @discord.ui.button(label="Модифицировать оружие", style=discord.ButtonStyle.primary, custom_id="rr:weapon_mods", row=3)
    async def weapon_mods(self, interaction: discord.Interaction, _: discord.ui.Button):
        character = await get_character(interaction)
        if not character:
            return
        items = await bot.db.inventory(character["id"])
        installed = await bot.db.weapon_attachments(character["id"])
        await interaction.response.send_message(
            embed=await weapon_modification_embed(character, None),
            view=WeaponModificationView(character, items, installed),
            ephemeral=True,
        )

async def apply_push_cost(pool: RollPool, character: dict) -> list[str]:
    messages: list[str] = []
    inventory_by_id = {item["id"]: item for item in await bot.db.inventory(character["id"])}
    attribute_ones = sum(value == 1 for value in pool.attribute_dice)
    new_attribute_ones = max(0, attribute_ones - pool.charged_attribute_ones)
    if new_attribute_ones:
        messages.append(await apply_damage(character, pool.attribute, new_attribute_ones))
        pool.charged_attribute_ones = attribute_ones
        character = await bot.db.character(character["guild_id"], character["user_id"])
    for item_id, values in pool.gear_dice.items():
        ones = sum(value == 1 for value in values)
        new_ones = max(0, ones - pool.charged_gear_ones.get(item_id, 0))
        if new_ones:
            durability = await bot.db.adjust_inventory_durability(item_id, character["id"], -new_ones)
            item_name = inventory_by_id.get(item_id, {}).get("name", "Снаряжение")
            messages.append(f'«{item_name}» теряет {new_ones} прочности → **{durability}**.')
            pool.charged_gear_ones[item_id] = ones
    return messages


class SkillRollView(discord.ui.View):
    def __init__(self, owner_id: int, character: dict, pool: RollPool, conditions: str, can_push: bool):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.character = character
        self.pool = pool
        self.conditions = conditions
        self.push_button.disabled = not can_push

    @discord.ui.button(label="Пуш", style=discord.ButtonStyle.primary)
    async def push_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Пушить может только владелец броска.", ephemeral=True)
            return
        self.pool.push()
        current = await bot.db.character(interaction.guild_id, self.owner_id)
        costs = await apply_push_cost(self.pool, current)
        embed = pool_embed(self.pool, f"Проверка · {self.pool.skill}", self.conditions)
        if costs:
            embed.add_field(name="Цена риска", value=short("\n".join(costs)), inline=False)
        await interaction.response.edit_message(embed=embed, view=self)


def armor_indestructible_dice(item: dict, weapon: dict | None, distance: str | None) -> int:
    text = item.get("conditions") or item.get("description") or ""
    weapon_text = " ".join(
        str(value or "")
        for value in (
            weapon.get("damage_type") if weapon else "",
            weapon.get("properties") if weapon else "",
            weapon.get("conditions") if weapon else "",
        )
    ).casefold()
    total = 0
    for sentence in re.split(r"[.;]", text):
        match = re.search(
            r"(?:да[её]т|\+)\s*(\d+)\s+неразрушаем\w*\s+куб",
            sentence,
            re.IGNORECASE,
        )
        if not match:
            continue
        condition = sentence.casefold()
        applies = True
        if "нулев" in condition and distance != "Нулевая":
            applies = False
        for marker in ("колющ", "режущ", "дробящ", "огнен", "огн"):
            if marker in condition and marker not in weapon_text:
                applies = False
        if ("взрыв" in condition or "оскол" in condition) and not (
            "взрыв" in weapon_text or "оскол" in weapon_text
        ):
            applies = False
        if applies:
            total += int(match.group(1))
    return total


class DefenseModifierModal(discord.ui.Modal, title="Пользовательская защита"):
    extra_dice = discord.ui.TextInput(
        label="Дополнительные кубы защиты",
        placeholder="Например: 2 или -1",
        default="0",
        max_length=3,
    )
    damage_reduction = discord.ui.TextInput(
        label="Снижение урона",
        placeholder="Положительное снижает, отрицательное увеличивает",
        default="0",
        max_length=3,
    )

    def __init__(self, attack_view: "AttackView"):
        super().__init__()
        self.attack_view = attack_view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            extra_dice = int(self.extra_dice.value)
            damage_reduction = int(self.damage_reduction.value)
        except ValueError:
            await interaction.response.send_message(
                "Оба модификатора должны быть целыми числами.",
                ephemeral=True,
            )
            return
        if not -20 <= extra_dice <= 20 or not -20 <= damage_reduction <= 20:
            await interaction.response.send_message(
                "Каждый модификатор должен быть от −20 до +20.",
                ephemeral=True,
            )
            return
        await self.attack_view.perform_defense(
            interaction,
            extra_dice=extra_dice,
            damage_reduction_modifier=damage_reduction,
        )


class AttackView(discord.ui.View):
    def __init__(
        self,
        attacker_id: int,
        target_id: int | None,
        attacker: dict,
        pools: list[RollPool],
        weapon: dict | None,
        ranged: bool,
        distance: str | None = None,
        distance_modifier: int = 0,
        target_attribute: str = "Телосложение",
        attacker_npc: dict | None = None,
        damage_modifier: int = 0,
    ):
        super().__init__(timeout=300)
        self.attacker_id = attacker_id
        self.target_id = target_id
        self.attacker = attacker
        self.pools = pools
        self.weapon = weapon
        self.ranged = ranged
        self.distance = distance
        self.distance_modifier = distance_modifier
        self.target_attribute = target_attribute
        self.attacker_npc = attacker_npc
        self.damage_modifier = damage_modifier
        self.resolved = False
        self.message: discord.Message | None = None
        fire_rate = int((weapon or {}).get("fire_rate") or 1)
        calm_trigger = not attacker_npc and has_talent(attacker, "\u0421\u043f\u043e\u043a\u043e\u0439\u043d\u044b\u0439 \u0441\u043f\u0443\u0441\u043a") and fire_rate == 1
        self.push_button.disabled = ranged and not (attacker_npc or calm_trigger)
        if target_id is None:
            self.remove_item(self.defend_button)
            self.remove_item(self.refuse_button)

    @property
    def attack_successes(self) -> int:
        return sum(pool.successes for pool in self.pools)

    def attack_embed(self) -> discord.Embed:
        weapon_name = self.weapon["name"] if self.weapon else "Удар кулаком"
        embed = discord.Embed(title=f"Атака · {weapon_name}", color=0x7A342E)
        for index, pool in enumerate(self.pools, 1):
            parts = [f'{pool.attribute}: {colored_dice(pool.attribute_dice, "attribute")}']
            if pool.skill:
                parts.append(f'{pool.skill}: {colored_dice(pool.skill_dice, "skill")}')
            if pool.gear_dice:
                parts.append(f'Снаряжение: {colored_dice(next(iter(pool.gear_dice.values()), []), "gear")}')
            if pool.negative_dice:
                parts.append(f'Отрицательные: {colored_dice(pool.negative_dice, "negative")}')
            if pool.skill_modifier_details:
                parts.append("Модификаторы кубов: " + ", ".join(
                    f'{name} **{value:+d}**' for name, value in pool.skill_modifier_details if value
                ))
            if pool.flat_success_modifier:
                parts.append(f'Модификатор успехов: **{pool.flat_success_modifier:+d}**')
            parts.append(f'Успехов: **{pool.successes}**')
            value = "\n".join(parts)
            embed.add_field(name=f"Очередь {index}", value=value, inline=False)
        if self.distance:
            embed.add_field(
                name="Дистанция",
                value=f"{self.distance} · модификатор **{self.distance_modifier:+d}**",
                inline=False,
            )
        if self.weapon:
            embed.add_field(
                name="Тип урона",
                value=self.weapon.get("damage_type") or "Не указан",
                inline=True,
            )
            embed.add_field(name="Условия оружия", value=short(self.weapon["conditions"]), inline=False)
        damage_factor = max(1, int(self.weapon["damage"])) if self.weapon else 1
        damage_before_defense = max(0, self.attack_successes * damage_factor + self.damage_modifier)
        modifier_text = (
            f' · модификатор урона: **{self.damage_modifier:+d}**'
            if self.damage_modifier else ""
        )
        embed.add_field(
            name="Общий итог очереди" if self.ranged and len(self.pools) > 1 else "Итог атаки",
            value=(
                f'Успехов: **{self.attack_successes}** · '
                f'урон до защиты: **{damage_before_defense}**{modifier_text} · '
                f'пушей: **{self.pools[0].push_count if self.pools else 0}**'
            ),
            inline=False,
        )
        return embed

    async def finish_damage(
        self,
        interaction: discord.Interaction,
        defense_successes: int,
        armor_rolls: dict[int, list[int]],
        indestructible_rolls: dict[str, list[int]] | None = None,
        damage_reduction_modifier: int = 0,
        extra_defense_dice: int = 0,
    ):
        if self.target_id is None:
            return
        self.resolved = True
        net = max(0, self.attack_successes - defense_successes)
        damage_factor = max(1, int(self.weapon["damage"])) if self.weapon else 1
        raw_damage = max(0, net * damage_factor + self.damage_modifier)
        target = await bot.db.character(interaction.guild_id, self.target_id)
        lines = [f"Атака: **{self.attack_successes}**", f"Защита: **{defense_successes}**"]
        target_items = {row["id"]: row for row in await bot.db.inventory(target["id"])}
        weapon_text = " ".join(
            str((self.weapon or {}).get(key) or "")
            for key in ("damage_type", "properties", "conditions")
        ).casefold()
        ignores_armor = "игнорирует броню" in weapon_text
        reduction = (
            0
            if ignores_armor
            else physical_armor_reduction(
                list(target_items.values()),
                self.weapon.get("damage_type", "") if self.weapon else "Дробящий",
            )
        )
        if "взрыв" in weapon_text:
            reduction += int(talent_effect(target, "explosion_damage_reduction", 0) or 0)
        damage = max(0, raw_damage - reduction - damage_reduction_modifier)
        for item_id, values in armor_rolls.items():
            item = target_items.get(item_id)
            if item:
                state = f'защита {item["durability"]}/{item["max_durability"]}'
                lines.append(f'{item["name"]} · {state}: {colored_dice(values, "gear")}')
        for source, values in (indestructible_rolls or {}).items():
            lines.append(f'{source} · неразрушаемые: {colored_dice(values, "gear")}')
        lines.append(f"Незаблокированных успехов: **{net}**")
        if extra_defense_dice:
            lines.append(f"Пользовательские кубы защиты: **{extra_defense_dice:+d}**")
        if damage_reduction_modifier:
            lines.append(
                f"Пользовательское снижение урона: **{damage_reduction_modifier:+d}**"
            )
        if self.damage_modifier:
            lines.append(f"Модификатор урона: **{self.damage_modifier:+d}**")
        if reduction:
            lines.append(f"Снижение бронёй ({self.weapon.get('damage_type')}): **−{reduction} урона**")
        lines.append(f"Итоговый урон: **{damage}**")
        if ignores_armor:
            lines.append("Огнемёт игнорирует экипированную броню.")
        if damage > 0 and "напалм" in weapon_text:
            lines.append("Наложен **Напалм**: 2 урона каждый ход; тушение — проверка Проворства и действие.")
        elif damage > 0 and "поджог" in weapon_text:
            lines.append("Наложен **Поджог**: 1 урон каждый ход; тушение — проверка Проворства и манёвр.")
        if damage > 0:
            for item_id, values in armor_rolls.items():
                ones = sum(value == 1 for value in values)
                item = target_items.get(item_id)
                if ones:
                    durability = await bot.db.adjust_inventory_durability(item_id, target["id"], -ones)
                    slot = item["size"] if item else "неизвестная"
                    kind = "броня" if item and item["category"] == "Броня" else "щит"
                    lines.append(f'{slot} {kind} «{item["name"] if item else "неизвестный"}» повреждён на {ones} → **{durability}**.')
            lines.append(await apply_damage(target, self.target_attribute, damage))
        embed = discord.Embed(
            title=f'Итог защиты · {target["surname"]} {target["name"]}',
            description=short("\n\n".join(lines), 4000),
            color=0x375A4A if damage == 0 else 0x7A342E,
        )
        PENDING_ATTACKS.pop((interaction.guild_id, self.target_id), None)
        await interaction.response.send_message(embed=embed)
        if self.message:
            await self.message.edit(view=None)

    @discord.ui.button(label="Пуш атаки", style=discord.ButtonStyle.primary)
    async def push_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self.resolved or interaction.user.id != self.attacker_id:
            await interaction.response.send_message("Эта кнопка сейчас недоступна.", ephemeral=True)
            return
        for pool in self.pools:
            pool.push()
        costs = []
        if self.attacker_npc:
            for pool in self.pools:
                ones = sum(value == 1 for value in pool.attribute_dice)
                new_ones = max(0, ones - pool.charged_attribute_ones)
                if new_ones:
                    before, after = await bot.db.damage_npc_attribute(
                        self.attacker_npc["id"], pool.attribute, new_ones
                    )
                    maximum = self.attacker_npc["physique_max"] if pool.attribute == "\u0422\u0435\u043b\u043e\u0441\u043b\u043e\u0436\u0435\u043d\u0438\u0435" else self.attacker_npc["agility_max"]
                    costs.append(f'{self.attacker_npc["name"]} \u00b7 {pool.attribute}: {before}/{maximum} \u2192 {after}/{maximum}')
                    pool.charged_attribute_ones = ones
        else:
            current = await bot.db.character(interaction.guild_id, self.attacker_id)
            for pool in self.pools:
                costs.extend(await apply_push_cost(pool, current))
        embed = self.attack_embed()
        if costs:
            embed.add_field(name="Цена риска", value=short("\n".join(costs)), inline=False)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Защищаться", style=discord.ButtonStyle.success)
    async def defend_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self.resolved or interaction.user.id != self.target_id:
            await interaction.response.send_message("Защищаться может только цель атаки.", ephemeral=True)
            return
        await interaction.response.send_modal(DefenseModifierModal(self))

    async def perform_defense(
        self,
        interaction: discord.Interaction,
        extra_dice: int = 0,
        damage_reduction_modifier: int = 0,
    ):
        target = await bot.db.character(interaction.guild_id, self.target_id)
        weapon_text = " ".join(
            str((self.weapon or {}).get(key) or "")
            for key in ("damage_type", "properties", "conditions")
        ).casefold()
        ignores_armor = "игнорирует броню" in weapon_text
        equipped = [
            item for item in await bot.db.inventory(target["id"])
            if item["equipped"] and item["durability"] > 0 and item["category"] in {"Броня", "Щит"}
        ]
        armor_rolls = {
            item["id"]: d6(int(item["durability"]))
            for item in equipped
            if item["category"] == "Броня" and not ignores_armor
        }
        armor_rolls.update({
            item["id"]: d6(int(item["durability"]))
            for item in equipped
            if item["category"] == "Щит"
        })
        indestructible_rolls = {}
        custom_defense_rolls = d6(max(0, extra_dice))
        negative_defense_rolls = d6(max(0, -extra_dice))
        for item in equipped:
            if ignores_armor and item["category"] == "Броня":
                continue
            count = armor_indestructible_dice(item, self.weapon, self.distance)
            if count:
                indestructible_rolls[item["name"]] = d6(count)
        if target["race"] == "Тараканы" and not ignores_armor:
            indestructible_rolls["Хитиновая броня"] = d6(2)
        successes = sum(value == 6 for values in armor_rolls.values() for value in values)
        successes += sum(
            value == 6
            for values in indestructible_rolls.values()
            for value in values
        )
        successes += sum(value == 6 for value in custom_defense_rolls)
        successes -= sum(value == 6 for value in negative_defense_rolls)
        if custom_defense_rolls:
            indestructible_rolls["Пользовательские кубы"] = custom_defense_rolls
        if negative_defense_rolls:
            indestructible_rolls["Отрицательные кубы защиты"] = negative_defense_rolls
        await self.finish_damage(
            interaction,
            successes,
            armor_rolls,
            indestructible_rolls,
            damage_reduction_modifier,
            extra_dice,
        )

    @discord.ui.button(label="Отказаться от защиты", style=discord.ButtonStyle.danger)
    async def refuse_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self.resolved or interaction.user.id != self.target_id:
            await interaction.response.send_message("Отказаться может только цель атаки.", ephemeral=True)
            return
        await self.finish_damage(interaction, 0, {})



@bot.tree.command(name="регистрация", description="Зарегистрировать или перезаписать личное дело персонажа")
async def register(interaction: discord.Interaction):
    if not interaction.guild_id:
        await interaction.response.send_message("Регистрация доступна только на сервере.", ephemeral=True)
        return
    embed = discord.Embed(
        title="Регистрация личного дела",
        description="Выберите класс и расу из списков, затем нажмите **Продолжить регистрацию**.",
        color=0x9B6A2F,
    )
    await interaction.response.send_message(embed=embed, view=RegistrationFlow(), ephemeral=True)


@bot.tree.command(name="кубы-проверить", description="Проверить набор пользовательских эмодзи кубов")
async def check_dice_emojis(interaction: discord.Interaction):
    lines = []
    missing = []
    labels = {
        "attribute": "Характеристика",
        "skill": "Навык",
        "negative": "Отрицательные",
        "gear": "Снаряжение",
    }
    for color, label in labels.items():
        values = []
        for face in range(1, 7):
            emoji = dice_emoji(color, face)
            if emoji:
                values.append(str(emoji))
            else:
                name = f'rr_{DIE_EMOJI_NAMES[color]}_{face}'
                values.append(f'`{name}`')
                missing.append(name)
        lines.append(f'**{label}:** {" ".join(values)}')
    footer = (
        f'\n\nНе найдено: **{len(missing)}**\n' + ", ".join(f'`{name}`' for name in missing)
        if missing else "\n\nВсе 24 эмодзи найдены."
    )
    await interaction.response.send_message(
        embed=discord.Embed(
            title="Проверка кубов",
            description=short("\n".join(lines) + footer, 4000),
            color=0x6E654F,
        ),
        ephemeral=True,
    )


@bot.tree.command(name="персонаж", description="Открыть панель управления персонажем")
@app_commands.describe(участник="Чей бланк открыть; если не указан — ваш")
async def character_panel(interaction: discord.Interaction, участник: discord.Member | None = None):
    owner = участник or interaction.user
    character = await bot.db.character(interaction.guild_id, owner.id)
    if not character:
        await interaction.response.send_message("У выбранного участника нет зарегистрированного персонажа.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    image = bot.renderer.render(character)
    await interaction.followup.send(
        file=discord.File(image, filename="личное-дело.png"),
        view=CharacterPanel(bot, owner_id=owner.id),
    )


@bot.tree.command(name="инвентарь", description="Показать инвентарь выбранного персонажа")
@app_commands.check(require_master_access)
async def inventory_command(interaction: discord.Interaction, участник: discord.Member):
    character = await bot.db.character(interaction.guild_id, участник.id)
    if not character:
        await interaction.response.send_message("У выбранного участника нет персонажа.", ephemeral=True)
        return
    items = await bot.db.inventory(character["id"])
    await interaction.response.send_message(
        embed=await build_inventory_embed(character),
        view=AdminInventoryActionsView(character, items),
        ephemeral=True,
    )


@bot.tree.command(name="модификация-оружия", description="Установить, снять или посмотреть насадки выбранного оружия")
@app_commands.choices(действие=[
    app_commands.Choice(name="Установить", value="установить"),
    app_commands.Choice(name="Снять", value="снять"),
    app_commands.Choice(name="Показать", value="показать"),
])
async def weapon_modification_command(
    interaction: discord.Interaction,
    действие: app_commands.Choice[str],
    оружие: str,
    насадка: str | None = None,
):
    character = await bot.db.character(interaction.guild_id, interaction.user.id)
    if not character:
        await interaction.response.send_message("Сначала зарегистрируйте персонажа.", ephemeral=True)
        return
    try:
        weapon_id = int(оружие)
        attachment_id = int(насадка) if насадка else None
    except ValueError:
        await interaction.response.send_message("Выберите оружие и насадку из подсказок команды.", ephemeral=True)
        return
    weapon = next((item for item in await bot.db.inventory(character["id"]) if int(item["id"]) == weapon_id), None)
    if not weapon or weapon["category"] != "Оружие дальнего боя":
        await interaction.response.send_message("Оружие не найдено.", ephemeral=True)
        return
    if действие.value == "показать":
        installed = await bot.db.weapon_attachments(character["id"], weapon_id)
        lines = [f'**{row["slot"]}:** {row["name"]}' for row in installed] or ["Насадки не установлены."]
        stats = (
            f'Урон **{weapon["damage"]}** · качество **{weapon["durability"]}'
            f'{int(weapon.get("attachment_gear_modifier") or 0):+d} · СКР **{weapon["fire_rate"]}** · '
            f'БК **{weapon["ammo"]}/{weapon["ammo_max"]}** · дистанция **{weapon["use_range"]}**'
        )
        await interaction.response.send_message(
            embed=discord.Embed(title=f'Модификация · {weapon["name"]} #{weapon_id}', description="\n".join(lines) + "\n\n" + stats, color=0x6E654F),
            ephemeral=True,
        )
        return
    if attachment_id is None:
        await interaction.response.send_message("Для этого действия выберите насадку.", ephemeral=True)
        return
    if действие.value == "установить":
        success, message = await bot.db.install_attachment(character["id"], weapon_id, attachment_id)
    else:
        success, message = await bot.db.remove_attachment(character["id"], weapon_id, attachment_id)
    await interaction.response.send_message(message, ephemeral=not success)


@weapon_modification_command.autocomplete("оружие")
async def weapon_modification_weapon_autocomplete(interaction: discord.Interaction, current: str):
    character = await bot.db.character(interaction.guild_id, interaction.user.id)
    if not character:
        return []
    return [
        app_commands.Choice(name=f'{item["name"]} #{item["id"]}'[:100], value=str(item["id"]))
        for item in await bot.db.inventory(character["id"])
        if item["category"] == "Оружие дальнего боя" and current.casefold() in item["name"].casefold()
    ][:25]


@weapon_modification_command.autocomplete("насадка")
async def weapon_modification_attachment_autocomplete(interaction: discord.Interaction, current: str):
    character = await bot.db.character(interaction.guild_id, interaction.user.id)
    if not character:
        return []
    return [
        app_commands.Choice(name=f'{item["name"]} #{item["id"]}'[:100], value=str(item["id"]))
        for item in await bot.db.inventory(character["id"])
        if item["category"] == "Насадка" and current.casefold() in item["name"].casefold()
    ][:25]


@bot.tree.command(name="навыки", description="Показать все навыки выбранного персонажа")
@app_commands.check(require_master_access)
async def skills_command(interaction: discord.Interaction, участник: discord.Member):
    character = await bot.db.character(interaction.guild_id, участник.id)
    if not character:
        await interaction.response.send_message("У выбранного участника нет персонажа.", ephemeral=True)
        return
    embed = discord.Embed(
        title=f'Навыки · {character["surname"]} {character["name"]}',
        color=0x6E654F,
    )
    for group, names in SKILL_GROUPS.items():
        values = "\n".join(f'**{name}:** {character["skills"].get(name, -3):+d}' for name in names)
        embed.add_field(name=group, value=values, inline=True)
    class_skill = CLASSES[character["class_name"]]
    embed.add_field(
        name=f'Классовый навык · {character["class_name"]}',
        value=f'**{class_skill}:** {character["skills"].get(class_skill, -3):+d}',
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="навыки-завершить", description="Зафиксировать стартовое распределение навыков")
async def finalize_skills_command(interaction: discord.Interaction):
    character = await bot.db.character(interaction.guild_id, interaction.user.id)
    if not character:
        await interaction.response.send_message("Сначала зарегистрируйте персонажа.", ephemeral=True)
        return
    success, message = await bot.db.finalize_starting_skills(
        character["id"], starting_skill_budget(character["race"])
    )
    await interaction.response.send_message(message, ephemeral=True)


@bot.tree.command(name="магазин-навыков", description="Повысить навык на 1 за 8 БС")
async def purchase_skill_command(interaction: discord.Interaction, навык: str):
    character = await bot.db.character(interaction.guild_id, interaction.user.id)
    if not character:
        await interaction.response.send_message("Сначала зарегистрируйте персонажа.", ephemeral=True)
        return
    skill = normalize(навык, tuple(character["skills"]))
    if not skill:
        await interaction.response.send_message("Навык не найден.", ephemeral=True)
        return
    success, message, _ = await bot.db.purchase_skill(
        character["id"], skill, character_skill_cap(character, skill), 8
    )
    await interaction.response.send_message(message, ephemeral=not success)


@purchase_skill_command.autocomplete("навык")
async def purchase_skill_autocomplete(interaction: discord.Interaction, current: str):
    character = await bot.db.character(interaction.guild_id, interaction.user.id)
    if not character:
        return []
    return [app_commands.Choice(name=name, value=name) for name in character["skills"] if current.casefold() in name.casefold()][:25]


@bot.tree.command(name="навык-изменить", description="Администратор: прибавить или отнять постоянный уровень навыка")
@app_commands.choices(операция=[
    app_commands.Choice(name="Плюс", value="plus"),
    app_commands.Choice(name="Минус", value="minus"),
])
@app_commands.check(require_master_access)
async def adjust_skill_command(
    interaction: discord.Interaction,
    участник: discord.Member,
    навык: str,
    операция: app_commands.Choice[str],
    цифра: app_commands.Range[int, 1, 20],
):
    character = await bot.db.character(interaction.guild_id, участник.id)
    if not character:
        await interaction.response.send_message("У участника нет персонажа.", ephemeral=True)
        return
    skill = normalize(навык, tuple(character["skills"]))
    if not skill:
        await interaction.response.send_message("Навык не найден.", ephemeral=True)
        return
    delta = цифра if операция.value == "plus" else -цифра
    before, after = await bot.db.adjust_skill(character["id"], skill, delta)
    await interaction.response.send_message(
        f'{участник.mention} · **{skill}**: {before:+d} → {after:+d}.', ephemeral=True
    )


@adjust_skill_command.autocomplete("навык")
async def adjust_skill_autocomplete(interaction: discord.Interaction, current: str):
    names = tuple(dict.fromkeys((*SKILL_ATTRIBUTES, *CLASSES.values())))
    return [app_commands.Choice(name=name, value=name) for name in names if current.casefold() in name.casefold()][:25]


@bot.tree.command(name="удалить-персонажа", description="Безвозвратно удалить своего персонажа")
async def character_delete(interaction: discord.Interaction):
    photo_path = await bot.db.delete_character(interaction.guild_id, interaction.user.id)
    if photo_path is None:
        await interaction.response.send_message("У вас нет зарегистрированного персонажа.", ephemeral=True)
        return
    if photo_path:
        path = Path(photo_path)
        photos_dir = PHOTOS_ROOT.resolve()
        try:
            resolved = path.resolve()
            if resolved.parent == photos_dir and resolved.is_file():
                resolved.unlink()
        except OSError:
            logging.warning("Не удалось удалить фотографию персонажа: %s", path)
    await interaction.response.send_message("Ваш персонаж и все связанные с ним записи удалены.", ephemeral=True)


@bot.tree.command(name="звание", description="Повысить или понизить звание выбранного персонажа")
@app_commands.describe(участник="Владелец персонажа", действие="Направление изменения звания")
@app_commands.choices(действие=[
    app_commands.Choice(name="Повысить", value="up"),
    app_commands.Choice(name="Понизить", value="down"),
])
@app_commands.check(require_master_access)
async def rank_command(interaction: discord.Interaction, участник: discord.Member, действие: app_commands.Choice[str]):
    character = await bot.db.character(interaction.guild_id, участник.id)
    if not character:
        await interaction.response.send_message("У выбранного участника нет зарегистрированного персонажа.", ephemeral=True)
        return
    direction = 1 if действие.value == "up" else -1
    index = max(0, min(len(RANKS) - 1, character["rank_index"] + direction))
    await bot.db.update_character(character["id"], "rank_index", index)
    await interaction.response.send_message(f'{участник.mention}: новое звание — **{RANKS[index]}**.')


@bot.tree.command(name="урон-лечение", description="Прибавить или убавить пункты характеристики")
@app_commands.describe(
    участник="Владелец персонажа",
    характеристика="Выберите характеристику",
    действие="Прибавить или убавить пункты",
    количество="Количество пунктов",
)
@app_commands.choices(характеристика=[app_commands.Choice(name=name, value=name) for name in ATTRIBUTES])
@app_commands.choices(действие=[
    app_commands.Choice(name="Прибавить (+)", value="add"),
    app_commands.Choice(name="Убавить (−)", value="subtract"),
])
@app_commands.check(require_master_access)
async def damage_command(
    interaction: discord.Interaction,
    характеристика: app_commands.Choice[str],
    действие: app_commands.Choice[str],
    количество: app_commands.Range[int, 1, 20],
    участник: discord.Member | None = None,
    нпс: str | None = None,
):
    if bool(участник) == bool(нпс):
        await interaction.response.send_message("Выберите либо участника, либо НПС.", ephemeral=True)
        return
    if нпс:
        npc = await bot.db.npc(interaction.guild_id, нпс)
        if not npc:
            await interaction.response.send_message("НПС не найден.", ephemeral=True)
            return
        if характеристика.value not in {"Телосложение", "Ловкость"}:
            await interaction.response.send_message(
                "У НПС сейчас доступны только Телосложение и Ловкость.",
                ephemeral=True,
            )
            return
        if действие.value == "add":
            before, after = await bot.db.heal_npc_attribute(
                npc["id"], характеристика.value, количество
            )
        else:
            before, after = await bot.db.damage_npc_attribute(
                npc["id"], характеристика.value, количество
            )
        maximum = npc["physique_max"] if характеристика.value == "Телосложение" else npc["agility_max"]
        await interaction.response.send_message(
            f'НПС **{npc["name"]}** · {характеристика.value}: '
            f'**{before}/{maximum} → {after}/{maximum}**.'
        )
        return
    character = await bot.db.character(interaction.guild_id, участник.id)
    if not character:
        await interaction.response.send_message("У выбранного участника нет зарегистрированного персонажа.", ephemeral=True)
        return
    if действие.value == "add":
        before = character["attributes"][характеристика.value]["current"]
        after = await bot.db.heal(character["id"], характеристика.value, количество)
        message = f'**{характеристика.value}:** {before} → {after}'
    else:
        message = await apply_damage(character, характеристика.value, количество)
    await interaction.response.send_message(f'{участник.mention}\n{message}')


@damage_command.autocomplete("нпс")
async def damage_npc_autocomplete(interaction: discord.Interaction, current: str):
    return await npc_choices(interaction, current)


@rank_command.error
@damage_command.error
async def master_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, (MasterAccessRequired, app_commands.MissingPermissions)):
        await interaction.response.send_message(MASTER_ACCESS_ERROR, ephemeral=True)
    else:
        raise error


@bot.tree.command(name="бланки", description="Изменить количество бланков снабжения")
@app_commands.choices(действие=[
    app_commands.Choice(name="Прибавить (+)", value="add"),
    app_commands.Choice(name="Убавить (−)", value="subtract"),
])
@app_commands.check(require_master_access)
async def supply(
    interaction: discord.Interaction,
    участник: discord.Member,
    действие: app_commands.Choice[str],
    количество: app_commands.Range[int, 1, 999],
):
    character = await bot.db.character(interaction.guild_id, участник.id)
    if not character:
        await interaction.response.send_message("У выбранного участника нет зарегистрированного персонажа.", ephemeral=True)
        return
    delta = количество if действие.value == "add" else -количество
    value = max(0, character["supply_forms"] + delta)
    await bot.db.update_character(character["id"], "supply_forms", value)
    await interaction.response.send_message(f'{участник.mention}: бланки снабжения — **{value}**.')


@bot.tree.command(name="воля", description="Изменить текущую Волю персонажа")
@app_commands.choices(действие=[
    app_commands.Choice(name="Прибавить (+)", value="add"),
    app_commands.Choice(name="Убавить (−)", value="subtract"),
])
@app_commands.check(require_master_access)
async def will(
    interaction: discord.Interaction,
    участник: discord.Member,
    действие: app_commands.Choice[str],
    количество: app_commands.Range[int, 1, 20],
):
    character = await bot.db.character(interaction.guild_id, участник.id)
    if not character:
        await interaction.response.send_message("У выбранного участника нет зарегистрированного персонажа.", ephemeral=True)
        return
    guard = await bot.db.consume_will_guard(character["id"]) if действие.value == "subtract" else 0
    delta = количество if действие.value == "add" else -max(0, количество - guard)
    value = max(-10, min(character["will_max"], character["will_current"] + delta))
    await bot.db.update_character(character["id"], "will_current", value)
    suffix = f" Защита расходника поглотила **{guard}**." if guard else ""
    await interaction.response.send_message(
        f'{участник.mention}: Воля — **{value}/{character["will_max"]}**.{suffix}'
    )


@bot.tree.command(name="заражение", description="Изменить уровень заражения выбранного персонажа")
@app_commands.choices(действие=[
    app_commands.Choice(name="Прибавить (+)", value="add"),
    app_commands.Choice(name="Убавить (−)", value="subtract"),
])
@app_commands.check(require_master_access)
async def infection_command(
    interaction: discord.Interaction,
    участник: discord.Member,
    действие: app_commands.Choice[str],
    количество: app_commands.Range[int, 1, 5],
):
    character = await bot.db.character(interaction.guild_id, участник.id)
    if not character:
        await interaction.response.send_message("У выбранного участника нет персонажа.", ephemeral=True)
        return
    delta = количество if действие.value == "add" else -количество
    before, after = await bot.db.adjust_infection(character["id"], delta)
    await interaction.response.send_message(
        f'{участник.mention}: заражение **{before}/5 → {after}/5**.'
    )


@bot.tree.command(
    name="крысиное-превозмогание",
    description="Раз в 24 часа восстановить крысе один пункт характеристики",
)
@app_commands.choices(характеристика=[
    app_commands.Choice(name=name, value=name) for name in ATTRIBUTES
])
async def rat_recovery_command(
    interaction: discord.Interaction,
    характеристика: app_commands.Choice[str],
):
    character = await bot.db.character(interaction.guild_id, interaction.user.id)
    if not character:
        await interaction.response.send_message("Сначала зарегистрируйте персонажа.", ephemeral=True)
        return
    status, payload = await bot.db.rat_recover(character["id"], характеристика.value)
    if status == "wrong_race":
        await interaction.response.send_message("Эта способность доступна только персонажам расы Крысы.", ephemeral=True)
        return
    if status == "cooldown":
        timestamp = int(payload.timestamp())
        await interaction.response.send_message(
            f"Крысиное превозмогание снова будет доступно <t:{timestamp}:R>.",
            ephemeral=True,
        )
        return
    if status == "full":
        current, maximum = payload
        await interaction.response.send_message(
            f'**{характеристика.value}** уже восстановлена полностью: {current}/{maximum}. '
            "Кулдаун не потрачен.",
            ephemeral=True,
        )
        return
    before, after, ready_at = payload
    await interaction.response.send_message(
        f'Крысиное превозмогание: **{характеристика.value} {before} → {after}**.\n'
        f'Следующее использование доступно <t:{int(ready_at.timestamp())}:R>.'
    )


@bot.tree.command(name="бланки-передать", description="Передать свои бланки снабжения другому игроку")
async def supply_transfer(
    interaction: discord.Interaction,
    получатель: discord.Member,
    количество: app_commands.Range[int, 1, 999],
):
    sender = await bot.db.character(interaction.guild_id, interaction.user.id)
    recipient = await bot.db.character(interaction.guild_id, получатель.id)
    if not sender:
        await interaction.response.send_message("У вас нет зарегистрированного персонажа.", ephemeral=True)
        return
    if not recipient:
        await interaction.response.send_message("У получателя нет зарегистрированного персонажа.", ephemeral=True)
        return
    try:
        sender_balance, recipient_balance = await bot.db.transfer_supply(sender["id"], recipient["id"], количество)
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    await interaction.response.send_message(
        f'{interaction.user.mention} передаёт {получатель.mention} **{количество} БС**. '
        f'Остаток отправителя: **{sender_balance}**, баланс получателя: **{recipient_balance}**.'
    )


@bot.tree.command(name="магазин", description="Открыть магазин имущества за Бланки Снабжения")
async def store_command(interaction: discord.Interaction):
    character = await bot.db.character(interaction.guild_id, interaction.user.id)
    if not character:
        await interaction.response.send_message("Сначала зарегистрируйте персонажа.", ephemeral=False)
        return
    items = await bot.db.catalog_items(interaction.guild_id, "", 500)
    await interaction.response.send_message(
        embed=build_store_embed(character, items, "Снаряжение", 0),
        view=StoreView(character, items),
        ephemeral=False,
    )


@bot.tree.command(name="снабжение", description="Рассмотреть ожидающие заявки на покупку предметов")
@app_commands.check(require_master_access)
async def supply_orders_command(interaction: discord.Interaction):
    orders = await bot.db.pending_purchase_orders(interaction.guild_id)
    await interaction.response.send_message(
        embed=build_supply_orders_embed(orders, 0),
        view=SupplyOrdersView(interaction.user.id, interaction.guild_id, orders),
        ephemeral=True,
    )


@bot.tree.command(name="купить", description="Купить доступный предмет по названию без листания магазина")
@app_commands.describe(предмет="Точное название предмета из доступного вам магазина")
async def buy_item_command(interaction: discord.Interaction, предмет: str):
    character = await bot.db.character(interaction.guild_id, interaction.user.id)
    if not character:
        await interaction.response.send_message("Сначала зарегистрируйте персонажа.", ephemeral=True)
        return
    items = await bot.db.catalog_items(interaction.guild_id, "", 500)
    visible = visible_purchasable_items(character, items)
    item = next(
        (candidate for candidate in visible if candidate["name"].casefold() == предмет.strip().casefold()),
        None,
    )
    if not item:
        await interaction.response.send_message(
            "Этот предмет не найден среди доступных вам товаров магазина.",
            ephemeral=True,
        )
        return
    required = required_supply_level(item) or 0
    success, message, _ = await bot.db.purchase_item(character["id"], item["id"], required)
    if not success:
        await interaction.response.send_message(message, ephemeral=True)
        return
    base_price = int(item.get("price") or 0)
    paid = store_price(character, item)
    discount = max(0, base_price - paid)
    await interaction.response.send_message(
        f'{interaction.user.mention} заказывает **{item["name"]}** за **{paid} БС**. '
        f'Скидка: **{discount} БС**. Заявка ожидает решения снабжения.',
        ephemeral=False,
    )


@buy_item_command.autocomplete("предмет")
async def buy_item_autocomplete(interaction: discord.Interaction, current: str):
    character = await bot.db.character(interaction.guild_id, interaction.user.id)
    if not character:
        return []
    items = await bot.db.catalog_items(interaction.guild_id, "", 500)
    query = current.casefold().strip()
    visible = visible_purchasable_items(character, items)
    matches = [item for item in visible if query in item["name"].casefold()]
    matches.sort(key=lambda item: (not item["name"].casefold().startswith(query), item["name"].casefold()))
    return [
        app_commands.Choice(
            name=f'{item["name"]} · {store_price(character, item)} БС'[:100],
            value=item["name"][:100],
        )
        for item in matches[:25]
    ]


@bot.tree.command(name="магазин-талантов", description="Открыть магазин талантов за 16 Бланков Снабжения")
async def talent_store_command(interaction: discord.Interaction):
    character = await bot.db.character(interaction.guild_id, interaction.user.id)
    if not character:
        await interaction.response.send_message("Сначала зарегистрируйте персонажа.", ephemeral=True)
        return
    await interaction.response.send_message(
        embed=build_talent_embed(character, "Доступные", 0),
        view=TalentView(character),
        ephemeral=True,
    )


@bot.tree.command(name="предмет-передать", description="Передать предмет из своего инвентаря другому игроку")
async def item_transfer_command(
    interaction: discord.Interaction,
    получатель: discord.Member,
    предмет: str,
    количество: app_commands.Range[int, 1, 20] = 1,
):
    if получатель.id == interaction.user.id:
        await interaction.response.send_message("Нельзя передать предмет самому себе.", ephemeral=True)
        return
    sender = await bot.db.character(interaction.guild_id, interaction.user.id)
    recipient = await bot.db.character(interaction.guild_id, получатель.id)
    if not sender or not recipient:
        await interaction.response.send_message(
            "У отправителя или получателя нет зарегистрированного персонажа.",
            ephemeral=True,
        )
        return
    success, message = await bot.db.transfer_item(
        sender["id"], recipient["id"], предмет, количество
    )
    if not success:
        await interaction.response.send_message(message, ephemeral=True)
        return
    await interaction.response.send_message(
        f'{interaction.user.mention} передаёт {получатель.mention}: '
        f'**{предмет} ×{количество}**.'
    )


@item_transfer_command.autocomplete("предмет")
async def item_transfer_autocomplete(interaction: discord.Interaction, current: str):
    character = await bot.db.character(interaction.guild_id, interaction.user.id)
    if not character:
        return []
    return [
        app_commands.Choice(
            name=f'{item["name"]} ×{item["quantity"]}'[:100],
            value=item["name"],
        )
        for item in await bot.db.inventory(character["id"])
        if not item["equipped"] and current.casefold() in item["name"].casefold()
    ][:25]


@bot.tree.command(name="талант-выдать", description="Выдать персонажу талант без оплаты")
@app_commands.check(require_master_access)
async def grant_talent_command(
    interaction: discord.Interaction,
    участник: discord.Member,
    талант: str,
):
    character = await bot.db.character(interaction.guild_id, участник.id)
    talent = TALENT_BY_NAME.get(талант.casefold())
    if not character or not talent:
        await interaction.response.send_message("Персонаж или талант не найден.", ephemeral=True)
        return
    if talent["class_name"] and talent["class_name"] != character["class_name"]:
        await interaction.response.send_message("Этот классовый талант не подходит персонажу.", ephemeral=True)
        return
    if not await bot.db.grant_talent(character["id"], talent["name"], talent["description"]):
        await interaction.response.send_message("У персонажа уже есть этот талант.", ephemeral=True)
        return
    await interaction.response.send_message(
        f'{участник.mention} получает талант **{talent["name"]}**.'
    )


@grant_talent_command.autocomplete("талант")
async def grant_talent_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=talent["name"][:100], value=talent["name"])
        for talent in TALENTS
        if current.casefold() in talent["name"].casefold()
    ][:25]


@bot.tree.command(name="посмотреть-таланты", description="Посмотреть полученные таланты персонажа")
async def view_talents_command(
    interaction: discord.Interaction,
    участник: discord.Member,
):
    character = await bot.db.character(interaction.guild_id, участник.id)
    if not character:
        await interaction.response.send_message(
            "У выбранного участника нет зарегистрированного персонажа.",
            ephemeral=True,
        )
        return
    talents = character.get("talents", {})
    lines = [
        f'**{name}**\n└─ {description}'
        for name, description in talents.items()
    ]
    embed = discord.Embed(
        title=f'Таланты · {character["surname"]} {character["name"]}',
        description=short("\n────────────\n".join(lines) or "У персонажа пока нет талантов.", 4000),
        color=0x6E654F,
    )
    embed.set_footer(
        text=f'{RANKS[character["rank_index"]]} · талантов: {len(talents)}'
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="ролл", description="Бросить проверку навыка")
@app_commands.describe(
    навык="Проверяемый навык",
    снаряжение="Необязательный предмет для броска",
    бонус="Дополнительные положительные кубы (необязательно)",
    штраф="Дополнительные отрицательные кубы (необязательно)",
)
async def skill_roll_command(
    interaction: discord.Interaction,
    навык: str,
    снаряжение: str | None = None,
    бонус: app_commands.Range[int, 0, 20] = 0,
    штраф: app_commands.Range[int, 0, 20] = 0,
):
    character = await get_character(interaction)
    if not character:
        return
    if await reject_unfinished_skills(interaction, character):
        return
    skill = normalize(навык, tuple(character["skills"]))
    if not skill or skill not in SKILL_ATTRIBUTES:
        await interaction.response.send_message("Выберите навык персонажа из списка.", ephemeral=True)
        return
    blocked_by = injury_blocks_skill(character, skill)
    if blocked_by:
        await interaction.response.send_message(
            f'Травма «{blocked_by}» не позволяет использовать навык **{skill}**.',
            ephemeral=True,
        )
        return
    item = None
    gear: dict[int, int] = {}
    modifier_items: list[dict] = []
    if снаряжение:
        item = await bot.db.inventory_item_by_name(character["id"], снаряжение)
        if not item or item["durability"] <= 0 or not is_general_roll_gear(item):
            await interaction.response.send_message("Подходящее исправное снаряжение не найдено.", ephemeral=True)
            return
        gear[item["id"]] = int(item["durability"])
        modifier_items.append(item)
    equipped = [row for row in await bot.db.inventory(character["id"]) if row["equipped"]]
    modifier_items.extend(row for row in equipped if row["id"] not in {item["id"] for item in modifier_items})
    auto_modifier = equipment_skill_modifier(modifier_items, skill)
    effects = await bot.db.active_effects(character["id"])
    success_modifier = equipment_success_modifier(modifier_items, skill)
    success_modifier += talent_equipment_success_modifier(character, modifier_items, skill)
    success_modifier += active_success_modifier(effects, SKILL_ATTRIBUTES[skill])
    pool = make_pool(
        character, skill, бонус - штраф + auto_modifier, gear,
        success_modifier=success_modifier,
    )
    pool.skill_modifier_details = [
        detail for detail in pool.skill_modifier_details if detail[0] != "Прочие модификаторы"
    ]
    if auto_modifier:
        pool.skill_modifier_details.append(("Снаряжение", auto_modifier))
    if бонус:
        pool.skill_modifier_details.append(("Опциональный бонус", бонус))
    if штраф:
        pool.skill_modifier_details.append(("Опциональный штраф", -штраф))
    conditions = item["conditions"] if item else ""
    embed = pool_embed(pool, f"Проверка · {skill}", conditions)
    injury_damage = await apply_injury_roll_damage(character, skill)
    if injury_damage:
        embed.add_field(name="Последствие травмы", value=injury_damage, inline=False)
    await interaction.response.send_message(
        embed=embed,
        view=SkillRollView(interaction.user.id, character, pool, conditions, can_push=skill != "Стрельба"),
    )


@skill_roll_command.autocomplete("навык")
async def skill_roll_skill_autocomplete(interaction: discord.Interaction, current: str):
    character = await bot.db.character(interaction.guild_id, interaction.user.id)
    if not character:
        return []
    return [
        app_commands.Choice(name=name, value=name)
        for name in character["skills"]
        if current.casefold() in name.casefold()
    ][:25]


@skill_roll_command.autocomplete("снаряжение")
async def skill_roll_item_autocomplete(interaction: discord.Interaction, current: str):
    character = await bot.db.character(interaction.guild_id, interaction.user.id)
    if not character:
        return []
    return [
        app_commands.Choice(name=f'{item["name"]} · гир {item["durability"]}'[:100], value=item["name"])
        for item in await bot.db.inventory(character["id"])
        if current.casefold() in item["name"].casefold()
        and item["durability"] > 0
        and is_general_roll_gear(item)
    ][:25]


class NPCTargetAttackView(AttackView):
    def __init__(self, *args, target_npc: dict, **kwargs):
        super().__init__(*args, **kwargs)
        self.target_npc = target_npc

    @discord.ui.button(label="Завершить атаку", style=discord.ButtonStyle.success)
    async def finish_npc_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self.resolved or interaction.user.id != self.attacker_id:
            await interaction.response.send_message("Завершить атаку может только атакующий.", ephemeral=True)
            return
        self.resolved = True
        npc = await bot.db.npc(interaction.guild_id, self.target_npc["name"])
        if not npc:
            await interaction.response.send_message("Выбранный НПС больше не существует.", ephemeral=True)
            return
        current = int(npc["physique"] if self.target_attribute == "Телосложение" else npc["agility"])
        maximum = int(npc["physique_max"] if self.target_attribute == "Телосложение" else npc["agility_max"])
        if current <= 0:
            await interaction.response.send_message("Этот НПС уже выведен из строя.", ephemeral=True)
            return
        armor_dice = d6(int(npc["defense"]))
        shield_dice = d6(int(npc["shield"]))
        indestructible_dice = d6(int(npc["indestructible_defense"]))
        defense_successes = sum(value == 6 for value in armor_dice + shield_dice + indestructible_dice)
        net = max(0, self.attack_successes - defense_successes)
        damage_factor = max(1, int(self.weapon["damage"])) if self.weapon else 1
        raw_damage = max(0, net * damage_factor + self.damage_modifier)
        damage_type = str(self.weapon.get("damage_type") or "") if self.weapon else ""
        reductions = json.loads(npc.get("damage_reductions") or "{}")
        reduction = max(0, int(reductions.get(damage_type, 0)))
        damage = max(0, raw_damage - reduction)
        before, after = await bot.db.damage_npc_attribute(npc["id"], self.target_attribute, damage)
        armor_change = shield_change = None
        if damage > 0:
            if int(npc["defense"]) > 0:
                armor_change = await bot.db.adjust_npc_protection(npc["id"], "Броня", -1)
            if int(npc["shield"]) > 0:
                shield_change = await bot.db.adjust_npc_protection(npc["id"], "Щит", -1)
        embed = self.attack_embed()
        lines = [
            f'Броня: {colored_dice(armor_dice, "gear")}',
            f'Щит: {colored_dice(shield_dice, "gear")}',
            f'Неразрушимая защита: {colored_dice(indestructible_dice, "gear")}',
            f'Успехов защиты: **{defense_successes}**',
            f'Незаблокированных успехов: **{net}**',
        ]
        if self.damage_modifier:
            lines.append(f'Модификатор урона: **{self.damage_modifier:+d}**')
        if reduction:
            lines.append(f'Снижение {damage_type}: **−{min(raw_damage, reduction)}**')
        lines.extend((f'Урон: **{damage}**', f'{self.target_attribute}: **{before}/{maximum} → {after}/{maximum}**'))
        if armor_change:
            lines.append(f'Броня повреждена: **{armor_change[0]} → {armor_change[1]}**')
        if shield_change:
            lines.append(f'Щит повреждён: **{shield_change[0]} → {shield_change[1]}**')
        embed.add_field(name=f'Автоматическая защита · {npc["name"]}', value="\n".join(lines), inline=False)
        await interaction.response.edit_message(embed=embed, view=None)


async def send_attack(
    interaction: discord.Interaction,
    target: discord.Member | None,
    weapon: dict | None,
    ranged: bool,
    shots: int,
    bonus: int,
    penalty: int,
    distance: str | None = None,
    distance_modifier: int = 0,
    npc: dict | None = None,
    target_attribute: str = "Телосложение",
    damage_bonus: int = 0,
    damage_penalty: int = 0,
):
    if target and target.id == interaction.user.id:
        await interaction.response.send_message("Нельзя атаковать собственного персонажа.", ephemeral=True)
        return
    attacker = await bot.db.character(interaction.guild_id, interaction.user.id)
    target_character = await bot.db.character(interaction.guild_id, target.id) if target else None
    if not attacker or (target and not target_character):
        await interaction.response.send_message("У атакующего или выбранной цели нет зарегистрированного персонажа.", ephemeral=True)
        return
    if await reject_unfinished_skills(interaction, attacker):
        return
    skill = "Стрельба" if ranged else "Драка"
    blocked_by = injury_blocks_skill(attacker, skill)
    if blocked_by:
        await interaction.response.send_message(
            f'Травма «{blocked_by}» не позволяет использовать навык **{skill}**.', ephemeral=True,
        )
        return
    two_handed_block = injury_blocks_two_handed(attacker)
    if weapon and int(weapon.get("hands") or 0) >= 2 and two_handed_block:
        await interaction.response.send_message(
            f'Травма «{two_handed_block}» не позволяет использовать двуручное оружие.', ephemeral=True,
        )
        return
    if ranged:
        fire_rate = max(1, int(weapon["fire_rate"] or 1))
        if shots > fire_rate:
            await interaction.response.send_message(f"Скорострельность оружия — {fire_rate}. Уменьшите число выстрелов.", ephemeral=True)
            return
        ammo_left = await bot.db.consume_ammo(weapon["id"], attacker["id"], shots)
        if ammo_left is None:
            await interaction.response.send_message(f"Для атаки требуется {shots} патронов.", ephemeral=True)
            return
    gear = {weapon["id"]: int(weapon["durability"])} if weapon else {}
    equipped_items = [row for row in await bot.db.inventory(attacker["id"]) if row["equipped"]]
    auto_modifier = equipment_skill_modifier(equipped_items, skill)
    if weapon:
        auto_modifier += int(weapon.get("attachment_skill_bonus") or 0)
        gear[weapon["id"]] = max(0, gear[weapon["id"]] + int(weapon.get("attachment_gear_modifier") or 0))
    if not ranged:
        auto_modifier += int(talent_effect(attacker, "familiar_melee", 0) or 0)
    if (
        has_talent(attacker, "Броневая связка")
        and any(item["category"] == "Броня" and item["size"] == "Большой" for item in equipped_items)
        and any(item["category"] == "Броня" and item["size"] == "Малый" for item in equipped_items)
    ):
        auto_modifier += 1
    automatic_damage_bonus = 0
    attribute_override = None
    weapon_text = " ".join(
        str(weapon.get(key) or "") for key in ("properties", "conditions")
    ).casefold() if weapon else ""
    if not ranged and weapon and "тяж" in weapon_text and has_talent(attacker, "Могучий удар"):
        auto_modifier += 2
        automatic_damage_bonus += 1
    if (
        not ranged and weapon and "точн" in weapon_text
        and talent_effect(attacker, "precise_melee_agility", False)
    ):
        attribute_override = "Ловкость"
    if ranged and distance == "Дальняя":
        automatic_damage_bonus += int(talent_effect(attacker, "long_range_damage", 0) or 0)
    if ranged and weapon:
        familiar = {
            "Д": "Привычное оружие: Дробовики",
            "В": "Привычное оружие: Винтовки",
            "П": "Привычное оружие: Пистолеты",
        }.get(ammo_code(weapon.get("conditions") or ""))
        if familiar and has_talent(attacker, familiar):
            auto_modifier += 2
    active_effects = await bot.db.active_effects(attacker["id"])
    success_modifier = equipment_success_modifier(equipped_items, skill)
    success_modifier += talent_equipment_success_modifier(attacker, equipped_items, skill)
    success_modifier += active_success_modifier(active_effects, SKILL_ATTRIBUTES[skill])
    pools = []
    for shot_index in range(shots):
        pool = make_pool(
            attacker, skill, bonus - penalty + auto_modifier + distance_modifier - shot_index, gear,
            success_modifier=success_modifier,
            attribute_override=attribute_override,
        )
        if weapon and int(weapon.get("attachment_skill_bonus") or 0):
            pool.skill_modifier_details.append(("Насадки", int(weapon["attachment_skill_bonus"])))
        if shot_index:
            pool.skill_modifier_details.append((f"\u041f\u043e\u0441\u043b\u0435\u0434\u0443\u044e\u0449\u0438\u0439 \u0432\u044b\u0441\u0442\u0440\u0435\u043b \u2116{shot_index + 1}", -shot_index))
        pools.append(pool)
    if npc:
        npc_current = int(npc["physique"] if target_attribute == "Телосложение" else npc["agility"])
        if npc_current <= 0:
            await interaction.response.send_message("Этот НПС уже выведен из строя.", ephemeral=True)
            return
        injury_damage = await apply_injury_roll_damage(attacker, skill)
        view = NPCTargetAttackView(
            interaction.user.id, None, attacker, pools, weapon, ranged,
            distance, distance_modifier, target_attribute,
            damage_modifier=damage_bonus - damage_penalty + automatic_damage_bonus,
            target_npc=npc,
        )
        embed = view.attack_embed()
        if injury_damage:
            embed.add_field(name="Последствие травмы атакующего", value=injury_damage, inline=False)
        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()
        return
    injury_damage = await apply_injury_roll_damage(attacker, skill)
    view = AttackView(
        interaction.user.id,
        target.id if target else None,
        attacker,
        pools,
        weapon,
        ranged,
        distance,
        distance_modifier,
        target_attribute,
        damage_modifier=damage_bonus - damage_penalty + automatic_damage_bonus,
    )
    embed = view.attack_embed()
    if injury_damage:
        embed.add_field(name="Последствие травмы атакующего", value=injury_damage, inline=False)
    await interaction.response.send_message(
        content=f"{target.mention}, по вашему персонажу проводится атака." if target else None,
        embed=embed,
        view=view,
    )
    view.message = await interaction.original_response()
    if target:
        PENDING_ATTACKS[(interaction.guild_id, target.id)] = view


async def weapon_choices(interaction: discord.Interaction, current: str, category: str):
    character = await bot.db.character(interaction.guild_id, interaction.user.id)
    if not character:
        return []
    items = [
        item for item in await bot.db.inventory(character["id"])
        if item["equipped"] and item["durability"] > 0
        and (item["category"] == category or (category == "Оружие ближнего боя" and int(item.get("attachment_melee_damage") or 0) > 0))
        and current.casefold() in item["name"].casefold()
    ]
    return [
        app_commands.Choice(
            name=f'{item["name"]} · гир {item["durability"]} · БП {item["ammo"] if item["ammo"] is not None else "—"}'[:100],
            value=item["name"],
        )
        for item in items[:25]
    ]


async def npc_choices(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(
            name=(
                f'{npc["name"]} · Т {npc["physique"]}/{npc["physique_max"]} · '
                f'Л {npc["agility"]}/{npc["agility_max"]} · защита {npc["defense"]}'
            )[:100],
            value=npc["name"],
        )
        for npc in (await bot.db.npcs(interaction.guild_id, current))[:25]
    ]


def shooting_distance_modifier(weapon: dict, distance: str) -> int:
    ammo = ammo_code(weapon.get("conditions") or "")
    if ammo == "Д":
        return {"Нулевая": -1, "Ближняя": 0, "Средняя": -3, "Дальняя": -3}[distance]
    if ammo == "В":
        return {"Нулевая": -3, "Ближняя": 0, "Средняя": -1, "Дальняя": -2}[distance]
    return 0


def shooting_talent_distance_modifier(character: dict, modifier: int) -> int:
    if has_talent(character, "Глазомер") and modifier < 0:
        return min(0, modifier + 1)
    return modifier


@bot.tree.command(name="ударить", description="Провести атаку в ближнем бою")
@app_commands.choices(характеристика=[
    app_commands.Choice(name="Телосложение", value="Телосложение"),
    app_commands.Choice(name="Ловкость", value="Ловкость"),
])
async def melee_attack(
    interaction: discord.Interaction,
    характеристика: app_commands.Choice[str],
    цель: discord.Member | None = None,
    оружие: str | None = None,
    бонус: app_commands.Range[int, 0, 20] = 0,
    штраф: app_commands.Range[int, 0, 20] = 0,
    бонус_к_урону: app_commands.Range[int, 0, 20] = 0,
    штраф_к_урону: app_commands.Range[int, 0, 20] = 0,
    нпс: str | None = None,
):
    if цель and нпс:
        await interaction.response.send_message("Выберите либо участника, либо НПС.", ephemeral=True)
        return
    attacker = await bot.db.character(interaction.guild_id, interaction.user.id)
    weapon = await bot.db.inventory_item_by_name(attacker["id"], оружие, equipped_only=True) if attacker and оружие else None
    if оружие and (not weapon or (weapon["category"] != "Оружие ближнего боя" and int(weapon.get("attachment_melee_damage") or 0) <= 0) or weapon["durability"] <= 0):
        await interaction.response.send_message("Выберите исправное экипированное оружие ближнего боя или оружие со штыком.", ephemeral=True)
        return
    if weapon and weapon["category"] == "Оружие дальнего боя" and int(weapon.get("attachment_melee_damage") or 0) > 0:
        weapon = dict(weapon)
        weapon["damage"] = int(weapon["attachment_melee_damage"])
        weapon["damage_type"] = "Колющий"
    npc = await bot.db.npc(interaction.guild_id, нпс) if нпс else None
    if нпс and not npc:
        await interaction.response.send_message("Выбранный НПС не найден.", ephemeral=True)
        return
    await send_attack(
        interaction, цель, weapon, False, 1, бонус, штраф,
        npc=npc, target_attribute=характеристика.value,
        damage_bonus=бонус_к_урону, damage_penalty=штраф_к_урону,
    )


@melee_attack.autocomplete("оружие")
async def melee_weapon_autocomplete(interaction: discord.Interaction, current: str):
    return await weapon_choices(interaction, current, "Оружие ближнего боя")


@melee_attack.autocomplete("нпс")
async def melee_npc_autocomplete(interaction: discord.Interaction, current: str):
    return await npc_choices(interaction, current)


@bot.tree.command(name="выстрелить", description="Выстрелить из экипированного оружия")
@app_commands.choices(дистанция=[
    app_commands.Choice(name="Нулевая дистанция", value="Нулевая"),
    app_commands.Choice(name="Ближняя дистанция", value="Ближняя"),
    app_commands.Choice(name="Средняя дистанция", value="Средняя"),
    app_commands.Choice(name="Дальняя дистанция", value="Дальняя"),
], характеристика=[
    app_commands.Choice(name="Телосложение", value="Телосложение"),
    app_commands.Choice(name="Ловкость", value="Ловкость"),
])
async def ranged_attack(
    interaction: discord.Interaction,
    оружие: str,
    выстрелы: app_commands.Range[int, 1, 20],
    дистанция: app_commands.Choice[str],
    характеристика: app_commands.Choice[str],
    цель: discord.Member | None = None,
    бонус: app_commands.Range[int, 0, 20] = 0,
    штраф: app_commands.Range[int, 0, 20] = 0,
    бонус_к_урону: app_commands.Range[int, 0, 20] = 0,
    штраф_к_урону: app_commands.Range[int, 0, 20] = 0,
    нпс: str | None = None,
):
    if цель and нпс:
        await interaction.response.send_message("Выберите либо участника, либо НПС.", ephemeral=True)
        return
    attacker = await bot.db.character(interaction.guild_id, interaction.user.id)
    weapon = await bot.db.inventory_item_by_name(attacker["id"], оружие, equipped_only=True) if attacker else None
    if not weapon or weapon["category"] != "Оружие дальнего боя" or weapon["durability"] <= 0:
        await interaction.response.send_message("Выберите исправное экипированное оружие дальнего боя.", ephemeral=True)
        return
    distance_modifier = shooting_distance_modifier(weapon, дистанция.value)
    distance_modifier = shooting_talent_distance_modifier(attacker, distance_modifier)
    if дистанция.value == "Нулевая" and talent_effect(attacker, "ignore_zero_range", False):
        distance_modifier = 0
    if (
        дистанция.value in {"Средняя", "Дальняя"}
        and talent_effect(attacker, "ignore_medium_long_range", False)
    ):
        distance_modifier = 0
    npc = await bot.db.npc(interaction.guild_id, нпс) if нпс else None
    if нпс and not npc:
        await interaction.response.send_message("Выбранный НПС не найден.", ephemeral=True)
        return
    await send_attack(
        interaction,
        цель,
        weapon,
        True,
        выстрелы,
        бонус,
        штраф,
        дистанция.value,
        distance_modifier,
        npc,
        характеристика.value,
        бонус_к_урону,
        штраф_к_урону,
    )


@ranged_attack.autocomplete("оружие")
async def ranged_weapon_autocomplete(interaction: discord.Interaction, current: str):
    return await weapon_choices(interaction, current, "Оружие дальнего боя")


@ranged_attack.autocomplete("нпс")
async def ranged_npc_autocomplete(interaction: discord.Interaction, current: str):
    return await npc_choices(interaction, current)


@bot.tree.command(name="перезарядить", description="Полностью перезарядить экипированное огнестрельное оружие")
async def reload_command(interaction: discord.Interaction, оружие: str):
    character = await bot.db.character(interaction.guild_id, interaction.user.id)
    if not character:
        await interaction.response.send_message("Сначала зарегистрируйте персонажа.", ephemeral=True)
        return
    weapon = await bot.db.inventory_item_by_name(character["id"], оружие, equipped_only=True)
    if not weapon or weapon["category"] != "Оружие дальнего боя" or weapon["ammo_max"] is None:
        await interaction.response.send_message(
            "Выберите экипированное огнестрельное оружие из списка.",
            ephemeral=True,
        )
        return
    if int(weapon["ammo"] or 0) >= int(weapon["ammo_max"]):
        await interaction.response.send_message("Боезапас оружия уже полный.", ephemeral=True)
        return
    code = ammo_code(weapon["conditions"])
    ammo_name = AMMO_ITEM_NAMES.get(code)
    if not ammo_name:
        await interaction.response.send_message(
            "Для этого оружия не определён тип боеприпаса.",
            ephemeral=True,
        )
        return
    result = await bot.db.reload_weapon(weapon["id"], character["id"], ammo_name)
    if result is None:
        await interaction.response.send_message(
            f'В инвентаре нет предмета «{ammo_name}». Для перезарядки нужен 1 комплект.',
            ephemeral=True,
        )
        return
    before, maximum = result
    await interaction.response.send_message(
        f'**{weapon["name"]}** перезаряжено: **{before}/{maximum} → {maximum}/{maximum}**.\n'
        f'Из инвентаря удалён 1 предмет «{ammo_name}».'
    )


@reload_command.autocomplete("оружие")
async def reload_weapon_autocomplete(interaction: discord.Interaction, current: str):
    character = await bot.db.character(interaction.guild_id, interaction.user.id)
    if not character:
        return []
    return [
        app_commands.Choice(
            name=f'{item["name"]} · БП {item["ammo"]}/{item["ammo_max"]}'[:100],
            value=item["name"],
        )
        for item in await bot.db.inventory(character["id"])
        if item["equipped"]
        and item["durability"] > 0
        and item["category"] == "Оружие дальнего боя"
        and item["ammo_max"] is not None
        and current.casefold() in item["name"].casefold()
    ][:25]


async def admin_ammo_change(
    interaction: discord.Interaction,
    member: discord.Member,
    weapon_name: str,
    amount: int,
):
    character = await bot.db.character(interaction.guild_id, member.id)
    if not character:
        await interaction.response.send_message("У участника нет персонажа.", ephemeral=True)
        return
    weapon = await bot.db.inventory_item_by_name(character["id"], weapon_name)
    if not weapon or weapon["category"] != "Оружие дальнего боя" or weapon["ammo_max"] is None:
        await interaction.response.send_message("Огнестрельное оружие не найдено.", ephemeral=True)
        return
    result = await bot.db.adjust_inventory_ammo(
        weapon["id"], character["id"], amount
    )
    if not result:
        await interaction.response.send_message("Боезапас изменить не удалось.", ephemeral=True)
        return
    before, after, maximum = result
    await interaction.response.send_message(
        f'{member.mention} · **{weapon["name"]}**: боезапас '
        f'**{before}/{maximum} → {after}/{maximum}**.'
    )


@bot.tree.command(name="разрядить", description="Убавить патроны в выбранном оружии персонажа")
@app_commands.check(require_master_access)
async def unload_weapon_command(
    interaction: discord.Interaction,
    участник: discord.Member,
    оружие: str,
    количество: app_commands.Range[int, 1, 999],
):
    await admin_ammo_change(interaction, участник, оружие, -количество)


@bot.tree.command(name="дозарядить", description="Добавить патроны в выбранное оружие персонажа")
@app_commands.check(require_master_access)
async def top_up_weapon_command(
    interaction: discord.Interaction,
    участник: discord.Member,
    оружие: str,
    количество: app_commands.Range[int, 1, 999],
):
    await admin_ammo_change(interaction, участник, оружие, количество)


async def member_weapon_autocomplete(interaction: discord.Interaction, current: str):
    member = getattr(interaction.namespace, "участник", None)
    if not member or not getattr(member, "id", None):
        return []
    character = await bot.db.character(interaction.guild_id, member.id)
    if not character:
        return []
    return [
        app_commands.Choice(
            name=f'{item["name"]} · {item["ammo"]}/{item["ammo_max"]}'[:100],
            value=item["name"],
        )
        for item in await bot.db.inventory(character["id"])
        if item["category"] == "Оружие дальнего боя"
        and item["ammo_max"] is not None
        and current.casefold() in item["name"].casefold()
    ][:25]


@unload_weapon_command.autocomplete("оружие")
async def unload_weapon_autocomplete(interaction: discord.Interaction, current: str):
    return await member_weapon_autocomplete(interaction, current)


@top_up_weapon_command.autocomplete("оружие")
async def top_up_weapon_autocomplete(interaction: discord.Interaction, current: str):
    return await member_weapon_autocomplete(interaction, current)


async def send_gm_attack(
    interaction: discord.Interaction,
    target: discord.Member,
    attack_name: str,
    dice_count: int,
    negative_count: int,
    damage: int,
    attacks: int,
    target_attribute: str,
    damage_type: str,
):
    target_character = await bot.db.character(interaction.guild_id, target.id)
    if not target_character:
        await interaction.response.send_message("У цели нет зарегистрированного персонажа.", ephemeral=True)
        return
    pools = [
        RollPool(
            attribute="Кубы атаки",
            skill="",
            attribute_dice=d6(dice_count),
            skill_dice=[],
            negative_dice=d6(negative_count),
        )
        for _ in range(attacks)
    ]
    weapon = {
        "name": attack_name,
        "damage": damage,
        "conditions": "Атака мастера",
        "damage_type": damage_type,
        "properties": "",
    }
    view = AttackView(
        interaction.user.id, target.id, {}, pools, weapon, True,
        target_attribute=target_attribute,
    )
    await interaction.response.send_message(
        content=f"{target.mention}, по вашему персонажу проводится атака мастера.",
        embed=view.attack_embed(),
        view=view,
    )
    view.message = await interaction.original_response()
    PENDING_ATTACKS[(interaction.guild_id, target.id)] = view


@bot.tree.command(name="гм-атака", description="Создать произвольную атаку мастера по персонажу")
@app_commands.choices(
    характеристика=[app_commands.Choice(name=name, value=name) for name in ATTRIBUTES],
    тип_урона=[app_commands.Choice(name=name, value=name) for name in ("Дробящий", "Колющий", "Режущий", "Огненный")],
)
@app_commands.check(require_master_access)
async def gm_attack_command(
    interaction: discord.Interaction,
    цель: discord.Member,
    название: str,
    кубы: app_commands.Range[int, 0, 50],
    урон: app_commands.Range[int, 1, 20],
    характеристика: app_commands.Choice[str],
    тип_урона: app_commands.Choice[str],
    атаки: app_commands.Range[int, 1, 10] = 1,
    отрицательные: app_commands.Range[int, 0, 50] = 0,
):
    await send_gm_attack(
        interaction, цель, название, кубы, отрицательные, урон, атаки,
        характеристика.value, тип_урона.value,
    )


def consumable_healing(item: dict) -> tuple[int, set[str]]:
    text = str(item.get("conditions") or "")
    match = re.search(r"восстанавлива\w*\s+(\d+)\s+пункт\w*\s+(.+?)(?:[.;]|$)", text, re.IGNORECASE)
    if not match:
        return 0, set()
    allowed = {name for name in ATTRIBUTES if name.casefold() in match.group(2).casefold()}
    return int(match.group(1)), allowed


@bot.tree.command(name="использовать-расходник", description="Применить расходный предмет из своего инвентаря")
@app_commands.choices(характеристика=[
    app_commands.Choice(name=name, value=name) for name in ATTRIBUTES
])
async def use_consumable_command(
    interaction: discord.Interaction,
    предмет: str,
    характеристика: app_commands.Choice[str] | None = None,
):
    character = await bot.db.character(interaction.guild_id, interaction.user.id)
    if not character:
        await interaction.response.send_message("Сначала зарегистрируйте персонажа.", ephemeral=True)
        return
    item = await bot.db.inventory_item_by_name(character["id"], предмет)
    if not item or "расходник" not in str(item.get("properties") or "").casefold():
        await interaction.response.send_message("Такого расходника нет в вашем инвентаре.", ephemeral=True)
        return
    text = str(item.get("conditions") or item.get("description") or "")
    name_text = str(item["name"]).casefold()
    is_explosive = any(word in name_text for word in ("гранат", "мина", "динамит", "заряд"))
    lines = [f'**{item["name"]}**', short(text, 1500)]

    if is_explosive:
        rolls = d6(int(item.get("gear") or item.get("durability") or 1))
        successes = sum(value == 6 for value in rolls)
        damage = successes * max(0, int(item.get("damage") or 0))
        lines.extend((
            f'Кубы взрыва: {colored_dice(rolls, "gear")}',
            f'Успехов: **{successes}**',
            f'Итоговый урон: **{damage}**',
        ))
    else:
        will_match = re.search(r"восстанавлива\w*\s+(\d+)\s+[Вв]ол", text)
        if will_match:
            restored = int(will_match.group(1))
            before = int(character["will_current"])
            after = min(int(character["will_max"]), before + restored)
            await bot.db.update_character(character["id"], "will_current", after)
            lines.append(f"Воля: **{before}/{character['will_max']} → {after}/{character['will_max']}**")
        if "следующий эффект потери Воли уменьшается" in text:
            guard_match = re.search(r"потери Воли уменьшается на (\d+)", text)
            guard = int(guard_match.group(1)) if guard_match else 1
            await bot.db.add_timed_effect(
                character["id"], item["name"], "Воля", guard, 24, text
            )
            lines.append(f"Следующая потеря Воли уменьшается на **{guard}**.")

        healing, allowed = consumable_healing(item)
        if healing:
            if not характеристика or характеристика.value not in allowed:
                choices = ", ".join(sorted(allowed))
                await interaction.response.send_message(
                    f"Для этого предмета выберите характеристику: **{choices}**.",
                    ephemeral=True,
                )
                return
            attribute = характеристика.value
            before = int(character["attributes"][attribute]["current"])
            after = await bot.db.heal(character["id"], attribute, healing)
            lines.append(f"{attribute}: **{before} → {after}**")

        penalty = re.search(
            r"(\d+)\s+час\w*\s+проверки\s+(.+?)\s+требуют\s+на\s+(\d+)\s+успех",
            text,
            re.IGNORECASE,
        )
        if penalty:
            hours, raw_attributes, amount = int(penalty.group(1)), penalty.group(2), int(penalty.group(3))
            affected = [name for name in ATTRIBUTES if name.casefold() in raw_attributes.casefold()]
            for attribute in affected:
                await bot.db.add_timed_effect(
                    character["id"], item["name"], attribute, -amount, hours, text
                )
            if affected:
                lines.append(
                    f'Временный модификатор: **−{amount}** к кубам '
                    f'{", ".join(affected)} на {hours} ч.'
                )

    removed = await bot.db.remove_inventory_by_name(character["id"], item["name"], 1)
    if not removed:
        await interaction.response.send_message("Расходник уже отсутствует.", ephemeral=True)
        return
    await interaction.response.send_message(
        embed=discord.Embed(
            title="Расходник применён",
            description="\n\n".join(lines),
            color=0x7A5B35,
        )
    )


@use_consumable_command.autocomplete("предмет")
async def consumable_autocomplete(interaction: discord.Interaction, current: str):
    character = await bot.db.character(interaction.guild_id, interaction.user.id)
    if not character:
        return []
    return [
        app_commands.Choice(
            name=f'{item["name"]} ×{item["quantity"]}'[:100],
            value=item["name"],
        )
        for item in await bot.db.inventory(character["id"])
        if "расходник" in str(item.get("properties") or "").casefold()
        and current.casefold() in item["name"].casefold()
    ][:25]


@bot.tree.command(name="нпс-создать", description="Создать или обновить боевую карточку НПС")
@app_commands.choices(
    тип_ближний=[app_commands.Choice(name=name, value=name) for name in ("Дробящий", "Колющий", "Режущий", "Огненный")],
    тип_дальний=[app_commands.Choice(name=name, value=name) for name in ("Дробящий", "Колющий", "Режущий", "Огненный")],
    тип_снижения=[app_commands.Choice(name=name, value=name) for name in ("Дробящий", "Колющий", "Режущий", "Огненный", "Взрывной", "Кислотный")],
)
@app_commands.check(require_master_access)
async def npc_create_command(
    interaction: discord.Interaction,
    имя: str,
    телосложение: app_commands.Range[int, 1, 99],
    ловкость: app_commands.Range[int, 1, 99],
    защита: app_commands.Range[int, 0, 50],
    драка: app_commands.Range[int, -20, 20],
    стрельба: app_commands.Range[int, -20, 20],
    урон_ближний: app_commands.Range[int, 1, 20],
    урон_дальний: app_commands.Range[int, 1, 20],
    тип_ближний: app_commands.Choice[str],
    тип_дальний: app_commands.Choice[str],
    описание: str = "",
    щит: app_commands.Range[int, 0, 50] = 0,
    неразрушимая_защита: app_commands.Range[int, 0, 50] = 0,
    тип_снижения: app_commands.Choice[str] | None = None,
    снижение_урона: app_commands.Range[int, 0, 50] = 0,
):
    await bot.db.create_npc(
        interaction.guild_id, имя.strip(), телосложение, ловкость, защита,
        драка, стрельба, урон_ближний, урон_дальний,
        тип_ближний.value, тип_дальний.value, описание, щит, неразрушимая_защита,
        json.dumps({тип_снижения.value: снижение_урона}, ensure_ascii=False)
        if тип_снижения and снижение_урона else "{}",
    )
    await interaction.response.send_message(
        f'НПС **{имя.strip()}** сохранён: Телосложение {телосложение}, '
        f'Ловкость {ловкость}, защита {защита}, Драка {драка} ({тип_ближний.value}), '
        f'Стрельба {стрельба} ({тип_дальний.value}), щит {щит}, '
        f'неразрушимая защита {неразрушимая_защита}.',
        ephemeral=True,
    )


async def send_npc_attack(
    interaction: discord.Interaction,
    npc: dict,
    target: discord.Member,
    ranged: bool,
    attacks: int,
    bonus: int,
    penalty: int,
    target_attribute: str,
):
    attribute = "Ловкость" if ranged else "Телосложение"
    attribute_value = int(npc["agility"] if ranged else npc["physique"])
    skill_name = "Стрельба" if ranged else "Драка"
    skill_value = int(npc["shooting_skill"] if ranged else npc["fight_skill"]) + bonus - penalty
    pools = []
    for attack_index in range(attacks):
        adjusted_skill = skill_value - attack_index
        pools.append(RollPool(
            attribute=attribute,
            skill=skill_name,
            attribute_dice=d6(attribute_value),
            skill_dice=d6(max(0, adjusted_skill)),
            negative_dice=d6(max(0, -adjusted_skill)),
            skill_modifier_details=([(f"\u041f\u043e\u0441\u043b\u0435\u0434\u0443\u044e\u0449\u0438\u0439 \u0432\u044b\u0441\u0442\u0440\u0435\u043b \u2116{attack_index + 1}", -attack_index)] if ranged and attack_index else []),
        ))
    damage = int(npc["ranged_damage"] if ranged else npc["melee_damage"])
    weapon = {
        "name": f'{npc["name"]} · {skill_name}',
        "damage": damage,
        "conditions": f'Атака НПС · {skill_name} {skill_value:+d}',
        "damage_type": npc["ranged_damage_type"] if ranged else npc["melee_damage_type"],
        "properties": "",
    }
    view = AttackView(
        interaction.user.id,
        target.id,
        {},
        pools,
        weapon,
        ranged,
        target_attribute=target_attribute,
        attacker_npc=npc,
    )
    await interaction.response.send_message(
        content=f'{target.mention}, вас атакует НПС **{npc["name"]}**.',
        embed=view.attack_embed(),
        view=view,
    )
    view.message = await interaction.original_response()
    PENDING_ATTACKS[(interaction.guild_id, target.id)] = view


@bot.tree.command(name="нпс-ударить", description="Ударить персонажа от имени НПС")
@app_commands.choices(характеристика=[
    app_commands.Choice(name=name, value=name) for name in ATTRIBUTES
])
@app_commands.check(require_master_access)
async def npc_melee_command(
    interaction: discord.Interaction,
    нпс: str,
    цель: discord.Member,
    характеристика: app_commands.Choice[str],
    бонус: app_commands.Range[int, 0, 20] = 0,
    штраф: app_commands.Range[int, 0, 20] = 0,
):
    npc = await bot.db.npc(interaction.guild_id, нпс)
    if not npc:
        await interaction.response.send_message("НПС не найден.", ephemeral=True)
        return
    await send_npc_attack(
        interaction, npc, цель, False, 1, бонус, штраф, характеристика.value
    )


@npc_melee_command.autocomplete("нпс")
async def npc_melee_autocomplete(interaction: discord.Interaction, current: str):
    return await npc_choices(interaction, current)


@bot.tree.command(name="нпс-выстрелить", description="Выстрелить в персонажа от имени НПС")
@app_commands.choices(характеристика=[
    app_commands.Choice(name=name, value=name) for name in ATTRIBUTES
])
@app_commands.check(require_master_access)
async def npc_ranged_command(
    interaction: discord.Interaction,
    нпс: str,
    цель: discord.Member,
    характеристика: app_commands.Choice[str],
    выстрелы: app_commands.Range[int, 1, 10] = 1,
    бонус: app_commands.Range[int, 0, 20] = 0,
    штраф: app_commands.Range[int, 0, 20] = 0,
):
    npc = await bot.db.npc(interaction.guild_id, нпс)
    if not npc:
        await interaction.response.send_message("НПС не найден.", ephemeral=True)
        return
    await send_npc_attack(
        interaction, npc, цель, True, выстрелы, бонус, штраф, характеристика.value
    )


@npc_ranged_command.autocomplete("нпс")
async def npc_ranged_autocomplete(interaction: discord.Interaction, current: str):
    return await npc_choices(interaction, current)


@bot.tree.command(name="защита-нпс", description="Повредить или починить броню, щит либо неразрушимую защиту НПС")
@app_commands.choices(
    вид=[app_commands.Choice(name=name, value=name) for name in ("Броня", "Щит", "Неразрушимая защита")],
    действие=[
        app_commands.Choice(name="Починить", value="плюс"),
        app_commands.Choice(name="Повредить", value="минус"),
    ],
)
@app_commands.check(require_master_access)
async def npc_protection_command(
    interaction: discord.Interaction,
    нпс: str,
    вид: app_commands.Choice[str],
    действие: app_commands.Choice[str],
    количество: app_commands.Range[int, 1, 50],
):
    npc = await bot.db.npc(interaction.guild_id, нпс)
    if not npc:
        await interaction.response.send_message("НПС не найден.", ephemeral=True)
        return
    delta = количество if действие.value == "плюс" else -количество
    result = await bot.db.adjust_npc_protection(npc["id"], вид.value, delta)
    before, after = result
    await interaction.response.send_message(
        f'НПС **{npc["name"]}** · {вид.value}: **{before} → {after}**.'
    )


@npc_protection_command.autocomplete("нпс")
async def npc_protection_autocomplete(interaction: discord.Interaction, current: str):
    return await npc_choices(interaction, current)


@bot.tree.command(name="снижение-урона-нпс", description="Задать НПС постоянное снижение выбранного типа урона")
@app_commands.choices(
    тип=[app_commands.Choice(name=name, value=name) for name in ("Дробящий", "Колющий", "Режущий", "Огненный", "Взрывной", "Кислотный")]
)
@app_commands.check(require_master_access)
async def npc_reduction_command(
    interaction: discord.Interaction,
    нпс: str,
    тип: app_commands.Choice[str],
    количество: app_commands.Range[int, 0, 50],
):
    npc = await bot.db.npc(interaction.guild_id, нпс)
    if not npc:
        await interaction.response.send_message("НПС не найден.", ephemeral=True)
        return
    await bot.db.set_npc_damage_reduction(npc["id"], тип.value, количество)
    await interaction.response.send_message(
        f'НПС **{npc["name"]}**: снижение типа **{тип.value}** = **{количество}**.'
    )


@npc_reduction_command.autocomplete("нпс")
async def npc_reduction_autocomplete(interaction: discord.Interaction, current: str):
    return await npc_choices(interaction, current)


@bot.tree.command(name="нпс-список", description="Показать боевые карточки НПС")
@app_commands.check(require_master_access)
async def npc_list_command(interaction: discord.Interaction):
    npcs = await bot.db.npcs(interaction.guild_id)
    lines = [
        f'**{npc["name"]}** · Телосложение {npc["physique"]}/{npc["physique_max"]} · '
        f'Ловкость {npc["agility"]}/{npc["agility_max"]} · '
        f'броня {npc["defense"]}/{npc["defense_max"]} · '
        f'щит {npc["shield"]}/{npc["shield_max"]} · '
        f'неразрушимая {npc["indestructible_defense"]}/{npc["indestructible_defense_max"]}\n'
        f'Драка {npc["fight_skill"]} · урон {npc["melee_damage"]} · '
        f'Стрельба {npc["shooting_skill"]} · урон {npc["ranged_damage"]}'
        + (f'\n{npc["description"]}' if npc["description"] else "")
        for npc in npcs
    ]
    await interaction.response.send_message(
        embed=discord.Embed(
            title="НПС",
            description=short("\n\n────────────\n\n".join(lines) or "НПС ещё не созданы", 4000),
            color=0x6E654F,
        ),
        ephemeral=True,
    )


@bot.tree.command(name="нпс-удалить", description="Удалить боевую карточку НПС")
@app_commands.check(require_master_access)
async def npc_delete_command(interaction: discord.Interaction, нпс: str):
    deleted = await bot.db.delete_npc(interaction.guild_id, нпс)
    await interaction.response.send_message(
        "НПС удалён." if deleted else "НПС не найден.",
        ephemeral=True,
    )


@npc_delete_command.autocomplete("нпс")
async def npc_delete_autocomplete(interaction: discord.Interaction, current: str):
    return await npc_choices(interaction, current)


@bot.tree.command(name="защита", description="Ответить на последнюю направленную на вас атаку")
@app_commands.choices(действие=[
    app_commands.Choice(name="Защищаться", value="defend"),
    app_commands.Choice(name="Отказаться", value="refuse"),
])
async def defense_command(
    interaction: discord.Interaction,
    действие: app_commands.Choice[str],
    кубы_защиты: app_commands.Range[int, -20, 20] = 0,
    снижение_урона: app_commands.Range[int, -20, 20] = 0,
):
    view = PENDING_ATTACKS.get((interaction.guild_id, interaction.user.id))
    if not view or view.resolved:
        await interaction.response.send_message("На вас сейчас не направлена активная атака.", ephemeral=True)
        return
    if действие.value == "defend":
        await view.perform_defense(
            interaction,
            extra_dice=кубы_защиты,
            damage_reduction_modifier=снижение_урона,
        )
    else:
        await view.finish_damage(interaction, 0, {})


def catalog_item_embed(item: dict) -> discord.Embed:
    description = str(item.get("description") or item.get("conditions") or "Описание отсутствует.")
    embed = discord.Embed(
        title=str(item["name"]),
        description=short(description, 4000),
        color=0x6E654F,
    )
    source_number = item.get("source_number")
    identity = [
        f'**Категория:** {item.get("category") or "—"}',
        f'**Размер:** {item.get("size") or "—"}',
        f'**Допуск:** {item.get("access") or "Общедоступное"}',
        f'**Цена:** {item.get("price")} БС' if int(item.get("price") or 0) > 0 else "**Цена:** не продаётся",
    ]
    if source_number is not None:
        identity.insert(0, f"**Номер:** {source_number}")
    embed.add_field(name="Сведения", value="\n".join(identity), inline=True)

    stats: list[str] = []
    category = str(item.get("category") or "")
    quality = int(item.get("max_durability") or item.get("gear") or 0)
    if quality:
        label = "Защита / качество" if category in {"Броня", "Щит"} else "Качество / :gears:"
        stats.append(f"**{label}:** {quality}")
    if int(item.get("damage") or 0) > 0:
        stats.append(f'**Урон:** {item["damage"]}')
    if item.get("damage_type"):
        stats.append(f'**Тип урона:** {item["damage_type"]}')
    hand = {1: "одноручное", 2: "двуручное"}.get(int(item.get("hands") or 0))
    if hand:
        stats.append(f"**Хват:** {hand}")
    if item.get("use_range"):
        stats.append(f'**Дистанция:** {item["use_range"]}')
    if item.get("ammo_max") is not None:
        stats.append(f'**Боезапас:** {item["ammo_max"]}')
    if item.get("fire_rate") is not None:
        stats.append(f'**Скорострельность:** {item["fire_rate"]}')
    embed.add_field(name="Характеристики", value="\n".join(stats) if stats else "—", inline=True)

    properties = str(item.get("properties") or "").strip()
    if properties:
        embed.add_field(name="Свойства", value=short(properties, 1024), inline=False)
    conditions = str(item.get("conditions") or "").strip()
    if conditions and conditions.casefold() != description.casefold():
        embed.add_field(name="Условия и эффекты", value=short(conditions, 1024), inline=False)

    modifiers: list[str] = []
    for field_name, label in (("attribute_modifiers", "Характеристики"), ("skill_modifiers", "Навыки")):
        raw = item.get(field_name) or "{}"
        try:
            values = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            values = {}
        if values:
            rendered = ", ".join(f"{name} {value:+d}" for name, value in values.items())
            modifiers.append(f"**{label}:** {rendered}")
    if modifiers:
        embed.add_field(name="Модификаторы", value="\n".join(modifiers), inline=False)
    return embed


ADMIN_CATALOG_PAGE_SIZE = 5


def admin_catalog_embed(items: list[dict], page: int, edit_prices: bool) -> discord.Embed:
    pages = max(1, (len(items) + ADMIN_CATALOG_PAGE_SIZE - 1) // ADMIN_CATALOG_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    shown = items[page * ADMIN_CATALOG_PAGE_SIZE:(page + 1) * ADMIN_CATALOG_PAGE_SIZE]
    lines = [
        f'**{item["name"]}** · {item.get("category") or "—"} · **{int(item.get("price") or 0)} БС**'
        for item in shown
    ]
    embed = discord.Embed(
        title="Редактирование цен" if edit_prices else "Все предметы",
        description="\n".join(lines) or "Каталог пуст.",
        color=0x745B38,
    )
    embed.set_footer(text=f"Страница {page + 1}/{pages} · выберите предмет")
    return embed


class PriceEditModal(discord.ui.Modal, title="Изменить цену предмета"):
    цена = discord.ui.TextInput(label="Новая цена в БС", min_length=1, max_length=6)

    def __init__(self, item: dict):
        super().__init__()
        self.item = item
        self.цена.default = str(int(item.get("price") or 0))

    async def on_submit(self, interaction: discord.Interaction):
        try:
            price = int(self.цена.value)
        except ValueError:
            await interaction.response.send_message("Цена должна быть целым числом.", ephemeral=True)
            return
        if price < 0:
            await interaction.response.send_message("Цена не может быть отрицательной.", ephemeral=True)
            return
        await bot.db.update_catalog_price(self.item["name"], price)
        await interaction.response.send_message(
            f'Цена **{self.item["name"]}** изменена на **{price} БС** во всём каталоге.',
            ephemeral=True,
        )


class AdminCatalogSelect(discord.ui.Select):
    def __init__(self, parent: "AdminCatalogView", shown: list[dict]):
        self.parent_view = parent
        super().__init__(
            placeholder="Выберите предмет",
            options=[
                discord.SelectOption(
                    label=item["name"][:100],
                    value=str(item["id"]),
                    description=f'{item.get("category") or "—"} · {int(item.get("price") or 0)} БС'[:100],
                ) for item in shown
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        item = self.parent_view.by_id[int(self.values[0])]
        if self.parent_view.edit_prices:
            await interaction.response.send_modal(PriceEditModal(item))
        else:
            await interaction.response.edit_message(embed=catalog_item_embed(item), view=self.parent_view)


class AdminCatalogView(discord.ui.View):
    def __init__(self, items: list[dict], edit_prices: bool = False, page: int = 0):
        super().__init__(timeout=600)
        self.items = items
        self.by_id = {int(item["id"]): item for item in items}
        self.edit_prices = edit_prices
        self.pages = max(1, (len(items) + ADMIN_CATALOG_PAGE_SIZE - 1) // ADMIN_CATALOG_PAGE_SIZE)
        self.page = max(0, min(page, self.pages - 1))
        shown = items[self.page * ADMIN_CATALOG_PAGE_SIZE:(self.page + 1) * ADMIN_CATALOG_PAGE_SIZE]
        if shown:
            self.add_item(AdminCatalogSelect(self, shown))
        self.previous.disabled = self.page == 0
        self.next.disabled = self.page >= self.pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        try:
            await require_master_access(interaction)
            return True
        except app_commands.CheckFailure:
            await interaction.response.send_message("Недостаточно прав.", ephemeral=True)
            return False

    async def refresh(self, interaction: discord.Interaction, page: int):
        fresh = await bot.db.catalog_items(interaction.guild_id, "", 500)
        view = AdminCatalogView(fresh, self.edit_prices, page)
        await interaction.response.edit_message(
            embed=admin_catalog_embed(fresh, view.page, self.edit_prices), view=view
        )

    @discord.ui.button(label="←", style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, self.page - 1)

    @discord.ui.button(label="→", style=discord.ButtonStyle.secondary, row=1)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.refresh(interaction, self.page + 1)


@bot.tree.command(name="все-предметы", description="Администратор: листать каталог и смотреть каждый предмет")
@app_commands.check(require_master_access)
async def all_items_command(interaction: discord.Interaction):
    items = await bot.db.catalog_items(interaction.guild_id, "", 500)
    await interaction.response.send_message(
        embed=admin_catalog_embed(items, 0, False),
        view=AdminCatalogView(items),
        ephemeral=True,
    )


@bot.tree.command(name="редактировать-цены", description="Администратор: выбрать предмет и изменить его цену везде")
@app_commands.check(require_master_access)
async def edit_prices_command(interaction: discord.Interaction):
    items = await bot.db.catalog_items(interaction.guild_id, "", 500)
    await interaction.response.send_message(
        embed=admin_catalog_embed(items, 0, True),
        view=AdminCatalogView(items, edit_prices=True),
        ephemeral=True,
    )


@bot.tree.command(name="предметы-просмотр", description="Посмотреть описание и характеристики предмета")
async def item_view_command(interaction: discord.Interaction, предмет: str):
    if not interaction.guild_id:
        await interaction.response.send_message("Каталог доступен только на сервере.", ephemeral=True)
        return
    item = await bot.db.catalog_item(interaction.guild_id, предмет)
    if not item:
        await interaction.response.send_message("Предмет с таким названием не найден в каталоге.", ephemeral=True)
        return
    await interaction.response.send_message(embed=catalog_item_embed(item), ephemeral=True)


@item_view_command.autocomplete("предмет")
async def item_view_autocomplete(interaction: discord.Interaction, current: str):
    if not interaction.guild_id:
        return []
    items = await bot.db.catalog_items(interaction.guild_id, current, 25)
    return [
        app_commands.Choice(
            name=f'{item["name"]} · {item["category"]} · {item["size"]}'[:100],
            value=item["name"],
        )
        for item in items
    ]

@bot.tree.command(name="предмет_создать", description="Добавить шаблон предмета в базу сервера")
@app_commands.choices(
    размер=[app_commands.Choice(name=name, value=name) for name in ITEM_SIZES],
    категория=[app_commands.Choice(name=name, value=name) for name in ITEM_CATEGORIES],
    дальность=[app_commands.Choice(name=name, value=name) for name in RANGES],
)
@app_commands.check(require_master_access)
async def item_create(
    interaction: discord.Interaction,
    название: str,
    описание: str,
    размер: app_commands.Choice[str],
    категория: app_commands.Choice[str],
    прочность: app_commands.Range[int, 0, 999],
    дальность: app_commands.Choice[str] | None = None,
):
    size = размер.value
    category = категория.value
    use_range = дальность.value if дальность else None
    if category in {"Оружие ближнего боя", "Оружие дальнего боя"} and not use_range:
        await interaction.response.send_message("Для оружия выберите дальность использования.", ephemeral=True)
        return
    try:
        item_id = await bot.db.create_catalog_item(interaction.guild_id, interaction.user.id, {
            "name": название, "size": size, "category": category, "durability": прочность,
            "description": описание, "range": use_range, "ammo": None, "fire_rate": None,
            "attribute_modifiers": {}, "skill_modifiers": {},
        })
    except (ValueError, json.JSONDecodeError) as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    except Exception as error:
        await interaction.response.send_message(f"Не удалось создать предмет: {error}", ephemeral=True)
        return
    await interaction.response.send_message(f'Предмет **{название}** создан в базе под номером #{item_id}.', ephemeral=True)


@bot.tree.command(name="предмет_выдать", description="Выдать участнику предмет из базы")
@app_commands.check(require_master_access)
async def item_give(interaction: discord.Interaction, участник: discord.Member, название: str, количество: app_commands.Range[int, 1, 20] = 1):
    character = await bot.db.character(interaction.guild_id, участник.id)
    if not character:
        await interaction.response.send_message(f"У {участник.mention} нет зарегистрированного персонажа.", ephemeral=True)
        return
    item = await bot.db.catalog_item(interaction.guild_id, название)
    if not item:
        await interaction.response.send_message("Предмет с таким названием не найден в базе сервера.", ephemeral=True)
        return
    current = await bot.db.inventory(character["id"])
    small_capacity, large_capacity = inventory_slot_capacities(character)
    if item["size"] == "Малый":
        occupied, capacity = sum(x["quantity"] for x in current if x["size"] == "Малый"), small_capacity
    elif item["size"] == "Большой":
        occupied, capacity = sum(x["quantity"] for x in current if x["size"] == "Большой"), large_capacity
    else:
        occupied, capacity = 0, 10**9
    if occupied + количество > capacity:
        await interaction.response.send_message(f"Недостаточно слотов: занято {occupied}/{capacity}, требуется ещё {количество}.", ephemeral=True)
        return
    row_id = await bot.db.give_item(character["id"], item, количество)
    await interaction.response.send_message(f'**{название}** ×{количество} выдан персонажу {участник.mention} (`#{row_id}`).')


@item_give.autocomplete("название")
async def item_give_autocomplete(interaction: discord.Interaction, current: str):
    items = await bot.db.catalog_items(interaction.guild_id, current, 25)
    return [
        app_commands.Choice(
            name=f'{item["name"]} · {item["size"]} · {item["category"]}'[:100],
            value=item["name"],
        )
        for item in items
    ]


@bot.tree.command(name="каталог-обновить", description="Перечитать каталог предметов из Markdown")
@app_commands.check(require_master_access)
async def catalog_reload(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    count = await bot.db.reload_base_catalog()
    await interaction.followup.send(f"Каталог обновлён: **{count} предметов**.", ephemeral=True)


@bot.tree.command(name="предмет-удалить", description="Выбрать и удалить предмет участника")
@app_commands.check(require_master_access)
async def item_delete(
    interaction: discord.Interaction,
    участник: discord.Member,
    предмет: str,
    количество: app_commands.Range[int, 1, 20] = 1,
):
    character = await bot.db.character(interaction.guild_id, участник.id)
    if character:
        deleted = await bot.db.remove_inventory_by_name(character["id"], предмет, количество)
        await interaction.response.send_message(f"Предмет персонажа {участник.mention} удалён." if deleted else "Выбранный предмет не найден.")
    else:
        await interaction.response.send_message(f"У {участник.mention} нет зарегистрированного персонажа.", ephemeral=True)


@item_delete.autocomplete("предмет")
async def item_delete_autocomplete(interaction: discord.Interaction, current: str):
    member = getattr(interaction.namespace, "участник", None)
    member_id = getattr(member, "id", None)
    if member_id is None:
        return []
    character = await bot.db.character(interaction.guild_id, member_id)
    if not character:
        return []
    query = str(current).casefold()
    items = await bot.db.inventory(character["id"])
    choices = []
    for item in items:
        label = f'{item["name"]} ×{item["quantity"]} · {item["durability"]}/{item["max_durability"]}'
        if query and query not in label.casefold():
            continue
        choices.append(app_commands.Choice(name=label[:100], value=item["name"]))
        if len(choices) == 25:
            break
    return choices


@bot.tree.command(name="сломать-починить-предмет", description="Изменить прочность выбранного предмета")
@app_commands.choices(действие=[
    app_commands.Choice(name="Починить (+)", value="repair"),
    app_commands.Choice(name="Сломать (−)", value="break"),
])
@app_commands.check(require_master_access)
async def item_state(
    interaction: discord.Interaction,
    участник: discord.Member,
    предмет: str,
    действие: app_commands.Choice[str],
    количество: app_commands.Range[int, 1, 999],
):
    character = await bot.db.character(interaction.guild_id, участник.id)
    if not character:
        await interaction.response.send_message(f"У {участник.mention} нет зарегистрированного персонажа.", ephemeral=True)
        return
    item = await bot.db.inventory_item_by_name(character["id"], предмет)
    if not item:
        await interaction.response.send_message("Выбранный предмет не найден.", ephemeral=True)
        return
    delta = количество if действие.value == "repair" else -количество
    value = await bot.db.adjust_inventory_durability(item["id"], character["id"], delta)
    if value is None:
        await interaction.response.send_message("Выбранный предмет не найден.", ephemeral=True)
        return
    await interaction.response.send_message(f"{участник.mention}: прочность предмета теперь **{value}**.")


@item_state.autocomplete("предмет")
async def item_state_autocomplete(interaction: discord.Interaction, current: str):
    return await item_delete_autocomplete(interaction, current)


@bot.tree.command(name="обслуживание", description="Очистить устаревшие данные и уплотнить базу")
@app_commands.check(require_master_access)
async def maintenance(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    expired = await bot.db.cleanup_expired_injuries()
    referenced = {str(Path(path).resolve()) for path in await bot.db.referenced_photo_paths()}
    photos_dir = PHOTOS_ROOT
    removed = 0
    if photos_dir.exists():
        for path in photos_dir.iterdir():
            if path.is_file() and str(path.resolve()) not in referenced:
                path.unlink()
                removed += 1
    await bot.db.vacuum()
    await interaction.followup.send(
        f"Обслуживание завершено: удалено просроченных травм — {expired}, сиротских фотографий — {removed}. База уплотнена.",
        ephemeral=True,
    )


@bot.tree.command(name="травмы", description="Показать активные травмы персонажа")
@app_commands.describe(участник="Персонаж, чьи травмы нужно посмотреть (по умолчанию — ваш)")
async def injuries_command(
    interaction: discord.Interaction,
    участник: discord.Member | None = None,
):
    target = участник or interaction.user
    character = await bot.db.character(interaction.guild_id, target.id)
    if not character:
        await interaction.response.send_message("У выбранного участника нет персонажа.", ephemeral=True)
        return
    await interaction.response.send_message(embed=injuries_embed(character))


@bot.tree.command(name="удалить-травму", description="Удалить выбранную травму персонажа")
@app_commands.describe(участник="Персонаж", травма="Активная травма персонажа")
@app_commands.check(require_master_access)
async def injury_delete(
    interaction: discord.Interaction,
    участник: discord.Member,
    травма: str,
):
    character = await bot.db.character(interaction.guild_id, участник.id)
    if not character:
        await interaction.response.send_message("У выбранного участника нет персонажа.", ephemeral=True)
        return
    try:
        injury_id = int(травма)
    except ValueError:
        await interaction.response.send_message("Выберите травму из списка.", ephemeral=True)
        return
    deleted = await bot.db.delete_owned_row("injuries", injury_id, character["id"])
    await interaction.response.send_message(
        "Травма удалена." if deleted else "Эта активная травма у персонажа не найдена.",
        ephemeral=True,
    )


@injury_delete.autocomplete("травма")
async def injury_delete_autocomplete(interaction: discord.Interaction, current: str):
    member = getattr(interaction.namespace, "участник", None)
    target_id = member.id if isinstance(member, discord.Member) else interaction.user.id
    character = await bot.db.character(interaction.guild_id, target_id)
    if not character:
        return []
    query = current.casefold().strip()
    injuries = [
        injury for injury in character.get("injuries", [])
        if query in f'{injury["id"]} {injury["roll_code"]} {injury["name"]}'.casefold()
    ]
    return [
        app_commands.Choice(
            name=f'ID {injury["id"]} · №{injury["roll_code"]} {injury["name"]}'[:100],
            value=str(injury["id"]),
        )
        for injury in injuries[:25]
    ]



@item_create.error
async def item_create_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, (MasterAccessRequired, app_commands.MissingPermissions)):
        await interaction.response.send_message(MASTER_ACCESS_ERROR, ephemeral=True)
    else:
        raise error


@bot.event
async def on_ready():
    logging.info("Бот готов: %s (%s)", bot.user, bot.user.id)


@bot.tree.error
async def tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, (MasterAccessRequired, app_commands.MissingPermissions)):
        message = MASTER_ACCESS_ERROR
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return
    logging.error("Ошибка slash-команды", exc_info=error)


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("Задайте переменную окружения DISCORD_TOKEN (локально — в discord_bot/.env)")
    bot.run(token)
