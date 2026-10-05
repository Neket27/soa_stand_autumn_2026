#!/usr/bin/env python3
"""Источники постановки: сама задача плюс связанные с ней страницы.

Печатает в stdout один JSON-объект. Файлов не создаёт: готовый пакет лежит в поле
`markdown`, записывает его агент своим инструментом.

Зачем отдельный инструмент. `fetch_swtr_sources.py` читает РОВНО ОДИН юнит —
его карточку и его же вики. Постановка почти никогда не самодостаточна: часть
требований живёт в связанных страницах вики и соседних задачах, и до сих пор их
приходилось перечислять руками, по вызову на ссылку. Обход связей нужен всем
потребителям одинаковый, поэтому он сделан один раз здесь, а не пересказывается
инструкцией в каждом навыке, который строит спецификацию.

Связанным считается то, что названо явно: связь трекера (`link find`) и код
юнита, упомянутый в тексте постановки. Догадок по смыслу здесь нет — «похожие»
задачи ищет `swtr_task_tool.py similar`, и смешивать эти два списка нельзя:
похожая задача не является источником требований.

Главное правило то же, что у `openspec_seed.py`: ничего не выдумывать.
Непрочитанное не исчезает из вывода — оно попадает в `skipped` или `external`
со своей причиной и командой, которой его дочитать.

Поле `source` — корневой юнит в том же виде, что печатает `fetch_swtr_sources.py`,
поэтому вывод годится как `--source-file` для `openspec_seed.py`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _swtr_common import (  # noqa: E402
    UNIT_CODE_RE,
    URL_RE,
    base_url_for,
    build_headers,
    build_ssl_context,
    emit_error,
    extract_unit_code,
    is_auth_error,
    load_env_file,
    redact_secrets,
    safe_headers_for_log,
    unit_url,
    value_to_text,
)
import fetch_swtr_sources as fetch  # noqa: E402
import swtr_task_tool as tracker  # noqa: E402
import swtr_unit_comments as comments_tool  # noqa: E402

COMMAND = "spec-sources"

# Сколько связанных юнитов дочитывается по умолчанию. Предел нужен не ради
# скорости: у эпика связей бывают десятки, и пак, куда попало всё, перестаёт
# быть контекстом — его никто не дочитает до конца. Что не влезло, перечислено
# в `links` и названо в `notes`, поэтому предел виден, а не молча срезает.
DEFAULT_MAX_LINKED = 10
DEFAULT_LINK_PAGE_SIZE = 50
# Длина текста одного источника в markdown-паке. Полный текст остаётся в JSON.
DEFAULT_MAX_CHARS = 4000
# Сколько кодоподобных строк из текста проверять на существование за один раз.
# Предел нужен телу запроса: проверка идёт OR-фильтром по кодам, и список на
# тысячу элементов — это уже не проверка, а другой запрос.
MAX_MENTION_CANDIDATES = 100

SBERWORKS_HOST_MARKER = "sberworks"

# Направления, в которых упоминание кода в тексте не является ссылкой на источник.
# Регистр приведён к верхнему до сравнения.
MENTION_STOP_PREFIXES = ("REQ-",)


# --- разбор источника --------------------------------------------------------

def source_texts(source: Dict[str, Any]) -> List[str]:
    """Все тексты юнита, в которых имеет смысл искать ссылки и коды.

    Сырой `unit` сюда не входит: он повторяет те же значения атрибутов, и коды
    из него приезжали бы вторым экземпляром.
    """
    texts: List[str] = []
    for key in ("summary", "description"):
        value = source.get(key)
        if isinstance(value, str) and value:
            texts.append(value)
    for attribute in source.get("attributes") or []:
        if isinstance(attribute, dict):
            text = attribute.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
    for item in source.get("wikiTexts") or []:
        if isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
    # Ссылки живут в `attrs.href` документа редактора и в развёрнутый текст не
    # попадают вовсе. Сырые payload источник отдаёт целиком, поэтому адреса
    # берутся оттуда — иначе обход перестал бы видеть смежные страницы, как
    # только источник начал отдавать текст вместо разметки.
    for payload in (source.get("unit"), source.get("wiki")):
        texts.extend(comments_tool.collect_rich_hrefs(payload))
    return texts


def mentioned_codes(texts: Sequence[str], exclude: Sequence[str]) -> List[str]:
    """Коды юнитов, названные в тексте, в порядке первого появления."""
    skip = {str(code).upper() for code in exclude if code}
    found: List[str] = []
    for text in texts:
        for match in UNIT_CODE_RE.finditer(text.upper()):
            code = match.group(0)
            if code in skip or code in found:
                continue
            if code.startswith(MENTION_STOP_PREFIXES):
                continue
            found.append(code)
    return found


def external_ref(url: str, system: str, key: Optional[str] = None,
                 read_command: Optional[str] = None) -> Dict[str, Any]:
    """Запись о непрочитанной внешней ссылке — одной формы для всех систем.

    `readCommand`, а не `hint`: в семействе `hint` — это фраза «что поправить»
    (`profile_error_context`), а здесь лежит готовая команда, которую можно
    выполнить. Ключи одинаковы у всех записей, включая пустые: список, где у
    части записей полей нет, потребитель разбирает условиями вместо чтения.
    """
    return {"url": url, "system": system, "key": key, "readCommand": read_command}


def system_match(parsed: urllib.parse.ParseResult, system: str) -> bool:
    """Ссылка ведёт в настроенный адрес системы (`confluence` / `jira`).

    Сравнивается хост И начало пути: на sberworks обе системы живут на ОДНОМ
    хосте и различаются только префиксом (`/wiki` против `/jira`), поэтому
    сравнение по одному хосту относило бы каждую вики-страницу к Jira. Адреса
    берутся из `base_url_for`, а не зашиты: `CONFLUENCE_BASE_URL`/`JIRA_BASE_URL`
    переопределяются, и на другом контуре зашитая подстрока не значит ничего.
    """
    base = urllib.parse.urlparse(base_url_for(system))
    if (parsed.netloc or "").lower() != (base.netloc or "").lower():
        return False
    base_path = (base.path or "").rstrip("/")
    return not base_path or (parsed.path or "").startswith(base_path)


def is_sberworks(host: str) -> bool:
    """Запасное правило для стандартной установки.

    Основной признак — совпадение с настроенным адресом системы
    (`base_url_for`), потому что `CONFLUENCE_BASE_URL`/`JIRA_BASE_URL`
    переопределяются, и на другом контуре подстрока «sberworks» ничего не
    значит. Но и обратное верно: адрес переопределён, а ссылка в постановке
    ведёт на sberworks — тогда работает эта подстрока.
    """
    return SBERWORKS_HOST_MARKER in host


def classify_url(url: str) -> Dict[str, Any]:
    """Внешняя ссылка: к какой системе относится и чем её дочитать.

    Гейт sberworks здесь не обходится и не отменяется: Confluence и Jira этот
    скрипт не читает никогда, он только показывает готовую команду. Решение
    пойти на sberworks остаётся явным действием человека — ровно как требует
    раздел «Приоритет систем» в SKILL.md.
    """
    parsed = urllib.parse.urlparse(url)
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    looks_like_jira = "/browse/" in path or path.startswith("/jira")
    if system_match(parsed, "jira") or (is_sberworks(host) and looks_like_jira):
        key = extract_unit_code(path)
        return external_ref(url, "jira", key=key,
                            read_command="python3 $SKILL_DIR/scripts/jira_tool.py get %s "
                                         "--confirm-sberworks" % (key or "<KEY>"))
    if system_match(parsed, "confluence") or is_sberworks(host):
        return external_ref(url, "confluence",
                            read_command="python3 $SKILL_DIR/scripts/confluence_tool.py get '%s' "
                                         "--confirm-sberworks" % url)
    return external_ref(url, "other")


def split_urls(texts: Sequence[str], base_url: str, exclude: Sequence[str]) -> Tuple[List[str], List[Dict[str, Any]]]:
    """``(коды юнитов из ссылок на трекер, внешние ссылки)``.

    Ссылка на карточку юнита — это тот же юнит, что и голый код, поэтому она не
    внешняя: она превращается в код и дочитывается штатно.
    """
    skip = {str(code).upper() for code in exclude if code}
    host = (urllib.parse.urlparse(base_url).netloc or "").lower()
    codes: List[str] = []
    external: List[Dict[str, Any]] = []
    seen_urls = set()
    for text in texts:
        for match in URL_RE.finditer(text):
            url = match.group(0).rstrip(".,;")
            if url in seen_urls:
                continue
            seen_urls.add(url)
            parsed = urllib.parse.urlparse(url)
            if host and (parsed.netloc or "").lower() == host:
                code = extract_unit_code(url)
                if code:
                    if code not in skip and code not in codes:
                        codes.append(code)
                    continue
            external.append(classify_url(url))
    return codes, external


# --- связи трекера -----------------------------------------------------------

def fetch_tracker_links(code: str, base_url: str, timeout: float, page_size: int,
                        types: Optional[Sequence[str]]) -> Tuple[List[Dict[str, Any]], List[str], bool]:
    """``(связи, заметки, прочитано)``. Отказ связей не роняет чтение задачи.

    Постановка прочитана и полезна сама по себе; превратить её в ошибку из-за
    недоступного справочника связей значит потерять то, что уже получено.

    Отказ авторизации — исключение из этого правила и пробрасывается наверх:
    протухший токен кладёт все каналы разом, и «связей нет» тогда означает не
    факт о задаче, а факт о токене. Правило общее для семейства
    (`_swtr_common.is_auth_error`), а не местное решение.

    Третьим значением возвращается признак «канал прочитан»: без него пустой
    список неотличим от несостоявшегося чтения, и пакет заявлял бы, что связей
    у задачи нет, хотя он их не спрашивал.
    """
    try:
        _, data = tracker.find_links(base_url, code, timeout, types=types, size=page_size)
    except Exception as exc:  # noqa: BLE001 - см. docstring: деградируем, кроме авторизации
        if is_auth_error(exc):
            raise
        return [], ["связи %s не прочитаны: %s" % (code, redact_secrets(str(exc)))], False
    rows = data.get("content") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return [], [], True
    notes = truncation_note(data, len(rows), "связей у %s" % code, "--link-page-size")
    alive = [row for row in rows if isinstance(row, dict) and not row.get("deleted")]
    resolved, enrich_notes = tracker.enrich_links(alive, code, base_url, timeout)
    return resolved, notes + enrich_notes, True


def truncation_note(payload: Any, got: int, subject: str, flag: str) -> List[str]:
    """Заметка о том, что страница ответа не последняя.

    Читается одна страница, и это осознанно: пагинация ради эпика с сотнями
    связей раздувает и число запросов, и сам пакет. Но срезать молча нельзя —
    инструмент обещает, что непрочитанное остаётся видимым, а `hasNext` из
    ответа как раз и есть та единственная запись, которой это обещание держится.
    """
    if not isinstance(payload, dict):
        return []
    total = payload.get("totalElements")
    truncated = bool(payload.get("hasNext")) or (isinstance(total, int) and total > got)
    if not truncated:
        return []
    return ["%s больше, чем прочитано: взято %d из %s — поднимите %s"
            % (subject, got, total if isinstance(total, int) else "неизвестного числа", flag)]


def fetch_comments(code: str, base_url: str, timeout: float) -> Tuple[List[Dict[str, Any]], List[str]]:
    """``(комментарии, заметки)``. Отказ канала виден, но не роняет чтение.

    Комментарии здесь — не переписка, а канал постановки: уточнение требования и
    ссылка на страницу с ним чаще дописываются комментарием, чем правкой
    описания. Ровно поэтому `guru-crpv-decomposer` считает их отдельным каналом
    контекста, и обход, который их не видит, теряет часть источников.

    Отказ авторизации пробрасывается по той же причине, что и у связей.
    """
    body = comments_tool.make_request_body(code, None, None)
    try:
        response = tracker.post_json(base_url, comments_tool.COMMENTS_ENDPOINT, body, timeout)
    except Exception as exc:  # noqa: BLE001 - канал, а не сама задача
        if is_auth_error(exc):
            raise
        return [], ["комментарии %s не прочитаны: %s" % (code, redact_secrets(str(exc)))]
    normalized = comments_tool.normalize_response(response)
    rows = [row for row in (normalized.get("content") or [])
            if isinstance(row, dict) and not row.get("deleted")]
    notes = truncation_note(response, len(normalized.get("content") or []),
                            "комментариев у %s" % code, "--with-comments с постраничным чтением")
    return rows, notes


def comment_texts(rows: Sequence[Dict[str, Any]]) -> List[str]:
    return [row.get("text") or "" for row in rows if (row.get("text") or "").strip()]


def existing_codes(codes: Sequence[str], base_url: str, timeout: float) -> Tuple[List[str], List[str]]:
    """``(коды, которые в трекере есть, заметки)`` — ОДНИМ запросом на весь список.

    `UNIT_CODE_RE` совпадает с любым `ABC-123`, и в тексте постановки под эту
    форму попадают `ISO-8601`, `UTF-8`, `HTTP-404`, `Q1-2026`. Без этой проверки
    каждый такой токен становился отдельным чтением с ретраями и строкой в
    разделе «Не прочитано»: на одном описании — сотни запросов и мусор вместо
    источников. `fetch_unit_summaries` отвечает на вопрос «какие из этих кодов
    существуют» одним `find`, поэтому цена проверки не зависит от их числа.

    Проверяются только кандидаты из текста. Коды из связей трекера пришли от
    самого трекера и существуют по построению — лишний запрос по ним не нужен.
    """
    wanted = [code for code in dict.fromkeys(codes) if code]
    if not wanted:
        return [], []
    notes: List[str] = []
    if len(wanted) > MAX_MENTION_CANDIDATES:
        notes.append("кодоподобных строк в тексте слишком много (%d): проверены первые %d"
                     % (len(wanted), MAX_MENTION_CANDIDATES))
        wanted = wanted[:MAX_MENTION_CANDIDATES]
    try:
        summaries = tracker.fetch_unit_summaries(base_url, wanted, timeout)
    except Exception as exc:  # noqa: BLE001 - см. fetch_tracker_links
        if is_auth_error(exc):
            raise
        # Проверка не состоялась — идём по кандидатам как раньше. Это хуже по
        # числу запросов, но лучше, чем молча выбросить настоящие источники.
        return list(wanted), ["существование упомянутых кодов не проверено: %s"
                              % redact_secrets(str(exc))]
    return [code for code in wanted if code in summaries], notes


def link_targets(links: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Связи → кандидаты на дочитывание, по одному на код.

    Отсев уже виденного здесь не делается: он всё равно повторяется в
    `merge_targets` и в самом цикле обхода, а три места с одним и тем же
    вопросом расходятся при первой же правке.
    """
    targets: List[Dict[str, Any]] = []
    seen = set()
    for link in links:
        code = link.get("unit")
        if not code or code in seen:
            continue
        seen.add(code)
        targets.append({
            "code": code,
            "origin": "link",
            # `resolve_link` уже гарантирует непустое `relation` с откатом на код
            # типа связи, поэтому цепочки запасных значений тут не нужно.
            "relation": link.get("relation"),
            "summary": link.get("unitSummary") or "",
        })
    return targets


