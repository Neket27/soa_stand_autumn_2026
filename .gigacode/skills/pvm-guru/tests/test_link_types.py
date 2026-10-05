"""Каталог типов связей: нормализация, кэш и единый контракт ошибки.

`link find` отдаёт голые коды (`dependend`, `partof`, `integration`), и без
каталога прочитать выдачу нельзя. Каталог читается один раз на процесс: запрос
на каждую связь превратил бы одно чтение в очередь одинаковых вызовов.
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

CATALOG = {"content": [
    {"code": "dependend", "name": "Dependend", "relationship": "PEER",
     "source": {"name": "depends on"}, "destination": {"name": "is depended on by"}},
    {"code": "partof", "name": "Part-of", "relationship": "PEER",
     "source": {"name": "is part of"}, "destination": {"name": "consists of"}},
    {"code": None, "name": "битая строка без кода"},
]}


class Poster:
    """Подменяет post_json и запоминает каждый вызов."""

    def __init__(self, routes=None, error=None):
        self.calls = []
        self.routes = routes or {}
        self.error = error

    def __call__(self, base_url, endpoint, body, timeout):
        self.calls.append({"endpoint": endpoint, "body": body})
        if self.error is not None:
            raise self.error
        return self.routes.get(endpoint, {})

    def endpoints(self, needle):
        return [c for c in self.calls if needle in c["endpoint"]]


def run(poster, *argv):
    original = tool.post_json
    tool.post_json = poster
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(list(argv))
    finally:
        tool.post_json = original
    return code, json.loads(out.getvalue() or err.getvalue())


class LinkTypesTest(unittest.TestCase):
    def setUp(self):
        tool._LINK_TYPES_CACHE.clear()

    def test_normalizes_codes_names_and_directions(self):
        poster = Poster({"/rest/api/unit/v2/link/types": CATALOG})
        code, out = run(poster, "link", "types", *BASE)
        self.assertEqual(code, 0)
        by_code = {row["code"]: row for row in out["types"]}
        self.assertEqual(by_code["dependend"]["name"], "Dependend")
        self.assertEqual(by_code["dependend"]["sourceName"], "depends on")
        self.assertEqual(by_code["dependend"]["destinationName"], "is depended on by")

    def test_row_without_a_code_is_dropped(self):
        poster = Poster({"/rest/api/unit/v2/link/types": CATALOG})
        _code, out = run(poster, "link", "types", *BASE)
        self.assertEqual(out["count"], 2)
        self.assertTrue(all(row["code"] for row in out["types"]))

    def test_empty_catalog_is_not_a_failure(self):
        poster = Poster({"/rest/api/unit/v2/link/types": {"content": []}})
        code, out = run(poster, "link", "types", *BASE)
        self.assertEqual(code, 0)
        self.assertEqual(out["types"], [])

    def test_bare_list_response_is_tolerated(self):
        poster = Poster({"/rest/api/unit/v2/link/types": CATALOG["content"]})
        code, out = run(poster, "link", "types", *BASE)
        self.assertEqual(code, 0)
        self.assertEqual(out["count"], 2)

    def test_catalog_is_fetched_once_per_process(self):
        poster = Poster({"/rest/api/unit/v2/link/types": CATALOG})
        original = tool.post_json
        tool.post_json = poster
        try:
            first = tool.fetch_link_types("https://example.invalid/swtr", 5.0)
            second = tool.fetch_link_types("https://example.invalid/swtr", 5.0)
        finally:
            tool.post_json = original
        self.assertEqual(first, second)
        self.assertEqual(len(poster.endpoints("link/types")), 1)

    def test_network_failure_uses_the_shared_error_contract(self):
        poster = Poster(error=RuntimeError("HTTP 500 for https://example.invalid/: boom"))
        code, out = run(poster, "link", "types", *BASE)
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertEqual(out["command"], "link")


if __name__ == "__main__":
    unittest.main()
