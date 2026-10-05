#!/usr/bin/env python3
"""Confluence (sberworks Data Center) read + gated-write helper for pvm-guru.

Prints exactly one JSON object to stdout per subcommand. Read-only by default;
``update`` mutates only with --execute (dry-run otherwise, no PUT). Uses the
shared _swtr_common auth/HTTP layer with system="confluence" (Bearer-first;
sberworks rejects Basic — use a Data Center PAT). Secrets are never printed:
all error text passes through redact_secrets(). The space key may come from the
user profile; the sberworks gate is checked first and the profile never lifts it
(--no-profile turns the defaults off).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

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
)

CONFLUENCE = "confluence"

# Профиль пользователя (~/.config/pvm-guru/profile.json) закрывает здесь ровно
# один пропуск — ключ пространства. Читается ПОСЛЕ гейта sberworks-not-default:
# профиль не делает sberworks доступным без явного признака, он лишь избавляет
# от повторения ключа там, где систему уже назвали (REQ-019). --no-profile
# кладёт в args.profile None, и всё ведёт себя как без профиля, а каждое
# применённое умолчание видно в JSON полем defaults_applied.
PROFILE_SPACE_KEY = "sberworks.confluence_space"
# Своей переменной окружения у пространства Confluence в семействе нет:
# источников ровно два — флаг и профиль. Пустой кортеж сохраняет единый порядок
# resolve_default.
SPACE_ENV_NAMES = ()


def emit(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


PROFILE_ERROR_HINT = (
    "Пространство взято из профиля (см. defaults_applied). Если отказ вызван им — поправь профиль "
    "(/guru_profile) или назови пространство явно; без профиля — повтори с --no-profile.")


def apply_profile_space(args: argparse.Namespace) -> None:
    """`--space` у поиска: явный флаг, иначе профиль.

    Готовый `--cql` профиль не трогает: такой запрос написан пользователем
    целиком, и дописать в него пространство значило бы поменять запрос, о
    котором не просили. Пусто и там, и там — поиск идёт по всей вики, как
    раньше: обязательным `--space` никогда не был.
    """
    if args.command != "search" or args.cql:
        return
    value, source = resolve_default(
        getattr(args, "space", None), SPACE_ENV_NAMES, getattr(args, "profile", None), PROFILE_SPACE_KEY)
    if source is None:
        return
    args.space = value
    if source != "explicit":
        args.defaults_applied.add("space", value, source)


def parse_page_id(raw: str) -> Optional[str]:
    """Extract a numeric Confluence pageId from a raw id or a page URL."""
    raw = (raw or "").strip()
    if raw.isdigit():
        return raw
    parsed = urlparse(raw)
    qs = parse_qs(parsed.query)
    if qs.get("pageId") and qs["pageId"][0].isdigit():
        return qs["pageId"][0]
    m = re.search(r"/pages/(\d+)", parsed.path)
    if m:
        return m.group(1)
    m = re.search(r"(\d{4,})", raw)
    return m.group(1) if m else None


def conf_get(base_url: str, endpoint: str, params: Optional[Dict[str, Any]], timeout: float) -> Any:
    headers = build_headers(CONFLUENCE, require_auth=True)
    query = ""
    if params:
        query = "?" + "&".join(
            f"{quote_path_part(str(k))}={quote_path_part(str(v))}"
            for k, v in params.items() if v is not None and v != ""
        )
    payload, _, _ = request_http("GET", base_url.rstrip("/") + endpoint + query, headers, timeout=timeout, system=CONFLUENCE)
    return payload


def _space_brief(space: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(space, dict):
        return None
    return {"key": space.get("key"), "name": space.get("name"), "id": space.get("id")}


def _content_url(base_url: str, payload: Any) -> Optional[str]:
    links = payload.get("_links") if isinstance(payload, dict) else None
    if isinstance(links, dict) and links.get("webui"):
        base = (links.get("base") or base_url).rstrip("/")
        return base + links["webui"]
    return None


def _version_number(payload: Any) -> Optional[int]:
    version = payload.get("version") if isinstance(payload, dict) else None
    return version.get("number") if isinstance(version, dict) else None


def cmd_get(args: argparse.Namespace) -> int:
    page_id = parse_page_id(args.page or args.id or args.url or "")
    if not page_id:
        emit({"command": "confluence.get", "ok": False, "error": "Could not resolve a Confluence pageId from input."})
        return 1
    payload = conf_get(
        args.base_url, f"/rest/api/content/{quote_path_part(page_id)}",
        {"expand": "version,space,body.storage,ancestors"}, args.timeout,
    )
    if not isinstance(payload, dict):
        emit({"command": "confluence.get", "id": page_id, "raw": payload})
        return 0
    body_storage = payload.get("body")
    body = None
    if isinstance(body_storage, dict):
        storage = body_storage.get("storage")
        if isinstance(storage, dict):
            body = storage.get("value")
    ancestors = [
        {"id": a.get("id"), "title": a.get("title")}
        for a in (payload.get("ancestors") or []) if isinstance(a, dict)
    ]
    emit({
        "command": "confluence.get",
        "id": page_id,
        "title": payload.get("title"),
        "type": payload.get("type"),
        "space": _space_brief(payload.get("space")),
        "version": _version_number(payload),
        "ancestors": ancestors,
        "body": body,
        "url": _content_url(args.base_url, payload),
    })
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    cql = args.cql
    if not cql:
        if not args.text:
            emit({"command": "confluence.search", "ok": False, "error": "Provide search text or --cql."})
            return 1
        safe = args.text.replace('"', '\\"')
        cql = f'text ~ "{safe}"'
        if args.space:
            cql += f' and space = "{args.space}"'
    payload = conf_get(
        args.base_url, "/rest/api/content/search",
        {"cql": cql, "limit": args.limit, "expand": "space,version"}, args.timeout,
    )
    results: List[Dict[str, Any]] = []
    items = payload.get("results") if isinstance(payload, dict) else None
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        results.append({
            "id": it.get("id"),
            "title": it.get("title"),
            "type": it.get("type"),
            "space": _space_brief(it.get("space")),
            "version": _version_number(it),
            "url": _content_url(args.base_url, it),
        })
    emit(args_with_defaults({"command": "confluence.search", "cql": cql,
                         "size": len(results), "results": results}, args))
    return 0


def cmd_space(args: argparse.Namespace) -> int:
    space = conf_get(args.base_url, f"/rest/api/space/{quote_path_part(args.key)}", None, args.timeout)
    pages = conf_get(
        args.base_url, f"/rest/api/space/{quote_path_part(args.key)}/content/page",
        {"limit": args.limit}, args.timeout,
    )
    page_list = [
        {"id": it.get("id"), "title": it.get("title")}
        for it in ((pages.get("results") if isinstance(pages, dict) else None) or []) if isinstance(it, dict)
    ]
    emit({
        "command": "confluence.space",
        "space": _space_brief(space) if isinstance(space, dict) else space,
        "pages": page_list,
    })
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    page_id = parse_page_id(args.id or "")
    if not page_id:
        emit({"command": "confluence.update", "ok": False, "error": "Could not resolve a Confluence pageId."})
        return 1
    # Missing/unreadable file -> unified error contract (OSError is not a
    # RuntimeError, so wrap it for main's emit_error handler); no network yet.
    try:
        storage = Path(args.storage_file).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Cannot read --storage-file: {exc}") from exc
    current = conf_get(args.base_url, f"/rest/api/content/{quote_path_part(page_id)}", {"expand": "version,space"}, args.timeout)
    if not isinstance(current, dict):
        emit({"command": "confluence.update", "ok": False, "error": "Could not read current page version."})
        return 1
    from_version = _version_number(current) or 0
    to_version = int(from_version) + 1
    title = args.title or current.get("title")
    request_body = {
        "version": {"number": to_version, "message": args.message or ""},
        "title": title,
        "type": "page",
        "body": {"storage": {"value": storage, "representation": "storage"}},
    }
    if not args.execute:
        emit({
            "command": "confluence.update",
            "mode": "dry-run",
            "id": page_id,
            "fromVersion": from_version,
            "toVersion": to_version,
            "title": title,
            "storageChars": len(storage),
            "note": "No PUT performed. Re-run with --execute to apply.",
        })
        return 0
    headers = build_headers(CONFLUENCE, require_auth=True)
    # No retry on a write: a retried PUT after a lost response conflicts with the
    # already-bumped page version; surface the error so the caller re-reads state.
    payload, _, status = request_http(
        "PUT", args.base_url.rstrip("/") + f"/rest/api/content/{quote_path_part(page_id)}",
        headers, body=request_body, timeout=args.timeout, system=CONFLUENCE, max_retries=0,
    )
    emit({
        "command": "confluence.update",
        "mode": "executed",
        "id": page_id,
        "toVersion": to_version,
        "httpStatus": status,
        "response": payload if isinstance(payload, dict) else {"raw": payload},
    })
    return 0


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default=None, help="Override CONFLUENCE_BASE_URL (default https://sberworks.ru/wiki)")
    parser.add_argument("--env-file", default="~/.swtr-review.env")
    parser.add_argument("--no-profile", action="store_true",
                        help="Do not apply defaults from the user profile (%s): the Confluence space key. "
                             "Behaves as if there were no profile." % PROFILE_PATH_HINT)
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds (default 30)")
    parser.add_argument(
        "--confirm-sberworks", action="store_true",
        help="REQUIRED. sberworks is NOT the default backend: tasks and wiki live in "
             "SWTR Task Tracker / SWTR Wiki (swtr_task_tool.py). Pass this flag ONLY when "
             "the user explicitly named Confluence/sberworks or gave a sberworks.ru link.")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Confluence (sberworks) read + gated write. Prints JSON to stdout.")
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("get", help="Fetch one page by id/url (storage body, version, space, ancestors)")
    add_common(g)
    g.add_argument("page", nargs="?", help="Page id or URL")
    g.add_argument("--id")
    g.add_argument("--url")

    s = sub.add_parser("search", help="Search content by free text or raw CQL")
    add_common(s)
    s.add_argument("text", nargs="?", help='Free text (builds text ~ "...")')
    s.add_argument("--cql", help="Raw CQL (overrides text)")
    s.add_argument("--space",
                   help="Restrict to a space key; taken from the profile (sberworks.confluence_space) when omitted")
    s.add_argument("--limit", type=int, default=25, help="Max results (Confluence REST API-native default 25)")

    sp = sub.add_parser("space", help="Show a space and its top pages")
    add_common(sp)
    sp.add_argument("--key", required=True)
    sp.add_argument("--limit", type=int, default=25, help="Max top pages (Confluence REST API-native default 25)")

    u = sub.add_parser("update", help="Update a page from a storage-format file (gated: dry-run unless --execute)")
    add_common(u)
    u.add_argument("--id", required=True)
    u.add_argument("--storage-file", required=True, help="Path to a file with the new XHTML storage body")
    u.add_argument("--title")
    u.add_argument("--message", help="Version comment")
    u.add_argument("--execute", action="store_true", help="Actually PUT the update (omit for dry-run)")

    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    # sberworks is the LAST-priority backend. The gate fires before env loading and
    # before any socket, so the refusal is identical whether or not a PAT is set.
    if not getattr(args, "confirm_sberworks", False):
        return emit_error(
            f"confluence.{args.command}",
            "sberworks — не дефолтный бэкенд. Вики и задачи по умолчанию: "
            "swtr_task_tool.py search <текст> (SWTR Wiki + Task Tracker). Если пользователь "
            "ЯВНО просил Confluence/sberworks — повторить ту же команду с --confirm-sberworks.",
            gate="sberworks-not-default",
            useInstead="swtr_task_tool.py search <текст>",
        )
    load_env_file(Path(args.env_file).expanduser())
    # Back-compat: the sberworks PAT may still live in ~/.sberworks.env. Loading
    # never overrides values already in the environment or the canonical file.
    load_env_file(Path("~/.sberworks.env").expanduser())
    if getattr(args, "base_url", None) in (None, ""):
        args.base_url = base_url_for(CONFLUENCE)
    # Профиль читается последним из источников и только здесь: гейт выше уже
    # пропустил вызов, env-файл уже загружен. --no-profile не открывает файл.
    attach_profile(args)
    apply_profile_space(args)
    try:
        if args.command == "get":
            return cmd_get(args)
        if args.command == "search":
            return cmd_search(args)
        if args.command == "space":
            return cmd_space(args)
        if args.command == "update":
            return cmd_update(args)
        raise SystemExit(f"Unsupported command: {args.command}")
    except RuntimeError as exc:
        return emit_error(f"confluence.{args.command}", exc, **args_error_context(args, PROFILE_ERROR_HINT))


if __name__ == "__main__":
    raise SystemExit(main())
