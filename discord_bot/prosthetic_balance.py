"""Распределение протезов по допускам Защиты и ценовым ступеням."""

PROSTHETIC_ACCESS_TIERS = {
    "Рука": ((808, 809, 810), (800, 803), (805, 807), (801, 806), (802, 804)),
    "Нога": ((821, 822), (813, 816, 820), (812, 817), (814, 819), (811, 815, 818)),
    "Глаза": ((832, 833, 834), (825, 826), (827, 829), (823, 828, 830), (824, 831)),
    "Голова": ((836, 840), (838, 843, 844), (839, 842), (835, 846), (837, 841, 845)),
    "Кожа": ((857, 858), (854, 856), (851, 853), (847, 848, 849, 850), (852, 855)),
    "Корпус": ((861, 865), (859, 860), (864, 867), (862, 863), (866, 868)),
    "Оружейный модуль": ((871, 876), (870, 874), (872, 877), (873, 878), (869, 875)),
    "Хвост": ((883, 885), (879, 884), (880, 886), (887, 888), (881, 882)),
}

_PRICE_FLOORS = (10, 15, 20, 25, 32)


def balance_prosthetic(item: dict) -> dict:
    """Возвращает копию протеза с актуальным допуском и высокой ценой."""
    source_number = int(item["source_number"])
    slot = str(item["slot"])
    tiers = PROSTHETIC_ACCESS_TIERS[slot]
    tier = next(index for index, numbers in enumerate(tiers) if source_number in numbers)
    balanced = dict(item)
    balanced["access"] = "Общедоступное" if tier == 0 else f"Защита {tier}"
    # Небольшой разброс сохраняет строгий рост цены, но делает товары разными.
    balanced["price"] = _PRICE_FLOORS[tier] + source_number % 5
    return balanced


def validate_prosthetic_balance(items: list[dict]) -> None:
    """Проверяет: все протезы учтены и в каждой ступени есть хотя бы два."""
    expected = {number for tiers in PROSTHETIC_ACCESS_TIERS.values() for group in tiers for number in group}
    actual = {int(item["source_number"]) for item in items}
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise ValueError(f"Некорректная матрица протезов: отсутствуют={missing}, лишние={unknown}")
    for slot, tiers in PROSTHETIC_ACCESS_TIERS.items():
        if any(len(numbers) < 2 for numbers in tiers):
            raise ValueError(f"В слоте {slot} есть ступень допуска менее чем с двумя протезами")
