"""Умолчания профиля в операциях трекера, SourceControl и sberworks: REQ-013…REQ-019.

Профиль закрывает пропуски, но не спорит с пользователем, поэтому тесты держат
четыре границы.

Порядок источников: явное → переменная трекера → профиль → прежнее поведение.
Для логина ``--me`` это ``SWTR_LOGIN`` → профиль → ``USER`` → половина
Basic-креда, и ``USER`` не считается явным значением (REQ-015). Для пространств
явный ``--space`` всегда сильнее; без него выборки идут по основному
пространству плюс пространствам «в поле зрения», а операции над одним
пространством — по основному (REQ-016, REQ-017).

Без профиля ничего не меняется: прежние тексты ошибок, прежние коды возврата
(``SystemExit`` у create, код 2 и слова argparse у suits/versions/directory/
workflow), прежние тела запросов. ``--no-profile`` обязан давать ровно то же.

Происхождение видно структурно: поле ``defaults_applied`` перечисляет
``{key, value, source}`` с источниками ``profile | env | os_user | credential``
и не печатается пустым (REQ-014). Пустая выдача при области из профиля несёт
подсказку про ``--no-profile`` от самого скрипта, а отказ трекера по значению из
профиля — источник и подсказку поправить профиль (REQ-016, REQ-017).

Сеть подменяется целиком (``request_http``), домашний каталог — временный: ни
один тест не открывает сокет и не читает настоящий профиль пользователя.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import confluence_tool as conf  # noqa: E402
import jira_tool as jira  # noqa: E402
import source_control_pr as pr  # noqa: E402
import source_control_search as sc  # noqa: E402
import swtr_task_tool as tool  # noqa: E402

BASE = ["--base-url", "https://example.invalid/swtr", "--env-file", "/нет/такого"]
SC_BASE = ["--base-url", "https://example.invalid/api/v3", "--env-file", "/нет/такого"]
# Переменные окружения SourceControl сильнее профиля, поэтому в тестах их не
# должно быть ни одной — иначе умолчание профиля никогда бы не проверялось.
SC_ENV_NAMES = ("SOURCE_CONTROL_TENANT", "SC_TENANT", "SOURCE_CONTROL_TENANT_UUID", "SC_TENANT_UUID",
                "SOURCE_CONTROL_PROJECT", "SC_PROJECT")
JIRA_BASE = ["--base-url", "https://example.invalid/jira", "--env-file", "/нет/такого"]
CONF_BASE = ["--base-url", "https://example.invalid/wiki", "--env-file", "/нет/такого"]

SUIT_SCHEMA = {
    "code": "task",
    "attributes": [
        {"code": "sber_component", "name": "Компонент", "type": "sber_component", "directoryValues": []},
        {"code": "customfield_24900", "name": "Код продукта", "type": "taxonomy",
         "directoryValues": [{"code": "273841", "name": "OPM"}]},
    ],
}
COMPONENTS = [{"code": "20ad2970-comp", "name": "COTE"}]
WORKFLOW = {"wfSchema": {"code": "WF_1", "name": "Flow"}, "wfTransitions": []}


def unit(code, summary, space="PVM"):
    return {"unit": {"code": code, "summary": summary, "description": "", "space": space, "suit": "task",
                     "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-02T00:00:00Z"},
            "attributes": []}


class FakeTracker:
    """Stand-in for ``swtr_task_tool.request_http``: routes by URL and records every call.

    ``units`` is the content of unit searches (search/similar), ``count`` the
    count/composite answer, ``tql`` the content of TQL selections. Anything not
    routed is a test bug, not a tracker answer.
    """

    def __init__(self, units=(), count=0, tql=()):
        self.units = list(units)
        self.count = count
        self.tql = list(tql)
        self.calls = []

    def __call__(self, method, url, headers, *, body=None, timeout=30.0, system="task_tracker", **kwargs):
        self.calls.append({"method": method, "url": url, "body": body})
        path = url.split("/swtr", 1)[1]
        if path == "/rest/api/unit/v3/find/tql":
            return {"content": self.tql}, {}, 200
        if path == "/rest/api/user/v1/find":
            return [], {}, 200
        if path == "/rest/api/unit/v3/find":
            return {"content": self.units}, {}, 200
        if path.endswith("/swtr_wiki_plugin/v1/wiki/unit/find"):
            return {"content": []}, {}, 200
        if path == "/rest/api/unit/v3/count/composite":
            return {"count": self.count}, {}, 200
        if path.startswith("/rest/api/suit/v1/"):
            return SUIT_SCHEMA, {}, 200
        if path == tool.COMPONENTS_DIRECTORY_ENDPOINT:
            return {"content": COMPONENTS}, {}, 200
        if path.startswith("/rest/api/space/v3/") and path.endswith("/suits"):
            return {"code": path.split("/")[4], "suits": [{"code": "task", "name": "Задача"}]}, {}, 200
        if path.endswith("/swtr_task_tracker_plugin/v1/version/find-all"):
            return {"content": [{"code": "v1", "name": "1.0", "archived": False}]}, {}, 200
        if path.startswith("/rest/api/workflow/v1/suit/"):
            return WORKFLOW, {}, 200
        raise AssertionError("unexpected request: %s %s" % (method, url))

    def bodies(self, path_suffix):
        return [call["body"] for call in self.calls if call["url"].endswith(path_suffix)]


class ProfileDefaultsCase(unittest.TestCase):
    """Общая обвязка: временный HOME, чистое окружение логина, подменённая сеть."""

    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.home = Path(self._home.name)
        # Path.home() читает HOME только на POSIX; на Windows ntpath.expanduser
        # берёт USERPROFILE (или HOMEDRIVE+HOMEPATH). Изоляция обязана держаться
        # на всех четырёх, иначе набор читает настоящий профиль разработчика.
        env = mock.patch.dict(os.environ, {"HOME": self._home.name, "USERPROFILE": self._home.name,
                                           "HOMEDRIVE": "", "HOMEPATH": self._home.name})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._home.cleanup)
        for name in ("SWTR_LOGIN", "USER", "SWTR_BASIC_AUTH"):
            os.environ.pop(name, None)

    def write_profile(self, **data):
        path = self.home / ".config" / "pvm-guru" / "profile.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict({"version": 1}, **data), ensure_ascii=False), encoding="utf-8")
        return path

    def run_tool(self, tracker, *argv):
        """``(exit code, stdout, stderr)`` of one main() call with the network replaced."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(tool, "request_http", new=tracker), \
                mock.patch.object(tool, "build_headers", new=lambda *a, **k: {"Accept": "application/json"}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = tool.main([*argv, *BASE])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def run_json(self, tracker, *argv):
        code, out, err = self.run_tool(tracker, *argv)
        printed = out.strip() or err.strip()
        self.assertTrue(printed, "команда обязана напечатать JSON; code=%r" % (code,))
        return code, json.loads(printed)


