#!/usr/bin/env python3
"""Fetch and normalize SWTR Task Tracker unit comments.

Calls only the read endpoint:
  POST /rest/api/unit-comment/v1/find with {"unitCode": "PVM-119134"}.

Prints a JSON document to stdout containing the raw response, the normalized
comments, and the redacted request metadata. The script never persists secrets
and no longer writes intermediate files.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _swtr_common import (  # noqa: E402
    UNIT_CODE_RE,
    base_url_for,
    build_headers,
    build_ssl_context,
    emit_error,
    load_env_file,
    request_http,
    safe_headers_for_log,
    value_to_text,
)

COMMENTS_ENDPOINT = "/rest/api/unit-comment/v1/find"


def extract_unit_code(value: str) -> str:
    decoded = urllib.parse.unquote(value.upper())
    match = UNIT_CODE_RE.search(decoded)
    if not match:
        raise SystemExit(
            "Unit code is not found. Pass a SWTR unit code or URL, for example PVM-119134."
        )
    return match.group(0)


def make_request_body(unit_code: str, page: Optional[int], size: Optional[int]) -> Dict[str, Any]:
    # `page` is a required field of UnitCommentSearchRqDto: always send it.
    return {
        "unitCode": unit_code,
        "page": {
            "page": 0 if page is None else page,
            "size": 100 if size is None else size,
        },
    }


def collect_rich_text(node: Any) -> str:
    """Extract readable plain text from ProseMirror-like JSON nodes."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, (int, float, bool)):
        return str(node)
    if isinstance(node, list):
        parts = [collect_rich_text(item) for item in node]
        return "".join(parts)
    if not isinstance(node, dict):
        return str(node)

    node_type = node.get("type")
    if node_type == "text":
        return str(node.get("text") or "")
    if node_type == "hardBreak":
        return "\n"

    children = node.get("content")
    text = collect_rich_text(children) if isinstance(children, list) else ""
    # Таблица. Без этих трёх веток ячейки складываются в столбик, и границы
    # строки исчезают: таблица требований («№ | тип | требование | критерий»)
    # превращается в перечень слов, где не видно, где кончается одно требование
    # и начинается следующее.
    if node_type in {"tableCell", "tableHeader"}:
        return " ".join(text.split()) + " | "
    if node_type == "tableRow":
        return text.rstrip().rstrip("|").rstrip() + "\n"
    if node_type == "table":
        return text.rstrip() + "\n"
    if node_type in {"paragraph", "heading", "blockquote", "listItem"}:
        return text.rstrip() + "\n"
    if node_type in {"bulletList", "orderedList"}:
        return text.rstrip() + "\n"
    return text


