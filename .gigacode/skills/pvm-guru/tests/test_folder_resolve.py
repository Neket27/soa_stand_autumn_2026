"""Папка тест-кейса: имя и путь превращаются в код, неоднозначность — отказ.

Папка адресуется UUID, но человек называет её именем. Имя само по себе
неоднозначно: в дереве PVM около 2700 папок, и `INST`, `K8S`, `AI Generated`
встречаются в разных ветках. Молча взятый первый попавшийся кандидат кладёт
кейс не туда, и обнаруживается это через недели. Поэтому совпадение ищется по
хвосту пути, а неоднозначность перечисляет кандидатов и требует уточнения.
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
FOLDER_UUID = "550c265e-2298-4f19-acfd-9ffda367a1f1"

# Форма ответа `POST …/swtr_tms_plugin/v1/folder/root` (портал, 18.08.2026):
# код лежит в id.code, имя — в title, вложенность — в children.
def folder(title, code, *children):
    return {"title": title, "id": {"code": code}, "key": code, "children": list(children)}


TREE = folder(
    "Все тест-кейсы", "PVM_test_case",
    folder("Корзина", FOLDER_UUID),
    folder("BUSINESS SERVICES", "bs",
           folder("AUDIT", "audit",
                  folder("UI", "ui",
                         folder("UI Audit PVM", "uiaudit",
                                folder("R7.2", "r72", folder("Источники", "sources-72")),
                                folder("R7.3", "r73", folder("Источники", "sources-73")))))),
    folder("ALOG", "alog", folder("INST", "alog-inst")),
    folder("AIOM", "aiom", folder("INST", "aiom-inst")),
)


class Recorder:
    """Сеть: дерево папок на folder/root, успех на всё остальное."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, *, body=None, timeout=30.0,
                 system=None, max_retries=2, **kwargs):
        self.calls.append({"method": method, "url": url, "body": body})
        if url.endswith("/folder/root"):
            return TREE, {}, 200
        return {"id": "PVM-1"}, {}, 200


def run(recorder, *argv):
    original_http, original_headers = tool.request_http, tool.build_headers
    tool.request_http = recorder
    tool.build_headers = lambda *a, **kw: {"Accept": "application/json"}
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(list(argv))
    finally:
        tool.request_http, tool.build_headers = original_http, original_headers
    return code, json.loads(out.getvalue() or err.getvalue())


def create(folder_value, recorder=None):
    recorder = recorder or Recorder()
    code, payload = run(recorder, "create", "--suit", "test_case", "--space", "PVM",
                        "--summary", "проверка", "--attribute", f"folder={folder_value}", *BASE)
    return code, payload, recorder


class UuidPassesThrough(unittest.TestCase):
    def test_uuid_is_not_resolved_and_costs_no_request(self):
        code, payload, recorder = create(FOLDER_UUID)
        self.assertEqual(code, 0)
        self.assertEqual(payload["request"]["body"]["attributes"]["folder"], FOLDER_UUID)
        self.assertEqual([c for c in recorder.calls if c["url"].endswith("/folder/root")], [])


class CodePassesThrough(unittest.TestCase):
    def test_root_folder_code_is_accepted(self):
        # Референс называет `<SPACE>_test_case` рабочим значением, и дерево
        # отдаёт корень именно с этим кодом. Раньше он не проходил регулярку
        # UUID, искался как ИМЯ и отвергался — а подсказка отправляла за
        # деревом, в котором он и лежит.
        code, payload, _ = create("PVM_test_case")
        self.assertEqual(code, 0)
        self.assertEqual(payload["request"]["body"]["attributes"]["folder"], "PVM_test_case")

    def test_non_uuid_code_from_the_tree_is_accepted(self):
        _, payload, _ = create("alog-inst")
        self.assertEqual(payload["request"]["body"]["attributes"]["folder"], "alog-inst")

    def test_code_wins_over_a_title_lookup(self):
        # Код точный, имя — нет: порядок тот же, что у match_directory_code.
        self.assertEqual(tool.match_folder_path(tool.folder_paths(TREE), "sources-72"), "sources-72")


