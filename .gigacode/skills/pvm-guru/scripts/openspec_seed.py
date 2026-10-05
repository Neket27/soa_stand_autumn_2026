#!/usr/bin/env python3
"""Заготовка proposal.md формата OpenSpec из задачи трекера.

Печатает в stdout один JSON-объект. Файлов не создаёт: готовый черновик лежит
в поле `markdown`, записывает его агент своим инструментом.

Главное правило: **ничего не выдумывать**. Раздела нет в источнике — он идёт
пустым с меткой `[🔴 Взято с потолка (<40%)]` и попадает в список вопросов.
Правдоподобный текст на месте отсутствующих данных — ровно та болезнь
автогенерации, против которой заведены метки уверенности.

Скелет — нативный OpenSpec 1.5.0: `## Why`, `## What Changes`, `## Capabilities`,
`## Impact`. Критерии приёмки из задачи в proposal не идут: нативного раздела под
них нет, их место — сценарии дельта-спеки, поэтому они отдаются отдельным полем.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _swtr_common import (  # noqa: E402
    base_url_for,
    build_headers,
    emit_error,
    load_env_file,
)
import fetch_swtr_sources as fetch  # noqa: E402

# --- метки уверенности -------------------------------------------------------
# Формулировки дословно из guru-openspec/references/confidence-markers.md:
# расхождение в тексте метки ломает разбор на стороне потребителя.

MARKER_SURE = "[🟢 Абсолютно уверен (≈100-80%)]"
MARKER_ALMOST = "[🔵 Почти уверен (≈80-60%)]"
MARKER_UNSURE = "[🟡 Не уверен (≈60-40%)]"
MARKER_GUESS = "[🔴 Взято с потолка (<40%)]"

# --- откуда берутся разделы --------------------------------------------------
# Атрибуты юнита опознаются по имени, а не по коду: коды различаются между
# пространствами SWTR, имена — нет. Таблица вынесена в константу, чтобы её можно
# было прочитать глазами и поправить, не читая код.

ATTRIBUTE_HINTS: Dict[str, Tuple[str, ...]] = {
    "what_changes": ("состав работ", "что делаем", "что меняется", "объём работ", "объем работ",
                     "описание решения", "решение"),
    "acceptance": ("критери", "приёмк", "приемк", "definition of done", "dod"),
    "constraints": ("ограничен", "границ", "вне scope", "не входит"),
    "impact": ("влияни", "затрагива", "затронут", "интеграц", "смежн"),
}

# Раздел → (заголовок, из какого поля берём, что спросить, если пусто)
SECTIONS: Tuple[Tuple[str, str, str], ...] = (
    ("Why", "description",
     "В чём проблема и почему её решаем сейчас? В описании задачи этого нет"),
    ("What Changes", "what_changes",
     "Что конкретно меняется? Состава работ в задаче нет"),
    ("Capabilities", "capabilities",
     "Какие capability добавляются или меняются? Из задачи это не выводится — "
     "нужен список kebab-case имён, каждое станет specs/<name>/spec.md"),
    ("Impact", "impact",
     "Какой код, API и смежные системы затрагиваются?"),
)

MIN_MEANINGFUL_CHARS = 40


def emit(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def resolve_openspec_tools() -> Optional[Path]:
    """Найти openspec_tools.py соседнего навыка перебором путей установки."""
    candidates = [SKILL_DIR.parent / "guru-openspec" / "scripts" / "openspec_tools.py"]
    home = Path.home()
    for base in (
        ".gigacode/skills", ".agents/skills",
        ".gigacode/skills", ".claude/skills", ".config/opencode/skills", ".codex/skills",
    ):
        candidates.append(home / base / "guru-openspec" / "scripts" / "openspec_tools.py")
    for path in candidates:
        if path.is_file():
            return path
    return None


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}


def _local_slug(title: str) -> str:
    """Запасная транслитерация, если соседнего guru-openspec рядом нет."""
    text = "".join(_TRANSLIT.get(ch, ch) for ch in title.lower())
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return re.sub(r"-+", "-", text)[:60]


def build_change_id(task: Optional[str], title: str) -> Dict[str, Any]:
    """change-id собирает guru-openspec; свой разбор — запасной вариант."""
    tools = resolve_openspec_tools()
    if tools is not None:
        argv = [sys.executable, str(tools), "changeid", "--title", title or ""]
        if task:
            argv += ["--task", task]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
            payload = json.loads(proc.stdout)
            if payload.get("change_id"):
                return {
                    "change_id": payload["change_id"],
                    "slug": payload.get("slug", ""),
                    "task": payload.get("task"),
                    "source": "guru-openspec",
                    "warnings": list(payload.get("warnings", [])),
                }
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    slug = _local_slug(title) or "change"
    code = (task or "").strip().upper()
    return {
        "change_id": f"{code}-{slug}" if code else slug,
        "slug": slug,
        "task": code or None,
        "source": "local",
        "warnings": ["guru-openspec не найден рядом — change-id собран своим разбором"],
    }


def _text_of(value: Any) -> str:
    return (value or "").strip() if isinstance(value, str) else ""


def pick_attribute(attributes: Sequence[Dict[str, Any]], kind: str) -> Optional[Dict[str, Any]]:
    """Первый атрибут, чьё имя похоже на искомое и у которого есть текст."""
    hints = ATTRIBUTE_HINTS.get(kind, ())
    for attribute in attributes:
        name = str(attribute.get("name") or attribute.get("code") or "").lower()
        if not any(hint in name for hint in hints):
            continue
        text = _text_of(attribute.get("text"))
        if text:
            return {"name": attribute.get("name"), "code": attribute.get("code"), "text": text}
    return None


def confidence_for(text: str, origin: Optional[str]) -> str:
    """Метка ставится по происхождению текста, а не по его длине."""
    if not text:
        return MARKER_GUESS
    if origin is None:
        return MARKER_GUESS
    if len(text) < MIN_MEANINGFUL_CHARS:
        return MARKER_UNSURE
    return MARKER_ALMOST


def collect_fields(source: Dict[str, Any]) -> Dict[str, Optional[Dict[str, Any]]]:
    """Сырые поля источника, разложенные по назначению."""
    attributes = source.get("attributes") or []
    description = _text_of(source.get("description"))
    fields: Dict[str, Optional[Dict[str, Any]]] = {
        "description": {"text": description, "name": "description"} if description else None,
        "capabilities": None,  # из задачи трекера не выводится никогда
    }
    for kind in ("what_changes", "acceptance", "constraints", "impact"):
        fields[kind] = pick_attribute(attributes, kind)
    return fields


def build_sections(fields: Dict[str, Optional[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    sections: List[Dict[str, Any]] = []
    for name, field_key, question in SECTIONS:
        field = fields.get(field_key)
        text = _text_of(field.get("text")) if field else ""
        sections.append(
            {
                "name": name,
                "text": text,
                "confidence": confidence_for(text, field.get("name") if field else None),
                "from": field.get("name") if field else None,
                "question": question,
            }
        )
    return sections


def render_markdown(change_id: str, task: Optional[str], summary: str,
                    sections: Sequence[Dict[str, Any]], unit_url: Optional[str]) -> str:
    lines = [
        "> **Статус**: На согласовании",
        "> **Целевая спецификация**: отсутствует",
        f"> **Задача трекера**: {task or 'нет'}",
        "",
        f"<!-- change-id: {change_id} -->",
        f"<!-- источник: {unit_url or 'локальный файл'} -->",
        "",
    ]
    if summary:
        lines += [f"<!-- тема задачи: {summary} -->", ""]
    for section in sections:
        lines.append(f"## {section['name']} {section['confidence']}")
        lines.append("")
        if section["text"]:
            lines.append(section["text"])
        else:
            lines.append(f"<!-- в источнике нет данных. Вопрос: {section['question']} -->")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def seed_from_source(source: Dict[str, Any]) -> Dict[str, Any]:
    """Заготовка proposal по нормализованному источнику fetch_swtr_sources."""
    result: Dict[str, Any] = {
        "task": None,
        "change_id": None,
        "slug": None,
        "source": {},
        "sections": [],
        "acceptance_criteria": None,
        "constraints": None,
        "questions": [],
        "missing": [],
        "markdown": "",
        "errors": [],
        "warnings": [],
    }

    summary = _text_of(source.get("summary"))
    code = _text_of(source.get("code")) or None
    if not summary:
        result["warnings"].append(
            "у источника нет темы — slug для change-id собран не будет, назови тему сам"
        )

    identity = build_change_id(code, summary)
    result.update(
        {"change_id": identity["change_id"], "slug": identity["slug"], "task": identity["task"]}
    )
    result["warnings"].extend(identity["warnings"])
    result["source"] = {
        "type": source.get("inputType"),
        "code": code,
        "summary": summary,
        # `url` — карточка юнита. Старые сохранённые источники несут только
        # `unitUrl` (там лежал адрес API), поэтому он остаётся запасным.
        "url": source.get("url") or source.get("unitUrl"),
        "fetchedAt": source.get("fetchedAt"),
    }

    fields = collect_fields(source)
    result["sections"] = build_sections(fields)
    for section in result["sections"]:
        if not section["text"]:
            result["missing"].append(section["name"])
            result["questions"].append(f"{section['name']}: {section['question']}")
        elif section["confidence"] == MARKER_UNSURE:
            result["questions"].append(
                f"{section['name']}: в источнике всего {len(section['text'])} символов — "
                "хватает ли этого, или раздел надо дописать?"
            )

    acceptance = fields.get("acceptance")
    if acceptance:
        result["acceptance_criteria"] = {
            "from": acceptance.get("name"),
            "text": acceptance["text"],
            "note": "в proposal нативного раздела под критерии нет — они станут сценариями "
                    "дельта-спеки в guru-brainstorm",
        }
    else:
        result["questions"].append(
            "Критерии приёмки: в задаче их нет — без них не из чего строить сценарии "
            "`#### Scenario:` в дельта-спеке"
        )

    constraints = fields.get("constraints")
    if constraints:
        result["constraints"] = {"from": constraints.get("name"), "text": constraints["text"]}

    result["markdown"] = render_markdown(
        result["change_id"], result["task"], summary, result["sections"],
        result["source"]["url"]
    )
    return result


def load_source(args: argparse.Namespace) -> Dict[str, Any]:
    """Источник: из готового JSON или свежим запросом теми же функциями."""
    if args.source_file:
        payload = json.loads(Path(args.source_file).expanduser().read_text(encoding="utf-8"))
        source = payload.get("source") if isinstance(payload, dict) else None
        return source if isinstance(source, dict) else payload

    load_env_file(Path(args.env_file).expanduser())
    if getattr(args, "base_url", None) in (None, ""):
        args.base_url = base_url_for("task_tracker")
    input_type, code, file_path = fetch.detect_input(args.input)
    if input_type == "local_file" and file_path is not None:
        return fetch.collect_local_source(args.input, file_path)
    headers = build_headers("task_tracker", require_auth=True)
    return fetch.collect_swtr_source(
        input_value=args.input,
        code=code or "",
        base_url=args.base_url,
        headers=headers,
        timeout=args.timeout,
        include_wiki=not args.skip_wiki,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Заготовка proposal.md формата OpenSpec из задачи трекера. "
                    "Печатает один JSON-объект в stdout; файлов не создаёт.",
    )
    parser.add_argument("input", nargs="?", help="Код юнита SWTR, ссылка или путь к файлу")
    parser.add_argument("--source-file", help="Готовый вывод fetch_swtr_sources.py вместо запроса")
    parser.add_argument("--base-url", default=None, help="Переопределить SWTR_BASE_URL")
    parser.add_argument("--env-file", default="~/.swtr-review.env")
    parser.add_argument("--timeout", type=float, default=30.0, help="Таймаут HTTP в секундах")
    parser.add_argument("--skip-wiki", action="store_true", help="Не ходить в wiki-эндпоинты")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.input and not args.source_file:
        emit({
            "command": "openspec-seed",
            "ok": False,
            "errors": ["нужен код задачи, ссылка, путь к файлу или --source-file"],
            "warnings": [],
        })
        return 1
    try:
        source = load_source(args)
    except (RuntimeError, OSError, ValueError) as exc:
        return emit_error("openspec_seed", exc)

    result = seed_from_source(source)
    result["command"] = "openspec-seed"
    result["ok"] = not result["errors"]
    emit(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
