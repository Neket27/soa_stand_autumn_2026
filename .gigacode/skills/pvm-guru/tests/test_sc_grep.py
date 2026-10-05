"""Поиск по SourceControl как замена локального grep: координаты и честные границы."""

import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _swtr_common as common  # noqa: E402
import source_control_search as sc  # noqa: E402

FILE_LINES = [f"строка {i}" for i in range(1, 11)]


def snippet_for(line: int, context: int = 2) -> str:
    start = max(0, line - 1 - context)
    end = min(len(FILE_LINES), line + context)
    return "\n".join(FILE_LINES[start:end])


def payload(matches, **overrides):
    base = {
        "command": "grep",
        "tenant": "t", "project": "p", "repo": "billing", "ref": "master",
        "pathPrefix": "", "queries": ["expiresAt"],
        "meta": {"errors": []},
        "matches": matches,
        "matchCount": len(matches),
    }
    base.update(overrides)
    return base


def args(**overrides):
    # Те же поля, что кладёт main(): профиль и накопитель умолчаний ставятся
    # одной точкой до вызова cmd_*, поэтому команда читает их напрямую.
    values = {
        "command": "grep", "repo": "billing", "context": 2, "evidence": False,
        "max_files": 5000, "profile": None, "defaults_applied": common.DefaultsApplied(),
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def run_grep(stub_payload, **arg_overrides):
    original = sc.search_payload
    sc.search_payload = lambda _a: stub_payload
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = sc.cmd_grep(args(**arg_overrides))
    finally:
        sc.search_payload = original
    return code, json.loads(buf.getvalue())


class NormalizeTest(unittest.TestCase):
    def test_finding_shape(self):
        found, warnings = sc.normalize_matches(
            payload([{"path": "src/Billing.java", "line": 5, "score": 4, "snippet": snippet_for(5)}]),
            context=2, evidence_only=False,
        )
        self.assertEqual(warnings, [])
        self.assertEqual(
            set(found[0]),
            {"repo", "ref", "path", "line", "text", "context", "evidence"},
        )
        self.assertEqual(found[0]["repo"], "billing")
        self.assertEqual(found[0]["ref"], "master")

    def test_evidence_is_path_and_line(self):
        found, _ = sc.normalize_matches(
            payload([{"path": "src/Billing.java", "line": 5, "snippet": snippet_for(5)}]),
            context=2, evidence_only=False,
        )
        self.assertEqual(found[0]["evidence"], "src/Billing.java:5")

    def test_text_is_the_matching_line_not_the_block(self):
        for line in (1, 2, 3, 5, 10):
            found, _ = sc.normalize_matches(
                payload([{"path": "f.java", "line": line, "snippet": snippet_for(line)}]),
                context=2, evidence_only=False,
            )
            self.assertEqual(found[0]["text"], f"строка {line}", f"строка {line}")

    def test_context_kept_whole(self):
        found, _ = sc.normalize_matches(
            payload([{"path": "f.java", "line": 5, "snippet": snippet_for(5)}]),
            context=2, evidence_only=False,
        )
        self.assertEqual(found[0]["context"].splitlines(), FILE_LINES[2:7])

    def test_match_without_coordinates_is_dropped(self):
        found, warnings = sc.normalize_matches(
            payload([
                {"path": "f.java", "line": 5, "snippet": snippet_for(5)},
                {"path": "", "line": 5, "snippet": "x"},
                {"path": "g.java", "snippet": "x"},
                {"path": "h.java", "line": 0, "snippet": "x"},
            ]),
            context=2, evidence_only=False,
        )
        self.assertEqual(len(found), 1)
        self.assertTrue(any("без координат отброшено совпадений: 3" in w for w in warnings))

    def test_evidence_flag_keeps_only_grounded(self):
        found, _ = sc.normalize_matches(
            payload([{"path": "f.java", "line": 5, "snippet": snippet_for(5)}]),
            context=2, evidence_only=True,
        )
        self.assertEqual(len(found), 1)

    def test_empty_input_gives_empty_output(self):
        found, warnings = sc.normalize_matches(payload([]), context=2, evidence_only=False)
        self.assertEqual(found, [])
        self.assertEqual(warnings, [])


class CommandTest(unittest.TestCase):
    def test_findings_reported_with_scope(self):
        code, out = run_grep(payload([{"path": "f.java", "line": 5, "snippet": snippet_for(5)}]))
        self.assertEqual(code, 0)
        self.assertEqual(out["command"], "grep")
        self.assertEqual(out["findingCount"], 1)
        self.assertEqual(out["scope"]["maxFiles"], 5000)
        self.assertFalse(out["scope"]["evidenceOnly"])

    def test_empty_result_is_not_proof_of_absence(self):
        """Пустой результат доказывает отсутствие в просмотренной области, не в кодовой базе."""
        code, out = run_grep(payload([]))
        self.assertEqual(code, 0)
        self.assertEqual(out["findings"], [])
        message = " ".join(out["warnings"])
        self.assertIn("НЕ доказательство отсутствия", message)
        self.assertIn("master", message)
        self.assertIn("billing", message)

    def test_file_limit_reached_is_surfaced(self):
        code, out = run_grep(
            payload([{"path": "f.java", "line": 1, "snippet": snippet_for(1)}],
                    meta={"errors": [], "filesScanned": 200}),
            max_files=200,
        )
        self.assertTrue(any("исчерпана не полностью" in w for w in out["warnings"]))

    def test_file_limit_not_reached_stays_quiet(self):
        code, out = run_grep(
            payload([{"path": "f.java", "line": 1, "snippet": snippet_for(1)}],
                    meta={"errors": [], "filesScanned": 12}),
            max_files=200,
        )
        self.assertEqual(out["warnings"], [])

    def test_notes_from_search_are_carried_through(self):
        code, out = run_grep(
            payload([{"path": "f.java", "line": 1, "snippet": snippet_for(1)}],
                    notes=["Archive search failed: 404"]),
        )
        self.assertEqual(out["notes"], ["Archive search failed: 404"])

    def test_missing_repo_stops_instead_of_guessing(self):
        code, out = run_grep({"command": "grep", "needsRepository": True, "candidates": []})
        self.assertEqual(code, 1)
        self.assertTrue(any("сначала выбери" in w for w in out["warnings"]))

    def test_evidence_flag_reflected_in_scope(self):
        code, out = run_grep(
            payload([{"path": "f.java", "line": 1, "snippet": snippet_for(1)}]), evidence=True
        )
        self.assertTrue(out["scope"]["evidenceOnly"])


class SearchUnchangedTest(unittest.TestCase):
    """Рефакторинг под grep не должен был поменять контракт `search`."""

    def test_search_payload_keeps_its_command_name(self):
        original_list = sc.list_repos
        sc.list_repos = lambda *a, **k: {"items": []}
        try:
            out = sc.search_payload(
                argparse.Namespace(command="search", repo=None, repo_query=None,
                                   query=["x"], repo_limit=20, base_url="http://x",
                                   tenant="t", project="p", timeout=1.0,
                                   profile=None, defaults_applied=common.DefaultsApplied())
            )
        finally:
            sc.list_repos = original_list
        self.assertEqual(out["command"], "search")
        self.assertTrue(out["needsRepository"])

    def test_both_subcommands_registered(self):
        parsed = sc.parse_args(["grep", "--tenant", "t", "--project", "p", "--query", "x"])
        self.assertEqual(parsed.command, "grep")
        self.assertFalse(parsed.evidence)
        parsed = sc.parse_args(["search", "--tenant", "t", "--project", "p", "--query", "x"])
        self.assertEqual(parsed.command, "search")
        self.assertFalse(hasattr(parsed, "evidence"), "--evidence живёт только у grep")


class ReferenceTest(unittest.TestCase):
    def test_grounding_section_documented(self):
        text = (SCRIPTS.parent / "references" / "source-control.md").read_text(encoding="utf-8")
        self.assertIn("Заземление без локального клона", text)
        for fragment in ("grep", "--evidence", "архив", "одной ветки"):
            self.assertIn(fragment, text, f"в справочнике нет: {fragment}")

    def test_negative_conclusion_rule_documented(self):
        text = (SCRIPTS.parent / "references" / "source-control.md").read_text(encoding="utf-8")
        self.assertIn("не доказывает отсутствия", text)


if __name__ == "__main__":
    unittest.main()
