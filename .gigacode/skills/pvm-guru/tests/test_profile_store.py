"""Профиль пользователя: писатель (``guru_profile.py``).

Профиль -- единственное несекретное пользовательское состояние, которое читают
все CLI навыка, поэтому писатель обязан быть единственным и аккуратным: запись
целиком и атомарно, права 0600, нормализация регистра и дублей до проверки,
отклонение недопустимых кодов без порчи прежнего файла, секретоподобные ключи --
отказ на запись и удаление с отчётом на перезаписи, неизвестные ключи --
сохранить. И ни одна файловая команда не открывает сокет.

Чтение профиля и порядок источников -- в tests/test_profile_read.py, подсказки
онбординга -- в tests/test_profile_suggest.py.
"""

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import threading
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


def profile_file(home):
    return Path(home) / ".config" / "pvm-guru" / "profile.json"


def write_raw(home, text, encoding="utf-8"):
    path = profile_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding=encoding)
    return path


class WriteReadTest(unittest.TestCase):
    def test_first_save_creates_directory_file_and_stamps(self):
        with tempfile.TemporaryDirectory() as home:
            code, out, err = run(["set", "--home", home, "login=familia.i.o",
                                  "task_tracker.primary_space=PVM", "task_tracker.spaces=PVM,CRPV"])
            self.assertEqual(code, 0, err)
            self.assertEqual(out["path"], str(profile_file(home)))
            self.assertEqual(out["version"], 1)
            self.assertRegex(out["updated_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            self.assertEqual(out["profile"]["login"], "familia.i.o")
            self.assertEqual(out["profile"]["task_tracker"]["primary_space"], "PVM")
            self.assertEqual(out["profile"]["task_tracker"]["spaces"], ["PVM", "CRPV"])
            self.assertEqual(out["changed"], ["login", "task_tracker.primary_space", "task_tracker.spaces"])

            on_disk = json.loads(profile_file(home).read_text(encoding="utf-8"))
            self.assertEqual(list(on_disk)[:2], ["version", "updated_at"], "штампы семейства идут первыми")
            loaded = common.load_profile(Path(home))
            self.assertEqual(loaded.status, "ok")
            self.assertEqual(loaded.version, 1)
            self.assertEqual(common.profile_get(loaded, "task_tracker.primary_space"), "PVM")

    def test_file_mode_is_owner_only(self):
        with tempfile.TemporaryDirectory() as home:
            run(["set", "--home", home, "login=alice"])
            mode = stat.S_IMODE(profile_file(home).stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_show_reports_missing_and_points_to_onboarding(self):
        with tempfile.TemporaryDirectory() as home:
            code, out, _ = run(["show", "--home", home])
            # Читатель не заводит файл: «профиля нет» обязано остаться правдой и
            # после ответа, иначе следующий show сообщит о пустом профиле вместо
            # отсутствующего и онбординг из подсказки исчезнет.
            self.assertFalse(profile_file(home).exists(), "show не создаёт файл")
        self.assertEqual(code, 0)
        self.assertEqual(out["status"], "missing")
        self.assertEqual(out["profile"], {})
        self.assertTrue(any("/guru-start" in note for note in out["notes"]))

    def test_show_returns_full_map_path_and_timestamp(self):
        with tempfile.TemporaryDirectory() as home:
            run(["set", "--home", home, "login=alice", "role=analyst"])
            code, out, _ = run(["show", "--home", home])
        self.assertEqual(code, 0)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["path"], str(profile_file(home)))
        self.assertTrue(out["updated_at"])
        self.assertEqual(out["profile"], {"login": "alice", "role": "analyst"})
        self.assertNotIn("version", out["profile"], "штампы показываются отдельно, не как ключи профиля")

    def test_partial_change_keeps_other_keys_and_names_only_changed(self):
        with tempfile.TemporaryDirectory() as home:
            run(["set", "--home", home, "login=alice", "task_tracker.primary_space=PVM"])
            code, out, _ = run(["set", "--home", home, "task_tracker.primary_space=CRPV"])
        self.assertEqual(code, 0)
        self.assertEqual(out["profile"]["login"], "alice")
        self.assertEqual(out["profile"]["task_tracker"]["primary_space"], "CRPV")
        self.assertEqual(out["changed"], ["task_tracker.primary_space"])

    def test_setting_the_same_value_changes_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            run(["set", "--home", home, "task_tracker.primary_space=PVM"])
            code, out, _ = run(["set", "--home", home, "task_tracker.primary_space=pvm"])
        self.assertEqual(code, 0)
        self.assertEqual(out["changed"], [])

    def test_unset_and_empty_value_remove_key_and_empty_section(self):
        with tempfile.TemporaryDirectory() as home:
            run(["set", "--home", home, "login=alice", "task_tracker.product=PVM-CORE",
                 "source_control.project=pvm"])
            code, out, _ = run(["unset", "--home", home, "task_tracker.product"])
            self.assertEqual(code, 0)
            self.assertEqual(out["changed"], ["task_tracker.product"])
            self.assertNotIn("task_tracker", out["profile"], "пустой раздел не остаётся")
            self.assertEqual(out["profile"]["login"], "alice")

            code, out, _ = run(["set", "--home", home, "source_control.project="])
            self.assertEqual(code, 0)
            self.assertEqual(out["changed"], ["source_control.project"])
            self.assertEqual(out["profile"], {"login": "alice"})

    def test_unset_of_absent_key_is_not_a_change(self):
        with tempfile.TemporaryDirectory() as home:
            run(["set", "--home", home, "login=alice"])
            code, out, _ = run(["unset", "--home", home, "task_tracker.product"])
        self.assertEqual(code, 0)
        self.assertEqual(out["changed"], [])

    def test_reset_removes_file_and_show_says_missing(self):
        with tempfile.TemporaryDirectory() as home:
            run(["set", "--home", home, "login=alice"])
            code, out, _ = run(["reset", "--home", home])
            self.assertEqual(code, 0)
            self.assertTrue(out["removed"])
            self.assertFalse(profile_file(home).exists())
            code, out, _ = run(["reset", "--home", home])
            self.assertEqual(code, 0)
            self.assertFalse(out["removed"])
            _, shown, _ = run(["show", "--home", home])
        self.assertEqual(shown["status"], "missing")

    def test_managed_key_named_in_the_call_is_not_stored_but_asks_for_a_rewrite(self):
        # Штамп из вызова не записывается никогда -- но и отказом не оборачивается:
        # отказ был тупиком для файла, у которого испорчен ровно штамп (см.
        # BrokenStampTest). Значение вызова в файл не попадает, и заметка это говорит.
        with tempfile.TemporaryDirectory() as home:
            run(["set", "--home", home, "login=alice"])
            code, out, err = run(["set", "--home", home, "version=7"])
            on_disk = json.loads(profile_file(home).read_text(encoding="utf-8"))

        self.assertEqual(code, 0, err)
        self.assertEqual(out["version"], 1, "версию схемы ставит семейство, а не вызов")
        self.assertEqual(on_disk["version"], 1)
        self.assertEqual(out["changed"], [])
        self.assertEqual(out["restamped"], ["version"])
        self.assertTrue(any("выставляет само семейство" in note for note in out["notes"]))
        self.assertEqual(out["profile"], {"login": "alice"}, "остальной профиль на месте")

    def test_a_managed_key_never_reaches_the_file_even_next_to_a_real_one(self):
        with tempfile.TemporaryDirectory() as home:
            code, out, err = run(["set", "--home", home, "updated_at=вчера", "login=alice"])
            on_disk = json.loads(profile_file(home).read_text(encoding="utf-8"))

        self.assertEqual(code, 0, err)
        self.assertEqual(out["changed"], ["login"])
        self.assertEqual(out["restamped"], ["updated_at"])
        self.assertRegex(on_disk["updated_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_malformed_pair_is_an_error(self):
        with tempfile.TemporaryDirectory() as home:
            code, _, err = run(["set", "--home", home, "login"])
        self.assertEqual(code, 1)
        self.assertEqual(err["command"], "set")
        self.assertIn("КЛЮЧ=ЗНАЧЕНИЕ", err["error"])

    def test_a_key_of_only_dots_is_an_error_and_leaves_the_file_alone(self):
        # Единственная защита от ключей "." и ".." -- ветка malformed в
        # _apply_patch; без теста она исчезает при первом рефакторинге parse_pairs.
        with tempfile.TemporaryDirectory() as home:
            run(["set", "--home", home, "login=alice"])
            before = profile_file(home).read_bytes()
            for argv in (["set", "--home", home, ".=x"], ["unset", "--home", home, "."],
                         ["set", "--home", home, "..=x"]):
                code, out, err = run(argv)
                self.assertEqual(code, 1, argv)
                self.assertIsNone(out)
                self.assertIn("пустой ключ", err["error"])
            self.assertEqual(profile_file(home).read_bytes(), before)

    def test_reset_reports_a_failed_unlink_through_the_error_contract(self):
        with tempfile.TemporaryDirectory() as home:
            run(["set", "--home", home, "login=alice"])
            with mock.patch.object(Path, "unlink", side_effect=OSError("файл занят")):
                code, out, err = run(["reset", "--home", home])
            self.assertEqual(code, 1)
            self.assertIsNone(out)
            self.assertFalse(err["ok"])
            self.assertEqual(err["command"], "reset")
            self.assertIn("не удалось удалить", err["error"])
            self.assertEqual(err["path"], str(profile_file(home)))
            self.assertTrue(profile_file(home).exists(), "файл на месте")

    def test_unknown_keys_survive_rewrite_and_are_not_applied(self):
        with tempfile.TemporaryDirectory() as home:
            write_raw(home, json.dumps({
                "version": 1, "updated_at": "2026-08-19T10:15:00Z",
                "login": "alice", "favourite_colour": "green",
                "task_tracker": {"primary_space": "PVM", "shelf": {"deep": True}},
            }))
            code, out, _ = run(["set", "--home", home, "task_tracker.primary_space=CRPV"])
            self.assertEqual(code, 0)
            on_disk = json.loads(profile_file(home).read_text(encoding="utf-8"))
        self.assertEqual(on_disk["favourite_colour"], "green")
        self.assertEqual(on_disk["task_tracker"]["shelf"], {"deep": True})
        self.assertEqual(on_disk["task_tracker"]["primary_space"], "CRPV")
        self.assertEqual(out["changed"], ["task_tracker.primary_space"])


class NormalizeValidateTest(unittest.TestCase):
    def test_case_blanks_and_duplicates_are_normalized(self):
        data = {
            "login": "  familia.i.o ",
            "role": " Analyst",
            "task_tracker": {"primary_space": " pvm", "spaces": ["PVM", "pvm", " ", "CRPV", "crpv"],
                             "components": [" billing ", "billing", ""]},
            "sberworks": {"jira_project": "crpltfrm", "confluence_space": "pvmdoc"},
        }
        out = common.normalize_profile(data)
        self.assertEqual(out["login"], "familia.i.o")
        self.assertEqual(out["role"], "analyst")
        self.assertEqual(out["task_tracker"]["primary_space"], "PVM")
        self.assertEqual(out["task_tracker"]["spaces"], ["PVM", "CRPV"])
        self.assertEqual(out["task_tracker"]["components"], ["billing"])
        self.assertEqual(out["sberworks"], {"jira_project": "CRPLTFRM", "confluence_space": "PVMDOC"})
        self.assertEqual(data["task_tracker"]["primary_space"], " pvm", "вход не мутируется")

    def test_blank_values_are_dropped_with_their_empty_section(self):
        out = common.normalize_profile({"login": "   ", "task_tracker": {"spaces": [" ", ""]}})
        self.assertEqual(out, {})

    def test_comma_string_becomes_a_list(self):
        out = common.normalize_profile({"source_control": {"repos": "pvm-core, pvm-ui,pvm-core"}})
        self.assertEqual(out["source_control"]["repos"], ["pvm-core", "pvm-ui"])

    def test_valid_profile_has_no_errors(self):
        data = common.normalize_profile({
            "login": "familia.i.o", "role": "developer",
            "task_tracker": {"primary_space": "PVM", "spaces": ["PVM", "CRPV"], "default_suit": "task",
                             "product": "PVM-CORE", "components": ["billing"]},
            "source_control": {"tenant": "22f434fb-0355-42a1-ac25-011074a20a2d", "project": "pvm",
                               "repos": ["pvm-core"]},
            "sberworks": {"jira_project": "CRPLTFRM", "confluence_space": "PVMDOC"},
            "custom": {"anything": 1},
        })
        self.assertEqual(common.validate_profile(data), [])

    def test_invalid_codes_are_named_with_reason(self):
        errors = common.validate_profile(common.normalize_profile({
            "login": "фамилия", "role": "product owner",
            "task_tracker": {"primary_space": "PVM PROJECT", "spaces": ["PVM", "X"], "default_suit": "bug-report"},
            "source_control": {"tenant": "not-a-uuid", "project": "bad project"},
            "sberworks": {"jira_project": "A", "confluence_space": "PVM DOC"},
        }))
        by_key = {item["key"]: item["reason"] for item in errors}
        self.assertEqual(set(by_key), {
            "login", "role", "task_tracker.primary_space", "task_tracker.spaces", "task_tracker.default_suit",
            "source_control.tenant", "source_control.project", "sberworks.jira_project",
            "sberworks.confluence_space"})
        # Дословно то, что зафиксировано REQ-002: сверка с константой реализации
        # меняла бы текст и тест одновременно, пряча расхождение со спекой.
        self.assertEqual(by_key["task_tracker.primary_space"],
                         "код пространства — заглавные латинские буквы и цифры, от 2 до 20 символов")
        self.assertEqual(by_key["login"],
                         "логин — латиница, цифры, точки, дефис и подчёркивание, до 64 символов")
        for code in ("manager", "analyst", "architect", "developer", "tester", "writer", "other"):
            self.assertIn(code, by_key["role"], "отказ по роли перечисляет допустимые коды")

    def test_wrong_types_and_oversized_lists_are_errors(self):
        errors = common.validate_profile({
            "version": "1", "login": 5, "task_tracker": "PVM",
            "source_control": {"repos": ["r%d" % i for i in range(21)]},
            "sberworks": {"jira_project": ["CRPLTFRM"]},
        })
        by_key = {item["key"]: item["reason"] for item in errors}
        self.assertIn("version", by_key)
        self.assertIn("login", by_key)
        self.assertIn("task_tracker", by_key)
        self.assertIn("не больше 20", by_key["source_control.repos"])
        self.assertIn("sberworks.jira_project", by_key)

    def test_schema_covers_every_key_of_req_002(self):
        self.assertEqual(set(common.profile_schema()), {
            "login", "role", "task_tracker.primary_space", "task_tracker.spaces", "task_tracker.default_suit",
            "task_tracker.product", "task_tracker.components", "source_control.tenant",
            "source_control.project", "source_control.repos", "sberworks.jira_project",
            "sberworks.confluence_space"})

    def test_merge_sets_nested_and_removes_on_empty(self):
        current = {"login": "alice", "task_tracker": {"primary_space": "PVM", "product": "X"}}
        merged = gp.merge_profile(current, {"task_tracker.spaces": "PVM,CRPV", "task_tracker.product": None,
                                            "login": "", "sberworks.jira_project": "CRPLTFRM"})
        self.assertEqual(merged, {"task_tracker": {"primary_space": "PVM", "spaces": "PVM,CRPV"},
                                  "sberworks": {"jira_project": "CRPLTFRM"}})
        self.assertEqual(current["task_tracker"]["product"], "X", "вход не мутируется")


class RejectionTest(unittest.TestCase):
    def test_invalid_space_code_is_rejected_and_file_is_untouched(self):
        with tempfile.TemporaryDirectory() as home:
            run(["set", "--home", home, "task_tracker.primary_space=PVM"])
            before = profile_file(home).read_bytes()
            code, out, err = run(["set", "--home", home, "task_tracker.primary_space=PVM PROJECT"])
            after = profile_file(home).read_bytes()
        self.assertEqual(code, 1)
        self.assertIsNone(out)
        self.assertFalse(err["ok"])
        self.assertIn("код пространства — заглавные латинские буквы и цифры, от 2 до 20 символов",
                      err["error"])
        self.assertEqual(err["errors"][0]["key"], "task_tracker.primary_space")
        self.assertEqual(before, after)

    def test_invalid_role_lists_allowed_codes_and_keeps_file(self):
        with tempfile.TemporaryDirectory() as home:
            run(["set", "--home", home, "role=analyst"])
            before = profile_file(home).read_bytes()
            code, _, err = run(["set", "--home", home, "role=product owner"])
            after = profile_file(home).read_bytes()
        self.assertEqual(code, 1)
        for role in ("manager", "analyst", "architect", "developer", "tester", "writer", "other"):
            self.assertIn(role, err["error"])
        self.assertEqual(before, after)

    def test_invalid_value_already_in_file_is_dropped_on_the_next_write(self):
        # Значение, испорченное руками, не запирает профиль: правка чужого ключа
        # проходит, а негодный ключ удаляется с отчётом -- как секретоподобный.
        # Файл при этом читается как повреждённый, пока не переписан (REQ-001).
        with tempfile.TemporaryDirectory() as home:
            write_raw(home, json.dumps({"version": 1, "updated_at": "x", "role": "Product Owner"}))
            loaded = common.load_profile(Path(home))
            self.assertEqual(loaded.status, "corrupt")
            self.assertEqual(loaded.data, {}, "операции идут как без профиля")
            _, shown, _ = run(["show", "--home", home])
            self.assertEqual([item["key"] for item in shown["issues"]], ["role"])

            code, out, err = run(["set", "--home", home, "login=alice"])
            self.assertEqual(code, 0, err)
            self.assertEqual(out["profile"], {"login": "alice"})
            self.assertEqual([item["key"] for item in out["dropped_invalid"]], ["role"])
            self.assertTrue(any("недопустимыми значениями" in note for note in out["notes"]))
            self.assertEqual(common.load_profile(Path(home)).status, "ok")

    def test_a_rewrite_that_dropped_a_value_is_never_called_unchanged(self):
        # То же, что у секретоподобного ключа: перезапись, выбросившая негодное
        # значение, изменила файл, и «значения не изменились» про неё — неправда.
        with tempfile.TemporaryDirectory() as home:
            write_raw(home, json.dumps({"version": 1, "updated_at": "x", "login": "alice",
                                        "role": "Product Owner"}))
            code, out, err = run(["set", "--home", home, "login=alice"])

        self.assertEqual(code, 0, err)
        self.assertEqual(out["changed"], [])
        self.assertNotIn("значения не изменились", out["notes"])
        self.assertTrue(any("файл перезаписан" in note for note in out["notes"]))

    def test_a_rewrite_that_changed_nothing_at_all_still_says_so(self):
        with tempfile.TemporaryDirectory() as home:
            run(["set", "--home", home, "login=alice"])
            code, out, err = run(["set", "--home", home, "login=alice"])

        self.assertEqual(code, 0, err)
        self.assertEqual(out["notes"], ["значения не изменились"])

    def test_invalid_value_in_the_call_is_refused_whole_and_file_is_untouched(self):
        # А вот значение, переданное самим вызовом, отклоняется целиком: REQ-002
        # требует именно отказа с причиной и неизменного файла.
        with tempfile.TemporaryDirectory() as home:
            run(["set", "--home", home, "login=alice"])
            before = profile_file(home).read_bytes()
            code, _, err = run(["set", "--home", home, "role=product owner"])
            self.assertEqual(code, 1)
            self.assertEqual([item["key"] for item in err["errors"]], ["role"])
            self.assertEqual(profile_file(home).read_bytes(), before)

    def test_one_bad_key_rejects_the_whole_call(self):
        with tempfile.TemporaryDirectory() as home:
            code, _, err = run(["set", "--home", home, "login=alice", "task_tracker.spaces=PVM,bad space"])
            self.assertEqual(code, 1)
            self.assertFalse(profile_file(home).exists())
        self.assertEqual([item["key"] for item in err["errors"]], ["task_tracker.spaces"])

    def test_an_unknown_key_is_refused_on_write_and_the_file_stays_as_it_was(self):
        # Таблица ключей REQ-002 закрыта: опечатку в имени ни один CLI не читает,
        # поэтому «ok» на неё выдавал бы несохранённое умолчание за сохранённое.
        with tempfile.TemporaryDirectory() as home:
            run(["set", "--home", home, "login=alice"])
            before = profile_file(home).read_bytes()
            for key in ("tracker_login", "task_tracker.primary-space", "spaces", "task_tracker.shelf.deep"):
                code, out, err = run(["set", "--home", home, "%s=PVM" % key])
                self.assertEqual(code, 1, key)
                self.assertIsNone(out)
                self.assertEqual(err["rejected_keys"], [key])
                self.assertIn("task_tracker.primary_space", err["error"], "отказ перечисляет допустимые имена")
                self.assertEqual(err["known_keys"], list(common.profile_schema()))
            self.assertEqual(profile_file(home).read_bytes(), before)

    def test_one_unknown_key_rejects_the_whole_call(self):
        with tempfile.TemporaryDirectory() as home:
            code, _, err = run(["set", "--home", home, "login=alice", "tracker_login=alice"])
            self.assertEqual(code, 1)
            self.assertEqual(err["rejected_keys"], ["tracker_login"])
            self.assertFalse(profile_file(home).exists(), "ни один ключ вызова не записан")

    def test_a_secret_like_unknown_key_is_refused_as_a_secret_first(self):
        # jira_token — и секретоподобный, и неизвестный: пользователю важнее
        # первое, иначе отказ уводит его дописывать ключ в схему.
        with tempfile.TemporaryDirectory() as home:
            code, _, err = run(["set", "--home", home, "jira_token=x"])
        self.assertEqual(code, 1)
        self.assertIn("файле креда", err["error"])
        self.assertNotIn("known_keys", err)

    def test_an_unknown_key_already_in_the_file_can_still_be_removed(self):
        # Убрать руками добавленный мусор — работа unset; запретить это значило бы
        # оставить единственным способом правку файла руками или reset.
        with tempfile.TemporaryDirectory() as home:
            write_raw(home, json.dumps({"version": 1, "updated_at": "x", "login": "alice",
                                        "favourite_colour": "green"}))
            code, out, err = run(["unset", "--home", home, "favourite_colour"])
            self.assertEqual(code, 0, err)
            on_disk = json.loads(profile_file(home).read_text(encoding="utf-8"))
        self.assertEqual(out["changed"], ["favourite_colour"])
        self.assertNotIn("favourite_colour", on_disk)
        self.assertEqual(on_disk["login"], "alice")


class BrokenStampTest(unittest.TestCase):
    """Штамп (``version``/``updated_at``), испорченный руками, — не тупик.

    Такой файл читается как ``corrupt``, то есть профиль перестаёт применяться
    во всех операциях, а карта в ``show`` пуста: назвать ключ, перезапись
    которого вылечит файл, пользователю неоткуда. Поэтому совет ``show`` про
    штамп отдельный, а ``set``/``unset`` штампа не отказывают, а запускают ту
    самую перезапись — с сохранением всех годных значений.
    """

    def test_show_keeps_the_stamp_out_of_the_keys_to_retype_and_names_a_working_command(self):
        with tempfile.TemporaryDirectory() as home:
            write_raw(home, json.dumps({"version": "1", "updated_at": "2026-08-19T10:15:00Z", "login": "alice"}))
            code, out, _ = run(["show", "--home", home])

        self.assertEqual(code, 0)
        self.assertEqual(out["status"], "corrupt")
        self.assertNotIn("issues", out, "штамп человек не задаёт — в список «поправь эти ключи» он не идёт")
        self.assertEqual(out["broken_stamp"], ["version"])
        self.assertTrue(any("unset version" in note for note in out["notes"]))

    def test_show_still_names_the_keys_the_person_owns(self):
        with tempfile.TemporaryDirectory() as home:
            write_raw(home, json.dumps({"version": 1, "updated_at": 1234567890, "role": "Product Owner"}))
            code, out, _ = run(["show", "--home", home])

        self.assertEqual(code, 0)
        self.assertEqual([item["key"] for item in out["issues"]], ["role"])
        self.assertEqual(out["broken_stamp"], ["updated_at"])
        self.assertTrue(any("set/unset" in note for note in out["notes"]))

    def test_the_command_show_names_repairs_the_file_and_keeps_the_profile(self):
        with tempfile.TemporaryDirectory() as home:
            write_raw(home, json.dumps({"version": "1", "updated_at": "2026-08-19T10:15:00Z", "login": "alice",
                                        "task_tracker": {"primary_space": "PVM"}}))
            code, out, err = run(["unset", "--home", home, "version"])
            reread = common.load_profile(Path(home))

        self.assertEqual(code, 0, err)
        self.assertEqual(out["restamped"], ["version"])
        self.assertEqual(out["profile"], {"login": "alice", "task_tracker": {"primary_space": "PVM"}})
        self.assertEqual(reread.status, "ok", "файл снова читается")
        self.assertEqual(reread.data["login"], "alice")

    def test_a_broken_stamp_is_never_offered_for_retyping_on_a_write(self):
        with tempfile.TemporaryDirectory() as home:
            write_raw(home, json.dumps({"version": 1, "updated_at": 1234567890, "login": "alice"}))
            code, out, err = run(["set", "--home", home, "role=analyst"])

        self.assertEqual(code, 0, err)
        self.assertNotIn("dropped_invalid", out, "штамп не в списке того, что задают заново")
        self.assertEqual(out["restamped"], ["updated_at"])
        self.assertFalse(any("задай их заново" in note for note in out["notes"]))
        self.assertTrue(any("штамп профиля" in note for note in out["notes"]))


class SecretLikeKeysTest(unittest.TestCase):
    def test_secret_like_key_is_refused_on_write(self):
        with tempfile.TemporaryDirectory() as home:
            run(["set", "--home", home, "login=alice"])
            before = profile_file(home).read_bytes()
            for key in ("jira_token", "task_tracker.password", "sberworks.Auth", "my_cookie", "client_secret"):
                code, _, err = run(["set", "--home", home, "%s=value" % key])
                self.assertEqual(code, 1, key)
                self.assertIn(key, err["rejected_keys"])
                self.assertIn("файле креда", err["error"])
            self.assertEqual(profile_file(home).read_bytes(), before)

    def test_secret_like_keys_in_file_are_dropped_on_next_write_with_report(self):
        with tempfile.TemporaryDirectory() as home:
            write_raw(home, json.dumps({
                "version": 1, "updated_at": "2026-08-19T10:15:00Z", "login": "alice",
                "api_token": "x", "task_tracker": {"primary_space": "PVM", "SWTR_COOKIE": "y"},
                "custom": {"nested_password": "z", "keep": 1},
            }))
            code, out, _ = run(["set", "--home", home, "task_tracker.primary_space=CRPV"])
            self.assertEqual(code, 0)
            on_disk = json.loads(profile_file(home).read_text(encoding="utf-8"))
        self.assertEqual(sorted(out["removed_secret_like"]),
                         ["api_token", "custom.nested_password", "task_tracker.SWTR_COOKIE"])
        self.assertTrue(any("файле креда" in note for note in out["notes"]))
        self.assertNotIn("api_token", on_disk)
        self.assertNotIn("SWTR_COOKIE", on_disk["task_tracker"])
        self.assertEqual(on_disk["custom"], {"keep": 1})
        self.assertEqual(on_disk["task_tracker"]["primary_space"], "CRPV")

    def test_unset_of_a_secret_like_key_goes_through(self):
        with tempfile.TemporaryDirectory() as home:
            write_raw(home, json.dumps({"version": 1, "updated_at": "x", "login": "alice", "api_token": "v"}))
            code, out, _ = run(["unset", "--home", home, "api_token"])
            self.assertEqual(code, 0)
            on_disk = json.loads(profile_file(home).read_text(encoding="utf-8"))
        self.assertNotIn("api_token", on_disk)
        self.assertEqual(out["removed_secret_like"], ["api_token"])

    def test_a_rewrite_that_removed_something_is_never_called_unchanged(self):
        # ``changed`` считается по ключам патча, поэтому у такой перезаписи он пуст.
        # Сказать при этом «значения не изменились» рядом со строкой о том, что
        # именно удалено, — два взаимоисключающих утверждения в одном ответе:
        # человек решит, что удаление не сработало (REQ-003, REQ-004).
        with tempfile.TemporaryDirectory() as home:
            write_raw(home, json.dumps({"version": 1, "updated_at": "x", "login": "alice", "api_token": "v"}))
            code, out, _ = run(["unset", "--home", home, "api_token"])

        self.assertEqual(code, 0)
        self.assertEqual(out["changed"], [])
        self.assertNotIn("значения не изменились", out["notes"])
        self.assertTrue(any("файл перезаписан" in note for note in out["notes"]))

    def test_show_names_secret_like_keys_without_hiding_values(self):
        with tempfile.TemporaryDirectory() as home:
            write_raw(home, json.dumps({"version": 1, "updated_at": "x", "login": "alice",
                                        "api_token": "SEKRET-VALUE-DO-NOT-LEAK"}))
            code, out, _ = run(["show", "--home", home])
        self.assertEqual(code, 0)
        self.assertEqual(out["secret_like_keys"], ["api_token"])
        self.assertEqual(out["profile"]["login"], "alice", "значения показываются открыто, без маскирования")
        self.assertNotIn("api_token", out["profile"], "значение секретоподобного ключа в ответ не попадает")
        self.assertNotIn("SEKRET-VALUE-DO-NOT-LEAK", json.dumps(out["profile"]))

    def test_a_marker_inside_a_word_is_not_a_secret(self):
        # ``AUTH`` подстрокой ловил ``author`` и ``notes_for_author``: show звал их
        # секретами, а первый же set удалял их со строкой про файл креда — и потеря
        # неизвестных ключей, которые REQ-002 обещает сохранять при перезаписи, и
        # заведомо ложное объяснение. Исключения перечислены поимённо и покрывают
        # всё семейство слова: authors, authored, co_author, tokenize, tokenization.
        for key in ("author", "notes_for_author", "authored_by", "cookbook", "tokenizer",
                    "authors", "co_author", "notesForAuthor", "tokenization", "task_tracker.spaces"):
            self.assertFalse(gp.is_secret_like(key), key)
        for key in ("api_token", "apiToken", "SeCrEt", "sberworks.Auth", "authorization_scheme",
                    "task_tracker.password", "my_cookie", "SWTR_COOKIE"):
            self.assertTrue(gp.is_secret_like(key), key)

    def test_a_marker_inside_a_hand_typed_name_is_a_secret(self):
        # Обратная сторона того же вопроса: REQ-003, README, SKILL.md и сценарий 25
        # обещают «содержит TOKEN/PASSWORD/AUTH/COOKIE/SECRET». Совпадение только
        # целым словом пропускало ровно те имена, которые человек и придумывает
        # руками, — их значения печатались в show и оставались в файле.
        for key in ("tokens", "mytoken", "authtoken", "jira_tokens", "my_secrets",
                    "passwords", "cookies", "misc.myToken"):
            self.assertTrue(gp.is_secret_like(key), key)

    def test_a_hand_typed_secret_name_is_dropped_from_the_file_and_from_show(self):
        with tempfile.TemporaryDirectory() as home:
            write_raw(home, json.dumps({
                "version": 1, "updated_at": "2026-08-19T10:15:00Z", "role": "developer",
                "author": "Alice B", "tokens": "SEKRET-1", "my_secrets": {"jira": "SEKRET-2"},
                "authtoken": "SEKRET-3",
            }))
            _, shown, _ = run(["show", "--home", home])
            code, out, err = run(["set", "--home", home, "role=analyst"])
            on_disk = json.loads(profile_file(home).read_text(encoding="utf-8"))

        self.assertEqual(sorted(shown["secret_like_keys"]), ["authtoken", "my_secrets", "tokens"])
        self.assertNotIn("SEKRET", json.dumps(shown["profile"]), "значения секретоподобных ключей не показываются")
        self.assertEqual(shown["profile"]["author"], "Alice B", "обычный ключ с AUTH внутри слова остаётся")

        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(out["removed_secret_like"]), ["authtoken", "my_secrets", "tokens"])
        self.assertNotIn("SEKRET", json.dumps(on_disk), "и в файле их не остаётся")
        self.assertEqual(on_disk["author"], "Alice B")

    def test_an_ordinary_key_that_merely_contains_auth_survives_a_rewrite(self):
        with tempfile.TemporaryDirectory() as home:
            write_raw(home, json.dumps({
                "version": 1, "updated_at": "2026-08-19T10:15:00Z", "role": "developer",
                "author": "Alice B", "notes_for_author": "keep me", "api_token": "SEKRET",
            }))
            _, shown, _ = run(["show", "--home", home])
            self.assertEqual(shown["secret_like_keys"], ["api_token"])
            self.assertEqual(shown["profile"]["author"], "Alice B")

            code, out, err = run(["set", "--home", home, "role=analyst"])
            on_disk = json.loads(profile_file(home).read_text(encoding="utf-8"))

        self.assertEqual(code, 0, err)
        self.assertEqual(out["removed_secret_like"], ["api_token"])
        self.assertEqual(on_disk["author"], "Alice B")
        self.assertEqual(on_disk["notes_for_author"], "keep me")
        self.assertNotIn("api_token", on_disk)

    def test_a_key_that_merely_contains_auth_is_refused_as_unknown_not_as_a_secret(self):
        with tempfile.TemporaryDirectory() as home:
            code, out, err = run(["set", "--home", home, "author=Alice B"])
            exists = profile_file(home).exists()

        self.assertEqual(code, 1, out)
        self.assertIn("author", err["rejected_keys"], "ключ вне схемы отклоняется как неизвестный")
        self.assertNotIn("файле креда", err["error"], "и уж точно не как секрет")
        self.assertFalse(exists, "отклонённая запись файла не заводит")

    def test_strip_is_pure_and_reports_dotted_names(self):
        data = {"a": {"b": {"SeCrEt": 1, "ok": 2}}, "token": 3, "fine": 4}
        clean, removed = gp.strip_secret_like_keys(data)
        self.assertEqual(clean, {"a": {"b": {"ok": 2}}, "fine": 4})
        self.assertEqual(sorted(removed), ["a.b.SeCrEt", "token"])
        self.assertIn("token", data)

    def test_strip_walks_lists_and_names_the_element_by_index(self):
        data = {"misc": [{"api_token": 1, "keep": 2}, ["deep", {"SWTR_COOKIE": 3}]], "fine": 4}
        clean, removed = gp.strip_secret_like_keys(data)
        self.assertEqual(clean, {"misc": [{"keep": 2}, ["deep", {}]], "fine": 4})
        self.assertEqual(sorted(removed), ["misc[0].api_token", "misc[1][1].SWTR_COOKIE"])
        self.assertEqual(data["misc"][0]["api_token"], 1, "вход не мутируется")

    def test_a_secret_like_key_inside_a_list_is_dropped_and_never_printed(self):
        # Список — такое же место, куда его могли вписать руками, как и раздел:
        # значение обязано и не попасть в вывод, и исчезнуть при перезаписи (REQ-003).
        sentinel = "SEKRET-VALUE-DO-NOT-LEAK"
        with tempfile.TemporaryDirectory() as home:
            write_raw(home, json.dumps({"version": 1, "updated_at": "x", "login": "alice",
                                        "misc": [{"api_token": sentinel, "keep": "yes"}]}))
            _, shown, _ = run(["show", "--home", home])
            self.assertEqual(shown["secret_like_keys"], ["misc[0].api_token"])
            self.assertNotIn(sentinel, json.dumps(shown, ensure_ascii=False))

            code, out, err = run(["set", "--home", home, "login=bob"])
            self.assertEqual(code, 0, err)
            on_disk = profile_file(home).read_text(encoding="utf-8")
        self.assertEqual(out["removed_secret_like"], ["misc[0].api_token"])
        self.assertNotIn(sentinel, on_disk)
        self.assertNotIn(sentinel, json.dumps(out, ensure_ascii=False))
        self.assertEqual(json.loads(on_disk)["misc"], [{"keep": "yes"}], "остальное в списке остаётся")

    def test_profile_is_not_a_source_of_secret_values(self):
        # Сентинел лежит В ПРОФИЛЕ, а не в окружении: утверждение обязано падать,
        # если значение профиля когда-нибудь начнёт попадать в список секретов
        # или в вывод show. Проверка «удалили секретные переменные -- список
        # пуст» доказывала бы только собственный setUp.
        sentinel = "SEKRET-VALUE-DO-NOT-LEAK"
        with tempfile.TemporaryDirectory() as home:
            run(["set", "--home", home, "login=alice", "task_tracker.product=" + sentinel])
            code, out, _ = run(["show", "--home", home])
            self.assertEqual(code, 0)
            self.assertEqual(out["profile"]["task_tracker"]["product"], sentinel,
                             "значения профиля показываются открыто")
            self.assertNotIn(sentinel, common.collect_secret_values(),
                             "профиль не источник секретов: redact_secrets не должен его знать")
            self.assertEqual(common.redact_secrets(sentinel), sentinel,
                             "значение профиля не маскируется")


class NoNetworkTest(unittest.TestCase):
    """REQ-003: просмотр, сохранение и сброс профиля в сеть не ходят.

    Проверяется не намерение, а сам сетевой слой: и общий request_http, и
    urllib под ним. Правка, добавившая в show/set/unset/reset проверку значения
    запросом к трекеру, обязана уронить эти четыре кейса.
    """

    def _assert_offline(self, argv):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.object(common, "request_http") as http, \
                mock.patch("urllib.request.urlopen") as urlopen:
            run(["set", "--home", home, "login=alice"])
            http.reset_mock()
            urlopen.reset_mock()
            code = run([*argv, "--home", home])[0]
        self.assertIn(code, (0, 1), argv)
        self.assertEqual(http.call_count, 0, "%s открыл сокет через request_http" % argv[0])
        self.assertEqual(urlopen.call_count, 0, "%s открыл сокет через urllib" % argv[0])

    def test_show_opens_no_socket(self):
        self._assert_offline(["show"])

    def test_set_opens_no_socket(self):
        self._assert_offline(["set", "task_tracker.primary_space=PVM"])

    def test_unset_opens_no_socket(self):
        self._assert_offline(["unset", "login"])

    def test_reset_opens_no_socket(self):
        self._assert_offline(["reset"])


class ConcurrentWriteTest(unittest.TestCase):
    """REQ-001: две одновременные записи оставляют файл целиком одной из них."""

    def test_two_writers_never_mix_keys(self):
        variants = (
            {"login": "alice", "task_tracker": {"primary_space": "PVM", "spaces": ["PVM"]}},
            {"login": "bob", "task_tracker": {"primary_space": "CRPV", "spaces": ["CRPV", "OTHER"]}},
        )
        seen = []
        errors = []
        stop = threading.Event()

        with tempfile.TemporaryDirectory() as home:
            gp.save_profile(Path(home), variants[0])  # читателю всегда есть что прочесть

            def writer(data):
                for _ in range(80):
                    try:
                        gp.save_profile(Path(home), data)
                    except OSError as exc:  # noqa: PERF203 - записываем, а не глотаем
                        errors.append(exc)
                        return

            def reader():
                while not stop.is_set():
                    loaded = common.load_profile(Path(home))
                    seen.append((loaded.status, loaded.data.get("login"),
                                 (loaded.data.get("task_tracker") or {}).get("primary_space")))

            reader_thread = threading.Thread(target=reader)
            reader_thread.start()
            writers = [threading.Thread(target=writer, args=(data,)) for data in variants]
            for thread in writers:
                thread.start()
            for thread in writers:
                thread.join()
            stop.set()
            reader_thread.join()

        self.assertEqual(errors, [])
        self.assertTrue(seen, "читатель обязан был хоть раз увидеть файл")
        for status, login, space in seen:
            self.assertEqual(status, "ok", "частично записанного состояния не бывает")
            self.assertIn((login, space), [("alice", "PVM"), ("bob", "CRPV")],
                          "ключи из разных записей не смешиваются")


class AtomicWriteTest(unittest.TestCase):
    def test_no_temp_files_remain_after_save(self):
        with tempfile.TemporaryDirectory() as home:
            gp.save_profile(Path(home), {"login": "alice"})
            gp.save_profile(Path(home), {"login": "bob"})
            names = sorted(p.name for p in profile_file(home).parent.iterdir())
        self.assertEqual(names, ["profile.json"])

    def test_failed_replace_leaves_old_file_intact_and_no_droppings(self):
        with tempfile.TemporaryDirectory() as home:
            gp.save_profile(Path(home), {"login": "alice"})
            before = profile_file(home).read_bytes()
            original = gp.os.replace

            def broken_replace(src, dst):
                raise OSError("диск полон")

            gp.os.replace = broken_replace
            try:
                with self.assertRaises(OSError):
                    gp.save_profile(Path(home), {"login": "bob"})
            finally:
                gp.os.replace = original
            self.assertEqual(profile_file(home).read_bytes(), before)
            names = sorted(p.name for p in profile_file(home).parent.iterdir())
        self.assertEqual(names, ["profile.json"], "временный файл убран")

    def test_save_stamps_version_and_utc_time_and_overrides_caller_stamps(self):
        with tempfile.TemporaryDirectory() as home:
            path, written = gp.save_profile(Path(home), {"version": 99, "updated_at": "старое", "login": "alice"})
            on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["version"], 1)
        self.assertRegex(on_disk["updated_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(on_disk["login"], "alice")
        self.assertEqual(written, on_disk, "возвращается ровно то, что записано -- файл не перечитывается")

    def test_write_error_is_reported_through_error_contract(self):
        with tempfile.TemporaryDirectory() as home:
            original = gp.save_profile

            def failing(home_, data):
                raise OSError("нет прав")

            gp.save_profile = failing
            try:
                code, out, err = run(["set", "--home", home, "login=alice"])
            finally:
                gp.save_profile = original
        self.assertEqual(code, 1)
        self.assertIsNone(out)
        self.assertIn("нет прав", err["error"])