class LoginChainTest(ProfileDefaultsCase):
    """``--me``: SWTR_LOGIN → профиль → USER → Basic; USER — не явное значение (REQ-015)."""

    def applied_login(self, data):
        return [entry for entry in data.get("defaults_applied", []) if entry["key"] == "login"]

    def test_explicit_user_needs_no_default(self):
        self.write_profile(login="familia.i.o")
        code, data = self.run_json(FakeTracker(), "assigned", "--user", "alice")
        self.assertEqual(code, 0)
        self.assertEqual(data["users"], ["alice"])
        self.assertNotIn("defaults_applied", data, "явное значение — не умолчание")

    def test_swtr_login_beats_the_profile(self):
        self.write_profile(login="familia.i.o")
        os.environ["SWTR_LOGIN"] = "primary.p.p"
        code, data = self.run_json(FakeTracker(), "assigned", "--me")
        self.assertEqual(code, 0)
        self.assertEqual(data["users"], ["primary.p.p"])
        self.assertEqual(self.applied_login(data), [{"key": "login", "value": "primary.p.p", "source": "env"}])
        self.assertTrue(any("SWTR_LOGIN" in note for note in data["notes"]), data["notes"])

    def test_profile_beats_the_os_account(self):
        self.write_profile(login="familia.i.o")
        os.environ["USER"] = "os.account"
        os.environ["SWTR_BASIC_AUTH"] = "basic.b.b:pw"
        code, data = self.run_json(FakeTracker(), "assigned", "--me")
        self.assertEqual(code, 0)
        self.assertEqual(data["users"], ["familia.i.o"])
        self.assertEqual(self.applied_login(data), [{"key": "login", "value": "familia.i.o", "source": "profile"}])
        self.assertTrue(any("из профиля" in note and "--no-profile" in note for note in data["notes"]), data["notes"])

    def test_without_profile_the_os_account_is_used_as_before(self):
        os.environ["USER"] = "os.account"
        os.environ["SWTR_BASIC_AUTH"] = "basic.b.b:pw"
        code, data = self.run_json(FakeTracker(), "done-weekly", "--me")
        self.assertEqual(code, 0)
        self.assertEqual(data["users"], ["os.account"])
        self.assertEqual(self.applied_login(data), [{"key": "login", "value": "os.account", "source": "os_user"}])
        self.assertTrue(any("USER" in note for note in data["notes"]), data["notes"])

    def test_basic_credential_is_the_last_resort(self):
        os.environ["SWTR_BASIC_AUTH"] = "basic.b.b:pw"
        code, data = self.run_json(FakeTracker(), "assigned", "--me")
        self.assertEqual(code, 0)
        self.assertEqual(data["users"], ["basic.b.b"])
        self.assertEqual(self.applied_login(data), [{"key": "login", "value": "basic.b.b", "source": "credential"}])

    def test_no_profile_flag_skips_the_profile_login(self):
        self.write_profile(login="familia.i.o")
        os.environ["USER"] = "os.account"
        code, data = self.run_json(FakeTracker(), "assigned", "--me", "--no-profile")
        self.assertEqual(code, 0)
        self.assertEqual(data["users"], ["os.account"])
        self.assertEqual(self.applied_login(data)[0]["source"], "os_user")

    def test_no_source_at_all_names_every_source_and_the_onboarding(self):
        code, out, err = self.run_tool(FakeTracker(), "assigned", "--me")
        self.assertEqual(code, "--me requires SWTR_LOGIN, USER, or the login half of SWTR_BASIC_AUTH; "
                               "or put the login into the profile (~/.config/pvm-guru/profile.json) "
                               "with /guru-start, or pass --user LOGIN.")
        for word in ("SWTR_LOGIN", "USER", "SWTR_BASIC_AUTH", "/guru-start", "profile"):
            self.assertIn(word, code)

    def test_a_blank_profile_login_does_not_shadow_the_next_source(self):
        self.write_profile(login="   ")
        os.environ["USER"] = "os.account"
        code, data = self.run_json(FakeTracker(), "assigned", "--me")
        self.assertEqual(data["users"], ["os.account"])


class SelectionSpacesTest(ProfileDefaultsCase):
    """assigned / done-weekly: пространства профиля при пустом --space (REQ-015)."""

    def setUp(self):
        super().setUp()
        os.environ["SWTR_LOGIN"] = "familia.i.o"

    def test_assigned_limits_to_primary_plus_spaces_in_view_without_duplicates(self):
        self.write_profile(task_tracker={"primary_space": "PVM", "spaces": ["CRPV", "PVM"]})
        tracker = FakeTracker()
        code, data = self.run_json(tracker, "assigned", "--me")
        self.assertEqual(code, 0)
        self.assertEqual(data["spaces"], ["PVM", "CRPV"], "основное первым, дубль убран")
        self.assertIn('space in ("PVM", "CRPV")', data["tql"])
        self.assertIn('space in ("PVM", "CRPV")', tracker.bodies("/find/tql")[0]["query"])
        self.assertIn({"key": "spaces", "value": ["PVM", "CRPV"], "source": "profile"}, data["defaults_applied"])
        self.assertTrue(any("пространства профиля" in note and "--no-profile" in note for note in data["notes"]),
                        data["notes"])

    def test_explicit_space_wins_and_is_not_reported_as_a_default(self):
        self.write_profile(task_tracker={"primary_space": "PVM", "spaces": ["CRPV"]})
        code, data = self.run_json(FakeTracker(), "assigned", "--me", "--space", "TMS")
        self.assertEqual(data["spaces"], ["TMS"])
        self.assertIn('space in ("TMS")', data["tql"])
        self.assertNotIn("PVM", data["tql"])
        self.assertEqual([e["key"] for e in data["defaults_applied"]], ["login"])

    def test_done_weekly_uses_the_profile_spaces_too(self):
        self.write_profile(task_tracker={"spaces": ["CRPV"]})
        code, data = self.run_json(FakeTracker(), "done-weekly", "--user", "alice", "--since", "2026-07-01")
        self.assertEqual(code, 0)
        self.assertEqual(data["spaces"], ["CRPV"])
        self.assertIn('space in ("CRPV")', data["tql"])
        self.assertEqual(data["defaults_applied"], [{"key": "spaces", "value": ["CRPV"], "source": "profile"}])

    def test_without_spaces_in_the_profile_the_selection_is_unrestricted(self):
        self.write_profile(login="familia.i.o")
        code, data = self.run_json(FakeTracker(), "assigned", "--user", "alice")
        self.assertEqual(data["spaces"], [])
        self.assertNotIn("space in", data["tql"])
        self.assertNotIn("defaults_applied", data)

    def test_no_profile_flag_leaves_the_selection_unrestricted(self):
        self.write_profile(task_tracker={"primary_space": "PVM"})
        code, data = self.run_json(FakeTracker(), "assigned", "--user", "alice", "--no-profile")
        self.assertEqual(data["spaces"], [])
        self.assertNotIn("defaults_applied", data)


