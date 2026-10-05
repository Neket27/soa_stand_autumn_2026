"""Перечень вложений юнита: форма ответа снята с portal 18.08.2026 на CRPV-106611."""

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import swtr_task_tool as tool  # noqa: E402

BASE = ["--base-url", "https://swtr.test/swtr", "--env-file", "/нет/такого"]

LIVE_RESPONSE = {
    "content": [{
        "fileId": "0bf411dd-e015-4838-ad2b-f6ebabcf1bec",
        "filePathParsedDto": {"relatedToType": "UNIT_FILE",
                              "relativePath": "18cb7daa/6ae83044",
                              "fileName": "RE Блокеры перехода ПАО на OneWork.7z"},
        "createdBy": {"externalId": "Shevchenko.D.Vl", "login": "shevchenko.d.vl"},
        "createdAt": "2025-12-15T07:09:25.719054Z",
        "fileMetadataDto": {"contentType": "application/x-7z-compressed",
                            "contentLength": 322508},
        "fileNotFound": False,
    }],
    "pageSize": 10, "hasNext": False, "pageNumber": 0, "totalElements": 1,
}

EMPTY_RESPONSE = {"content": [], "pageSize": 10, "hasNext": False,
                  "pageNumber": 0, "totalElements": 0}


def run(*argv, response=LIVE_RESPONSE):
    seen = {}

    def fake_get(base_url, endpoint, timeout, **kwargs):
        seen["endpoint"] = endpoint
        return response

    original = tool.get_json
    tool.get_json = fake_get
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(list(argv))
    finally:
        tool.get_json = original
    return code, json.loads(out.getvalue().strip() or err.getvalue().strip()), seen


class TestAttachments(unittest.TestCase):
    def test_lists_one_attachment_with_name_type_and_size(self):
        code, payload, _ = run("attachments", "CRPV-106611", *BASE)
        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 1)
        row = payload["attachments"][0]
        self.assertEqual(row["fileName"], "RE Блокеры перехода ПАО на OneWork.7z")
        self.assertEqual(row["contentType"], "application/x-7z-compressed")
        self.assertEqual(row["contentLength"], 322508)
        self.assertEqual(row["fileId"], "0bf411dd-e015-4838-ad2b-f6ebabcf1bec")
        self.assertEqual(row["createdBy"], "shevchenko.d.vl")
        self.assertFalse(row["missingInStorage"])

    def test_safe_mode_is_always_on(self):
        """Без safe=true метод падает, когда файла нет в S3, — и вместо перечня
        вложений вызывающий получает отказ по всему юниту."""
        _code, _payload, seen = run("attachments", "CRPV-106611", *BASE)
        self.assertIn("safe=true", seen["endpoint"])
        self.assertIn("/rest/api/unit/files/v2/CRPV-106611", seen["endpoint"])

    def test_unit_without_attachments_is_not_an_error(self):
        code, payload, _ = run("attachments", "CRPV-155159", *BASE, response=EMPTY_RESPONSE)
        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["attachments"], [])

    def test_url_is_accepted_instead_of_a_code(self):
        _code, payload, _ = run(
            "attachments",
            "https://portal.works.prod.sbt/swtr/units/all/unit/CRPV-106611?space=CRPV",
            *BASE)
        self.assertEqual(payload["unit"], "CRPV-106611")

    def test_file_missing_in_storage_is_reported(self):
        response = json.loads(json.dumps(LIVE_RESPONSE))
        response["content"][0]["fileNotFound"] = True
        _code, payload, _ = run("attachments", "CRPV-106611", *BASE, response=response)
        self.assertTrue(payload["attachments"][0]["missingInStorage"])


if __name__ == "__main__":
    unittest.main()