def merge_targets(from_links: Sequence[Dict[str, Any]], from_text: Sequence[str],
                  exclude: Sequence[str]) -> List[Dict[str, Any]]:
    """Связи идут первыми: связь названа автором явно, упоминание — лишь текст."""
    skip = {str(code).upper() for code in exclude if code}
    merged: List[Dict[str, Any]] = []
    seen = set()
    for target in from_links:
        code = target.get("code")
        if not code or code in skip or code in seen:
            continue
        seen.add(code)
        merged.append(dict(target))
    for code in from_text:
        if not code or code in skip or code in seen:
            continue
        seen.add(code)
        merged.append({"code": code, "origin": "mention", "relation": "упомянута в тексте", "summary": ""})
    return merged


# --- сборка ------------------------------------------------------------------

def collect(args: argparse.Namespace, headers: Dict[str, str]) -> Dict[str, Any]:
    base_url = args.base_url.rstrip("/")
    include_wiki = not args.skip_wiki
    root_input_type, root_code, _ = fetch.detect_input(args.input)
    if root_input_type != "swtr_unit" or not root_code:
        raise ValueError("spec_sources работает только с задачей или страницей трекера: "
                         "нужен код юнита или ссылка на него.")

    root_source = fetch.collect_swtr_source(
        input_value=args.input,
        code=root_code,
        base_url=base_url,
        headers=headers,
        timeout=args.timeout,
        include_wiki=include_wiki,
    )

    notes: List[str] = []
    links: List[Dict[str, Any]] = []
    linked: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    external: List[Dict[str, Any]] = []
    seen_urls = set()
    links_unread = False

    visited = {root_code}
    skipped_codes = set()
    attempts = 0
    # Очередь обхода: (кандидат, глубина). Глубина 1 — то, на что ссылается сама
    # задача; глубина 2 — то, на что ссылаются уже её страницы.
    queue: List[Tuple[Dict[str, Any], int]] = []

    def scan(source: Dict[str, Any], depth: int, extra_texts: Sequence[str] = ()) -> None:
        """Разобрать тексты прочитанного источника: внешние ссылки и кандидаты.

        Внешние ссылки собираются с КАЖДОГО прочитанного источника, а не только
        с корня. Ссылка на Confluence чаще стоит в связанной странице, чем в
        самой задаче, а на глубине обхода по умолчанию дальше корня уже не идут —
        и раньше такие ссылки не попадали никуда, хотя именно про них Шаг 0
        велит спросить пользователя.
        """
        nonlocal links_unread
        texts = source_texts(source) + list(extra_texts)
        url_codes, url_external = split_urls(texts, base_url, visited)
        for item in url_external:
            if item["url"] not in seen_urls:
                seen_urls.add(item["url"])
                external.append(item)
        if depth >= args.depth:
            return
        from_links: List[Dict[str, Any]] = []
        if args.follow_links:
            found, link_notes, ok = fetch_tracker_links(
                source.get("code") or "", base_url, args.timeout, args.link_page_size, args.link_type)
            notes.extend(note for note in link_notes if note not in notes)
            links.extend(found)
            links_unread = links_unread or not ok
            from_links = link_targets(found)
        text_codes: List[str] = []
        if args.follow_mentions:
            # Ссылка на карточку юнита — то же упоминание в тексте, что и голый
            # код, поэтому гасится тем же флагом: иначе `--no-mentions` выключал
            # бы только половину, а `request.followMentions: false` в выводе
            # утверждал бы обратное.
            text_codes = mentioned_codes(texts, visited)
            for code in url_codes:
                if code not in text_codes:
                    text_codes.append(code)
            text_codes, probe_notes = existing_codes(text_codes, base_url, args.timeout)
            notes.extend(note for note in probe_notes if note not in notes)
        for target in merge_targets(from_links, text_codes, visited):
            queue.append((target, depth + 1))

    root_comments: List[Dict[str, Any]] = []
    if args.with_comments:
        root_comments, comment_notes = fetch_comments(root_code, base_url, args.timeout)
        notes.extend(comment_notes)
    scan(root_source, 0, comment_texts(root_comments))

    while queue:
        target, depth = queue.pop(0)
        code = target["code"]
        if code in visited or code in skipped_codes:
            continue
        # Предел считает ПОПЫТКИ, а не удачи: иначе недоступный юнит бюджета не
        # тратил, и текст с кодоподобными строками уводил обход в сотни запросов
        # при `--max-linked 10`.
        if attempts >= args.max_linked:
            skipped_codes.add(code)
            skipped.append({"code": code, "origin": target.get("origin"), "kind": "limit",
                            "reason": "предел --max-linked=%d" % args.max_linked,
                            "url": unit_url(base_url, code)})
            continue
        visited.add(code)
        attempts += 1
        try:
            child = fetch.collect_swtr_source(
                input_value=code,
                code=code,
                base_url=base_url,
                headers=headers,
                timeout=args.timeout,
                include_wiki=include_wiki,
            )
        except Exception as exc:  # noqa: BLE001 - см. fetch_tracker_links
            if is_auth_error(exc):
                raise
            skipped_codes.add(code)
            skipped.append({"code": code, "origin": target.get("origin"), "kind": "error",
                            "reason": redact_secrets(str(exc)),
                            "url": unit_url(base_url, code)})
            continue
        child_comments: List[Dict[str, Any]] = []
        if args.with_comments:
            child_comments, comment_notes = fetch_comments(code, base_url, args.timeout)
            notes.extend(note for note in comment_notes if note not in notes)
        linked.append({
            "code": code,
            "origin": target.get("origin"),
            "relation": target.get("relation"),
            "summary": child.get("summary") or target.get("summary") or "",
            "url": child.get("url"),
            "suit": child.get("suit"),
            "space": child.get("space"),
            "source": child,
            "comments": child_comments,
        })
        scan(child, depth, comment_texts(child_comments))

    over_limit = [row for row in skipped if row.get("kind") == "limit"]
    if over_limit:
        notes.append("связанных страниц больше, чем предел чтения: не прочитано %d, "
                     "поднимите --max-linked при необходимости" % len(over_limit))
    if external:
        notes.append("внешних ссылок: %d — они не прочитаны, команды для чтения указаны в `external`"
                     % len(external))

    return {
        "root": {
            "code": root_code,
            "summary": root_source.get("summary") or "",
            "url": root_source.get("url"),
            "space": root_source.get("space"),
            "suit": root_source.get("suit"),
            "fetchedAt": root_source.get("fetchedAt"),
        },
        "source": root_source,
        "comments": root_comments,
        "links": links,
        "linksUnread": links_unread,
        "linked": linked,
        "external": external,
        "skipped": skipped,
        "notes": notes,
    }


