import json
import shutil
import subprocess
import unittest
from pathlib import Path

import supplier_comparison as comparison
from test_supplier_comparison import quote, result_with

PAGE = Path(__file__).with_name("comparison_assets") / "podbor.html"

# (название товара, единица заявки, единица поставщика, ожидаемый коэффициент)
CASES = [
    ("Шпатлёвка Ветонит LR+ (20кг)", "кг", "шт", 20.0),
    ("Штукатурка Ротбанд 30 кг", "кг", "Мешок", 30.0),
    ("Грунтовка 10 л", "л", "Канистра", 10.0),
    ("Краска 300 мл", "л", "шт", 0.3),
    ("Цемент 50 кг", "т", "Упаковка", 0.05),
    ("Смесь 5 л (6 кг)", "кг", "ведро", 6.0),
    ("Клей 25 кг, расход 1,5 кг/м²", "кг", "шт", 25.0),
    ("Саморезы 500 г", "кг", "уп", 0.5),
    ("Гвозди строительные (5кг)", "кг", "шт", None),   # гвозди: своё строгое правило
    ("Лента (50м)", "кг", "шт", None),
    ("Краска 300 мл", "кг", "шт", None),
    ("Шпаклёвка 20 кг и 25 кг", "кг", "шт", None),
    ("Лента 1,05x10 м рулон", "кг", "шт", None),
    ("Болт М8", "кг", "шт", None),
    ("Нагель ГОСТ 1985 г", "кг", "шт", None),
    ("Цемент 50 кг", "кг", "кг", None),     # одинаковые единицы: правило не нужно
    ("Цемент 50 кг", "шт", "шт", None),     # заявка не в массе
    ("Труба 50 кг", "кг", "м", None),       # поставщик не штучный
]


class UnitFromName(unittest.TestCase):
    def test_factor_is_read_from_the_name(self):
        for name, requested, supplied, expected in CASES:
            with self.subTest(name=name, requested=requested, supplied=supplied):
                found = comparison.name_amount(name, comparison.unit(supplied), comparison.unit(requested))
                self.assertEqual(found[0] if found else None, expected)

    def test_other_dimension_is_explained(self):
        self.assertEqual(comparison.name_amount("Лента (50м)", "шт", "кг")[1], "Нет пересчёта: в названии 50 м, заявка в «кг»")
        self.assertIn("300 мл", comparison.name_amount("Краска 300 мл", "шт", "кг")[1])

    def test_offer_uses_the_name_factor_and_buys_whole_packs(self):
        item = dict(position=1, requested_name="Шпатлёвка финишная", quantity=75, unit="кг")
        offer = dict(name="Шпатлёвка Ветонит LR+ (20кг)", price=1000, unit="шт", supplier="А", sku="a")
        view = comparison.offer_view(item, offer)
        self.assertEqual((view["factor"], view["quantity"], view["total"]), (20.0, 4, 4000))
        self.assertIn("из наименования", view["factor_source"])

    def test_hand_set_conversion_wins(self):
        offer = {**quote("Поставщик", name="Шпатлёвка (20кг)"), "unit": "шт"}
        item = dict(position=1, requested_name="Шпатлёвка", quantity=75, unit="кг")
        item["comparison"] = dict(request_signature=comparison.signature(item), conversions={
            comparison.quote_key(offer): dict(supplier_unit="шт", request_unit="кг", factor=25.0)})
        self.assertEqual(comparison.conversion(item, offer), (25.0, "Задан вручную"))

    @unittest.skipUnless(shutil.which("node"), "node не установлен")
    def test_page_gives_the_same_answers_as_the_server(self):
        html = PAGE.read_text(encoding="utf-8")
        start, end = html.index("function unit(u){"), html.index("const convKey=")
        script = html[start:end] + """
const cases = JSON.parse(process.argv[1]);
console.log(JSON.stringify(cases.map(([name, req, sup]) => nameAmount(name, unit(sup), unit(req)))));
"""
        raw = subprocess.run(["node", "-e", script, json.dumps([[n, r, s] for n, r, s, _ in CASES])],
                             capture_output=True, text=True, check=True).stdout
        for (name, requested, supplied, _), page in zip(CASES, json.loads(raw)):
            server = comparison.name_amount(name, comparison.unit(supplied), comparison.unit(requested))
            with self.subTest(name=name, requested=requested, supplied=supplied):
                self.assertEqual(None if server is None else list(server), page)


if __name__ == "__main__":
    unittest.main()
