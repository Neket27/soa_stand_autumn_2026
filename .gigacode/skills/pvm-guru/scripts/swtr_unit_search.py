#!/usr/bin/env python3
"""Call SWTR unit search API with a user-provided QLRequestDto body.

Prints the API response to stdout as JSON. Authorization secrets are never
printed; HTTP errors are redacted.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _swtr_common import (  # noqa: E402
    base_url_for,
    build_headers,
    emit_error,
    load_env_file,
    request_http,
    safe_headers_for_log,
    unit_urls_in,
)


def read_body(args: argparse.Namespace) -> Dict[str, Any]:
    # Route malformed --body-file/--body-json (invalid JSON, unreadable file)
    # through the unified error contract (RuntimeError -> emit_error) instead of
    # dumping a raw Python traceback to stderr.
    try:
        if args.body_file:
            return json.loads(Path(args.body_file).expanduser().read_text(encoding="utf-8"))
        if args.body_json:
            return json.loads(args.body_json)
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"Invalid request body: {exc}") from exc
    return {"filters": {}, "page": {"page": 0, "size": 10}}


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search SWTR units via /rest/api/unit/v3 endpoints. Prints JSON to stdout.")
    parser.add_argument("--body-file", help="JSON file with QLRequestDto body")
    parser.add_argument("--body-json", help="Inline JSON body for QLRequestDto")
    parser.add_argument("--filter", help="Fast filter identifier for /rest/api/unit/v3/{filter}/find")
    parser.add_argument("--count", action="store_true", help="Call /rest/api/unit/v3/count instead of find")
    parser.add_argument("--base-url", default=None,
                        help="Override SWTR_BASE_URL (resolved after the env file loads; default https://portal.works.prod.sbt/swtr)")
    parser.add_argument("--env-file", default="~/.swtr-review.env")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds (default 30)")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    load_env_file(Path(args.env_file).expanduser())
    # Resolve the base URL AFTER the env file is loaded so a SWTR_BASE_URL set
    # only in ~/.swtr-review.env is honored (an explicit --base-url still wins).
    if getattr(args, "base_url", None) in (None, ""):
        args.base_url = base_url_for("task_tracker")
    clean_base = args.base_url.rstrip("/")
    if args.count:
        endpoint = "/rest/api/unit/v3/count"
    elif args.filter:
        endpoint = f"/rest/api/unit/v3/{urllib.parse.quote(args.filter, safe='')}/find"
    else:
        endpoint = "/rest/api/unit/v3/find"
    url = clean_base + endpoint
    try:
        # build_headers() is inside the try so a missing-auth RuntimeError reaches
        # emit_error (unified error contract), not an uncaught traceback.
        headers = build_headers("task_tracker", require_auth=True)
        body = read_body(args)
        result, _, _ = request_http("POST", url, headers, body=body, timeout=args.timeout, system="task_tracker")
    except RuntimeError as exc:
        return emit_error("swtr_unit_search", exc)
    print(json.dumps(
        {
            "ok": True,
            "endpoint": endpoint,
            "request": {"url": url, "headers": safe_headers_for_log(headers), "body": body},
            "response": result,
            # Веб-ссылки на найденные юниты отдельной картой: ответ трекера
            # остаётся ровно тем, что он прислал, а собирать ссылку по образцу
            # вызывающему больше не нужно.
            "urls": unit_urls_in(clean_base, result),
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
