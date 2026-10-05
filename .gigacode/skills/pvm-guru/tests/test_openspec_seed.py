"""Заготовка proposal из задачи трекера: ничего не выдумывать."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import openspec_seed as seed  # noqa: E402

FULL_SOURCE = {
    "inputType": "swtr_unit",
    "code": "PVM-160042",
    "summary": "Добавить срок действия счёта",
    "description": "Счета живут вечно, и просроченные засоряют выдачу. Пользователи "
                   "жалуются, что не могут отличить активный счёт от закрытого.",
    "unitUrl": "https://portal.works.prod.sbt/swtr/rest/api/unit/v2/PVM-160042",
    "fetchedAt": "2026-08-03T10:00:00+00:00",
    "attributes": [
        {"code": "a1", "name": "Состав работ", "type": "text",
         "text": "Добавить поле expiresAt в модель счёта и фильтрацию в выдаче."},
        {"code": "a2", "name": "Критерии приёмки", "type": "text",
         "text": "Просроченный счёт не попадает в выдачу по умолчанию."},
        {"code": "a3", "name": "Ограничения", "type": "text",
         "text": "Историю не мигрируем, старые счета остаются без срока."},
        {"code": "a4", "name": "Влияние на смежные системы", "type": "text",
         "text": "Затрагивается витрина отчётов и экспорт в хранилище."},
    ],
}


def run(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = seed.main(list(argv))
    return code, json.loads(buf.getvalue())


def without(*names):
    source = json.loads(json.dumps(FULL_SOURCE))
    source["attributes"] = [a for a in source["attributes"] if a["name"] not in names]
    return source


class ChangeIdTest(unittest.TestCase):
    def test_cyrillic_summary_transliterated(self):
        out = seed.seed_from_source(FULL_SOURCE)
        self.assertEqual(out["change_id"], "PVM-160042-dobavit-srok-deystviya-scheta")

    def test_task_code_stays_upper_case(self):
        source = dict(FULL_SOURCE, code="pvm-160042")
        out = seed.seed_from_source(source)
        self.assertTrue(out["change_id"].startswith("PVM-160042-"), out["change_id"])
        self.assertEqual(out["task"], "PVM-160042")

    def test_local_transliteration_matches_the_neighbour(self):
        """Запасной разбор не должен давать другой идентификатор."""
        original = seed.resolve_openspec_tools
        seed.resolve_openspec_tools = lambda: None
        try:
            out = seed.seed_from_source(FULL_SOURCE)
        finally:
            seed.resolve_openspec_tools = original
        self.assertEqual(out["change_id"], "PVM-160042-dobavit-srok-deystviya-scheta")
        self.assertTrue(any("запасной" in w or "не найден" in w for w in out["warnings"]))

    def test_missing_summary_warns(self):
        out = seed.seed_from_source(dict(FULL_SOURCE, summary=""))
        self.assertTrue(any("нет темы" in w for w in out["warnings"]))


class SectionsTest(unittest.TestCase):
    def test_full_source_fills_three_of_four(self):
        out = seed.seed_from_source(FULL_SOURCE)
        filled = {s["name"] for s in out["sections"] if s["text"]}
        self.assertEqual(filled, {"Why", "What Changes", "Impact"})

    def test_native_section_names(self):
        out = seed.seed_from_source(FULL_SOURCE)
        self.assertEqual(
            [s["name"] for s in out["sections"]],
            ["Why", "What Changes", "Capabilities", "Impact"],
        )

    def test_capabilities_never_invented(self):
        """Из задачи трекера capability не выводится — это всегда вопрос."""
        out = seed.seed_from_source(FULL_SOURCE)
        capabilities = next(s for s in out["sections"] if s["name"] == "Capabilities")
        self.assertEqual(capabilities["text"], "")
        self.assertEqual(capabilities["confidence"], seed.MARKER_GUESS)
        self.assertIn("Capabilities", out["missing"])

    def test_missing_section_is_red_and_becomes_a_question(self):
        out = seed.seed_from_source(without("Состав работ"))
        section = next(s for s in out["sections"] if s["name"] == "What Changes")
        self.assertEqual(section["text"], "")
        self.assertEqual(section["confidence"], seed.MARKER_GUESS)
        self.assertIn("What Changes", out["missing"])
        self.assertTrue(any(q.startswith("What Changes:") for q in out["questions"]))

    def test_filled_section_never_claims_certainty(self):
        """Пересказанный источник — не прочитанный код: 🟢 машина не ставит."""
        out = seed.seed_from_source(FULL_SOURCE)
        self.assertNotIn(seed.MARKER_SURE, json.dumps(out, ensure_ascii=False))

    def test_short_text_is_flagged_unsure(self):
        source = json.loads(json.dumps(FULL_SOURCE))
        source["description"] = "Надо доделать."
        out = seed.seed_from_source(source)
        why = next(s for s in out["sections"] if s["name"] == "Why")
        self.assertEqual(why["confidence"], seed.MARKER_UNSURE)
        self.assertTrue(any(q.startswith("Why:") for q in out["questions"]))

    def test_source_field_is_named(self):
        out = seed.seed_from_source(FULL_SOURCE)
        changes = next(s for s in out["sections"] if s["name"] == "What Changes")
        self.assertEqual(changes["from"], "Состав работ")

    def test_attribute_matched_by_name_not_code(self):
        source = json.loads(json.dumps(FULL_SOURCE))
        for attribute in source["attributes"]:
            attribute["code"] = "unknown"
        out = seed.seed_from_source(source)
        self.assertTrue(next(s for s in out["sections"] if s["name"] == "Impact")["text"])

    def test_empty_attribute_does_not_count_as_filled(self):
        source = json.loads(json.dumps(FULL_SOURCE))
        for attribute in source["attributes"]:
            if attribute["name"] == "Состав работ":
                attribute["text"] = "   "
        out = seed.seed_from_source(source)
        self.assertIn("What Changes", out["missing"])


class AcceptanceCriteriaTest(unittest.TestCase):
    def test_criteria_are_carried_separately(self):
        out = seed.seed_from_source(FULL_SOURCE)
        self.assertIsNotNone(out["acceptance_criteria"])
        self.assertIn("Просроченный счёт", out["acceptance_criteria"]["text"])
        self.assertIn("дельта-спеки", out["acceptance_criteria"]["note"])

    def test_criteria_are_not_a_proposal_section(self):
        """Нативного раздела под критерии в proposal нет — выдумывать его нельзя."""
        out = seed.seed_from_source(FULL_SOURCE)
        self.assertNotIn("Критерии", out["markdown"])
        self.assertNotIn("Acceptance", out["markdown"])

    def test_missing_criteria_become_a_question(self):
        out = seed.seed_from_source(without("Критерии приёмки"))
        self.assertIsNone(out["acceptance_criteria"])
        self.assertTrue(any("Критерии приёмки" in q for q in out["questions"]))

    def test_constraints_carried_separately(self):
        out = seed.seed_from_source(FULL_SOURCE)
        self.assertIn("Историю не мигрируем", out["constraints"]["text"])


class MarkdownTest(unittest.TestCase):
    def test_header_carries_status_and_task(self):
        out = seed.seed_from_source(FULL_SOURCE)
        self.assertIn("> **Статус**: На согласовании", out["markdown"])
        self.assertIn("> **Задача трекера**: PVM-160042", out["markdown"])

    def test_all_four_native_sections_present(self):
        out = seed.seed_from_source(FULL_SOURCE)
        for name in ("## Why", "## What Changes", "## Capabilities", "## Impact"):
            self.assertIn(name, out["markdown"])

    def test_marker_sits_in_the_heading(self):
        out = seed.seed_from_source(FULL_SOURCE)
        self.assertIn(f"## Capabilities {seed.MARKER_GUESS}", out["markdown"])

    def test_empty_section_holds_the_question_not_prose(self):
        out = seed.seed_from_source(without("Состав работ"))
        block = out["markdown"].split("## What Changes")[1].split("## Capabilities")[0]
        self.assertIn("в источнике нет данных", block)
        self.assertNotIn("Добавить поле", block)

    def test_change_id_recorded_in_the_draft(self):
        out = seed.seed_from_source(FULL_SOURCE)
        self.assertIn("change-id: PVM-160042-dobavit-srok-deystviya-scheta", out["markdown"])


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)

    def _source_file(self, source=FULL_SOURCE, wrapped=True):
        path = self.dir / "source.json"
        payload = {"ok": True, "request": {}, "source": source} if wrapped else source
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def test_source_file_needs_no_network(self):
        code, out = run("--source-file", self._source_file())
        self.assertEqual(code, 0, out.get("errors"))
        self.assertEqual(out["command"], "openspec-seed")
        self.assertEqual(out["change_id"], "PVM-160042-dobavit-srok-deystviya-scheta")

    def test_bare_source_accepted(self):
        code, out = run("--source-file", self._source_file(wrapped=False))
        self.assertEqual(code, 0)
        self.assertTrue(out["markdown"])

    def test_no_input_is_an_error(self):
        code, out = run()
        self.assertEqual(code, 1)
        self.assertTrue(out["errors"])

    def test_writes_nothing(self):
        path = self._source_file()
        before = sorted(str(p.relative_to(self.dir)) for p in self.dir.rglob("*"))
        run("--source-file", path)
        after = sorted(str(p.relative_to(self.dir)) for p in self.dir.rglob("*"))
        self.assertEqual(before, after, "скрипт создал файл — черновик пишет агент")

    def test_script_never_writes(self):
        src = (SCRIPTS / "openspec_seed.py").read_text(encoding="utf-8")
        for forbidden in ("write_text(", "mkdir(", "unlink("):
            self.assertNotIn(forbidden, src, f"скрипт меняет файловую систему: {forbidden}")


if __name__ == "__main__":
    unittest.main()
