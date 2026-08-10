from __future__ import annotations

from collections.abc import Iterable


CLASS_SKILLS = {"Снабжение", "Лечение", "Обращение", "Защита"}
MEDAL_SKILL_BONUS_CAP = 2
MEDAL_WILL_BONUS_CAP = 2
MEDAL_INFECTION_BONUS_CAP = 2


def medal(
    code: str,
    name: str,
    image: str,
    description: str,
    effect: str,
    **effects: object,
) -> dict[str, object]:
    return {
        "code": code,
        "name": name,
        "image": image,
        "description": description,
        "effect": effect,
        "effects": effects,
    }


def skill_medal(code: str, name: str, image: str, description: str, skill: str) -> dict[str, object]:
    return medal(
        code,
        name,
        image,
        description,
        f"Постоянно даёт +1 куб навыка к проверкам «{skill}».",
        skill_bonus={skill: 1},
    )


def class_skill_medal(code: str, name: str, image: str, description: str) -> dict[str, object]:
    return medal(
        code,
        name,
        image,
        description,
        "постоянно дает +1 куб к проверкам классового навыка.",
        class_skill_bonus=1,
    )


def will_medal(code: str, name: str, image: str, description: str) -> dict[str, object]:
    return medal(
        code,
        name,
        image,
        description,
        "Постоянно увеличивает максимум Воли на 1.",
        will_max_bonus=1,
    )


def infection_medal(code: str, name: str, image: str, description: str) -> dict[str, object]:
    return medal(
        code,
        name,
        image,
        description,
        "Постоянно увеличивает предел заражения на 1.",
        infection_max_bonus=1,
    )


