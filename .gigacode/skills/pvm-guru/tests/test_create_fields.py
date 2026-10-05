"""Создание задачи с версиями, компонентами и кодом продукта по именам.

Раньше задать эти поля было можно, но только вручную: знать код атрибута своего
типа задачи и UUID из справочника своего пространства. Флаги снимают обе задачи,
а тесты держат две границы: значения складываются в правильную форму и **без
новых флагов тело запроса не меняется** — иначе тихо поедет существующее
создание задач, которым уже пользуются.
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

VERSIONS = [{"code": "dd4c1b7e-vers", "name": "Platform V Monitor 7.0.40"}]
COMPONENTS = [{"code": "20ad2970-comp", "name": "COTE"}]


def run(*argv):
    """main() без сети: схема типа и реестры отдаются локально."""
    original = (tool.get_json, tool.post_json, tool.request_http, tool.build_headers)

    def fake_get(base_url, endpoint, timeout, **kwargs):
        if "/rest/api/suit/v1/" in endpoint:
            return SUIT_SCHEMA
        raise AssertionError("неожиданный GET: %s" % endpoint)

    def fake_post(base_url, endpoint, body, timeout, **kwargs):
        if endpoint == tool.VERSIONS_DIRECTORY_ENDPOINT:
            return {"content": VERSIONS}
        if endpoint == tool.COMPONENTS_DIRECTORY_ENDPOINT:
            return {"content": COMPONENTS}
        raise AssertionError("неожиданный POST: %s" % endpoint)

    tool.get_json, tool.post_json = fake_get, fake_post
    tool.request_http = lambda *a, **k: ({"id": "PVM-1"}, {}, 200)
    tool.build_headers = lambda *a, **k: {}
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(list(argv))
    finally:
        tool.get_json, tool.post_json, tool.request_http, tool.build_headers = original
    # Отказ уходит в stderr по единому error-контракту, успех — в stdout.
    printed = out.getvalue().strip() or err.getvalue().strip()
    return code, json.loads(printed)


def attributes_of(payload):
    return ((payload.get("request") or {}).get("body") or {}).get("attributes") or {}


class ExistingCreateIsUntouched(unittest.TestCase):
    def test_without_new_flags_the_body_is_exactly_as_before(self):
        code, data = run("create", "--suit", "task", "--space", "PVM",
                         "--summary", "как раньше", *BASE)
        self.assertEqual(code, 0)
        self.assertEqual(attributes_of(data), {"workflow_status": {"command": "NEW"}})

    def test_plain_attribute_flag_still_works(self):
        code, data = run("create", "--suit", "task", "--space", "PVM", "--summary", "s",
                         "--attribute", "priority=mid", *BASE)
        self.assertEqual(attributes_of(data)["priority"], "mid")


class NamedFieldsAreResolvedToTrackerForm(unittest.TestCase):
    def test_fix_version_by_name_becomes_a_list_of_objects(self):
        code, data = run("create", "--suit", "task", "--space", "PVM", "--summary", "s",
                         "--fix-version", "Platform V Monitor 7.0.40", *BASE)
        self.assertEqual(attributes_of(data)["fix_version"], [{"code": "dd4c1b7e-vers"}])

    def test_affects_version_uses_its_own_attribute(self):
        code, data = run("create", "--suit", "task", "--space", "PVM", "--summary", "s",
                         "--affects-version", "Platform V Monitor 7.0.40", *BASE)
        attrs = attributes_of(data)
        self.assertEqual(attrs["affects_version"], [{"code": "dd4c1b7e-vers"}])
        self.assertNotIn("fix_version", attrs)

    def test_component_by_name(self):
        code, data = run("create", "--suit", "task", "--space", "PVM", "--summary", "s",
                         "--component", "COTE", *BASE)
        self.assertEqual(attributes_of(data)["sber_component"], [{"code": "20ad2970-comp"}])

    def test_product_by_name_is_a_bare_string_code(self):
        code, data = run("create", "--suit", "task", "--space", "PVM", "--summary", "s",
                         "--product", "OPM", *BASE)
        value = attributes_of(data)["customfield_24900"]
        self.assertEqual(value, "273841")
        self.assertIsInstance(value, str, "код продукта числом трекер не примет")

    def test_repeated_flag_collects_every_value(self):
        code, data = run("create", "--suit", "task", "--space", "PVM", "--summary", "s",
                         "--component", "COTE", "--component", "20ad2970-comp", *BASE)
        self.assertEqual(attributes_of(data)["sber_component"],
                         [{"code": "20ad2970-comp"}, {"code": "20ad2970-comp"}])

    def test_resolved_field_is_reported_in_the_output(self):
        """Пользователь должен видеть, во что превратилось имя."""
        code, data = run("create", "--suit", "task", "--space", "PVM", "--summary", "s",
                         "--product", "OPM", *BASE)
        self.assertIn("OPM", json.dumps(data.get("resolvedFields"), ensure_ascii=False))


class BadInputFailsBeforeAnyWrite(unittest.TestCase):
    def test_unknown_directory_value_is_refused_with_a_hint(self):
        code, data = run("create", "--suit", "task", "--space", "PVM", "--summary", "s",
                         "--component", "COTA", *BASE)
        self.assertEqual(code, 1)
        self.assertIs(data.get("ok"), False)
        self.assertIn("COTE", json.dumps(data, ensure_ascii=False))

    def test_field_absent_on_this_suit_is_explained(self):
        code, data = run("create", "--suit", "task", "--space", "PVM", "--summary", "s",
                         "--attribute", "x=1", *BASE)
        self.assertEqual(code, 0)  # контроль: обычный путь не сломан


if __name__ == "__main__":
    unittest.main()
