"""Создание Pull Request: dry-run по умолчанию, дубли отклоняются."""

import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import source_control_pr as pr  # noqa: E402

BASE = ["--tenant", "t", "--project", "p", "--repo", "billing",
        "--base-url", "https://example.invalid/api/v3", "--env-file", "/нет/такого"]
CREATE = BASE + ["--head", "feature/PVM-160042-add-expiry", "--base", "master",
                 "--title", "Срок действия счёта в модели и выдаче"]


class Recorder:
    """Подменяет сетевой слой и запоминает каждый вызов."""

    def __init__(self, pulls=None, created=None):
        self.calls = []
        self.pulls = pulls if pulls is not None else []
        self.created = created or {"index": 77, "title": "t",
                                   "html_url": "https://example.invalid/pulls/77"}

    def __call__(self, method, base_url, endpoint, *, params=None, body=None, timeout=30.0):
        self.calls.append({"method": method, "endpoint": endpoint, "body": body})
        if method == "GET" and endpoint.endswith("/pulls"):
            return self.pulls
        if method == "POST" and endpoint.endswith("/pulls"):
            return self.created
        return {}

    @property
    def writes(self):
        return [c for c in self.calls if c["method"] != "GET"]


def run(recorder, *argv):
    original = pr.sc_request
    pr.sc_request = recorder
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = pr.main(list(argv))
    finally:
        pr.sc_request = original
    return code, json.loads(buf.getvalue())


class DryRunTest(unittest.TestCase):
    def test_dry_run_makes_no_write_call(self):
        recorder = Recorder()
        code, out = run(recorder, "create", *CREATE)
        self.assertEqual(code, 0, out["errors"])
        self.assertEqual(recorder.writes, [], "dry-run сделал сетевую запись")
        self.assertFalse(out["created"])
        self.assertFalse(out["executed"])
        self.assertTrue(any("dry-run" in w for w in out["warnings"]))

    def test_dry_run_shows_the_exact_request(self):
        code, out = run(Recorder(), "create", *CREATE, "--change-id", "PVM-160042-add-expiry")
        request = out["requests"][0]
        self.assertEqual(request["method"], "POST")
        self.assertTrue(request["endpoint"].endswith("/pulls"))
        self.assertEqual(request["body"]["head"], "feature/PVM-160042-add-expiry")
        self.assertEqual(request["body"]["base"], "master")

    def test_duplicate_check_runs_before_any_write(self):
        recorder = Recorder()
        run(recorder, "create", *CREATE)
        self.assertEqual(recorder.calls[0]["method"], "GET", "дубли не проверены до записи")

    def test_execute_creates(self):
        recorder = Recorder()
        code, out = run(recorder, "create", *CREATE, "--execute")
        self.assertEqual(code, 0, out["errors"])
        self.assertTrue(out["created"])
        self.assertEqual(out["pull"]["index"], 77)
        self.assertEqual([c["method"] for c in recorder.writes], ["POST"])


