#!/usr/bin/env python3
"""High-level SWTR Task Tracker CLI.

Subcommands:
  get          - fetch one task/wiki page by unit code, SWTR URL, or title-like text
  search       - search tasks and wiki pages by words in text/properties/selected attributes
  count        - count units matching OR-property filter (summary/description/code) via /v3/count/composite
  similar      - find tasks and wiki pages similar to a given task or text
  create       - prepare or execute creation of a task in a SWTR space/suit
  status       - read a unit's workflow state, or move it to a target status by transitions
  tms          - read-only Test Management System scenarios (test plans, cycles, cases, folders, runs)
  assigned     - tasks assigned to one or more users with optional status filter
  done-weekly  - tasks completed by one or more users in the last N days

All commands print a JSON object to stdout. The CLI does not create intermediate
files in `docs/ai/`; the agent decides when to persist a final report (for
example, a completed document review) using its own file-writing tools.

Values the user did not name may come from the profile
(~/.config/pvm-guru/profile.json): the login for --me, the spaces of
search/count/similar/assigned/done-weekly, the space of create/suits/versions/
directory/workflow, the suit/product/components of create. Explicit flags and
the tracker's own environment variables always win; `--no-profile` ignores the
file; every applied default is listed in the `defaults_applied` field of the
result. Without a profile every command behaves exactly as before.

Authorization secrets are never printed: HTTP errors are redacted and request
metadata is logged via `safe_headers_for_log`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import os
import re
import sys
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import test_steps  # noqa: E402
import wiki_body  # noqa: E402

from _swtr_common import (  # noqa: E402
    PROFILE_PATH_HINT,
    DefaultsApplied,
    args_error_context,
    args_with_defaults,
    attach_profile,
    base_url_for,
    build_headers,
    emit_error,
    extract_unit_code,
    is_auth_error,
    load_env_file,
    normalize_text,
    profile_get,
    quote_path_part,
    redact_secrets,
    request_http,
    safe_headers_for_log,
    safe_response_headers,
    significant_terms,
    unit_url,
    unit_urls_in,
)

STOPWORDS = {
    "для", "или", "при", "как", "что", "это", "над", "под", "надо", "нужно", "будет", "есть",
    "через", "если", "чтобы", "после", "перед", "the", "and", "for", "with", "from", "this", "that",
    "реализовать", "доработка", "необходимо", "требование", "задача", "описание", "решение",
}
COMMON_ATTRIBUTE_CODES = [
    "source", "business_block", "solution", "reporter", "label", "labels", "component", "components",
    "product", "products", "theme", "topic", "direction", "group", "epic_name", "customer", "status",
    "priority", "requirement", "requirements", "expected_date", "target_date", "release", "version",
]
DEFAULT_RETURN_ATTRIBUTES = [
    "source", "business_block", "solution", "reporter", "status", "priority", "component", "label",
    "expected_date", "updatedAt", "createdAt",
]

TMS_BASE = "/extension/plugin/v2/rest/api/swtr_tms_plugin/v1"
TMS_DEFAULT_PLAN_ATTRIBUTES = [
    "test_plan_status", "release", "owner", "plan_date_start", "plan_date_end",
    "type_of_testing", "folder",
]
TMS_DEFAULT_CYCLE_ATTRIBUTES = [
    "test_cycle_status", "plan_date_end", "plan_date_start", "release", "owner",
    "iteration_type", "cycles_number", "type_of_testing", "test_case_test_type",
    "old_jira_key", "folder",
]
TMS_DEFAULT_CASE_ATTRIBUTES = [
    "status", "test_case_test_type", "priority", "owner", "labels", "folder",
]
ASSIGNEE_ATTRIBUTE_CODE = "assigned_to"
WORKFLOW_STATUS_ATTRIBUTE_CODE = "workflow_status"
# Тело wiki-страницы. Код атрибута фиксирован платформой, тип — `wiki_page_wisiwig`,
# а значение принимается ТОЛЬКО строкой с JSON ProseMirror внутри: объект даёт
# HTTP 500, голый текст без разметки — тоже. Подробности — references/wiki-model.md.
WIKI_BODY_ATTRIBUTE_CODE = "wiki_page_body"
WIKI_SUIT_CODE = "wiki_page"
# Тест-кейс. Коды атрибутов фиксированы платформой; формы записи проверены
# записью 18.08.2026 и описаны в references/test-management-system.md.
TEST_CASE_SUIT_CODE = "test_case"
FOLDER_ATTRIBUTE_CODE = "folder"
PRECONDITION_ATTRIBUTE_CODE = "precondition"
TEST_STEP_ATTRIBUTE_CODE = "test_step"
# Иерархия страниц: ИСТОЧНИК — родитель, НАЗНАЧЕНИЕ — ребёнок. Обратное направление
# трекер отвергает HTTP 570 «связь уже существует»: родитель у страницы один.
WIKI_HIERARCHY_LINK_TYPE = "wiki_page_hierarchy"
LINK_CREATE_ENDPOINT = "/rest/api/unit/v1/link"
LINK_FIND_ENDPOINT = "/rest/api/unit/v1/link/find"
# Резолв исполнителя. Атрибут `assigned_to` имеет тип `user`, и фильтр по нему
# сравнивает НЕ логин, а `externalId` учётки, причём с учётом регистра:
# `assigned_to in ("savinov.m.y")` находит ноль задач, `("Savinov.M.Y")` — все.
# Этот endpoint принимает телом голый JSON-массив логинов (нижний регистр) и
# возвращает пользователей, где `code` и есть нужный `externalId`.
USER_FIND_ENDPOINT = "/rest/api/user/v1/find"
ASSIGNED_RETURN_ATTRIBUTES = [
    "assigned_to", "workflow_status", "status", "priority", "reporter", "component",
    "label", "expected_date", "updatedAt", "createdAt",
]


# Адрес стенда для веб-ссылок. Ставится в main() после разбора аргументов и
# загрузки env: до этого момента ссылку строить не из чего.
LINK_BASE_URL: Optional[str] = None


def emit(data: Any) -> None:
    """Print a JSON document to stdout, sorted keys for stable output.

    Заодно кладёт карту `urls` — «код юнита → веб-ссылка». Команд, отдающих
    коды, больше десятка, и перечислять их поимённо значит забыть следующую;
    здесь же правило действует на любой вывод. Готовую карту команды не трогаем.
    """
    if isinstance(data, dict) and LINK_BASE_URL and "urls" not in data:
        urls = unit_urls_in(LINK_BASE_URL, data)
        if urls:
            data = dict(data, urls=urls)
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def post_json(base_url: str, endpoint: str, body: Dict[str, Any], timeout: float) -> Any:
    headers = build_headers("task_tracker", require_auth=True)
    url = base_url.rstrip("/") + endpoint
    payload, _, _ = request_http("POST", url, headers, body=body, timeout=timeout, system="task_tracker")
    return payload


def get_json(base_url: str, endpoint: str, timeout: float) -> Any:
    headers = build_headers("task_tracker", require_auth=True)
    url = base_url.rstrip("/") + endpoint
    payload, _, _ = request_http("GET", url, headers, timeout=timeout, system="task_tracker")
    return payload


def build_property_filters(query: str) -> List[Dict[str, Any]]:
    if not (query or "").strip():
        return []
    return [
        {"property": "code", "operator": "like", "value": query},
        {"property": "summary", "operator": "like", "value": query},
        {"property": "description", "operator": "like", "value": query},
    ]


def build_attribute_filters(query: str, attribute_codes: Sequence[str]) -> List[Dict[str, Any]]:
    filters = []
    for code in attribute_codes:
        code = code.strip()
        if code:
            filters.append({"attribute": code, "operator": "like", "value": query})
    return filters


def build_find_body(
    *,
    operator: str,
    property_filters: Sequence[Dict[str, Any]],
    attribute_filters: Sequence[Dict[str, Any]],
    spaces: Sequence[str],
    suits: Sequence[str],
    return_attributes: Sequence[str],
    page: int,
    size: int,
    sort_attribute: str,
    sort_direction: str,
    properties_first: bool,
) -> Dict[str, Any]:
    """Assemble a POST /rest/api/unit/v3/find body. ``operator`` selects the OR/AND
    grouping applied to both property and attribute filters. ``properties_first``
    reproduces a caller's exact filter-key insertion order (search: properties ->
    spaces -> suits; the AND/spaces-first order: spaces -> suits -> properties) so
    the emitted request body is byte-identical.

    Единственный сегодняшний вызывающий — ``search_units``. Фильтр по атрибуту типа
    ``user`` (``assigned_to``) собирать этим телом нельзя: обычный ``find`` по нему
    не матчит ничего и молча отдаёт пустую выдачу, для таких условий есть
    ``find_units_tql``.
    """
    filters: Dict[str, Any] = {}
    if properties_first and property_filters:
        filters["properties"] = {operator: list(property_filters)}
    if spaces:
        filters["spaces"] = list(spaces)
    if suits:
        filters["suits"] = list(suits)
    if not properties_first and property_filters:
        filters["properties"] = {operator: list(property_filters)}
    if attribute_filters:
        filters["attributes"] = {operator: list(attribute_filters)}
    return {
        "filters": filters,
        "attributes": list(dict.fromkeys(return_attributes)),
        "page": {"page": page, "size": size},
        "sort": {"attribute": [sort_attribute], "direction": sort_direction},
    }


def search_units(
    base_url: str,
    query: str,
    *,
    spaces: Sequence[str],
    suits: Sequence[str],
    attribute_codes: Sequence[str],
    return_attributes: Sequence[str],
    page: int,
    size: int,
    timeout: float,
) -> Any:
    body = build_find_body(
        operator="or",
        property_filters=build_property_filters(query),
        attribute_filters=build_attribute_filters(query, attribute_codes),
        spaces=spaces,
        suits=suits,
        return_attributes=return_attributes,
        page=page,
        size=size,
        sort_attribute="updatedAt",
        sort_direction="DESC",
        properties_first=True,
    )
    return post_json(base_url, "/rest/api/unit/v3/find", body, timeout)


def search_wiki(
    base_url: str,
    query: str,
    *,
    spaces: Sequence[str],
    units: Sequence[str],
    page: int,
    size: int,
    timeout: float,
) -> Any:
    filters: Dict[str, Any] = {"query": query}
    if spaces:
        filters["spaces"] = list(spaces)
    if units:
        filters["units"] = list(units)
    body = {"filters": filters, "page": {"page": page, "size": size}}
    return post_json(
        base_url,
        "/extension/plugin/v2/rest/api/swtr_wiki_plugin/v1/wiki/unit/find",
        body,
        timeout,
    )


def count_composite(
    base_url: str,
    query: str,
    *,
    spaces: Sequence[str],
    suits: Sequence[str],
    extra_property_or: Sequence[Dict[str, Any]],
    extra_property_and: Sequence[Dict[str, Any]],
    tql_filter_ids: Sequence[str],
    timeout: float,
) -> Dict[str, Any]:
    """Call POST /rest/api/unit/v3/count/composite with OR over code/summary/description.

    Returns a dict {"request": body, "response": api_response, "matched": optional_int}.
    """
    or_filters = list(build_property_filters(query)) + list(extra_property_or)
    properties: Dict[str, Any] = {}
    if or_filters:
        properties["or"] = or_filters
    if extra_property_and:
        properties["and"] = list(extra_property_and)
    filters: Dict[str, Any] = {}
    if properties:
        filters["properties"] = properties
    if spaces:
        filters["spaces"] = list(spaces)
    if suits:
        filters["suits"] = list(suits)
    body = {"filters": filters, "tqlFilterIds": list(tql_filter_ids)}
    response = post_json(base_url, "/rest/api/unit/v3/count/composite", body, timeout)
    matched: Optional[int] = None
    if isinstance(response, dict):
        for key in ("count", "total", "totalCount", "matched", "totalElements", "numberOfElements"):
            value = response.get(key)
            if isinstance(value, int):
                matched = value
                break
    elif isinstance(response, int):
        matched = response
    return {"request": body, "response": response, "matched": matched}


def get_unit(base_url: str, code: str, timeout: float) -> Dict[str, Any]:
    encoded = quote_path_part(code)
    payload = get_json(base_url, f"/rest/api/unit/v2/{encoded}", timeout)
    return payload if isinstance(payload, dict) else {"raw": payload}


def get_wiki_payloads(base_url: str, code: str, timeout: float) -> Dict[str, Any]:
    encoded = quote_path_part(code)
    result: Dict[str, Any] = {}
    for name, endpoint in [
        ("wiki-v2", f"/extension/plugin/v2/rest/api/swtr_wiki_plugin/v2/wiki/unit/{encoded}"),
        ("wiki-v1", f"/extension/plugin/v2/rest/api/swtr_wiki_plugin/v1/wiki/unit/{encoded}"),
    ]:
        try:
            result[name] = get_json(base_url, endpoint, timeout)
        except RuntimeError as exc:
            result[name + ".error"] = redact_secrets(str(exc))
    return result


def unit_result_key(item: Dict[str, Any]) -> str:
    unit = item.get("unit") if isinstance(item.get("unit"), dict) else item
    return str(unit.get("code") or "")


def summarize_unit_item(item: Dict[str, Any]) -> Dict[str, Any]:
    unit = item.get("unit") if isinstance(item.get("unit"), dict) else item
    attrs = item.get("attributes") or unit.get("attributes") or []
    attr_map: Dict[str, str] = {}
    if isinstance(attrs, list):
        for attr in attrs:
            if not isinstance(attr, dict):
                continue
            info = attr.get("attribute") if isinstance(attr.get("attribute"), dict) else attr
            code = info.get("code") or attr.get("code")
            if code:
                attr_map[code] = normalize_text(attr.get("value"))
    return {
        "code": normalize_text(unit.get("code")),
        "summary": normalize_text(unit.get("summary")),
        "description": normalize_text(unit.get("description"))[:500],
        "space": normalize_text(unit.get("space")),
        "suit": normalize_text(unit.get("suit")),
        "createdAt": normalize_text(unit.get("createdAt")),
        "updatedAt": normalize_text(unit.get("updatedAt")),
        "attributes": attr_map,
    }


def page_content(payload: Any) -> List[Any]:
    if isinstance(payload, dict):
        content = payload.get("content") or payload.get("data") or payload.get("items") or []
        if isinstance(content, list):
            return content
    if isinstance(payload, list):
        return payload
    return []


def collect_text_from_unit(unit: Dict[str, Any]) -> str:
    parts = [normalize_text(unit.get("code")), normalize_text(unit.get("summary")), normalize_text(unit.get("description"))]
    for attr in unit.get("attributes") or []:
        if isinstance(attr, dict):
            parts.append(normalize_text(attr.get("name") or attr.get("code")))
            parts.append(normalize_text(attr.get("value")))
    return " ".join(part for part in parts if part)


def score_item(item: Dict[str, Any], terms: Sequence[str], source_code: Optional[str]) -> int:
    summary = summarize_unit_item(item)
    haystack = " ".join(
        [summary.get("code", ""), summary.get("summary", ""), summary.get("description", "")]
        + list(summary.get("attributes", {}).values())
    ).lower()
    score = sum(1 for term in terms if term.lower() in haystack)
    if source_code and summary.get("code") == source_code:
        score = -999
    return score


# Профиль пользователя (~/.config/pvm-guru/profile.json) закрывает пропуски там,
# где значение не названо: логин для --me, пространства выборок, основное
# пространство операций над пространством, тип/продукт/компоненты при создании.
# Читается одной точкой в main() и лежит в args.profile; --no-profile кладёт туда
# None, и всё ведёт себя как без профиля — так «найди везде» и «без профиля»
# исполняются одним способом. Порядок всегда явное → (окружение) → профиль →
# прежнее поведение, и каждое применённое умолчание записывается в
# args.defaults_applied: источник значения виден в JSON структурно, а не по
# памяти агента. Пустой список не печатается.
# Команды над одним пространством и имя аргумента, которым argparse назовёт его
# отсутствие, когда пространства нет ни во флагах, ни в профиле.
SINGLE_SPACE_COMMANDS = {"suits": "space", "versions": "space", "directory": "--space", "workflow": "--space"}
# Область выборки, взятая из профиля: строку о ней даёт сам скрипт, а не память
# агента (REQ-016). Непустая выдача называет область, пустая добавляет, что в ней
# ничего нет; подсказка про «--no-profile» нужна в обоих случаях. Слова «ничего не
# найдено в пространствах профиля» и «--no-profile» — часть контракта с навыком.
EMPTY_IN_PROFILE_SPACES_NOTE = (
    "Ничего не найдено в пространствах профиля ({spaces}) — чтобы искать везде, повтори с --no-profile "
    "или назови пространство явно (--space).")
PROFILE_SPACES_NOTE = (
    "Область выборки — пространства профиля: {spaces}. Чтобы искать везде, повтори с --no-profile "
    "или назови пространство явно (--space).")
# Эндпойнт выборки по TQL. Одна константа, а не литерал в шести местах: её
# же читает guru_profile.suggest, чтобы печатать в отчёте ровно тот адрес,
# по которому ушёл запрос.
TQL_FIND_ENDPOINT = "/rest/api/unit/v3/find/tql"
LOGIN_SOURCE_NOTES = {
    "env": "Логин {login} взят из переменной окружения SWTR_LOGIN.",
    "profile": "Логин {login} взят из профиля ({path}); без профиля — повтори с --no-profile или передай --user.",
    "os_user": ("Логин {login} взят из имени учётной записи ОС (USER). Если в трекере логин другой — "
                "задай SWTR_LOGIN, заполни профиль (/guru-start) или передай --user."),
    "credential": "Логин {login} взят из логина креда SWTR_BASIC_AUTH.",
}


def profile_spaces(profile: Any) -> List[str]:
    """Пространства выборки из профиля: основное первым, затем «в поле зрения», без дублей.

    Значения приходят проверенными: load_profile отдаёт `ok` только тому файлу,
    который прошёл схему, поэтому список здесь — список строк, а не «что угодно
    из JSON». isinstance остаётся ровно как защита от прямого вызова с чужим
    объектом — падать TypeError'ом наружу этой функции нельзя.
    """
    in_view = profile_get(profile, "task_tracker.spaces")
    if not isinstance(in_view, list):
        in_view = [in_view] if isinstance(in_view, str) else []
    spaces: List[str] = []
    for raw in [profile_get(profile, "task_tracker.primary_space")] + in_view:
        code = str(raw or "").strip()
        if code and code not in spaces:
            spaces.append(code)
    return spaces


def _resolve_scope_spaces(args: argparse.Namespace) -> List[str]:
    """Пространства выборки: явный --space, иначе пространства профиля.

    Явный --space всегда сильнее. Без него берутся пространства профиля, и это
    записывается в defaults_applied; пустой профиль оставляет выборку без
    ограничения по пространству — как до появления профиля. Признак «взяты из
    профиля» отдельно не возвращается: он и есть запись `spaces` в накопителе.
    """
    explicit = [str(code).strip() for code in (getattr(args, "space", None) or []) if str(code).strip()]
    if explicit:
        return explicit
    spaces = profile_spaces(args.profile)
    if spaces:
        args.defaults_applied.add("spaces", list(spaces), "profile")
    return spaces


def _profile_scope(applied: DefaultsApplied) -> Optional[List[str]]:
    """Пространства, взятые из профиля для этой выборки, или None, если область не оттуда.

    Единственный источник признака — сам накопитель: отдельный флаг рядом с ним
    хранил бы то же состояние во второй раз и расходился бы с ним при правках.
    """
    for entry in applied.as_payload():
        if entry["key"] == "spaces" and entry["source"] == "profile":
            return list(entry["value"])
    return None


def _scope_note(applied: DefaultsApplied, *, empty: bool) -> List[str]:
    """Одна строка о происхождении области выборки — общая для всех выборок.

    Разница между «искал в пространствах профиля» и «ничего не нашёл в них»
    только в тексте: подсказка про --no-profile нужна в обоих случаях, и оба
    берут состав области из одного места (REQ-016).
    """
    spaces = _profile_scope(applied)
    if spaces is None:
        return []
    template = EMPTY_IN_PROFILE_SPACES_NOTE if empty else PROFILE_SPACES_NOTE
    return [template.format(spaces=", ".join(spaces))]


def _origin_notes(applied: DefaultsApplied) -> List[str]:
    """Человекочитаемые строки к записям defaults_applied — для результатов, у которых notes уже есть.

    Источник приходит из resolve_default, который знает и `explicit` (в накопитель
    он не попадает), и любой источник, который добавят позже. Записи без шаблона
    пропускаются: строкой к сведению меньше — не повод уронить всю выдачу
    `assigned`/`done-weekly` KeyError'ом, а сама запись в defaults_applied уже
    называет источник.
    """
    notes: List[str] = []
    for entry in applied.as_payload():
        if entry["key"] != "login":
            continue
        template = LOGIN_SOURCE_NOTES.get(entry["source"])
        if template is not None:
            notes.append(template.format(login=entry["value"], path=PROFILE_PATH_HINT))
    return notes + _scope_note(applied, empty=False)


def _apply_profile_space(args: argparse.Namespace) -> None:
    """Команды над одним пространством: явное значение, иначе primary_space профиля.

    Нет ни там, ни там — отказ словами самого argparse и его кодом 2, через
    сабпарсер команды: пользователь без профиля не должен заметить, что
    аргумент перестал быть обязательным.
    """
    name = SINGLE_SPACE_COMMANDS.get(getattr(args, "command", None))
    if name is None or args.space:
        return
    space = profile_get(args.profile, "task_tracker.primary_space")
    if space:
        args.space = str(space).strip()
        args.defaults_applied.add("space", args.space, "profile")
        return
    args.subparser.error("the following arguments are required: %s" % name)


def cmd_get(args: argparse.Namespace) -> int:
    code = extract_unit_code(args.target)
    if code is None:
        unit_payload = search_units(
            args.base_url,
            args.target,
            spaces=args.space,
            suits=args.suit,
            attribute_codes=args.attribute_code,
            return_attributes=args.return_attribute or DEFAULT_RETURN_ATTRIBUTES,
            page=0,
            size=args.limit,
            timeout=args.timeout,
        )
        wiki_payload = search_wiki(
            args.base_url,
            args.target,
            spaces=args.space,
            units=[],
            page=0,
            size=args.limit,
            timeout=args.timeout,
        )
        emit({
            "command": "get",
            "mode": "lookup",
            "query": args.target,
            "units": unit_payload,
            "wiki": wiki_payload,
            # Ссылки отдельной картой: сырой ответ трекера своими полями не дополняем.
            "urls": unit_urls_in(args.base_url, unit_payload),
        })
        return 0

    unit = get_unit(args.base_url, code, args.timeout)
    wiki = {} if args.skip_wiki else get_wiki_payloads(args.base_url, code, args.timeout)
    emit({
        "command": "get",
        "mode": "code",
        "code": code,
        "url": unit_url(args.base_url, code),
        "summary": summarize_unit_item(unit),
        "unit": unit,
        "wiki": wiki,
    })
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    if not (args.query or "").strip():
        raise SystemExit("search requires a non-empty query. Pass meaningful keywords.")
    attr_codes = list(args.attribute_code)
    if args.common_attributes:
        attr_codes = list(dict.fromkeys(attr_codes + COMMON_ATTRIBUTE_CODES))
    # Вики идёт через search_wiki, где фильтр ставится только при непустом списке:
    # пустой профиль оставляет поиск по всем пространствам.
    spaces = _resolve_scope_spaces(args)
    unit_payload = search_units(
        args.base_url,
        args.query,
        spaces=spaces,
        suits=args.suit,
        attribute_codes=attr_codes,
        return_attributes=args.return_attribute,
        page=args.page,
        size=args.limit,
        timeout=args.timeout,
    )
    wiki_payload = None
    if not args.no_wiki:
        wiki_payload = search_wiki(
            args.base_url,
            args.query,
            spaces=spaces,
            units=[],
            page=args.page,
            size=args.limit,
            timeout=args.timeout,
        )
    payload: Dict[str, Any] = {
        "command": "search",
        "query": args.query,
        "spaces": list(spaces),
        "suits": list(args.suit),
        "attributeCodes": attr_codes,
        "units": unit_payload,
        "wiki": wiki_payload,
    }
    empty = not page_content(unit_payload) and not page_content(wiki_payload)
    notes = _scope_note(args.defaults_applied, empty=empty)
    if notes:
        payload["notes"] = notes
    emit(args_with_defaults(payload, args))
    return 0


def cmd_count(args: argparse.Namespace) -> int:
    if not (args.query or "").strip() and not (args.extra_or or args.extra_and or args.tql_filter_id):
        raise SystemExit(
            "count requires a non-empty query or an explicit filter "
            "(--extra-or/--extra-and/--tql-filter-id). An empty query would match everything."
        )
    # Malformed --extra-or/--extra-and JSON -> unified error contract, not a
    # raw traceback (json.loads raises JSONDecodeError, not RuntimeError).
    try:
        extra_or = json.loads(args.extra_or) if args.extra_or else []
        extra_and = json.loads(args.extra_and) if args.extra_and else []
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid --extra-or/--extra-and JSON: {exc}") from exc
    tql_ids = list(args.tql_filter_id) if args.tql_filter_id else []
    if not isinstance(extra_or, list) or not isinstance(extra_and, list):
        raise SystemExit("--extra-or and --extra-and must be JSON arrays of property filters.")
    spaces = _resolve_scope_spaces(args)
    result = count_composite(
        args.base_url,
        args.query,
        spaces=spaces,
        suits=args.suit,
        extra_property_or=extra_or,
        extra_property_and=extra_and,
        tql_filter_ids=tql_ids,
        timeout=args.timeout,
    )
    payload: Dict[str, Any] = {
        "command": "count",
        "query": args.query,
        "spaces": list(spaces),
        "suits": list(args.suit),
        "endpoint": "/rest/api/unit/v3/count/composite",
        "request": result["request"],
        "matched": result["matched"],
        "response": result["response"],
    }
    notes = _scope_note(args.defaults_applied, empty=result["matched"] == 0)
    if notes:
        payload["notes"] = notes
    emit(args_with_defaults(payload, args))
    return 0


def cmd_similar(args: argparse.Namespace) -> int:
    # Deterministic auth precheck. Missing/misconfigured SWTR auth is the ONE real
    # "total failure": build_headers(require_auth=True) raises RuntimeError, which
    # propagates to main's `except RuntimeError -> emit_error` (unified contract,
    # exit 1). Do it up front, BEFORE the per-term loop that deliberately catches
    # RuntimeError per term, so a genuine no-auth failure is never swallowed as an
    # exit-0 empty result. Per-term errors below remain PARTIAL success: they are
    # collected into rawRuns and the command still exits 0 with whatever it found.
    build_headers("task_tracker", require_auth=True)
    code = extract_unit_code(args.target)
    if code:
        source_unit = get_unit(args.base_url, code, args.timeout)
        source_text = collect_text_from_unit(source_unit)
    else:
        source_unit = {}
        source_text = args.target
    terms = args.term or significant_terms(source_text, STOPWORDS, args.terms_limit)
    if not terms:
        raise SystemExit("Could not extract significant terms. Pass --term explicitly.")
    spaces = _resolve_scope_spaces(args)
    aggregated: Dict[str, Dict[str, Any]] = {}
    raw_runs: List[Dict[str, Any]] = []
    for term in terms:
        try:
            payload = search_units(
                args.base_url,
                term,
                spaces=spaces,
                suits=args.suit,
                attribute_codes=args.attribute_code,
                return_attributes=args.return_attribute or DEFAULT_RETURN_ATTRIBUTES,
                page=0,
                size=args.per_term_limit,
                timeout=args.timeout,
            )
            raw_runs.append({"term": term, "payload": payload})
            for item in page_content(payload):
                if not isinstance(item, dict):
                    continue
                key = unit_result_key(item)
                if key:
                    aggregated[key] = item
        except RuntimeError as exc:
            # A benign per-term error (404/timeout) is collected and partial success
            # is preserved; an auth failure (401/403) is fatal for every term, so
            # propagate it to main's error contract instead of an exit-0 aggregate.
            if is_auth_error(exc):
                raise
            raw_runs.append({"term": term, "error": redact_secrets(str(exc))})
    ranked = sorted(aggregated.values(), key=lambda item: score_item(item, terms, code), reverse=True)
    ranked = [item for item in ranked if score_item(item, terms, code) > 0][: args.limit]
    result: Dict[str, Any] = {
        "command": "similar",
        "sourceCode": code,
        "terms": terms,
        "spaces": list(spaces),
        "items": [summarize_unit_item(item) for item in ranked],
        "rawItems": ranked,
        "rawRuns": raw_runs,
    }
    notes = _scope_note(args.defaults_applied, empty=not ranked)
    if notes:
        result["notes"] = notes
    emit(args_with_defaults(result, args))
    return 0


def parse_attribute_pairs(pairs: Sequence[str]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"Attribute must be key=value, got: {pair}")
        key, value = pair.split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit(f"Attribute key is empty in: {pair}")
        value = value.strip()
        if value.startswith(("{", "[", '"')) or value in {"true", "false", "null"} or re.fullmatch(r"-?\d+(\.\d+)?", value):
            try:
                result[key] = json.loads(value)
                continue
            except json.JSONDecodeError:
                pass
        result[key] = value
    return result


# A suit that has a workflow refuses creation without ``workflow_status`` and
# answers HTTP 570 "Не заполнены обязательные поля". ``NEW`` is a pseudo-command
# that works for every workflow; a real transition code returns HTTP 500 instead.
# Live-tested matrix: references/task-tracker-operations.md.
DEFAULT_WORKFLOW_STATUS = {"command": "NEW"}
# Тип задачи у create, когда его не назвали ни флагом, ни в профиле. Бывшее
# argparse-умолчание `--suit`: у парсера теперь None, чтобы профиль был отличим
# от «не задано».
DEFAULT_CREATE_SUIT = "task"


def read_text_source(source: str, flag: str) -> str:
    """Текст из файла или стандартного ввода (`-`) под именем флага в ошибке."""
    if source == "-":
        return sys.stdin.read()
    try:
        return Path(source).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Не читается {flag} {source}: {exc}") from exc


def read_wiki_body(source: str) -> str:
    """Текст тела страницы: путь к файлу или `-` для стандартного ввода."""
    return read_text_source(source, "--wiki-body")


def apply_wiki_body(attributes: Dict[str, Any], source: Optional[str]) -> Dict[str, Any]:
    """Привести тело страницы к тому виду, который трекер принимает.

    Одна точка на create и update: и явный `--wiki-body`, и сырой
    `--attribute wiki_page_body=...` проходят через `body_value`, который всегда
    отдаёт СТРОКУ. Без этого markdown уезжает как текст, а JSON-документ —
    объектом, и оба получают HTTP 500; именно так рождается вывод «API не
    поддерживает создание страниц с текстом».
    """
    if source is not None:
        attributes[WIKI_BODY_ATTRIBUTE_CODE] = read_wiki_body(source)
    if WIKI_BODY_ATTRIBUTE_CODE in attributes:
        attributes[WIKI_BODY_ATTRIBUTE_CODE] = wiki_body.body_value(
            attributes[WIKI_BODY_ATTRIBUTE_CODE])
    return attributes


def apply_test_case_fields(attributes: Dict[str, Any],
                           precondition: Optional[str] = None,
                           steps: Optional[Sequence[str]] = None,
                           steps_file: Optional[str] = None) -> Dict[str, Any]:
    """Привести поля тест-кейса к форме ЗАПИСИ.

    Три поля читаются в одной форме, а принимаются в другой, и каждое
    расхождение стоит невнятного отказа:

    - `folder` читается объектом `{"code", "title"}`, а на запись берёт голый
      код строкой; объект отвечает `HTTP 500 «Что-то пошло не так»` без имени
      поля — именно так рождается вывод «создание тест-кейсов через API не
      работает»;
    - `precondition` — поле типа `wisiwig`, то есть строка с документом
      ProseMirror внутри (та же ловушка, что у `wiki_page_body`);
    - `test_step` на чтении приходит списком, а на запись берёт объект
      `{"testStepList": [...]}`; список проходит с `200` и молча не
      сохраняется — отказа нет, шагов тоже.

    Одна точка на create и update, чтобы форма не разъезжалась между командами.
    """
    if precondition is not None:
        attributes[PRECONDITION_ATTRIBUTE_CODE] = precondition
    if PRECONDITION_ATTRIBUTE_CODE in attributes:
        attributes[PRECONDITION_ATTRIBUTE_CODE] = wiki_body.body_value(
            attributes[PRECONDITION_ATTRIBUTE_CODE])
    folder = attributes.get(FOLDER_ATTRIBUTE_CODE)
    if isinstance(folder, dict):
        code = folder.get("code")
        if not code:
            raise RuntimeError("Атрибут folder задан объектом без ключа code. "
                               "Папка задаётся кодом: --attribute folder=<UUID>.")
        attributes[FOLDER_ATTRIBUTE_CODE] = code
    if steps and steps_file is not None:
        raise RuntimeError("--step и --steps-file заданы вместе. Шаги берутся из одного источника: "
                           "либо повторяемый --step, либо файл.")
    if steps_file is not None:
        try:
            attributes[TEST_STEP_ATTRIBUTE_CODE] = json.loads(
                read_text_source(steps_file, "--steps-file"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"--steps-file {steps_file}: это не JSON ({exc}). Ожидается массив "
                               "шагов вида [{\"description\": …, \"data\": …, \"result\": …}] "
                               "либо обёртка {\"testStepList\": [ … ]} с другого кейса.") from exc
    if steps:
        attributes[TEST_STEP_ATTRIBUTE_CODE] = list(steps)
    if TEST_STEP_ATTRIBUTE_CODE in attributes:
        value = test_steps.steps_value(attributes[TEST_STEP_ATTRIBUTE_CODE])
        # Пустой список шагов — не «стереть шаги», а «шагов не задавали»: атрибут
        # с пустой обёрткой трекер принимает, и кейс остаётся без шагов молча.
        if value is None:
            attributes.pop(TEST_STEP_ATTRIBUTE_CODE)
        else:
            attributes[TEST_STEP_ATTRIBUTE_CODE] = value
    return attributes


def missing_fields_hint(error: Exception) -> Optional[str]:
    """Turn the tracker's HTTP 570 into a line that says what to do next.

    570 is not a standard status and its body only names the empty fields, so a
    caller who has not read task-tracker-operations.md reads it as a blanket
    refusal -- which is how "creation does not work here" gets concluded from
    what is really one missing attribute.
    """
    message = str(error)
    if "HTTP 570" not in message:
        return None
    if "workflow_status" in message:
        return ("suit с workflow требует атрибут workflow_status. Скрипт подставляет его сам; "
                "если подстановка выключена флагом --no-default-workflow-status, передай "
                "--attribute 'workflow_status={\"command\":\"NEW\"}'. NEW — псевдокоманда, "
                "коды действий workflow здесь не принимаются (они дают HTTP 500).")
    return ("трекер отверг создание: не заполнены обязательные поля (их имена — в тексте ошибки). "
            "Добавь каждое через --attribute key=value. Полный список обязательных полей типа и "
            "коды, которые они принимают, показывает команда `attributes <suit>` "
            "(поля mandatoryCodes и values).")


def _apply_create_defaults(args: argparse.Namespace) -> List[str]:
    """Подставить в create умолчания профиля и вернуть имена полей, взятых оттуда.

    Тип: явный --suit → task_tracker.default_suit → прежнее умолчание task
    (argparse-умолчание переехало сюда, иначе профиль было бы не отличить от
    «не задано»). Пространство, продукт и компоненты — только при пустых флагах:
    явное значение всегда сильнее. Проверка продукта и компонентов по
    справочнику остаётся прежней — отказ трекера по значению из профиля виден
    с источником, а не подменяется молча.

    Тип разрешается ДО раннего возврата по --body-file намеренно: он стоит в URL
    (/rest/api/unit/v2/{suit}/create), а не в теле, поэтому «телом распоряжается
    вызывающий» его не касается, и пропуск --suit — такой же пропуск, как везде
    (REQ-017). В само тело --body-file не подставляется ничего, а взятый из
    профиля тип виден в defaults_applied и в URL dry-run до --execute.
    """
    profile = args.profile
    applied = args.defaults_applied
    from_profile: List[str] = []
    if not args.suit:
        suit = profile_get(profile, "task_tracker.default_suit")
        if suit:
            args.suit = str(suit).strip()
            applied.add("suit", args.suit, "profile")
        else:
            args.suit = DEFAULT_CREATE_SUIT
    if args.body_file:
        # --body-file — полный контроль вызывающего над телом: ни пространство,
        # ни именованные поля в него не подставляются. Тип выше — не поле тела.
        return from_profile
    if not args.space:
        space = profile_get(profile, "task_tracker.primary_space")
        if space:
            args.space = str(space).strip()
            applied.add("space", args.space, "profile")
    if not args.product:
        product = profile_get(profile, "task_tracker.product")
        if product:
            args.product = [str(product).strip()]
            applied.add("product", args.product[0], "profile")
            from_profile.append("product")
    if not args.component:
        components = profile_get(profile, "task_tracker.components")
        if not isinstance(components, list):
            # Схема уже гарантирует список строк для `ok`-профиля; проверка
            # оставлена, чтобы прямой вызов с чужим объектом не падал TypeError.
            components = [components] if isinstance(components, str) else []
        if components:
            args.component = [str(c).strip() for c in components if str(c).strip()]
            applied.add("components", list(args.component), "profile")
            from_profile.append("component")
    return from_profile


def cmd_create(args: argparse.Namespace) -> int:
    fields_from_profile = _apply_create_defaults(args)
    # --body-file — полный контроль вызывающего: именованные поля туда не лезут,
    # поэтому разбор остаётся пустым, а не «не определён».
    resolved_fields: List[Dict[str, Any]] = []
    if args.body_file:
        # Unreadable file / invalid JSON -> unified error contract (OSError and
        # JSONDecodeError are not RuntimeError, so wrap them for main's handler).
        try:
            body = json.loads(Path(args.body_file).expanduser().read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"Cannot read --body-file JSON: {exc}") from exc
    else:
        if not args.summary or not args.space:
            raise SystemExit("For create without --body-file pass --summary and --space.")
        attributes = parse_attribute_pairs(args.attribute)
        # Именованные поля резолвятся ДО отправки: неизвестное имя должно стать
        # внятной ошибкой здесь, а не HTTP 500 от трекера после записи. Явный
        # --attribute сильнее: он последнее слово пользователя.
        resolved_fields = resolve_named_fields(args, attributes)
        # Предпросмотр обязан показывать, какие значения взяты из профиля.
        for entry in resolved_fields:
            if entry.get("field") in fields_from_profile:
                entry["source"] = "profile"
        apply_wiki_body(attributes, getattr(args, "wiki_body", None))
        apply_test_case_fields(attributes,
                               precondition=getattr(args, "precondition", None),
                               steps=getattr(args, "step", None),
                               steps_file=getattr(args, "steps_file", None))
        resolved_fields += resolve_folder_field(args, attributes)
        # An explicit workflow_status always wins, and --body-file is left alone
        # entirely: it is the escape hatch for full control over the payload, so
        # injecting into it would take that control away.
        if not args.no_default_workflow_status and "workflow_status" not in attributes:
            attributes["workflow_status"] = dict(DEFAULT_WORKFLOW_STATUS)
        body = {
            "summary": args.summary,
            "description": args.description or "",
            "space": args.space,
            "attributes": attributes,
        }
    endpoint = f"/rest/api/unit/v2/{quote_path_part(args.suit)}/create"
    url = args.base_url.rstrip("/") + endpoint
    request_info = {
        "method": "POST",
        "url": url,
        "headers": safe_headers_for_log(build_headers("task_tracker", require_auth=args.execute)),
        "body": body,
    }
    parent = getattr(args, "parent", None)
    plan: Dict[str, Any] = {}
    if parent:
        plan["parent"] = {"code": parent, "type": WIKI_HIERARCHY_LINK_TYPE,
                          "url": args.base_url.rstrip("/") + LINK_CREATE_ENDPOINT,
                          "note": "источник связи — родитель, назначение — созданная страница"}
    elif args.suit == WIKI_SUIT_CODE:
        # Страница без связи создаётся, но в дереве вики её не видно: молчаливая
        # потеря, которую легко принять за неудачное создание.
        plan["note"] = ("Страница создаётся без родителя и в дереве вики не появится. "
                        "Родитель задаётся флагом --parent <КОД>.")
    if not args.execute:
        emit(args_with_defaults({"command": "create", "dryRun": True, "request": request_info,
                             "resolvedFields": resolved_fields, **plan}, args))
        return 0
    headers = build_headers("task_tracker", require_auth=True)
    # Do not retry a write-action: a retried POST could create a duplicate task.
    try:
        response, response_headers, status = request_http(
            "POST", url, headers, body=body, timeout=args.timeout, system="task_tracker", max_retries=0,
        )
    except RuntimeError as exc:
        hint = missing_fields_hint(exc)
        if hint is None:
            raise
        # Подсказка про 570 точнее общей подсказки про профиль; источник
        # значений всё равно остаётся виден в defaults_applied.
        return emit_error("create", exc, **dict(args_error_context(args), hint=hint))
    created_code = None
    if isinstance(response, dict):
        created_code = response.get("id") or response.get("code")
    if parent and created_code:
        plan["parent"] = link_to_parent(args, parent, created_code)
    elif parent:
        plan["parent"] = {"code": parent, "linked": False,
                          "error": "в ответе трекера нет кода созданной страницы — "
                                   "связь с родителем не заводилась"}
    emit(args_with_defaults({
        "command": "create",
        "dryRun": False,
        "resolvedFields": resolved_fields,
        # Ссылка на созданный юнит — готовым полем: собирать её по образцу нельзя.
        **({"url": unit_url(args.base_url, created_code)} if created_code else {}),
        "request": request_info,
        "response": {"status": status, "headers": safe_response_headers(response_headers), "body": response},
        **plan,
    }, args))
    return 0


def link_to_parent(args: argparse.Namespace, parent: str, child: str) -> Dict[str, Any]:
    """Поставить созданную страницу под родителя.

    Отдельным вызовом после создания: у трекера нет поля «родитель» в теле
    создания, дерево держится на связях. Ошибка связи не отменяет созданную
    страницу, поэтому она возвращается фактом, а не исключением.
    """
    body = {"source": parent, "destination": child, "type": WIKI_HIERARCHY_LINK_TYPE}
    url = args.base_url.rstrip("/") + LINK_CREATE_ENDPOINT
    try:
        response, _headers, status = request_http(
            "POST", url, build_headers("task_tracker", require_auth=True), body=body,
            timeout=args.timeout, system="task_tracker", max_retries=0)
    except RuntimeError as exc:
        return {"code": parent, "type": WIKI_HIERARCHY_LINK_TYPE, "linked": False,
                "child": child, "error": redact_secrets(str(exc))}
    return {"code": parent, "type": WIKI_HIERARCHY_LINK_TYPE, "linked": True,
            "child": child, "request": body,
            "response": {"status": status, "body": response}}


def _rows_of(data: Any) -> List[Dict[str, Any]]:
    """Строки ответа независимо от обёртки: у трекера это то `{"content": [...]}`, то голый список."""
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("content") or data.get("items") or []
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def normalize_spaces(data: Any) -> List[Dict[str, Any]]:
    out = []
    for row in _rows_of(data):
        space_type = row.get("type")
        out.append({
            "code": row.get("code"),
            "name": normalize_text(row.get("name")),
            "type": space_type.get("code") if isinstance(space_type, dict) else space_type,
        })
    return out


def normalize_filters(data: Any) -> List[Dict[str, Any]]:
    return [
        {
            "code": row.get("code"),
            "name": normalize_text(row.get("name")),
            "description": normalize_text(row.get("description")),
        }
        for row in _rows_of(data)
    ]


def normalize_versions(data: Any) -> List[Dict[str, Any]]:
    return [
        {
            "code": row.get("code"),
            "name": normalize_text(row.get("name")),
            "description": normalize_text(row.get("description")),
            "archived": row.get("archived"),
        }
        for row in _rows_of(data)
    ]


def normalize_workflow(data: Any) -> Dict[str, Any]:
    """Схема процесса: статусы с категориями и переходы `действие: откуда -> куда`.

    Категория (`statusType.code`) — единственная переносимая между пространствами
    характеристика статуса, поэтому она вытаскивается наверх. У перехода создания
    `oldStatus` пуст, и это нормально.
    """
    schema = data.get("wfSchema") if isinstance(data, dict) else None
    transitions_raw = (data or {}).get("wfTransitions") or [] if isinstance(data, dict) else []

    statuses: Dict[str, Dict[str, Any]] = {}
    transitions: List[Dict[str, Any]] = []

    def remember(status: Any) -> Optional[str]:
        if not isinstance(status, dict) or not status.get("code"):
            return None
        code = status["code"]
        if code not in statuses:
            category = status.get("statusType")
            statuses[code] = {
                "code": code,
                "name": normalize_text(status.get("name")),
                "category": category.get("code") if isinstance(category, dict) else category,
            }
        return code

    for row in transitions_raw:
        if not isinstance(row, dict):
            continue
        action = row.get("wfAction") if isinstance(row.get("wfAction"), dict) else {}
        transitions.append({
            "action": action.get("code"),
            "actionName": normalize_text(action.get("name")),
            "from": remember(row.get("oldStatus")),
            "to": remember(row.get("destStatus")),
        })

    return {
        "schema": schema.get("code") if isinstance(schema, dict) else schema,
        "schemaName": normalize_text(schema.get("name")) if isinstance(schema, dict) else None,
        "statuses": list(statuses.values()),
        "transitions": transitions,
    }


# Поля, которые пользователь называет по-человечески, и коды атрибутов, под
# которыми они живут у разных типов задач. Порядок значим: берётся первый код,
# который реально объявлен у типа. Проверено на портале 14.08.2026 —
# «Fix Version» у task и story это `fix_version`, у epic_crpv `fix_version_s`;
# у task рядом с `component_s` есть `sber_component`, и записью проверен именно он.
FIELD_ATTRIBUTE_CANDIDATES: Dict[str, List[str]] = {
    "fix-version": ["fix_version", "fix_version_s"],
    "affects-version": ["affects_version", "affects_version_s"],
    "component": ["sber_component", "component_s_crpv", "component_s"],
    "product": ["customfield_24900"],
}

# Реестры значений живут по пространству: одно имя в разных пространствах имеет
# разные идентификаторы, поэтому резолв всегда идёт в реестре целевого space.
COMPONENTS_DIRECTORY_ENDPOINT = (
    "/extension/plugin/v2/rest/api/sber_components_plugin/v1/component/find")
VERSIONS_DIRECTORY_ENDPOINT = (
    "/extension/plugin/v2/rest/api/swtr_task_tracker_plugin/v1/version/find")


def normalize_suit_attributes(data: Any) -> List[Dict[str, Any]]:
    """Атрибуты типа задачи из `GET /rest/api/suit/v1/{suit}`.

    Обязательность и значение по умолчанию лежат не отдельными полями, а внутри
    `parameters` (`MANDATORY`, `DEFAULT_VALUE`) — рядом с `ORDER`, который к
    контракту отношения не имеет. Без них схема не отвечает на два вопроса,
    ради которых её и читают: что трекер потребует при создании и какие
    значения он вообще принимает.
    """
    rows = (data or {}).get("attributes") if isinstance(data, dict) else None
    out = []
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("code"):
            continue
        values = row.get("directoryValues")
        parameters = row.get("parameters") if isinstance(row.get("parameters"), dict) else {}
        out.append({
            "code": row.get("code"),
            "name": normalize_text(row.get("name")),
            "type": row.get("type"),
            "mandatory": bool(parameters.get("MANDATORY")),
            "defaultValue": parameters.get("DEFAULT_VALUE"),
            "directoryValues": values if isinstance(values, list) else [],
        })
    return out


def pick_field_attribute(schema: Any, field: str) -> Optional[Dict[str, Any]]:
    """Атрибут типа задачи под человеческое имя поля, либо `None`, если его нет.

    Код не угадывается и не хардкодится под пространство: он выбирается из
    реальной схемы типа, поэтому epic и task разводятся сами собой.
    """
    if field not in FIELD_ATTRIBUTE_CANDIDATES:
        raise KeyError(f"Неизвестное поле {field!r}. Доступны: "
                       f"{sorted(FIELD_ATTRIBUTE_CANDIDATES)}")
    attributes = schema if isinstance(schema, list) else normalize_suit_attributes(schema)
    by_code = {a.get("code"): a for a in attributes}
    for code in FIELD_ATTRIBUTE_CANDIDATES[field]:
        if code in by_code:
            return by_code[code]
    return None


def build_attribute_value(attribute: Dict[str, Any], codes: Sequence[Any]) -> Any:
    """Значение атрибута в форме, которую принимает создание задачи.

    Форма зависит от ТИПА поля, а не от того, мультизначное оно: таксономия —
    голый код строкой, ссылочные поля — список объектов с ключом `code`. Список
    голых строк ссылочные поля отвергают с HTTP 500 (проверено записью).
    """
    values = [str(code).strip() for code in codes if str(code).strip()]
    if attribute.get("type") == "taxonomy":
        return values[0] if values else ""
    return [{"code": value} for value in values]


def match_directory_code(rows: Sequence[Dict[str, Any]], wanted: str) -> str:
    """Код справочника по имени. Не угадывает: неоднозначность — ошибка."""
    needle = str(wanted or "").strip()
    if not needle:
        raise RuntimeError("Пустое имя значения справочника.")
    if not rows:
        raise RuntimeError(
            f"Справочник пуст, сопоставить {needle!r} не с чем. Проверьте, что значения заведены "
            f"в этом пространстве.")

    exact_code = [r for r in rows if str(r.get("code") or "") == needle]
    if exact_code:
        return exact_code[0]["code"]

    matches = [r for r in rows if str(r.get("name") or "").strip().casefold() == needle.casefold()]
    if len(matches) == 1:
        return matches[0]["code"]
    if len(matches) > 1:
        listed = ", ".join(f"{r.get('name')} ({r.get('code')})" for r in matches)
        raise RuntimeError(
            f"Имя {needle!r} в справочнике неоднозначно: {listed}. Передайте код вместо имени.")

    near = [str(r.get("name")) for r in rows
            if needle.casefold() in str(r.get("name") or "").casefold()
            or str(r.get("name") or "").casefold() in needle.casefold()]
    names = [str(r.get("name")) for r in rows]
    if not near:
        near = difflib.get_close_matches(needle, names, n=5, cutoff=0.5)
    if near:
        hint = f" Похожее: {', '.join(near[:5])}."
    else:
        # Совсем непохожее имя — показать, что вообще есть, иначе пользователю
        # придётся гадать вслепую или отдельно ходить за справочником.
        sample = ", ".join(names[:5])
        hint = f" Доступно {len(names)} значений, например: {sample}."
    raise RuntimeError(f"Значение {needle!r} не найдено в справочнике.{hint}")


def fetch_directory(base_url: str, kind: str, space: str, timeout: float,
                    suit: Optional[str] = None) -> List[Dict[str, Any]]:
    """Значения справочника: у таксономии они в схеме типа, у ссылочных — в реестре space."""
    if kind == "product":
        if not suit:
            raise RuntimeError("Для справочника продукта нужен тип задачи: --suit.")
        schema = get_json(base_url, f"/rest/api/suit/v1/{quote_path_part(suit)}", timeout)
        attribute = pick_field_attribute(schema, "product")
        if attribute is None:
            raise RuntimeError(f"У типа {suit!r} нет атрибута кода продукта.")
        return [{"code": v.get("code"), "name": normalize_text(v.get("name"))}
                for v in attribute.get("directoryValues") or [] if isinstance(v, dict)]

    endpoint = {"components": COMPONENTS_DIRECTORY_ENDPOINT,
                "versions": VERSIONS_DIRECTORY_ENDPOINT}.get(kind)
    if not endpoint:
        raise RuntimeError(f"Неизвестный справочник {kind!r}: components, versions или product.")
    rows: List[Dict[str, Any]] = []
    page = 0
    while page < DIRECTORY_MAX_PAGES:
        data = post_json(base_url, endpoint,
                         {"space": space, "page": {"page": page, "size": DIRECTORY_PAGE_SIZE}},
                         timeout)
        chunk = _rows_of(data)
        rows.extend({"code": r.get("code"), "name": normalize_text(r.get("name"))} for r in chunk)
        if len(chunk) < DIRECTORY_PAGE_SIZE:
            break
        page += 1
    return rows


DIRECTORY_PAGE_SIZE = 200
DIRECTORY_MAX_PAGES = 20


def resolve_named_fields(args: argparse.Namespace,
                         attributes: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Разложить `--fix-version/--affects-version/--component/--product` по атрибутам.

    Схема типа задачи и реестры пространства читаются лениво — ровно один раз и
    только если хотя бы одно поле задано, чтобы обычное создание не платило
    лишними запросами.

    Возвращает разбор «что во что превратилось»: пользователь назвал имя, а в
    трекер уходит код, и увидеть подстановку он должен до записи.
    """
    requested = {field: list(getattr(args, field.replace("-", "_"), []) or [])
                 for field in FIELD_ATTRIBUTE_CANDIDATES}
    if not any(requested.values()):
        return []

    schema = get_json(args.base_url, f"/rest/api/suit/v1/{quote_path_part(args.suit)}", args.timeout)
    suit_attributes = normalize_suit_attributes(schema)
    directories: Dict[str, List[Dict[str, Any]]] = {}
    resolved: List[Dict[str, Any]] = []

    for field, names in requested.items():
        if not names:
            continue
        attribute = pick_field_attribute(suit_attributes, field)
        if attribute is None:
            raise RuntimeError(
                f"У типа задачи {args.suit!r} нет поля {field!r}. Доступные поля этого типа "
                f"смотрите командой `attributes {args.suit}`.")
        kind = {"product": "product"}.get(field,
                                          "versions" if attribute["type"] == "version" else "components")
        if kind not in directories:
            directories[kind] = (
                [{"code": v.get("code"), "name": normalize_text(v.get("name"))}
                 for v in attribute.get("directoryValues") or []]
                if kind == "product"
                else fetch_directory(args.base_url, kind, args.space, args.timeout, suit=args.suit))
        codes = [match_directory_code(directories[kind], name) for name in names]
        value = build_attribute_value(attribute, codes)
        if attribute["code"] in attributes:
            resolved.append({"field": field, "attribute": attribute["code"], "names": names,
                             "codes": codes, "applied": False,
                             "note": "перекрыто явным --attribute"})
            continue
        attributes[attribute["code"]] = value
        resolved.append({"field": field, "attribute": attribute["code"], "names": names,
                         "codes": codes, "applied": True})
    return resolved


