"""Запись связей: create и delete идут в разные ручки.

DELETE на `/rest/api/unit/v1/link` трекер не принимает — HTTP 405, метод там не
объявлен. Удаление живёт в `/rest/api/unit/v1/link/delete` и, судя по названию
операции в спеке, помечает связь удалённой, а не стирает её.

Ручку проверяем отдельным тестом, потому что 405 виден только на живом стенде:
dry-run печатает URL, но не ходит по нему, и опечатка в пути доживает до первой
настоящей записи.
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
LINK = ["--source", "PVM-1", "--destination", "PVM-2", "--type", "decomposition"]


class Recorder:
    """Подменяет сетевой слой и запоминает каждый вызов."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, *, body=None, timeout=30.0,
                 system=None, max_retries=0, **kwargs):
        self.calls.append({"method": method, "url": url, "body": body})
        return {}, {}, 200


def run(recorder, *argv):
    original_request, original_headers = tool.request_http, tool.build_headers
    tool.request_http = recorder
    tool.build_headers = lambda *a, **k: {"Authorization": "Bearer x"}
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            code = tool.main(list(argv))
    finally:
        tool.request_http, tool.build_headers = original_request, original_headers
    return code, json.loads(out.getvalue())


class LinkWriteTest(unittest.TestCase):
    def test_create_posts_to_the_link_endpoint(self):
        recorder = Recorder()
        code, _out = run(recorder, "link", "create", *LINK, "--execute", *BASE)
        self.assertEqual(code, 0)
        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(recorder.calls[0]["method"], "POST")
        self.assertEqual(recorder.calls[0]["url"],
                         "https://example.invalid/swtr/rest/api/unit/v1/link")

    def test_delete_uses_the_delete_endpoint(self):
        recorder = Recorder()
        code, _out = run(recorder, "link", "delete", *LINK, "--execute", *BASE)
        self.assertEqual(code, 0)
        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(recorder.calls[0]["method"], "DELETE")
        self.assertEqual(recorder.calls[0]["url"],
                         "https://example.invalid/swtr/rest/api/unit/v1/link/delete",
                         "DELETE на /link трекер отдаёт 405 — удаление в /link/delete")

    def test_delete_sends_the_same_body_as_create(self):
        recorder = Recorder()
        run(recorder, "link", "delete", *LINK, "--execute", *BASE)
        self.assertEqual(recorder.calls[0]["body"],
                         {"source": "PVM-1", "destination": "PVM-2",
                          "type": "decomposition"})

    def test_dry_run_shows_the_url_that_execute_will_call(self):
        """Предпросмотр обязан совпадать с записью: иначе он врёт про ручку."""
        recorder = Recorder()
        _code, preview = run(recorder, "link", "delete", *LINK, *BASE)
        self.assertEqual(recorder.calls, [], "dry-run сделал сетевую запись")
        self.assertTrue(preview["dryRun"])

        executed = Recorder()
        run(executed, "link", "delete", *LINK, "--execute", *BASE)
        self.assertEqual(preview["request"]["url"], executed.calls[0]["url"])
        self.assertEqual(preview["request"]["method"], executed.calls[0]["method"])


if __name__ == "__main__":
    unittest.main()
