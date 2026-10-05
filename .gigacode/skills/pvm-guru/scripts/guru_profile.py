#!/usr/bin/env python3
"""Show, change and reset the user profile of the pvm-guru skill.

The profile (``~/.config/pvm-guru/profile.json``) holds the defaults the other
CLIs substitute where a value is otherwise required by a flag: tracker login,
spaces, default suit, product and components, SourceControl tenant/project/
repos, sberworks Jira project and Confluence space. Everything in it is plain
text the user may see; secrets live only in ``~/.swtr-review.env`` and a key
whose name looks like a secret is refused on write and dropped on rewrite. The
table of keys is closed: a name that is not in it is refused on write too, and
one that is already in the file is kept there but never applied.

This script is the only writer, just as guru_env_setup.py is the only writer of
the env file: the agent never edits the JSON by hand, so normalization and
validation exist in exactly one place. Reading for every other CLI lives in
``_swtr_common.load_profile``. ``show``/``set``/``unset``/``reset`` never open a
socket; ``suggest`` is the one read-only network command: it builds the answer
options of the onboarding step from the user's own tracker data (REQ-007).

Usage:
    python3 guru_profile.py show [--home DIR]
    python3 guru_profile.py set KEY=VALUE [KEY=VALUE ...] [--home DIR]   (lists: a,b,c; empty value removes the key)
    python3 guru_profile.py unset KEY [KEY ...] [--home DIR]
    python3 guru_profile.py reset [--home DIR]
    python3 guru_profile.py suggest [--env-file FILE] [--timeout SEC]

``--home`` belongs to the four file commands and ``--env-file``/``--timeout`` to
``suggest``: neither half has anything to do with the other.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _swtr_common import (  # noqa: E402
    PROFILE_MANAGED_KEYS as MANAGED_KEYS,  # noqa: N811 - the family stamps these keys itself
    PROFILE_SCHEMA_VERSION,
    LoadedProfile,
    base_url_for,
    build_authorization,
    emit_error,
    load_env_file,
    load_profile,
    normalize_profile,
    normalize_text,
    profile_delete_path,
    profile_get_path,
    profile_path,
    profile_schema,
    profile_set_path,
    profile_split_key,
    redact_secrets,
    validate_profile,
)

ENV_FILE_DEFAULT = "~/.swtr-review.env"

# ``suggest`` (REQ-007): the sample is the user's assigned units of any status,
# one page of at most SUGGEST_SAMPLE_LIMIT (the tracker's page, not the default
# 50 of ``assigned``). A space is suggested from one unit on; a default suit only
# when the sample has at least SUGGEST_SUIT_MIN_TASKS units and one suit holds at
# least SUGGEST_SUIT_MIN_SHARE of them (numerator, denominator -- compared in
# integers, so exactly two thirds qualifies).
SUGGEST_SAMPLE_LIMIT = 200
SUGGEST_SUIT_MIN_TASKS = 10
SUGGEST_SUIT_MIN_SHARE = (2, 3)
SUGGEST_BASIS_MAX_SPACES = 5
SPACES_ENDPOINT = "/rest/api/space/v3/find"

# A key whose name CONTAINS one of these is a secret by name, whatever its value
# -- the wording of REQ-003, of README.md, of SKILL.md and of
# references/usage-scenarios.md, word for word. Agreed with
# collect_secret_values(): every env name it reads carries TOKEN, AUTH,
# AUTHORIZATION or COOKIE; PASSWORD and SECRET cover what a user might invent.
SECRET_LIKE_MARKERS = ("TOKEN", "PASSWORD", "AUTH", "AUTHORIZATION", "COOKIE", "SECRET")

# Substring matching alone once called ``author`` and ``notes_for_author``
# secrets: ``show`` hid them and the first ``set`` deleted them with the note
# about the credential file -- a loss of the unknown keys REQ-002 promises to
# keep on rewrite, explained by something that was not true. Matching by whole
# word instead traded that for the opposite hole: ``tokens``, ``authtoken`` and
# ``my_secrets`` are hand-typed credential names and every one of them went
# through into ``show`` and into the file.
#
# So the marker counts as a substring, and the false positives are closed by
# name: an occurrence of a marker that lies INSIDE an occurrence of one of the
# innocent stems below is not a secret. Containment, not deletion, is what makes
# the rule safe both ways -- ``AUTHORIZATION`` in ``authorization_scheme`` is
# longer than the ``AUTHOR`` it starts with, so it still counts, while the
# ``AUTH`` of ``author`` does not. A stem covers its whole family (``AUTHOR``
# also covers ``authors``/``authored``/``co_author``, ``TOKENIZ`` also covers
# ``tokenizer``/``tokenize``/``tokenization``).
SECRET_WORD_EXCEPTIONS = ("AUTHOR", "TOKENIZ")


def _spans(text: str, needles: Sequence[str]) -> List[Tuple[int, int]]:
    """Every occurrence of every needle in ``text`` as a ``(start, end)`` pair."""
    found: List[Tuple[int, int]] = []
    for needle in needles:
        start = text.find(needle)
        while start != -1:
            found.append((start, start + len(needle)))
            start = text.find(needle, start + 1)
    return found


def is_secret_like(key: str) -> bool:
    """True for a key whose name says "secret" (dotted keys included).

    A marker found inside an innocent stem (``author``, ``tokenizer``) does not
    count; anything else does, wherever in the name it sits.
    """
    name = key.upper()
    innocent = _spans(name, SECRET_WORD_EXCEPTIONS)
    for start, end in _spans(name, SECRET_LIKE_MARKERS):
        if not any(low <= start and end <= high for low, high in innocent):
            return True
    return False


def strip_secret_like_keys(data: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """``(copy without secret-like keys, dotted names of what was removed)``.

    Walks dicts AND lists, so a token hidden inside a section, inside an unknown
    nested object or inside an element of a hand-typed list is found too -- the
    same full walk unit_urls_in() does over a tracker answer. A list that holds
    one is kept with its element cleaned: the value has to go, the shape around
    it is not ours to rewrite. The profile is not the place for a credential
    whatever the key is called; the caller reports the removals. An element of a
    list is named by index (``misc[0].api_token``), because a plain dotted name
    would not say which one.
    """
    removed: List[str] = []

    def walk(node: Any, path: str) -> Any:
        if isinstance(node, dict):
            clean: Dict[str, Any] = {}
            for name, value in node.items():
                dotted = "%s.%s" % (path, name) if path else name
                if is_secret_like(name):
                    removed.append(dotted)
                    continue
                clean[name] = walk(value, dotted)
            return clean
        if isinstance(node, list):
            return [walk(item, "%s[%d]" % (path, index)) for index, item in enumerate(node)]
        return copy.deepcopy(node)

    return walk(data, ""), removed


def merge_profile(current: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Partial update: ``patch`` keys win, everything else is kept.

    ``patch`` maps dotted keys to values; ``None``, ``""`` and ``[]`` remove the
    key (an emptied section disappears with it). Returns a new dict.
    """
    merged = copy.deepcopy(current)
    for key, value in patch.items():
        if value is None or value == "" or value == []:
            profile_delete_path(merged, key)
        else:
            profile_set_path(merged, key, value)
    return merged


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save_profile(home: Optional[Path], data: Dict[str, Any]) -> Tuple[Path, Dict[str, Any]]:
    """Write the profile whole and return ``(path, written payload)``.

    ``version`` and ``updated_at`` go first, then the data.

    Atomic like guru_env_setup.write_env_file: a temp file in the same directory
    (mode 0600 from mkstemp) and os.replace, so a crash never leaves a truncated
    JSON and two hosts writing at once end with one of the two files intact, not
    a mix. The directory is created on first save. No ``.bak``: the file holds
    no secrets and the onboarding can rebuild it.
    """
    path = profile_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: Dict[str, Any] = {"version": PROFILE_SCHEMA_VERSION, "updated_at": utc_now_iso()}
    for key, value in data.items():
        if key not in MANAGED_KEYS:
            payload[key] = value
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    tmp_fd = None
    tmp_path: Optional[Path] = None
    try:
        tmp_fd, tmp_path_str = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        tmp_path = Path(tmp_path_str)
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            tmp_fd = None  # fdopen owns the descriptor now
            handle.write(text)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise
    return path, payload


