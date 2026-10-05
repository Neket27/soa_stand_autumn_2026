"""Доктор: блок состояния профиля пользователя.

Профиль -- локальный файл умолчаний. Повреждённый или слишком новый файл
инструменты молча пропускают, и пользователь видит только «почему не
подставилось»; блок диагностики -- единственное место, где это названо вслух.
Отсюда три требования к блоку.

Четыре состояния файла различимы, и у каждого назван следующий шаг: нет файла --
пройти /guru-start; повреждён или версия новее -- причина и путь.

Вывод чисто ASCII. Значения профиля пользователь вводит сам, а доктор обязан
пережить Windows-пайп (см. WindowsOutputTest): печатаются только признаки
заполненности, момент сохранения и код пространства; логин не повторяется.

И блок не имеет права влиять на вердикты доступа, строку FIXABLE и код
возврата: профиль и доступ -- независимые диагнозы.
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

import guru_doctor as d  # noqa: E402

PROFILE_REL = (".config", "pvm-guru", "profile.json")
SAVED_AT = "2026-08-19T10:00:00Z"


def render(home):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        d.profile_state_block(Path(home))
    return buffer.getvalue()


def write_profile(home, text):
    path = Path(home).joinpath(*PROFILE_REL)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_profile_json(home, **fields):
    return write_profile(home, json.dumps(fields, ensure_ascii=False))


class ProfileStateBlock(unittest.TestCase):
    def test_missing_names_the_onboarding_command(self):
        with tempfile.TemporaryDirectory() as home:
            out = render(home)
            expected_path = str(Path(home).joinpath(*PROFILE_REL))
        self.assertIn("profile state", out)
        self.assertIn("missing", out)
        self.assertIn("/guru-start", out, "без файла единственный следующий шаг -- онбординг")
        self.assertIn("/guru_profile", out, "карту покажет команда профиля")
        self.assertIn(expected_path, out)
        self.assertNotIn("!!", out, "отсутствие профиля -- не поломка: инструменты работают как раньше")

    def test_present_shows_save_time_and_filled_in_flags(self):
        with tempfile.TemporaryDirectory() as home:
            write_profile_json(
                home,
                version=1,
                updated_at=SAVED_AT,
                login="alice",
                task_tracker={"primary_space": "PVM", "spaces": ["PVM", "CRPV"]},
            )
            out = render(home)
            expected_path = str(Path(home).joinpath(*PROFILE_REL))
        self.assertIn("present", out)
        self.assertIn(SAVED_AT, out)
        self.assertIn("login         : yes", out)
        self.assertIn("primary space : yes (PVM)", out)
        self.assertIn(expected_path, out)
        self.assertNotIn("alice", out, "доктор не печатает личности -- только признак заполненности")
        self.assertNotIn("!!", out)

    def test_present_without_login_and_space_says_no(self):
        with tempfile.TemporaryDirectory() as home:
            write_profile_json(home, version=1, updated_at=SAVED_AT, role="developer")
            out = render(home)
        self.assertIn("present", out)
        self.assertIn("login         : no", out)
        self.assertIn("primary space : no", out)
        self.assertIn("/guru_profile", out, "пустые поля должны вести к команде, которая их заполнит")

    def test_blank_values_count_as_not_filled_in(self):
        # Явно опустошённый ключ -- то же, что отсутствующий: так его читает
        # и profile_get, а доктор не должен расходиться с инструментами.
        with tempfile.TemporaryDirectory() as home:
            write_profile_json(home, version=1, updated_at=SAVED_AT, login="  ",
                               task_tracker={"primary_space": ""})
            out = render(home)
        self.assertIn("login         : no", out)
        self.assertIn("primary space : no", out)

    def test_hand_made_file_without_stamp_is_still_present(self):
        with tempfile.TemporaryDirectory() as home:
            write_profile_json(home, login="alice")
            out = render(home)
        self.assertIn("present", out)
        self.assertIn("saved at      : unknown", out)
        self.assertIn("login         : yes", out)

    def test_a_stamp_this_report_cannot_print_is_not_a_missing_stamp(self):
        # Схема проверяет у штампа только тип, поэтому «вчера» оставляет файл
        # читаемым (status ok). Сказать про него «штампа нет» -- сообщить как
        # факт то, чего нет: строка обязана разводить «значения нет» и
        # «значение непечатаемо», как это уже делает строка про пространство.
        with tempfile.TemporaryDirectory() as home:
            write_profile_json(home, version=1, updated_at="вчера", login="alice",
                               task_tracker={"primary_space": "PVM"})
            out = render(home)
        self.assertIn("file          : present", out)
        self.assertIn("saved at      : present (non-ascii, not shown)", out)
        self.assertNotIn("no updated_at stamp", out)
        self.assertTrue(out.isascii(), out)

    def test_a_non_ascii_home_path_does_not_break_the_ascii_block(self):
        # Путь -- такое же значение: он несёт имя домашнего каталога, а его
        # никто не обещал держать в латинице.
        with tempfile.TemporaryDirectory() as parent:
            home = Path(parent) / "домой"
            home.mkdir()
            write_profile_json(str(home), version=1, updated_at=SAVED_AT, login="alice")
            out = render(str(home))
        self.assertIn("file          : present", out)
        self.assertTrue(out.isascii(), out)
        self.assertNotIn("домой", out)

    def test_corrupt_names_reason_and_path(self):
        with tempfile.TemporaryDirectory() as home:
            write_profile(home, "{ this is not json")
            out = render(home)
            expected_path = str(Path(home).joinpath(*PROFILE_REL))
        self.assertIn("corrupt", out)
        self.assertIn("not valid JSON", out, "причина обязательна: без неё «повреждён» не починить")
        self.assertIn(expected_path, out)
        self.assertIn("!!", out, "игнорируемый профиль -- громкая строка, как застрявшее обновление")
        self.assertIn("fix", out, "признак без способа починить бесполезен")
        self.assertIn("/guru-start", out)

    def test_not_an_object_is_corrupt_too(self):
        with tempfile.TemporaryDirectory() as home:
            write_profile(home, "[1, 2, 3]")
            out = render(home)
        self.assertIn("corrupt", out)
        self.assertIn("not a JSON object", out)

    def test_unsupported_version_names_reason_and_path(self):
        with tempfile.TemporaryDirectory() as home:
            write_profile_json(home, version=2, updated_at=SAVED_AT, login="alice")
            out = render(home)
            expected_path = str(Path(home).joinpath(*PROFILE_REL))
        self.assertIn("unsupported", out)
        self.assertIn("schema version 2", out)
        self.assertIn(expected_path, out)
        self.assertIn("!!", out)
        self.assertIn("npx skills update", out, "файл новее семейства -- лечится обновлением, а не правкой")
        self.assertIn("/guru_profile", out, "или сбросом через команду профиля")
        self.assertNotIn("present", out)
        self.assertNotIn("alice", out)

    def test_output_is_ascii_in_every_state(self):
        # Значения профиля пользователь вводит сам, в том числе кириллицей и
        # руками в блокноте. Под Windows stdout в пайпе кодируется ANSI-страницей,
        # и один символ вне её роняет печать -- блок обязан остаться ASCII.
        cases = {
            "missing": None,
            "present-cyrillic": json.dumps(
                {"version": 1, "updated_at": "сегодня", "login": "иван",
                 "task_tracker": {"primary_space": "ПВМ"}}, ensure_ascii=False),
            "present-ascii": json.dumps(
                {"version": 1, "updated_at": SAVED_AT, "login": "alice",
                 "task_tracker": {"primary_space": "PVM"}}),
            "corrupt": "{ это не json",
            "unsupported": json.dumps({"version": 7, "login": "иван"}, ensure_ascii=False),
        }
        for label, text in cases.items():
            with self.subTest(state=label), tempfile.TemporaryDirectory() as home:
                if text is not None:
                    write_profile(home, text)
                out = render(home)
                self.assertTrue(out.isascii(), "%s: non-ASCII in %r" % (label, out))
                self.assertNotIn("иван", out)
                self.assertNotIn("ПВМ", out)

    def test_cyrillic_values_make_the_file_corrupt_and_are_not_echoed(self):
        # Схема держит логин и код пространства в латинице, и проверяет её сам
        # читатель: кириллица в этих ключах -- повреждённый файл, а не значение,
        # которое доктор мог бы напечатать.
        with tempfile.TemporaryDirectory() as home:
            write_profile_json(home, version=1, updated_at=SAVED_AT, login="иван",
                               task_tracker={"primary_space": "ПВМ"})
            out = render(home)
        self.assertIn("!! file       : corrupt", out)
        self.assertIn("login", out, "причина называет ключи, но не значения")
        self.assertNotIn("иван", out)
        self.assertNotIn("ПВМ", out)

    def test_a_multiline_value_cannot_forge_a_line_of_the_block(self):
        # Блок читают построчно, поэтому значение с переводом строки обязано
        # схлопнуться в одну: иначе оно печатает собственные строки вида
        # "login : root" и вводит читателя в заблуждение.
        with tempfile.TemporaryDirectory() as home:
            write_profile_json(home, version=1, updated_at="2026-08-19\n  login         : root",
                               task_tracker={"primary_space": "PVM"})
            out = render(home)
        lines = [line for line in out.splitlines() if line.startswith("  login")]
        self.assertEqual(lines, ["  login         : no"], out)
        self.assertIn("  saved at      : 2026-08-19 login : root", out)

    def test_a_very_long_value_is_cut(self):
        with tempfile.TemporaryDirectory() as home:
            write_profile_json(home, version=1, updated_at="x" * 200, task_tracker={"primary_space": "PVM"})
            out = render(home)
        saved = [line for line in out.splitlines() if line.startswith("  saved at")][0]
        self.assertTrue(saved.endswith("..."), saved)
        self.assertLess(len(saved), 90, saved)

    def test_block_is_printed_after_the_summary(self):
        # Порядок в main(): вердикты -> SUMMARY -> FIXABLE -> профиль. Блок
        # регулярного обновления отсюда убран вместе с самой подсистемой:
        # обновление идёт штатной командой менеджера скиллов, и своего состояния
        # у него на машине нет.
        with tempfile.TemporaryDirectory() as home:
            out = _run_main(home)
        self.assertLess(out.index("SUMMARY"), out.index("profile state"))
        self.assertNotIn("skills update", out)

    def test_verdicts_fixable_and_exit_code_do_not_depend_on_profile(self):
        # Тот же прогон с --no-network при четырёх профилях: одинаковые SUMMARY,
        # FIXABLE и код возврата; различается только блок профиля.
        states = {
            "file          : missing": None,
            "file          : present": json.dumps({"version": 1, "updated_at": SAVED_AT, "login": "alice",
                                                   "task_tracker": {"primary_space": "PVM"}}),
            "!! file       : corrupt": "{ broken",
            "!! file       : unsupported": json.dumps({"version": 2}),
        }
        seen = {}
        for marker, text in states.items():
            with tempfile.TemporaryDirectory() as home:
                if text is not None:
                    write_profile(home, text)
                out, rc = _run_main_with_code(home)
            summary = [line for line in out.splitlines() if line.startswith("SUMMARY")]
            fixable = [line for line in out.splitlines() if line.startswith("FIXABLE")]
            self.assertEqual(len(summary), 1, out)
            self.assertIn(marker, out)
            seen[marker] = (rc, summary[0], tuple(fixable))
        verdict_views = set(seen.values())
        self.assertEqual(len(verdict_views), 1,
                         "профиль не смеет менять вердикты, FIXABLE или код возврата: %r" % (seen,))
        rc, summary, _fixable = verdict_views.pop()
        self.assertEqual(rc, 0, "с кредами и --no-network все четыре системы skipped -> 0")
        for system in d.ALL_SYSTEMS:
            self.assertIn("%s [OK]" % system, summary)


_FAKE_CREDENTIALS = {
    "SWTR_BEARER_TOKEN": "PROFILEBLOCKSWTRTOKEN",
    "SOURCE_CONTROL_BEARER_TOKEN": "PROFILEBLOCKSCTOKEN",
    "CONFLUENCE_BEARER_TOKEN": "PROFILEBLOCKCONFTOKEN",
    "JIRA_BEARER_TOKEN": "PROFILEBLOCKJIRATOKEN",
}


def _run_main_with_code(home):
    """main() в герметичном окружении: временный HOME, фальшивые креды, без сети.

    Env-файлы указывают внутрь того же временного каталога, поэтому ни реальный
    ~/.swtr-review.env, ни ~/.sberworks.env не читаются; обновление и профиль
    тоже ищутся под временным HOME.
    """
    env = dict(_FAKE_CREDENTIALS)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    argv = ["guru_doctor.py", "--no-network", "--env-file", str(Path(home) / ".swtr-review.env")]
    buffer = io.StringIO()
    with mock.patch.dict(os.environ, env), mock.patch.object(sys, "argv", argv), \
            contextlib.redirect_stdout(buffer):
        rc = d.main()
    return buffer.getvalue(), rc


def _run_main(home):
    return _run_main_with_code(home)[0]


if __name__ == "__main__":
    unittest.main()
