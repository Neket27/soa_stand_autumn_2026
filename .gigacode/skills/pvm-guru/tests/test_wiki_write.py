"""Запись wiki-страницы: тело и место в дереве.

Проверено на живом трекере 14.08.2026 (PVM-175549 и соседи):

* `wiki_page_body` принимается ТОЛЬКО строкой с JSON ProseMirror внутри. Объект
  вместо строки — HTTP 500, голый текст без разметки — тоже 500. Отсюда и взялся
  вывод «API не умеет создавать страницы с текстом»: `--attribute` разбирал
  значение как JSON и отправлял объект;
* созданная страница остаётся без родителя и в дереве вики не видна, пока не
  заведена связь `wiki_page_hierarchy`, где ИСТОЧНИК — родитель, а НАЗНАЧЕНИЕ —
  ребёнок. Обратное направление трекер отвергает HTTP 570, потому что у страницы
  не больше одного родителя.
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

WIKI_UNIT = {
    "code": "PVM-175549",
    "suit": {"code": "wiki_page", "name": "Страница WIKI"},
    "space": {"code": "PVM", "name": "PVM"},
    "attributes": [{"code": "wiki_page_body", "value": "{\"type\": \"doc\", \"content\": []}"}],
}

WIKI_SCHEMA = {
    "code": "wiki_page",
    "attributes": [
        {"code": "wiki_page_body", "name": "Тело страницы WIKI",
         "type": "wiki_page_wisiwig", "directoryValues": []},
    ],
}

DOC_JSON = json.dumps({"type": "doc", "content": [
    {"type": "paragraph", "content": [{"type": "text", "text": "готовое"}]}]},
    ensure_ascii=False)


class Recorder:
    def __init__(self, created="PVM-175560"):
        self.calls = []
        self.created = created

    def __call__(self, method, url, headers, *, body=None, timeout=30.0,
                 system=None, max_retries=2, **kwargs):
        self.calls.append({"method": method, "url": url, "body": body})
        if url.endswith("/link"):
            return True, {}, 200
        return {"id": self.created}, {}, 200

    @property
    def writes(self):
        return [c for c in self.calls if c["method"] in ("PATCH", "POST", "PUT", "DELETE")]


def run(recorder, *argv, stdin=None):
    original = (tool.get_json, tool.post_json, tool.request_http, tool.build_headers, sys.stdin)

    def fake_get(base_url, endpoint, timeout, **kwargs):
        if "/rest/api/suit/v1/" in endpoint:
            return WIKI_SCHEMA
        if "/rest/api/unit/v2/" in endpoint:
            return WIKI_UNIT
        raise AssertionError("неожиданный GET: %s" % endpoint)

    def fake_post(base_url, endpoint, body, timeout, **kwargs):
        raise AssertionError("неожиданный POST: %s" % endpoint)

    tool.get_json, tool.post_json = fake_get, fake_post
    tool.request_http = recorder
    tool.build_headers = lambda *a, **k: {}
    if stdin is not None:
        sys.stdin = io.StringIO(stdin)
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(list(argv))
    finally:
        (tool.get_json, tool.post_json, tool.request_http,
         tool.build_headers, sys.stdin) = original
    return code, json.loads(out.getvalue().strip() or err.getvalue().strip())


def markdown_file(text):
    handle = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
    handle.write(text)
    handle.close()
    return handle.name


class BodyIsAlwaysSentAsAString(unittest.TestCase):
    def test_create_turns_markdown_into_a_string_attribute(self):
        rec = Recorder()
        path = markdown_file("# Заголовок\n\nтекст")
        code, _ = run(rec, "create", "--suit", "wiki_page", "--space", "PVM",
                      "--summary", "страница", "--wiki-body", path, "--execute", *BASE)
        self.assertEqual(code, 0)
        sent = rec.writes[0]["body"]["attributes"]["wiki_page_body"]
        self.assertIsInstance(sent, str, "объект вместо строки трекер отвергает HTTP 500")
        self.assertEqual(json.loads(sent)["content"][0]["type"], "heading")

    def test_update_turns_markdown_into_a_string_attribute(self):
        rec = Recorder()
        path = markdown_file("- пункт")
        code, _ = run(rec, "update", "PVM-175549", "--wiki-body", path, "--execute", *BASE)
        self.assertEqual(code, 0)
        call = rec.writes[0]
        self.assertEqual(call["method"], "PATCH")
        sent = call["body"]["attributes"]["wiki_page_body"]
        self.assertIsInstance(sent, str)
        self.assertEqual(json.loads(sent)["content"][0]["type"], "bulletList")

    def test_raw_attribute_with_a_document_is_serialized_not_sent_as_an_object(self):
        """Ровно та ловушка, из которой родился вывод «API не поддерживает»."""
        rec = Recorder()
        code, _ = run(rec, "create", "--suit", "wiki_page", "--space", "PVM",
                      "--summary", "страница", "--attribute", f"wiki_page_body={DOC_JSON}",
                      "--execute", *BASE)
        self.assertEqual(code, 0)
        sent = rec.writes[0]["body"]["attributes"]["wiki_page_body"]
        self.assertIsInstance(sent, str)
        self.assertEqual(json.loads(sent)["content"][0]["content"][0]["text"], "готовое")

    def test_update_serializes_a_raw_document_attribute_too(self):
        rec = Recorder()
        code, _ = run(rec, "update", "PVM-175549", "--attribute", f"wiki_page_body={DOC_JSON}",
                      "--execute", *BASE)
        self.assertEqual(code, 0)
        self.assertIsInstance(rec.writes[0]["body"]["attributes"]["wiki_page_body"], str)

    def test_ready_document_json_is_not_converted_twice(self):
        rec = Recorder()
        path = markdown_file(DOC_JSON)
        run(rec, "update", "PVM-175549", "--wiki-body", path, "--execute", *BASE)
        sent = json.loads(rec.writes[0]["body"]["attributes"]["wiki_page_body"])
        self.assertEqual(sent["content"][0]["content"][0]["text"], "готовое")

    def test_body_can_come_from_stdin(self):
        rec = Recorder()
        code, _ = run(rec, "update", "PVM-175549", "--wiki-body", "-", "--execute",
                      *BASE, stdin="## из потока")
        self.assertEqual(code, 0)
        sent = json.loads(rec.writes[0]["body"]["attributes"]["wiki_page_body"])
        self.assertEqual(sent["content"][0]["attrs"]["level"], 2)

    def test_unreadable_body_file_is_refused_before_any_write(self):
        rec = Recorder()
        code, data = run(rec, "update", "PVM-175549", "--wiki-body", "/нет/такого.md",
                         "--execute", *BASE)
        self.assertEqual(code, 1)
        self.assertEqual(rec.writes, [])
        self.assertIn("wiki-body", json.dumps(data, ensure_ascii=False))


class PlaceInTheTree(unittest.TestCase):
    def test_parent_link_is_created_after_the_page(self):
        rec = Recorder(created="PVM-175560")
        path = markdown_file("текст")
        code, data = run(rec, "create", "--suit", "wiki_page", "--space", "PVM",
                         "--summary", "ребёнок", "--wiki-body", path,
                         "--parent", "PVM-175549", "--execute", *BASE)
        self.assertEqual(code, 0)
        self.assertEqual(len(rec.writes), 2, "создание страницы и связь с родителем")
        link = rec.writes[1]
        self.assertTrue(link["url"].endswith("/rest/api/unit/v1/link"), link["url"])
        self.assertEqual(link["body"], {"source": "PVM-175549",
                                        "destination": "PVM-175560",
                                        "type": "wiki_page_hierarchy"})

    def test_parent_link_result_is_reported(self):
        rec = Recorder()
        code, data = run(rec, "create", "--suit", "wiki_page", "--space", "PVM",
                         "--summary", "ребёнок", "--parent", "PVM-175549", "--execute", *BASE)
        self.assertIn("parent", data)
        self.assertEqual(data["parent"]["code"], "PVM-175549")

    def test_dry_run_shows_the_planned_link_and_writes_nothing(self):
        rec = Recorder()
        code, data = run(rec, "create", "--suit", "wiki_page", "--space", "PVM",
                         "--summary", "ребёнок", "--parent", "PVM-175549", *BASE)
        self.assertEqual(code, 0)
        self.assertEqual(rec.writes, [])
        self.assertTrue(data.get("dryRun"))
        self.assertEqual(data["parent"]["type"], "wiki_page_hierarchy")

    def test_page_without_parent_is_flagged_as_detached(self):
        """Молчаливая потеря: страница создана, но в дереве её не видно."""
        rec = Recorder()
        code, data = run(rec, "create", "--suit", "wiki_page", "--space", "PVM",
                         "--summary", "сирота", "--execute", *BASE)
        self.assertEqual(code, 0)
        self.assertRegex(json.dumps(data, ensure_ascii=False).lower(),
                         r"родител|дерев")


if __name__ == "__main__":
    unittest.main()
