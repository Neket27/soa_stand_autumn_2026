"""Профиль пользователя: подсказки онбординга (``guru_profile.py suggest``).

REQ-007: подсказки строятся из данных самого пользователя с явным
происхождением, пороги держатся точно (пространство -- от одной задачи, тип --
от десяти задач и двух третей), а отказ трекера даёт частичную сводку с
``notes`` и кодом 0, потому что онбординг обязан продолжиться. Сетевой слой
подменяется целиком, домашний каталог -- временный: ни один тест не открывает
сокет и не трогает профиль разработчика.

Запись профиля -- в tests/test_profile_store.py, чтение -- в
tests/test_profile_read.py.
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

import _swtr_common as common  # noqa: E402
import guru_profile as gp  # noqa: E402


def run(argv):
    """``(exit code, stdout JSON or None, stderr JSON or None)`` of one CLI call."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = gp.main(argv)
    return code, _parse(out.getvalue()), _parse(err.getvalue())


def _parse(text):
    text = text.strip()
    return json.loads(text) if text else None


SPACES_RESPONSE = {
    "content": [
        {"code": "PVM", "name": "PVM", "type": {"code": "AC21_new", "name": "AC21 NEW"}},
        {"code": "CRPV", "name": "CRPV", "type": {"code": "AC21_new", "name": "AC21 NEW"}},
        {"code": "OTHER", "name": "Other Space", "type": {"code": "AC21_new", "name": "AC21 NEW"}},
    ],
    "pageSize": 20,
    "hasNext": False,
}
USERS_RESPONSE = [{"login": "familia.i.o", "code": "Familia.I.O"}]


def units(*groups, total=None):
    """find/tql page of synthetic units: ``groups`` are ``(space, suit, how_many)``.

    ``total`` is what the tracker reports as ``totalElements``; pass a bigger
    number than the page holds to model a sample the page cut short.
    """
    rows = []
    for space, suit, count in groups:
        for _ in range(count):
            rows.append({
                "unit": {"code": "%s-%d" % (space, len(rows) + 1), "summary": "synthetic", "space": space,
                         "suit": suit, "updatedAt": "2026-01-01T00:00:00Z"},
                "attributes": [],
            })
    return {"content": rows, "page": {"page": 0, "size": 200},
            "totalElements": len(rows) if total is None else total}


class FakeTracker:
    """Stand-in for ``swtr_task_tool.request_http``: routes by URL, records every call, fails on demand.

    ``fail`` names routes (``spaces``, ``users``, ``sample``) that raise the same
    RuntimeError request_http raises after its retries, so the command sees a
    real-looking tracker failure without a socket or a sleep.
    """

    def __init__(self, sample=None, spaces=SPACES_RESPONSE, users=USERS_RESPONSE, fail=()):
        self.sample = sample if sample is not None else units()
        self.spaces = spaces
        self.users = users
        self.fail = set(fail)
        self.calls = []

    def __call__(self, method, url, headers, *, body=None, timeout=30.0, system="task_tracker", **kwargs):
        self.calls.append({"method": method, "url": url, "body": body, "timeout": timeout, "system": system})
        if url.endswith("/rest/api/space/v3/find"):
            if "spaces" in self.fail:
                raise RuntimeError("HTTP 503 for %s: gateway unavailable" % url)
            return self.spaces, {}, 200
        if url.endswith("/rest/api/user/v1/find"):
            if "users" in self.fail:
                raise RuntimeError("Network error for %s: timed out" % url)
            return self.users, {}, 200
        if url.endswith("/rest/api/unit/v3/find/tql"):
            if "sample" in self.fail:
                raise RuntimeError("Network error for %s: timed out" % url)
            return self.sample, {}, 200
        raise AssertionError("unexpected request: %s %s" % (method, url))

    def urls(self, suffix):
        return [call for call in self.calls if call["url"].endswith(suffix)]