# Папка тест-кейса адресуется UUID, но человек называет её именем или путём в
# дереве. Имя само по себе неоднозначно: в дереве PVM около 2700 папок, и
# `INST`, `K8S`, `AI Generated` встречаются в разных ветках, поэтому совпадение
# ищется по ХВОСТУ пути, а неоднозначность не разрешается молча.
FOLDER_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
FOLDER_ROOT_ENDPOINT = f"{TMS_BASE}/folder/root"
FOLDER_CANDIDATES_SHOWN = 10


def folder_paths(node: Any, prefix: Sequence[str] = ()) -> List[Tuple[str, str]]:
    """Дерево папок из `folder/root` → плоский список `(полный путь, код)`."""
    out: List[Tuple[str, str]] = []
    if isinstance(node, list):
        for item in node:
            out += folder_paths(item, prefix)
        return out
    if not isinstance(node, dict):
        return out
    title = normalize_text(node.get("title") or node.get("name"))
    path = list(prefix) + ([title] if title else [])
    code = node.get("id", {}).get("code") if isinstance(node.get("id"), dict) else node.get("code")
    if code and path:
        out.append(("/".join(path), code))
    for child in node.get("children") or []:
        out += folder_paths(child, path)
    return out


def match_folder_path(folders: Sequence[Tuple[str, str]], wanted: str) -> str:
    """Код папки по коду, имени или хвосту пути; неоднозначность — отказ со списком.

    Код проверяется первым — тем же порядком, что у `match_directory_code`. Не
    всякий код папки похож на UUID: корень пространства закодирован как
    `<SPACE>_test_case`, и без этой ветки значение, которое референс навыка
    объявляет рабочим, отвергалось бы как несуществующее имя — причём подсказка
    в отказе отправляла бы за деревом, в котором лежит ровно этот код. Той же
    веткой держится сценарий «прочитал кейс → создал похожий»: у кейса из
    корневой папки прочитанный код именно такой.
    """
    exact_code = [code for _, code in folders if code == wanted.strip()]
    if exact_code:
        return exact_code[0]
    needle = [part.strip().casefold() for part in wanted.strip("/").split("/") if part.strip()]
    matches = [(path, code) for path, code in folders
               if [p.casefold() for p in path.split("/")][-len(needle):] == needle]
    if len(matches) == 1:
        return matches[0][1]
    if not matches:
        raise RuntimeError(
            f"Папка {wanted!r} не найдена. Дерево папок пространства — команда "
            f"`tms folder-root --space <SPACE> --type TEST_CASE`.")
    # Кандидаты печатаются вместе с кодами: в живом дереве PVM есть две разные
    # папки с ОДИНАКОВЫМ полным путём («Все тест-кейсы/Корзина»), и список одних
    # путей в таком случае не даёт ничем уточнить выбор.
    shown = "; ".join(f"{path} [{code}]" for path, code in matches[:FOLDER_CANDIDATES_SHOWN])
    tail = f" и ещё {len(matches) - FOLDER_CANDIDATES_SHOWN}" if len(
        matches) > FOLDER_CANDIDATES_SHOWN else ""
    raise RuntimeError(
        f"Имя папки {wanted!r} неоднозначно: {len(matches)} совпадений — {shown}{tail}. "
        f"Уточните путь (например 'UI Audit PVM/R7.2/Источники') или передайте UUID.")