class SearchSpacesTest(ProfileDefaultsCase):
    """search / count / similar: область поиска из профиля и подсказка при пустой выдаче (REQ-016)."""

    def setUp(self):
        super().setUp()
        self.write_profile(task_tracker={"primary_space": "PVM", "spaces": ["CRPV"]})

    def test_search_filters_units_and_wiki_by_the_profile_spaces(self):
        tracker = FakeTracker(units=[unit("PVM-1", "payment export")])
        code, data = self.run_json(tracker, "search", "payment")
        self.assertEqual(code, 0)
        self.assertEqual(tracker.bodies("/rest/api/unit/v3/find")[0]["filters"]["spaces"], ["PVM", "CRPV"])
        self.assertEqual(tracker.bodies("/wiki/unit/find")[0]["filters"]["spaces"], ["PVM", "CRPV"])
        self.assertEqual(data["spaces"], ["PVM", "CRPV"])
        self.assertEqual(data["defaults_applied"], [{"key": "spaces", "value": ["PVM", "CRPV"], "source": "profile"}])
        # Область названа и при непустой выдаче: REQ-016 требует называть её у
        # любого результата, иначе типовой поиск молча сужается до профиля.
        self.assertEqual(len(data["notes"]), 1)
        self.assertIn("область выборки — пространства профиля", data["notes"][0].lower())
        self.assertIn("PVM, CRPV", data["notes"][0])
        self.assertIn("--no-profile", data["notes"][0])

    def test_explicit_space_wins_for_search(self):
        tracker = FakeTracker()
        code, data = self.run_json(tracker, "search", "payment", "--space", "TMS")
        self.assertEqual(tracker.bodies("/rest/api/unit/v3/find")[0]["filters"]["spaces"], ["TMS"])
        self.assertEqual(data["spaces"], ["TMS"])
        self.assertNotIn("defaults_applied", data)
        self.assertNotIn("notes", data, "при явном пространстве пустая выдача — не повод звать --no-profile")

    def test_empty_search_in_profile_spaces_suggests_no_profile(self):
        code, data = self.run_json(FakeTracker(), "search", "payment")
        self.assertEqual(code, 0)
        self.assertEqual(len(data["notes"]), 1)
        self.assertIn("ничего не найдено в пространствах профиля", data["notes"][0].lower())
        self.assertIn("--no-profile", data["notes"][0])
        self.assertIn("PVM, CRPV", data["notes"][0])

    def test_the_two_scope_notes_differ_but_both_name_the_scope_and_the_way_out(self):
        # Разница между «искал вот здесь» и «здесь ничего нет» — только в тексте:
        # состав области и подсказка про --no-profile нужны в обоих случаях.
        _, found = self.run_json(FakeTracker(units=[unit("PVM-1", "payment export")]), "search", "payment")
        _, nothing = self.run_json(FakeTracker(), "search", "payment")
        self.assertNotEqual(found["notes"][0], nothing["notes"][0])
        for note in (found["notes"][0], nothing["notes"][0]):
            self.assertIn("PVM, CRPV", note)
            self.assertIn("--no-profile", note)

    def test_count_uses_the_profile_spaces_and_flags_a_zero(self):
        tracker = FakeTracker(count=0)
        code, data = self.run_json(tracker, "count", "payment")
        self.assertEqual(code, 0)
        self.assertEqual(tracker.bodies("/count/composite")[0]["filters"]["spaces"], ["PVM", "CRPV"])
        self.assertEqual(data["matched"], 0)
        self.assertIn("--no-profile", data["notes"][0])
        self.assertEqual(data["defaults_applied"][0]["key"], "spaces")

    def test_count_with_matches_names_the_scope_too(self):
        code, data = self.run_json(FakeTracker(count=3), "count", "payment")
        self.assertEqual(data["spaces"], ["PVM", "CRPV"])
        self.assertEqual(len(data["notes"]), 1)
        self.assertIn("область выборки — пространства профиля", data["notes"][0].lower())
        self.assertIn("PVM, CRPV", data["notes"][0])
        self.assertNotIn("ничего не найдено", data["notes"][0].lower())

    def test_similar_searches_every_term_in_the_profile_spaces(self):
        tracker = FakeTracker(units=[unit("PVM-7", "payment gateway integration")])
        code, data = self.run_json(tracker, "similar", "payment gateway integration")
        self.assertEqual(code, 0)
        bodies = tracker.bodies("/rest/api/unit/v3/find")
        self.assertEqual(len(bodies), 3, "по запросу на каждый термин")
        self.assertTrue(all(body["filters"]["spaces"] == ["PVM", "CRPV"] for body in bodies))
        self.assertEqual(data["spaces"], ["PVM", "CRPV"])
        self.assertEqual([item["code"] for item in data["items"]], ["PVM-7"])
        self.assertEqual(len(data["notes"]), 1)
        self.assertIn("область выборки — пространства профиля", data["notes"][0].lower())
        self.assertIn("PVM, CRPV", data["notes"][0])
        self.assertEqual(data["defaults_applied"][0]["source"], "profile")

    def test_similar_with_nothing_found_suggests_no_profile(self):
        code, data = self.run_json(FakeTracker(), "similar", "payment gateway integration")
        self.assertEqual(data["items"], [])
        self.assertIn("--no-profile", data["notes"][0])

    def test_no_profile_flag_searches_everywhere(self):
        tracker = FakeTracker()
        code, data = self.run_json(tracker, "search", "payment", "--no-profile")
        self.assertNotIn("spaces", tracker.bodies("/rest/api/unit/v3/find")[0]["filters"])
        self.assertNotIn("spaces", tracker.bodies("/wiki/unit/find")[0]["filters"])
        self.assertEqual(data["spaces"], [])
        self.assertNotIn("defaults_applied", data)
        self.assertNotIn("notes", data)


class CreateDefaultsTest(ProfileDefaultsCase):
    """create: тип, пространство, продукт и компоненты из профиля с пометкой в предпросмотре (REQ-017).

    На ветке ``--body-file`` из профиля берётся только тип: он стоит в URL, а не
    в теле, поэтому в само тело не подставляется ничего.
    """

    @staticmethod
    def body_of(data):
        return data["request"]["body"]

    def test_explicit_suit_wins_over_the_profile(self):
        self.write_profile(task_tracker={"default_suit": "bug"})
        code, data = self.run_json(FakeTracker(), "create", "--suit", "story", "--space", "PVM", "--summary", "S")
        self.assertEqual(code, 0)
        self.assertTrue(data["request"]["url"].endswith("/rest/api/unit/v2/story/create"))
        self.assertNotIn("defaults_applied", data)

    def test_suit_from_the_profile(self):
        self.write_profile(task_tracker={"default_suit": "bug"})
        code, data = self.run_json(FakeTracker(), "create", "--space", "PVM", "--summary", "S")
        self.assertTrue(data["request"]["url"].endswith("/rest/api/unit/v2/bug/create"))
        self.assertEqual(data["defaults_applied"], [{"key": "suit", "value": "bug", "source": "profile"}])

    def test_suit_falls_back_to_task_without_a_profile(self):
        code, data = self.run_json(FakeTracker(), "create", "--space", "PVM", "--summary", "S")
        self.assertEqual(code, 0)
        self.assertTrue(data["request"]["url"].endswith("/rest/api/unit/v2/task/create"))
        self.assertNotIn("defaults_applied", data)
        self.assertEqual(self.body_of(data), {"summary": "S", "description": "", "space": "PVM",
                                              "attributes": {"workflow_status": {"command": "NEW"}}})

    def test_space_product_and_components_from_the_profile_are_marked_in_the_preview(self):
        self.write_profile(task_tracker={"primary_space": "PVM", "product": "OPM", "components": ["COTE"]})
        tracker = FakeTracker()
        code, data = self.run_json(tracker, "create", "--summary", "S")
        self.assertEqual(code, 0, data)
        self.assertTrue(data["dryRun"])
        body = self.body_of(data)
        self.assertEqual(body["space"], "PVM")
        self.assertEqual(body["attributes"]["customfield_24900"], "273841")
        self.assertEqual(body["attributes"]["sber_component"], [{"code": "20ad2970-comp"}])
        self.assertEqual(data["defaults_applied"], [
            {"key": "space", "value": "PVM", "source": "profile"},
            {"key": "product", "value": "OPM", "source": "profile"},
            {"key": "components", "value": ["COTE"], "source": "profile"},
        ])
        sources = {entry["field"]: entry.get("source") for entry in data["resolvedFields"]}
        self.assertEqual(sources, {"product": "profile", "component": "profile"})
        self.assertEqual(tracker.bodies(tool.COMPONENTS_DIRECTORY_ENDPOINT)[0]["space"], "PVM",
                         "справочник читается в пространстве из профиля")
        self.assertFalse(any(call["method"] == "POST" and call["url"].endswith("/create") for call in tracker.calls),
                         "dry-run ничего не создаёт")

    def test_explicit_product_wins_over_the_profile(self):
        self.write_profile(task_tracker={"product": "OTHER"})
        code, data = self.run_json(FakeTracker(), "create", "--space", "PVM", "--summary", "S", "--product", "OPM")
        self.assertEqual(code, 0, data)
        self.assertEqual(self.body_of(data)["attributes"]["customfield_24900"], "273841")
        self.assertNotIn("defaults_applied", data)
        self.assertNotIn("source", data["resolvedFields"][0])

    def test_product_from_the_profile_rejected_by_the_directory_names_the_source(self):
        self.write_profile(task_tracker={"primary_space": "PVM", "product": "NOPE"})
        code, data = self.run_json(FakeTracker(), "create", "--summary", "S")
        self.assertEqual(code, 1)
        self.assertFalse(data["ok"])
        self.assertIn("NOPE", data["error"])
        self.assertIn({"key": "product", "value": "NOPE", "source": "profile"}, data["defaults_applied"])
        self.assertIn("профил", data["hint"])
        self.assertIn("--no-profile", data["hint"])

    def test_missing_space_keeps_the_old_error_without_a_profile(self):
        code, out, err = self.run_tool(FakeTracker(), "create", "--summary", "S")
        self.assertEqual(code, "For create without --body-file pass --summary and --space.")

    def test_missing_space_keeps_the_old_error_with_no_profile_flag(self):
        self.write_profile(task_tracker={"primary_space": "PVM"})
        code, out, err = self.run_tool(FakeTracker(), "create", "--summary", "S", "--no-profile")
        self.assertEqual(code, "For create without --body-file pass --summary and --space.")

    FULL_PROFILE = {"primary_space": "PVM", "product": "OPM", "components": ["COTE"], "default_suit": "bug"}

    def body_file(self):
        """Тело, собранное вызывающим: чужое пространство и пустые атрибуты."""
        path = self.home / "body.json"
        path.write_text(json.dumps({"summary": "S", "space": "CRPV", "attributes": {}}), encoding="utf-8")
        return str(path)

    def test_body_file_takes_the_suit_from_the_profile_and_nothing_else(self):
        """Тип стоит в URL, а не в теле: он подставляется, само тело остаётся как есть."""
        self.write_profile(task_tracker=self.FULL_PROFILE)
        tracker = FakeTracker()
        code, data = self.run_json(tracker, "create", "--body-file", self.body_file())
        self.assertEqual(code, 0)
        self.assertEqual(self.body_of(data), {"summary": "S", "space": "CRPV", "attributes": {}},
                         "в тело не попадают ни пространство, ни продукт, ни компоненты, ни workflow_status")
        self.assertTrue(data["request"]["url"].endswith("/rest/api/unit/v2/bug/create"))
        self.assertEqual(data["defaults_applied"], [{"key": "suit", "value": "bug", "source": "profile"}],
                         "взятый из профиля тип виден до --execute")
        self.assertEqual(data["resolvedFields"], [])
        self.assertEqual(tracker.calls, [], "тело чужое — справочники не читаются")

    def test_explicit_suit_beats_the_profile_with_a_body_file(self):
        self.write_profile(task_tracker=self.FULL_PROFILE)
        code, data = self.run_json(FakeTracker(), "create", "--suit", "story", "--body-file", self.body_file())
        self.assertEqual(code, 0)
        self.assertTrue(data["request"]["url"].endswith("/rest/api/unit/v2/story/create"))
        self.assertNotIn("defaults_applied", data)

    def test_body_file_falls_back_to_task_with_no_profile_flag(self):
        self.write_profile(task_tracker=self.FULL_PROFILE)
        code, data = self.run_json(FakeTracker(), "create", "--body-file", self.body_file(), "--no-profile")
        self.assertEqual(code, 0)
        self.assertTrue(data["request"]["url"].endswith("/rest/api/unit/v2/task/create"), "как до профиля")
        self.assertNotIn("defaults_applied", data)