class SuggestTest(unittest.TestCase):
    """``suggest``: подсказки из данных пользователя (REQ-007) поверх подменённого сетевого слоя."""

    ENV_KEYS = (
        "SWTR_LOGIN", "SWTR_BASIC_AUTH", "SWTR_BEARER_TOKEN", "SWTR_AUTHORIZATION", "TASKTRACKER_AUTHORIZATION",
        "SWTR_TOKEN", "TASKTRACKER_TOKEN", "SWTR_COOKIE", "SWTR_BASE_URL", "USER",
        "SOURCE_CONTROL_PROBE_TENANT", "SOURCE_CONTROL_PROBE_PROJECT",
    )

    def setUp(self):
        self._saved = dict(os.environ)
        for name in self.ENV_KEYS:
            os.environ.pop(name, None)
        # A fake credential so build_headers lets the (patched) request through; the
        # login comes from SWTR_LOGIN unless a test says otherwise.
        os.environ["SWTR_BEARER_TOKEN"] = "test-token-value"
        os.environ["SWTR_LOGIN"] = "familia.i.o"
        self._tmp = tempfile.TemporaryDirectory()
        # Never created: the user's real ~/.swtr-review.env must stay out of the test.
        self.env_file = os.path.join(self._tmp.name, "absent.env")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
        self._tmp.cleanup()

    def suggest(self, tracker, *extra):
        # Домашний каталог подменён на весь вызов: у ``suggest`` нет ``--home``,
        # и регрессия, которая начнёт писать, обязана попасть во временный
        # каталог, а не в настоящий профиль разработчика. Path.home() читает
        # HOME только на POSIX; на Windows ntpath.expanduser берёт USERPROFILE
        # (или HOMEDRIVE+HOMEPATH), поэтому подменяются все три.
        home = os.path.join(self._tmp.name, "home")
        os.makedirs(home, exist_ok=True)
        with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home,
                                          "HOMEDRIVE": "", "HOMEPATH": home}), \
                mock.patch("swtr_task_tool.request_http", new=tracker):
            return run(["suggest", "--env-file", self.env_file, *extra])

    def test_suggest_never_writes_the_profile(self):
        # REQ-007 и обещание /guru-start «ничего никуда не записывается»: подсказки
        # только читают трекер, файл заводит потом ``set``. Проверяется весь
        # домашний каталог, а не один путь: запись мимо profile_path() — та же
        # регрессия.
        home = os.path.join(self._tmp.name, "home")
        os.makedirs(home, exist_ok=True)
        self.assertEqual(sorted(os.listdir(home)), [])

        code, out, err = self.suggest(FakeTracker(units(("PVM", "task", 12))))

        self.assertEqual(code, 0, err)
        self.assertTrue(out["ok"])
        self.assertFalse(common.profile_path(Path(home)).exists(), "suggest завёл файл профиля")
        self.assertEqual(sorted(os.listdir(home)), [], "suggest вообще ничего не пишет в HOME")

    def test_suggest_leaves_an_existing_profile_untouched(self):
        # Подсказки предлагают значения, а не применяют их: прогон поверх готового
        # профиля не имеет права переписать ни байта.
        home = os.path.join(self._tmp.name, "home")
        os.makedirs(home, exist_ok=True)
        path = common.profile_path(Path(home))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "updated_at": "2026-08-19T10:15:00Z",
                                    "login": "alice"}), encoding="utf-8")
        before = path.read_bytes()

        code, out, err = self.suggest(FakeTracker(units(("PVM", "task", 12))))

        self.assertEqual(code, 0, err)
        self.assertEqual(out["primary_space"]["value"], "PVM", "подсказка всё-таки посчитана")
        self.assertEqual(path.read_bytes(), before, "suggest переписал профиль")

    def test_space_hint_from_a_single_task(self):
        code, out, err = self.suggest(FakeTracker(units(("PVM", "task", 1))))

        self.assertEqual(code, 0, err)
        self.assertTrue(out["ok"])
        self.assertEqual(out["command"], "suggest")
        self.assertEqual(out["primary_space"]["value"], "PVM")
        self.assertEqual(out["primary_space"]["options"], ["PVM"])
        self.assertEqual(out["primary_space"]["source"], "tasks")
        self.assertEqual(out["primary_space"]["basis"], "по вашим задачам: 1 в PVM")
        self.assertEqual(out["spaces_in_view"]["value"], ["PVM"])
        self.assertIsNone(out["default_suit"], "одна задача — порог типа не достигнут")
        self.assertTrue(any("не меньше 10" in note for note in out["notes"]), out["notes"])

    def test_spaces_are_ranked_by_task_count_with_the_origin_text(self):
        tracker = FakeTracker(units(("CRPV", "task", 5), ("PVM", "task", 34)))

        code, out, _ = self.suggest(tracker)

        self.assertEqual(code, 0)
        self.assertEqual(out["primary_space"]["value"], "PVM")
        self.assertEqual(out["primary_space"]["options"], ["PVM", "CRPV"])
        self.assertEqual(out["primary_space"]["basis"], "по вашим задачам: 34 в PVM, 5 в CRPV")
        self.assertEqual(out["spaces_in_view"]["value"], ["PVM", "CRPV"])
        self.assertEqual(out["spaces_in_view"]["basis"], out["primary_space"]["basis"])
        self.assertEqual([(s["code"], s["tasks"]) for s in out["spaces"]],
                         [("PVM", 34), ("CRPV", 5), ("OTHER", 0)],
                         "доступные пространства с числом задач, по убыванию, без задач — последними")
        self.assertEqual(out["spaces"][0]["name"], "PVM")
        self.assertEqual(out["spaces"][0]["type"], "AC21_new")
        self.assertEqual(out["sample"]["by_space"], [{"code": "PVM", "tasks": 34}, {"code": "CRPV", "tasks": 5}])
        self.assertEqual(out["sample"]["count"], 39)
        self.assertEqual(out["sample"]["total"], 39)

    def test_suit_hint_at_ten_tasks_and_two_thirds(self):
        code, out, _ = self.suggest(FakeTracker(units(("PVM", "task", 7), ("PVM", "bug", 3))))

        self.assertEqual(code, 0)
        suit = out["default_suit"]
        self.assertEqual(suit["value"], "task")
        self.assertEqual((suit["tasks"], suit["of"], suit["share"]), (7, 10, 0.7))
        self.assertEqual(suit["source"], "tasks")
        self.assertEqual(suit["basis"], "по вашим задачам: task — 7 из 10 (70%)")
        self.assertEqual(out["sample"]["by_suit"], [{"code": "task", "tasks": 7}, {"code": "bug", "tasks": 3}])

    def test_exactly_two_thirds_qualifies(self):
        code, out, _ = self.suggest(FakeTracker(units(("PVM", "task", 8), ("PVM", "bug", 4))))

        self.assertEqual(code, 0)
        self.assertEqual(out["default_suit"]["value"], "task")
        self.assertEqual(out["default_suit"]["basis"], "по вашим задачам: task — 8 из 12 (67%)")

    def test_no_suit_hint_below_ten_tasks(self):
        code, out, _ = self.suggest(FakeTracker(units(("PVM", "task", 9))))

        self.assertEqual(code, 0)
        self.assertIsNone(out["default_suit"])
        self.assertEqual(out["primary_space"]["value"], "PVM", "подсказка по пространству от порога типа не зависит")
        self.assertTrue(any("в выборке 9, нужно не меньше 10" in note for note in out["notes"]), out["notes"])

    def test_a_sample_without_suits_says_so_instead_of_contradicting_its_own_numbers(self):
        # Достаточно задач, но ни у одной нет кода типа: заметка «в выборке 15,
        # нужно не меньше 10» противоречила бы собственным числам.
        code, out, _ = self.suggest(FakeTracker(units(("PVM", None, 15))))

        self.assertEqual(code, 0)
        self.assertIsNone(out["default_suit"])
        self.assertEqual(out["sample"]["by_suit"], [])
        self.assertTrue(any("ни у одной задачи выборки не назван тип" in note for note in out["notes"]),
                        out["notes"])

    def test_no_suit_hint_below_two_thirds(self):
        code, out, _ = self.suggest(FakeTracker(units(("PVM", "task", 7), ("PVM", "bug", 5))))

        # Порог назван самой константой: текст, набранный словами, разошёлся бы
        # с ней молча при первой же её правке.
        share = "%d/%d" % gp.SUGGEST_SUIT_MIN_SHARE
        self.assertEqual(code, 0)
        self.assertIsNone(out["default_suit"])
        self.assertTrue(any("task — 7 из 12 (58%)" in note and share in note for note in out["notes"]),
                        out["notes"])

    def test_sample_request_is_the_assigned_query_with_an_explicit_limit_of_200(self):
        tracker = FakeTracker(units(("PVM", "task", 1)))

        code, out, _ = self.suggest(tracker)

        self.assertEqual(code, 0)
        tql_calls = tracker.urls("/rest/api/unit/v3/find/tql")
        self.assertEqual(len(tql_calls), 1, "одна страница выборки")
        body = tql_calls[0]["body"]
        self.assertEqual(body["page"], {"page": 0, "size": 200})
        self.assertEqual(body["query"], 'assigned_to in ("Familia.I.O")',
                         "тот же TQL, что у assigned --me --status all: логин заменён на externalId, без "
                         "условий по статусу, пространству и типу")
        self.assertEqual(tracker.urls("/rest/api/user/v1/find")[0]["body"], ["familia.i.o"])
        self.assertEqual(out["sample"]["tql"], body["query"])
        self.assertEqual(out["sample"]["limit"], 200)
        # Не литерал рядом с литералом: в отчёте обязан стоять ровно тот адрес,
        # по которому ушёл запрос, иначе смена эндпойнта заставит вывод врать.
        self.assertTrue(tql_calls[0]["url"].endswith(out["sample"]["endpoint"]),
                        (tql_calls[0]["url"], out["sample"]["endpoint"]))
        self.assertEqual(out["sample"]["assignee_values"], ["Familia.I.O"])
        self.assertNotIn("rows", out["sample"], "сырые строки выборки в вывод не идут")
        self.assertEqual(len(tracker.urls("/rest/api/space/v3/find")), 1)

    def test_tracker_failure_is_a_partial_summary_not_an_error(self):
        tracker = FakeTracker(fail={"sample"})

        code, out, err = self.suggest(tracker)

        self.assertEqual(code, 0, "сетевой отказ — не ошибка команды")
        self.assertIsNone(err)
        self.assertTrue(out["ok"])
        self.assertIsNone(out["primary_space"])
        self.assertIsNone(out["spaces_in_view"])
        self.assertIsNone(out["default_suit"])
        self.assertIsNone(out["sample"])
        self.assertTrue(any("выборка назначенных задач недоступна" in note and "timed out" in note
                            for note in out["notes"]), out["notes"])
        self.assertEqual([s["code"] for s in out["spaces"]], ["CRPV", "OTHER", "PVM"],
                         "список пространств трекера остаётся, по коду")
        self.assertTrue(all("tasks" not in s for s in out["spaces"]),
                        "без выборки числа задач нет вовсе, а не ноль")
        self.assertEqual(out["login"]["value"], "familia.i.o")
        self.assertEqual(out["source_control"], {}, "переменных проверки доступа нет — подсказки нет")

    def test_spaces_failure_keeps_the_suggestions_from_the_sample(self):
        tracker = FakeTracker(units(("PVM", "task", 3)), fail={"spaces"})

        code, out, _ = self.suggest(tracker)

        self.assertEqual(code, 0)
        self.assertEqual(out["primary_space"]["value"], "PVM")
        self.assertEqual(out["spaces"], [{"code": "PVM", "name": None, "type": None, "tasks": 3}],
                         "пространства — только из выборки, имени у них нет")
        self.assertTrue(any("список пространств трекера недоступен" in note and "HTTP 503" in note
                            for note in out["notes"]), out["notes"])

    def test_everything_down_still_gives_login_and_source_control(self):
        tracker = FakeTracker(fail={"spaces", "users", "sample"})

        code, out, _ = self.suggest(tracker)

        self.assertEqual(code, 0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["spaces"], [])
        self.assertIsNone(out["primary_space"])
        self.assertEqual(out["login"], {"value": "familia.i.o", "source": "env", "basis": "переменная SWTR_LOGIN"})
        self.assertEqual(out["source_control"], {}, "без настроенных переменных проверки доступа подсказки нет")
        self.assertGreaterEqual(len(out["notes"]), 2)

    def test_user_lookup_failure_falls_back_to_the_login_candidates(self):
        tracker = FakeTracker(units(("PVM", "task", 2)), fail={"users"})

        code, out, _ = self.suggest(tracker)

        self.assertEqual(code, 0)
        self.assertEqual(out["primary_space"]["value"], "PVM")
        self.assertEqual(out["sample"]["assignee_values"], ["familia.i.o", "Familia.I.O"])
        self.assertTrue(any("externalId" in note for note in out["sample"]["notes"]))

    def test_empty_sample_gives_no_space_hint_and_says_so(self):
        code, out, _ = self.suggest(FakeTracker(units()))

        self.assertEqual(code, 0)
        self.assertIsNone(out["primary_space"])
        self.assertIsNone(out["default_suit"])
        self.assertEqual(out["sample"]["count"], 0)
        self.assertEqual([(s["code"], s["tasks"]) for s in out["spaces"]],
                         [("CRPV", 0), ("OTHER", 0), ("PVM", 0)])
        self.assertTrue(any("нет задач, назначенных на familia.i.o" in note for note in out["notes"]), out["notes"])
        self.assertFalse(any("не меньше 10" in note for note in out["notes"]),
                         "при пустой выборке порог типа не комментируется отдельно")

    def test_login_from_the_basic_credential_without_the_password(self):
        os.environ.pop("SWTR_LOGIN")
        os.environ.pop("SWTR_BEARER_TOKEN")
        os.environ["SWTR_BASIC_AUTH"] = "familia.i.o:sekret-pw"
        tracker = FakeTracker(units(("PVM", "task", 1)))
        out_text = io.StringIO()

        with mock.patch("swtr_task_tool.request_http", new=tracker), contextlib.redirect_stdout(out_text):
            code = gp.main(["suggest", "--env-file", self.env_file])

        self.assertEqual(code, 0)
        out = json.loads(out_text.getvalue())
        self.assertEqual(out["login"]["value"], "familia.i.o")
        self.assertEqual(out["login"]["source"], "credential")
        self.assertIn("SWTR_BASIC_AUTH", out["login"]["basis"])
        self.assertNotIn("sekret-pw", out_text.getvalue(), "пароль не должен попасть в вывод")
        self.assertEqual(tracker.urls("/rest/api/user/v1/find")[0]["body"], ["familia.i.o"])

    def test_a_credential_without_a_colon_is_not_a_login_and_never_reaches_stdout(self):
        # Голый токен, вставленный в SWTR_BASIC_AUTH, -- не пара «логин:пароль».
        # partition() отдал бы его целиком как «логин», и он ушёл бы в stdout,
        # в подсказку и оттуда в профиль (REQ-003).
        os.environ.pop("SWTR_LOGIN")
        os.environ.pop("SWTR_BEARER_TOKEN")
        os.environ["SWTR_BASIC_AUTH"] = "glpat-SEKRETTOKENVALUE123"
        out_text = io.StringIO()

        with mock.patch("swtr_task_tool.request_http", new=FakeTracker()), \
                contextlib.redirect_stdout(out_text):
            code = gp.main(["suggest", "--env-file", self.env_file])

        self.assertEqual(code, 0)
        out = json.loads(out_text.getvalue())
        self.assertIsNone(out["login"]["value"])
        self.assertIn("нет пары", out["login"]["basis"])
        self.assertNotIn("glpat-SEKRETTOKENVALUE123", out_text.getvalue())

    def test_env_login_wins_over_the_basic_credential(self):
        os.environ["SWTR_BASIC_AUTH"] = "other.login:pw"

        code, out, _ = self.suggest(FakeTracker(units(("PVM", "task", 1))))

        self.assertEqual(code, 0)
        self.assertEqual(out["login"], {"value": "familia.i.o", "source": "env", "basis": "переменная SWTR_LOGIN"})

    def test_without_a_login_no_sample_is_requested_and_user_is_not_a_source(self):
        os.environ.pop("SWTR_LOGIN")
        os.environ["USER"] = "os-account"
        tracker = FakeTracker(units(("PVM", "task", 1)))

        code, out, _ = self.suggest(tracker)

        self.assertEqual(code, 0)
        self.assertEqual(out["login"]["value"], None)
        self.assertEqual(out["login"]["source"], None)
        self.assertIsNone(out["sample"])
        self.assertIsNone(out["primary_space"])
        self.assertEqual(tracker.urls("/rest/api/unit/v3/find/tql"), [], "без логина выборка не запрашивается")
        self.assertEqual(tracker.urls("/rest/api/user/v1/find"), [])
        self.assertEqual(len(tracker.urls("/rest/api/space/v3/find")), 1, "а список пространств — да")
        self.assertTrue(all("tasks" not in s for s in out["spaces"]))
        self.assertTrue(any("логин не извлекается" in note for note in out["notes"]), out["notes"])

    def test_tracker_not_configured_skips_the_network_and_continues(self):
        os.environ.pop("SWTR_BEARER_TOKEN")
        tracker = FakeTracker(units(("PVM", "task", 1)))

        code, out, _ = self.suggest(tracker)

        self.assertEqual(code, 0)
        self.assertTrue(out["ok"])
        self.assertEqual(tracker.calls, [], "без креда в сеть не ходим")
        self.assertEqual(out["spaces"], [])
        self.assertIsNone(out["sample"])
        self.assertEqual(out["login"]["value"], "familia.i.o", "логин из SWTR_LOGIN креда не требует")
        self.assertTrue(any("доступ к трекеру не настроен" in note and "/guru_doctor" in note
                            for note in out["notes"]), out["notes"])

    def test_source_control_hints_only_from_the_configured_probe_variables(self):
        # REQ-006: вопрос про SourceControl задаётся «только при наличии
        # настроенных параметров проверки доступа». Общесемейное умолчание —
        # чужой проект: подсказкой оно быть не может, и в ответе его нет.
        code, by_default, _ = self.suggest(FakeTracker())
        os.environ["SOURCE_CONTROL_PROBE_TENANT"] = "11111111-2222-3333-4444-555555555555"
        os.environ["SOURCE_CONTROL_PROBE_PROJECT"] = "team-project"
        code2, by_env, _ = self.suggest(FakeTracker())

        self.assertEqual((code, code2), (0, 0))
        self.assertEqual(by_default["source_control"], {})
        self.assertEqual(by_env["source_control"]["tenant"],
                         {"value": "11111111-2222-3333-4444-555555555555", "source": "env",
                          "basis": "переменная SOURCE_CONTROL_PROBE_TENANT"})
        self.assertEqual(by_env["source_control"]["project"],
                         {"value": "team-project", "source": "env", "basis": "переменная SOURCE_CONTROL_PROBE_PROJECT"})

    def test_only_one_probe_variable_gives_only_that_key(self):
        os.environ["SOURCE_CONTROL_PROBE_PROJECT"] = "team-project"

        code, out, _ = self.suggest(FakeTracker())

        self.assertEqual(code, 0)
        self.assertEqual(list(out["source_control"]), ["project"])

    def test_env_file_is_loaded_before_the_login_is_resolved(self):
        os.environ.pop("SWTR_LOGIN")
        env_file = os.path.join(self._tmp.name, "cred.env")
        with open(env_file, "w", encoding="utf-8") as handle:
            handle.write('SWTR_LOGIN="from.file"\n')
        tracker = FakeTracker(units(("PVM", "task", 1)))

        with mock.patch("swtr_task_tool.request_http", new=tracker):
            code, out, _ = run(["suggest", "--env-file", env_file])

        self.assertEqual(code, 0)
        self.assertEqual(out["login"], {"value": "from.file", "source": "env", "basis": "переменная SWTR_LOGIN"})

    def test_timeout_flag_reaches_the_requests(self):
        tracker = FakeTracker(units(("PVM", "task", 1)))

        code, _, _ = self.suggest(tracker, "--timeout", "7")

        self.assertEqual(code, 0)
        self.assertTrue(tracker.calls)
        self.assertTrue(all(call["timeout"] == 7.0 for call in tracker.calls), tracker.calls)

    def test_basis_text_caps_the_space_list(self):
        ranked = [("S%d" % i, 10 - i) for i in range(8)]

        text = gp.spaces_basis(ranked)

        self.assertEqual(text, "по вашим задачам: 10 в S0, 9 в S1, 8 в S2, 7 в S3, 6 в S4, и ещё 3")

    def test_a_capped_sample_says_the_page_is_not_the_newest(self):
        # REQ-007 берёт «не больше 200 задач одной страницей»: когда трекер
        # говорит, что задач больше, ответ обязан назвать это ограничение, а не
        # выдавать долю по одной странице за долю по всем задачам.
        tracker = FakeTracker(units(("PVM", "task", 12), total=500))

        code, out, _ = self.suggest(tracker)

        self.assertEqual(code, 0)
        self.assertTrue(out["sample"]["truncated"])
        self.assertTrue(any("одной странице выборки" in note and "не срез последних по дате" in note
                            for note in out["notes"]), out["notes"])

    def test_an_uncapped_sample_says_nothing_about_the_page(self):
        code, out, _ = self.suggest(FakeTracker(units(("PVM", "task", 12))))

        self.assertEqual(code, 0)
        self.assertFalse(out["sample"]["truncated"])
        self.assertFalse(any("одной странице выборки" in note for note in out["notes"]), out["notes"])


if __name__ == "__main__":
    unittest.main()
