"""status set: статус — автомат, поэтому цель достигается маршрутом, а не записью.

Разбор живого отказа 11.08.2026 на STS-452231: скрипт писал `workflow_status`
значением и получал HTTP 500, потому что RESOLVED был недостижим из текущего
статуса за один переход. Здесь закреплено то, что должно делать это невозможным:
поиск кратчайшего маршрута по графу схемы, резолв цели по имени или коду и
разделение «действия нет» от «действие закрыто условием».

Граф — реальный BUG APPSEC v.2.1 (space STS), коды действий подлинные: они
генерируются для каждой схемы, и тест обязан работать с такими, а не с «start».
"""

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import swtr_task_tool as tool  # noqa: E402

OPEN = "PN_xySDTWtJOhUePFpLX"
ANALYSIS = "PRBLMN_ZghEqKJlAzmUx"
PROGRESS = "NPRGRS_pCjkadCKRetgB"
RESOLVED = "RSLVD_CSFoarAUZriILD"
CANCELLED = "CNCLLD_KdSyKcQZDXagZ"
CLOSED = "CLSD_fcbOheeIXkmKDVj"

A_OPEN = "TKRT_VbRDwmYQyHDBEWZ"        # Открыто
A_ANALYSIS = "NLZPR_CJROuwRnxFnHen"    # Анализ проблемы
A_DONE = "ZVRSHT_lMacNZCcxwjcw"        # Завершить
A_CANCEL = "TMNT_atJRHPFPzxvgDNw"      # Отменить
A_CLOSE = "ZKRT_kWtnHVNuPNYZBcd"       # Закрыть
A_PROGRESS = "VRBT_QZekaaVNwYdLkkW"    # В работе

NAMES = {
    OPEN: "OPEN", ANALYSIS: "PROBLEM ANALYSIS", PROGRESS: "IN PROGRESS",
    RESOLVED: "RESOLVED", CANCELLED: "CANCELLED", CLOSED: "CLOSED",
}
ACTION_NAMES = {
    A_OPEN: "Открыто", A_ANALYSIS: "Анализ проблемы", A_DONE: "Завершить",
    A_CANCEL: "Отменить", A_CLOSE: "Закрыть", A_PROGRESS: "В работе",
}


def transition(action, old, dest):
    """Ребро графа в форме ответа /rest/api/workflow/v1/workflow/{schema}."""
    node = None if old is None else {"code": old, "name": NAMES[old]}
    return {
        "wfAction": {"code": action, "name": ACTION_NAMES.get(action, action)},
        "oldStatus": node,
        "destStatus": {"code": dest, "name": NAMES[dest]},
    }


# Тот же граф, что отдаёт трекер: 25 переходов, включая псевдопереход создания.
GRAPH = [
    transition("initAction", None, OPEN),
    transition(A_ANALYSIS, OPEN, ANALYSIS),
    transition(A_CANCEL, OPEN, CANCELLED),
    transition(A_PROGRESS, ANALYSIS, PROGRESS),
    transition(A_DONE, ANALYSIS, RESOLVED),
    transition(A_CANCEL, ANALYSIS, CANCELLED),
    transition(A_DONE, PROGRESS, RESOLVED),
    transition(A_CANCEL, PROGRESS, CANCELLED),
    transition(A_CLOSE, RESOLVED, CLOSED),
    transition(A_ANALYSIS, RESOLVED, ANALYSIS),
    transition(A_CANCEL, RESOLVED, CANCELLED),
    transition(A_OPEN, CANCELLED, OPEN),
    transition(A_OPEN, CLOSED, OPEN),
]


