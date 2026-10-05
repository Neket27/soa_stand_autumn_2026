"""Веб-ссылка на юнит: одна форма, собранная кодом, а не моделью.

Правило в SKILL.md уже было, но оставалось мягким: модель печатала
`…/swtr/task/<КОД>` (форма чужого трекера) и пользователь попадал в никуда.
Детерминированная часть — ссылка приезжает готовым полем в выводе скриптов, и
собирать её по образцу неоткуда.

Форма решена: `{SWTR_BASE_URL}/units/all/unit/<КОД>`, без `tenant` и `space`.
"""

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from _swtr_common import unit_url, unit_urls_in  # noqa: E402

BASE = "https://portal.works.prod.sbt/swtr"


class Form(unittest.TestCase):
    def test_the_only_working_form(self):
        self.assertEqual(unit_url(BASE, "PVM-175194"),
                         "https://portal.works.prod.sbt/swtr/units/all/unit/PVM-175194")

    def test_trailing_slash_in_the_base_does_not_double(self):
        self.assertEqual(unit_url(BASE + "/", "PVM-1"), unit_url(BASE, "PVM-1"))

    def test_no_tenant_and_no_space_parameters(self):
        """Решение принято: ссылка голая. Параметры привязали бы её к контексту."""
        link = unit_url(BASE, "PVM-1")
        self.assertNotIn("tenant", link)
        self.assertNotIn("space", link)
        self.assertNotIn("?", link)

    def test_forms_that_do_not_exist_are_not_produced(self):
        link = unit_url(BASE, "PVM-175194")
        for wrong in ("/swtr/task/", "/swtr/browse/", "/swtr/PVM-"):
            self.assertNotIn(wrong, link)

    def test_a_custom_stand_keeps_its_own_base(self):
        self.assertEqual(unit_url("https://stand.example/swtr", "CRPV-1"),
                         "https://stand.example/swtr/units/all/unit/CRPV-1")

    def test_code_is_percent_encoded(self):
        self.assertNotIn(" ", unit_url(BASE, "PVM 1"))

    def test_no_code_means_no_link(self):
        """Ссылка без кода — выдумка: пусть вызывающий увидит None, а не битый адрес."""
        self.assertIsNone(unit_url(BASE, ""))
        self.assertIsNone(unit_url(BASE, None))


class LinksForARawResponse(unittest.TestCase):
    """Сырой ответ API нельзя дополнять полями: он должен остаться тем, что
    прислал трекер. Поэтому ссылки едут отдельной картой «код → ссылка»."""

    RESPONSE = {
        "content": [
            {"unit": {"code": "PVM-175194", "summary": "первая",
                      "suit": {"code": "task"}, "space": {"code": "PVM"}}},
            {"unit": {"code": "CRPV-35498", "suit": {"code": "wiki_page"}}},
        ],
        "totalElements": 2,
    }

    def test_every_unit_code_gets_a_link(self):
        urls = unit_urls_in(BASE, self.RESPONSE)
        self.assertEqual(sorted(urls), ["CRPV-35498", "PVM-175194"])
        self.assertEqual(urls["PVM-175194"], unit_url(BASE, "PVM-175194"))

    def test_suit_and_space_codes_are_not_units(self):
        """У сюита и пространства тоже поле `code` — по нему ссылку строить нельзя."""
        urls = unit_urls_in(BASE, self.RESPONSE)
        for junk in ("task", "wiki_page", "PVM"):
            self.assertNotIn(junk, urls)

    def test_a_code_met_twice_gives_one_entry(self):
        payload = {"a": {"code": "PVM-1"}, "b": [{"code": "PVM-1"}]}
        self.assertEqual(list(unit_urls_in(BASE, payload)), ["PVM-1"])

    def test_nothing_found_is_an_empty_map(self):
        self.assertEqual(unit_urls_in(BASE, {"content": []}), {})
        self.assertEqual(unit_urls_in(BASE, None), {})


if __name__ == "__main__":
    unittest.main()