class DuplicateTest(unittest.TestCase):
    def _existing(self, head, state="open"):
        return [{"index": 42, "title": "уже открыт", "state": state,
                 "head": {"ref": head}, "base": {"ref": "master"},
                 "html_url": "https://example.invalid/pulls/42"}]

    def test_same_head_is_refused(self):
        recorder = Recorder(pulls=self._existing("feature/PVM-160042-add-expiry"))
        code, out = run(recorder, "create", *CREATE, "--execute")
        self.assertEqual(code, 1)
        self.assertEqual(out["duplicate"]["index"], 42)
        self.assertEqual(recorder.writes, [], "при дубле запись всё равно произошла")
        self.assertTrue(any("уже открыт PR #42" in e for e in out["errors"]))

    def test_closed_pr_is_not_a_duplicate(self):
        recorder = Recorder(pulls=self._existing("feature/PVM-160042-add-expiry", state="closed"))
        code, out = run(recorder, "create", *CREATE)
        self.assertEqual(code, 0)
        self.assertIsNone(out["duplicate"])

    def test_other_branch_is_not_a_duplicate(self):
        recorder = Recorder(pulls=self._existing("feature/другая"))
        code, out = run(recorder, "create", *CREATE)
        self.assertEqual(code, 0)
        self.assertIsNone(out["duplicate"])

    def test_head_with_owner_prefix_still_matches(self):
        recorder = Recorder(pulls=self._existing("user:feature/PVM-160042-add-expiry"))
        code, out = run(recorder, "create", *CREATE)
        self.assertEqual(code, 1)

    def test_plain_string_head_supported(self):
        recorder = Recorder(pulls=[{"index": 7, "state": "open",
                                    "head": "feature/PVM-160042-add-expiry", "base": "master"}])
        code, out = run(recorder, "create", *CREATE)
        self.assertEqual(code, 1)

    def test_same_head_and_base_is_refused_without_network(self):
        recorder = Recorder()
        code, out = run(recorder, "create", *BASE, "--head", "master", "--base", "master",
                        "--title", "что-то осмысленное")
        self.assertEqual(code, 1)
        self.assertEqual(recorder.calls, [], "запрос ушёл при заведомо неверных ветках")


class BodyTest(unittest.TestCase):
    def test_body_carries_change_id_and_task(self):
        code, out = run(Recorder(), "create", *CREATE,
                        "--change-id", "PVM-160042-add-expiry",
                        "--task", "PVM-160042",
                        "--task-url", "https://portal.works.prod.sbt/swtr/units/all/unit/PVM-160042")
        body = out["requests"][0]["body"]["body"]
        self.assertIn("PVM-160042-add-expiry", body)
        self.assertIn("https://portal.works.prod.sbt/swtr/units/all/unit/PVM-160042", body)

    def test_body_lists_requirements_and_related(self):
        code, out = run(Recorder(), "create", *CREATE,
                        "--requirement", "REQ-001", "--requirement", "REQ-002",
                        "--related", "https://example.invalid/other/pulls/3")
        body = out["requests"][0]["body"]["body"]
        self.assertIn("- REQ-001", body)
        self.assertIn("- REQ-002", body)
        self.assertIn("https://example.invalid/other/pulls/3", body)

    def test_body_is_empty_when_nothing_to_say(self):
        code, out = run(Recorder(), "create", *CREATE)
        self.assertEqual(out["requests"][0]["body"]["body"], "")

    def test_summary_goes_first(self):
        code, out = run(Recorder(), "create", *CREATE, "--summary", "Счета получают срок жизни.",
                        "--change-id", "PVM-160042-add-expiry")
        body = out["requests"][0]["body"]["body"]
        self.assertTrue(body.startswith("Счета получают срок жизни."))

    def test_empty_title_warns(self):
        self.assertTrue(pr.check_title("Фикс", None))
        self.assertTrue(any("ничего не сообщает" in w for w in pr.check_title("fix", None)))

    def test_meaningful_title_is_quiet(self):
        self.assertEqual(pr.check_title("Срок действия счёта в модели и выдаче", None), [])

    def test_change_id_in_title_is_redundant(self):
        warnings = pr.check_title("PVM-160042-add-expiry: срок действия", "PVM-160042-add-expiry")
        self.assertTrue(any("занимает место зря" in w for w in warnings))