class ShortestPath(unittest.TestCase):
    def test_multi_hop_route_out_of_a_terminal_status(self):
        """CANCELLED -> RESOLVED: тот самый случай, где запись значением давала 500."""
        path = tool.shortest_status_path(GRAPH, CANCELLED, RESOLVED)
        self.assertEqual([step["actionCode"] for step in path], [A_OPEN, A_ANALYSIS, A_DONE])
        self.assertEqual([step["toName"] for step in path], ["OPEN", "PROBLEM ANALYSIS", "RESOLVED"])
        # Каждый шаг стартует там, где закончился предыдущий — иначе маршрут
        # развалится на исполнении.
        self.assertEqual(path[0]["from"], CANCELLED)
        for previous, following in zip(path, path[1:]):
            self.assertEqual(previous["to"], following["from"])

    def test_single_hop_when_target_is_adjacent(self):
        path = tool.shortest_status_path(GRAPH, ANALYSIS, RESOLVED)
        self.assertEqual([step["actionCode"] for step in path], [A_DONE])

    def test_start_equals_target_is_an_empty_route(self):
        self.assertEqual(tool.shortest_status_path(GRAPH, RESOLVED, RESOLVED), [])

    def test_init_action_is_not_a_usable_edge(self):
        """initAction ведёт в OPEN, но существующий юнит им двигать нельзя."""
        edges = tool.build_status_edges(GRAPH)
        self.assertNotIn(None, edges)
        self.assertTrue(all(tool.INIT_ACTION_CODE != code
                            for rows in edges.values() for _, code, _ in rows))

    def test_unreachable_target_is_none_not_an_empty_route(self):
        """Пустой список значит «уже на месте», None — «маршрута нет». Разные вещи."""
        one_way = [transition(A_CANCEL, OPEN, CANCELLED)]
        self.assertIsNone(tool.shortest_status_path(one_way, CANCELLED, OPEN))

    def test_max_hops_cuts_off_a_long_route(self):
        self.assertIsNone(tool.shortest_status_path(GRAPH, CANCELLED, RESOLVED, max_hops=2))
        self.assertIsNotNone(tool.shortest_status_path(GRAPH, CANCELLED, RESOLVED, max_hops=3))

    def test_route_is_stable_across_runs(self):
        """Из двух маршрутов равной длины всегда выбирается один и тот же."""
        first = tool.shortest_status_path(GRAPH, CANCELLED, RESOLVED)
        for _ in range(5):
            self.assertEqual(tool.shortest_status_path(GRAPH, CANCELLED, RESOLVED), first)


class ResolveTarget(unittest.TestCase):
    def test_status_name_is_case_insensitive(self):
        for wanted in ("RESOLVED", "resolved", "  Resolved "):
            self.assertEqual(tool.resolve_status_code(GRAPH, wanted), [RESOLVED])

    def test_status_code_matches_exactly(self):
        self.assertEqual(tool.resolve_status_code(GRAPH, RESOLVED), [RESOLVED])

    def test_unknown_status_resolves_to_nothing(self):
        self.assertEqual(tool.resolve_status_code(GRAPH, "DONE"), [])

    def test_catalog_covers_statuses_reachable_only_as_a_source(self):
        catalog = tool.status_catalog(GRAPH)
        self.assertEqual(catalog[CLOSED], "CLOSED")
        self.assertEqual(len(catalog), 6)

    def test_available_action_matches_by_code_and_by_name(self):
        actions = [{"code": A_OPEN, "name": "Открыто"}, {"code": A_CANCEL, "name": "Отменить"}]
        self.assertEqual(tool.resolve_available_action(actions, A_OPEN)[0]["code"], A_OPEN)
        self.assertEqual(tool.resolve_available_action(actions, "отменить")[0]["code"], A_CANCEL)
        self.assertEqual(tool.resolve_available_action(actions, "Завершить"), [])


class StateSummary(unittest.TestCase):
    def test_blocked_actions_stay_separate_from_available_ones(self):
        """Закрытое условием действие есть в схеме — это не то же, что его отсутствие."""
        summary = tool.summarize_status_state({
            "currentStatus": {"code": ANALYSIS, "name": "PROBLEM ANALYSIS",
                              "statusType": {"code": "progress", "name": "В работе"}},
            "wfSchemaDto": {"code": "BGPPSC_zCeicQqufMIam", "name": "BUG APPSEC v.2.1"},
            "availableActions": [{"code": A_DONE, "name": "Завершить"}],
            "actionsExcludedByCondition": [{"code": A_CANCEL, "name": "Отменить"}],
        })
        self.assertEqual(summary["currentStatus"], {"code": ANALYSIS, "name": "PROBLEM ANALYSIS"})
        self.assertEqual(summary["statusType"], "В работе")
        self.assertEqual([a["code"] for a in summary["availableActions"]], [A_DONE])
        self.assertEqual([a["code"] for a in summary["actionsExcludedByCondition"]], [A_CANCEL])

    def test_missing_workflow_block_does_not_crash(self):
        summary = tool.summarize_status_state({})
        self.assertEqual(summary["currentStatus"], {"code": None, "name": None})
        self.assertEqual(summary["availableActions"], [])


class ChangeHint(unittest.TestCase):
    """570 и 500 — разные ошибки, и подсказка обязана их различать (см. модуль)."""

    def test_570_points_at_the_transition(self):
        hint = tool.status_change_hint(RuntimeError("HTTP 570 for …: валидатор"))
        self.assertIn("availableActions", hint)

    def test_500_points_at_the_wrong_endpoint(self):
        hint = tool.status_change_hint(RuntimeError("HTTP 500 for …: Что-то пошло не так"))
        self.assertIn(tool.STATUS_CHANGE_ENDPOINT, hint)

    def test_other_errors_get_no_invented_explanation(self):
        self.assertIsNone(tool.status_change_hint(RuntimeError("HTTP 401 for …")))


if __name__ == "__main__":
    unittest.main()