class SingleSpaceCommandsTest(ProfileDefaultsCase):
    """suits / versions / directory / workflow: основное пространство профиля, иначе слова argparse (REQ-017)."""

    CASES = (
        (["suits"], "space"),
        (["versions"], "space"),
        (["directory", "--kind", "components"], "--space"),
        (["workflow", "--suit", "task"], "--space"),
    )

    def test_primary_space_is_used_and_reported(self):
        self.write_profile(task_tracker={"primary_space": "PVM"})
        for argv, _name in self.CASES:
            with self.subTest(command=argv[0]):
                code, data = self.run_json(FakeTracker(), *argv)
                self.assertEqual(code, 0, data)
                self.assertEqual(data["space"], "PVM")
                self.assertEqual(data["defaults_applied"], [{"key": "space", "value": "PVM", "source": "profile"}])

    def test_explicit_space_wins(self):
        self.write_profile(task_tracker={"primary_space": "PVM"})
        code, data = self.run_json(FakeTracker(), "suits", "CRPV")
        self.assertEqual(data["space"], "CRPV")
        self.assertNotIn("defaults_applied", data)
        code, data = self.run_json(FakeTracker(), "workflow", "--suit", "task", "--space", "CRPV")
        self.assertEqual(data["space"], "CRPV")
        self.assertNotIn("defaults_applied", data)

    def test_without_a_profile_argparse_still_requires_the_space(self):
        for argv, name in self.CASES:
            with self.subTest(command=argv[0]):
                code, out, err = self.run_tool(FakeTracker(), *argv)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("error: the following arguments are required: %s" % name, err)
                self.assertIn("usage: %s %s" % (Path(sys.argv[0]).name, argv[0]), err)

    def test_no_profile_flag_behaves_like_no_profile(self):
        self.write_profile(task_tracker={"primary_space": "PVM"})
        for argv, name in self.CASES:
            with self.subTest(command=argv[0]):
                code, out, err = self.run_tool(FakeTracker(), *argv, "--no-profile")
                self.assertEqual(code, 2)
                self.assertIn("the following arguments are required: %s" % name, err)

    def test_a_corrupt_profile_counts_as_no_profile(self):
        path = self.write_profile()
        path.write_text("{ это не json", encoding="utf-8")
        code, out, err = self.run_tool(FakeTracker(), "suits")
        self.assertEqual(code, 2)
        self.assertIn("the following arguments are required: space", err)

    def test_an_unsupported_schema_version_counts_as_no_profile(self):
        path = self.write_profile(task_tracker={"primary_space": "PVM"})
        path.write_text(json.dumps({"version": 2, "task_tracker": {"primary_space": "PVM"}}), encoding="utf-8")
        code, out, err = self.run_tool(FakeTracker(), "suits")
        self.assertEqual(code, 2)
        self.assertIn("the following arguments are required: space", err)

    def test_a_hand_edited_value_counts_as_no_profile(self):
        # REQ-001: файл, не прошедший проверку состава, для операций -- то же,
        # что отсутствие файла. Мусор из профиля не имеет права уехать в запрос.
        self.write_profile(task_tracker={"primary_space": 'PVM SPACE" or 1=1', "spaces": 5})
        code, out, err = self.run_tool(FakeTracker(), "suits")
        self.assertEqual(code, 2)
        self.assertIn("the following arguments are required: space", err)

        tracker = FakeTracker()
        code, data = self.run_json(tracker, "search", "комиссия")
        self.assertEqual(code, 0)
        self.assertEqual(data["spaces"], [], "область поиска не берётся из повреждённого профиля")
        self.assertNotIn("defaults_applied", data)
        self.assertNotIn("1=1", json.dumps(tracker.calls, ensure_ascii=False))


class DefaultsAppliedShapeTest(ProfileDefaultsCase):
    """Состав записей defaults_applied и их словарь источников (REQ-014)."""

    def test_login_and_spaces_are_two_entries_in_application_order(self):
        self.write_profile(login="familia.i.o", task_tracker={"primary_space": "PVM", "spaces": ["CRPV"]})
        code, data = self.run_json(FakeTracker(), "assigned", "--me")
        self.assertEqual(data["defaults_applied"], [
            {"key": "login", "value": "familia.i.o", "source": "profile"},
            {"key": "spaces", "value": ["PVM", "CRPV"], "source": "profile"},
        ])
        self.assertTrue(all(set(entry) == {"key", "value", "source"} for entry in data["defaults_applied"]))

    def test_nothing_applied_means_no_field_at_all(self):
        self.write_profile(login="familia.i.o", task_tracker={"primary_space": "PVM"})
        code, data = self.run_json(FakeTracker(), "assigned", "--user", "alice", "--space", "TMS")
        self.assertNotIn("defaults_applied", data)
        code, data = self.run_json(FakeTracker(), "suits", "TMS")
        self.assertNotIn("defaults_applied", data)

    def test_sources_come_from_the_agreed_vocabulary(self):
        seen = set()
        self.write_profile(login="familia.i.o")
        _, data = self.run_json(FakeTracker(), "assigned", "--me")
        seen.update(e["source"] for e in data["defaults_applied"])
        os.environ["SWTR_LOGIN"] = "primary.p.p"
        _, data = self.run_json(FakeTracker(), "assigned", "--me")
        seen.update(e["source"] for e in data["defaults_applied"])
        os.environ.pop("SWTR_LOGIN")
        os.environ["USER"] = "os.account"
        _, data = self.run_json(FakeTracker(), "assigned", "--me", "--no-profile")
        seen.update(e["source"] for e in data["defaults_applied"])
        os.environ.pop("USER")
        os.environ["SWTR_BASIC_AUTH"] = "basic.b.b:pw"
        _, data = self.run_json(FakeTracker(), "assigned", "--me", "--no-profile")
        seen.update(e["source"] for e in data["defaults_applied"])
        self.assertEqual(seen, {"profile", "env", "os_user", "credential"})