# --- markdown ----------------------------------------------------------------

def clip(text: str, limit: int) -> Tuple[str, bool]:
    if limit <= 0 or len(text) <= limit:
        return text, False
    return text[:limit].rstrip(), True


def render_attributes(source: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    for attribute in source.get("attributes") or []:
        if not isinstance(attribute, dict):
            continue
        text = (attribute.get("text") or "").strip()
        if not text:
            continue
        name = attribute.get("name") or attribute.get("code")
        if "\n" in text:
            # Многострочный атрибут (богатый текст) в одну строку списка не лезет:
            # продолжение с отступом сохраняет и абзацы, и сам список.
            lines.append("- **%s**:" % name)
            lines.extend("  " + line for line in text.splitlines())
        else:
            lines.append("- **%s**: %s" % (name, text))
    return lines


def render_body(source: Dict[str, Any], max_chars: int, where: str = "source") -> List[str]:
    """Описание и тело вики одного юнита. Пустое называется пустым.

    `where` — путь к этому юниту в JSON: у корня `source`, у связанной страницы
    `linked[N].source`. Он попадает в пометку об усечении, и без него она
    отправляла читателя за полным текстом связанной страницы в поле корня.
    """
    lines: List[str] = []
    description = (source.get("description") or "").strip()
    if description:
        text, clipped = clip(description, max_chars)
        lines.append(text)
        if clipped:
            lines.append("_…срезано, полный текст — в JSON-поле `%s.description`._" % where)
    else:
        lines.append("_Описание пустое: текста в карточке нет._")
    # Плагин вики эхом отдаёт карточку самого юнита, и её summary/description уже
    # напечатаны выше: без отсева описание попадает в пакет дважды. Опознаётся
    # эхо по пути, а не сравнением текстов, — тем же предикатом, которым его
    # отсеивает сам сборщик источника (`fetch_swtr_sources.is_unit_card_path`).
    wiki_texts = []
    for item in source.get("wikiTexts") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        if fetch.is_unit_card_path(path, path.split(".", 1)[0]):
            continue
        text = (item.get("text") or "").strip()
        if text and text not in wiki_texts:
            wiki_texts.append(text)
    if wiki_texts:
        lines.append("")
        lines.append("**Тело вики:**")
        text, clipped = clip("\n\n".join(wiki_texts), max_chars)
        lines.append("")
        lines.append(text)
        if clipped:
            lines.append("")
            lines.append("_…срезано, полный текст — в JSON-поле `%s.wikiTexts`._" % where)
    return lines


def render_comments(rows: Sequence[Dict[str, Any]], max_chars: int) -> List[str]:
    if not rows:
        return []
    printable = [row for row in rows if (row.get("text") or "").strip()]
    if not printable:
        return []
    lines = ["", "**Комментарии (%d):**" % len(printable), ""]
    for row in printable:
        text, clipped = clip((row.get("text") or "").strip(), max_chars)
        stamp = row.get("createdAt") or ""
        lines.append("- %s%s: %s%s" % (row.get("createdBy") or "не указан",
                                       (", " + stamp) if stamp else "", text,
                                       " _…срезано_" if clipped else ""))
    return lines


def render_markdown(payload: Dict[str, Any], max_chars: int) -> str:
    root = payload["root"]
    source = payload["source"]
    lines: List[str] = []
    title = root.get("summary") or root["code"]
    lines.append("# Источники постановки: %s — %s" % (root["code"], title))
    lines.append("")
    lines.append("- Источник: %s" % (root.get("url") or "—"))
    lines.append("- Прочитано: %s" % (root.get("fetchedAt") or "—"))
    # `space` и `suit` приезжают из трекера словарями (`{"code": …, "name": …}`),
    # а не строками: без развёртки в заголовке пакета печатался сырой dict.
    lines.append("- Пространство / тип: %s / %s" % (value_to_text(root.get("space")) or "—",
                                                    value_to_text(root.get("suit")) or "—"))
    lines.append("")
    lines.append("## Постановка")
    lines.append("")
    lines.extend(render_body(source, max_chars))
    attributes = render_attributes(source)
    if attributes:
        lines.append("")
        lines.append("**Атрибуты:**")
        lines.append("")
        lines.extend(attributes)
    lines.extend(render_comments(payload.get("comments") or [], max_chars))

    linked = payload.get("linked") or []
    lines.append("")
    lines.append("## Связанные материалы (%d)" % len(linked))
    lines.append("")
    if not linked:
        # «Не найдено» и «не прочитано» — разные факты, и раньше пакет печатал
        # первый вместо второго: при отказе `link find` или при срезе пределом он
        # заявлял, что связей у задачи нет, хотя он их не спрашивал.
        if payload.get("linksUnread") or payload.get("skipped"):
            lines.append("_Связанных страниц в пакете нет, но это НЕ значит, что их нет у задачи: "
                         "часть источников не прочитана — см. «Не прочитано» и «Заметки сборки»._")
        else:
            lines.append("_Связанных страниц не найдено: у задачи нет связей трекера, "
                         "и в тексте не упомянут ни один код юнита._")
    for index, item in enumerate(linked):
        lines.append("### %s — %s" % (item["code"], item.get("summary") or "без темы"))
        lines.append("")
        # У упоминания «связь» и «откуда узнали» — одно и то же, и печатать это
        # дважды («упомянута в тексте (упоминание в тексте)») незачем.
        if item.get("origin") == "link":
            lines.append("- Связь: %s (связь трекера)" % (item.get("relation") or "—"))
        else:
            lines.append("- Связь: %s" % (item.get("relation") or "упомянута в тексте"))
        lines.append("- Ссылка: %s" % (item.get("url") or "—"))
        lines.append("")
        lines.extend(render_body(item.get("source") or {}, max_chars,
                                 where="linked[%d].source" % index))
        lines.extend(render_comments(item.get("comments") or [], max_chars))
        lines.append("")

    external = payload.get("external") or []
    if external:
        lines.append("## Внешние ссылки — НЕ прочитаны (%d)" % len(external))
        lines.append("")
        for ref in external:
            if ref.get("readCommand"):
                lines.append("- %s — %s; дочитать: `%s`"
                             % (ref["url"], ref.get("system"), ref["readCommand"]))
            else:
                lines.append("- %s — %s" % (ref["url"], ref.get("system")))
        lines.append("")

    skipped = payload.get("skipped") or []
    if skipped:
        lines.append("## Не прочитано (%d)" % len(skipped))
        lines.append("")
        for row in skipped:
            lines.append("- %s — %s" % (row.get("code"), row.get("reason")))
        lines.append("")

    notes = payload.get("notes") or []
    if notes:
        lines.append("## Заметки сборки")
        lines.append("")
        for note in notes:
            lines.append("- %s" % note)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# --- CLI ---------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Источники постановки: задача трекера плюс связанные с ней страницы. "
                    "Печатает один JSON-объект в stdout; файлов не создаёт.")
    parser.add_argument("input", help="Код юнита SWTR или ссылка на него")
    parser.add_argument("--depth", type=int, default=1,
                        help="Глубина обхода связей: 1 — только связи самой задачи (по умолчанию)")
    parser.add_argument("--max-linked", type=int, default=DEFAULT_MAX_LINKED,
                        help="Сколько связанных юнитов читать; неудачная попытка тоже тратит предел "
                             "(по умолчанию %d)" % DEFAULT_MAX_LINKED)
    parser.add_argument("--link-type", action="append",
                        help="Ограничить связи типами (повторяемый; коды из `link types`)")
    parser.add_argument("--link-page-size", type=int, default=DEFAULT_LINK_PAGE_SIZE,
                        help="Размер страницы `link find` (по умолчанию %d)" % DEFAULT_LINK_PAGE_SIZE)
    parser.add_argument("--no-links", dest="follow_links", action="store_false",
                        help="Не читать связи трекера")
    parser.add_argument("--no-mentions", dest="follow_mentions", action="store_false",
                        help="Не идти по кодам юнитов, упомянутым в тексте, включая ссылки на их карточки")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS,
                        help="Предел текста одного источника в markdown (по умолчанию %d, "
                             "0 — без предела; полный текст всегда остаётся в JSON)" % DEFAULT_MAX_CHARS)
    parser.add_argument("--with-comments", action="store_true",
                        help="Читать комментарии юнитов: уточнения постановки и ссылки чаще "
                             "дописывают комментарием, чем правкой описания")
    parser.add_argument("--skip-wiki", action="store_true", help="Не ходить в wiki-эндпоинты")
    parser.add_argument("--base-url", help="Переопределить SWTR_BASE_URL")
    parser.add_argument("--env-file", default="~/.swtr-review.env")
    parser.add_argument("--timeout", type=float, default=30.0, help="Таймаут HTTP в секундах")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    load_env_file(Path(args.env_file).expanduser())
    if getattr(args, "base_url", None) in (None, ""):
        args.base_url = base_url_for("task_tracker")
    if args.depth < 1:
        args.depth = 1
    if args.max_linked < 0:
        args.max_linked = 0

    try:
        headers = build_headers("task_tracker", require_auth=True)
        payload = collect(args, headers)
    except (RuntimeError, OSError, ValueError, SystemExit) as exc:
        # SystemExit сюда приходит из `fetch.detect_input`: без этой ветки отказ
        # по нечитаемому входу печатался голой строкой мимо контракта вывода.
        return emit_error("spec-sources", exc, input=args.input)

    payload["ok"] = True
    payload["command"] = COMMAND
    payload["request"] = {
        "input": args.input,
        "code": payload["root"]["code"],
        "baseUrl": args.base_url.rstrip("/"),
        "headers": safe_headers_for_log(headers),
        "depth": args.depth,
        "maxLinked": args.max_linked,
        "followLinks": args.follow_links,
        "followMentions": args.follow_mentions,
        "linkTypes": list(args.link_type) if args.link_type else None,
        "linkPageSize": args.link_page_size,
        "maxChars": args.max_chars,
        "timeout": args.timeout,
        "includeWiki": not args.skip_wiki,
        "withComments": args.with_comments,
        "skipTlsVerify": build_ssl_context("task_tracker") is not None,
    }
    payload["markdown"] = render_markdown(payload, args.max_chars)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