class ReviewersTest(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.pop(pr.REVIEWERS_ENV, None)
        self.addCleanup(
            lambda: os.environ.__setitem__(pr.REVIEWERS_ENV, self.saved)
            if self.saved is not None else None
        )

    def test_missing_reviewers_do_not_block(self):
        code, out = run(Recorder(), "create", *CREATE)
        self.assertEqual(code, 0)
        self.assertEqual(out["reviewers"], [])
        self.assertTrue(any(pr.REVIEWERS_ENV in w for w in out["warnings"]))

    def test_reviewers_from_environment(self):
        os.environ[pr.REVIEWERS_ENV] = "ivanov, petrov"
        self.addCleanup(lambda: os.environ.pop(pr.REVIEWERS_ENV, None))
        code, out = run(Recorder(), "create", *CREATE)
        self.assertEqual(out["reviewers"], ["ivanov", "petrov"])
        self.assertFalse(any(pr.REVIEWERS_ENV in w for w in out["warnings"]))

    def test_explicit_reviewers_win(self):
        os.environ[pr.REVIEWERS_ENV] = "ivanov"
        self.addCleanup(lambda: os.environ.pop(pr.REVIEWERS_ENV, None))
        code, out = run(Recorder(), "create", *CREATE, "--reviewer", "sidorov")
        self.assertEqual(out["reviewers"], ["sidorov"])

    def test_reviewers_are_a_separate_request(self):
        os.environ[pr.REVIEWERS_ENV] = "ivanov"
        self.addCleanup(lambda: os.environ.pop(pr.REVIEWERS_ENV, None))
        code, out = run(Recorder(), "create", *CREATE)
        self.assertEqual(len(out["requests"]), 2)
        self.assertIn("requested_reviewers", out["requests"][1]["endpoint"])

    def test_reviewer_failure_never_leaks_a_secret(self):
        """Текст ошибки может нести тело ответа с токеном — в stdout он идёт замаскированным."""
        os.environ[pr.REVIEWERS_ENV] = "ivanov"
        os.environ["SOURCE_CONTROL_TOKEN"] = "sekret-token-value"
        self.addCleanup(lambda: os.environ.pop(pr.REVIEWERS_ENV, None))
        self.addCleanup(lambda: os.environ.pop("SOURCE_CONTROL_TOKEN", None))

        class Leaking(Recorder):
            def __call__(self, method, base_url, endpoint, **kwargs):
                if "requested_reviewers" in endpoint:
                    raise RuntimeError('401 {"token": "sekret-token-value"}')
                return super().__call__(method, base_url, endpoint, **kwargs)

        code, out = run(Leaking(), "create", *CREATE, "--execute")
        self.assertNotIn("sekret-token-value", json.dumps(out, ensure_ascii=False))
        self.assertTrue(any("redacted" in w for w in out["warnings"]))

    def test_reviewer_failure_does_not_lose_the_pr(self):
        os.environ[pr.REVIEWERS_ENV] = "ivanov"
        self.addCleanup(lambda: os.environ.pop(pr.REVIEWERS_ENV, None))

        class Failing(Recorder):
            def __call__(self, method, base_url, endpoint, **kwargs):
                if "requested_reviewers" in endpoint:
                    raise RuntimeError("403")
                return super().__call__(method, base_url, endpoint, **kwargs)

        code, out = run(Failing(), "create", *CREATE, "--execute")
        self.assertEqual(code, 0)
        self.assertTrue(out["created"])
        self.assertTrue(any("ревьюеры не назначены" in w for w in out["warnings"]))


class ListTest(unittest.TestCase):
    def test_list_normalizes(self):
        recorder = Recorder(pulls={"items": [
            {"index": 1, "title": "a", "state": "open",
             "head": {"ref": "feature/a"}, "base": {"ref": "master"},
             "html_url": "https://example.invalid/pulls/1"},
        ]})
        code, out = run(recorder, "list", *BASE)
        self.assertEqual(code, 0)
        self.assertEqual(out["pullCount"], 1)
        self.assertEqual(out["pulls"][0]["head"], "feature/a")

    def test_list_makes_no_writes(self):
        recorder = Recorder()
        run(recorder, "list", *BASE)
        self.assertEqual(recorder.writes, [])


class RetryTest(unittest.TestCase):
    """Создающий запрос не повторяется сам: повтор — это второй PR."""

    def test_mutating_methods_default_to_no_retry(self):
        seen = {}
        os.environ["SOURCE_CONTROL_TOKEN"] = "тестовый"
        self.addCleanup(os.environ.pop, "SOURCE_CONTROL_TOKEN", None)

        def fake_request_http(method, url, headers, *, body=None, timeout=30.0,
                              system="task_tracker", binary=False, max_retries=3):
            seen[method] = max_retries
            return {}, {}, 200

        original = pr.request_http
        pr.request_http = fake_request_http
        try:
            pr.sc_request("POST", "https://example.invalid", "/x")
            pr.sc_request("GET", "https://example.invalid", "/x")
        finally:
            pr.request_http = original
        self.assertEqual(seen["POST"], 0, "повтор POST создаёт второй PR")
        self.assertEqual(seen["GET"], 3, "чтение повторять безопасно")

    def test_ambiguous_failure_is_reconciled_against_the_repository(self):
        """Сервер мог PR создать и не донести ответ — идём смотреть, а не повторяем."""

        landed = {"index": 91, "head": "feature/PVM-160042-add-expiry", "state": "open",
                  "title": "t", "url": "https://example.invalid/pulls/91"}

        class Flaky(Recorder):
            def __init__(self):
                super().__init__(pulls=[])
                self.posted = False

            def __call__(self, method, base_url, endpoint, *, params=None, body=None, timeout=30.0):
                if method == "POST" and endpoint.endswith("/pulls"):
                    self.calls.append({"method": method, "endpoint": endpoint, "body": body})
                    self.posted = True
                    raise RuntimeError("HTTP 504 for /pulls: gateway timeout")
                if method == "GET" and endpoint.endswith("/pulls"):
                    return [landed] if self.posted else []
                return super().__call__(method, base_url, endpoint, params=params, body=body,
                                        timeout=timeout)

        recorder = Flaky()
        code, out = run(recorder, "create", *CREATE, "--execute")
        self.assertEqual(code, 1)
        self.assertTrue(out["reconciled"])
        self.assertEqual(out["pull"]["index"], 91)
        self.assertTrue(any("повторять создание не нужно" in e.lower() for e in out["errors"]), out["errors"])
        self.assertEqual(len([c for c in recorder.calls if c["method"] == "POST"]), 1)

    def test_failure_without_a_landed_pr_says_it_is_safe_to_retry(self):
        class Failing(Recorder):
            def __call__(self, method, base_url, endpoint, *, params=None, body=None, timeout=30.0):
                if method == "POST" and endpoint.endswith("/pulls"):
                    raise RuntimeError("HTTP 502 for /pulls: bad gateway")
                return super().__call__(method, base_url, endpoint, params=params, body=body,
                                        timeout=timeout)

        code, out = run(Failing(), "create", *CREATE, "--execute")
        self.assertEqual(code, 1)
        self.assertFalse(out["reconciled"])
        self.assertFalse(out["created"])
        self.assertTrue(any("не появился" in e for e in out["errors"]), out["errors"])


class BoundaryTest(unittest.TestCase):
    def test_skill_separates_creation_from_review(self):
        skill = (SCRIPTS.parent / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("source_control_pr.py", skill)
        self.assertIn("guru-pr-review", skill)

    def test_script_never_touches_the_filesystem(self):
        src = (SCRIPTS / "source_control_pr.py").read_text(encoding="utf-8")
        for forbidden in ("write_text(", "mkdir(", "unlink("):
            self.assertNotIn(forbidden, src, f"скрипт меняет файловую систему: {forbidden}")

    def test_execute_guard_precedes_the_write(self):
        src = (SCRIPTS / "source_control_pr.py").read_text(encoding="utf-8")
        body = src.split("def cmd_create(")[1].split("\ndef ")[0]
        self.assertLess(
            body.index("if not args.execute:"),
            body.index('sc_request("POST"'),
            "запись раньше проверки --execute",
        )


if __name__ == "__main__":
    unittest.main()
