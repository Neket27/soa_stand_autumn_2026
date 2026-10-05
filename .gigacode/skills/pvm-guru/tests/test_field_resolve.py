"""Резолв версий, компонентов и кода продукта: имя человека → значение для трекера.

Три вещи, которые нельзя угадать и которые проверены на живом портале 14.08.2026.

**Код атрибута зависит от типа задачи.** «Fix Version» у `task` и `story` — это
`fix_version`, а у `epic_crpv` — `fix_version_s`. Хардкод одного кода ломается на
соседнем типе, поэтому код выбирается из реальной схемы типа.

**Форма значения зависит от типа поля, а не от того, мультизначное оно.**
Таксономия (код продукта) принимает голый код строкой; ссылочные поля (версия,
компонент) — список объектов `[{"code": …}]`. Список голых строк те же поля
отвергают с HTTP 500.

**Справочник живёт в двух разных местах.** У таксономии значения лежат прямо в
схеме типа задачи, у версий и компонентов — в реестрах пространства, и одно имя
в разных пространствах имеет разные идентификаторы.

Формы данных ниже сняты с портала, а не придуманы.
"""

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import swtr_task_tool as tool  # noqa: E402

TASK_SCHEMA = {
    "code": "task",
    "attributes": [
        {"code": "priority", "name": "Приоритет", "type": "taxonomy", "directoryValues": []},
        {"code": "fix_version", "name": "Fix Version", "type": "version", "directoryValues": []},
        {"code": "affects_version", "name": "Affects Version", "type": "version",
         "directoryValues": []},
        {"code": "component_s", "name": "Component/s", "type": "sber_component",
         "directoryValues": []},
        {"code": "sber_component", "name": "Компонент", "type": "sber_component",
         "directoryValues": []},
        {"code": "customfield_24900", "name": "Код продукта", "type": "taxonomy",
         "directoryValues": [{"code": "281924", "name": "#AM"},
                             {"code": "273841", "name": "OPM"}]},
    ],
}

EPIC_SCHEMA = {
    "code": "epic_crpv",
    "attributes": [
        {"code": "fix_version_s", "name": "Fix Version/s", "type": "version",
         "directoryValues": []},
        {"code": "affects_version_s", "name": "Affects Version/s", "type": "version",
         "directoryValues": []},
        {"code": "component_s", "name": "Component/s", "type": "sber_component",
         "directoryValues": []},
    ],
}

VERSIONS_REGISTRY = [
    {"code": "dd4c1b7e-1111-2222-3333-444444444444", "name": "Platform V Monitor 7.0.40"},
    {"code": "3e8bab72-c57f-4e2c-a149-b4876e5f1d96", "name": "Platform V Dictionaries 2.1.1"},
]

COMPONENTS_REGISTRY = [
    {"code": "20ad2970-4ec2-40b5-befc-97eed0970af3", "name": "COTE"},
    {"code": "77440481-c886-4aad-a14c-fbf151afbc01", "name": "ADMS"},
]


class AttributeCodeComesFromTheSuitSchema(unittest.TestCase):
    def test_fix_version_differs_between_task_and_epic(self):
        self.assertEqual(tool.pick_field_attribute(TASK_SCHEMA, "fix-version")["code"],
                         "fix_version")
        self.assertEqual(tool.pick_field_attribute(EPIC_SCHEMA, "fix-version")["code"],
                         "fix_version_s")

    def test_affects_version_differs_too(self):
        self.assertEqual(tool.pick_field_attribute(TASK_SCHEMA, "affects-version")["code"],
                         "affects_version")
        self.assertEqual(tool.pick_field_attribute(EPIC_SCHEMA, "affects-version")["code"],
                         "affects_version_s")

    def test_component_prefers_the_dedicated_attribute(self):
        """У задачи есть и `component_s`, и `sber_component` — берём проверенный записью."""
        self.assertEqual(tool.pick_field_attribute(TASK_SCHEMA, "component")["code"],
                         "sber_component")
        self.assertEqual(tool.pick_field_attribute(EPIC_SCHEMA, "component")["code"],
                         "component_s")

    def test_missing_field_is_reported_as_absent(self):
        """У эпика CRPV нет кода продукта — это не ошибка резолва, а свойство типа."""
        self.assertIsNone(tool.pick_field_attribute(EPIC_SCHEMA, "product"))

    def test_unknown_field_name_is_a_programming_error(self):
        with self.assertRaises(KeyError):
            tool.pick_field_attribute(TASK_SCHEMA, "нет-такого-поля")


