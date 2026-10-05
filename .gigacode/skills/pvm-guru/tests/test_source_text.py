"""Источник отдаёт текст, а не разметку редактора.

Описание задачи, значения атрибутов и тело вики хранятся строкой, внутри которой
лежит документ ProseMirror. Разворачивать его должен сборщик источника: его
читают и ревью документа (сценарий 4), и `openspec_seed` (сценарий 18), и обход
связанных страниц (сценарий 26) — три копии развёртки разошлись бы.
"""

import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import fetch_swtr_sources as fetch  # noqa: E402
import swtr_unit_comments as rich  # noqa: E402
import wiki_body  # noqa: E402

BASE_URL = "https://example.invalid/swtr"


def doc(*paragraphs):
    return json.dumps({"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": text}]} for text in paragraphs]},
        ensure_ascii=False)


def collect(unit, wiki=None):
    """`collect_swtr_source` с подменённой сетью."""
    def request_json(url, headers, timeout):
        if "wiki" in url:
            if wiki is None:
                raise RuntimeError("HTTP 404")
            return wiki
        return unit

    real = fetch.request_json
    fetch.request_json = request_json
    try:
        return fetch.collect_swtr_source("PVM-1", "PVM-1", BASE_URL, {}, 5.0, wiki is not None)
    finally:
        fetch.request_json = real


class DescriptionTest(unittest.TestCase):
    def test_description_arrives_as_text(self):
        source = collect({"code": "PVM-1", "summary": "Тема",
                          "description": doc("Проблема:", "Счета живут вечно."), "attributes": []})
        self.assertIn("Проблема:", source["description"])
        self.assertIn("Счета живут вечно.", source["description"])
        self.assertNotIn('"type"', source["description"])

    def test_raw_markup_is_still_reachable(self):
        # Развёртка не теряет исходник: сырой юнит источник отдаёт целиком, и по
        # нему берутся ссылки, которых в тексте нет.
        raw = doc("Проблема:")
        source = collect({"code": "PVM-1", "summary": "Тема", "description": raw, "attributes": []})
        self.assertEqual(source["unit"]["description"], raw)

    def test_plain_description_is_untouched(self):
        source = collect({"code": "PVM-1", "summary": "Тема",
                          "description": "Обычный текст без разметки.", "attributes": []})
        self.assertEqual(source["description"], "Обычный текст без разметки.")

    def test_attribute_values_are_unfolded_too(self):
        unit = {"code": "PVM-1", "summary": "Тема", "description": "",
                "attributes": [{"attribute": {"code": "a1", "name": "Состав работ", "type": "text"},
                                "value": doc("Поле expiresAt.")}]}
        source = collect(unit)
        self.assertEqual(source["attributes"][0]["text"], "Поле expiresAt.")

    def test_requirement_table_keeps_its_rows(self):
        # Таблица требований — основной носитель постановки. Документ собирает
        # писатель самого навыка, а не рука: его схема снята с живых страниц PVM.
        table = wiki_body.markdown_to_doc(
            "| # | Тип | Требование |\n| --- | --- | --- |\n"
            "| 1 | Функциональные | Список тенантов |\n")
        source = collect({"code": "PVM-1", "summary": "Тема",
                          "description": json.dumps(table, ensure_ascii=False), "attributes": []})
        self.assertIn("1 | Функциональные | Список тенантов", source["description"])


class WikiTest(unittest.TestCase):
    def test_wiki_body_arrives_as_text(self):
        source = collect({"code": "PVM-1", "summary": "Тема", "description": "", "attributes": []},
                         wiki={"details": {"body": doc("Счёт активен до даты окончания.")}})
        texts = " ".join(item["text"] for item in source["wikiTexts"])
        self.assertIn("Счёт активен до даты окончания.", texts)
        self.assertNotIn('"type"', texts)


class HrefTest(unittest.TestCase):
    def test_links_live_in_marks_and_survive_only_in_the_raw_payload(self):
        raw = json.dumps({"type": "doc", "content": [{"type": "paragraph", "content": [
            {"type": "text", "text": "регламент",
             "marks": [{"type": "link", "attrs": {"href": "https://sberworks.ru/wiki/x"}}]}]}]},
            ensure_ascii=False)
        source = collect({"code": "PVM-1", "summary": "Тема", "description": raw, "attributes": []})
        self.assertNotIn("sberworks", source["description"])
        self.assertEqual(rich.collect_rich_hrefs(source["unit"]), ["https://sberworks.ru/wiki/x"])


class SeedHandoffTest(unittest.TestCase):
    def test_openspec_seed_gets_text_not_markup(self):
        # Ровно тот дефект, ради которого развёртка переехала в источник:
        # заготовка proposal сеялась разметкой.
        import openspec_seed

        source = collect({"code": "PVM-1", "summary": "Добавить срок действия счёта",
                          "description": doc("Счета живут вечно."), "attributes": []})
        result = openspec_seed.seed_from_source(source)
        markdown = result["markdown"]
        self.assertIn("Счета живут вечно.", markdown)
        self.assertNotIn('"type"', markdown)


if __name__ == "__main__":
    unittest.main()
