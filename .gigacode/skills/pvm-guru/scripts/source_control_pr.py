#!/usr/bin/env python3
"""Pull Request в SourceControl: посмотреть открытые и создать новый.

Печатает в stdout один JSON-объект. **Создание — единственная в навыке операция,
публикующая что-то наружу**, поэтому по умолчанию это dry-run: печатается тело
будущего запроса, а сеть не трогается. Реальная публикация — только `--execute`.

Эндпоинты — те же, что читает `guru-pr-review`:
  GET  /repos/{tenant}/{project}/{repo}/pulls
  POST /repos/{tenant}/{project}/{repo}/pulls
  POST /repos/{tenant}/{project}/{repo}/pulls/{index}/requested_reviewers

Ревью существующего PR — навык `guru-pr-review`. Здесь только создание.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _swtr_common import (  # noqa: E402
    PROFILE_PATH_HINT,
    apply_sc_project_defaults,
    args_error_context,
    args_with_defaults,
    attach_profile,
    base_url_for,
    build_headers,
    emit_error,
    load_env_file,
    quote_path_part,
    redact_secrets,
    request_http,
)

REVIEWERS_ENV = "SOURCE_CONTROL_DEFAULT_REVIEWERS"
OPEN_STATES = ("open", "opened")

# Профиль пользователя (~/.config/pvm-guru/profile.json) закрывает пропуски там,
# где значение не названо: tenant и проект SourceControl. Репозиторий умолчания
# не имеет — PR создаётся в одном конкретном репозитории, и угадывать его нельзя,
# — но и он проверяется вместе с ними, одним отказом: три пропущенных аргумента
# пользователь должен увидеть за один запуск, как и до появления профиля.
# Порядок всегда явное → окружение → профиль → прежнее поведение (слова argparse
# и код 2); применённое умолчание попадает в defaults_applied. Таблица
# tenant/проекта и текст подсказки к отказу общие с source_control_search.py
# и живут в _swtr_common.


def emit(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def endpoint_for(tenant: str, project: str, repo: str, suffix: str = "") -> str:
    base = f"/repos/{quote_path_part(tenant)}/{quote_path_part(project)}/{quote_path_part(repo)}"
    return base + suffix


def sc_request(method: str, base_url: str, endpoint: str, *, params: Optional[Dict[str, Any]] = None,
               body: Optional[Any] = None, timeout: float = 30.0,
               max_retries: Optional[int] = None) -> Any:
    # POST здесь создаёт сущность. Общий helper по умолчанию ретраит три раза, и
    # если сервер PR создал, а ответ до клиента не дошёл, повтор создаёт второй.
    # Проверка дублей отрабатывает один раз, до первого запроса, и от этого не
    # спасает — поэтому создающие запросы идут без автоматического повтора.
    if max_retries is None:
        max_retries = 0 if method.upper() in ("POST", "PUT", "PATCH", "DELETE") else 3
    headers = build_headers("source_control", require_auth=True)
    query = ""
    if params:
        query = "?" + "&".join(
            f"{quote_path_part(str(k))}={quote_path_part(str(v))}"
            for k, v in params.items() if v is not None and v != ""
        )
    payload, _, _ = request_http(
        method, base_url.rstrip("/") + endpoint + query, headers,
        body=body, timeout=timeout, system="source_control", max_retries=max_retries,
    )
    return payload


def _items(payload: Any) -> List[Dict[str, Any]]:
    """Список PR из ответа API, каким бы конвертом он ни был обёрнут."""
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        for key in ("items", "data", "content", "values", "pullRequests"):
            value = payload.get(key)
            if isinstance(value, list):
                return [p for p in value if isinstance(p, dict)]
    return []


def _branch_of(pull: Dict[str, Any], side: str) -> Optional[str]:
    """Имя ветки PR: у разных сборок это либо строка, либо объект с `ref`."""
    value = pull.get(side)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("ref", "label", "name", "branch"):
            if isinstance(value.get(key), str):
                return value[key]
    return None


def list_pulls(base_url: str, tenant: str, project: str, repo: str, *,
               state: str, timeout: float) -> List[Dict[str, Any]]:
    payload = sc_request(
        "GET", base_url, endpoint_for(tenant, project, repo, "/pulls"),
        params={"state": state, "limit": 100}, timeout=timeout,
    )
    pulls = []
    for pull in _items(payload):
        pulls.append(
            {
                "index": pull.get("index") or pull.get("number") or pull.get("id"),
                "title": pull.get("title"),
                "state": pull.get("state"),
                "head": _branch_of(pull, "head"),
                "base": _branch_of(pull, "base"),
                "url": pull.get("html_url") or pull.get("url"),
            }
        )
    return pulls


def find_duplicate(pulls: Sequence[Dict[str, Any]], head: str) -> Optional[Dict[str, Any]]:
    """Открытый PR из той же исходной ветки. Второй такой создавать нельзя."""
    for pull in pulls:
        if str(pull.get("state") or "").lower() not in OPEN_STATES:
            continue
        if pull.get("head") and pull["head"].split(":")[-1] == head.split(":")[-1]:
            return pull
    return None


def read_default_reviewers(env_file: Optional[Path]) -> Tuple[List[str], List[str]]:
    """Ревьюеры по умолчанию из окружения. Нет — не подставляем никого."""
    if env_file is not None:
        load_env_file(env_file)
    raw = os.environ.get(REVIEWERS_ENV, "").strip()
    if not raw:
        return [], [
            f"{REVIEWERS_ENV} не задана — ревьюеры не подставлены. Назначь их в интерфейсе "
            "или задай переменную в ~/.swtr-review.env"
        ]
    reviewers = [r.strip() for r in re.split(r"[,\s]+", raw) if r.strip()]
    return reviewers, []


# --- описание PR -------------------------------------------------------------
# Шаблон намеренно короткий: длинное автоописание не читают. Каждая строка — то,
# чего ревьюер не увидит из diff.

def render_body(change_id: Optional[str], task: Optional[str], task_url: Optional[str],
                requirements: Sequence[str], related: Sequence[str], summary: Optional[str]) -> str:
    lines: List[str] = []
    if summary:
        lines += [summary.strip(), ""]
    if change_id:
        lines.append(f"**Изменение:** `{change_id}`")
    if task:
        lines.append(f"**Задача:** {task_url or task}")
    if requirements:
        lines.append("")
        lines.append("**Закрывает требования:**")
        lines += [f"- {req}" for req in requirements]
    if related:
        lines.append("")
        lines.append("**Связанные PR:**")
        lines += [f"- {link}" for link in related]
    return "\n".join(lines).rstrip() + "\n" if lines else ""


def build_payload(args: argparse.Namespace, reviewers: Sequence[str]) -> Dict[str, Any]:
    body = render_body(
        args.change_id, args.task, args.task_url,
        args.requirement or [], args.related or [], args.summary,
    )
    payload: Dict[str, Any] = {
        "head": args.head,
        "base": args.base,
        "title": args.title,
        "body": body,
    }
    if args.assignee:
        payload["assignees"] = list(args.assignee)
    return payload


def check_title(title: str, change_id: Optional[str]) -> List[str]:
    warnings: List[str] = []
    stripped = title.strip()
    if len(stripped) < 10:
        warnings.append("заголовок короче 10 символов — из него не понять, что в PR")
    if re.match(r"^(fix|wip|update|правки|фикс)\b", stripped, re.I) and len(stripped) < 30:
        warnings.append(
            f"заголовок «{stripped}» ничего не сообщает — напиши, что меняется по существу"
        )
    if change_id and change_id.lower() in stripped.lower():
        warnings.append("change-id уже есть в описании — в заголовке он занимает место зря")
    return warnings


def cmd_list(args: argparse.Namespace) -> int:
    pulls = list_pulls(
        args.base_url, args.tenant, args.project, args.repo,
        state=args.state, timeout=args.timeout,
    )
    emit(args_with_defaults(
        {
            "command": "list",
            "ok": True,
            "tenant": args.tenant,
            "project": args.project,
            "repo": args.repo,
            "state": args.state,
            "pulls": pulls,
            "pullCount": len(pulls),
            "warnings": [],
        },
        args,
    ))
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    result: Dict[str, Any] = args_with_defaults({
        "command": "create",
        "tenant": args.tenant,
        "project": args.project,
        "repo": args.repo,
        "executed": bool(args.execute),
        "created": False,
        "duplicate": None,
        "reviewers": [],
        "requests": [],
        "errors": [],
        "warnings": [],
    }, args)

    if args.head == args.base:
        result["errors"].append(f"исходная и целевая ветка совпадают ({args.head})")
        result["ok"] = False
        emit(result)
        return 1

    reviewers, reviewer_warnings = read_default_reviewers(
        Path(args.env_file).expanduser() if args.env_file else None
    )
    if args.reviewer:
        reviewers = list(args.reviewer)
        reviewer_warnings = []
    result["reviewers"] = reviewers
    result["warnings"].extend(reviewer_warnings)
    result["warnings"].extend(check_title(args.title, args.change_id))

    # Дубли проверяются ДО любой записи: два открытых PR из одной ветки — это
    # расходящееся ревью и почти наверняка ошибка, а не намерение.
    existing = list_pulls(
        args.base_url, args.tenant, args.project, args.repo, state="open", timeout=args.timeout
    )
    duplicate = find_duplicate(existing, args.head)
    if duplicate:
        result["duplicate"] = duplicate
        result["errors"].append(
            f"из ветки {args.head} уже открыт PR #{duplicate.get('index')} "
            f"({duplicate.get('url') or 'ссылки нет'}) — дополняй его, а не создавай второй"
        )
        result["ok"] = False
        emit(result)
        return 1

    payload = build_payload(args, reviewers)
    endpoint = endpoint_for(args.tenant, args.project, args.repo, "/pulls")
    result["requests"].append({"method": "POST", "endpoint": endpoint, "body": payload})
    if reviewers:
        result["requests"].append(
            {
                "method": "POST",
                "endpoint": endpoint + "/<index>/requested_reviewers",
                "body": {"reviewers": reviewers},
                "note": "отдельным запросом после создания — API назначает ревьюеров не в теле PR",
            }
        )

    if not args.execute:
        result["warnings"].append("dry-run: сетевой записи не было, повтори с --execute")
        result["ok"] = True
        emit(result)
        return 0

    try:
        created = sc_request("POST", args.base_url, endpoint, body=payload, timeout=args.timeout)
    except RuntimeError as exc:
        # Отказ создающего запроса неоднозначен: сервер мог PR создать и не
        # донести ответ. Повторять нельзя — идём смотреть, что на самом деле
        # получилось, и говорим об этом прямо.
        result["errors"].append(f"создание PR не подтвердилось: {redact_secrets(str(exc))}")
        try:
            after = list_pulls(args.base_url, args.tenant, args.project, args.repo,
                               state="open", timeout=args.timeout)
        except RuntimeError as check_exc:
            result["errors"].append(
                "и сверить состояние не удалось: "
                f"{redact_secrets(str(check_exc))} — проверь репозиторий глазами перед повтором"
            )
        else:
            landed = find_duplicate(after, args.head)
            result["reconciled"] = bool(landed)
            if landed:
                result["pull"] = {"index": landed.get("index"),
                                  "title": landed.get("title"),
                                  "url": landed.get("url") or landed.get("html_url")}
                result["created"] = True
                result["errors"].append(
                    f"но PR #{landed.get('index')} в репозитории есть — запрос всё-таки прошёл. "
                    "Повторять создание не нужно"
                )
            else:
                result["errors"].append(
                    f"PR из ветки {args.head} в репозитории не появился — можно повторить"
                )
        result["ok"] = False
        emit(result)
        return 1
    index = None
    if isinstance(created, dict):
        index = created.get("index") or created.get("number") or created.get("id")
        result["pull"] = {
            "index": index,
            "title": created.get("title"),
            "url": created.get("html_url") or created.get("url"),
        }
    result["created"] = True

    if reviewers and index is not None:
        try:
            sc_request(
                "POST", args.base_url, endpoint_for(
                    args.tenant, args.project, args.repo, f"/pulls/{index}/requested_reviewers"
                ),
                body={"reviewers": reviewers}, timeout=args.timeout,
            )
        except RuntimeError as exc:
            # Текст ошибки может нести тело ответа сервера вместе с токеном —
            # в stdout он идёт только через redact_secrets.
            result["warnings"].append(
                f"PR создан, но ревьюеры не назначены: {redact_secrets(str(exc))}. "
                "Назначь их вручную"
            )
    result["ok"] = True
    emit(result)
    return 0


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default=None, help="Переопределить SOURCE_CONTROL_BASE_URL")
    parser.add_argument("--env-file", default="~/.swtr-review.env")
    parser.add_argument("--no-profile", action="store_true",
                        help="Не применять умолчания из профиля пользователя (%s): tenant и проект "
                             "SourceControl. Ведёт себя так, будто профиля нет." % PROFILE_PATH_HINT)
    parser.add_argument("--timeout", type=float, default=30.0, help="Таймаут HTTP в секундах")
    # Ни один из трёх не required у argparse: tenant и проект могут прийти из
    # окружения или профиля, а репозиторий не может — но отказ обязан быть один
    # на все недостающие, как его собирал сам argparse. Его и отдаёт
    # apply_sc_project_defaults() через сабпарсер команды: прежние слова, прежний
    # код 2, прежний порядок аргументов.
    parser.add_argument("--tenant", help="Идентификатор tenant SourceControl")
    parser.add_argument("--project", help="Проект SourceControl")
    parser.add_argument("--repo", help="Репозиторий (умолчания не имеет)")
    parser.set_defaults(subparser=parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pull Request в SourceControl. Печатает один JSON-объект в stdout. "
                    "Создание без --execute ничего не публикует.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="Открытые Pull Request репозитория")
    add_common_args(p_list)
    p_list.add_argument("--state", default="open", choices=("open", "closed", "all"))
    p_list.set_defaults(func=cmd_list)

    p_create = sub.add_parser("create", help="Создать Pull Request из ветки")
    add_common_args(p_create)
    p_create.add_argument("--head", required=True, help="Исходная ветка")
    p_create.add_argument("--base", required=True, help="Целевая ветка")
    p_create.add_argument("--title", required=True, help="Заголовок PR")
    p_create.add_argument("--summary", help="Одно-два предложения о сути изменения")
    p_create.add_argument("--change-id", help="Идентификатор изменения OpenSpec")
    p_create.add_argument("--task", help="Код задачи трекера")
    p_create.add_argument("--task-url", help="Ссылка на задачу трекера")
    p_create.add_argument("--requirement", action="append",
                          help="Закрытое требование REQ-00X; можно повторять")
    p_create.add_argument("--related", action="append",
                          help="Ссылка на связанный PR другого репозитория; можно повторять")
    p_create.add_argument("--reviewer", action="append",
                          help=f"Ревьюер; без флага берутся из {REVIEWERS_ENV}")
    p_create.add_argument("--assignee", action="append", help="Исполнитель PR")
    p_create.add_argument("--execute", action="store_true",
                          help="Опубликовать PR; без флага печатается только тело запроса")
    p_create.set_defaults(func=cmd_create)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    load_env_file(Path(args.env_file).expanduser())
    if getattr(args, "base_url", None) in (None, ""):
        args.base_url = base_url_for("source_control")
    # Профиль — одной точкой и ПОСЛЕ env-файла: переменные окружения стоят
    # раньше профиля и к этому моменту уже должны быть видны. --no-profile не
    # читает файл вовсе.
    attach_profile(args)
    apply_sc_project_defaults(args, extra_required=("--repo",))
    try:
        return args.func(args)
    except RuntimeError as exc:
        return emit_error(args.command, exc, **args_error_context(args))


if __name__ == "__main__":
    sys.exit(main())
