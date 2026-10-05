"""Создание тест-кейса: форма записи полей отличается от формы чтения.

Три поля тест-кейса читаются в одной форме, а принимаются в другой, и каждое
расхождение наказывается по-своему: `folder` объектом даёт HTTP 500 без имени
поля, `precondition` голым текстом — тоже 500, а `test_step` в форме чтения
проходит с 200 и молча не сохраняется. Именно из этой связки родился вывод
пользователя «создание тест-кейсов через API не работает». Тесты держат
границы: форма приводится до отправки, чужие типы задач не задеты, а
`--body-file` остаётся нетронутым.
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
import test_steps  # noqa: E402

BASE = ["--base-url", "https://example.invalid/swtr", "--env-file", "/нет/такого"]
FOLDER_UUID = "550c265e-2298-4f19-acfd-9ffda367a1f1"


class Recorder:
    """Подменяет сетевой слой и запоминает каждый вызов."""

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def __call__(self, method, url, headers, *, body=None, timeout=30.0,
                 system=None, max_retries=2, **kwargs):
        self.calls.append({"method": method, "url": url, "body": body})
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
    # Ошибочный путь печатает контракт ошибки в stderr — там же его и читаем.
    return code, json.loads(out.getvalue() or err.getvalue())


def create(*extra, recorder=None):
    """Dry-run создания тест-кейса с обязательными полями и добавкой из теста."""
    recorder = recorder or Recorder()
    return run(recorder, "create", "--suit", "test_case", "--space", "PVM",
               "--summary", "проверка", *extra, *BASE)


def attributes_of(payload):
    return payload["request"]["body"]["attributes"]


class FolderShape(unittest.TestCase):
    def test_object_from_read_becomes_bare_code(self):
        _, payload = create("--attribute", json.dumps({"code": FOLDER_UUID},
                                                      ensure_ascii=False).join(["folder=", ""]))
        self.assertEqual(attributes_of(payload)["folder"], FOLDER_UUID)

    def test_bare_uuid_passes_through_untouched(self):
        _, payload = create("--attribute", f"folder={FOLDER_UUID}")
        self.assertEqual(attributes_of(payload)["folder"], FOLDER_UUID)

    def test_object_without_code_is_refused_by_name(self):
        code, payload = create("--attribute", 'folder={"title": "Корзина"}')
        self.assertEqual(code, 1)
        self.assertIn("folder", payload["error"])
        self.assertFalse(payload["ok"])


class PreconditionShape(unittest.TestCase):
    def test_plain_text_becomes_prosemirror_string(self):
        _, payload = create("--precondition", "Пользователь авторизован")
        value = attributes_of(payload)["precondition"]
        self.assertIsInstance(value, str)
        document = json.loads(value)
        self.assertEqual(document["type"], "doc")
        self.assertEqual(document["content"][0]["content"][0]["text"], "Пользователь авторизован")

    def test_raw_attribute_goes_through_the_same_point(self):
        _, payload = create("--attribute", "precondition=Есть тестовая учётка")
        self.assertIsInstance(attributes_of(payload)["precondition"], str)
        self.assertIn("Есть тестовая учётка", attributes_of(payload)["precondition"])


class StepsShape(unittest.TestCase):
    def test_steps_are_wrapped_into_test_step_list(self):
        _, payload = create("--step", "Открыть вход||Форма логина",
                            "--step", "Ввести креды|user/pass|Дашборд")
        steps = attributes_of(payload)["test_step"]["testStepList"]
        self.assertEqual([s["stepNumber"] for s in steps], [1, 2])
        self.assertEqual(steps[0]["stepDescription"]["plainText"], "Открыть вход")
        self.assertEqual(steps[1]["stepData"]["plainText"], "user/pass")
        self.assertEqual(steps[0]["stepType"], "step_by_step")

    def test_empty_part_is_an_empty_paragraph_not_an_empty_document(self):
        _, payload = create("--step", "Открыть вход||Форма логина")
        step = attributes_of(payload)["test_step"]["testStepList"][0]
        document = json.loads(step["stepData"]["formattedText"])
        self.assertEqual(document["content"][0]["type"], "paragraph")
        self.assertEqual(step["stepData"]["plainText"], "")

    def test_escaped_separator_stays_in_the_text(self):
        _, payload = create("--step", r"Ввести a\|b|данные|результат")
        step = attributes_of(payload)["test_step"]["testStepList"][0]
        self.assertEqual(step["stepDescription"]["plainText"], "Ввести a|b")

    def test_step_without_action_is_refused(self):
        code, payload = create("--step", "|данные|результат")
        self.assertEqual(code, 1)
        self.assertIn("без действия", payload["error"])

    def test_too_many_parts_are_refused_by_name(self):
        code, payload = create("--step", "a|b|c|d")
        self.assertEqual(code, 1)
        self.assertIn("действие|данные|результат", payload["error"])

    def test_steps_file_and_step_together_are_refused(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump([{"description": "шаг"}], handle)
        code, payload = create("--step", "a|b|c", "--steps-file", handle.name)
        self.assertEqual(code, 1)
        self.assertIn("--steps-file", payload["error"])

    def test_steps_file_accepts_plain_objects(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump([{"description": "Открыть вход", "data": "", "result": "Форма"},
                       {"description": "Выйти", "data": "", "result": "Логин"}], handle)
        _, payload = create("--steps-file", handle.name)
        steps = attributes_of(payload)["test_step"]["testStepList"]
        self.assertEqual([s["stepDescription"]["plainText"] for s in steps],
                         ["Открыть вход", "Выйти"])

    def test_object_without_test_step_list_is_refused_not_dropped(self):
        # Словарь без ключа-обёртки — одиночный шаг или опечатка. Молчаливый
        # пустой список здесь означал бы удаление атрибута и кейс без шагов с
        # кодом возврата 0 — ровно тот отказ, против которого написан модуль.
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump({"steps": [{"description": "Открыть", "result": "ок"}]}, handle)
        code, payload = create("--steps-file", handle.name)
        self.assertEqual(code, 1)
        self.assertIn("testStepList", payload["error"])

    def test_single_step_object_is_refused_by_name(self):
        with self.assertRaises(RuntimeError) as ctx:
            test_steps.steps_value({"description": "Открыть", "result": "Форма"})
        self.assertIn("testStepList", str(ctx.exception))

    def test_read_back_shape_is_converted_not_lost(self):
        # Форма чтения на записи проходит с 200 и молча теряется — поэтому она
        # конвертируется. Иначе «прочитал кейс, создал похожий» отдаёт пустой кейс.
        document = json.dumps({"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Шаг из трекера"}]}]})
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump([{"code": "uuid", "stepNumber": 1, "stepType": "step_by_step",
                        "stepDescription": {"text": document},
                        "stepData": {"text": "{}"},
                        "stepResult": {"text": document}}], handle)
        _, payload = create("--steps-file", handle.name)
        step = attributes_of(payload)["test_step"]["testStepList"][0]
        self.assertEqual(step["stepDescription"]["plainText"], "Шаг из трекера")
        self.assertIsNone(step["code"])
        # Пустую часть трекер отдаёт как `{"text": "{}"}`. Она обязана стать
        # пустым параграфом, а не ВИДИМЫМ текстом «{}» в новом кейсе: `{}` —
        # валидный JSON, но не документ, и конвертер разбирал его как markdown.
        self.assertEqual(step["stepData"]["plainText"], "")
        document = json.loads(step["stepData"]["formattedText"])
        self.assertEqual(document["content"][0]["type"], "paragraph")
        self.assertNotIn("{}", step["stepData"]["formattedText"])

    def test_empty_document_from_the_tracker_is_empty_in_every_form(self):
        for empty in ("{}", "", "   ", '{"type": "doc", "content": []}', '{"type": "doc"}'):
            with self.subTest(empty=empty):
                self.assertEqual(test_steps._read_part({"text": empty}), "")

    def test_json_payload_in_step_data_is_text_not_emptiness(self):
        # JSON-нагрузка в тестовых данных — обычное дело для API-кейсов. Правило
        # «словарь без content пуст» шире механизма, который оно чинило, и
        # стирало такие данные молча: 200 и пустая часть шага.
        payload = '{"login": "u", "password": "p"}'
        self.assertEqual(test_steps._read_part({"text": payload}), payload)
        steps = test_steps.steps_value(
            [{"stepDescription": "Отправить POST /login", "stepData": payload,
              "stepResult": "200 OK"}])["testStepList"]
        self.assertEqual(steps[0]["stepData"]["plainText"], payload)

    def test_json_payload_in_the_description_is_not_a_false_refusal(self):
        # Тот же перекос давал ложный отказ «шаг без действия», хотя действие
        # задано — просто выглядит как JSON.
        steps = test_steps.steps_value(
            [{"stepDescription": {"text": '{"login": "u"}'}}])["testStepList"]
        self.assertEqual(steps[0]["stepDescription"]["plainText"], '{"login": "u"}')

    def test_json_array_string_is_text(self):
        self.assertEqual(test_steps._read_part({"text": '[1, 2]'}), '[1, 2]')

    def test_read_shape_part_with_a_typo_key_is_refused(self):
        with self.assertRaises(RuntimeError) as ctx:
            test_steps.steps_value([{"stepDescription": {"txt": "опечатка"}}])
        self.assertIn("txt", str(ctx.exception))

    def test_read_shape_step_without_action_is_refused(self):
        with self.assertRaises(RuntimeError) as ctx:
            test_steps.steps_value([{"stepDescription": {"text": "{}"},
                                     "stepResult": {"text": "{}"}}])
        self.assertIn("stepDescription", str(ctx.exception))

    def test_empty_step_list_does_not_add_the_attribute(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump([], handle)
        _, payload = create("--steps-file", handle.name)
        self.assertNotIn("test_step", attributes_of(payload))

    def test_steps_file_with_broken_json_names_the_flag(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write("не json")
        code, payload = create("--steps-file", handle.name)
        self.assertEqual(code, 1)
        self.assertIn("--steps-file", payload["error"])


class NonTextValues(unittest.TestCase):
    def test_numeric_precondition_is_a_contract_error_not_a_traceback(self):
        # --attribute разбирает `123` как JSON, то есть в int. Раньше он доезжал
        # до .strip() внутри конвертера и рвал процесс трейсбеком мимо контракта
        # ошибки навыка.
        code, payload = create("--attribute", "precondition=123")
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("int", payload["error"])

    def test_boolean_wiki_body_is_refused_the_same_way(self):
        # Тот же конвертер обслуживает тело wiki-страницы: чинится конструкт,
        # а не одно поле.
        code, payload = run(Recorder(), "create", "--suit", "wiki_page", "--space", "PVM",
                            "--summary", "страница", "--attribute", "wiki_page_body=true", *BASE)
        self.assertEqual(code, 1)
        self.assertIn("bool", payload["error"])

    def test_quoted_number_still_works_as_text(self):
        _, payload = create("--attribute", 'precondition="123"')
        self.assertIn("123", attributes_of(payload)["precondition"])


class BodyFileIsUntouched(unittest.TestCase):
    def test_body_file_keeps_the_read_shape_as_is(self):
        # --body-file — escape hatch: полный контроль тела означает и полную
        # ответственность за форму, поэтому нормализация туда не лезет.
        body = {"summary": "как есть", "space": "PVM",
                "attributes": {"folder": {"code": FOLDER_UUID}, "test_step": []}}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(body, handle, ensure_ascii=False)
        _, payload = run(Recorder(), "create", "--suit", "test_case",
                         "--body-file", handle.name, *BASE)
        self.assertEqual(payload["request"]["body"]["attributes"]["folder"], {"code": FOLDER_UUID})


class OtherSuitsAreNotTouched(unittest.TestCase):
    def test_task_without_test_case_fields_is_unchanged(self):
        _, payload = run(Recorder(), "create", "--suit", "task", "--space", "PVM",
                         "--summary", "обычная задача", "--attribute", "priority=major", *BASE)
        self.assertEqual(attributes_of(payload),
                         {"priority": "major", "workflow_status": {"command": "NEW"}})


class Hint570(unittest.TestCase):
    def test_missing_test_case_fields_point_at_the_schema_command(self):
        error = RuntimeError('HTTP 570 for …/test_case/create: {"uiErrorMessage":'
                             '"Не заполнены обязательные поля: \'type_of_testing, test_type\'"}')
        code, payload = create("--attribute", f"folder={FOLDER_UUID}", "--execute",
                               recorder=Recorder(error=error))
        self.assertEqual(code, 1)
        self.assertIn("attributes", payload["hint"])


class StepsModuleUnits(unittest.TestCase):
    def test_split_keeps_backslash_before_other_characters(self):
        self.assertEqual(test_steps.split_step_spec(r"путь C:\temp|данные"),
                         [r"путь C:\temp", "данные"])

    def test_steps_value_is_none_for_empty_input(self):
        self.assertIsNone(test_steps.steps_value([]))
        self.assertIsNone(test_steps.steps_value(None))

    def test_wrapper_from_another_case_is_accepted(self):
        wrapper = test_steps.steps_value([{"description": "шаг", "data": "", "result": "итог"}])
        again = test_steps.steps_value(wrapper)
        self.assertEqual(again["testStepList"][0]["stepDescription"]["plainText"], "шаг")

    def test_unknown_keys_are_refused_with_their_names(self):
        with self.assertRaises(RuntimeError) as ctx:
            test_steps.steps_value([{"desc": "опечатка"}])
        self.assertIn("desc", str(ctx.exception))

    def test_steps_as_a_bare_string_are_refused(self):
        with self.assertRaises(RuntimeError) as ctx:
            test_steps.steps_value("шаг1, шаг2")
        self.assertIn("список", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
