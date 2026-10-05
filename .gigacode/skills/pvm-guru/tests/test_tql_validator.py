"""Локальный валидатор TQL и переход фильтров статуса на statusCategory().

Почему валидатор вообще нужен. Живые проверки 13.08.2026 показали два разных
отказа: скобки бэкенд отвергает явно (`HTTP 400 Не распознан элемент '('`), а вот
смешение AND и OR без скобок проходит с `200` и **неверной** выдачей. Второй
случай молчит, поэтому ловить его должен клиент — иначе агент покажет
пользователю уверенно неправильный ответ.

Тонкость, из-за которой правило «скобок не бывает» неверно: круглые скобки в TQL
законны в двух местах — список `IN ("a", "b")` и вызов функции
`statusCategory("done")`. Запрещена только скобка, открывающая логическую группу.

Про statusCategory: коды статусов уникальны для каждой схемы workflow, поэтому
хардкод `("done","closed","resolved","rejected")` работает в одном пространстве и
молча не находит ничего в другом. Категория — единственная переносимая форма.
"""

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import swtr_task_tool as tool  # noqa: E402


class ValidatorAcceptsLegalQueries(unittest.TestCase):
    def test_pure_and_chain_is_accepted(self):
        self.assertEqual(tool.validate_tql('space = "CRPV" AND priority = "high"'), [])

    def test_pure_or_chain_is_accepted(self):
        self.assertEqual(tool.validate_tql('priority = "high" OR priority = "low"'), [])

    def test_in_list_parentheses_are_not_a_group(self):
        self.assertEqual(tool.validate_tql('space = "CRPV" AND label IN ("a", "b")'), [])

    def test_function_call_parentheses_are_not_a_group(self):
        query = 'space = "CRPV" AND workflow_status NOT IN statusCategory("done")'
        self.assertEqual(tool.validate_tql(query), [])

    def test_nested_function_argument_is_accepted(self):
        query = 'unit IN linkedUnitsOf("unit = \'CRPV-1\'", "Является частью")'
        self.assertEqual(tool.validate_tql(query), [])


class ValidatorRejectsWhatTheBackendCannotDo(unittest.TestCase):
    def test_grouping_parenthesis_is_rejected(self):
        problems = tool.validate_tql('space = "CRPV" AND (priority = "high" OR priority = "low")')
        self.assertTrue(problems, "скобка логической группы должна быть найдена")
        self.assertIn("скобк", " ".join(problems).lower())

    def test_mixed_and_or_is_rejected(self):
        problems = tool.validate_tql('space = "CRPV" AND priority = "high" OR priority = "low"')
        self.assertTrue(problems, "смешение AND и OR должно быть найдено")
        joined = " ".join(problems).upper()
        self.assertIn("AND", joined)
        self.assertIn("OR", joined)

    def test_mixed_and_or_message_explains_the_silent_failure(self):
        """Сообщение обязано объяснить, почему это опаснее скобок: ошибки не будет."""
        problems = tool.validate_tql('a = "1" AND b = "2" OR c = "3"')
        self.assertRegex(" ".join(problems).lower(), r"тих|молч|200|без ошибк")


class ValidatorIgnoresQuotedText(unittest.TestCase):
    """Строковое значение — не синтаксис: скобки и союзы внутри кавычек законны."""

    def test_and_inside_a_quoted_value_is_not_mixing(self):
        self.assertEqual(tool.validate_tql('summary ~ "закупка AND продажа"'), [])

    def test_or_inside_a_quoted_value_does_not_mix_with_outer_and(self):
        query = 'space = "CRPV" AND summary ~ "миграция OR перенос"'
        self.assertEqual(tool.validate_tql(query), [])

    def test_parenthesis_inside_a_quoted_value_is_not_a_group(self):
        self.assertEqual(tool.validate_tql('summary ~ "отчёт (годовой)"'), [])


class AssignedUsesStatusCategory(unittest.TestCase):
    def test_in_progress_asks_for_everything_outside_done(self):
        query = tool.build_assigned_tql(["Savinov.M.Y"], status="in-progress", spaces=[], suits=[])
        self.assertIn('workflow_status not in statusCategory("done")', query)

    def test_in_progress_no_longer_hardcodes_status_codes(self):
        query = tool.build_assigned_tql(["Savinov.M.Y"], status="in-progress", spaces=[], suits=[])
        for hardcoded in ("closed", "rejected"):
            self.assertNotIn(hardcoded, query)

    def test_explicit_status_value_is_still_honoured(self):
        query = tool.build_assigned_tql(["Savinov.M.Y"], status="in_review", spaces=["PVM"], suits=[])
        self.assertIn('workflow_status = "in_review"', query)

    def test_status_all_adds_no_status_condition(self):
        query = tool.build_assigned_tql(["Savinov.M.Y"], status="all", spaces=[], suits=[])
        self.assertNotIn("workflow_status", query)

    def test_query_stays_a_pure_and_chain(self):
        query = tool.build_assigned_tql(["A.B.C"], status="in-progress", spaces=["PVM"], suits=["task"])
        self.assertEqual(tool.validate_tql(query), [])


class DoneWeeklyUsesStatusCategory(unittest.TestCase):
    def test_default_asks_for_the_done_category(self):
        query = tool.build_done_weekly_tql(["Savinov.M.Y"], since="2026-04-30", done_status=[], spaces=[])
        self.assertIn('workflow_status in statusCategory("done")', query)

    def test_explicit_done_status_overrides_the_category(self):
        query = tool.build_done_weekly_tql(
            ["Savinov.M.Y"], since="2026-04-30", done_status=["closed"], spaces=[])
        self.assertIn('workflow_status in ("closed")', query)
        self.assertNotIn("statusCategory", query)

    def test_bare_date_is_lifted_to_datetime(self):
        """Голую дату трекер отвергает для типа date_time — проверено вживую."""
        query = tool.build_done_weekly_tql(["A.B.C"], since="2026-04-30", done_status=[], spaces=[])
        self.assertIn("2026-04-30T00:00:00Z", query)

    def test_query_stays_a_pure_and_chain(self):
        query = tool.build_done_weekly_tql(["A.B.C"], since="2026-04-30", done_status=[], spaces=["PVM"])
        self.assertEqual(tool.validate_tql(query), [])


if __name__ == "__main__":
    unittest.main()