class ReadBackFolderRoundTrips(unittest.TestCase):
    def test_folder_read_from_a_root_case_is_written_back(self):
        # Сценарий «прочитал кейс → создал похожий»: у кейса из корневой папки
        # прочитанный код не UUID, и именно на нём ломался резолв.
        code, payload, _ = create('{"code": "PVM_test_case", "title": "Все тест-кейсы"}')
        self.assertEqual(code, 0)
        self.assertEqual(payload["request"]["body"]["attributes"]["folder"], "PVM_test_case")


class NameAndPath(unittest.TestCase):
    def test_unique_name_resolves(self):
        code, payload, _ = create("Корзина")
        self.assertEqual(code, 0)
        self.assertEqual(payload["request"]["body"]["attributes"]["folder"], FOLDER_UUID)

    def test_path_tail_disambiguates(self):
        _, payload, _ = create("R7.2/Источники")
        self.assertEqual(payload["request"]["body"]["attributes"]["folder"], "sources-72")

    def test_full_path_resolves(self):
        _, payload, _ = create("Все тест-кейсы/ALOG/INST")
        self.assertEqual(payload["request"]["body"]["attributes"]["folder"], "alog-inst")

    def test_case_is_ignored(self):
        _, payload, _ = create("корзина")
        self.assertEqual(payload["request"]["body"]["attributes"]["folder"], FOLDER_UUID)

    def test_resolution_is_visible_before_the_write(self):
        # Подстановку нужно видеть в dry-run: иначе кейс уезжает не в ту папку
        # и это выясняется уже после записи.
        _, payload, _ = create("Корзина")
        applied = [row for row in payload["resolvedFields"] if row["field"] == "folder"]
        self.assertEqual(applied[0]["codes"], [FOLDER_UUID])
        self.assertTrue(applied[0]["applied"])
        self.assertTrue(payload["dryRun"])


class Refusals(unittest.TestCase):
    def test_ambiguous_name_lists_candidates_with_codes(self):
        code, payload, _ = create("INST")
        self.assertEqual(code, 1)
        self.assertIn("Все тест-кейсы/ALOG/INST [alog-inst]", payload["error"])
        self.assertIn("Все тест-кейсы/AIOM/INST [aiom-inst]", payload["error"])

    def test_identical_paths_are_still_told_apart_by_code(self):
        # В живом дереве PVM есть две разные папки с одинаковым полным путём.
        # Список одних путей в таком случае нечем уточнить — нужны коды.
        tree = folder("Все тест-кейсы", "root",
                      folder("Корзина", "первая"), folder("Корзина", "вторая"))
        with self.assertRaises(RuntimeError) as ctx:
            tool.match_folder_path(tool.folder_paths(tree), "Корзина")
        self.assertIn("[первая]", str(ctx.exception))
        self.assertIn("[вторая]", str(ctx.exception))

    def test_missing_folder_points_at_the_tree_command(self):
        code, payload, _ = create("Такой папки нет")
        self.assertEqual(code, 1)
        self.assertIn("tms folder-root", payload["error"])


class TreeParsing(unittest.TestCase):
    def test_paths_are_built_from_id_code_and_title(self):
        paths = dict(tool.folder_paths(TREE))
        self.assertEqual(paths["Все тест-кейсы/Корзина"], FOLDER_UUID)
        self.assertEqual(paths["Все тест-кейсы/BUSINESS SERVICES/AUDIT/UI/UI Audit PVM/R7.3/Источники"],
                         "sources-73")

    def test_list_at_the_root_is_accepted(self):
        # Ручка отдаёт то объект, то список корней — обе формы должны разбираться.
        paths = dict(tool.folder_paths([TREE]))
        self.assertIn("Все тест-кейсы/ALOG/INST", paths)


if __name__ == "__main__":
    unittest.main()