MEDALS = (
    will_medal("iron_crown_grand", "Высший крест Железной Короны", "Железный крест высшей степени.png", "Высшая степень воинского отличия за подвиг, изменивший ход кампании."),
    skill_medal("iron_cross_valor", "Железный крест", "Планка_Железного_креста_2_класс.png", "Вручается за личное мужество и исполнение приказа, всем категориям военнослужающих вне зависимости от ранга или сословия.", "Выносливость"),
    skill_medal("scarlet_mercy", "Лента Сестер Милосердия", "Медаль Красного Креста.png", "Награда за спасение раненых и помощь бойцам в условиях смертельной опасности.", "Проницательность"),
    class_skill_medal("golden_sky", "Мышиный Крест", "Крест Заслуг.webp", "Орденская планка за выдающиеся заслуги перед армией и государством."),
    skill_medal("azure_throne", "Почетный Орден Крысиной Войны", "Имперский Орден.webp", "Высокая имперская награда за службу, укрепившую власть и безопасность державы.", "Влияние"),
    skill_medal("steel_unity", "Орден Крысиных Заслуг", "Орден.webp", "Вручается за сохранение единства подразделения в критической обстановке.", "Воодушевление"),
    skill_medal("white_oath", "Крест Славы Легиона «Конфедератос»", "Орден Святой Луизы.webp", "Знак безупречной верности присяге и долгу перед Империей.", "Сила"),
    infection_medal("krystov", "Орден Крыстова", "Орден Крыстова.webp", "Редкий орден за деяние, достойное быть внесённым в государственную летопись."),
    will_medal("blood_merit", "Орден Имперской Верности", "Орден за заслуги.webp", "Вручается тем, кто исполнил долг ценой тяжёлых потерь и собственной крови."),
    skill_medal("scarlet_honor", "Почётная планка Рейха", "Крысиный Почетный Знак.webp", "Знак особого расположения и признания заслуг перед Рейхом.", "Влияние"),
    skill_medal("red_banner", "Орден Алого Знамени", "Крысиный Орден.jpg", "Награда за решительные действия, сохранившие честь подразделения.", "Драка"),
    skill_medal("field_care", "Почетный Знак Полевого Попечения", "Почетный знак за Заботу.jpg", "Вручается за постоянную заботу о бойцах, раненых и гражданском населении.", "Проницательность"),
    class_skill_medal("imperial_red_cross", "Почётный знак Имперского Красного Креста", "Почетный знак Крысиного Красного Креста.png", "Высшая медицинская награда за исключительную службу делу спасения жизни."),
    skill_medal("old_guard", "Шеврон Старой Гвардии", "Шефрон старого бойца.webp", "Носится ветеранами, прошедшими несколько кампаний и сохранившими безупречную службу.", "Наблюдательность"),
    skill_medal("imperial_marksman", "Знак Имперского Снайпера", "Нашивка снайпера.png", "Вручается стрелкам, подтвердившим исключительное мастерство в боевой обстановке.", "Стрельба"),
    skill_medal("armor_hunter", "Знак Истребителя Танков", "Знак за уничтоженный танк.png", "Награда за лично подтверждённое уничтожение вражеской бронированной машины.", "Анализ"),
    skill_medal("northern_line", "Лента Северного Рубежа", "Ärmelband_Kurland.jpg", "Памятная нарукавная лента участника тяжёлой оборонительной кампании на северном рубеже.", "Скрытность"),
    infection_medal("long_service", "Медаль Службы", "За выслугу лет.png", "Вручается за многолетнюю безупречную службу в вооружённых силах."),
    skill_medal("southern_campaign", "Лента Пустынного Континента", "Ärmelbänder_Kreta.jpg", "Памятная лента участника десантной и экспедиционной кампании на театре пустынного континента.", "Проворство"),
    class_skill_medal("blue_legion", "Планка Легиона", "Голубая дивизия.webp", "Знак службы в прославленном иностранном соединении Имперской армии."),
    skill_medal("winter_front", "Медаль Героя Фронтовика", "Зимнее сражение.webp", "Вручается участникам боевых действий в условиях суровой зимней кампании.", "Сила"),
    skill_medal("fortifier", "Знак Окопного героизма", "За сооружение.webp", "Награда за удержание укреплений, мостов и сооружений под огнём противника.", "Сила"),
    skill_medal("memory_1931", "Памятная медаль 1930 года", "Медаль в память 1931.png", "Памятный знак участника событий и военной службы 1930 года.", "Знания"),
    infection_medal("memory_1930", "Памятная медаль 1931 года", "В память 1930.png", "Памятный знак участника событий и военной службы 1930 года."),
    will_medal("wound_gold", "Золотой знак Ранения", "Нагрудный знак за ранение в золоте.png", "Высшая степень знака за многочисленные тяжёлые ранения, полученные при исполнении долга."),
    skill_medal("wound_silver", "Серебряный знак Ранения", "Нагрудный знак за ранение в серебре.png", "Вторая степень знака за тяжёлые или повторные ранения в бою.", "Выносливость"),
    class_skill_medal("honor_without_swords", "Почётный крест Мирной Службы", "Почетный крест без мечей.webp", "Награда за выдающуюся небоевую службу армии и государству."),
    skill_medal("danzig_campaign", "Крест Мышиной Кампании", "Данцигский крест.webp", "Кампанейский крест участника операции на Мышином направлении.", "Скрытность"),
    skill_medal("honor_swords", "Почётный крест с Мечами", "Почетный Крест.webp", "Вручается ветеранам, чья долгая служба отмечена непосредственным участием в боях.", "Драка"),
    skill_medal("wound_black", "Чёрный знак Ранения", "Нагрудный знак за Ранение.png", "Первая степень знака за ранение, полученное в боевой обстановке.", "Выносливость"),
    skill_medal("imperial_glory", "Крест Имперской Славы", "Крест Славы.webp", "Высокая награда за победу, прославившую Империю и её вооружённые силы.", "Воодушевление"),
    skill_medal("war_merit", "Крест Военной Заслуги", "Крест Военных Заслуг.webp", "Вручается за важный вклад в военные действия как на фронте, так и в тылу.", "Наблюдательность"),
    skill_medal("foreign_legion", "Крест Иностранного Легиона", "Испанский Крест.webp", "Награда за доблесть, проявленную в составе союзного или экспедиционного соединения.", "Проворство"),
    class_skill_medal("warlord_order", "Орден Полководца", "Военный Орден.png", "Высокая командирская награда за блестяще проведённую операцию."),
    skill_medal("oak_knight", "Рыцарский крест Дубовой Короны", "Рыцарский Крест с дубовыми листьями.webp", "Особая степень Рыцарского креста за повторный выдающийся подвиг.", "Стрельба"),
    skill_medal("oak_swords_knight", "Рыцарский крест Дубовой Короны с Мечами", "Рыцарский крест с дубовыми листьями и мечами.webp", "Высшая боевая степень Рыцарского креста за череду исключительных побед.", "Драка"),
    skill_medal("knight_cross", "Рыцарский крест", "Рыцарский Крест.png", "Одна из высших наград Империи за исключительную воинскую доблесть.", "Стрельба"),
    will_medal("grand_cross", "Большой крест Империи", "Большой Крест.png", "Высшая награда Империи, вручаемая за заслуги череду выдающихся подвигов."),
)

MEDAL_BY_CODE = {item["code"]: item for item in MEDALS}
MEDAL_BY_NAME = {item["name"].casefold(): item for item in MEDALS}


def medal_bonus_summary(codes: Iterable[str]) -> dict[str, object]:
    skill_bonuses: dict[str, int] = {}
    class_skill_bonus = 0
    will_max_bonus = 0
    infection_max_bonus = 0
    for code in codes:
        effects = MEDAL_BY_CODE.get(code, {}).get("effects", {})
        for skill, value in effects.get("skill_bonus", {}).items():
            skill_bonuses[skill] = min(
                MEDAL_SKILL_BONUS_CAP,
                skill_bonuses.get(skill, 0) + int(value),
            )
        class_skill_bonus = min(
            MEDAL_SKILL_BONUS_CAP,
            class_skill_bonus + int(effects.get("class_skill_bonus", 0)),
        )
        will_max_bonus = min(
            MEDAL_WILL_BONUS_CAP,
            will_max_bonus + int(effects.get("will_max_bonus", 0)),
        )
        infection_max_bonus = min(
            MEDAL_INFECTION_BONUS_CAP,
            infection_max_bonus + int(effects.get("infection_max_bonus", 0)),
        )
    return {
        "skill_bonuses": skill_bonuses,
        "class_skill_bonus": class_skill_bonus,
        "will_max_bonus": will_max_bonus,
        "infection_max_bonus": infection_max_bonus,
    }