def resolve_folder_field(args: argparse.Namespace, attributes: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Превратить имя или путь папки в её код.

    UUID проходит насквозь и сети не стоит. Резолв работает и в dry-run: это
    чтение, а подстановку нужно видеть ДО записи, как и у остальных именованных
    полей.
    """
    wanted = attributes.get(FOLDER_ATTRIBUTE_CODE)
    if not isinstance(wanted, str) or not wanted.strip() or FOLDER_UUID_RE.match(wanted.strip()):
        return []
    space = getattr(args, "space", None)
    if not space:
        raise RuntimeError(f"Папка задана именем {wanted!r}, но пространство неизвестно — "
                           f"дерево папок читается по пространству. Передайте UUID папки.")
    body = {"type": "TEST_CASE", "spaceId": {"code": space}}
    data = post_json(args.base_url, FOLDER_ROOT_ENDPOINT, body, args.timeout)
    code = match_folder_path(folder_paths(data), wanted)
    attributes[FOLDER_ATTRIBUTE_CODE] = code
    return [{"field": "folder", "attribute": FOLDER_ATTRIBUTE_CODE, "names": [wanted],
             "codes": [code], "applied": True}]


# Изменение полей существующей задачи. Форма пути неочевидна: глагол стоит ПЕРЕД
# кодом, поэтому привычные `/unit/v2/{code}/update` отвечают 404.
UNIT_UPDATE_ENDPOINT = "/rest/api/unit/v2/update/{code}"


def _unit_attribute_values(unit: Any) -> Dict[str, Any]:
    """Текущие значения атрибутов юнита кодом → значение, для диффа «было → станет»."""
    attributes = (unit or {}).get("attributes") if isinstance(unit, dict) else None
    values: Dict[str, Any] = {}
    if isinstance(attributes, dict):
        return dict(attributes)
    for row in attributes or []:
        if isinstance(row, dict) and row.get("code"):
            values[row["code"]] = row.get("value")
    return values


def _unit_facet(unit: Any, key: str) -> Optional[str]:
    """`suit`/`space` юнита: приходят объектом, изредка строкой."""
    value = (unit or {}).get(key) if isinstance(unit, dict) else None
    if isinstance(value, dict):
        return value.get("code")
    return value if isinstance(value, str) else None


def cmd_update(args: argparse.Namespace) -> int:
    """Изменить атрибуты существующей задачи.

    Тип задачи и пространство берутся из самого юнита: без них не резолвятся ни
    код атрибута (у эпика он другой), ни справочник (значения привязаны к space).
    """
    unit = get_json(args.base_url, f"/rest/api/unit/v2/{quote_path_part(args.code)}", args.timeout)
    suit = _unit_facet(unit, "suit")
    space = _unit_facet(unit, "space")
    if not suit or not space:
        raise RuntimeError(
            f"У юнита {args.code} не удалось определить тип задачи и пространство — без них "
            f"нельзя выбрать код атрибута и справочник значений.")

    # Резолв именованных полей идёт по типу и пространству ЭТОГО юнита.
    resolve_args = argparse.Namespace(
        base_url=args.base_url, timeout=args.timeout, suit=suit, space=space,
        **{field.replace("-", "_"): list(getattr(args, field.replace("-", "_"), []) or [])
           for field in FIELD_ATTRIBUTE_CANDIDATES})
    attributes = parse_attribute_pairs(args.attribute)
    resolved = resolve_named_fields(resolve_args, attributes)
    apply_wiki_body(attributes, getattr(args, "wiki_body", None))
    apply_test_case_fields(attributes,
                           precondition=getattr(args, "precondition", None),
                           steps=getattr(args, "step", None),
                           steps_file=getattr(args, "steps_file", None))
    resolved += resolve_folder_field(resolve_args, attributes)

    if WORKFLOW_STATUS_ATTRIBUTE_CODE in attributes:
        return emit_error(
            "update",
            "Статус этим путём не меняется: он проекция конечного автомата, а запись значением "
            "даёт HTTP 500 без объяснения.",
            unitCode=args.code,
            hint=f"Используйте `status set {args.code} --to <СТАТУС>`.")
    # summary и description — базовые поля юнита, а не атрибуты: внутри
    # `attributes` трекер их не видит, они лежат на верхнем уровне тела.
    base_fields = {name: getattr(args, name)
                   for name in ("summary", "description")
                   if getattr(args, name, None) is not None}
    if not attributes and not base_fields:
        return emit_error(
            "update", "Не задано ни одного поля для изменения.", unitCode=args.code,
            hint="Передайте --fix-version/--affects-version/--component/--product, "
                 "--summary/--description или --attribute.")

    current = _unit_attribute_values(unit)
    diff = [{"attribute": code,
             "before": current.get(code),
             "after": value,
             "note": ("значение заменяется целиком — прежний список перезаписывается"
                      if isinstance(value, list) else None)}
            for code, value in attributes.items()]
    for name, value in base_fields.items():
        before = unit.get(name) if isinstance(unit, dict) else None
        diff.append({"attribute": name,
                     "before": before if isinstance(before, str) else None,
                     "after": value, "note": "базовое поле юнита, не атрибут"})

    body: Dict[str, Any] = dict(base_fields)
    if attributes:
        body["attributes"] = attributes
    endpoint = UNIT_UPDATE_ENDPOINT.format(code=quote_path_part(args.code))
    url = args.base_url.rstrip("/") + endpoint
    request_info = {"method": "PATCH", "url": url, "body": body}

    if not args.execute:
        emit({"command": "update", "dryRun": True, "unitCode": args.code, "suit": suit,
              "space": space, "resolvedFields": resolved, "diff": diff, "request": request_info,
              "note": "Мультизначное поле перезаписывается целиком: прежние значения не сохраняются."})
        return 0

    headers = build_headers("task_tracker", require_auth=True)
    # Запись не повторяется: повтор после таймаута перетёр бы поле второй раз.
    response, _, status = request_http("PATCH", url, headers, body=body, timeout=args.timeout,
                                       system="task_tracker", max_retries=0)
    readback = get_json(args.base_url, f"/rest/api/unit/v2/{quote_path_part(args.code)}",
                        args.timeout)
    after = _unit_attribute_values(readback)
    emit({"command": "update", "dryRun": False, "unitCode": args.code, "suit": suit,
          "space": space, "resolvedFields": resolved, "diff": diff,
          "request": request_info,
          "response": {"status": status, "body": response},
          "readBack": dict(
              {code: after.get(code) for code in attributes},
              **{name: readback.get(name) for name in base_fields
                 if isinstance(readback, dict)})})
    return 0


def describe_suit_attribute(attribute: Dict[str, Any], values_limit: int) -> Dict[str, Any]:
    """Строка вывода `attributes`: тип, обязательность, дефолт и коды значений.

    Раньше здесь стояло только число значений справочника. Число отвечает на
    вопрос «сколько», а работать нужно с «какими»: без кодов агент подставляет
    в `type_of_testing` человеческое слово, получает отказ и идёт угадывать.
    Длинные справочники (33 версии) режутся, но факт усечения виден в выводе —
    молча укороченный список выглядит как полный.
    """
    values = [{"code": v.get("code"), "name": normalize_text(v.get("name"))}
              for v in attribute["directoryValues"] if isinstance(v, dict)]
    row: Dict[str, Any] = {
        "code": attribute["code"],
        "name": attribute["name"],
        "type": attribute["type"],
        "mandatory": attribute["mandatory"],
        "directoryValues": len(attribute["directoryValues"]),
    }
    if attribute.get("defaultValue") is not None:
        row["defaultValue"] = attribute["defaultValue"]
    if values:
        row["values"] = values[:values_limit]
        if len(values) > values_limit:
            row["valuesTruncated"] = len(values) - values_limit
    return row


def cmd_attributes(args: argparse.Namespace) -> int:
    schema = get_json(args.base_url, f"/rest/api/suit/v1/{quote_path_part(args.suit)}", args.timeout)
    attributes = normalize_suit_attributes(schema)
    # namedFields считается по ПОЛНОЙ схеме: иначе фильтр вывода превращается в
    # ложное «у типа нет такого поля».
    known = {field: (pick_field_attribute(attributes, field) or {}).get("code")
             for field in sorted(FIELD_ATTRIBUTE_CANDIDATES)}
    # Обязательные поля считаются по ПОЛНОЙ схеме по той же причине, что и
    # namedFields: под фильтром `--query priority` список обязательных полей
    # обязан остаться списком обязательных полей типа, а не тем, что попало в
    # выдачу. Именно его агент подставляет в create, чтобы не ловить HTTP 570.
    mandatory_codes = [a["code"] for a in attributes if a["mandatory"]]
    if args.query:
        needle = args.query.casefold()
        attributes = [a for a in attributes
                      if needle in str(a["code"]).casefold() or needle in a["name"].casefold()]
    rows = [describe_suit_attribute(a, args.values_limit) for a in attributes]
    emit({
        "command": "attributes",
        "endpoint": f"/rest/api/suit/v1/{args.suit}",
        "suit": args.suit,
        "count": len(rows),
        "attributes": rows,
        "mandatoryCodes": mandatory_codes,
        "namedFields": known,
        "note": "namedFields — коды под человеческие имена полей у ЭТОГО типа задачи. "
                "mandatoryCodes — поля, без которых создание отвечает HTTP 570. "
                "values — коды, которые принимает справочник; в запрос идёт code, не name.",
    })
    return 0


def cmd_directory(args: argparse.Namespace) -> int:
    rows = fetch_directory(args.base_url, args.kind, args.space, args.timeout, suit=args.suit)
    if args.query:
        needle = args.query.casefold()
        rows = [r for r in rows if needle in str(r.get("name") or "").casefold()]
    emit(args_with_defaults({
        "command": "directory",
        "kind": args.kind,
        "space": args.space,
        "suit": args.suit,
        "count": len(rows),
        "values": rows[:args.limit],
        "note": "Один и тот же справочник в другом пространстве имеет другие коды.",
    }, args))
    return 0


def cmd_spaces(args: argparse.Namespace) -> int:
    data = post_json(args.base_url, "/rest/api/space/v3/find", {}, args.timeout)
    spaces = normalize_spaces(data)
    if args.query:
        needle = args.query.lower()
        spaces = [s for s in spaces
                  if needle in str(s.get("code", "")).lower() or needle in str(s.get("name", "")).lower()]
    emit({
        "command": "spaces",
        "endpoint": "/rest/api/space/v3/find",
        "query": args.query,
        "count": len(spaces),
        "spaces": spaces,
    })
    return 0


def cmd_filters(args: argparse.Namespace) -> int:
    endpoint = "/rest/api/unit/v2/filters"
    data = get_json(args.base_url, endpoint, args.timeout)
    filters = normalize_filters(data)
    emit({
        "command": "filters",
        "endpoint": endpoint,
        "count": len(filters),
        "filters": filters,
        "note": "Поиск внутри фильтра — swtr_unit_search.py --filter <code>.",
    })
    return 0


def cmd_versions(args: argparse.Namespace) -> int:
    endpoint = "/extension/plugin/v2/rest/api/swtr_task_tracker_plugin/v1/version/find-all"
    body = {"space": args.space, "page": {"page": args.page, "size": args.limit}}
    data = post_json(args.base_url, endpoint, body, args.timeout)
    versions = normalize_versions(data)
    if not args.include_archived:
        versions = [v for v in versions if not v.get("archived")]
    emit(args_with_defaults({
        "command": "versions",
        "endpoint": endpoint,
        "space": args.space,
        "count": len(versions),
        "versions": versions,
        "response": data,
    }, args))
    return 0


# Форма ответа снята с portal 18.08.2026 (CRPV-106611): content[] с fileId,
# filePathParsedDto.fileName, fileMetadataDto.{contentType,contentLength},
# createdBy, createdAt, fileNotFound. До этого раздел в
# references/task-tracker-operations.md стоял UNCONFIRMED.
UNIT_FILES_ENDPOINT = "/rest/api/unit/files/v2/{code}?safe=true"


def normalize_attachments(payload: Any) -> List[Dict[str, Any]]:
    """Плоский перечень вложений. Содержимое файлов не читается и не разбирается."""
    rows = payload.get("content") if isinstance(payload, dict) else None
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        path = row.get("filePathParsedDto") or {}
        meta = row.get("fileMetadataDto") or {}
        author = row.get("createdBy") or {}
        out.append({
            "fileId": row.get("fileId"),
            "fileName": path.get("fileName"),
            "contentType": meta.get("contentType"),
            "contentLength": meta.get("contentLength"),
            "createdAt": row.get("createdAt"),
            "createdBy": author.get("login") if isinstance(author, dict) else None,
            # safe=true отдаёт юнит, даже если файла нет в хранилище. Молчать об
            # этом нельзя: «вложение есть» и «вложение скачается» — разные вещи.
            "missingInStorage": bool(row.get("fileNotFound")),
        })
    return out


def cmd_attachments(args: argparse.Namespace) -> int:
    code = extract_unit_code(args.code) or args.code
    endpoint = UNIT_FILES_ENDPOINT.format(code=quote_path_part(code))
    data = get_json(args.base_url, endpoint, args.timeout)
    files = normalize_attachments(data)
    emit({
        "command": "attachments",
        "endpoint": endpoint,
        "unit": code,
        "count": len(files),
        "attachments": files,
        "response": data,
    })
    return 0


def cmd_workflow(args: argparse.Namespace) -> int:
    endpoint = (
        f"/rest/api/workflow/v1/suit/{quote_path_part(args.suit)}"
        f"/space/{quote_path_part(args.space)}/workflow"
    )
    data = get_json(args.base_url, endpoint, args.timeout)
    schema = normalize_workflow(data)
    emit(args_with_defaults({
        "command": "workflow",
        "endpoint": endpoint,
        "space": args.space,
        "suit": args.suit,
        "schema": schema["schema"],
        "schemaName": schema["schemaName"],
        "statuses": schema["statuses"],
        "transitions": schema["transitions"],
        "note": "Схема читается без юнита. Коды действий уникальны для схемы — не переносить в другое пространство.",
    }, args))
    return 0


def cmd_tql(args: argparse.Namespace) -> int:
    """Выполнить произвольный TQL, но сперва проверить форму запроса локально.

    Смысл проверки — не «строгость», а единственный отказ трекера, который ничем
    себя не выдаёт: смешение AND и OR он принимает и возвращает неверную выборку
    под кодом 200. Скобки он отвергает сам, но объяснить их дешевле здесь.
    """
    problems = validate_tql(args.query)
    if problems and not args.force:
        return emit_error(
            "tql",
            "Запрос отклонён локально: " + " ".join(problems),
            query=args.query,
            problems=problems,
            hint="Исправьте форму запроса либо повторите с --force, если уверены.",
        )
    body = {
        "query": args.query,
        "page": {"page": args.page, "size": args.limit},
    }
    attributes = list(dict.fromkeys(args.return_attribute or []))
    if attributes:
        body["attributes"] = attributes
    response = post_json(args.base_url, TQL_FIND_ENDPOINT, body, args.timeout)
    emit({
        "command": "tql",
        "endpoint": TQL_FIND_ENDPOINT,
        "tql": args.query,
        "warnings": problems,
        "request": body,
        "response": response,
    })
    return 0


def cmd_suits(args: argparse.Namespace) -> int:
    endpoint = f"/rest/api/space/v3/{quote_path_part(args.space)}/suits"
    data = get_json(args.base_url, endpoint, args.timeout)
    suits: List[Dict[str, Any]] = []
    # Live API returns {"code": <space>, "suits": [...]}; tolerate a bare list too.
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("suits") or data.get("content") or []
    else:
        rows = []
    for row in rows:
        if isinstance(row, dict):
            suits.append({"code": row.get("code"), "name": row.get("name")})
    emit(args_with_defaults({"command": "suits", "space": args.space, "suits": suits, "response": data}, args))
    return 0


# Catalog of link types, keyed by base URL and fetched at most once per process.
# `link find` returns bare codes (`dependend`, `partof`, `integration`) and a
# reader cannot tell what they mean; this catalog is what turns them into words.
_LINK_TYPES_CACHE: Dict[str, Dict[str, Any]] = {}


def normalize_link_type(row: Dict[str, Any]) -> Dict[str, Any]:
    source = row.get("source") if isinstance(row.get("source"), dict) else {}
    destination = row.get("destination") if isinstance(row.get("destination"), dict) else {}
    return {
        "code": row.get("code"),
        "name": normalize_text(row.get("name")),
        "sourceName": normalize_text(source.get("name")),
        "destinationName": normalize_text(destination.get("name")),
        "relationship": row.get("relationship"),
    }


def fetch_link_types(base_url: str, timeout: float) -> Dict[str, Dict[str, Any]]:
    """``{code: normalized-type}`` from ``POST /rest/api/unit/v2/link/types``.

    Cached per base URL: `link find` resolves every link through this catalog, and
    one lookup per link would turn a single read into a burst of identical calls.
    """
    cached = _LINK_TYPES_CACHE.get(base_url)
    if cached is not None:
        return cached
    data = post_json(base_url, "/rest/api/unit/v2/link/types", {}, timeout)
    rows = data.get("content") if isinstance(data, dict) else data
    catalog: Dict[str, Dict[str, Any]] = {}
    for row in rows or []:
        if isinstance(row, dict) and row.get("code"):
            catalog[row["code"]] = normalize_link_type(row)
    _LINK_TYPES_CACHE[base_url] = catalog
    return catalog


def fetch_unit_summaries(base_url: str, codes: Sequence[str], timeout: float) -> Dict[str, str]:
    """``{code: summary}`` for a batch of unit codes in ONE find call.

    A call per link would turn "show me the links of this task" into a burst of
    requests that grows with the answer.
    """
    wanted = [code for code in dict.fromkeys(codes) if code]
    if not wanted:
        return {}
    body = {
        "filters": {"properties": {"or": [{"property": "code", "operator": "eq", "value": code}
                                          for code in wanted]}},
        "attributes": [],
        "page": {"page": 0, "size": len(wanted)},
        "sort": {"attribute": ["updatedAt"], "direction": "DESC"},
    }
    data = post_json(base_url, "/rest/api/unit/v3/find", body, timeout)
    rows = data.get("content") if isinstance(data, dict) else data
    summaries: Dict[str, str] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        item = summarize_unit_item(row)
        if item.get("code"):
            summaries[item["code"]] = item.get("summary") or ""
    return summaries


def find_links(base_url: str, unit: str, timeout: float,
               types: Optional[Sequence[str]] = None, direction: Optional[str] = None,
               page: int = 0, size: int = 50) -> Tuple[Dict[str, Any], Any]:
    """``(тело запроса, сырой ответ)`` для связей одного юнита.

    Вынесено из `cmd_link`, потому что тот же запрос делает `spec_sources.py`.
    Двойная вложенность `page` — знание про схему API (`additionalProperties:
    false`), и держать его в двух местах значит однажды починить только одно.
    """
    body: Dict[str, Any] = {"unitId": unit, "page": {"page": {"size": size, "page": page}}}
    if types:
        body["types"] = list(types)
    if direction:
        body["direction"] = direction
    return body, post_json(base_url, LINK_FIND_ENDPOINT, body, timeout)


def resolve_link(link: Dict[str, Any], unit_code: str, catalog: Dict[str, Dict[str, Any]],
                 summaries: Dict[str, str]) -> Dict[str, Any]:
    """One link rendered from the point of view of the unit that was asked about.

    Direction matters for reading: the same `dependend` link is "depends on" from
    the source and "is depended on by" from the destination. Printing the bare
    code makes the reader guess, and guessing the direction backwards is worse
    than not knowing it.
    """
    type_code = link.get("type")
    meta = catalog.get(type_code) or {}
    source, destination = link.get("source"), link.get("destination")
    if source == unit_code:
        relation, other = meta.get("sourceName"), destination
    elif destination == unit_code:
        relation, other = meta.get("destinationName"), source
    else:
        relation, other = meta.get("name"), destination
    return {
        "type": type_code,
        "typeName": meta.get("name") or type_code,
        "relation": relation or type_code,
        "unit": other,
        "unitSummary": summaries.get(other, ""),
        "source": source,
        "destination": destination,
        "deleted": link.get("deleted"),
    }


def enrich_links(links: List[Dict[str, Any]], unit_code: str, base_url: str,
                 timeout: float) -> Tuple[List[Dict[str, Any]], List[str]]:
    """``(resolved links, notes)``.

    Both lookups are best-effort by design: they are a readability layer over a
    read that already succeeded, so a failing catalog or batch degrades to the
    raw codes and says so in ``notes``. Turning a working read into an error
    because an optional lookup failed would be a worse bug than the one this
    resolution fixes.
    """
    if not links:
        return [], []
    notes: List[str] = []
    catalog: Dict[str, Dict[str, Any]] = {}
    try:
        catalog = fetch_link_types(base_url, timeout)
    except Exception as exc:  # noqa: BLE001 - see docstring: degrade, never fail
        notes.append("справочник типов связей недоступен, типы остались кодами: %s"
                     % redact_secrets(str(exc)))
    others = []
    for link in links:
        for code in (link.get("source"), link.get("destination")):
            if code and code != unit_code:
                others.append(code)
    summaries: Dict[str, str] = {}
    try:
        summaries = fetch_unit_summaries(base_url, others, timeout)
    except Exception as exc:  # noqa: BLE001 - see docstring: degrade, never fail
        notes.append("summary связанных юнитов не получены: %s" % redact_secrets(str(exc)))
    return [resolve_link(link, unit_code, catalog, summaries) for link in links], notes


def cmd_link(args: argparse.Namespace) -> int:
    base = args.base_url.rstrip("/")
    if args.action == "types":
        catalog = fetch_link_types(args.base_url, args.timeout)
        emit({"command": "link.types", "count": len(catalog),
              "types": sorted(catalog.values(), key=lambda row: row["code"] or "")})
        return 0
    if args.action == "find":
        if not args.unit:
            raise SystemExit("link find requires --unit CODE.")
        body, data = find_links(args.base_url, args.unit, args.timeout,
                                types=args.filter_type, direction=args.direction,
                                page=args.page, size=args.limit)
        payload: Dict[str, Any] = {"command": "link.find", "request": body, "response": data}
        links = data.get("content") if isinstance(data, dict) else None
        if not args.no_resolve and isinstance(links, list):
            payload["links"], notes = enrich_links(links, args.unit, args.base_url, args.timeout)
            if notes:
                payload["notes"] = notes
        emit(payload)
        return 0
    if args.action in ("create", "delete"):
        if not (args.source and args.destination and args.link_type):
            raise SystemExit(f"link {args.action} requires --source, --destination and --type.")
        body = {"source": args.source, "destination": args.destination, "type": args.link_type}
        create = args.action == "create"
        method = "POST" if create else "DELETE"
        # У создания и удаления разные ручки: DELETE на /link трекер не объявляет
        # и отвечает 405. Своя ручка помечает связь удалённой (в link/find она
        # остаётся с deleted=true), поэтому снятое можно вернуть link/restore.
        url = base + ("/rest/api/unit/v1/link" if create else "/rest/api/unit/v1/link/delete")
        request_info = {
            "method": method, "url": url,
            "headers": safe_headers_for_log(build_headers("task_tracker", require_auth=args.execute)),
            "body": body,
        }
        if not args.execute:
            emit({"command": f"link.{args.action}", "dryRun": True, "request": request_info})
            return 0
        headers = build_headers("task_tracker", require_auth=True)
        # No retry on a write: a replayed POST/DELETE could duplicate/undo the link.
        response, response_headers, status = request_http(
            method, url, headers, body=body, timeout=args.timeout, system="task_tracker", max_retries=0,
        )
        emit({
            "command": f"link.{args.action}", "dryRun": False, "request": request_info,
            "response": {"status": status, "headers": safe_response_headers(response_headers), "body": response},
        })
        return 0
    raise SystemExit(f"Unknown link action: {args.action}")


def _flatten_user_logins(user_args: Sequence[str]) -> List[str]:
    """Allow --user u1 --user "u2,u3" to expand into ["u1","u2","u3"]."""
    logins: List[str] = []
    for raw in user_args or []:
        for chunk in str(raw).split(","):
            login = chunk.strip()
            if login and login not in logins:
                logins.append(login)
    return logins


def login_from_basic_auth() -> str:
    """Login half of SWTR_BASIC_AUTH ("" when unset or not a ``login:password`` pair).

    main() calls load_env_file() before dispatching, so a credential that
    exists only in ~/.swtr-review.env is already visible in the environment
    here. Only the login half is read; the password half is never touched,
    stored, or printed — the login itself is not a secret.

    The colon is required, not optional: a value without one is not a Basic
    credential at all but a bare token pasted into the wrong variable, and
    ``partition`` would hand the whole secret back as if it were a login — into
    a note, a suggestion and, from there, into the profile.
    """
    raw = os.environ.get("SWTR_BASIC_AUTH", "")
    if ":" not in raw:
        return ""
    return raw.partition(":")[0].strip()


def _resolve_me_login(profile: Any) -> Tuple[str, str]:
    """Login behind ``--me`` and where it came from: ``(login, source)``.

    Order: SWTR_LOGIN -> profile -> USER -> login half of SWTR_BASIC_AUTH. The
    profile sits after the tracker's own variable (an explicit choice) and
    before the OS account name (a guess that does not match the tracker login
    for half of the users); the tail is unchanged, so a user without a profile
    sees no difference. Sources follow the defaults_applied vocabulary:
    ``env``, ``profile``, ``os_user``, ``credential``. ``("", "")`` when no
    source supplied a login.
    """
    login = (os.environ.get("SWTR_LOGIN") or "").strip()
    if login:
        return login, "env"
    login = str(profile_get(profile, "login") or "").strip()
    if login:
        return login, "profile"
    login = (os.environ.get("USER") or "").strip()
    if login:
        return login, "os_user"
    login = login_from_basic_auth()
    if login:
        return login, "credential"
    return "", ""


def _resolve_assignees(user_args: Sequence[str], use_me: bool, profile: Any = None,
                       applied: Optional[DefaultsApplied] = None) -> List[str]:
    """Assignee logins; with ``--me`` the resolved login goes first and its source into ``applied``."""
    logins = _flatten_user_logins(user_args)
    if use_me:
        login, source = _resolve_me_login(profile)
        if not login:
            raise SystemExit(
                "--me requires SWTR_LOGIN, USER, or the login half of SWTR_BASIC_AUTH; "
                "or put the login into the profile (%s) with /guru-start, or pass --user LOGIN."
                % PROFILE_PATH_HINT)
        if applied is not None:
            applied.add("login", login, source)
        if login not in logins:
            logins.insert(0, login)
    if not logins:
        raise SystemExit("Pass at least one --user LOGIN, repeat the flag, or use --me.")
    return logins


def find_users(base_url: str, logins: Sequence[str], timeout: float) -> List[Dict[str, Any]]:
    """Users behind the given logins (empty list when none matched).

    Тело запроса — голый JSON-массив, а не объект: объектное тело этот endpoint
    отвергает с `HttpMessageNotReadableException`. Матч идёт по точному логину в
    нижнем регистре, поэтому вызывающий обязан привести значения сам.
    """
    headers = build_headers("task_tracker", require_auth=True)
    url = base_url.rstrip("/") + USER_FIND_ENDPOINT
    payload, _, _ = request_http(
        "POST", url, headers, body=list(logins), timeout=timeout, system="task_tracker")
    return payload if isinstance(payload, list) else []


def _titlecased_external_id(login: str) -> str:
    """`savinov.m.y` -> `Savinov.M.Y` — запасной вариант, если резолв не сработал."""
    return ".".join(part[:1].upper() + part[1:] if part else part for part in login.split("."))


def resolve_assignee_codes(
    base_url: str,
    logins: Sequence[str],
    timeout: float,
) -> Tuple[List[str], List[str]]:
    """Значения для фильтра `assigned_to` и заметки о том, как они получены.

    Возвращает `externalId` каждого логина, потому что фильтр сравнивает именно
    его (см. комментарий у USER_FIND_ENDPOINT). Логин, который сервер не знает,
    не выбрасывается: он идёт в фильтр как есть плюс title-case кандидат — так
    запрос работает и для служебных учёток, чей `externalId` равен логину, и
    когда пользователь сразу передал `--user Savinov.M.Y`. Сетевая ошибка резолва
    тоже не роняет команду: она превращается в заметку и тот же fallback, иначе
    диагностируемый запрос стал бы недоступен из-за необязательного шага.
    """
    notes: List[str] = []
    resolved: Dict[str, str] = {}
    wanted = [login.lower() for login in logins]
    if wanted:
        try:
            for user in find_users(base_url, wanted, timeout):
                login = str(user.get("login") or "").lower()
                code = str(user.get("code") or "").strip()
                if login and code:
                    resolved[login] = code
        except Exception as exc:  # noqa: BLE001 - резолв необязателен, см. docstring
            notes.append(
                "Не удалось сопоставить логины с externalId через %s (%s). "
                "Фильтр собран из переданных значений и их title-case вариантов."
                % (USER_FIND_ENDPOINT, redact_secrets(str(exc))))

    values: List[str] = []
    for login in logins:
        code = resolved.get(login.lower())
        if code:
            candidates = [code]
            if code != login:
                notes.append("Логин %s сопоставлен с externalId %s — фильтр идёт по нему." % (login, code))
        else:
            candidates = [login, _titlecased_external_id(login)]
            notes.append(
                "Логин %s не найден в справочнике пользователей: в фильтр добавлены значения %s. "
                "Если задачи не нашлись, передайте externalId явно (--user Familia.I.O)."
                % (login, ", ".join(dict.fromkeys(candidates))))
        for candidate in candidates:
            if candidate and candidate not in values:
                values.append(candidate)
    return values, notes


def _date_n_days_ago(days: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(days))).strftime("%Y-%m-%d")


def _tql_datetime(value: str) -> str:
    """Границу периода — в формате, который принимает атрибут типа `date_time`.

    TQL отвергает голую дату (`Некорректное значение '2026-05-26' для типа
    атрибута 'date_time'`) и вариант с пробелом, поэтому `YYYY-MM-DD` из `--since`
    и `--days` достраивается до начала суток по UTC. Значение, уже содержащее
    время, отдаётся как есть — пользователь мог передать точную границу.
    """
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text + "T00:00:00Z"
    return text


def _tql_quote(value: str) -> str:
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def _tql_in(values: Sequence[str]) -> str:
    return "(" + ", ".join(_tql_quote(v) for v in values) + ")"


# Категория статуса вместо перечня кодов. Коды статусов уникальны для каждой
# схемы workflow (в боевом тенанте встречаются и `closed`, и `в_очереди`, и
# `CLSD_YLquKLRWNLxhnnC`), поэтому список значений работает в одном пространстве
# и молча не находит ничего в соседнем. Категорий ровно три: pause/progress/done.
DONE_CATEGORY_TQL = 'statusCategory("done")'


def _tql_strip_strings(query: str) -> str:
    """Заменить содержимое кавычек пробелами, сохранив длину строки.

    Значение — не синтаксис: `summary ~ "закупка AND продажа"` не является
    смешением связок, а `"отчёт (годовой)"` не открывает логическую группу.
    """
    out = []
    quote = None
    escaped = False
    for char in query:
        if quote:
            out.append(" ")
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
                out[-1] = char
            continue
        if char in ('"', "'"):
            quote = char
            out.append(char)
            continue
        out.append(char)
    return "".join(out)


def validate_tql(query: str) -> List[str]:
    """Проблемы формы запроса, которые бэкенд либо отвергнет, либо посчитает молча неверно.

    Проверено вживую 13.08.2026: скобка логической группы даёт `HTTP 400`
    («Не распознан элемент '('»), а смешение AND и OR без скобок отвечает `200` с
    неверной выдачей. Второй случай ничем себя не выдаёт, поэтому ловится здесь.

    Скобки сами по себе законны: список `IN ("a", "b")` и вызов функции
    `statusCategory("done")`. Запрещена только скобка, открывающая группу условий.
    """
    problems: List[str] = []
    bare = _tql_strip_strings(query)

    for index, char in enumerate(bare):
        if char != "(":
            continue
        raw_prefix = bare[:index]
        prefix = raw_prefix.rstrip()
        if not prefix:
            problems.append("Запрос начинается со скобки: скобки логических групп TQL не поддерживает.")
            continue
        match = re.search(r"[A-Za-z_][A-Za-z0-9_]*$", prefix)
        token = match.group(0).upper() if match else ""
        # Легальны только два владельца скобки:
        #   * список значений — `IN ("a", "b")` и `NOT IN (...)`, пробел допустим;
        #   * вызов функции — имя вплотную к скобке, `statusCategory("done")`.
        # Именно вплотную: `AND (` — это уже логическая группа, а не функция.
        if token == "IN":
            continue
        if raw_prefix == prefix and token and token not in ("AND", "OR", "NOT"):
            continue
        problems.append(
            "Скобка логической группы: бэкенд отвергает такой запрос с HTTP 400 "
            "«Не распознан элемент '('». Уберите скобки — вложенности в TQL нет."
        )

    tokens = {t.upper() for t in re.findall(r"[A-Za-z]+", bare)}
    if "AND" in tokens and "OR" in tokens:
        problems.append(
            "В запросе одновременно AND и OR. Это самый опасный случай: ошибки не будет, "
            "бэкенд ответит 200 и тихо вернёт неверную выборку. Оставьте один тип связки "
            "или перечислите варианты одного поля через IN."
        )
    return problems


def build_assigned_tql(
    assignee_values: Sequence[str],
    *,
    status: str,
    spaces: Sequence[str],
    suits: Sequence[str],
) -> str:
    parts = [f"{ASSIGNEE_ATTRIBUTE_CODE} in {_tql_in(assignee_values)}"]
    mode = (status or "all").lower()
    if mode == "in-progress":
        parts.append(f"{WORKFLOW_STATUS_ATTRIBUTE_CODE} not in {DONE_CATEGORY_TQL}")
    elif mode not in ("", "all"):
        parts.append(f"{WORKFLOW_STATUS_ATTRIBUTE_CODE} = {_tql_quote(status)}")
    if spaces:
        parts.append("space in " + _tql_in(spaces))
    if suits:
        parts.append("suit in " + _tql_in(suits))
    return " and ".join(parts)


def build_done_weekly_tql(
    assignee_values: Sequence[str],
    *,
    since: str,
    done_status: Sequence[str],
    spaces: Sequence[str],
) -> str:
    explicit = [str(v).strip() for v in (done_status or []) if str(v).strip()]
    done_condition = (
        f"{WORKFLOW_STATUS_ATTRIBUTE_CODE} in {_tql_in(explicit)}"
        if explicit
        else f"{WORKFLOW_STATUS_ATTRIBUTE_CODE} in {DONE_CATEGORY_TQL}"
    )
    parts = [
        f"{ASSIGNEE_ATTRIBUTE_CODE} in {_tql_in(assignee_values)}",
        done_condition,
        f"updatedAt >= {_tql_quote(_tql_datetime(since))}",
    ]
    if spaces:
        parts.append("space in " + _tql_in(spaces))
    return " and ".join(parts)


def find_units_tql(
    base_url: str,
    *,
    query: str,
    return_attributes: Sequence[str],
    page: int,
    size: int,
    timeout: float,
) -> Any:
    body = {
        "query": query,
        "attributes": list(dict.fromkeys(return_attributes)),
        "page": {"page": page, "size": size},
    }
    return post_json(base_url, TQL_FIND_ENDPOINT, body, timeout)


# ---- TMS subcommand ----
#
# Every TMS scenario is described declaratively in ``TMS_KINDS`` and executed by
# the single generic ``cmd_tms`` below. Each entry is a ``TmsKind`` describing:
#   * ``method``   -- "GET" or "POST".
#   * ``endpoint`` -- a str (static path) or a callable(args) -> str for paths
#                     that embed a code (``folder``/``unit``/``run``) or a
#                     ``--with-progress`` suffix.
#   * ``required`` -- optional (arg_attr, message): if ``getattr(args, attr)`` is
#                     falsy, raise ``SystemExit(message)`` -- the exact message the
#                     old per-branch code raised.
#   * ``body``     -- for POST kinds, a callable(args) -> dict request body. The
#                     emitted ``data`` is ``{"request": body, "response": <api>}``.
#                     GET kinds have no body and emit ``data`` = the raw response.
#   * ``catch_errors`` -- GET only: wrap a failing call as ``{"error": <redacted>}``
#                     (with rc 0) instead of surfacing it via ``emit_error``.
# Optional params consumed by the builders: ``--attribute`` (return attributes;
# defaults per kind), ``--page``/``--limit`` (paging), ``--type`` (folder/unit
# type), ``--linked-to`` (test-plans, folder-root-units/progress), ``--from-date``/
# ``--to-date`` (integration).


def _tms_page(args: argparse.Namespace) -> Dict[str, Any]:
    return {"page": args.page, "size": args.limit}


def _tms_unit_filters(args: argparse.Namespace, default_attributes: Sequence[str]) -> Dict[str, Any]:
    return {
        "attributes": list(args.attribute or default_attributes),
        "page": _tms_page(args),
    }


def _tms_root_request(args: argparse.Namespace) -> Dict[str, Any]:
    return {"type": (args.type or "TEST_CASE").upper(), "spaceId": {"code": args.space[0]}}


def _tms_body_test_plans(args: argparse.Namespace) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "unitFilters": {
            "filters": {"spaces": list(args.space)},
            "attributes": list(args.attribute or TMS_DEFAULT_PLAN_ATTRIBUTES),
            "page": _tms_page(args),
        }
    }
    if args.linked_to:
        body["linkedTo"] = {"code": args.linked_to}
    return body


def _tms_body_plan_cycles(args: argparse.Namespace) -> Dict[str, Any]:
    return {"unitId": {"code": args.plan}, "unitFilters": _tms_unit_filters(args, TMS_DEFAULT_CYCLE_ATTRIBUTES)}


def _tms_body_cycle_cases(args: argparse.Namespace) -> Dict[str, Any]:
    return {"unitId": {"code": args.cycle}, "unitFilters": _tms_unit_filters(args, TMS_DEFAULT_CASE_ATTRIBUTES)}


def _tms_body_folder_root_units(args: argparse.Namespace) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "getRootFolderRq": _tms_root_request(args),
        "unitFilters": _tms_unit_filters(args, TMS_DEFAULT_CASE_ATTRIBUTES),
    }
    if args.linked_to:
        body["linkedTo"] = {"code": args.linked_to}
    return body


def _tms_body_folder_hierarchy(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "filters": {},
        "attributes": list(args.attribute or TMS_DEFAULT_CASE_ATTRIBUTES),
        "page": _tms_page(args),
    }


def _tms_body_integration(args: argparse.Namespace) -> Dict[str, Any]:
    # POST /integration/units with SPUnitSearchRq. `from` is a required
    # date-time field; `type`/`attributes`/`page` are also part of the request
    # body. This endpoint does not accept a GET.
    body: Dict[str, Any] = {
        "type": (args.type or "TEST_CASE").upper(),
        "attributes": list(args.attribute or []),
        "from": args.from_date or "2000-01-01T00:00:00.000Z",
        "page": _tms_page(args),
    }
    if args.to_date:
        body["to"] = args.to_date
    return body


def _tms_endpoint_folder_hierarchy(args: argparse.Namespace) -> str:
    endpoint = f"{TMS_BASE}/folder/hierarchy/{quote_path_part(args.folder)}/units"
    return endpoint + "/progress" if args.with_progress else endpoint


# Declaration order also drives the `tms` subparser `choices`/help text, so keep
# it aligned with the historical kind ordering.
TMS_KINDS: Dict[str, Dict[str, Any]] = {
    "test-plans": {
        "method": "POST",
        "endpoint": f"{TMS_BASE}/test_plans",
        "required": ("space", "tms test-plans requires --space SPACE."),
        "body": _tms_body_test_plans,
    },
    "plan-cycles": {
        "method": "POST",
        "endpoint": f"{TMS_BASE}/link/test_plan/test_cycles",
        "required": ("plan", "tms plan-cycles requires --plan TEST-PLAN-CODE."),
        "body": _tms_body_plan_cycles,
    },
    "cycle-cases": {
        "method": "POST",
        "endpoint": f"{TMS_BASE}/link/test_cycle/test_cases",
        "required": ("cycle", "tms cycle-cases requires --cycle TEST-CYCLE-CODE."),
        "body": _tms_body_cycle_cases,
    },
    "folder-root": {
        "method": "POST",
        "endpoint": f"{TMS_BASE}/folder/root",
        "required": ("space", "tms folder-root requires --space SPACE."),
        "body": _tms_root_request,
    },
    "folder-root-units": {
        "method": "POST",
        "endpoint": f"{TMS_BASE}/folder/root/units",
        "required": ("space", "tms folder-root-units requires --space SPACE."),
        "body": _tms_body_folder_root_units,
    },
    "folder-root-progress": {
        "method": "POST",
        "endpoint": f"{TMS_BASE}/folder/root/units/progress",
        "required": ("space", "tms folder-root-progress requires --space SPACE."),
        "body": _tms_body_folder_root_units,
    },
    "folder-hierarchy": {
        "method": "POST",
        "endpoint": _tms_endpoint_folder_hierarchy,
        "required": ("folder", "tms folder-hierarchy requires --folder FOLDER_CODE."),
        "body": _tms_body_folder_hierarchy,
    },
    "test-case": {
        "method": "GET",
        "endpoint": lambda args: f"{TMS_BASE}/test_case/{quote_path_part(args.unit)}/test_case_data",
        "required": ("unit", "tms test-case requires --unit UNIT_KEY."),
        "catch_errors": True,
    },
    "run-info": {
        "method": "GET",
        "endpoint": lambda args: f"{TMS_BASE}/run/{quote_path_part(args.run)}/test_step_info",
        "required": ("run", "tms run-info requires --run RUN_CODE."),
    },
    "integration": {
        "method": "POST",
        "endpoint": f"{TMS_BASE}/integration/units",
        "body": _tms_body_integration,
    },
    "acceptable-spaces": {
        "method": "GET",
        "endpoint": f"{TMS_BASE}/test_plans/acceptable_spaces",
    },
}


def cmd_tms(args: argparse.Namespace) -> int:
    kind = args.kind
    spec = TMS_KINDS[kind]  # kind is constrained to TMS_KINDS keys by argparse choices

    required = spec.get("required")
    if required:
        attr, message = required
        if not getattr(args, attr):
            raise SystemExit(message)

    endpoint_spec = spec["endpoint"]
    endpoint = endpoint_spec(args) if callable(endpoint_spec) else endpoint_spec

    if spec["method"] == "GET":
        if spec.get("catch_errors"):
            try:
                data: Any = get_json(args.base_url, endpoint, args.timeout)
            except RuntimeError as exc:
                # test-case-not-found (404) stays a graceful data.error at exit 0;
                # an auth failure (401/403) is fatal and re-raised to the contract.
                if is_auth_error(exc):
                    raise
                data = {"error": redact_secrets(str(exc))}
        else:
            data = get_json(args.base_url, endpoint, args.timeout)
    else:
        body = spec["body"](args)
        data = {"request": body, "response": post_json(args.base_url, endpoint, body, args.timeout)}

    emit({"command": "tms", "kind": kind, "endpoint": endpoint, "data": data})
    return 0


# ---- assigned subcommand ----


def cmd_assigned(args: argparse.Namespace) -> int:
    # Только TQL. Атрибутный фильтр `/rest/api/unit/v3/find` по `assigned_to`
    # не находит ничего ни в одном формате значения (логин, externalId, объект
    # пользователя, ФИО), хотя тот же запрос без фильтра юниты отдаёт, — а
    # `/rest/api/unit/v3/find/tql` с тем же условием находит. Раньше сюда
    # попадал только `--status in-progress`, поэтому `assigned --me` всегда
    # возвращал пустой список.
    applied = args.defaults_applied
    logins = _resolve_assignees(args.user, args.me, profile=args.profile, applied=applied)
    values, notes = resolve_assignee_codes(args.base_url, logins, args.timeout)
    spaces = _resolve_scope_spaces(args)
    status_mode = (args.status or "all").lower()
    return_attributes = list(args.return_attribute or ASSIGNED_RETURN_ATTRIBUTES)

    tql_query = build_assigned_tql(
        values, status=status_mode, spaces=spaces, suits=args.suit
    )
    if status_mode == "in-progress":
        notes.append(
            "in-progress — это `workflow_status not in statusCategory(\"done\")`: категория переносима "
            "между пространствами, коды статусов — нет (проверено 13.08.2026)."
        )
        if not args.suit:
            notes.append(
                "Отрицательная форма statusCategory забирает и юниты без workflow: в выдачу попадут "
                "вики-страницы пространства. Сузьте `--suit`, если нужны только задачи."
            )
    elif status_mode not in ("", "all"):
        notes.append(
            f"Фильтр по статусу: `{WORKFLOW_STATUS_ATTRIBUTE_CODE} = \"{status_mode}\"`. "
            "Если значение не подходит для пространства, измените `--status` или используйте `--status all`."
        )

    notes += _origin_notes(applied)

    response = find_units_tql(
        args.base_url,
        query=tql_query,
        return_attributes=return_attributes,
        page=args.page,
        size=args.limit,
        timeout=args.timeout,
    )

    emit(args_with_defaults({
        "command": "assigned",
        "users": logins,
        "assigneeValues": values,
        "status": status_mode,
        "spaces": list(spaces),
        "suits": list(args.suit),
        "endpoint": TQL_FIND_ENDPOINT,
        "tql": tql_query,
        "request": {
            "query": tql_query,
            "attributes": return_attributes,
            "page": {"page": args.page, "size": args.limit},
        },
        "notes": notes,
        "response": response,
    }, args))
    return 0


# ---- done-weekly subcommand ----


def cmd_done_weekly(args: argparse.Namespace) -> int:
    applied = args.defaults_applied
    logins = _resolve_assignees(args.user, args.me, profile=args.profile, applied=applied)
    # Тот же резолв логина в externalId, что и в `assigned`: фильтр `assigned_to`
    # сравнивает externalId с учётом регистра, поэтому по логину отчёт был пустым.
    values, resolve_notes = resolve_assignee_codes(args.base_url, logins, args.timeout)
    spaces = _resolve_scope_spaces(args)
    since = args.since or _date_n_days_ago(args.days)
    done_values = [v.strip() for v in (args.done_status or []) if str(v).strip()]
    return_attributes = list(args.return_attribute or ASSIGNED_RETURN_ATTRIBUTES)

    query = build_done_weekly_tql(
        values, since=since, done_status=done_values, spaces=spaces
    )
    notes = list(resolve_notes) + [f"Период: с {since} (включительно)."]
    notes.append(
        "Завершённость определяется категорией `statusCategory(\"done\")` — она переносима между "
        "пространствами, в отличие от кодов статусов. Явный перечень задаётся `--done-status`."
        if not done_values
        else f"Завершённость определяется явным перечнем статусов: {done_values}."
    )
    notes.append(
        "Границей окна служит `updatedAt`: честная дата завершения `resolved_at` на стенде не "
        "заполняется (проверено 13.08.2026 в CRPV, PVM, STS, CORESUP), поэтому в выдачу попадают и "
        "задачи, закрытые раньше окна, но отредактированные внутри него."
    )
    notes += _origin_notes(applied)
    response = find_units_tql(
        args.base_url,
        query=query,
        return_attributes=return_attributes,
        page=args.page,
        size=args.limit,
        timeout=args.timeout,
    )
    emit(args_with_defaults({
        "command": "done-weekly",
        "users": logins,
        "assigneeValues": values,
        "since": since,
        "doneStatus": done_values,
        "spaces": list(spaces),
        "endpoint": TQL_FIND_ENDPOINT,
        "tql": query,
        "notes": notes,
        "response": response,
    }, args))
    return 0


# ---- status subcommand ----

# Статус юнита — НЕ поле, а проекция конечного автомата. Записать значение в
# `workflow_status` нельзя: статус меняется выполнением ДЕЙСТВИЯ (перехода),
# доступного из текущего статуса, через отдельную ручку. Коды действий
# генерируются для каждой схемы workflow отдельно (`ZVRSHT_lMacNZCcxwjcw`),
# поэтому резолвятся в рантайме и никогда не хардкодятся: то же «Завершить» в
# другом space имеет другой код. Имя статуса кодом действия не является.
STATUS_GET_ENDPOINT = "/rest/api/workflow/v1/unit/{code}/status/get?conditionEnabled=true"
WORKFLOW_SCHEMA_ENDPOINT = "/rest/api/workflow/v1/workflow/{schema}"
STATUS_CHANGE_ENDPOINT = "/rest/api/workflow/v2/unit/status/change"
# Псевдопереход создания: в графе схемы у него `oldStatus: null`. На
# существующем юните неприменим, поэтому в поиске пути не участвует.
INIT_ACTION_CODE = "initAction"
# Предел длины маршрута. Смысл не в производительности, а в защите от
# бессмысленного пути: цель в семь переходов от текущего статуса почти всегда
# означает, что цель выбрана неверно.
STATUS_MAX_HOPS = 6


def status_change_hint(error: Exception) -> Optional[str]:
    """Расшифровка отказа на смене статуса. Проверено живьём 11.08.2026 на STS-452231.

    Коды у двух разных ошибок разные, и это единственный способ отличить «не тот
    переход» от «не та ручка»:
      * 570 — ответ САМОЙ ручки смены статуса: действие неприменимо из текущего
        статуса, кода нет в схеме, либо у перехода не прошёл валидатор;
      * 500 «Что-то пошло не так» — так отвечает обновление юнита, когда
        `workflow_status` пришёл в него как обычное поле. Статус полем не пишется.
    """
    message = str(error)
    if "HTTP 570" in message:
        return ("Трекер отвечает 570, когда действие неприменимо из текущего статуса, "
                "кода действия нет в схеме workflow либо у перехода не прошёл валидатор. "
                "Годятся только коды из availableActions команды `status get <код>`; "
                "имя статуса (RESOLVED) кодом действия НЕ является.")
    if "HTTP 500" in message:
        return ("HTTP 500 на смене статуса означает, что запрос ушёл не в ручку перехода, "
                "а в обновление юнита с `workflow_status` в теле. Статус — проекция "
                "автомата, а не поле: писать только через "
                f"PATCH {STATUS_CHANGE_ENDPOINT} с actionCode.")
    return None


def _wf_node(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _wf_row(value: Any) -> Dict[str, Any]:
    node = _wf_node(value)
    return {"code": node.get("code"), "name": node.get("name")}


def fetch_unit_status(base_url: str, code: str, timeout: float) -> Dict[str, Any]:
    """Живое состояние автомата: текущий статус и действия, доступные сейчас."""
    endpoint = STATUS_GET_ENDPOINT.format(code=quote_path_part(code))
    return _wf_node(get_json(base_url, endpoint, timeout))


def summarize_status_state(payload: Dict[str, Any]) -> Dict[str, Any]:
    current = _wf_node(payload.get("currentStatus"))
    return {
        "currentStatus": _wf_row(current),
        "statusType": _wf_node(current.get("statusType")).get("name"),
        "workflow": _wf_row(payload.get("wfSchemaDto")),
        "availableActions": [_wf_row(a) for a in payload.get("availableActions") or []],
        # Действие может существовать и быть закрыто условием (права, незаполненные
        # поля). Это не то же самое, что «действия нет в графе»: разные причины —
        # разные исправления, поэтому в выводе они разделены.
        "actionsExcludedByCondition": [
            _wf_row(a) for a in payload.get("actionsExcludedByCondition") or []
        ],
    }


def fetch_workflow_transitions(base_url: str, schema_code: str, timeout: float) -> List[Dict[str, Any]]:
    endpoint = WORKFLOW_SCHEMA_ENDPOINT.format(schema=quote_path_part(schema_code))
    rows = _wf_node(get_json(base_url, endpoint, timeout)).get("wfTransitions")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def status_catalog(transitions: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """{код статуса: имя} по всем статусам, встреченным в переходах схемы."""
    catalog: Dict[str, str] = {}
    for row in transitions:
        for key in ("oldStatus", "destStatus"):
            node = _wf_node(row.get(key))
            code = node.get("code")
            if code:
                catalog[str(code)] = str(node.get("name") or code)
    return catalog


def resolve_status_code(transitions: Sequence[Dict[str, Any]], wanted: str) -> List[str]:
    """Коды статусов, подходящих под запрос: код точно либо имя без учёта регистра.

    Пусто — цели нет в схеме; больше одного — имя неоднозначно, нужен код.
    """
    catalog = status_catalog(transitions)
    if wanted in catalog:
        return [wanted]
    needle = str(wanted).strip().casefold()
    return sorted(code for code, name in catalog.items() if name.strip().casefold() == needle)


def resolve_available_action(actions: Sequence[Dict[str, Any]], wanted: str) -> List[Dict[str, Any]]:
    """Действия из availableActions, подходящие под код или имя."""
    exact = [row for row in actions if row.get("code") == wanted]
    if exact:
        return exact
    needle = str(wanted).strip().casefold()
    return [row for row in actions if str(row.get("name") or "").strip().casefold() == needle]


def build_status_edges(transitions: Sequence[Dict[str, Any]]) -> Dict[str, List[Tuple[str, str, str]]]:
    """{код статуса: [(имя действия, код действия, код целевого статуса)]}."""
    edges: Dict[str, List[Tuple[str, str, str]]] = {}
    for row in transitions:
        old = _wf_node(row.get("oldStatus"))
        dest = _wf_node(row.get("destStatus"))
        action = _wf_node(row.get("wfAction"))
        # oldStatus=null — переход создания; на существующем юните его нет.
        if not (old.get("code") and dest.get("code") and action.get("code")):
            continue
        if str(action.get("code")) == INIT_ACTION_CODE:
            continue
        edges.setdefault(str(old["code"]), []).append(
            (str(action.get("name") or action["code"]), str(action["code"]), str(dest["code"])))
    return edges


def shortest_status_path(
    transitions: Sequence[Dict[str, Any]],
    start_code: str,
    target_code: str,
    max_hops: int = STATUS_MAX_HOPS,
) -> Optional[List[Dict[str, str]]]:
    """Кратчайший маршрут переходов start -> target, или None если недостижим.

    Обход в ширину по графу схемы. Рёбра перебираются в отсортированном порядке,
    поэтому из двух маршрутов одинаковой длины всегда выбирается один и тот же.
    Пустой список — старт уже равен цели.
    """
    names = status_catalog(transitions)
    edges = build_status_edges(transitions)
    queue = deque([(start_code, [])])
    seen = {start_code}
    while queue:
        node, path = queue.popleft()
        if node == target_code:
            return path
        if len(path) >= max_hops:
            continue
        for action_name, action_code, dest in sorted(edges.get(node, [])):
            if dest in seen:
                continue
            seen.add(dest)
            queue.append((dest, path + [{
                "from": node,
                "fromName": names.get(node, node),
                "action": action_name,
                "actionCode": action_code,
                "to": dest,
                "toName": names.get(dest, dest),
            }]))
    return None


def cmd_status(args: argparse.Namespace) -> int:
    base = args.base_url.rstrip("/")
    state = fetch_unit_status(args.base_url, args.code, args.timeout)
    summary = summarize_status_state(state)
    if args.action == "get":
        emit({"command": "status.get", "unit": args.code, **summary, "response": state})
        return 0

    if bool(args.to) == bool(args.action_code):
        raise SystemExit("status set requires exactly one of --to STATUS or --action ACTION.")

    schema_code = summary["workflow"].get("code")
    if not schema_code:
        raise RuntimeError(f"Unit {args.code} has no workflow schema: nothing to transition.")
    transitions = fetch_workflow_transitions(base, str(schema_code), args.timeout)
    current_code = summary["currentStatus"].get("code")

    if args.to:
        matches = resolve_status_code(transitions, args.to)
        if not matches:
            return emit_error(
                "status.set", RuntimeError(f"Status {args.to!r} is not part of this workflow."),
                unit=args.code, knownStatuses=sorted(set(status_catalog(transitions).values())),
                **summary)
        if len(matches) > 1:
            return emit_error(
                "status.set", RuntimeError(f"Status name {args.to!r} is ambiguous: pass one of these codes."),
                unit=args.code, candidates=matches, **summary)
        target = matches[0]
        if target == current_code:
            emit({"command": "status.set", "unit": args.code, "target": args.to,
                  "plan": [], "hops": 0, "note": "уже в целевом статусе, делать нечего", **summary})
            return 0
        path = shortest_status_path(transitions, str(current_code), target)
        if path is None:
            return emit_error(
                "status.set",
                RuntimeError(f"No route from {summary['currentStatus'].get('name')} to {args.to} "
                             f"within {STATUS_MAX_HOPS} transitions."),
                unit=args.code, **summary)
    else:
        chosen = resolve_available_action(summary["availableActions"], str(args.action_code))
        if len(chosen) != 1:
            return emit_error(
                "status.set",
                RuntimeError(f"Action {args.action_code!r} is not available from "
                             f"{summary['currentStatus'].get('name')}."),
                unit=args.code, matched=chosen, **summary)
        action_code = str(chosen[0].get("code"))
        names = status_catalog(transitions)
        step = next(
            ({"from": str(current_code), "fromName": names.get(str(current_code), str(current_code)),
              "action": name, "actionCode": code, "to": dest, "toName": names.get(dest, dest)}
             for name, code, dest in build_status_edges(transitions).get(str(current_code), [])
             if code == action_code),
            None)
        if step is None:
            raise RuntimeError(f"Action {action_code} is offered for {args.code} but absent from the schema graph.")
        path = [step]

    url = base + STATUS_CHANGE_ENDPOINT
    result: Dict[str, Any] = {
        "command": "status.set", "unit": args.code, "target": args.to or args.action_code,
        "plan": path, "hops": len(path), "endpoint": STATUS_CHANGE_ENDPOINT,
    }
    if not args.execute:
        result["dryRun"] = True
        result["request"] = {
            "method": "PATCH", "url": url,
            "headers": safe_headers_for_log(build_headers("task_tracker", require_auth=False)),
            "bodies": [{"unitCode": args.code, "actionCode": step["actionCode"]} for step in path],
        }
        emit({**result, **summary})
        return 0

    headers = build_headers("task_tracker", require_auth=True)
    performed: List[Dict[str, Any]] = []
    for step in path:
        # Перечитываем состояние перед КАЖДЫМ шагом: доступность действия
        # вычисляется от живого статуса, а условие могло закрыться. Локальная
        # проверка превращает невнятный HTTP 500 трекера в понятную ошибку.
        live = summarize_status_state(fetch_unit_status(base, args.code, args.timeout))
        if live["currentStatus"].get("code") != step["from"]:
            return emit_error(
                "status.set",
                RuntimeError(f"Unit moved to {live['currentStatus'].get('name')} mid-route; "
                             f"step expected {step['fromName']}. Re-run to re-plan."),
                unit=args.code, performed=performed, plan=path, **live)
        if step["actionCode"] not in {row.get("code") for row in live["availableActions"]}:
            return emit_error(
                "status.set",
                RuntimeError(f"Action {step['action']} ({step['actionCode']}) is not available from "
                             f"{live['currentStatus'].get('name')}."),
                unit=args.code, performed=performed, plan=path,
                hint=("Если действие видно в actionsExcludedByCondition — оно есть в схеме, но "
                      "закрыто условием: не хватает прав либо не заполнены поля, которые требует "
                      "переход. Заполнить их надо ДО смены статуса."),
                **live)
        body = {"unitCode": args.code, "actionCode": step["actionCode"]}
        try:
            # Переход не идемпотентен: повтор увёл бы юнит на статус дальше.
            response, response_headers, http_status = request_http(
                "PATCH", url, headers, body=body, timeout=args.timeout,
                system="task_tracker", max_retries=0,
            )
        except RuntimeError as exc:
            return emit_error("status.set", exc, unit=args.code, performed=performed,
                              plan=path, failedStep=step, hint=status_change_hint(exc), **live)
        performed.append({
            "step": step, "status": http_status,
            "headers": safe_response_headers(response_headers), "body": response,
        })
    final = summarize_status_state(fetch_unit_status(base, args.code, args.timeout))
    emit({**result, "dryRun": False, "performed": performed, **final})
    return 0


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default=None,
                        help="Override SWTR_BASE_URL (resolved after the env file loads; default https://portal.works.prod.sbt/swtr)")
    parser.add_argument("--env-file", default="~/.swtr-review.env")
    parser.add_argument("--no-profile", action="store_true",
                        help="Do not apply defaults from the user profile (%s): login for --me, spaces, "
                             "suit/product/components of create. Behaves as if there were no profile."
                             % PROFILE_PATH_HINT)
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds (default 30)")


def add_test_case_args(parser: argparse.ArgumentParser) -> None:
    """Поля тест-кейса, у которых форма записи отличается от формы чтения.

    Одинаковый набор у create и update: кейс заводится и правится одними и теми
    же полями, а расхождение флагов между командами и есть та причина, по которой
    правку начинают делать руками в UI.
    """
    parser.add_argument("--step", action="append", default=[], metavar="СПЕЦ",
                        help="Test case step as 'action|data|expected result'; repeatable. "
                             "Escape a literal separator as '\\|'.")
    parser.add_argument("--steps-file", dest="steps_file", metavar="PATH",
                        help="Test case steps as JSON (or - for stdin): an array of "
                             "{description, data, result}, an array of steps read back from the "
                             "tracker, or a whole {testStepList: [...]} wrapper copied from "
                             "another case")
    parser.add_argument("--precondition", metavar="ТЕКСТ",
                        help="Test case precondition as plain text or markdown")


def add_search_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--space", action="append", default=[], help="Limit search to a SWTR space; can be repeated (default: spaces of the profile)")
    parser.add_argument("--suit", action="append", default=[], help="Limit search to a unit suit/type; can be repeated")
    parser.add_argument("--attribute-code", action="append", default=[], help="Attribute code to search by LIKE; can be repeated")
    parser.add_argument("--return-attribute", action="append", default=[],
                        help="Attribute code to include in unit result; can be repeated. "
                             "If omitted, a default set is used; if given, it REPLACES the defaults.")
    parser.add_argument("--limit", type=int, default=20, help="Page size (default 20)")
    parser.add_argument("--page", type=int, default=0,
                        help="Page number, 0-based (SWTR API-native; the SWTR APIs count pages from 0)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="High-level SWTR Task Tracker CLI. All commands print JSON to stdout.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_get = sub.add_parser("get", help="Fetch one task/wiki page by code/url, or lookup candidates by title text")
    p_get.add_argument("target")
    p_get.add_argument("--skip-wiki", action="store_true")
    add_common_args(p_get)
    add_search_args(p_get)

    p_search = sub.add_parser("search", help="Search tasks and wiki pages by query")
    p_search.add_argument("query")
    p_search.add_argument("--no-wiki", action="store_true")
    p_search.add_argument("--common-attributes", action="store_true", help="Also try a safe set of common attribute codes")
    add_common_args(p_search)
    add_search_args(p_search)

    p_count = sub.add_parser(
        "count",
        help="Count units matching OR-property filter via /rest/api/unit/v3/count/composite",
    )
    p_count.add_argument("query")
    p_count.add_argument("--space", action="append", default=[],
                     help="SWTR space; can be repeated (default: spaces of the profile)")
    p_count.add_argument("--suit", action="append", default=[], help="Suit/type; can be repeated")
    p_count.add_argument("--extra-or", help="JSON array of additional QLPropertyFilter entries to merge into properties.or")
    p_count.add_argument("--extra-and", help="JSON array of QLPropertyFilter entries for properties.and")
    p_count.add_argument("--tql-filter-id", action="append", default=[], help="Optional tqlFilterIds; can be repeated")
    add_common_args(p_count)

    p_sim = sub.add_parser("similar", help="Find tasks similar to a task code/url or free text")
    p_sim.add_argument("target")
    p_sim.add_argument("--term", action="append", default=[], help="Explicit search term; can be repeated")
    p_sim.add_argument("--terms-limit", type=int, default=8)
    p_sim.add_argument("--per-term-limit", type=int, default=20)
    add_common_args(p_sim)
    add_search_args(p_sim)

    p_create = sub.add_parser("create", help="Prepare or execute creation of a SWTR task")
    # Умолчание типа живёт в коде (DEFAULT_CREATE_SUIT), не в парсере: None
    # отличает «не задано» от явного значения, и профиль встаёт между ними.
    p_create.add_argument("--suit", default=None,
                          help="Unit type (default: task_tracker.default_suit from the profile, else task)")
    p_create.add_argument("--space", help="Space code (default: task_tracker.primary_space from the profile)")
    p_create.add_argument("--summary")
    p_create.add_argument("--description", default="")
    p_create.add_argument("--fix-version", action="append", default=[], dest="fix_version",
                          help="Fix Version by name or code; repeatable")
    p_create.add_argument("--affects-version", action="append", default=[], dest="affects_version",
                          help="Affects Version by name or code; repeatable")
    p_create.add_argument("--component", action="append", default=[], dest="component",
                          help="Component by name or code; repeatable "
                               "(default: task_tracker.components from the profile)")
    p_create.add_argument("--product", action="append", default=[], dest="product",
                          help="Product code by name or code (default: task_tracker.product from the profile)")
    p_create.add_argument("--attribute", action="append", default=[], help="Task attribute as key=value; can be repeated")
    p_create.add_argument("--no-default-workflow-status", action="store_true",
                          help="Do not inject workflow_status={\"command\":\"NEW\"} when the attribute is absent. "
                               "A suit with a workflow then answers HTTP 570.")
    p_create.add_argument("--body-file", help="Full JSON body for /rest/api/unit/v2/{suit}/create")
    p_create.add_argument("--wiki-body", dest="wiki_body", metavar="PATH",
                          help="Wiki page body: markdown file, ProseMirror JSON file, or - for stdin")
    p_create.add_argument("--parent", metavar="CODE",
                          help="Put the new wiki page under this page (link wiki_page_hierarchy). "
                               "Without it the page stays out of the wiki tree.")
    add_test_case_args(p_create)
    p_create.add_argument("--execute", action="store_true", help="Actually call POST. Without it only the request payload is printed.")
    add_common_args(p_create)

    # Пространство у suits/versions/directory/workflow может прийти из профиля,
    # поэтому argparse его не требует; отсутствие после резолва отдаёт
    # _apply_profile_space() через сабпарсер команды — прежними словами и кодом 2.
    p_suits = sub.add_parser("suits", help="List unit types (suits) available in a space")
    p_suits.add_argument("space", nargs="?", default=None,
                         help="Space code, e.g. PVM (default: task_tracker.primary_space from the profile)")
    p_suits.set_defaults(subparser=p_suits)
    add_common_args(p_suits)

    p_update = sub.add_parser(
        "update", help="Change attributes of an existing unit (write; dry-run by default)")
    p_update.add_argument("code", help="Unit code, e.g. PVM-175440")
    p_update.add_argument("--fix-version", action="append", default=[], dest="fix_version",
                          help="Fix Version by name or code; repeatable")
    p_update.add_argument("--affects-version", action="append", default=[], dest="affects_version",
                          help="Affects Version by name or code; repeatable")
    p_update.add_argument("--component", action="append", default=[], dest="component",
                          help="Component by name or code; repeatable")
    p_update.add_argument("--product", action="append", default=[], dest="product",
                          help="Product code by name or code")
    p_update.add_argument("--summary", help="New unit title (base field, not an attribute)")
    p_update.add_argument("--description", help="New unit description (base field)")
    p_update.add_argument("--wiki-body", dest="wiki_body", metavar="PATH",
                          help="Replace the wiki page body: markdown file, ProseMirror JSON file, "
                               "or - for stdin")
    p_update.add_argument("--attribute", action="append", default=[],
                          help="Raw attribute as code=value; value may be JSON; repeatable")
    add_test_case_args(p_update)
    p_update.add_argument("--execute", action="store_true",
                          help="Actually send the PATCH. Without it only the diff is printed.")
    add_common_args(p_update)

    p_attributes = sub.add_parser(
        "attributes", help="List attributes declared for a unit type, with their directories")
    p_attributes.add_argument("suit", help="Unit type code, e.g. task")
    p_attributes.add_argument("--query", default="", help="Substring filter over code and name")
    p_attributes.add_argument("--values-limit", type=int, default=40,
                              help="How many directory values to print per attribute (default 40); "
                                   "the rest is reported as valuesTruncated")
    add_common_args(p_attributes)

    p_directory = sub.add_parser(
        "directory", help="Values of a space directory: components, versions or product codes")
    p_directory.add_argument("--space",
                             help="Space code, e.g. PVM (default: task_tracker.primary_space from the profile)")
    p_directory.set_defaults(subparser=p_directory)
    p_directory.add_argument("--kind", required=True,
                             choices=["components", "versions", "product"],
                             help="Which directory to read")
    p_directory.add_argument("--suit", default="task",
                             help="Unit type; required for product codes (default: task)")
    p_directory.add_argument("--query", default="", help="Substring filter over value name")
    p_directory.add_argument("--limit", type=int, default=50, help="How many values to print")
    add_common_args(p_directory)

    p_spaces = sub.add_parser("spaces", help="List spaces available to the current user")
    p_spaces.add_argument("query", nargs="?", default="",
                          help="Optional substring filter over space code and name")
    add_common_args(p_spaces)

    p_filters = sub.add_parser("filters", help="List the current user's saved unit filters")
    add_common_args(p_filters)

    p_versions = sub.add_parser("versions", help="List versions declared in a space")
    p_versions.add_argument("space", nargs="?", default=None,
                            help="Space code, e.g. CRPV (default: task_tracker.primary_space from the profile)")
    p_versions.set_defaults(subparser=p_versions)
    p_versions.add_argument("--include-archived", action="store_true",
                            help="Keep archived versions in the output (hidden by default)")
    p_versions.add_argument("--page", type=int, default=0, help="Page number (0-based, default 0)")
    p_versions.add_argument("--limit", type=int, default=50, help="Page size (default 50)")
    add_common_args(p_versions)

    p_attach = sub.add_parser("attachments",
                              help="List files attached to a unit (read-only, no download)")
    p_attach.add_argument("code", help="Unit code or URL, e.g. CRPV-106611")
    add_common_args(p_attach)

    p_workflow = sub.add_parser(
        "workflow",
        help="Read the workflow schema of a (suit, space) pair without touching a unit")
    p_workflow.add_argument("--suit", required=True, help="Unit type code, e.g. epic_crpv")
    p_workflow.add_argument("--space",
                            help="Space code, e.g. CRPV (default: task_tracker.primary_space from the profile)")
    p_workflow.set_defaults(subparser=p_workflow)
    add_common_args(p_workflow)

    p_tql = sub.add_parser(
        "tql",
        help="Run a raw TQL query; the query form is validated locally before the call")
    p_tql.add_argument("query", help="TQL expression, e.g. 'space = \"PVM\" AND priority = \"high\"'")
    p_tql.add_argument("--force", action="store_true",
                       help="Send the query even if local validation objects")
    p_tql.add_argument("--return-attribute", action="append", default=[],
                       help="Attribute code to include in the result; can be repeated")
    p_tql.add_argument("--page", type=int, default=0, help="Page number (0-based, default 0)")
    p_tql.add_argument("--limit", type=int, default=20, help="Page size (default 20)")
    add_common_args(p_tql)

    p_link = sub.add_parser("link", help="Create / find / delete unit-to-unit links (Russian relation types)")
    p_link.add_argument("action", choices=["create", "find", "delete", "types"])
    p_link.add_argument("--source", help="Source unit code (create/delete)")
    p_link.add_argument("--destination", help="Destination unit code (create/delete)")
    p_link.add_argument("--type", dest="link_type", help="Link type code, e.g. realized_in / decomposition (create/delete)")
    p_link.add_argument("--unit", help="Unit code to search links for (find)")
    p_link.add_argument("--filter-type", action="append", default=[], help="Restrict find to these link type codes; repeatable")
    p_link.add_argument("--direction", choices=["source", "destination"], help="Restrict find to one direction")
    p_link.add_argument("--limit", type=int, default=50, help="find page size (default 50)")
    p_link.add_argument("--page", type=int, default=0, help="find page number, 0-based")
    p_link.add_argument("--no-resolve", action="store_true",
                        help="find: do not resolve link-type names and linked-unit summaries "
                             "(raw codes only, no extra calls)")
    p_link.add_argument("--execute", action="store_true", help="Actually POST/DELETE. Without it only the payload is printed.")
    add_common_args(p_link)

    p_status = sub.add_parser(
        "status",
        help="Read a unit's workflow state, or move it to a target status by transitions",
        description="Status is a state machine, not a field: `set` walks the transition graph "
                    "and resolves action codes at runtime. Without --execute it only prints the route.",
    )
    p_status.add_argument("action", choices=["get", "set"])
    p_status.add_argument("code", help="Unit code, e.g. STS-452231")
    p_status.add_argument("--to", help="set: target status name or code, e.g. RESOLVED")
    p_status.add_argument("--action", dest="action_code",
                          help="set: perform exactly one transition by action code or name "
                               "(escape hatch; must be in availableActions)")
    p_status.add_argument("--execute", action="store_true",
                          help="Actually PATCH the transitions. Without it only the route is printed.")
    add_common_args(p_status)

    # Single source of truth: the TMS_KINDS table drives both the executor and
    # the CLI choices/help. Its declaration order is the historical kind order.
    tms_kinds = list(TMS_KINDS)
    p_tms = sub.add_parser(
        "tms",
        help="Read-only Test Management System scenarios",
        description="TMS scenarios: " + ", ".join(tms_kinds),
    )
    p_tms.add_argument("kind", choices=tms_kinds, help="TMS scenario kind")
    p_tms.add_argument("--space", action="append", default=[], help="TMS space code; can be repeated")
    p_tms.add_argument("--linked-to", help="Filter test plans/units linked to this unit code")
    p_tms.add_argument("--plan", help="Test plan code (for plan-cycles)")
    p_tms.add_argument("--cycle", help="Test cycle code (for cycle-cases)")
    p_tms.add_argument("--folder", help="Folder code (for folder-hierarchy)")
    p_tms.add_argument("--unit", help="Unit/test-case key (for test-case)")
    p_tms.add_argument("--run", help="Run code (for run-info)")
    p_tms.add_argument("--type", help="Unit/folder type: TEST_CASE | TEST_CYCLE | TEST_CASE_RUN | ARCHIVE", default="TEST_CASE")
    p_tms.add_argument("--from-date", dest="from_date",
                       help="integration: created-from date-time (ISO 8601), e.g. 2024-01-01T00:00:00.000Z")
    p_tms.add_argument("--to-date", dest="to_date",
                       help="integration: optional created-to date-time (ISO 8601)")
    p_tms.add_argument("--with-progress", action="store_true",
                       help="For folder-hierarchy: append /progress endpoint")
    p_tms.add_argument("--attribute", action="append", default=[],
                       help="Attribute code to include in unit results; can be repeated")
    p_tms.add_argument("--limit", type=int, default=20, help="Page size (default 20)")
    p_tms.add_argument("--page", type=int, default=0,
                       help="Page number, 0-based (SWTR API-native)")
    add_common_args(p_tms)

    p_assigned = sub.add_parser("assigned", help="Tasks assigned to a user or list of users")
    p_assigned.add_argument("--user", action="append", default=[],
                            help="Assignee login; can be repeated or comma-separated")
    p_assigned.add_argument("--me", action="store_true",
                            help="Add your own login to the assignees list: SWTR_LOGIN, then the profile, then USER, "
                                 "then the login half of SWTR_BASIC_AUTH")
    p_assigned.add_argument("--space", action="append", default=[],
                            help="Limit to spaces; can be repeated (default: spaces of the profile)")
    p_assigned.add_argument("--suit", action="append", default=[],
                            help="Limit to suits/types; can be repeated")
    p_assigned.add_argument("--status", default="all",
                            help="Status mode: all | in-progress | <workflow_status value>")
    p_assigned.add_argument("--return-attribute", action="append", default=[],
                            help="Attribute code to include in result; can be repeated")
    p_assigned.add_argument("--limit", type=int, default=50, help="Page size (default 50)")
    p_assigned.add_argument("--page", type=int, default=0,
                            help="Page number, 0-based (SWTR API-native)")
    add_common_args(p_assigned)

    p_done = sub.add_parser("done-weekly", help="Tasks completed by a user/group in the last N days")
    p_done.add_argument("--user", action="append", default=[],
                        help="Assignee login; can be repeated or comma-separated")
    p_done.add_argument("--me", action="store_true",
                        help="Add your own login to the assignees list: SWTR_LOGIN, then the profile, then USER, "
                             "then the login half of SWTR_BASIC_AUTH")
    p_done.add_argument("--space", action="append", default=[],
                        help="Limit to spaces; can be repeated (default: spaces of the profile)")
    p_done.add_argument("--days", type=int, default=7,
                        help="Window in days (default: 7); ignored if --since is set")
    p_done.add_argument("--since",
                        help="Inclusive start date YYYY-MM-DD; overrides --days")
    p_done.add_argument("--done-status", action="append", default=[],
                        help="Explicit workflow_status codes that count as 'done'; can be repeated. "
                             "Default: none -- the portable statusCategory(\"done\") is used instead")
    p_done.add_argument("--return-attribute", action="append", default=[],
                        help="Attribute code to include in result; can be repeated")
    p_done.add_argument("--limit", type=int, default=100, help="Page size (default 100)")
    p_done.add_argument("--page", type=int, default=0,
                        help="Page number, 0-based (SWTR API-native)")
    add_common_args(p_done)

    return parser


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    load_env_file(Path(args.env_file).expanduser())
    # Resolve the base URL AFTER the env file is loaded so a SWTR_BASE_URL set
    # only in ~/.swtr-review.env is honored (an explicit --base-url still wins).
    if getattr(args, "base_url", None) in (None, ""):
        args.base_url = base_url_for("task_tracker")
    global LINK_BASE_URL
    LINK_BASE_URL = args.base_url
    # Профиль — одной точкой и ПОСЛЕ env-файла: в цепочке логина переменные
    # окружения стоят раньше профиля и к этому моменту уже должны быть видны.
    # --no-profile не читает файл вовсе. Команды читают args.profile и пишут
    # применённые умолчания в args.defaults_applied.
    attach_profile(args)
    _apply_profile_space(args)
    try:
        if args.command == "get":
            return cmd_get(args)
        if args.command == "search":
            return cmd_search(args)
        if args.command == "count":
            return cmd_count(args)
        if args.command == "similar":
            return cmd_similar(args)
        if args.command == "create":
            return cmd_create(args)
        if args.command == "suits":
            return cmd_suits(args)
        if args.command == "update":
            return cmd_update(args)
        if args.command == "attributes":
            return cmd_attributes(args)
        if args.command == "directory":
            return cmd_directory(args)
        if args.command == "spaces":
            return cmd_spaces(args)
        if args.command == "filters":
            return cmd_filters(args)
        if args.command == "versions":
            return cmd_versions(args)
        if args.command == "attachments":
            return cmd_attachments(args)
        if args.command == "workflow":
            return cmd_workflow(args)
        if args.command == "tql":
            return cmd_tql(args)
        if args.command == "link":
            return cmd_link(args)
        if args.command == "status":
            return cmd_status(args)
        if args.command == "tms":
            return cmd_tms(args)
        if args.command == "assigned":
            return cmd_assigned(args)
        if args.command == "done-weekly":
            return cmd_done_weekly(args)
        raise SystemExit(f"Unsupported command: {args.command}")
    except RuntimeError as exc:
        # Отказ трекера при значениях из профиля несёт их источник: недоступное
        # пространство или чужой продукт не расширяются и не подменяются молча.
        return emit_error(args.command, exc, **args_error_context(args))


if __name__ == "__main__":
    raise SystemExit(main())