def _home_of(args) -> Optional[Path]:
    return Path(args.home).expanduser() if getattr(args, "home", None) else None


def _public_data(data: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in data.items() if key not in MANAGED_KEYS}


def _saved_view(path: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
    """The same shape as _profile_view() for the payload save_profile() just wrote.

    Reading the file back would only tell us what we already know, so the view
    is built from the payload itself.
    """
    clean, secret_like = strip_secret_like_keys(payload)
    view: Dict[str, Any] = {
        "path": str(path),
        "status": "ok",
        "version": payload.get("version"),
        "updated_at": payload.get("updated_at"),
        "profile": _public_data(clean),
    }
    if secret_like:
        view["secret_like_keys"] = secret_like
    return view


def _profile_view(loaded: LoadedProfile) -> Dict[str, Any]:
    """The part of every answer that describes the file as it is on disk.

    Values are shown in the open (no masking), but a secret-like key that got
    into the file by hand is left out: it is not a profile value, it is about
    to be removed, and its value must not travel into an agent's context.
    """
    usable = loaded.status == "ok"
    clean, secret_like = strip_secret_like_keys(loaded.data) if usable else ({}, [])
    view: Dict[str, Any] = {
        "path": loaded.path,
        "status": loaded.status,
        "version": loaded.version,
        "updated_at": loaded.data.get("updated_at") if usable else None,
        "profile": _public_data(clean),
    }
    if secret_like:
        view["secret_like_keys"] = secret_like
    if loaded.reason:
        view["reason"] = loaded.reason
    return view


def _emit(payload: Dict[str, Any]) -> int:
    """One JSON object to stdout, redacted.

    ``suggest`` reads the environment to name the login, and the profile itself
    holds no secret -- but a credential the user pasted into the wrong variable
    must not travel into the agent's context through this command either
    (REQ-003), so the rendered text goes through the same filter as every error.
    """
    print(redact_secrets(json.dumps(payload, ensure_ascii=False, indent=2)))
    return 0


def cmd_show(args) -> int:
    """Full map, path and save time. Never opens a socket (REQ-003)."""
    loaded = load_profile(_home_of(args))
    payload: Dict[str, Any] = {"ok": True, "command": "show"}
    payload.update(_profile_view(loaded))
    notes: List[str] = []
    if loaded.status == "missing":
        notes.append("профиля нет — собрать его можно через /guru-start или /guru_profile")
    elif loaded.status == "corrupt":
        # The reader names the offending keys in one ASCII line; the person who
        # has to fix them needs the per-key wording, so it is recomputed here
        # from the raw file (the only place a human reads it).
        #
        # ``issues`` is what the PERSON has to retype, so the stamps the family
        # writes itself are kept out of it: pointing someone at ``version`` or
        # ``updated_at`` sends them to a key ``set``/``unset`` never store. A
        # broken stamp is repaired by the next write of any key -- save_profile()
        # always writes a fresh one -- so it gets its own field and its own line.
        issues = validate_profile(normalize_profile(loaded.raw)) if loaded.raw is not None else []
        fixable = [item for item in issues if item["key"] not in MANAGED_KEYS]
        broken_stamp = [item["key"] for item in issues if item["key"] in MANAGED_KEYS]
        if fixable:
            payload["issues"] = fixable
        if broken_stamp:
            payload["broken_stamp"] = broken_stamp
        note = "файл профиля повреждён (%s) — операции идут без профиля" % loaded.reason
        if fixable:
            note += "; поправь эти ключи через set/unset или выполни reset"
        elif broken_stamp:
            note += ("; негоден только штамп (%s) — его выставляет само семейство, и любая запись профиля "
                     "перепишет его, например `unset %s`" % (", ".join(broken_stamp), broken_stamp[0]))
        else:
            note += "; файл не разбирается как JSON-объект — поправь его руками по пути из path или выполни reset"
        notes.append(note)
    elif loaded.status == "unsupported":
        notes.append("версия схемы профиля не поддерживается — обнови семейство или сбрось профиль (reset)")
    else:
        secret_like = payload.get("secret_like_keys") or []
        if secret_like:
            notes.append("в файле есть секретоподобные ключи (%s) — они не применяются и будут удалены при "
                         "ближайшем сохранении; секреты хранятся только в файле креда" % ", ".join(secret_like))
    if notes:
        payload["notes"] = notes
    return _emit(payload)


def parse_pairs(pairs: Sequence[str]) -> Dict[str, str]:
    """``KEY=VALUE`` arguments into a dotted-key patch (later duplicates win)."""
    patch: Dict[str, str] = {}
    for raw in pairs:
        if "=" not in raw:
            raise ValueError("ожидается КЛЮЧ=ЗНАЧЕНИЕ, получено %r" % raw)
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError("пустой ключ в %r" % raw)
        patch[key] = value
    return patch


def _apply_patch(command: str, args, patch: Dict[str, Any]) -> int:
    """Shared body of ``set`` and ``unset``: load, clean, merge, check, save.

    A file whose VALUES do not pass the schema is still editable: it parsed into
    a JSON object, so the keys that are fine are kept, the patch is applied on
    top, and the keys that stay invalid are dropped from what is written. That
    is the same self-healing rewrite secret-like keys already get, and without
    it one hand-typed value would lock the whole profile behind ``reset``. A
    file that is not a JSON object at all cannot be merged into, so it is still
    refused. The same goes for a broken stamp: ``version``/``updated_at`` are
    written by save_profile() alone, so they are never stored from the call and
    never kept from the file -- naming one only asks for the rewrite that repairs
    it, and the notes say which of the two happened.
    """
    home = _home_of(args)
    loaded = load_profile(home)
    if loaded.status == "unsupported":
        return emit_error(command, "профиль не сохранён: %s" % loaded.reason, path=loaded.path,
                          status=loaded.status)
    if loaded.status == "corrupt" and loaded.raw is None:
        return emit_error(
            command,
            "профиль не сохранён: файл повреждён (%s) — поправь его руками или выполни reset" % loaded.reason,
            path=loaded.path, status=loaded.status)

    malformed = [key for key in patch if not profile_split_key(key)]
    if malformed:
        return emit_error(command, "пустой ключ: %s" % ", ".join(repr(key) for key in malformed), path=loaded.path)
    # ``version``/``updated_at`` are written by save_profile() and by nothing
    # else, so a stamp named in the call is not refused -- it is dropped from the
    # patch and the write goes on, ending with a fresh stamp. Refusing it was a
    # dead end: a stamp broken by hand makes the file ``corrupt``, ``show`` then
    # prints an empty map, and the two commands the person reaches for
    # (``set version=1``, ``unset version``) were exactly the two that failed.
    # The value they passed is still not stored, and the note says so.
    named_stamps = [key for key in patch if key in MANAGED_KEYS]
    patch = {key: value for key, value in patch.items() if key not in MANAGED_KEYS}
    # Writing a secret-like key is refused; removing one is what strip_secret_like_keys
    # does anyway, so ``unset api_token`` (or ``set api_token=``) must go through.
    secret_keys = [key for key in patch if is_secret_like(key) and patch[key] not in (None, "", [])]
    if secret_keys:
        return emit_error(
            command,
            "профиль не сохранён: ключ %s похож на секрет — секреты хранятся только в файле креда "
            "(~/.swtr-review.env), в профиле их не бывает" % ", ".join(secret_keys),
            path=loaded.path, rejected_keys=secret_keys)
    # The key table of REQ-002 is closed, so a name outside it is a typo on write,
    # not a new key: no operation reads such a key, and "ok" would report as done
    # something that was not. The refusal lists the allowed names and the file is
    # left as it was. Removal goes through for an unknown key too, exactly as for
    # a secret-like one: clearing out junk added by hand has to be possible,
    # writing new junk does not. Keys already in the file are still kept on
    # rewrite.
    known_keys = list(profile_schema())
    unknown = [key for key in patch if key not in known_keys and patch[key] not in (None, "", [])]
    if unknown:
        return emit_error(
            command,
            "профиль не сохранён: в таблице ключей профиля нет %s; допустимые ключи: %s"
            % (", ".join(unknown), ", ".join(known_keys)),
            path=loaded.path, rejected_keys=unknown, known_keys=known_keys)

    # ``ok`` -- normalized data; a file broken by its values is repaired from its
    # own raw content; "no profile" is an empty base.
    source = loaded.data if loaded.status == "ok" else (loaded.raw if loaded.raw is not None else {})
    current, removed_secret_like = strip_secret_like_keys(source)
    before = normalize_profile(current)
    after = normalize_profile(merge_profile(current, patch))
    errors = validate_profile(after)
    # An invalid value the caller just passed is the caller's mistake: the key is
    # refused whole and the file stays as it was (REQ-002). An invalid value that
    # was already in the file is not: it is dropped, named, and the edit goes on.
    mine = [item for item in errors if item["key"] in patch]
    if mine:
        return emit_error(
            command,
            "профиль не сохранён: " + "; ".join("%s — %s" % (item["key"], item["reason"]) for item in mine),
            path=loaded.path, errors=mine)
    # A stamp that fails the check is not a value the person loses: save_profile()
    # writes a correct one anyway. It is reported apart from the keys they have to
    # type again, so no answer ever says "задай updated_at заново".
    dropped_items = [item for item in errors if item["key"] not in MANAGED_KEYS]
    dropped_invalid = [item["key"] for item in dropped_items]
    broken_stamps = [item["key"] for item in errors if item["key"] in MANAGED_KEYS]
    for item in errors:
        profile_delete_path(after, item["key"])

    changed = [key for key in patch if profile_get_path(before, key) != profile_get_path(after, key)]
    try:
        path, written = save_profile(home, after)
    except OSError as exc:
        return emit_error(command, "не удалось записать %s: %s" % (loaded.path, exc), path=loaded.path)

    payload: Dict[str, Any] = {"ok": True, "command": command}
    payload.update(_saved_view(path, written))
    payload["changed"] = changed
    notes: List[str] = []
    if removed_secret_like:
        payload["removed_secret_like"] = removed_secret_like
        notes.append("из файла удалены секретоподобные ключи: %s — секреты хранятся только в файле креда"
                     % ", ".join(removed_secret_like))
    if dropped_invalid:
        payload["dropped_invalid"] = dropped_items
        notes.append("из файла удалены ключи с недопустимыми значениями: %s — задай их заново через set"
                     % ", ".join(dropped_invalid))
    restamped = sorted(set(named_stamps) | set(broken_stamps))
    if restamped:
        payload["restamped"] = restamped
    if named_stamps:
        notes.append("ключи %s выставляет само семейство — переданные значения не записаны, в файле текущий штамп"
                     % ", ".join(named_stamps))
    if broken_stamps:
        notes.append("штамп профиля (%s) в файле был негоден — семейство переписало его само, файл снова читается"
                     % ", ".join(broken_stamps))
    # "Значения не изменились" is only about a file that changed in nothing but
    # the moment it was saved. A rewrite that dropped a secret-like or an invalid
    # key, or repaired the stamp, DID change the file, and saying "nothing
    # changed" about it next to the line naming what was removed hands the user
    # two mutually exclusive statements (REQ-004).
    if not changed:
        if removed_secret_like or dropped_invalid or broken_stamps:
            notes.append("по запрошенным ключам изменений нет — файл перезаписан ради названного выше")
        else:
            notes.append("значения не изменились")
    if notes:
        payload["notes"] = notes
    return _emit(payload)


def cmd_set(args) -> int:
    try:
        patch = parse_pairs(args.pairs)
    except ValueError as exc:
        return emit_error("set", exc)
    return _apply_patch("set", args, patch)


def cmd_unset(args) -> int:
    keys = [key.strip() for key in args.keys if key.strip()]
    if not keys:
        return emit_error("unset", "укажи хотя бы один ключ")
    return _apply_patch("unset", args, {key: None for key in keys})


def cmd_reset(args) -> int:
    path = profile_path(_home_of(args))
    removed = False
    try:
        if path.exists():
            path.unlink()
            removed = True
    except OSError as exc:
        return emit_error("reset", "не удалось удалить %s: %s" % (path, exc), path=str(path))
    return _emit({
        "ok": True,
        "command": "reset",
        "path": str(path),
        "removed": removed,
        "notes": ["профиль сброшен — собрать заново можно через /guru-start или /guru_profile"
                  if removed else "профиля и так не было"],
    })


def _tracker():
    """swtr_task_tool, imported on demand.

    Only ``suggest`` needs the tracker CLI (spaces request, assignee resolution,
    the ``assigned`` TQL); the file commands must neither depend on it nor pay
    for its import. The module is imported by name, so a test that swaps its
    network layer (``swtr_task_tool.request_http``) reaches every call made here.
    """
    import swtr_task_tool  # lazy on purpose, see docstring
    return swtr_task_tool


def suggest_login(tracker) -> Dict[str, Any]:
    """Login the sample is drawn for: ``SWTR_LOGIN``, else the login half of ``SWTR_BASIC_AUTH``.

    ``USER`` is deliberately not consulted, unlike in ``_resolve_assignees``: an
    OS account name is a guess, and a suggestion must name a real origin. The
    password half of the Basic credential is never read, and a value without a
    colon is not a credential pair at all -- the tracker helper returns nothing
    for it, so a bare token pasted into SWTR_BASIC_AUTH can never be offered as
    a login and can never reach stdout (REQ-003).
    """
    env_login = os.environ.get("SWTR_LOGIN", "").strip()
    if env_login:
        return {"value": env_login, "source": "env", "basis": "переменная SWTR_LOGIN"}
    basic_login = tracker.login_from_basic_auth()
    if basic_login:
        return {"value": basic_login, "source": "credential",
                "basis": "логин из SWTR_BASIC_AUTH (часть до двоеточия; пароль не читается)"}
    if os.environ.get("SWTR_BASIC_AUTH", "").strip():
        return {"value": None, "source": None,
                "basis": "не извлекается: в SWTR_BASIC_AUTH нет пары «логин:пароль» — значение не читается"}
    return {"value": None, "source": None,
            "basis": "не извлекается: нет ни SWTR_LOGIN, ни SWTR_BASIC_AUTH"}


def source_control_hints() -> Dict[str, Dict[str, Any]]:
    """SourceControl ``tenant``/``project`` from the access-check probe parameters.

    REQ-006 asks for the SourceControl question "only when the access-check
    parameters are configured", so only a value the user actually set counts.
    The family-wide probe default is somebody else's project: offering it would
    produce a plausible-looking answer option that the onboarding is required to
    throw away, and a mis-tap would write a foreign tenant into the profile.
    A key without its variable is simply absent from the answer.
    """
    hints: Dict[str, Dict[str, Any]] = {}
    for key, env_name in (
        ("tenant", "SOURCE_CONTROL_PROBE_TENANT"),
        ("project", "SOURCE_CONTROL_PROBE_PROJECT"),
    ):
        env_value = os.environ.get(env_name, "").strip()
        if env_value:
            hints[key] = {"value": env_value, "source": "env", "basis": "переменная %s" % env_name}
    return hints


def _unit_of(row: Dict[str, Any]) -> Dict[str, Any]:
    """The unit object of a find/tql row (wrapped in ``unit`` on the live portal), as unit_result_key does."""
    unit = row.get("unit")
    return unit if isinstance(unit, dict) else row


def _code_text(value: Any) -> str:
    """Code of a ``space``/``suit`` field: a bare string on the portal; ``code`` of an object if it ever is one."""
    if isinstance(value, dict):
        code = value.get("code")
        if isinstance(code, str) and code.strip():
            return code.strip()
    return normalize_text(value)


def count_units(rows: Sequence[Any]) -> Tuple[Dict[str, int], Dict[str, int], int]:
    """``(by_space, by_suit, total)`` over find/tql rows; rows without a code are skipped per field."""
    by_space: Dict[str, int] = {}
    by_suit: Dict[str, int] = {}
    total = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        unit = _unit_of(row)
        total += 1
        space = _code_text(unit.get("space"))
        if space:
            by_space[space] = by_space.get(space, 0) + 1
        suit = _code_text(unit.get("suit"))
        if suit:
            by_suit[suit] = by_suit.get(suit, 0) + 1
    return by_space, by_suit, total


def _ranked(counts: Dict[str, int]) -> List[Tuple[str, int]]:
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def spaces_basis(ranked: Sequence[Tuple[str, int]]) -> str:
    """«по вашим задачам: 34 в PVM, 5 в CRPV» -- the origin text of every space suggestion (REQ-007)."""
    shown = ranked[:SUGGEST_BASIS_MAX_SPACES]
    text = "по вашим задачам: " + ", ".join("%d в %s" % (count, code) for code, count in shown)
    rest = len(ranked) - len(shown)
    if rest > 0:
        text += ", и ещё %d" % rest
    return text


def space_suggestions(ranked: Sequence[Tuple[str, int]]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """``(primary_space, spaces_in_view)`` from one unit on; ``(None, None)`` on an empty sample."""
    if not ranked:
        return None, None
    basis = spaces_basis(ranked)
    codes = [code for code, _ in ranked]
    primary = {"value": codes[0], "options": codes, "source": "tasks", "basis": basis}
    in_view = {"value": codes, "source": "tasks", "basis": basis}
    return primary, in_view


def suit_suggestion(ranked: Sequence[Tuple[str, int]], total: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """``(default_suit, note)``: the suggestion when one suit dominates, else a note saying why not."""
    if total < SUGGEST_SUIT_MIN_TASKS:
        return None, ("тип задачи по умолчанию не предлагается: в выборке %d, нужно не меньше %d"
                      % (total, SUGGEST_SUIT_MIN_TASKS))
    if not ranked:
        # Enough units, none of them carrying a suit code: saying "the sample is
        # too small" here would contradict the number right next to it.
        return None, "тип задачи по умолчанию не предлагается: ни у одной задачи выборки не назван тип"
    code, count = ranked[0]
    numerator, denominator = SUGGEST_SUIT_MIN_SHARE
    percent = int(round(100.0 * count / total))
    if count * denominator < total * numerator:
        return None, ("тип задачи по умолчанию не предлагается: самый частый тип %s — %d из %d (%d%%), "
                      "нужно не меньше %d/%d" % (code, count, total, percent, numerator, denominator))
    return {
        "value": code,
        "tasks": count,
        "of": total,
        "share": round(count / total, 2),
        "source": "tasks",
        "basis": "по вашим задачам: %s — %d из %d (%d%%)" % (code, count, total, percent),
    }, None


def merge_spaces(available: Sequence[Dict[str, Any]], by_space: Dict[str, int], counted: bool) -> List[Dict[str, Any]]:
    """Spaces the user can see plus any the sample mentions, with the sample count per space.

    ``tasks`` is present only when there WAS a sample to count: an unknown count
    must not be printed as ``0``, and ``0`` next to a space the sample simply
    never mentioned is a fact. Sorted by count, then code.
    """
    rows: Dict[str, Dict[str, Any]] = {}
    for space in available:
        code = str(space.get("code") or "").strip()
        if not code:
            continue
        rows[code] = {"code": code, "name": space.get("name") or None, "type": space.get("type") or None}
        if counted:
            rows[code]["tasks"] = by_space.get(code, 0)
    for code, count in by_space.items():
        if code not in rows:
            rows[code] = {"code": code, "name": None, "type": None, "tasks": count}
    return sorted(rows.values(), key=lambda row: (-row.get("tasks", 0), row["code"]))


def fetch_sample(tracker, base_url: str, login: str, timeout: float) -> Tuple[Dict[str, Any], List[Any]]:
    """``(sample, rows)``: up to SUGGEST_SAMPLE_LIMIT units assigned to ``login``, any status, one page.

    The query is the one ``assigned --me --status all`` sends -- the login is
    resolved to its ``externalId`` the same way and the TQL comes from
    build_assigned_tql -- with the page size raised to the limit. The tracker
    documents no sort for the TQL endpoint, so the page is the tracker's own
    order and the sample is NOT "the newest N": when the tracker says there are
    more units than the page holds, ``truncated`` says so, and cmd_suggest turns
    that into a note, so a share computed over one page is never presented as a
    share over everything. The units themselves are returned APART from the
    sample and never put inside it: the sample is printed as is, and a whole
    page of tracker rows in the answer would flood the agent's context -- a
    contract "the caller must throw a key away" is not one this file writes
    down anywhere.
    """
    values, resolve_notes = tracker.resolve_assignee_codes(base_url, [login], timeout)
    tql = tracker.build_assigned_tql(values, status="all", spaces=[], suits=[])
    response = tracker.find_units_tql(
        base_url,
        query=tql,
        return_attributes=list(tracker.ASSIGNED_RETURN_ATTRIBUTES),
        page=0,
        size=SUGGEST_SAMPLE_LIMIT,
        timeout=timeout,
    )
    rows = tracker.page_content(response)
    sample: Dict[str, Any] = {
        "login": login,
        "assignee_values": values,
        "endpoint": tracker.TQL_FIND_ENDPOINT,
        "tql": tql,
        "limit": SUGGEST_SAMPLE_LIMIT,
        "count": len(rows),
    }
    total = response.get("totalElements") if isinstance(response, dict) else None
    if isinstance(total, int) and not isinstance(total, bool):
        sample["total"] = total
        sample["truncated"] = total > len(rows)
    else:
        sample["truncated"] = len(rows) >= SUGGEST_SAMPLE_LIMIT
    if resolve_notes:
        sample["notes"] = list(resolve_notes)
    return sample, rows


def cmd_suggest(args) -> int:
    """Answer options for the onboarding step, from the user's own data (REQ-007).

    Read-only. Every failure of the tracker -- no credential, network, HTTP --
    is a line in ``notes`` and a missing suggestion, never an error exit: the
    onboarding goes on with free-text questions. Every suggestion names its
    origin (``source`` + ``basis``).
    """
    load_env_file(Path(args.env_file).expanduser())
    tracker = _tracker()
    base_url = base_url_for("task_tracker")
    timeout = float(args.timeout)
    notes: List[str] = []

    login = suggest_login(tracker)
    if login["value"] is None:
        notes.append("логин не извлекается (нет ни SWTR_LOGIN, ни SWTR_BASIC_AUTH) — выборка ваших задач не "
                     "делалась, подсказок по пространству и типу задачи нет; логин спрашивается свободным вводом")

    tracker_ready = build_authorization("task_tracker") is not None
    if not tracker_ready:
        notes.append("доступ к трекеру не настроен — список пространств и выборка задач недоступны, вопросы "
                     "задаются без подсказок; кред настраивается через /guru_doctor")

    available: List[Dict[str, Any]] = []
    if tracker_ready:
        try:
            available = tracker.normalize_spaces(tracker.post_json(base_url, SPACES_ENDPOINT, {}, timeout))
        except Exception as exc:  # noqa: BLE001 - a tracker failure is a partial summary, not an error (REQ-007)
            notes.append("список пространств трекера недоступен (%s) — пространства спрашиваются свободным вводом"
                         % redact_secrets(str(exc)))

    sample: Optional[Dict[str, Any]] = None
    sample_rows: List[Any] = []
    if tracker_ready and login["value"]:
        try:
            sample, sample_rows = fetch_sample(tracker, base_url, login["value"], timeout)
        except Exception as exc:  # noqa: BLE001 - same rule: degrade, do not fail
            notes.append("выборка назначенных задач недоступна (%s) — подсказок по пространству и типу задачи нет"
                         % redact_secrets(str(exc)))

    by_space: Dict[str, int] = {}
    by_suit: Dict[str, int] = {}
    primary_space = spaces_in_view = default_suit = None
    if sample is not None:
        by_space, by_suit, total = count_units(sample_rows)
        ranked_spaces = _ranked(by_space)
        ranked_suits = _ranked(by_suit)
        sample["by_space"] = [{"code": code, "tasks": count} for code, count in ranked_spaces]
        sample["by_suit"] = [{"code": code, "tasks": count} for code, count in ranked_suits]
        if total == 0:
            notes.append("в трекере нет задач, назначенных на %s — подсказок по пространству и типу задачи нет"
                         % login["value"])
        elif sample.get("truncated"):
            notes.append("подсказки построены по одной странице выборки (%d задач из %s) в порядке, который "
                         "задаёт трекер, — это не срез последних по дате; доли и порядок пространств могут "
                         "отличаться от картины по всем задачам"
                         % (total, sample.get("total", "большего числа")))
        primary_space, spaces_in_view = space_suggestions(ranked_spaces)
        default_suit, suit_note = suit_suggestion(ranked_suits, total)
        if suit_note and total > 0:
            notes.append(suit_note)

    return _emit({
        "ok": True,
        "command": "suggest",
        "login": login,
        "spaces": merge_spaces(available, by_space, counted=sample is not None),
        "primary_space": primary_space,
        "spaces_in_view": spaces_in_view,
        "default_suit": default_suit,
        "source_control": source_control_hints(),
        "sample": sample,
        "notes": notes,
    })


def add_home_arg(parser: argparse.ArgumentParser) -> None:
    """``--home`` for the four file commands. ``suggest`` does not touch the file."""
    parser.add_argument("--home", default=None,
                        help="Домашний каталог (по умолчанию — текущий пользователь)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Профиль пользователя pvm-guru (~/.config/pvm-guru/profile.json): показать, изменить, "
                    "сбросить; suggest — подсказки для онбординга из данных пользователя (только чтение "
                    "трекера). Один JSON-объект в stdout; show/set/unset/reset в сеть не ходят.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_show = sub.add_parser("show", help="карта профиля, путь к файлу и момент сохранения")
    add_home_arg(p_show)
    p_show.set_defaults(func=cmd_show)

    p_set = sub.add_parser("set", help="изменить ключи: КЛЮЧ=ЗНАЧЕНИЕ (списки через запятую, пустое значение удаляет)")
    p_set.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    add_home_arg(p_set)
    p_set.set_defaults(func=cmd_set)

    p_unset = sub.add_parser("unset", help="удалить ключи")
    p_unset.add_argument("keys", nargs="+", metavar="KEY")
    add_home_arg(p_unset)
    p_unset.set_defaults(func=cmd_unset)

    p_reset = sub.add_parser("reset", help="удалить файл профиля")
    add_home_arg(p_reset)
    p_reset.set_defaults(func=cmd_reset)

    p_suggest = sub.add_parser(
        "suggest",
        help="подсказки для шага умолчаний: логин из креда, пространства и тип задачи по вашим задачам "
             "(одна страница до %d), tenant/проект SourceControl из параметров проверки доступа; только чтение"
             % SUGGEST_SAMPLE_LIMIT)
    p_suggest.add_argument("--env-file", default=ENV_FILE_DEFAULT,
                           help="Файл креда; его читает только suggest — файловым командам он не нужен")
    p_suggest.add_argument("--timeout", type=float, default=30.0,
                           help="таймаут HTTP в секундах (по умолчанию 30); отказ трекера — частичная сводка")
    p_suggest.set_defaults(func=cmd_suggest)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
