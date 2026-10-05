"""Изменение полей существующей задачи: `PATCH /rest/api/unit/v2/update/{code}`.

Форма пути неочевидна и стоила долгих поисков: глагол стоит ПЕРЕД кодом
(`/unit/v2/update/PVM-1`), а не после, поэтому привычные `/unit/v2/{code}/update`
отвечают 404. Тело — `{"attributes": {...}}`, ответ — `{"id": "<код>"}`.

Границы, которые держат тесты:

* без `--execute` в сеть не уходит ни одного запроса на запись, а пользователь
  видит дифф «было → станет»: правка мультизначного поля перетирает список
  целиком, и увидеть это надо до отправки, а не после;
* статус через этот путь не меняется — у него свой автомат (`status set`),
  а запись `workflow_status` значением даёт HTTP 500 без объяснения;
* запись не повторяется при таймауте: PATCH не идемпотентен по смыслу задачи.
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

UNIT = {
    "code": "PVM-175440",
    "suit": {"code": "task", "name": "Задача"},
    "space": {"code": "PVM", "name": "PVM"},
    "attributes": [
        {"code": "fix_version", "value": [{"code": "old-version", "name": "Старая версия"}]},
        {"code": "sber_component", "value": []},
    ],
}

SUIT_SCHEMA = {
    "code": "task",
    "attributes": [
        {"code": "fix_version", "name": "Fix Version", "type": "version", "directoryValues": []},
        {"code": "affects_version", "name": "Affects Version", "type": "version",
         "directoryValues": []},
        {"code": "sber_component", "name": "Компонент", "type": "sber_component",
         "directoryValues": []},
        {"code": "customfield_24900", "name": "Код продукта", "type": "taxonomy",
         "directoryValues": [{"code": "273841", "name": "OPM"}]},
    ],
}

VERSIONS = [{"code": "new-version", "name": "Platform V Monitor 8.2.70"}]
COMPONENTS = [{"code": "vlog-code", "name": "VLOG"}]


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, *, body=None, timeout=30.0,
                 system=None, max_retries=2, **kwargs):
        self.calls.append({"method": method, "url": url, "body": body,
                           "max_retries": max_retries})
        return {"id": "PVM-175440"}, {}, 200

    @property
    def writes(self):
        return [c for c in self.calls if c["method"] in ("PATCH", "POST", "PUT")]


def run(recorder, *argv):
    original = (tool.get_json, tool.post_json, tool.request_http, tool.build_headers)

    def fake_get(base_url, endpoint, timeout, **kwargs):
        if "/rest/api/suit/v1/" in endpoint:
            return SUIT_SCHEMA
        if "/rest/api/unit/v2/" in endpoint:
            return UNIT
        raise AssertionError("неожиданный GET: %s" % endpoint)

    def fake_post(base_url, endpoint, body, timeout, **kwargs):
        if endpoint == tool.VERSIONS_DIRECTORY_ENDPOINT:
            return {"content": VERSIONS}
        if endpoint == tool.COMPONENTS_DIRECTORY_ENDPOINT:
            return {"content": COMPONENTS}
        raise AssertionError("неожиданный POST: %s" % endpoint)

    tool.get_json, tool.post_json = fake_get, fake_post
    tool.request_http = recorder
    tool.build_headers = lambda *a, **k: {}
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(list(argv))
    finally:
        tool.get_json, tool.post_json, tool.request_http, tool.build_headers = original
    return code, json.loads(out.getvalue().strip() or err.getvalue().strip())


class DryRunShowsTheDiffAndSendsNothing(unittest.TestCase):
    def test_no_write_leaves_the_wire_untouched(self):
        rec = Recorder()
        code, data = run(rec, "update", "PVM-175440", "--fix-version",
                         "Platform V Monitor 8.2.70", *BASE)
        self.assertEqual(code, 0)
        self.assertTrue(data.get("dryRun"))
        self.assertEqual(rec.writes, [], "без --execute запись уходить не должна")

    def test_diff_shows_both_sides(self):
        rec = Recorder()
        code, data = run(rec, "update", "PVM-175440", "--fix-version",
                         "Platform V Monitor 8.2.70", *BASE)
        diff = {row["attribute"]: row for row in data["diff"]}
        self.assertIn("fix_version", diff)
        self.assertEqual(diff["fix_version"]["before"], [{"code": "old-version",
                                                          "name": "Старая версия"}])
        self.assertEqual(diff["fix_version"]["after"], [{"code": "new-version"}])

    def test_diff_warns_that_a_multivalue_field_is_replaced(self):
        rec = Recorder()
        code, data = run(rec, "update", "PVM-175440", "--fix-version",
                         "Platform V Monitor 8.2.70", *BASE)
        self.assertRegex(json.dumps(data, ensure_ascii=False).lower(),
                         r"перезапис|заменя|целиком")


class WriteGoesToTheRightPlace(unittest.TestCase):
    def test_execute_patches_the_update_endpoint(self):
        rec = Recorder()
        code, data = run(rec, "update", "PVM-175440", "--component", "VLOG", "--execute", *BASE)
        self.assertEqual(code, 0)
        self.assertEqual(len(rec.writes), 1)
        call = rec.writes[0]
        self.assertEqual(call["method"], "PATCH")
        self.assertTrue(call["url"].endswith("/rest/api/unit/v2/update/PVM-175440"), call["url"])
        self.assertEqual(call["body"], {"attributes": {"sber_component": [{"code": "vlog-code"}]}})

    def test_write_is_never_retried(self):
        rec = Recorder()
        run(rec, "update", "PVM-175440", "--component", "VLOG", "--execute", *BASE)
        self.assertEqual(rec.writes[0]["max_retries"], 0)

    def test_named_and_raw_attributes_are_merged(self):
        rec = Recorder()
        run(rec, "update", "PVM-175440", "--product", "OPM",
            "--attribute", "priority=high", "--execute", *BASE)
        sent = rec.writes[0]["body"]["attributes"]
        self.assertEqual(sent["customfield_24900"], "273841")
        self.assertEqual(sent["priority"], "high")


class BaseFieldsGoOutsideAttributes(unittest.TestCase):
    """`summary` и `description` — базовые поля юнита, а не атрибуты: в теле они
    лежат на верхнем уровне. Внутри `attributes` трекер их не видит."""

    def test_summary_is_sent_at_the_top_level(self):
        rec = Recorder()
        code, data = run(rec, "update", "PVM-175440", "--summary", "новое название",
                         "--execute", *BASE)
        self.assertEqual(code, 0)
        body = rec.writes[0]["body"]
        self.assertEqual(body["summary"], "новое название")
        self.assertNotIn("summary", body.get("attributes", {}))

    def test_description_too(self):
        rec = Recorder()
        run(rec, "update", "PVM-175440", "--description", "новое описание", "--execute", *BASE)
        self.assertEqual(rec.writes[0]["body"]["description"], "новое описание")

    def test_base_field_alone_is_enough_to_patch(self):
        """Патч только названия — валидный патч, отказ «нечего менять» не годится."""
        rec = Recorder()
        code, _ = run(rec, "update", "PVM-175440", "--summary", "s", "--execute", *BASE)
        self.assertEqual(code, 0)
        self.assertEqual(len(rec.writes), 1)

    def test_base_fields_show_up_in_the_diff(self):
        rec = Recorder()
        code, data = run(rec, "update", "PVM-175440", "--summary", "новое название", *BASE)
        diff = {row["attribute"]: row for row in data["diff"]}
        self.assertIn("summary", diff)


class RefusalsHappenBeforeTheWrite(unittest.TestCase):
    def test_status_change_is_rejected_with_a_pointer(self):
        rec = Recorder()
        code, data = run(rec, "update", "PVM-175440",
                         "--attribute", "workflow_status=done", "--execute", *BASE)
        self.assertEqual(code, 1)
        self.assertIn("status set", json.dumps(data, ensure_ascii=False))
        self.assertEqual(rec.writes, [], "запись статуса не должна уходить этим путём")

    def test_nothing_to_change_is_refused(self):
        rec = Recorder()
        code, data = run(rec, "update", "PVM-175440", "--execute", *BASE)
        self.assertEqual(code, 1)
        self.assertEqual(rec.writes, [])

    def test_unknown_directory_value_never_reaches_the_tracker(self):
        rec = Recorder()
        code, data = run(rec, "update", "PVM-175440", "--component", "НЕТ-ТАКОГО",
                         "--execute", *BASE)
        self.assertEqual(code, 1)
        self.assertEqual(rec.writes, [])
        self.assertIn("VLOG", json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
