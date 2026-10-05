"""Тело wiki-страницы SWTR: markdown → документ редактора.

Страница хранится в атрибуте `wiki_page_body` (тип `wiki_page_wisiwig`), и его
значение — СТРОКА, внутри которой лежит JSON-документ ProseMirror. Объект вместо
строки трекер отвергает HTTP 500, голый текст без разметки — тоже.

Схема узлов снята с живых страниц пространства PVM, а не придумана: трекер
принимает любой JSON и не валидирует его, поэтому единственный ориентир —
то, что пишет сам редактор.
"""

import json
import re
import uuid
from typing import Any, Callable, Dict, List, Optional

# Атрибуты, которые редактор ставит каждому блоку текста.
TEXT_ALIGN = "justify"

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
BULLET_RE = re.compile(r"^\s*[-*+]\s+(?!\[[ xX]\]\s)(.*)$")
ORDERED_RE = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
TASK_RE = re.compile(r"^\s*[-*+]\s+\[([ xX])\]\s+(.*)$")


def new_id() -> str:
    return str(uuid.uuid4())


def _block_attrs(id_factory: Callable[[], str]) -> Dict[str, Any]:
    return {"id": id_factory(), "indent": 0, "textAlign": TEXT_ALIGN}


# Разметка внутри строки. Порядок важен: код забирает свой кусок первым и внутрь
# него разбор уже не заходит, иначе `**звёздочки**` в примере кода станут жирными.
# У подчёркивания и звёздочки границы сужены: иначе `swtr_task_tool` превращается
# в курсив, а «2 * 2» — в открытый маркер.
INLINE_RE = re.compile(
    r"(?P<code>`(?P<code_text>[^`]+)`)"
    r"|(?P<link>\[(?P<link_text>[^\]]+)\]\((?P<link_href>[^)\s]+)\))"
    r"|(?P<bold>\*\*(?P<bold_text>[^*]+)\*\*)"
    r"|(?P<strike>~~(?P<strike_text>.+?)~~)"
    r"|(?P<istar>(?<!\*)\*(?P<istar_text>[^*\s][^*]*?)\*(?!\*))"
    r"|(?P<iunder>(?<!\w)_(?P<iunder_text>[^_\s][^_]*?)_(?!\w))"
)

LINK_ATTRS = {"target": "_blank", "rel": "noopener noreferrer nofollow", "class": None}


