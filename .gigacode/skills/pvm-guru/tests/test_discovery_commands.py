"""Разбор ответов discovery-ручек и поведение команды `tql`.

Формы ответов взяты с живого портала 13.08.2026, а не придуманы: у трекера они
разные у каждой ручки — где-то `{"content": [...]}`, где-то голый список, а у
схемы workflow статус лежит в двух концах перехода вместе с категорией.

Команда `tql` обязана отказываться от заведомо неверного запроса ДО сети:
смешение AND и OR бэкенд принимает и молча возвращает не то, поэтому отправлять
такой запрос нельзя.
"""

import argparse
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import swtr_task_tool as tool  # noqa: E402

SPACES_RESPONSE = {
    "content": [
        {"code": "AIHUBCOM", "name": "AI Hub Community",
         "type": {"code": "AC21_new", "name": "AC21 NEW"},
         "createdAt": "2025-06-17T12:55:20.783024Z"},
        {"code": "CRPV", "name": "CRPV", "type": {"code": "AC21_new", "name": "AC21 NEW"}},
    ]
}

FILTERS_RESPONSE = [
    {"code": "favorite-units", "name": "Избранное",
     "description": "Все избранные задачи текущего пользователя"},
    {"code": "my-unit", "name": "Мои задачи",
     "description": "Все задачи назначенные на текущего пользователя"},
]

VERSIONS_RESPONSE = {
    "content": [
        {"id": "98313d34", "code": "fcfab2fe", "space": "CRPV", "name": "14.0.1ise",
         "description": "", "archived": False, "canDeleted": True},
        {"id": "f982e6bc", "code": "66855564", "space": "CRPV", "name": "BackEnd Platform 4.1",
         "description": "", "archived": True, "canDeleted": False},
    ]
}

WORKFLOW_RESPONSE = {
    "wfSchema": {"code": "CRPVPC_XcPQlrMlkjYwf", "name": "CRPV Epic Flow"},
    "wfTransitions": [
        {
            "wfAction": {"code": "backlog", "name": "backlog"},
            "oldStatus": {"code": "ready_to_development", "name": "ready to development",
                          "statusType": {"code": "progress", "name": "В работе"}},
            "destStatus": {"code": "backlog", "name": "backlog",
                           "statusType": {"code": "pause", "name": "Ожидание"}},
        },
        {
            "wfAction": {"code": "init", "name": "Создание"},
            "oldStatus": None,
            "destStatus": {"code": "backlog", "name": "backlog",
                           "statusType": {"code": "pause", "name": "Ожидание"}},
        },
    ],
}


class SpacesAreNormalized(unittest.TestCase):
    def test_every_space_keeps_code_and_name(self):
        rows = tool.normalize_spaces(SPACES_RESPONSE)
        self.assertEqual([r["code"] for r in rows], ["AIHUBCOM", "CRPV"])
        self.assertEqual(rows[0]["name"], "AI Hub Community")

    def test_space_type_is_flattened_to_its_code(self):
        rows = tool.normalize_spaces(SPACES_RESPONSE)
        self.assertEqual(rows[0]["type"], "AC21_new")

    def test_bare_list_response_is_tolerated(self):
        rows = tool.normalize_spaces(SPACES_RESPONSE["content"])
        self.assertEqual(len(rows), 2)


class FiltersAreNormalized(unittest.TestCase):
    def test_bare_list_is_the_live_shape(self):
        rows = tool.normalize_filters(FILTERS_RESPONSE)
        self.assertEqual([r["code"] for r in rows], ["favorite-units", "my-unit"])

    def test_description_is_kept(self):
        rows = tool.normalize_filters(FILTERS_RESPONSE)
        self.assertIn("избранные", rows[0]["description"].lower())


