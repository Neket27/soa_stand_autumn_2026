"""Шаги тест-кейса TMS: простой текст → форма записи трекера.

Форма ЧТЕНИЯ и форма ЗАПИСИ у атрибута `test_step` разные, и расхождение
наказывается молчанием, а не отказом. Читается список шагов:

    [{"code": "…", "stepNumber": 1, "stepDescription": {"text": "<ProseMirror>"}, …}]

Пишется объект-обёртка, а части шага несут ДВА поля — форматированный документ
строкой и его же плоский текст:

    {"testStepList": [{"code": null, "deleted": false, "callToTestId": null,
                       "stepType": "step_by_step", "stepNumber": 1,
                       "stepDescription": {"formattedText": "<ProseMirror>",
                                           "plainText": "…"},
                       "stepData": {…}, "stepResult": {…},
                       "files": [], "stepFiles": []}]}

Отправить на запись прочитанное — получить `200` и пустые шаги: трекер принимает
тело, но список до кейса не доезжает. Отправить строку или голый текст в частях —
получить `HTTP 500` без имени поля. Проверено записью 18.08.2026 (PVM-177292,
PVM-177308); независимо подтверждается реализацией плагина TMS.

`stepType` у кейса взаимоисключающий: `step_by_step` либо `plain_text`, и
переключение на `plain_text` безвозвратно удаляет шаги. Здесь пишется только
`step_by_step` — режим сплошного текста навык не собирает.
"""

import json
from typing import Any, Dict, List, Optional, Sequence

import wiki_body

STEP_TYPE = "step_by_step"
# Разделитель частей в `--step "действие|данные|результат"`. Экранированный
# `\|` — литерал: вертикальная черта живёт внутри таблиц и регулярок, и резать
# по ней текст шага значит терять его молча.
STEP_FIELD_SEPARATOR = "|"
STEP_PART_KEYS = ("description", "data", "result")
PART_ATTRIBUTE_KEYS = {
    "description": "stepDescription",
    "data": "stepData",
    "result": "stepResult",
}