def _text_node(text: str, marks: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    node: Dict[str, Any] = {"type": "text", "text": text}
    if marks:
        node["marks"] = marks
    return node


def _inline_line(line: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    position = 0
    for match in INLINE_RE.finditer(line):
        if match.start() > position:
            out.append(_text_node(line[position:match.start()]))
        if match.group("code"):
            out.append(_text_node(match.group("code_text"), [{"type": "code"}]))
        elif match.group("link"):
            attrs = dict(LINK_ATTRS, href=match.group("link_href"))
            out.append(_text_node(match.group("link_text"), [{"type": "link", "attrs": attrs}]))
        elif match.group("bold"):
            out.append(_text_node(match.group("bold_text"), [{"type": "bold"}]))
        elif match.group("strike"):
            out.append(_text_node(match.group("strike_text"), [{"type": "strike"}]))
        else:
            text = match.group("istar_text") or match.group("iunder_text")
            out.append(_text_node(text, [{"type": "italic"}]))
        position = match.end()
    if position < len(line):
        out.append(_text_node(line[position:]))
    return out


def _inline(text: str) -> List[Dict[str, Any]]:
    """Строки абзаца: перевод строки внутри блока — это hardBreak, как в редакторе."""
    out: List[Dict[str, Any]] = []
    for index, line in enumerate(text.split("\n")):
        if index:
            out.append({"type": "hardBreak"})
        if line:
            out.extend(_inline_line(line))
    return out


def _paragraph(text: str, id_factory: Callable[[], str]) -> Dict[str, Any]:
    return {"type": "paragraph", "attrs": _block_attrs(id_factory),
            "content": _inline(text)}


def _heading(level: int, text: str, id_factory: Callable[[], str]) -> Dict[str, Any]:
    attrs = _block_attrs(id_factory)
    attrs["level"] = level
    return {"type": "heading", "attrs": attrs, "content": _inline(text)}


def _list_item(text: str, id_factory: Callable[[], str]) -> Dict[str, Any]:
    return {"type": "listItem", "attrs": {"id": id_factory()},
            "content": [_paragraph(text, id_factory)]}


def _task_item(text: str, checked: bool, id_factory: Callable[[], str]) -> Dict[str, Any]:
    return {"type": "taskItem", "attrs": {"id": id_factory(), "checked": checked, "indent": 0},
            "content": [_paragraph(text, id_factory)]}


def _list_block(block: str, id_factory: Callable[[], str]) -> Optional[Dict[str, Any]]:
    """Список целиком: блок считается списком, если так размечена ПЕРВАЯ строка."""
    lines = block.split("\n")
    if TASK_RE.match(lines[0]):
        items = []
        for line in lines:
            match = TASK_RE.match(line)
            if not match:
                return None
            items.append(_task_item(match.group(2).strip(),
                                    match.group(1).lower() == "x", id_factory))
        return {"type": "taskList", "attrs": {"id": id_factory(), "textAlign": None},
                "content": items}
    if BULLET_RE.match(lines[0]):
        items = []
        for line in lines:
            match = BULLET_RE.match(line)
            if not match:
                return None
            items.append(_list_item(match.group(1).strip(), id_factory))
        return {"type": "bulletList", "attrs": {"id": id_factory()}, "content": items}
    first = ORDERED_RE.match(lines[0])
    if first:
        items = []
        for line in lines:
            match = ORDERED_RE.match(line)
            if not match:
                return None
            items.append(_list_item(match.group(2).strip(), id_factory))
        return {"type": "orderedList",
                "attrs": {"id": id_factory(), "type": None, "start": int(first.group(1))},
                "content": items}
    return None


SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
# Ширина колонки: редактор хранит пиксели, 492 — то, что он ставит двум колонкам.
DEFAULT_COLUMN_WIDTH = 492


def _table_cells(line: str) -> List[str]:
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [cell.strip() for cell in row.split("|")]


def _table_cell(kind: str, text: str, id_factory: Callable[[], str]) -> Dict[str, Any]:
    return {"type": kind,
            "attrs": {"id": id_factory(), "colspan": 1, "rowspan": 1,
                      "colwidth": None, "backgroundColor": None},
            "content": [_paragraph(text, id_factory)]}


def _table_block(block: str, id_factory: Callable[[], str]) -> Optional[Dict[str, Any]]:
    """GFM-таблица: шапка, строка-разделитель, дальше данные."""
    lines = block.split("\n")
    if len(lines) < 2 or "|" not in lines[0] or not SEPARATOR_RE.match(lines[1]):
        return None
    header = _table_cells(lines[0])
    rows = [{"kind": "tableHeader", "cells": header}]
    for line in lines[2:]:
        cells = _table_cells(line)
        cells += [""] * (len(header) - len(cells))
        rows.append({"kind": "tableCell", "cells": cells[:len(header)]})
    return {
        "type": "table",
        "attrs": {"id": id_factory(), "tableId": id_factory(),
                  "columnWidths": [DEFAULT_COLUMN_WIDTH] * len(header),
                  "hasHeaderRow": True, "hasHeaderColumn": False,
                  "filters": [], "summaryRow": {}, "numberedActive": False,
                  "sort": None, "style": None, "calcColumn": None},
        "content": [
            {"type": "tableRow",
             "attrs": {"id": id_factory(), "filterHidden": None,
                       "originalRowIndex": index, "readOnly": None},
             "content": [_table_cell(row["kind"], text, id_factory) for text in row["cells"]]}
            for index, row in enumerate(rows)],
    }


def _split_blocks(markdown: str) -> List[Dict[str, Any]]:
    """Блоки — куски, разделённые пустой строкой.

    Ограждённый код — исключение: внутри ``` пустая строка ничего не разделяет,
    иначе пример кода разъезжается на несколько блоков.
    """
    blocks: List[Dict[str, Any]] = []
    current: List[str] = []
    fence: Optional[Dict[str, Any]] = None

    def flush() -> None:
        if current:
            blocks.append({"kind": "text", "text": "\n".join(current)})
            current.clear()

    for line in markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if fence is not None:
            if line.strip().startswith("```"):
                blocks.append({"kind": "code", "language": fence["language"],
                               "text": "\n".join(fence["lines"])})
                fence = None
            else:
                fence["lines"].append(line)
            continue
        if line.strip().startswith("```"):
            flush()
            fence = {"language": line.strip()[3:].strip() or None, "lines": []}
            continue
        if line.strip():
            current.append(line)
        else:
            flush()
    if fence is not None:  # незакрытая ограда — код всё равно код
        blocks.append({"kind": "code", "language": fence["language"],
                       "text": "\n".join(fence["lines"])})
    flush()
    return blocks


def markdown_to_doc(markdown: str,
                    id_factory: Optional[Callable[[], str]] = None) -> Dict[str, Any]:
    """Markdown → документ ProseMirror того вида, что пишет редактор SWTR."""
    ids = id_factory or new_id
    content: List[Dict[str, Any]] = []
    for raw in _split_blocks(markdown or ""):
        if raw["kind"] == "code":
            content.append({
                "type": "codeBlock",
                "attrs": {"id": ids(), "language": raw["language"]},
                "content": [_text_node(raw["text"])] if raw["text"] else [],
            })
            continue
        block = raw["text"]
        heading = HEADING_RE.match(block)
        if heading and "\n" not in block:
            content.append(_heading(len(heading.group(1)), heading.group(2).strip(), ids))
            continue
        listing = _list_block(block, ids)
        if listing is not None:
            content.append(listing)
            continue
        table = _table_block(block, ids)
        if table is not None:
            content.append(table)
            continue
        content.append(_paragraph(block, ids))
    return {"type": "doc", "content": content}


def doc_to_value(doc: Dict[str, Any]) -> str:
    """Значение атрибута — строка с JSON внутри; объект трекер не принимает."""
    return json.dumps(doc, ensure_ascii=False)


def body_value(source: Any, id_factory: Optional[Callable[[], str]] = None) -> str:
    """Готовое значение `wiki_page_body` из markdown, готового документа или dict.

    Всегда строка. Готовый документ ProseMirror проходит насквозь: агент, который
    уже собрал узлы сам (макросы, виджеты задач, картинки), не должен терять их
    на конвертере.
    """
    if isinstance(source, (dict, list)):
        return json.dumps(source, ensure_ascii=False)
    if source is not None and not isinstance(source, str):
        # Значение прилетает из --attribute, где `123` и `true` разбираются как
        # JSON, то есть в int и bool. Без этой проверки они доходят до .strip()
        # и рвут процесс трейсбеком мимо контракта ошибки навыка. Число как
        # текст документа не угадываем: намерение неочевидно, а поле есть у
        # каждого wisiwig-атрибута.
        raise RuntimeError(
            f"Значение поля с форматированным текстом задано типом {type(source).__name__}. "
            f"Ожидается текст, markdown или документ ProseMirror; число или логическое значение "
            f"передавайте строкой: 'значение'.")
    text = source or ""
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and parsed.get("type") == "doc":
            return json.dumps(parsed, ensure_ascii=False)
    return doc_to_value(markdown_to_doc(text, id_factory))
