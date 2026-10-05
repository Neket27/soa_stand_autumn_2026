"""Профиль пользователя: чтение и порядок источников (_swtr_common).

Читатель обязан не падать никогда: битый файл, чужая версия схемы и отсутствие
файла -- три разных статуса, и все они для операций значат «как без профиля».

И резолв умолчаний обязан держать порядок явное -> окружение -> профиль и
называть источник, иначе поле defaults_applied врёт.

Запись профиля -- в tests/test_profile_store.py, подсказки онбординга -- в
tests/test_profile_suggest.py.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

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


class LoadProfileTest(unittest.TestCase):
    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as home:
            loaded = common.load_profile(Path(home))
        self.assertEqual(loaded.status, "missing")
        self.assertEqual(loaded.data, {})
        self.assertEqual(loaded.path, str(profile_file(home)))
        self.assertIsNone(common.profile_get(loaded, "login"))

    def test_corrupt_json_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as home:
            write_raw(home, "{ это не json")
            loaded = common.load_profile(Path(home))
            code, out, _ = run(["show", "--home", home])
            code_set, _, err = run(["set", "--home", home, "login=alice"])
            still = profile_file(home).read_text(encoding="utf-8")
        self.assertEqual(loaded.status, "corrupt")
        self.assertIn("JSON", loaded.reason)
        self.assertEqual(loaded.data, {})
        self.assertIsNone(common.profile_get(loaded, "login"))
        self.assertEqual(code, 0)
        self.assertEqual(out["status"], "corrupt")
        self.assertIn("reason", out)
        self.assertEqual(out["path"], str(profile_file(home)))
        self.assertEqual(code_set, 1, "set не перезаписывает битый файл молча")
        self.assertIn("reset", err["error"])
        self.assertEqual(still, "{ это не json")

    def test_wrong_shapes_are_corrupt_with_reason(self):
        cases = {
            "[1, 2]": "JSON object",
            '{"version": "1"}': "version",
            '{"version": 1, "task_tracker": "PVM"}': "task_tracker",
        }
        for text, marker in cases.items():
            with tempfile.TemporaryDirectory() as home:
                write_raw(home, text)
                loaded = common.load_profile(Path(home))
            self.assertEqual(loaded.status, "corrupt", text)
            self.assertIn(marker, loaded.reason)

    def test_newer_schema_is_unsupported_and_only_reset_may_overwrite(self):
        with tempfile.TemporaryDirectory() as home:
            write_raw(home, json.dumps({"version": 2, "updated_at": "x", "login": "alice", "future": True}))
            loaded = common.load_profile(Path(home))
            self.assertEqual(loaded.status, "unsupported")
            self.assertEqual(loaded.version, 2)
            self.assertIsNone(common.profile_get(loaded, "login"), "операции ведут себя как без профиля")

            code, out, _ = run(["show", "--home", home])
            self.assertEqual(code, 0)
            self.assertEqual(out["status"], "unsupported")
            self.assertEqual(out["profile"], {})
            self.assertTrue(any("reset" in note for note in out["notes"]))

            before = profile_file(home).read_bytes()
            code, _, err = run(["set", "--home", home, "login=bob"])
            self.assertEqual(code, 1)
            self.assertEqual(err["status"], "unsupported")
            self.assertEqual(profile_file(home).read_bytes(), before)
            code, _, err = run(["unset", "--home", home, "login"])
            self.assertEqual(code, 1)
            self.assertEqual(profile_file(home).read_bytes(), before)

            code, out, _ = run(["reset", "--home", home])
            self.assertEqual(code, 0)
            self.assertTrue(out["removed"])
            self.assertFalse(profile_file(home).exists())

    def test_missing_version_is_tolerated_as_current(self):
        with tempfile.TemporaryDirectory() as home:
            write_raw(home, json.dumps({"login": "alice"}))
            loaded = common.load_profile(Path(home))
        self.assertEqual(loaded.status, "ok")
        self.assertEqual(common.profile_get(loaded, "login"), "alice")

    def test_bom_and_utf16_files_are_read(self):
        body = json.dumps({"version": 1, "login": "alice"})
        with tempfile.TemporaryDirectory() as home:
            write_raw(home, b"\xef\xbb\xbf" + body.encode("utf-8"))
            self.assertEqual(common.load_profile(Path(home)).status, "ok")
        with tempfile.TemporaryDirectory() as home:
            write_raw(home, body.encode("utf-16"))
            loaded = common.load_profile(Path(home))
        self.assertEqual(loaded.status, "ok")
        self.assertEqual(common.profile_get(loaded, "login"), "alice")

    def test_profile_path_honours_home(self):
        self.assertEqual(common.profile_path(Path("/tmp/h")), Path("/tmp/h/.config/pvm-guru/profile.json"))
        self.assertEqual(common.profile_path(), Path.home() / ".config" / "pvm-guru" / "profile.json")
        self.assertEqual(common.PROFILE_REL, (".config", "pvm-guru", "profile.json"))


class ResolveDefaultTest(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)
        os.environ.pop("GP_TEST_SPACE", None)
        os.environ.pop("GP_TEST_SPACE_ALT", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def _loaded(self, home):
        run(["set", "--home", home, "task_tracker.primary_space=PVM", "task_tracker.spaces=PVM,CRPV",
             "login=alice"])
        return common.load_profile(Path(home))

    def test_profile_get_handles_handle_and_none(self):
        with tempfile.TemporaryDirectory() as home:
            loaded = self._loaded(home)
            self.assertEqual(common.profile_get(loaded, "task_tracker.spaces"), ["PVM", "CRPV"])
            self.assertEqual(common.profile_get(loaded, "login"), "alice")
            self.assertIsNone(common.profile_get(loaded, "task_tracker.product"))
            self.assertIsNone(common.profile_get(None, "login"), "--no-profile")
            self.assertIsNone(common.profile_get(loaded, "login.deeper"))
            blank = common.load_profile(Path(home) / "nowhere")
            self.assertIsNone(common.profile_get(blank, "login"), "любой не-ok статус -- как без профиля")

    def test_order_is_explicit_then_env_then_profile(self):
        with tempfile.TemporaryDirectory() as home:
            loaded = self._loaded(home)
            key = "task_tracker.primary_space"
            names = ("GP_TEST_SPACE", "GP_TEST_SPACE_ALT")
            self.assertEqual(common.resolve_default(None, names, loaded, key), ("PVM", "profile"))
            self.assertEqual(common.resolve_default("", names, loaded, key), ("PVM", "profile"))
            self.assertEqual(common.resolve_default("   ", names, loaded, key), ("PVM", "profile"))
            os.environ["GP_TEST_SPACE_ALT"] = "ENV2"
            self.assertEqual(common.resolve_default(None, names, loaded, key), ("ENV2", "env"))
            os.environ["GP_TEST_SPACE"] = "ENV1"
            self.assertEqual(common.resolve_default(None, names, loaded, key), ("ENV1", "env"))
            self.assertEqual(common.resolve_default("X", names, loaded, key), ("X", "explicit"))

    def test_nothing_found_returns_none_pair(self):
        with tempfile.TemporaryDirectory() as home:
            loaded = self._loaded(home)
            self.assertEqual(common.resolve_default(None, (), loaded, "task_tracker.product"), (None, None))
            self.assertEqual(common.resolve_default(None, (), None, "login"), (None, None))
            missing = common.load_profile(Path(home) / "nowhere")
            self.assertEqual(common.resolve_default(None, (), missing, "login"), (None, None))

    def test_defaults_applied_payload(self):
        applied = common.DefaultsApplied()
        self.assertFalse(applied)
        self.assertEqual(applied.as_payload(), [])
        applied.add("task_tracker.spaces", ["PVM", "CRPV"], "profile")
        applied.add("login", "alice", "env")
        self.assertTrue(applied)
        self.assertEqual(applied.as_payload(), [
            {"key": "task_tracker.spaces", "value": ["PVM", "CRPV"], "source": "profile"},
            {"key": "login", "value": "alice", "source": "env"},
        ])
        json.dumps(applied.as_payload())

    def test_with_defaults_prints_nothing_when_empty(self):
        empty = common.DefaultsApplied()
        self.assertEqual(common.with_defaults({"command": "x"}, empty), {"command": "x"})
        self.assertEqual(common.with_defaults({"command": "x"}, None), {"command": "x"})
        empty.add("space", "PVM", "profile")
        self.assertEqual(common.with_defaults({"command": "x"}, empty)["defaults_applied"],
                         [{"key": "space", "value": "PVM", "source": "profile"}])

    def test_profile_error_context_adds_the_hint_only_for_profile_values(self):
        applied = common.DefaultsApplied()
        self.assertEqual(common.profile_error_context(applied, "подсказка"), {})
        applied.add("login", "alice", "env")
        self.assertEqual(common.profile_error_context(applied, "подсказка"),
                         {"defaults_applied": applied.as_payload()})
        applied.add("space", "PVM", "profile")
        self.assertEqual(common.profile_error_context(applied, "подсказка")["hint"], "подсказка")
