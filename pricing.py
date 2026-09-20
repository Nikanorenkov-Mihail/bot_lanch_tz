"""
Расчёт цены заказа.

Правила столовой:
- Комплексный обед из 3-х блюд (суп + салат + горячее с гарниром + напиток + хлеб) — COMBO_3_PRICE.
- Комплексный обед из 2-х блюд (суп ИЛИ салат + горячее с гарниром + напиток + хлеб) — COMBO_2_PRICE.
- Если набор неполный — считаем по ценам блюд из меню.
"""

import config

# Что обязательно входит в комплекс (хлеб идёт в комплекте, отдельной категорией не выбирается)
COMBO_CORE = ("hot", "garnish", "drink")


def calculate(order: dict):
    """
    order: {категория: (название, цена)} — только реально выбранные блюда.
    Возвращает (итоговая_сумма, пояснение) — пояснение показываем пользователю.
    """
    chosen = {cat for cat, (dish, _) in order.items() if dish}

    core_ok = all(cat in chosen for cat in COMBO_CORE)
    has_salad = "salad" in chosen
    has_soup = "soup" in chosen

    if core_ok and has_salad and has_soup:
        return config.COMBO_3_PRICE, "комплексный обед из 3-х блюд"

    if core_ok and (has_salad or has_soup):
        return config.COMBO_2_PRICE, "комплексный обед из 2-х блюд"

    total = sum(price for dish, price in order.values() if dish and price)
    return total, "по ценам меню"
