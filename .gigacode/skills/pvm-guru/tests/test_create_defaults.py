"""Создание задачи: workflow_status подставляется сам, HTTP 570 объясняется.

Suit с workflow отвергает создание без ``workflow_status`` кодом HTTP 570, и
без подстановки каждый первый вызов ``create`` упирался в этот отказ. Тесты
держат три границы: подстановка есть по умолчанию, она не перетирает явное
значение и не лезет в ``--body-file``, а сам 570 больше не выглядит как
безымянный отказ трекера.
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import swtr_task_tool as tool  # noqa: E402

BASE = ["--base-url", "https://example.invalid/swtr", "--env-file", "/нет/такого"]


class Recorder:
    """Подменяет сетевой слой и запоминает каждый вызов."""

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def __call__(self, method, url, headers, *, body=None, timeout=30.0,
                 system=None, max_retries=2, **kwargs):
        self.calls.append({"method": method, "url": url, "body": body,
                           "max_retries": max_retries})
        if self.error is not None:
            raise self.error
        return {"id": "PVM-1"}, {}, 200


def run(recorder, *argv):
    """Гоняет main() с подменённым HTTP и авторизацией, возвращает (код, JSON)."""
    original_http, original_headers = tool.request_http, tool.build_headers
    tool.request_http = recorder
    tool.build_headers = lambda *a, **kw: {"Accept": "application/json"}
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(list(argv))
    finally:
        tool.request_http, tool.build_headers = original_http, original_headers
    payload = out.getvalue() or err.getvalue()
    return code, json.loads(payload)


class DefaultWorkflowStatusTest(unittest.TestCase):
    def test_injected_when_absent(self):
        code, out = run(Recorder(), "create", "--space", "PVM", "--summary", "S", *BASE)
        self.assertEqual(code, 0)
        self.assertEqual(out["request"]["body"]["attributes"],
                         {"workflow_status": {"command": "NEW"}})

    def test_visible_in_dry_run_payload(self):
        """Агент обязан видеть в dry-run ровно то, что уйдёт на сервер."""
        recorder = Recorder()
        code, out = run(recorder, "create", "--space", "PVM", "--summary", "S", *BASE)
        self.assertTrue(out["dryRun"])
        self.assertEqual(recorder.calls, [])
        self.assertIn("workflow_status", out["request"]["body"]["attributes"])

    def test_explicit_value_is_not_overwritten(self):
        code, out = run(Recorder(), "create", "--space", "PVM", "--summary", "S",
                        "--attribute", 'workflow_status={"command":"START"}', *BASE)
        self.assertEqual(code, 0)
        self.assertEqual(out["request"]["body"]["attributes"]["workflow_status"],
                         {"command": "START"})

    def test_opt_out_leaves_attributes_empty(self):
        code, out = run(Recorder(), "create", "--space", "PVM", "--summary", "S",
                        "--no-default-workflow-status", *BASE)
        self.assertEqual(code, 0)
        self.assertEqual(out["request"]["body"]["attributes"], {})

    def test_other_attributes_survive(self):
        code, out = run(Recorder(), "create", "--space", "PVM", "--summary", "S",
                        "--attribute", "priority=mid", *BASE)
        attributes = out["request"]["body"]["attributes"]
        self.assertEqual(attributes["priority"], "mid")
        self.assertEqual(attributes["workflow_status"], {"command": "NEW"})

    def test_body_file_is_left_alone(self):
        """--body-file — полный контроль вызывающего, подстановка его не трогает."""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as handle:
            json.dump({"summary": "S", "space": "PVM", "attributes": {}}, handle)
            path = handle.name
        try:
            code, out = run(Recorder(), "create", "--body-file", path, *BASE)
        finally:
            Path(path).unlink()
        self.assertEqual(code, 0)
        self.assertEqual(out["request"]["body"]["attributes"], {})


class ExecuteTest(unittest.TestCase):
    def test_execute_posts_without_retry(self):
        recorder = Recorder()
        code, out = run(recorder, "create", "--space", "PVM", "--summary", "S",
                        "--execute", *BASE)
        self.assertEqual(code, 0)
        self.assertFalse(out["dryRun"])
        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(recorder.calls[0]["method"], "POST")
        # Повтор записи создал бы дубль задачи.
        self.assertEqual(recorder.calls[0]["max_retries"], 0)


class MissingFieldsHintTest(unittest.TestCase):
    def test_570_about_workflow_status_names_the_fix(self):
        error = RuntimeError("HTTP 570 for https://example.invalid/swtr/rest/api/unit/v2/task/"
                             "create: {\"uiErrorMessage\":\"Не заполнены обязательные поля: "
                             "'workflow_status'\"}")
        code, out = run(Recorder(error=error), "create", "--space", "PVM",
                        "--summary", "S", "--execute", *BASE)
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertIn("workflow_status", out["hint"])
        self.assertIn("NEW", out["hint"])

    def test_570_about_other_fields_still_explained(self):
        error = RuntimeError("HTTP 570 for https://example.invalid/: "
                             "{\"uiErrorMessage\":\"Не заполнены обязательные поля: 'component'\"}")
        code, out = run(Recorder(error=error), "create", "--space", "PVM",
                        "--summary", "S", "--execute", *BASE)
        self.assertEqual(code, 1)
        self.assertIn("--attribute", out["hint"])

    def test_unrelated_error_keeps_the_plain_contract(self):
        """403 — не про обязательные поля, подсказку сюда цеплять нельзя."""
        error = RuntimeError("HTTP 403 for https://example.invalid/: {\"errorType\":\"FORBIDDEN\"}")
        code, out = run(Recorder(error=error), "create", "--space", "PVM",
                        "--summary", "S", "--execute", *BASE)
        self.assertEqual(code, 1)
        self.assertNotIn("hint", out)

    def test_helper_ignores_non_570(self):
        self.assertIsNone(tool.missing_fields_hint(RuntimeError("HTTP 500 boom")))


if __name__ == "__main__":
    unittest.main()
