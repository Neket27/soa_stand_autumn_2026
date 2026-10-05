"""link find: связи читаются словами, а сбой обогащения не ломает чтение.

Голые коды типов и коды юнитов без summary — это та выдача, из-за которой
сценарий сочли неработающим. Обогащение добавляет названия и summary, но
остаётся надстройкой: справочник или батч не ответили — печатается прежняя
выдача и причина в notes, код возврата 0.
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
UNIT = "CRPV-1"

TYPES_EP = "/rest/api/unit/v2/link/types"
FIND_EP = "/rest/api/unit/v3/find"
LINKS_EP = "/rest/api/unit/v1/link/find"

CATALOG = {"content": [
    {"code": "dependend", "name": "Dependend",
     "source": {"name": "depends on"}, "destination": {"name": "is depended on by"}},
    {"code": "partof", "name": "Part-of",
     "source": {"name": "is part of"}, "destination": {"name": "consists of"}},
]}

LINKS = {"content": [
    {"source": UNIT, "destination": "PVM-2", "type": "dependend", "deleted": False},
    {"source": "PVM-3", "destination": UNIT, "type": "partof", "deleted": False},
]}

UNITS = {"content": [
    {"unit": {"code": "PVM-2", "summary": "Вторая задача"}},
    {"unit": {"code": "PVM-3", "summary": "Третья задача"}},
]}

ALL_ROUTES = {TYPES_EP: CATALOG, LINKS_EP: LINKS, FIND_EP: UNITS}


class Poster:
    def __init__(self, routes=None, fail_on=()):
        self.calls = []
        self.routes = routes or {}
        self.fail_on = set(fail_on)

    def __call__(self, base_url, endpoint, body, timeout):
        self.calls.append({"endpoint": endpoint, "body": body})
        if endpoint in self.fail_on:
            raise RuntimeError("HTTP 500 for %s: boom" % endpoint)
        return self.routes.get(endpoint, {})

    def hits(self, endpoint):
        return [c for c in self.calls if c["endpoint"] == endpoint]


def run(poster, *argv):
    original = tool.post_json
    tool.post_json = poster
    tool._LINK_TYPES_CACHE.clear()
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(list(argv))
    finally:
        tool.post_json = original
    return code, json.loads(out.getvalue() or err.getvalue())


def find_links(poster, *extra):
    return run(poster, "link", "find", "--unit", UNIT, *BASE, *extra)


class ResolutionTest(unittest.TestCase):
    def test_type_names_and_summaries_reach_the_output(self):
        code, out = find_links(Poster(ALL_ROUTES))
        self.assertEqual(code, 0)
        by_unit = {link["unit"]: link for link in out["links"]}
        self.assertEqual(by_unit["PVM-2"]["typeName"], "Dependend")
        self.assertEqual(by_unit["PVM-2"]["unitSummary"], "Вторая задача")

    def test_direction_is_read_from_the_asked_unit(self):
        """Одна и та же связь читается по-разному с двух концов."""
        _code, out = find_links(Poster(ALL_ROUTES))
        by_unit = {link["unit"]: link for link in out["links"]}
        self.assertEqual(by_unit["PVM-2"]["relation"], "depends on")
        self.assertEqual(by_unit["PVM-3"]["relation"], "consists of")

    def test_raw_response_is_still_there(self):
        _code, out = find_links(Poster(ALL_ROUTES))
        self.assertEqual(out["response"], LINKS)

    def test_summaries_are_fetched_in_one_batch(self):
        poster = Poster(ALL_ROUTES)
        find_links(poster)
        self.assertEqual(len(poster.hits(FIND_EP)), 1)
        values = [f["value"] for f in poster.hits(FIND_EP)[0]["body"]["filters"]["properties"]["or"]]
        self.assertEqual(sorted(values), ["PVM-2", "PVM-3"])

    def test_the_asked_unit_is_not_looked_up(self):
        poster = Poster(ALL_ROUTES)
        find_links(poster)
        values = [f["value"] for f in poster.hits(FIND_EP)[0]["body"]["filters"]["properties"]["or"]]
        self.assertNotIn(UNIT, values)


class DegradationTest(unittest.TestCase):
    def test_missing_catalog_keeps_the_read_working(self):
        code, out = find_links(Poster(ALL_ROUTES, fail_on=[TYPES_EP]))
        self.assertEqual(code, 0)
        by_unit = {link["unit"]: link for link in out["links"]}
        self.assertEqual(by_unit["PVM-2"]["typeName"], "dependend")
        self.assertEqual(by_unit["PVM-2"]["relation"], "dependend")
        self.assertEqual(by_unit["PVM-2"]["unitSummary"], "Вторая задача")
        self.assertTrue(any("справочник" in note for note in out["notes"]))

    def test_missing_batch_keeps_the_read_working(self):
        code, out = find_links(Poster(ALL_ROUTES, fail_on=[FIND_EP]))
        self.assertEqual(code, 0)
        by_unit = {link["unit"]: link for link in out["links"]}
        self.assertEqual(by_unit["PVM-2"]["typeName"], "Dependend")
        self.assertEqual(by_unit["PVM-2"]["unitSummary"], "")
        self.assertTrue(any("summary" in note for note in out["notes"]))

    def test_both_lookups_failing_is_still_exit_zero(self):
        code, out = find_links(Poster(ALL_ROUTES, fail_on=[TYPES_EP, FIND_EP]))
        self.assertEqual(code, 0)
        self.assertEqual(len(out["links"]), 2)
        self.assertEqual(len(out["notes"]), 2)

    def test_a_failing_link_read_is_still_a_real_error(self):
        """Деградирует обогащение, а не само чтение."""
        code, out = find_links(Poster(ALL_ROUTES, fail_on=[LINKS_EP]))
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])

    def test_no_notes_when_everything_resolved(self):
        _code, out = find_links(Poster(ALL_ROUTES))
        self.assertNotIn("notes", out)


class OptOutTest(unittest.TestCase):
    def test_no_resolve_makes_no_extra_calls(self):
        poster = Poster(ALL_ROUTES)
        code, out = find_links(poster, "--no-resolve")
        self.assertEqual(code, 0)
        self.assertNotIn("links", out)
        self.assertEqual(poster.hits(TYPES_EP), [])
        self.assertEqual(poster.hits(FIND_EP), [])

    def test_filter_type_still_reaches_the_request(self):
        poster = Poster(ALL_ROUTES)
        _code, out = find_links(poster, "--filter-type", "partof")
        self.assertEqual(out["request"]["types"], ["partof"])

    def test_empty_link_list_produces_no_lookups(self):
        """Нечего расшифровывать — незачем и ходить в сеть."""
        poster = Poster({**ALL_ROUTES, LINKS_EP: {"content": []}})
        code, out = find_links(poster)
        self.assertEqual(code, 0)
        self.assertEqual(out["links"], [])
        self.assertEqual(poster.hits(FIND_EP), [])
        self.assertEqual(poster.hits(TYPES_EP), [])


if __name__ == "__main__":
    unittest.main()
