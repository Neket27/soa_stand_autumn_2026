#!/usr/bin/env python3
"""Jira (sberworks Data Center) read + gated-write helper for pvm-guru.

Prints exactly one JSON object to stdout per subcommand. Read-only by default;
``comment`` and ``create`` mutate only with --execute (dry-run otherwise, no
POST). Uses the shared _swtr_common auth/HTTP layer with system="jira"
(Bearer-first; sberworks rejects Basic — use a Data Center PAT, distinct from
the Confluence PAT). Secrets are never printed (errors pass redact_secrets()).
The project key may come from the user profile; the sberworks gate is checked
first and the profile never lifts it (--no-profile turns the defaults off).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _swtr_common import (  # noqa: E402
    PROFILE_PATH_HINT,
    args_error_context,
    args_with_defaults,
    attach_profile,
    base_url_for,
    build_headers,
    emit_error,
    load_env_file,
    quote_path_part,
    request_http,
    resolve_default,
    value_to_text,
)

JIRA = "jira"

# Профиль пользователя (~/.config/pvm-guru/profile.json) закрывает здесь ровно
# один пропуск — ключ проекта. Читается ПОСЛЕ гейта sberworks-not-default:
# профиль не делает sberworks доступным без явного признака, он лишь избавляет
# от повторения ключа там, где систему уже назвали (REQ-019). --no-profile
# кладёт в args.profile None, и всё ведёт себя как без профиля, а каждое
# применённое умолчание видно в JSON полем defaults_applied.
PROFILE_PROJECT_KEY = "sberworks.jira_project"
# Своей переменной окружения у проекта Jira в семействе нет: источников ровно
# два — флаг и профиль. Пустой кортеж сохраняет единый порядок resolve_default.
PROJECT_ENV_NAMES = ()
# Проект есть у search и similar (необязательный фильтр) и у create
# (обязательное поле запроса) — остальные команды адресуются ключом задачи.
PROJECT_COMMANDS = ("search", "similar", "create")

GET_FIELDS = (
    "summary,status,issuetype,priority,assignee,reporter,created,updated,labels,"
    "components,description,parent,subtasks,comment,issuelinks"
)
SEARCH_FIELDS = "summary,status,issuetype,project,updated"

# Small bilingual stop list so "similar" builds meaningful summary terms.
STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "are", "was", "has",
    "при", "для", "это", "как", "что", "над", "под", "без", "или", "его", "ему",
    "не", "на", "по", "из", "до", "от", "за", "об", "во", "со", "the", "of",
}


def emit(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


PROFILE_ERROR_HINT = (
    "Проект взят из профиля (см. defaults_applied). Если отказ вызван им — поправь профиль "
    "(/guru_profile) или назови проект явно; без профиля — повтори с --no-profile.")


def apply_profile_project(args: argparse.Namespace) -> None:
    """`--project`: явный флаг, иначе профиль; у `create` пусто — прежний отказ argparse.

    Готовый `--jql` профиль не трогает: такой запрос написан пользователем
    целиком, и дописать в него проект значило бы поменять запрос, о котором не
    просили. У `create` проект обязателен, поэтому его отсутствие отдаётся
    словами самого argparse и его кодом возврата 2 — пользователь без профиля
    не должен заметить, что аргумент перестал быть обязательным.
    """
    if args.command not in PROJECT_COMMANDS:
        return
    if args.command == "search" and args.jql:
        return
    value, source = resolve_default(
        getattr(args, "project", None), PROJECT_ENV_NAMES, getattr(args, "profile", None), PROFILE_PROJECT_KEY)
    if source is None:
        if args.command == "create":
            args.subparser.error("the following arguments are required: --project")
        return
    args.project = value
    if source != "explicit":
        args.defaults_applied.add("project", value, source)


def jira_get(base_url: str, endpoint: str, params: Optional[Dict[str, Any]], timeout: float) -> Any:
    headers = build_headers(JIRA, require_auth=True)
    query = ""
    if params:
        query = "?" + "&".join(
            f"{quote_path_part(str(k))}={quote_path_part(str(v))}"
            for k, v in params.items() if v is not None and v != ""
        )
    payload, _, _ = request_http("GET", base_url.rstrip("/") + endpoint + query, headers, timeout=timeout, system=JIRA)
    return payload


def _name(obj: Any) -> Optional[str]:
    if isinstance(obj, dict):
        return obj.get("name") or obj.get("displayName") or obj.get("value")
    return value_to_text(obj) or None


def normalize_issue(payload: Dict[str, Any]) -> Dict[str, Any]:
    fields = payload.get("fields") or {}
    rendered = payload.get("renderedFields") or {}
    comment = fields.get("comment") or {}
    parent = fields.get("parent") or {}
    return {
        "key": payload.get("key"),
        "type": _name(fields.get("issuetype")),
        "status": _name(fields.get("status")),
        "priority": _name(fields.get("priority")),
        "summary": fields.get("summary"),
        "assignee": _name(fields.get("assignee")),
        "reporter": _name(fields.get("reporter")),
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "labels": fields.get("labels") or [],
        "components": [_name(c) for c in (fields.get("components") or []) if c],
        "parent": parent.get("key") if isinstance(parent, dict) else None,
        "subtasks": [s.get("key") for s in (fields.get("subtasks") or []) if isinstance(s, dict)],
        "links": _issue_links(fields.get("issuelinks") or []),
        "comments_total": comment.get("total") if isinstance(comment, dict) else None,
        "description": fields.get("description"),
        "rendered_description": rendered.get("description"),
    }


def _issue_links(links: List[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for link in links:
        if not isinstance(link, dict):
            continue
        link_type = (link.get("type") or {}).get("name") if isinstance(link.get("type"), dict) else None
        other = link.get("outwardIssue") or link.get("inwardIssue") or {}
        if isinstance(other, dict) and other.get("key"):
            out.append({"type": link_type, "key": other.get("key")})
    return out


def extract_terms(text: str, limit: int = 8) -> List[str]:
    terms: List[str] = []
    for word in re.findall(r"[\w\-]{3,}", (text or "").lower(), re.UNICODE):
        if word.isdigit() or word in STOPWORDS:
            continue
        if word not in terms:
            terms.append(word)
    return terms[:limit]


def _jql_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def cmd_get(args: argparse.Namespace) -> int:
    payload = jira_get(
        args.base_url, f"/rest/api/2/issue/{quote_path_part(args.key)}",
        {"fields": GET_FIELDS, "expand": "renderedFields"}, args.timeout,
    )
    if not isinstance(payload, dict):
        emit({"command": "jira.get", "key": args.key, "raw": payload})
        return 0
    result = normalize_issue(payload)
    result["command"] = "jira.get"
    emit(result)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    jql = args.jql
    if not jql:
        if not args.text:
            emit({"command": "jira.search", "ok": False, "error": "Provide search text or --jql."})
            return 1
        jql = f'text ~ "{_jql_escape(args.text)}"'
        if args.project:
            jql += f' and project = "{_jql_escape(args.project)}"'
        jql += " order by updated desc"
    payload = jira_get(
        args.base_url, "/rest/api/2/search",
        {"jql": jql, "maxResults": args.limit, "fields": SEARCH_FIELDS}, args.timeout,
    )
    issues = []
    for it in (payload.get("issues") if isinstance(payload, dict) else None) or []:
        if not isinstance(it, dict):
            continue
        f = it.get("fields") or {}
        issues.append({
            "key": it.get("key"),
            "project": (f.get("project") or {}).get("key") if isinstance(f.get("project"), dict) else None,
            "type": _name(f.get("issuetype")),
            "status": _name(f.get("status")),
            "summary": f.get("summary"),
            "updated": f.get("updated"),
        })
    total = payload.get("total") if isinstance(payload, dict) else None
    emit(args_with_defaults({"command": "jira.search", "jql": jql, "total": total,
                         "size": len(issues), "issues": issues}, args))
    return 0


def cmd_similar(args: argparse.Namespace) -> int:
    seed = args.seed
    seed_key = None
    if re.match(r"^[A-Za-z][A-Za-z0-9_]+-\d+$", seed.strip()):
        seed_key = seed.strip().upper()
        issue = jira_get(args.base_url, f"/rest/api/2/issue/{quote_path_part(seed_key)}", {"fields": "summary"}, args.timeout)
        summary = ((issue.get("fields") or {}).get("summary")) if isinstance(issue, dict) else ""
        terms = extract_terms(summary or seed_key)
    else:
        terms = extract_terms(seed)
    if not terms:
        emit({"command": "jira.similar", "ok": False, "error": "No usable terms extracted from seed."})
        return 1
    clause = " OR ".join(f'summary ~ "{_jql_escape(t)}"' for t in terms)
    jql = f"({clause})"
    if args.project:
        jql += f' and project = "{_jql_escape(args.project)}"'
    if seed_key:
        jql += f" and key != {seed_key}"
    jql += " order by updated desc"
    payload = jira_get(
        args.base_url, "/rest/api/2/search",
        {"jql": jql, "maxResults": args.limit, "fields": SEARCH_FIELDS}, args.timeout,
    )
    results = []
    for it in (payload.get("issues") if isinstance(payload, dict) else None) or []:
        if not isinstance(it, dict):
            continue
        f = it.get("fields") or {}
        summary = (f.get("summary") or "").lower()
        overlap = sum(1 for t in terms if t in summary)
        results.append({
            "key": it.get("key"),
            "project": (f.get("project") or {}).get("key") if isinstance(f.get("project"), dict) else None,
            "status": _name(f.get("status")),
            "summary": f.get("summary"),
            "updated": f.get("updated"),
            "score": overlap,
        })
    # API already returns "order by updated desc"; stable-sort by score keeps
    # the most-recent issue first within equal-overlap groups.
    results.sort(key=lambda r: -int(r["score"]))
    emit(args_with_defaults({"command": "jira.similar", "seed": seed, "terms": terms, "jql": jql,
                         "total": payload.get("total") if isinstance(payload, dict) else None,
                         "results": results}, args))
    return 0


def cmd_comments(args: argparse.Namespace) -> int:
    payload = jira_get(args.base_url, f"/rest/api/2/issue/{quote_path_part(args.key)}/comment", None, args.timeout)
    comments = []
    for c in (payload.get("comments") if isinstance(payload, dict) else None) or []:
        if isinstance(c, dict):
            comments.append({"author": _name(c.get("author")), "created": c.get("created"), "body": c.get("body")})
    emit({"command": "jira.comments", "key": args.key,
          "total": payload.get("total") if isinstance(payload, dict) else len(comments), "comments": comments})
    return 0


def cmd_comment(args: argparse.Namespace) -> int:
    # Unreadable/missing file -> unified error contract (OSError is not a
    # RuntimeError, so wrap it for main's emit_error handler).
    try:
        body = Path(args.body_file).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Cannot read --body-file: {exc}") from exc
    request_body = {"body": body}
    if not args.execute:
        emit({"command": "jira.comment", "mode": "dry-run", "key": args.key,
              "bodyChars": len(body), "note": "No POST performed. Re-run with --execute to apply."})
        return 0
    headers = build_headers(JIRA, require_auth=True)
    # No retry on a write: a retried POST after a lost response duplicates the comment.
    payload, _, status = request_http(
        "POST", args.base_url.rstrip("/") + f"/rest/api/2/issue/{quote_path_part(args.key)}/comment",
        headers, body=request_body, timeout=args.timeout, system=JIRA, max_retries=0,
    )
    emit({"command": "jira.comment", "mode": "executed", "key": args.key, "httpStatus": status,
          "response": payload if isinstance(payload, dict) else {"raw": payload}})
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    fields: Dict[str, Any] = {
        "project": {"key": args.project},
        "issuetype": {"name": args.type},
        "summary": args.summary,
    }
    if args.description_file:
        # Missing/unreadable file -> unified error contract (see cmd_comment).
        try:
            fields["description"] = Path(args.description_file).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"Cannot read --description-file: {exc}") from exc
    for raw in args.field or []:
        if "=" in raw:
            k, v = raw.split("=", 1)
            fields[k.strip()] = v
    request_body = {"fields": fields}
    if not args.execute:
        emit(args_with_defaults({"command": "jira.create", "mode": "dry-run", "request": request_body,
                             "note": "No POST performed. Re-run with --execute to apply."}, args))
        return 0
    headers = build_headers(JIRA, require_auth=True)
    # No retry on a write: a retried POST after a lost response duplicates the issue.
    payload, _, status = request_http(
        "POST", args.base_url.rstrip("/") + "/rest/api/2/issue",
        headers, body=request_body, timeout=args.timeout, system=JIRA, max_retries=0,
    )
    emit(args_with_defaults({"command": "jira.create", "mode": "executed", "httpStatus": status,
                         "response": payload if isinstance(payload, dict) else {"raw": payload}}, args))
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    fields: Dict[str, Any] = {}
    for raw in (args.field or []):
        if "=" in raw:
            k, v = raw.split("=", 1)
            fields.setdefault(k.strip(), []).append(v)  # labels-type field: a list of values
    for raw in (args.set or []):
        if "=" in raw:
            k, v = raw.split("=", 1)
            fields[k.strip()] = v  # raw scalar value
    if getattr(args, "component", None):
        # Component/s is an object array; replace the set with the given names.
        fields["components"] = [{"name": n.strip()} for n in args.component if n.strip()]
    if not fields:
        emit({"command": "jira.edit", "ok": False,
              "error": "Nothing to set — give --field <id>=<value> (labels) or --set <id>=<value> (scalar)."})
        return 1
    request_body = {"fields": fields}
    if not args.execute:
        emit({"command": "jira.edit", "mode": "dry-run", "key": args.key, "request": request_body,
              "note": "No PUT performed. Re-run with --execute to apply."})
        return 0
    headers = build_headers(JIRA, require_auth=True)
    # No retry on a write: a retried PUT after a lost response can clobber a
    # concurrent edit; surface the error instead so the caller re-checks state.
    payload, _, status = request_http(
        "PUT", args.base_url.rstrip("/") + f"/rest/api/2/issue/{quote_path_part(args.key)}",
        headers, body=request_body, timeout=args.timeout, system=JIRA, max_retries=0,
    )
    emit({"command": "jira.edit", "mode": "executed", "key": args.key, "httpStatus": status,
          "applied": fields,
          "response": payload if isinstance(payload, dict) else ({"raw": payload} if payload else None)})
    return 0


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default=None, help="Override JIRA_BASE_URL (default https://sberworks.ru/jira)")
    parser.add_argument("--env-file", default="~/.swtr-review.env")
    parser.add_argument("--no-profile", action="store_true",
                        help="Do not apply defaults from the user profile (%s): the Jira project key. "
                             "Behaves as if there were no profile." % PROFILE_PATH_HINT)
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds (default 30)")
    parser.add_argument(
        "--confirm-sberworks", action="store_true",
        help="REQUIRED. sberworks is NOT the default backend: tasks and wiki live in "
             "SWTR Task Tracker (swtr_task_tool.py). Pass this flag ONLY when the user "
             "explicitly named Jira/sberworks or gave a sberworks.ru link.")
    # Отказ «аргумент обязателен» отдаётся из apply_profile_project() через
    # сабпарсер той команды, которую запустили: слова и код те же, что у argparse.
    parser.set_defaults(subparser=parser)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Jira (sberworks) read + gated write. Prints JSON to stdout.")
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("get", help="Fetch one issue by key (normalized fields + comments total)")
    add_common(g)
    g.add_argument("key", help="Issue key, e.g. PVM-52489")

    s = sub.add_parser("search", help="Search issues by free text or raw JQL")
    add_common(s)
    s.add_argument("text", nargs="?", help='Free text (builds text ~ "...")')
    s.add_argument("--jql", help="Raw JQL (overrides text)")
    s.add_argument("--project", help="Restrict to a project key")
    s.add_argument("--limit", type=int, default=30, help="Max results (Jira REST maxResults; default 30)")

    sim = sub.add_parser("similar", help="Find issues similar to a key or text (summary-term overlap)")
    add_common(sim)
    sim.add_argument("seed", help="Issue key (e.g. PVM-52489) or free text")
    sim.add_argument("--project", help="Restrict candidates to a project key (reduces homonym noise)")
    sim.add_argument("--limit", type=int, default=30, help="Max candidates (Jira REST maxResults; default 30)")

    c = sub.add_parser("comments", help="List comments on an issue")
    add_common(c)
    c.add_argument("key")

    cm = sub.add_parser("comment", help="Add a comment from a file (gated: dry-run unless --execute)")
    add_common(cm)
    cm.add_argument("key")
    cm.add_argument("--body-file", required=True, help="File with the comment body (Jira DC wiki markup)")
    cm.add_argument("--execute", action="store_true")

    cr = sub.add_parser("create", help="Create an issue (gated: dry-run unless --execute)")
    add_common(cr)
    # Не required: значение может прийти из профиля. Отсутствие всюду отдаётся
    # из apply_profile_project() прежними словами argparse и его кодом 2.
    cr.add_argument("--project", help="Project key; taken from the profile (sberworks.jira_project) when omitted")
    cr.add_argument("--type", required=True, help="Issue type name, e.g. Task")
    cr.add_argument("--summary", required=True)
    cr.add_argument("--description-file")
    cr.add_argument("--field", action="append", help="Extra field as key=value; repeatable")
    cr.add_argument("--execute", action="store_true")

    e = sub.add_parser("edit", help="Set issue fields (gated: dry-run unless --execute)")
    add_common(e)
    e.add_argument("key", help="Issue key, e.g. CRPV-152289")
    e.add_argument("--field", action="append",
                   help="labels-field add: <fieldId>=<value> (repeatable; e.g. customfield_30901=Platform_V_backlog)")
    e.add_argument("--set", action="append", help="raw scalar set: <fieldId>=<value> (repeatable)")
    e.add_argument("--component", action="append",
                   help="Set Component/s by name (repeatable) — replaces the component set")
    e.add_argument("--execute", action="store_true")

    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    # sberworks is the LAST-priority backend. The gate fires before env loading and
    # before any socket, so the refusal is identical whether or not a PAT is set.
    if not getattr(args, "confirm_sberworks", False):
        return emit_error(
            f"jira.{args.command}",
            "sberworks — не дефолтный бэкенд. Задачи и вики по умолчанию: "
            "swtr_task_tool.py get <код> (SWTR Task Tracker). Если пользователь ЯВНО "
            "просил Jira/sberworks — повторить ту же команду с --confirm-sberworks.",
            gate="sberworks-not-default",
            useInstead="swtr_task_tool.py get <код>",
        )
    load_env_file(Path(args.env_file).expanduser())
    # Back-compat: the sberworks Jira PAT may still live in ~/.sberworks.env.
    load_env_file(Path("~/.sberworks.env").expanduser())
    if getattr(args, "base_url", None) in (None, ""):
        args.base_url = base_url_for(JIRA)
    # Профиль читается последним из источников и только здесь: гейт выше уже
    # пропустил вызов, env-файл уже загружен. --no-profile не открывает файл.
    attach_profile(args)
    apply_profile_project(args)
    try:
        handler = {
            "get": cmd_get, "search": cmd_search, "similar": cmd_similar,
            "comments": cmd_comments, "comment": cmd_comment, "create": cmd_create,
            "edit": cmd_edit,
        }.get(args.command)
        if handler is None:
            raise SystemExit(f"Unsupported command: {args.command}")
        return handler(args)
    except RuntimeError as exc:
        return emit_error(f"jira.{args.command}", exc, **args_error_context(args, PROFILE_ERROR_HINT))


if __name__ == "__main__":
    raise SystemExit(main())