def collect_rich_hrefs(node: Any) -> List[str]:
    """Адреса ссылок из ProseMirror-документа, в порядке появления.

    В тексте ссылки нет: она живёт в `attrs.href` узла-марки, и после
    разворачивания документа в текст исчезает совсем. Тому, кто ищет по
    источнику ссылки на смежные страницы, нужен этот отдельный проход.
    """
    found: List[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            attrs = value.get("attrs")
            if isinstance(attrs, dict) and isinstance(attrs.get("href"), str):
                href = attrs["href"].strip()
                if href and href not in found:
                    found.append(href)
            for key, item in value.items():
                if key != "text":
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    walk(json.loads(stripped))
                except json.JSONDecodeError:
                    pass

    walk(node)
    return found


def normalize_plain_text(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_comment_text(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {"format": "empty", "text": "", "parsed": None}
    if not isinstance(raw, str):
        return {"format": "json_value", "text": normalize_plain_text(value_to_text(raw)), "parsed": raw}

    stripped = raw.strip()
    if not stripped:
        return {"format": "empty", "text": "", "parsed": None}

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return {"format": "plain", "text": normalize_plain_text(stripped), "parsed": None}

    if isinstance(parsed, (dict, list)):
        extracted = normalize_plain_text(collect_rich_text(parsed))
        if not extracted:
            extracted = normalize_plain_text(value_to_text(parsed))
        return {"format": "rich_json", "text": extracted, "parsed": parsed}

    return {"format": "json_scalar", "text": normalize_plain_text(value_to_text(parsed)), "parsed": parsed}


def user_display_name(user: Any) -> str:
    if not isinstance(user, dict):
        return value_to_text(user) or "не указан"
    parts = [
        value_to_text(user.get("lastName")),
        value_to_text(user.get("firstName")),
        value_to_text(user.get("middleName")),
    ]
    fio = " ".join(part for part in parts if part).strip()
    login = value_to_text(user.get("login")) or value_to_text(user.get("externalId"))
    if fio and login:
        return f"{fio} ({login})"
    return fio or login or "не указан"


def normalize_comment(comment: Any, index: int) -> Dict[str, Any]:
    if not isinstance(comment, dict):
        parsed = parse_comment_text(comment)
        return {
            "index": index,
            "unitCommentCode": "",
            "unitCode": "",
            "text": parsed["text"],
            "textFormat": parsed["format"],
            "createdBy": "не указан",
            "createdAt": "",
            "updatedBy": "не указан",
            "updatedAt": "",
            "deleted": False,
            "raw": comment,
        }

    parsed = parse_comment_text(comment.get("commentText"))
    return {
        "index": index,
        "unitCommentCode": value_to_text(comment.get("unitCommentCode")),
        "unitCode": value_to_text(comment.get("unitCode")),
        "text": parsed["text"],
        "textFormat": parsed["format"],
        "createdBy": user_display_name(comment.get("createdBy")),
        "createdAt": value_to_text(comment.get("createdAt")),
        "updatedBy": user_display_name(comment.get("updatedBy")),
        "updatedAt": value_to_text(comment.get("updatedAt")),
        "deleted": bool(comment.get("deleted")),
        "rawCommentText": comment.get("commentText"),
        "raw": comment,
    }


def normalize_response(response: Any) -> Dict[str, Any]:
    if not isinstance(response, dict):
        return {
            "content": [],
            "pageSize": None,
            "hasNext": False,
            "pageNumber": None,
            "totalElements": None,
            "raw": response,
        }
    content = response.get("content")
    if not isinstance(content, list):
        content = []
    return {
        "content": [normalize_comment(comment, index + 1) for index, comment in enumerate(content)],
        "pageSize": response.get("pageSize"),
        "hasNext": bool(response.get("hasNext")),
        "pageNumber": response.get("pageNumber"),
        "totalElements": response.get("totalElements"),
        "raw": response,
    }


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch SWTR Task Tracker unit comments via /rest/api/unit-comment/v1/find. Prints JSON to stdout."
    )
    parser.add_argument("unit", help="SWTR unit code or URL, for example PVM-119134")
    parser.add_argument("--base-url", default=None,
                        help="Override SWTR_BASE_URL (resolved after the env file loads; default https://portal.works.prod.sbt/swtr)")
    parser.add_argument("--env-file", default="~/.swtr-review.env", help="Optional local env file with SWTR secrets")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    parser.add_argument("--include-deleted", action="store_true", help="Include deleted comments in normalized output")
    parser.add_argument("--page", type=int, help="Optional page number, 0-based (SWTR API-native), if the API instance supports it")
    parser.add_argument("--size", type=int, help="Optional page size if the API instance supports it")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    load_env_file(Path(args.env_file).expanduser())
    # Resolve the base URL AFTER the env file is loaded so a SWTR_BASE_URL set
    # only in ~/.swtr-review.env is honored (an explicit --base-url still wins).
    if getattr(args, "base_url", None) in (None, ""):
        args.base_url = base_url_for("task_tracker")
    unit_code = extract_unit_code(args.unit)

    clean_base = args.base_url.rstrip("/")
    url = clean_base + COMMENTS_ENDPOINT
    body = make_request_body(unit_code, args.page, args.size)

    try:
        # build_headers() is inside the try so a missing-auth RuntimeError reaches
        # emit_error (unified error contract), not an uncaught traceback.
        headers = build_headers("task_tracker", require_auth=True)
        response, _, _ = request_http("POST", url, headers, body=body, timeout=args.timeout, system="task_tracker")
    except RuntimeError as exc:
        # emit_error() redacts the error text before printing: an upstream error
        # message can embed a token (echoed back in a 401/403 body). request_http
        # already redacts, but emit_error redacts again so any RuntimeError path is
        # safe (defense in depth). unitCode is preserved as an extra field.
        return emit_error("swtr_unit_comments", exc, unitCode=unit_code)

    normalized = normalize_response(response)
    comments = normalized.get("content", []) if isinstance(normalized.get("content"), list) else []
    if not args.include_deleted:
        comments_filtered = [c for c in comments if not c.get("deleted")]
    else:
        comments_filtered = comments

    print(json.dumps(
        {
            "ok": True,
            "unitCode": unit_code,
            "fetchedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            "request": {
                "url": url,
                "method": "POST",
                "headers": safe_headers_for_log(headers),
                "body": body,
                "skipTlsVerify": build_ssl_context("task_tracker") is not None,
            },
            "response": response,
            "normalized": {
                "pageSize": normalized.get("pageSize"),
                "hasNext": normalized.get("hasNext"),
                "pageNumber": normalized.get("pageNumber"),
                "totalElements": normalized.get("totalElements"),
                "comments": comments_filtered,
                "totalOnPage": len(comments),
                "deletedOnPage": sum(1 for c in comments if c.get("deleted")),
                "includeDeleted": bool(args.include_deleted),
            },
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