class ValueFormFollowsTheAttributeType(unittest.TestCase):
    def test_taxonomy_takes_a_bare_string_code(self):
        attr = tool.pick_field_attribute(TASK_SCHEMA, "product")
        self.assertEqual(tool.build_attribute_value(attr, ["273841"]), "273841")

    def test_numeric_taxonomy_code_stays_a_string(self):
        """Голый код продукта числом трекер не примет — форма именно строковая."""
        attr = tool.pick_field_attribute(TASK_SCHEMA, "product")
        value = tool.build_attribute_value(attr, [273841])
        self.assertIsInstance(value, str)
        self.assertEqual(value, "273841")

    def test_reference_field_takes_a_list_of_objects(self):
        attr = tool.pick_field_attribute(TASK_SCHEMA, "fix-version")
        self.assertEqual(tool.build_attribute_value(attr, ["dd4c1b7e-1111-2222-3333-444444444444"]),
                         [{"code": "dd4c1b7e-1111-2222-3333-444444444444"}])

    def test_reference_field_keeps_every_value(self):
        attr = tool.pick_field_attribute(TASK_SCHEMA, "component")
        value = tool.build_attribute_value(attr, ["20ad2970", "77440481"])
        self.assertEqual(value, [{"code": "20ad2970"}, {"code": "77440481"}])


class DirectoryLookupIsForgivingButNeverGuesses(unittest.TestCase):
    def test_exact_name_resolves_to_its_code(self):
        self.assertEqual(tool.match_directory_code(VERSIONS_REGISTRY, "Platform V Monitor 7.0.40"),
                         "dd4c1b7e-1111-2222-3333-444444444444")

    def test_lookup_ignores_case_and_surrounding_spaces(self):
        self.assertEqual(tool.match_directory_code(COMPONENTS_REGISTRY, "  cote "),
                         "20ad2970-4ec2-40b5-befc-97eed0970af3")

    def test_an_exact_code_is_accepted_as_is(self):
        """Пользователь мог принести готовый код — не заставлять искать имя."""
        self.assertEqual(
            tool.match_directory_code(COMPONENTS_REGISTRY, "20ad2970-4ec2-40b5-befc-97eed0970af3"),
            "20ad2970-4ec2-40b5-befc-97eed0970af3")

    def test_unknown_name_raises_and_suggests_candidates(self):
        with self.assertRaises(RuntimeError) as ctx:
            tool.match_directory_code(COMPONENTS_REGISTRY, "COTA")
        message = str(ctx.exception)
        self.assertIn("COTA", message)
        self.assertIn("COTE", message, "подсказка должна называть похожее имя из справочника")

    def test_ambiguous_name_refuses_to_pick_the_first(self):
        rows = [{"code": "a-1", "name": "Дубль"}, {"code": "b-2", "name": "дубль"}]
        with self.assertRaises(RuntimeError) as ctx:
            tool.match_directory_code(rows, "Дубль")
        self.assertIn("a-1", str(ctx.exception))
        self.assertIn("b-2", str(ctx.exception))

    def test_empty_directory_says_so_instead_of_failing_silently(self):
        with self.assertRaises(RuntimeError) as ctx:
            tool.match_directory_code([], "COTE")
        self.assertRegex(str(ctx.exception).lower(), r"пуст|нет значений|не заполнен")


if __name__ == "__main__":
    unittest.main()