class FakeSourceControl:
    """Stand-in for ``source_control_search.request_http``: answers repo listings, records URLs.

    ``bare`` makes the API answer a plain JSON array instead of a ``data``
    envelope -- the shape list_repos() wraps into ``{"raw": ...}``.
    """

    def __init__(self, repos=("billing", "gateway"), bare=False):
        self.repos = list(repos)
        self.bare = bare
        self.urls = []

    def __call__(self, method, url, headers, *, timeout=30.0, system="source_control", binary=False, **kwargs):
        self.urls.append(url)
        items = [{"name": name} for name in self.repos]
        return (items if self.bare else {"data": items}), {}, 200


class FakePullRequests:
    """Stand-in for ``source_control_pr.sc_request``: no open PR, records endpoints."""

    def __init__(self):
        self.endpoints = []

    def __call__(self, method, base_url, endpoint, *, params=None, body=None, timeout=30.0):
        self.endpoints.append(endpoint)
        return []


class SourceControlDefaultsTest(ProfileDefaultsCase):
    """tenant, проект и репозитории SourceControl из профиля (REQ-013, REQ-018).

    Порядок тот же, что у трекера: явный флаг → переменная окружения → профиль →
    прежнее поведение. Прежнее поведение здесь — дословные слова argparse и код
    возврата 2, потому что без профиля ``--tenant``/``--project`` как были
    обязательными, так и остаются. ``--repo`` умолчания не получает: у ``search``
    он и так необязателен, а PR создаётся в одном конкретном репозитории.
    """

    MISSING_CASES = (
        (["repos"], "--tenant, --project"),
        (["repos", "--tenant", "t"], "--project"),
        (["repos", "--project", "p"], "--tenant"),
        (["branches", "--repo", "billing"], "--tenant, --project"),
        (["search", "--query", "комиссия"], "--tenant, --project"),
        (["grep", "--query", "комиссия"], "--tenant, --project"),
        (["file", "--repo", "billing", "--filepath", "README.md"], "--tenant, --project"),
    )

    def setUp(self):
        super().setUp()
        for name in SC_ENV_NAMES:
            os.environ.pop(name, None)

    def run_sc(self, fake, *argv):
        """``(exit code, stdout, stderr)`` of one source_control_search.main() call."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sc, "request_http", new=fake), \
                mock.patch.object(sc, "build_headers", new=lambda *a, **k: {"Accept": "application/json"}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = sc.main([*argv, *SC_BASE])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def sc_json(self, fake, *argv):
        code, out, err = self.run_sc(fake, *argv)
        self.assertTrue(out.strip(), "команда обязана напечатать JSON; code=%r stderr=%r" % (code, err))
        return code, json.loads(out)

    def run_pr(self, fake, *argv):
        """``(exit code, stdout, stderr)`` of one source_control_pr.main() call."""
        out, err = io.StringIO(), io.StringIO()
        original = pr.sc_request
        pr.sc_request = fake
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = pr.main([*argv, *SC_BASE])
                except SystemExit as exc:
                    code = exc.code
        finally:
            pr.sc_request = original
        return code, out.getvalue(), err.getvalue()

    def pr_json(self, fake, *argv):
        code, out, err = self.run_pr(fake, *argv)
        self.assertTrue(out.strip(), "команда обязана напечатать JSON; code=%r stderr=%r" % (code, err))
        return code, json.loads(out)

    def test_tenant_and_project_come_from_the_profile(self):
        self.write_profile(source_control={"tenant": "22f434fb-0355-42a1-ac25-011074a20a2d", "project": "pvm"})
        code, data = self.sc_json(FakeSourceControl(), "repos")
        self.assertEqual(code, 0)
        self.assertEqual((data["tenant"], data["project"]), ("22f434fb-0355-42a1-ac25-011074a20a2d", "pvm"))
        self.assertEqual(data["defaults_applied"], [
            {"key": "tenant", "value": "22f434fb-0355-42a1-ac25-011074a20a2d", "source": "profile"},
            {"key": "project", "value": "pvm", "source": "profile"},
        ])

    def test_the_profile_value_reaches_the_request_url(self):
        self.write_profile(source_control={"tenant": "22f434fb-0355-42a1-ac25-011074a20a2d", "project": "pvm"})
        fake = FakeSourceControl()
        self.sc_json(fake, "repos")
        self.assertTrue(fake.urls[0].endswith("/repos/22f434fb-0355-42a1-ac25-011074a20a2d/pvm?page=1&limit=30&sort=updated_at%3Adesc%2Cname%3Aasc"),
                        fake.urls)

    def test_explicit_flags_beat_the_profile(self):
        self.write_profile(source_control={"tenant": "22f434fb-0355-42a1-ac25-011074a20a2d", "project": "pvm"})
        code, data = self.sc_json(FakeSourceControl(), "repos", "--tenant", "другой", "--project", "crpv")
        self.assertEqual((data["tenant"], data["project"]), ("другой", "crpv"))
        self.assertNotIn("defaults_applied", data, "явное значение — не умолчание")

    def test_the_environment_beats_the_profile(self):
        self.write_profile(source_control={"tenant": "22f434fb-0355-42a1-ac25-011074a20a2d", "project": "pvm"})
        os.environ["SOURCE_CONTROL_TENANT"] = "env-tenant"
        os.environ["SC_PROJECT"] = "env-project"
        code, data = self.sc_json(FakeSourceControl(), "repos")
        self.assertEqual((data["tenant"], data["project"]), ("env-tenant", "env-project"))
        self.assertEqual(data["defaults_applied"], [
            {"key": "tenant", "value": "env-tenant", "source": "env"},
            {"key": "project", "value": "env-project", "source": "env"},
        ])

    def test_one_value_from_the_environment_and_one_from_the_profile(self):
        self.write_profile(source_control={"tenant": "22f434fb-0355-42a1-ac25-011074a20a2d", "project": "pvm"})
        os.environ["SC_TENANT"] = "env-tenant"
        code, data = self.sc_json(FakeSourceControl(), "repos")
        self.assertEqual(data["defaults_applied"], [
            {"key": "tenant", "value": "env-tenant", "source": "env"},
            {"key": "project", "value": "pvm", "source": "profile"},
        ])

    def test_without_a_profile_argparse_still_requires_them(self):
        for argv, names in self.MISSING_CASES:
            with self.subTest(command=argv[0], missing=names):
                code, out, err = self.run_sc(FakeSourceControl(), *argv)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("error: the following arguments are required: %s" % names, err)
                self.assertIn("usage: %s %s" % (Path(sys.argv[0]).name, argv[0]), err)

    def test_no_profile_flag_ignores_the_profile(self):
        self.write_profile(source_control={"tenant": "22f434fb-0355-42a1-ac25-011074a20a2d", "project": "pvm"})
        code, out, err = self.run_sc(FakeSourceControl(), "repos", "--no-profile")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("the following arguments are required: --tenant, --project", err)

    def test_a_corrupt_profile_counts_as_no_profile(self):
        path = self.write_profile(source_control={"tenant": "22f434fb-0355-42a1-ac25-011074a20a2d", "project": "pvm"})
        path.write_text("{ это не json", encoding="utf-8")
        code, out, err = self.run_sc(FakeSourceControl(), "repos")
        self.assertEqual(code, 2)
        self.assertIn("the following arguments are required: --tenant, --project", err)

    def test_an_unsupported_schema_version_counts_as_no_profile(self):
        path = self.write_profile(source_control={"tenant": "22f434fb-0355-42a1-ac25-011074a20a2d",
                                                  "project": "pvm"})
        path.write_text(json.dumps({"version": 2, "source_control": {"tenant": "x", "project": "pvm"}}),
                        encoding="utf-8")
        code, out, err = self.run_sc(FakeSourceControl(), "repos")
        self.assertEqual(code, 2)
        self.assertIn("the following arguments are required: --tenant, --project", err)

    def test_a_hand_edited_repos_value_counts_as_no_profile(self):
        self.write_profile(source_control={"tenant": "22f434fb-0355-42a1-ac25-011074a20a2d",
                                           "project": "pvm", "repos": 9})
        code, out, err = self.run_sc(FakeSourceControl(), "repos")
        self.assertEqual(code, 2, out)
        self.assertIn("the following arguments are required: --tenant, --project", err)

    def test_repos_names_the_repositories_of_the_profile_it_found(self):
        self.write_profile(source_control={"tenant": "22f434fb-0355-42a1-ac25-011074a20a2d",
                                           "project": "p", "repos": ["Billing"]})
        code, data = self.sc_json(FakeSourceControl(), "repos")
        self.assertEqual(data["response"], {"data": [{"name": "billing"}, {"name": "gateway"}]},
                         "сырой ответ API печатается ровно таким, каким пришёл")
        self.assertEqual(data["profileRepoMatches"], ["Billing"])

    def test_without_repos_in_the_profile_there_is_no_marker(self):
        self.write_profile(source_control={"tenant": "22f434fb-0355-42a1-ac25-011074a20a2d", "project": "p"})
        code, data = self.sc_json(FakeSourceControl(), "repos")
        items = data["response"]["data"]
        self.assertEqual(len(items), 2, "выдача обязана быть непустой, иначе утверждение ниже всегда истинно")
        self.assertNotIn("profileRepoMatches", data, "без списка в профиле пересечению неоткуда взяться")
        self.assertNotIn("profileRepos", data)

    def test_no_profile_flag_leaves_the_listing_unmarked(self):
        self.write_profile(source_control={"repos": ["billing"]})
        code, data = self.sc_json(FakeSourceControl(), "repos", "--tenant", "t", "--project", "p", "--no-profile")
        items = data["response"]["data"]
        self.assertEqual(len(items), 2)
        self.assertNotIn("profileRepoMatches", data)
        self.assertNotIn("profileRepos", data)

    def test_search_candidates_are_matched_too(self):
        self.write_profile(source_control={"tenant": "22f434fb-0355-42a1-ac25-011074a20a2d",
                                           "project": "p", "repos": ["gateway"]})
        code, data = self.sc_json(FakeSourceControl(), "search", "--query", "комиссия")
        self.assertTrue(data["needsRepository"])
        self.assertEqual(data["candidates"], {"data": [{"name": "billing"}, {"name": "gateway"}]},
                         "сырой листинг кандидатов печатается ровно таким, каким пришёл")
        self.assertEqual(data["profileRepoMatches"], ["gateway"])

    def test_the_profile_repositories_are_returned_even_when_the_listing_misses_them(self):
        # Листинг кандидатов фильтруется поисковым запросом, поэтому знакомый
        # репозиторий в него может не попасть вовсе; агенту всё равно есть что
        # предложить первым (REQ-018).
        self.write_profile(source_control={"tenant": "22f434fb-0355-42a1-ac25-011074a20a2d", "project": "p",
                                           "repos": ["pvm-core", "pvm-ui"]})
        code, data = self.sc_json(FakeSourceControl(), "search", "--query", "комиссия")
        self.assertEqual(data["profileRepos"], ["pvm-core", "pvm-ui"])
        self.assertEqual(data["profileRepoMatches"], [],
                         "листинг вернул чужие репозитории — тем важнее отдельное поле")

        code, listing = self.sc_json(FakeSourceControl(), "repos")
        self.assertEqual(listing["profileRepos"], ["pvm-core", "pvm-ui"])

    def test_a_bare_array_answer_is_still_matched(self):
        # list_repos заворачивает не-объектный ответ в {"raw": ...}; без этого
        # конверта пересечение с профилем молча оказывалось бы пустым на таких
        # инсталляциях.
        self.write_profile(source_control={"tenant": "22f434fb-0355-42a1-ac25-011074a20a2d", "project": "p",
                                           "repos": ["billing"]})
        fake = FakeSourceControl(bare=True)
        code, data = self.sc_json(fake, "repos")
        self.assertEqual(data["response"]["raw"], [{"name": "billing"}, {"name": "gateway"}])
        self.assertEqual(data["profileRepoMatches"], ["billing"])

    def test_pull_requests_take_tenant_and_project_from_the_profile(self):
        self.write_profile(source_control={"tenant": "22f434fb-0355-42a1-ac25-011074a20a2d", "project": "pvm"})
        fake = FakePullRequests()
        code, data = self.pr_json(fake, "list", "--repo", "billing")
        self.assertEqual(code, 0)
        self.assertEqual((data["tenant"], data["project"]), ("22f434fb-0355-42a1-ac25-011074a20a2d", "pvm"))
        self.assertEqual(data["defaults_applied"], [
            {"key": "tenant", "value": "22f434fb-0355-42a1-ac25-011074a20a2d", "source": "profile"},
            {"key": "project", "value": "pvm", "source": "profile"},
        ])
        self.assertEqual(fake.endpoints, ["/repos/22f434fb-0355-42a1-ac25-011074a20a2d/pvm/billing/pulls"])

    def test_pull_request_creation_reports_the_applied_defaults(self):
        self.write_profile(source_control={"tenant": "22f434fb-0355-42a1-ac25-011074a20a2d", "project": "pvm"})
        code, data = self.pr_json(
            FakePullRequests(), "create", "--repo", "billing", "--head", "feature/срок-действия",
            "--base", "master", "--title", "Срок действия счёта в модели и выдаче")
        self.assertEqual(code, 0)
        self.assertFalse(data["created"], "создание без --execute остаётся dry-run")
        self.assertEqual([entry["key"] for entry in data["defaults_applied"]], ["tenant", "project"])

    def test_pull_requests_keep_the_repository_required(self):
        self.write_profile(source_control={"tenant": "22f434fb-0355-42a1-ac25-011074a20a2d",
                                           "project": "p", "repos": ["billing"]})
        code, out, err = self.run_pr(FakePullRequests(), "list")
        self.assertEqual(code, 2)
        self.assertIn("the following arguments are required: --repo", err)

    def test_all_three_missing_arguments_are_named_in_one_refusal(self):
        # До появления профиля argparse называл все три сразу; два круга вместо
        # одного -- это как раз то изменение, которого не должно было быть.
        code, out, err = self.run_pr(FakePullRequests(), "list")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("error: the following arguments are required: --tenant, --project, --repo", err)

    def test_pull_requests_without_a_profile_ask_for_tenant_and_project(self):
        code, out, err = self.run_pr(FakePullRequests(), "list", "--repo", "billing")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("error: the following arguments are required: --tenant, --project", err)

    def test_pull_requests_explicit_flags_beat_the_profile(self):
        self.write_profile(source_control={"tenant": "22f434fb-0355-42a1-ac25-011074a20a2d", "project": "pvm"})
        code, data = self.pr_json(FakePullRequests(), "list", "--repo", "billing",
                                  "--tenant", "другой", "--project", "crpv")
        self.assertEqual((data["tenant"], data["project"]), ("другой", "crpv"))
        self.assertNotIn("defaults_applied", data)

    def test_a_refusal_names_the_profile_as_the_source(self):
        self.write_profile(source_control={"tenant": "22f434fb-0355-42a1-ac25-011074a20a2d", "project": "net-takogo"})

        def refuse(*a, **k):
            raise RuntimeError("HTTP 404: project not found")

        code, out, err = self.run_sc(refuse, "repos")

        self.assertEqual(code, 1)
        error = json.loads(err)
        self.assertFalse(error["ok"])
        self.assertEqual(error["defaults_applied"], [
            {"key": "tenant", "value": "22f434fb-0355-42a1-ac25-011074a20a2d", "source": "profile"},
            {"key": "project", "value": "net-takogo", "source": "profile"},
        ])
        self.assertIn("профиля", error["hint"])
        self.assertIn("--no-profile", error["hint"])

    def test_a_refusal_on_explicit_values_says_nothing_about_the_profile(self):
        self.write_profile(source_control={"tenant": "22f434fb-0355-42a1-ac25-011074a20a2d", "project": "pvm"})

        def refuse(*a, **k):
            raise RuntimeError("HTTP 404: project not found")

        code, out, err = self.run_sc(refuse, "repos", "--tenant", "t", "--project", "p")

        self.assertEqual(code, 1)
        error = json.loads(err)
        self.assertNotIn("defaults_applied", error)
        self.assertNotIn("hint", error)

    def test_the_source_vocabulary_is_the_agreed_one(self):
        self.write_profile(source_control={"tenant": "22f434fb-0355-42a1-ac25-011074a20a2d", "project": "pvm"})
        _, data = self.sc_json(FakeSourceControl(), "repos")
        entries = data["defaults_applied"]
        self.assertTrue(all(set(entry) == {"key", "value", "source"} for entry in entries))
        self.assertTrue(all(entry["source"] in {"profile", "env", "os_user", "credential"} for entry in entries))


class RepoEnvelopeTest(unittest.TestCase):
    """Все конверты и имена полей, которые разбирает repo_items (REQ-018).

    Разбор рассчитан на несколько форм ответа SourceControl; непроверенная форма
    молча теряет репозиторий профиля, и «репозитории профиля первыми» перестают
    работать без единого признака в выводе.
    """

    ITEMS = [{"name": "billing"}, {"name": "gateway"}]

    def test_every_envelope_is_unwrapped(self):
        for key in sc.REPO_LIST_ENVELOPES:
            with self.subTest(envelope=key):
                self.assertEqual(sc.repo_items({key: list(self.ITEMS)}), self.ITEMS)
        self.assertEqual(sc.repo_items({"unknown": list(self.ITEMS)}), [])
        self.assertEqual(sc.repo_items("не объект"), [])

    def test_every_name_field_is_recognised(self):
        for field in ("name", "repo", "slug", "full_name"):
            with self.subTest(field=field):
                payload = {"data": [{field: "Billing"}, {field: "gateway"}]}
                self.assertEqual(sc.profile_repo_matches(payload, ["billing"]), ["billing"])

    def test_an_empty_profile_list_leaves_the_payload_untouched(self):
        payload = {"data": [{"name": "billing"}]}
        self.assertEqual(sc.profile_repo_matches(payload, []), [])
        self.assertEqual(payload, {"data": [{"name": "billing"}]})


class FakeSberworks:
    """Stand-in for ``request_http`` in jira_tool/confluence_tool: empty listings, records URLs."""

    def __init__(self, payload=None):
        self.payload = {"issues": [], "results": [], "total": 0} if payload is None else payload
        self.urls = []

    def __call__(self, method, url, headers, *, body=None, timeout=30.0, system=None, **kwargs):
        self.urls.append(url)
        return self.payload, {}, 200


class SberworksDefaultsTest(ProfileDefaultsCase):
    """Проект Jira и пространство Confluence из профиля (REQ-019).

    Здесь у профиля одна работа — не заставлять повторять ключ там, где систему
    уже назвали. Поэтому главная граница теста не в резолве, а в порядке:
    гейт ``sberworks-not-default`` срабатывает раньше и заполненный профиль его
    не отменяет — sberworks по-прежнему включается только явным признаком.

    Остальное как у трекера и SourceControl: явный флаг сильнее профиля,
    ``--no-profile`` возвращает прежнее поведение (у ``create`` — те же слова
    argparse и код 2), происхождение видно полем ``defaults_applied``. Готовый
    ``--jql``/``--cql`` профиль не трогает: запрос написан пользователем целиком.
    """

    def run_jira(self, fake, *argv):
        """``(exit code, stdout, stderr)`` of one jira_tool.main() call."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(jira, "request_http", new=fake), \
                mock.patch.object(jira, "build_headers", new=lambda *a, **k: {"Accept": "application/json"}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = jira.main([*argv, *JIRA_BASE])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def jira_json(self, fake, *argv):
        code, out, err = self.run_jira(fake, *argv)
        self.assertTrue(out.strip(), "команда обязана напечатать JSON; code=%r stderr=%r" % (code, err))
        return code, json.loads(out)

    def run_conf(self, fake, *argv):
        """``(exit code, stdout, stderr)`` of one confluence_tool.main() call."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(conf, "request_http", new=fake), \
                mock.patch.object(conf, "build_headers", new=lambda *a, **k: {"Accept": "application/json"}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = conf.main([*argv, *CONF_BASE])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def conf_json(self, fake, *argv):
        code, out, err = self.run_conf(fake, *argv)
        self.assertTrue(out.strip(), "команда обязана напечатать JSON; code=%r stderr=%r" % (code, err))
        return code, json.loads(out)

    def write_sberworks(self, **keys):
        return self.write_profile(sberworks=keys)

    def test_jira_search_takes_the_project_from_the_profile(self):
        self.write_sberworks(jira_project="PVM")
        code, data = self.jira_json(FakeSberworks(), "search", "комиссия", "--confirm-sberworks")
        self.assertEqual(code, 0)
        self.assertIn('project = "PVM"', data["jql"])
        self.assertEqual(data["defaults_applied"], [{"key": "project", "value": "PVM", "source": "profile"}])

    def test_jira_explicit_project_beats_the_profile(self):
        self.write_sberworks(jira_project="PVM")
        code, data = self.jira_json(FakeSberworks(), "search", "комиссия", "--project", "CRPV", "--confirm-sberworks")
        self.assertIn('project = "CRPV"', data["jql"])
        self.assertNotIn("defaults_applied", data, "явное значение — не умолчание")

    def test_jira_similar_takes_the_project_from_the_profile(self):
        self.write_sberworks(jira_project="PVM")
        code, data = self.jira_json(FakeSberworks(), "similar", "срок действия счёта", "--confirm-sberworks")
        self.assertEqual(code, 0)
        self.assertIn('project = "PVM"', data["jql"])
        self.assertEqual(data["defaults_applied"], [{"key": "project", "value": "PVM", "source": "profile"}])

    def test_jira_raw_jql_is_left_alone(self):
        self.write_sberworks(jira_project="PVM")
        code, data = self.jira_json(FakeSberworks(), "search", "--jql", "labels = чек", "--confirm-sberworks")
        self.assertEqual(data["jql"], "labels = чек", "готовый JQL написан пользователем целиком")
        self.assertNotIn("defaults_applied", data)

    def test_jira_create_takes_the_project_from_the_profile(self):
        self.write_sberworks(jira_project="PVM")
        code, data = self.jira_json(FakeSberworks(), "create", "--type", "Task", "--summary",
                                    "Срок действия счёта", "--confirm-sberworks")
        self.assertEqual(code, 0)
        self.assertEqual(data["mode"], "dry-run", "создание без --execute остаётся dry-run")
        self.assertEqual(data["request"]["fields"]["project"], {"key": "PVM"})
        self.assertEqual(data["defaults_applied"], [{"key": "project", "value": "PVM", "source": "profile"}])

    def test_jira_create_without_a_profile_keeps_the_old_refusal(self):
        code, out, err = self.run_jira(FakeSberworks(), "create", "--type", "Task",
                                       "--summary", "Срок действия счёта", "--confirm-sberworks")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("error: the following arguments are required: --project", err)
        self.assertIn("usage: %s create" % Path(sys.argv[0]).name, err)

    def test_jira_no_profile_flag_restores_the_old_refusal(self):
        self.write_sberworks(jira_project="PVM")
        code, out, err = self.run_jira(FakeSberworks(), "create", "--type", "Task", "--summary",
                                       "Срок действия счёта", "--confirm-sberworks", "--no-profile")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("error: the following arguments are required: --project", err)

    def test_jira_no_profile_flag_searches_across_projects(self):
        self.write_sberworks(jira_project="PVM")
        code, data = self.jira_json(FakeSberworks(), "search", "комиссия", "--confirm-sberworks", "--no-profile")
        self.assertNotIn("project", data["jql"])
        self.assertNotIn("defaults_applied", data)

    def test_jira_gate_is_not_lifted_by_a_filled_profile(self):
        self.write_sberworks(jira_project="PVM")
        fake = FakeSberworks()
        code, out, err = self.run_jira(fake, "search", "комиссия")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        error = json.loads(err)
        self.assertFalse(error["ok"])
        self.assertEqual(error["gate"], "sberworks-not-default")
        self.assertEqual(error["useInstead"], "swtr_task_tool.py get <код>")
        self.assertNotIn("defaults_applied", error, "гейт срабатывает до чтения профиля")
        self.assertEqual(fake.urls, [], "гейт обязан сработать до сети")

    def test_jira_gate_beats_the_missing_project(self):
        code, out, err = self.run_jira(FakeSberworks(), "create", "--type", "Task", "--summary", "Срок действия")
        self.assertEqual(code, 1, "сначала отказ по системе, и только за ним разговор про аргументы")
        self.assertEqual(json.loads(err)["gate"], "sberworks-not-default")

    def test_jira_refusal_names_the_profile_as_the_source(self):
        self.write_sberworks(jira_project="NETTAKOGO")

        def refuse(*a, **k):
            raise RuntimeError("HTTP 400: project does not exist")

        code, out, err = self.run_jira(refuse, "search", "комиссия", "--confirm-sberworks")

        self.assertEqual(code, 1)
        error = json.loads(err)
        self.assertEqual(error["defaults_applied"], [{"key": "project", "value": "NETTAKOGO", "source": "profile"}])
        self.assertIn("профиля", error["hint"])
        self.assertIn("--no-profile", error["hint"])

    def test_jira_refusal_on_an_explicit_project_says_nothing_about_the_profile(self):
        self.write_sberworks(jira_project="PVM")

        def refuse(*a, **k):
            raise RuntimeError("HTTP 400: project does not exist")

        code, out, err = self.run_jira(refuse, "search", "комиссия", "--project", "CRPV", "--confirm-sberworks")

        self.assertEqual(code, 1)
        error = json.loads(err)
        self.assertNotIn("defaults_applied", error)
        self.assertNotIn("hint", error)

    def test_jira_project_does_not_leak_into_issue_commands(self):
        self.write_sberworks(jira_project="PVM")
        code, data = self.jira_json(FakeSberworks({"comments": [], "total": 0}), "comments", "PVM-1",
                                    "--confirm-sberworks")
        self.assertEqual(code, 0)
        self.assertNotIn("defaults_applied", data, "задача адресуется ключом — проекту тут делать нечего")

    def test_confluence_search_takes_the_space_from_the_profile(self):
        self.write_sberworks(confluence_space="TEAM")
        code, data = self.conf_json(FakeSberworks(), "search", "регламент", "--confirm-sberworks")
        self.assertEqual(code, 0)
        self.assertIn('space = "TEAM"', data["cql"])
        self.assertEqual(data["defaults_applied"], [{"key": "space", "value": "TEAM", "source": "profile"}])

    def test_confluence_explicit_space_beats_the_profile(self):
        self.write_sberworks(confluence_space="TEAM")
        code, data = self.conf_json(FakeSberworks(), "search", "регламент", "--space", "OPS", "--confirm-sberworks")
        self.assertIn('space = "OPS"', data["cql"])
        self.assertNotIn("defaults_applied", data)

    def test_confluence_raw_cql_is_left_alone(self):
        self.write_sberworks(confluence_space="TEAM")
        code, data = self.conf_json(FakeSberworks(), "search", "--cql", 'title ~ "регламент"', "--confirm-sberworks")
        self.assertEqual(data["cql"], 'title ~ "регламент"')
        self.assertNotIn("defaults_applied", data)

    def test_confluence_without_a_profile_the_search_stays_global(self):
        code, data = self.conf_json(FakeSberworks(), "search", "регламент", "--confirm-sberworks")
        self.assertEqual(data["cql"], 'text ~ "регламент"')
        self.assertNotIn("defaults_applied", data)

    def test_confluence_no_profile_flag_ignores_the_profile(self):
        self.write_sberworks(confluence_space="TEAM")
        code, data = self.conf_json(FakeSberworks(), "search", "регламент", "--confirm-sberworks", "--no-profile")
        self.assertEqual(data["cql"], 'text ~ "регламент"')
        self.assertNotIn("defaults_applied", data)

    def test_confluence_gate_is_not_lifted_by_a_filled_profile(self):
        self.write_sberworks(confluence_space="TEAM")
        fake = FakeSberworks()
        code, out, err = self.run_conf(fake, "search", "регламент")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        error = json.loads(err)
        self.assertFalse(error["ok"])
        self.assertEqual(error["gate"], "sberworks-not-default")
        self.assertNotIn("defaults_applied", error, "гейт срабатывает до чтения профиля")
        self.assertEqual(fake.urls, [], "гейт обязан сработать до сети")

    def test_confluence_space_does_not_leak_into_page_commands(self):
        self.write_sberworks(confluence_space="TEAM")
        code, data = self.conf_json(FakeSberworks({"id": "1001", "title": "Регламент"}), "get", "1001",
                                    "--confirm-sberworks")
        self.assertEqual(code, 0)
        self.assertNotIn("defaults_applied", data, "страница адресуется идентификатором")

    def test_a_corrupt_profile_counts_as_no_profile(self):
        path = self.write_sberworks(jira_project="PVM", confluence_space="TEAM")
        path.write_text("{ это не json", encoding="utf-8")
        code, data = self.jira_json(FakeSberworks(), "search", "комиссия", "--confirm-sberworks")
        self.assertNotIn("project", data["jql"])
        self.assertNotIn("defaults_applied", data)

    def test_an_unsupported_schema_version_counts_as_no_profile(self):
        path = self.write_sberworks(jira_project="PVM")
        path.write_text(json.dumps({"version": 2, "sberworks": {"jira_project": "PVM"}}), encoding="utf-8")
        code, data = self.jira_json(FakeSberworks(), "search", "комиссия", "--confirm-sberworks")
        self.assertNotIn("project", data["jql"])
        self.assertNotIn("defaults_applied", data)

    def test_a_hand_edited_project_key_counts_as_no_profile(self):
        self.write_sberworks(jira_project='PVM" or key = "X')
        code, data = self.jira_json(FakeSberworks(), "search", "комиссия", "--confirm-sberworks")
        self.assertNotIn("project", data["jql"])
        self.assertNotIn("defaults_applied", data)

    def test_the_source_vocabulary_is_the_agreed_one(self):
        self.write_sberworks(jira_project="PVM", confluence_space="TEAM")
        _, jira_data = self.jira_json(FakeSberworks(), "search", "комиссия", "--confirm-sberworks")
        _, conf_data = self.conf_json(FakeSberworks(), "search", "регламент", "--confirm-sberworks")
        for entries in (jira_data["defaults_applied"], conf_data["defaults_applied"]):
            self.assertTrue(all(set(entry) == {"key", "value", "source"} for entry in entries))
            self.assertTrue(all(entry["source"] in {"profile", "env", "os_user", "credential"} for entry in entries))


if __name__ == "__main__":
    unittest.main()
