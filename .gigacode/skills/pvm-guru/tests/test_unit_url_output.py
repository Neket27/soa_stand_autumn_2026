"""Ссылка приезжает полем вывода — её больше не собирает модель.

Правило «единственная форма — `{SWTR_BASE_URL}/units/all/unit/<КОД>`» было
только текстом в SKILL.md, и модель всё равно печатала `…/swtr/task/<КОД>`.
Детерминированная часть: каждый вывод, где есть код юнита, несёт готовый `url`.

Раздельные формы намеренно: там, где юнит один, `url` стоит рядом с кодом; там,
где в выводе лежит СЫРОЙ ответ трекера, ссылки едут отдельной картой `urls`,
потому что дополнять ответ сервера своими полями нельзя.
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
from _swtr_common import unit_url  # noqa: E402

BASE_URL = "https://example.invalid/swtr"
BASE = ["--base-url", BASE_URL, "--env-file", "/нет/такого"]

UNIT = {"code": "PVM-175194", "summary": "тестовая задача",
        "suit": {"code": "task", "name": "Задача"},
        "space": {"code": "PVM", "name": "PVM"}, "attributes": []}

FOUND = {"content": [{"unit": {"code": "PVM-175194", "summary": "первая",
                              "suit": {"code": "task"}, "space": {"code": "PVM"}}}],
         "totalElements": 1}


def run(*argv, created="PVM-175560"):
    original = (tool.get_json, tool.post_json, tool.request_http, tool.build_headers)

    def fake_get(base_url, endpoint, timeout, **kwargs):
        if "/wiki/unit/" in endpoint:
            return {}
        return UNIT

    def fake_post(base_url, endpoint, body, timeout, **kwargs):
        return FOUND

    def fake_http(method, url, headers, *, body=None, timeout=30.0, system=None,
                  max_retries=2, **kwargs):
        return {"id": created}, {}, 200

    tool.get_json, tool.post_json = fake_get, fake_post
    tool.request_http, tool.build_headers = fake_http, lambda *a, **k: {}
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(list(argv))
    finally:
        tool.get_json, tool.post_json, tool.request_http, tool.build_headers = original
    return code, json.loads(out.getvalue().strip() or err.getvalue().strip())


class GetCarriesTheLink(unittest.TestCase):
    def test_one_unit_gets_the_link_next_to_its_code(self):
        code, data = run("get", "PVM-175194", "--skip-wiki", *BASE)
        self.assertEqual(code, 0)
        self.assertEqual(data["url"], unit_url(BASE_URL, "PVM-175194"))

    def test_the_link_uses_the_configured_stand(self):
        code, data = run("get", "PVM-175194", "--skip-wiki",
                         "--base-url", "https://stand.example/swtr",
                         "--env-file", "/нет/такого")
        self.assertTrue(data["url"].startswith("https://stand.example/swtr/units/all/unit/"))

    def test_lookup_by_text_maps_every_found_code(self):
        code, data = run("get", "какой-то текст запроса", *BASE)
        self.assertEqual(data["mode"], "lookup")
        self.assertEqual(data["urls"], {"PVM-175194": unit_url(BASE_URL, "PVM-175194")})

    def test_lookup_does_not_touch_the_raw_response(self):
        code, data = run("get", "какой-то текст запроса", *BASE)
        self.assertNotIn("url", data["units"]["content"][0]["unit"])


class CreateCarriesTheLink(unittest.TestCase):
    def test_created_unit_comes_back_with_its_link(self):
        code, data = run("create", "--suit", "task", "--space", "PVM",
                         "--summary", "новая", "--execute", *BASE, created="PVM-9001")
        self.assertEqual(code, 0)
        self.assertEqual(data["url"], unit_url(BASE_URL, "PVM-9001"))

    def test_dry_run_has_no_link_because_there_is_no_unit_yet(self):
        code, data = run("create", "--suit", "task", "--space", "PVM",
                         "--summary", "новая", *BASE)
        self.assertTrue(data["dryRun"])
        self.assertNotIn("url", data)


class RawSearchCarriesTheLinks(unittest.TestCase):
    """`swtr_unit_search.py` отдаёт ответ трекера как есть — ссылки рядом, картой."""

    def run_search(self, *argv):
        import swtr_unit_search as search

        original = (search.request_http, search.build_headers)
        search.request_http = lambda *a, **k: (FOUND, {}, 200)
        search.build_headers = lambda *a, **k: {}
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = search.main(list(argv))
        finally:
            search.request_http, search.build_headers = original
        return code, json.loads(out.getvalue())

    def test_found_units_come_with_links(self):
        code, data = self.run_search("--base-url", BASE_URL, "--env-file", "/нет/такого")
        self.assertEqual(code, 0)
        self.assertEqual(data["urls"], {"PVM-175194": unit_url(BASE_URL, "PVM-175194")})

    def test_the_response_itself_stays_untouched(self):
        code, data = self.run_search("--base-url", BASE_URL, "--env-file", "/нет/такого")
        self.assertEqual(data["response"], FOUND)


class EveryOutputWithACodeCarriesLinks(unittest.TestCase):
    """Команд, отдающих коды юнитов, больше десятка — перечислять их поимённо
    значит забыть следующую. Карта `urls` собирается на выходе: где в выводе
    встретился код юнита, там появилась и ссылка."""

    def emitted(self, payload):
        out = io.StringIO()
        original = tool.LINK_BASE_URL
        tool.LINK_BASE_URL = BASE_URL
        try:
            with contextlib.redirect_stdout(out):
                tool.emit(payload)
        finally:
            tool.LINK_BASE_URL = original
        return json.loads(out.getvalue())

    def test_a_code_anywhere_in_the_payload_gets_a_link(self):
        data = self.emitted({"command": "что-угодно", "response": FOUND})
        self.assertEqual(data["urls"], {"PVM-175194": unit_url(BASE_URL, "PVM-175194")})

    def test_a_payload_without_codes_gets_no_empty_map(self):
        self.assertNotIn("urls", self.emitted({"command": "suits", "suits": ["task"]}))

    def test_links_prepared_by_the_command_win(self):
        prepared = {"PVM-1": "уже готово"}
        self.assertEqual(self.emitted({"urls": prepared, "response": FOUND})["urls"], prepared)

    def test_search_output_carries_links(self):
        code, data = run("search", "какой-то запрос", *BASE)
        self.assertEqual(data["urls"], {"PVM-175194": unit_url(BASE_URL, "PVM-175194")})

    def test_tql_output_carries_links(self):
        code, data = run("tql", 'space = "PVM"', *BASE)
        self.assertEqual(data["urls"], {"PVM-175194": unit_url(BASE_URL, "PVM-175194")})


class SourceForReviewCarriesTheLink(unittest.TestCase):
    """`fetch_swtr_sources.py` собирает источник для ревью — и для человека.

    Поле `unitUrl` там было, но держало АДРЕС API (`/rest/api/unit/v2/…`): по нему
    открывался JSON, а не карточка. Оно же уезжало в `openspec_seed` и печаталось
    в proposal как «источник». Веб-ссылка теперь в `url`, адрес API — в `unitApiUrl`.
    """

    def collect(self):
        import fetch_swtr_sources as fetch

        original = fetch.request_json
        fetch.request_json = lambda url, headers, timeout: (
            UNIT if "/rest/api/unit/v2/" in url else {})
        try:
            return fetch.collect_swtr_source("PVM-175194", "PVM-175194", BASE_URL, {}, 30.0,
                                             False)
        finally:
            fetch.request_json = original

    def test_web_link_is_the_card_not_the_api(self):
        source = self.collect()
        self.assertEqual(source["url"], unit_url(BASE_URL, "PVM-175194"))
        self.assertNotIn("/rest/api/", source["url"])

    def test_api_address_keeps_its_own_name(self):
        source = self.collect()
        self.assertIn("/rest/api/unit/v2/", source["unitApiUrl"])

    def test_the_old_misleading_name_is_gone(self):
        self.assertNotIn("unitUrl", self.collect())


class SeedRendersTheCardLink(unittest.TestCase):
    def test_proposal_source_points_at_the_card(self):
        import openspec_seed as seed

        source = {"inputType": "swtr_unit", "code": "PVM-160042", "summary": "тема",
                  "url": unit_url(BASE_URL, "PVM-160042"), "attributes": []}
        result = seed.seed_from_source(source)
        self.assertEqual(result["source"]["url"], unit_url(BASE_URL, "PVM-160042"))
        self.assertIn(unit_url(BASE_URL, "PVM-160042"), result["markdown"])

    def test_a_source_file_saved_before_the_rename_still_works(self):
        """Старый JSON с `unitUrl` не должен ронять сборку proposal."""
        import openspec_seed as seed

        source = {"inputType": "swtr_unit", "code": "PVM-160042", "summary": "тема",
                  "unitUrl": "https://example.invalid/swtr/rest/api/unit/v2/PVM-160042",
                  "attributes": []}
        result = seed.seed_from_source(source)
        self.assertTrue(result["source"]["url"])


if __name__ == "__main__":
    unittest.main()