def split_step_spec(spec: str) -> List[str]:
    """Разбить строку по неэкранированному `|`, оставив `\\|` литералом."""
    parts: List[str] = []
    current: List[str] = []
    escaped = False
    for char in spec:
        if escaped:
            # Экранируется только сам разделитель: `\n` и прочие последовательности
            # в тексте шага должны доехать как есть.
            current.append(char if char == STEP_FIELD_SEPARATOR else "\\" + char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == STEP_FIELD_SEPARATOR:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    parts.append("".join(current))
    return parts


def parse_step_spec(spec: str) -> Dict[str, str]:
    """Шаг из строки `действие|данные|результат`.

    Недостающие части пустые: шаг без тестовых данных — норма, шаг без действия —
    нет, поэтому пустая спецификация отвергается здесь, а не превращается в
    пустой шаг на стороне трекера.
    """
    parts = split_step_spec(spec)
    if len(parts) > len(STEP_PART_KEYS):
        raise RuntimeError(
            f"В шаге больше {len(STEP_PART_KEYS)} частей: {spec!r}. Формат — "
            f"'действие|данные|результат'; чтобы оставить '|' в тексте, экранируй его как '\\|'.")
    parts += [""] * (len(STEP_PART_KEYS) - len(parts))
    step = {key: value.strip() for key, value in zip(STEP_PART_KEYS, parts)}
    if not step["description"]:
        raise RuntimeError(f"Шаг без действия: {spec!r}. Первая часть строки обязательна.")
    return step


def _plain_text(source: Any) -> str:
    """Плоский текст части шага: то, что видно в списке кейсов и в поиске."""
    if isinstance(source, (dict, list)):
        return _plain_from_doc(source)
    text = str(source or "").strip()
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(parsed, dict) and parsed.get("type") == "doc":
            return _plain_from_doc(parsed)
    return text


def _plain_from_doc(node: Any) -> str:
    """Текст из документа ProseMirror — без разметки, блоки через перевод строки."""
    if isinstance(node, list):
        return "\n".join(filter(None, (_plain_from_doc(item) for item in node)))
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return str(node.get("text") or "")
    children = node.get("content")
    if not isinstance(children, list):
        return ""
    inline = "".join(_plain_from_doc(child) for child in children
                     if isinstance(child, dict) and child.get("type") == "text")
    blocks = [_plain_from_doc(child) for child in children
              if isinstance(child, dict) and child.get("type") != "text"]
    return "\n".join(filter(None, [inline, *blocks]))


def _empty_doc() -> Dict[str, Any]:
    """Пустая часть шага — документ с пустым параграфом, а не документ без узлов.

    Так пишет сам редактор, и это единственный ориентир: трекер схему не
    валидирует и принимает любой JSON, поэтому «прошло с 200» ничего не говорит
    о том, отрисуется ли шаг.
    """
    return {"type": "doc", "content": [{"type": "paragraph",
                                        "attrs": {"id": wiki_body.new_id(), "indent": 0,
                                                  "textAlign": wiki_body.TEXT_ALIGN}}]}


def _part(source: Any) -> Dict[str, str]:
    """Часть шага в форме записи: документ строкой плюс его плоский текст."""
    text = source if isinstance(source, (dict, list)) else str(source or "")
    document = _empty_doc() if not text else text
    return {"formattedText": wiki_body.body_value(document), "plainText": _plain_text(source)}


def _normalize_step(step: Any, number: int) -> Dict[str, str]:
    """Один шаг из любой поддерживаемой формы → части `description/data/result`.

    Принимается и простой словарь, и форма ЧТЕНИЯ трекера: последняя на записи
    теряется молча, поэтому она конвертируется, а не отвергается — иначе
    сценарий «прочитал кейс, создал похожий» тихо отдаёт кейс без шагов.
    """
    if isinstance(step, str):
        return parse_step_spec(step)
    if not isinstance(step, dict):
        raise RuntimeError(f"Шаг №{number} задан не объектом и не строкой: {type(step).__name__}.")
    if any(key in step for key in PART_ATTRIBUTE_KEYS.values()):
        parts = {key: _read_part(step.get(attribute))
                 for key, attribute in PART_ATTRIBUTE_KEYS.items()}
        source_key = PART_ATTRIBUTE_KEYS["description"]
    else:
        unknown = set(step) - set(STEP_PART_KEYS) - {"stepNumber"}
        if unknown:
            raise RuntimeError(
                f"Шаг №{number}: неизвестные ключи {sorted(unknown)}. Ожидаются "
                f"{list(STEP_PART_KEYS)} либо форма чтения трекера "
                f"(stepDescription/stepData/stepResult).")
        parts = {key: str(step.get(key) or "") for key in STEP_PART_KEYS}
        source_key = "description"
    # Проверка одна на обе формы: шаг без действия одинаково бессмыслен, откуда
    # бы он ни пришёл, и в форме чтения он появляется ровно тогда, когда часть
    # оказалась пустым документом.
    if not parts["description"].strip():
        raise RuntimeError(f"Шаг №{number} без действия: ключ {source_key} обязателен.")
    return parts


def _read_part(part: Any) -> str:
    """Значение части из формы чтения: строка документа лежит в `text`.

    Пустую часть трекер отдаёт не пустой строкой, а пустым документом — в живых
    кейсах это `{"text": "{}"}`. Без приведения такая часть перестаёт быть
    пустой и доезжает до нового кейса ВИДИМЫМ текстом «{}», потому что `{}` —
    валидный JSON, но не документ, и конвертер разбирает его как markdown.
    """
    if isinstance(part, dict):
        unknown = set(part) - {"text", "formattedText", "plainText"}
        if unknown and not (set(part) & {"text", "formattedText"}):
            raise RuntimeError(
                f"Часть шага задана ключами {sorted(unknown)}. Ожидается `text` (форма чтения) "
                f"или `formattedText`.")
        text = str(part.get("text") or part.get("formattedText") or "")
    else:
        text = str(part or "")
    return "" if _is_empty_document(text) else text


def _is_empty_document(text: str) -> bool:
    """Пустая часть в любой из форм, которыми её отдаёт трекер.

    Пустых форм ровно две: пустая строка и пустой объект `{}`, плюс документ без
    узлов. Всё остальное — текст, даже если это валидный JSON: в тестовых данных
    шага JSON-нагрузка (`{"login": "u"}`) — обычное дело для API-кейсов, и
    объявить её пустой значит молча стереть данные шага. Отсюда проверка именно
    на ДОКУМЕНТНОСТЬ (`type == "doc"`), как у `_plain_text` и
    `wiki_body.body_value`, а не на отсутствие ключа `content`.
    """
    stripped = text.strip()
    if not stripped:
        return True
    if not stripped.startswith("{"):
        return False
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    if not isinstance(parsed, dict):
        return False
    if not parsed:
        return True
    return parsed.get("type") == "doc" and not parsed.get("content")


def normalize_steps(source: Any) -> List[Dict[str, str]]:
    """Список шагов из списка словарей, строк или формы чтения трекера."""
    if source is None:
        return []
    if isinstance(source, dict):
        # Уже готовая обёртка: пришла из --steps-file, снятого с другого кейса.
        # Словарь без этого ключа — не обёртка, а одиночный шаг или опечатка, и
        # молчаливый пустой список здесь означал бы ровно тот отказ, против
        # которого написан модуль: атрибут удаляется, кейс создаётся без шагов,
        # код возврата 0. Опечатка в ключе ЭЛЕМЕНТА уже отвергается по имени —
        # опечатка в ключе ОБЁРТКИ обязана вести себя так же.
        if "testStepList" not in source:
            raise RuntimeError(
                f"Шаги заданы объектом без ключа `testStepList`: {sorted(source)[:5]}. "
                f"Одиночный шаг оборачивается в список: [{{\"description\": …}}]; обёртка "
                f"с другого кейса — {{\"testStepList\": [ … ]}}.")
        source = source["testStepList"]
    if isinstance(source, str):
        raise RuntimeError("Шаги заданы строкой. Ожидается список шагов: "
                           "JSON-массив в --steps-file либо повторяемый --step.")
    if not isinstance(source, Sequence):
        raise RuntimeError(f"Шаги заданы не списком: {type(source).__name__}.")
    return [_normalize_step(step, number) for number, step in enumerate(source, start=1)]


def steps_value(source: Any) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """Значение атрибута `test_step` в форме записи, либо `None` для пустого списка."""
    steps = normalize_steps(source)
    if not steps:
        return None
    return {"testStepList": [
        {
            "code": None,
            "deleted": False,
            "callToTestId": None,
            "stepType": STEP_TYPE,
            "stepNumber": number,
            "files": [],
            "stepFiles": [],
            **{attribute: _part(step[key]) for key, attribute in PART_ATTRIBUTE_KEYS.items()},
        }
        for number, step in enumerate(steps, start=1)
    ]}
