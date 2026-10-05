#!/usr/bin/env python3
"""Fetch and normalize a SWTR unit, SWTR wiki page, or local review source.

Prints a JSON document to stdout with the unit, wiki payloads, and a normalized
summary suitable as input for a document review. The script never persists
secrets and no longer writes intermediate files; the caller (the agent or a
review skill) decides whether to save a final review report.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import swtr_unit_comments as rich  # noqa: E402

from _swtr_common import (  # noqa: E402
    base_url_for,
    build_headers,
    build_ssl_context,
    emit_error,
    load_env_file,
    redact_secrets,
    request_http,
    safe_headers_for_log,
    UNIT_CODE_RE,
    unit_url,
    value_to_text,
)
LONG_TEXT_KEYS = {
    "body",
    "content",
    "description",
    "details",
    "html",
    "markup",
    "storage",
    "text",
    "value",
    "wiki",
    "wikiBody",
    "wikiText",
}


def request_json(url: str, headers: Dict[str, str], timeout: float) -> Any:
    """GET helper delegating to the shared request_http (auth/redaction/retry)."""
    payload, _, _ = request_http("GET", url, headers, timeout=timeout, system="task_tracker")
    return payload


def detect_input(value: str) -> Tuple[str, Optional[str], Optional[Path]]:
    candidate = Path(value).expanduser()
    if candidate.exists() and candidate.is_file():
        return "local_file", None, candidate
    match = UNIT_CODE_RE.search(value.upper())
    if match:
        return "swtr_unit", match.group(0), None
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme in {"http", "https"}:
        decoded = urllib.parse.unquote(value)
        match = UNIT_CODE_RE.search(decoded.upper())
        if match:
            return "swtr_unit", match.group(0), None
    raise SystemExit(
        "Input is not a readable local file, SWTR URL, or SWTR unit code. "
        "Examples: CRPV-35498, PVM-53979, /workspace/review.md."
    )


def slugify(text: str) -> str:
    normalized = text.lower().strip()
    normalized = re.sub(r"[^a-z0-9а-яё]+", "-", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    return normalized[:80] or "review-source"


def readable_text(value: Any) -> str:
    """Читаемый текст значения трекера, включая документ ProseMirror.

    Описание задачи, значения атрибутов и тело вики хранятся СТРОКОЙ, внутри
    которой лежит JSON-документ редактора. `value_to_text` разворачивает HTML, но
    такую строку отдаёт как есть, и в источник уезжала разметка: на живом эпике
    CRPV-110079 это 8679 знаков JSON вместо 2987 знаков текста. Читают эти поля
    ревью документа (сценарий 4) и `openspec_seed` (сценарий 18), поэтому
    разворачивать надо здесь, а не в каждом потребителе по копии.

    Сырое значение не теряется: `unit` и `wiki` источник отдаёт целиком, и
    ссылки из `attrs.href` берутся оттуда (`rich.collect_rich_hrefs`).
    """
    parsed = rich.parse_comment_text(value)
    return parsed["text"] or value_to_text(value, strip_html=True)


def extract_attributes(unit: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_attributes = unit.get("attributes") or []
    normalized: List[Dict[str, Any]] = []
    if not isinstance(raw_attributes, list):
        return normalized
    for item in raw_attributes:
        if not isinstance(item, dict):
            continue
        attribute_info = item.get("attribute") if isinstance(item.get("attribute"), dict) else item
        code = attribute_info.get("code") or item.get("code") or "unknown"
        name = attribute_info.get("name") or item.get("name") or code
        attr_type = attribute_info.get("type") or item.get("type") or "unknown"
        value = item.get("value")
        normalized.append(
            {
                "code": code,
                "name": name,
                "type": attr_type,
                "value": value,
                "text": readable_text(value),
            }
        )
    return normalized


def flatten_long_texts(obj: Any, path: str = "root") -> List[Tuple[str, str]]:
    results: List[Tuple[str, str]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            next_path = f"{path}.{key}"
            if isinstance(value, str):
                text = readable_text(value)
                if len(text) >= 80 or key in LONG_TEXT_KEYS:
                    results.append((next_path, text))
            else:
                results.extend(flatten_long_texts(value, next_path))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            results.extend(flatten_long_texts(value, f"{path}[{index}]"))
    return results


# Плагин вики эхом отдаёт карточку самого юнита: в v2 под `info`, в v1 плоско.
# Её `summary` и `description` уже приезжают отдельными полями источника, поэтому
# доказательством того, что вики прочиталась, они не являются. Порог извлекателя —
# 80 знаков, и на девяти живых юнитах у четырёх `summary` его переходит, так что
# без этого отсева эхо карточки выдавало бы себя за текст вики. Тело вики лежит в
# `details`, отдельно от карточки.
UNIT_CARD_KEYS = ("summary", "description")


def is_unit_card_path(path: str, name: str) -> bool:
    """Путь указывает на эхо карточки юнита, а не на тело вики.

    Правило вынесено из `wiki_body_texts` отдельной функцией, потому что тот же
    вопрос задаёт потребитель `wikiTexts` (`spec_sources.py`): без общего
    предиката он сравнивал бы тексты строками, а после `strip_html` такое
    сравнение расходится.
    """
    return path == f"{name}.summary" or path == f"{name}.description" \
        or path.startswith(f"{name}.info.")


def wiki_body_texts(payload: Any, name: str) -> List[Tuple[str, str]]:
    """Длинные тексты ответа вики за вычетом эха карточки юнита.

    Карточка вычитается целиком, а не двумя известными ключами: в v2 это весь
    подузел `info`, в v1 — те же поля плоско. Порог в 80 знаков тут не защищает —
    `flatten_long_texts` берёт строку любой длины, если её ключ входит в
    `LONG_TEXT_KEYS` (`value`, `text`, `content`, `description`), а ровно так
    SWTR отдаёт атрибуты. Любое поле карточки сошло бы за прочитанную вики.
    """
    return [
        (path, text)
        for path, text in flatten_long_texts(payload, name)
        if not is_unit_card_path(path, name)
    ]


def read_local_file(path: Path) -> Tuple[str, Optional[Any]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    parsed: Optional[Any] = None
    if path.suffix.lower() == ".json":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
    return text, parsed


def collect_local_source(input_value: str, file_path: Path) -> Dict[str, Any]:
    text, parsed = read_local_file(file_path)
    return {
        "input": input_value,
        "inputType": "local_file",
        "slug": slugify(file_path.stem),
        "filePath": str(file_path),
        "fetchedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "textLength": len(text),
        "text": text,
        "parsedJson": parsed,
    }


def collect_swtr_source(
    input_value: str,
    code: str,
    base_url: str,
    headers: Dict[str, str],
    timeout: float,
    include_wiki: bool,
) -> Dict[str, Any]:
    encoded_code = urllib.parse.quote(code, safe="")
    clean_base = base_url.rstrip("/")
    unit_api_url = f"{clean_base}/rest/api/unit/v2/{encoded_code}"
    unit = request_json(unit_api_url, headers, timeout)
    if not isinstance(unit, dict):
        unit = {"raw": unit}

    wiki_payloads: Dict[str, Any] = {}
    if include_wiki:
        wiki_endpoints = [
            ("wiki-v2", f"{clean_base}/extension/plugin/v2/rest/api/swtr_wiki_plugin/v2/wiki/unit/{encoded_code}"),
            ("wiki-v1", f"{clean_base}/extension/plugin/v2/rest/api/swtr_wiki_plugin/v1/wiki/unit/{encoded_code}"),
        ]
        # v1 — запасной путь, а не вторая половина ответа. Пока v2 отдаёт вики,
        # второй вызов приносит тот же текст: лишний поход в сеть и удвоенный
        # источник (проверено на восьми юнитах — совпадение до символа).
        #
        # Решает именно извлечённый текст, а не сам ответ: на живом портале v2
        # отвечает конвертом с метаданными юнита всегда, даже когда вики нет, —
        # `{"details": null, "info": {...}}`. Условие «ответ непустой» на нём
        # истинно всегда, и запасной путь стал бы недостижим.
        for name, url in wiki_endpoints:
            try:
                payload = request_json(url, headers, timeout)
            except RuntimeError as exc:
                wiki_payloads[f"{name}.error"] = redact_secrets(str(exc))
                continue
            if payload is None:
                continue
            wiki_payloads[name] = payload
            if wiki_body_texts(payload, name):
                break

    attributes = extract_attributes(unit)
    summary = readable_text(unit.get("summary"))
    description = readable_text(unit.get("description"))
    wiki_texts = []
    seen_texts = set()
    for name, payload in wiki_payloads.items():
        if name.endswith(".error"):
            continue
        for path, text in flatten_long_texts(payload, name):
            # Когда v2 не дал вики и прочитана ещё и v1, обе версии приносят одну
            # и ту же карточку. Дважды в источнике она не нужна: это и есть то
            # удвоение, ради которого запасной путь перестал вызываться всегда.
            if text in seen_texts:
                continue
            seen_texts.add(text)
            wiki_texts.append({"path": path, "text": text})

    return {
        "input": input_value,
        "inputType": "swtr_unit",
        "slug": slugify(code),
        "code": code,
        "baseUrl": clean_base,
        # Две разные ссылки, и их путали: `url` — карточка для человека,
        # `unitApiUrl` — адрес, с которого приехали данные. Раньше здесь лежал
        # только API-адрес под именем `unitUrl`, и он же печатался в proposal
        # как «источник»: по такой ссылке открывался JSON.
        "url": unit_url(clean_base, code),
        "unitApiUrl": unit_api_url,
        "fetchedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "summary": summary,
        "description": description,
        "space": unit.get("space"),
        "suit": unit.get("suit"),
        "createdAt": value_to_text(unit.get("createdAt"), strip_html=True),
        "updatedAt": value_to_text(unit.get("updatedAt"), strip_html=True),
        "attributes": attributes,
        "unit": unit,
        "wiki": wiki_payloads,
        "wikiTexts": wiki_texts,
    }


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch and normalize SWTR or local review sources. Prints JSON to stdout.")
    parser.add_argument("input", help="SWTR unit code, SWTR URL, or local file path")
    parser.add_argument("--base-url", default=None,
                        help="Override SWTR_BASE_URL (resolved after the env file loads; default https://portal.works.prod.sbt/swtr)")
    parser.add_argument("--env-file", default="~/.swtr-review.env", help="Optional local env file with SWTR secrets")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    parser.add_argument("--skip-wiki", action="store_true", help="Do not try wiki page endpoints")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    load_env_file(Path(args.env_file).expanduser())
    # Resolve the base URL AFTER the env file is loaded so a SWTR_BASE_URL set
    # only in ~/.swtr-review.env is honored (an explicit --base-url still wins).
    if getattr(args, "base_url", None) in (None, ""):
        args.base_url = base_url_for("task_tracker")

    input_type, code, file_path = detect_input(args.input)
    try:
        if input_type == "local_file" and file_path is not None:
            source = collect_local_source(args.input, file_path)
            request_info: Dict[str, Any] = {"type": "local_file", "filePath": str(file_path)}
        else:
            headers = build_headers("task_tracker", require_auth=True)
            request_info = {
                "baseUrl": args.base_url.rstrip("/"),
                "headers": safe_headers_for_log(headers),
                "includeWiki": not args.skip_wiki,
                "skipTlsVerify": build_ssl_context("task_tracker") is not None,
            }
            source = collect_swtr_source(
                input_value=args.input,
                code=code or "",
                base_url=args.base_url,
                headers=headers,
                timeout=args.timeout,
                include_wiki=not args.skip_wiki,
            )
    except RuntimeError as exc:
        return emit_error("fetch_swtr_sources", exc)

    print(json.dumps({"ok": True, "request": request_info, "source": source}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
