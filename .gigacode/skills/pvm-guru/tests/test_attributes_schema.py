"""Схема типа задачи: обязательность, дефолт и коды значений справочника.

Раньше `attributes` печатал только число значений справочника, и по выводу
нельзя было узнать ни того, что `type_of_testing` обязателен, ни того, что он
принимает `new|regress`. Из-за этого создание тест-кейса упиралось в HTTP 570,
а следом — в угаданное человеческое слово вместо кода. Тесты держат границы:
обязательность и дефолт видны, коды выводятся, длинный справочник режется НЕ
молча, а `--query` не подменяет собой список обязательных полей типа.
"""

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import swtr_task_tool as tool  # noqa: E402

BASE = ["--base-url", "https://example.invalid/swtr", "--env-file", "/нет/такого"]

# Срез живой схемы `GET /rest/api/suit/v1/test_case` (портал, 18.08.2026):
# обязательность и дефолт лежат внутри parameters, рядом с ORDER.
SCHEMA = {
    "code": "test_case",
    "name": "Тест-кейс",
    "baseAttributes": ["summary", "description"],
    "attributes": [
        {"code": "type_of_testing", "name": "Вид тестирования", "type": "taxonomy",
         "parameters": {"ORDER": 16, "MANDATORY": True},
         "directoryValues": [{"code": "new", "name": "Новый функционал"},
                             {"code": "regress", "name": "Регресс"}]},
        {"code": "test_case_status", "name": "Статус", "type": "taxonomy",
         "parameters": {"ORDER": 8, "MANDATORY": True, "DEFAULT_VALUE": "draft"},
         "directoryValues": [{"code": "draft", "name": "Черновик"},
                             {"code": "relevant", "name": "Актуальный"}]},
        {"code": "folder", "name": "Папка", "type": "folder",
         "parameters": {"ORDER": 3, "MANDATORY": True}},
        {"code": "product_version", "name": "Версия продукта", "type": "text",
         "parameters": {"ORDER": 5}},
        {"code": "case_version_relevant_from", "name": "Версия кейса актуальна с",
         "type": "taxonomy", "parameters": {"ORDER": 20},
         "directoryValues": [{"code": f"v{i}", "name": f"7.2.{i}"} for i in range(33)]},
    ],
}


def run(*argv, schema=SCHEMA):
    """Гоняет main() с подменённой схемой, возвращает (код возврата, JSON)."""
    original = tool.get_json
    tool.get_json = lambda base_url, endpoint, timeout: schema
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(list(argv))
    finally:
        tool.get_json = original
    return code, json.loads(out.getvalue())


def by_code(payload):
    return {row["code"]: row for row in payload["attributes"]}


class MandatoryAndDefault(unittest.TestCase):
    def test_mandatory_flag_reaches_output(self):
        _, payload = run("attributes", "test_case", *BASE)
        rows = by_code(payload)
        self.assertTrue(rows["type_of_testing"]["mandatory"])
        self.assertFalse(rows["product_version"]["mandatory"])

    def test_mandatory_codes_listed_separately(self):
        _, payload = run("attributes", "test_case", *BASE)
        self.assertEqual(payload["mandatoryCodes"],
                         ["type_of_testing", "test_case_status", "folder"])

    def test_default_value_printed_only_when_present(self):
        _, payload = run("attributes", "test_case", *BASE)
        rows = by_code(payload)
        self.assertEqual(rows["test_case_status"]["defaultValue"], "draft")
        self.assertNotIn("defaultValue", rows["type_of_testing"])


class DirectoryValues(unittest.TestCase):
    def test_codes_are_printed_not_just_counted(self):
        _, payload = run("attributes", "test_case", *BASE)
        row = by_code(payload)["type_of_testing"]
        self.assertEqual([v["code"] for v in row["values"]], ["new", "regress"])
        self.assertEqual(row["directoryValues"], 2)

    def test_long_directory_is_truncated_out_loud(self):
        _, payload = run("attributes", "test_case", "--values-limit", "5", *BASE)
        row = by_code(payload)["case_version_relevant_from"]
        self.assertEqual(len(row["values"]), 5)
        self.assertEqual(row["valuesTruncated"], 28)
        self.assertEqual(row["directoryValues"], 33)

    def test_attribute_without_directory_has_no_values_key(self):
        _, payload = run("attributes", "test_case", *BASE)
        self.assertNotIn("values", by_code(payload)["folder"])


class QueryFilter(unittest.TestCase):
    def test_query_filters_rows(self):
        _, payload = run("attributes", "test_case", "--query", "version", *BASE)
        self.assertEqual(sorted(by_code(payload)),
                         ["case_version_relevant_from", "product_version"])

    def test_query_does_not_shrink_mandatory_codes(self):
        # Список обязательных полей — свойство типа, а не выдачи: под фильтром
        # он обязан остаться полным, иначе агент соберёт неполное тело create.
        _, payload = run("attributes", "test_case", "--query", "version", *BASE)
        self.assertEqual(payload["mandatoryCodes"],
                         ["type_of_testing", "test_case_status", "folder"])


if __name__ == "__main__":
    unittest.main()
