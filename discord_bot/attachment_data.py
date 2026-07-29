ATTACHMENTS = [
    # Универсальные
    dict(name="Открытый траншейный прицел", slot="Прицел", kinds="Все", price=5, access="Снабжение I", skill=1, effect="+1 куб Стрельбы на Ближней и Средней дистанции."),
    dict(name="Оптический прицел ×2", slot="Прицел", kinds="Винтовка,Пулемёт", price=8, access="Снабжение II", skill=1, effect="+1 куб Стрельбы на Средней и Дальней дистанции."),
    dict(name="Складной штык", slot="Ствол", kinds="Винтовка,Дробовик", price=6, access="Снабжение I", melee_damage=1, effect="Позволяет использовать оружие в Драке с уроном 1 Колющий."),
    dict(name="Пламегаситель", slot="Ствол", kinds="Пистолет,Винтовка,Пулемёт", price=7, access="Снабжение II", skill=1, effect="+1 куб первого выстрела в ход."),
    dict(name="Утяжелённая рукоять", slot="Рукоять", kinds="Пистолет,Винтовка,Дробовик", price=5, access="Снабжение I", skill=1, effect="+1 куб Стрельбы при одном выстреле за ход."),
    dict(name="Ремень быстрого хвата", slot="Опора", kinds="Все", price=5, access="Снабжение I", effect="Экипировка и снятие оружия не требуют манёвра."),
    # Пистолеты
    dict(name="Увеличенный магазин пистолета", slot="Магазин", kinds="Пистолет", price=7, access="Снабжение II", ammo=2, effect="+2 к боезапасу."),
    dict(name="Барабанный магазин пистолета", slot="Магазин", kinds="Пистолет", price=11, access="Снабжение III", ammo=5, hands=1, effect="+5 к боезапасу; оружие занимает на 1 руку больше."),
    dict(name="Длинный пистолетный ствол", slot="Ствол", kinds="Пистолет", price=8, access="Снабжение II", range=1, effect="Максимальная дистанция увеличивается на одну ступень."),
    dict(name="Ускоренный пистолетный затвор", slot="Механизм", kinds="Пистолет", price=10, access="Снабжение III", fire_rate=1, effect="+1 СКР."),
    # Винтовки
    dict(name="Винтовочный коробчатый магазин", slot="Магазин", kinds="Винтовка", price=9, access="Снабжение II", ammo=4, effect="+4 к боезапасу."),
    dict(name="Тяжёлый винтовочный ствол", slot="Ствол", kinds="Винтовка", price=15, access="Снабжение IV", damage=1, fire_rate=-1, effect="+1 урон, −1 СКР (минимум 1)."),
    dict(name="Щёчный упор с ремнём", slot="Опора", kinds="Винтовка", price=8, access="Снабжение II", skill=1, effect="+1 куб Стрельбы, если стрелок не перемещался."),
    dict(name="Самозарядный винтовочный блок", slot="Механизм", kinds="Винтовка", price=14, access="Снабжение IV", fire_rate=1, gear=-1, effect="+1 СКР, −1 куб качества при броске."),
    # Дробовики
    dict(name="Удлинитель трубчатого магазина", slot="Магазин", kinds="Дробовик", price=8, access="Снабжение II", ammo=2, effect="+2 к боезапасу."),
    dict(name="Чок полного сужения", slot="Ствол", kinds="Дробовик", price=9, access="Снабжение III", range=1, effect="Максимальная дистанция увеличивается на одну ступень."),
    dict(name="Раструб траншейной зачистки", slot="Ствол", kinds="Дробовик", price=9, access="Снабжение III", skill=2, range=-1, effect="+2 куба Стрельбы; максимальная дистанция уменьшается на ступень."),
    dict(name="Усиленная помпа", slot="Механизм", kinds="Дробовик", price=11, access="Снабжение III", fire_rate=1, effect="+1 СКР."),
    # Пулемёты
    dict(name="Ленточный короб увеличенной ёмкости", slot="Магазин", kinds="Пулемёт", price=15, access="Снабжение IV", ammo=8, hands=1, effect="+8 к боезапасу; оружие занимает на 1 руку больше."),
    dict(name="Сошки пулемётчика", slot="Опора", kinds="Пулемёт", price=10, access="Снабжение III", skill=2, effect="+2 куба Стрельбы, если стрелок не перемещался."),
    dict(name="Водяной кожух ствола", slot="Ствол", kinds="Пулемёт", price=14, access="Снабжение IV", fire_rate=2, hands=1, effect="+2 СКР; оружие занимает на 1 руку больше."),
    dict(name="Облегчённый пулемётный затвор", slot="Механизм", kinds="Пулемёт", price=18, access="Снабжение V", fire_rate=2, gear=-1, effect="+2 СКР, −1 куб качества при броске."),
]

ATTACHMENT_BY_NAME = {item["name"]: item for item in ATTACHMENTS}

RANGE_ORDER = ("Нулевая", "Ближняя", "Средняя", "Дальняя")


def weapon_kind(item):
    name = str(item.get("name") or "").casefold()
    text = str(item.get("conditions") or "").casefold()
    if int(item.get("fire_rate") or 0) >= 4 or "пулемёт" in name:
        return "Пулемёт"
    if "дроб" in name or "ружь" in name or "боеприпас: д" in text:
        return "Дробовик"
    if "пистолет" in name or "револьвер" in name or "ракетниц" in name or "боеприпас: п" in text:
        return "Пистолет"
    return "Винтовка"


def compatible(spec, weapon):
    kinds = {part.strip() for part in spec["kinds"].split(",")}
    return "Все" in kinds or weapon_kind(weapon) in kinds


def apply_attachments(weapon, specs):
    result = dict(weapon)
    result["attachments"] = [spec["name"] for spec in specs]
    result["attachment_skill_bonus"] = sum(int(spec.get("skill") or 0) for spec in specs)
    result["damage"] = max(0, int(result.get("damage") or 0) + sum(int(spec.get("damage") or 0) for spec in specs))
    result["fire_rate"] = max(1, int(result.get("fire_rate") or 1) + sum(int(spec.get("fire_rate") or 0) for spec in specs))
    if result.get("ammo_max") is not None:
        result["ammo_max"] = max(1, int(result["ammo_max"]) + sum(int(spec.get("ammo") or 0) for spec in specs))
    result["hands"] = max(0, int(result.get("hands") or 0) + sum(int(spec.get("hands") or 0) for spec in specs))
    result["attachment_gear_modifier"] = sum(int(spec.get("gear") or 0) for spec in specs)
    shift = sum(int(spec.get("range") or 0) for spec in specs)
    if result.get("use_range") in RANGE_ORDER and shift:
        index = max(0, min(len(RANGE_ORDER) - 1, RANGE_ORDER.index(result["use_range"]) + shift))
        result["use_range"] = RANGE_ORDER[index]
    return result