class VersionsAreNormalized(unittest.TestCase):
    def test_names_and_archived_flag_survive(self):
        rows = tool.normalize_versions(VERSIONS_RESPONSE)
        self.assertEqual([r["name"] for r in rows], ["14.0.1ise", "BackEnd Platform 4.1"])
        self.assertEqual([r["archived"] for r in rows], [False, True])


class WorkflowIsNormalized(unittest.TestCase):
    def test_schema_code_is_extracted(self):
        data = tool.normalize_workflow(WORKFLOW_RESPONSE)
        self.assertEqual(data["schema"], "CRPVPC_XcPQlrMlkjYwf")

    def test_statuses_carry_their_category(self):
        data = tool.normalize_workflow(WORKFLOW_RESPONSE)
        by_code = {s["code"]: s for s in data["statuses"]}
        self.assertEqual(by_code["ready_to_development"]["category"], "progress")
        self.assertEqual(by_code["backlog"]["category"], "pause")

    def test_each_status_appears_once(self):
        data = tool.normalize_workflow(WORKFLOW_RESPONSE)
        codes = [s["code"] for s in data["statuses"]]
        self.assertEqual(len(codes), len(set(codes)), "статусы не должны дублироваться")

    def test_transitions_keep_action_and_both_ends(self):
        data = tool.normalize_workflow(WORKFLOW_RESPONSE)
        first = data["transitions"][0]
        self.assertEqual(first["action"], "backlog")
        self.assertEqual(first["from"], "ready_to_development")
        self.assertEqual(first["to"], "backlog")

    def test_creation_transition_has_no_source_status(self):
        """У перехода создания `oldStatus` пуст — это не ошибка разбора."""
        data = tool.normalize_workflow(WORKFLOW_RESPONSE)
        self.assertIsNone(data["transitions"][1]["from"])


class TqlCommandRefusesBadFormBeforeTheNetwork(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self._real_post = tool.post_json

        def explode(*args, **kwargs):
            self.calls.append(args)
            raise AssertionError("запрос не должен уходить в сеть при неверной форме")

        tool.post_json = explode
        self.addCleanup(lambda: setattr(tool, "post_json", self._real_post))

    def _args(self, query, force=False):
        return argparse.Namespace(
            query=query, base_url="https://example.invalid/swtr", timeout=30.0,
            page=0, limit=20, return_attribute=[], force=force,
        )

    def test_mixed_and_or_is_rejected_without_calling_the_api(self):
        code = tool.cmd_tql(self._args('space = "CRPV" AND a = "1" OR b = "2"'))
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, [], "сеть не должна быть тронута")

    def test_grouping_parenthesis_is_rejected_without_calling_the_api(self):
        code = tool.cmd_tql(self._args('space = "CRPV" AND (a = "1" OR b = "2")'))
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, [])

    def test_force_lets_a_suspicious_query_through(self):
        """Аварийный люк: валидатор может ошибиться, запрет должен обходиться явно."""
        sent = {}

        def capture(base_url, endpoint, body, timeout, **kwargs):
            sent["endpoint"] = endpoint
            sent["body"] = body
            return {"content": []}

        tool.post_json = capture
        code = tool.cmd_tql(self._args('a = "1" AND b = "2" OR c = "3"', force=True))
        self.assertEqual(code, 0)
        self.assertEqual(sent["endpoint"], "/rest/api/unit/v3/find/tql")

    def test_valid_query_goes_to_the_tql_endpoint(self):
        sent = {}

        def capture(base_url, endpoint, body, timeout, **kwargs):
            sent["endpoint"] = endpoint
            sent["body"] = body
            return {"content": []}

        tool.post_json = capture
        code = tool.cmd_tql(self._args('space = "CRPV" AND workflow_status not in statusCategory("done")'))
        self.assertEqual(code, 0)
        self.assertEqual(sent["endpoint"], "/rest/api/unit/v3/find/tql")
        self.assertIn("statusCategory", sent["body"]["query"])


if __name__ == "__main__":
    unittest.main()
