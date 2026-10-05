#!/usr/bin/env python3
"""SourceControl lookup and code search helper.

Prints a JSON document to stdout for every subcommand (repos / branches / file
/ search). The script no longer writes intermediate files; the agent decides
whether to persist anything based on the workflow.

The script prefers downloading a repository archive for one branch/commit
because that is usually much faster than recursive API traversal. It falls
back to the contents/raw API when the archive endpoint is unavailable.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _swtr_common import (  # noqa: E402
    PROFILE_PATH_HINT,
    apply_sc_project_defaults,
    args_error_context,
    args_with_defaults,
    attach_profile,
    base_url_for,
    build_headers,
    emit_error,
    is_auth_error,
    load_env_file,
    profile_get,
    quote_path_part,
    quote_repo_path,
    redact_secrets,
    request_http,
    value_to_text,
)

TEXT_EXTENSIONS = {
    ".py", ".java", ".kt", ".kts", ".scala", ".go", ".js", ".jsx", ".ts", ".tsx", ".vue",
    ".sql", ".yaml", ".yml", ".json", ".xml", ".properties", ".conf", ".cfg", ".ini", ".toml",
    ".md", ".adoc", ".txt", ".sh", ".bash", ".gradle", ".groovy", ".html", ".css", ".scss",
    ".proto", ".graphql", ".tf", ".dockerfile",
}
TEXT_FILENAMES = {"dockerfile", "makefile", "gradlew", "pom.xml", "build.gradle", "settings.gradle"}
BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tar", ".jar",
    ".war", ".class", ".so", ".dll", ".dylib", ".exe", ".bin", ".p12", ".jks", ".keystore",
}
HEX_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

# Профиль пользователя (~/.config/pvm-guru/profile.json) закрывает пропуски там,
# где значение не названо: tenant и проект SourceControl. Читается одной точкой в
# main() и лежит в args.profile; --no-profile кладёт туда None, и всё ведёт себя
# как без профиля. Порядок всегда явное → окружение → профиль → прежнее
# поведение (слова argparse и код 2), а каждое применённое умолчание попадает в
# args.defaults_applied: источник значения виден в JSON структурно, а не по
# памяти агента. Пустой список не печатается.
# Таблица tenant/проекта, её резолв и текст подсказки к отказу — общие с
# source_control_pr.py и живут в _swtr_common.


def emit(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def profile_repos(profile: Any) -> List[str]:
    """Репозитории «в поле зрения» из профиля, без пустых значений.

    Значение приходит проверенным схемой (load_profile отдаёт `ok` только такому
    файлу); isinstance остаётся защитой от прямого вызова с чужим объектом —
    падать TypeError'ом наружу этой функции нельзя.
    """
    repos = profile_get(profile, "source_control.repos")
    if not isinstance(repos, list):
        repos = [repos] if isinstance(repos, str) else []
    return [str(repo).strip() for repo in repos if str(repo).strip()]


# Конверты, в которые SourceControl заворачивает список: `raw` — тот, который
# ставит сам list_repos(), когда API ответил голым JSON-массивом. Без него
# пересечение с профилем молча оказывалось бы пустым именно на таких
# инсталляциях.
REPO_LIST_ENVELOPES = ("data", "content", "items", "values", "repositories", "raw")


def repo_items(payload: Any) -> List[Dict[str, Any]]:
    """Репозитории из ответа API, каким бы конвертом он ни был обёрнут.

    Голого массива здесь не бывает: единственный источник `payload` — list_repos(),
    который сам заворачивает не-объектный ответ в `{"raw": ...}`, а `raw` есть в
    списке конвертов.
    """
    if isinstance(payload, dict):
        for key in REPO_LIST_ENVELOPES:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def profile_repo_matches(payload: Any, repos: Sequence[str]) -> List[str]:
    """Репозитории профиля, которые встретились в выдаче, — в порядке профиля.

    Ответ бэкенда не трогается: `response` и `candidates` печатаются ровно
    такими, какими их вернул API, а пересечение с профилем живёт отдельным
    нормализованным полем рядом с `profileRepos` — вывод содержит и сырой ответ,
    и нормализацию, и одно от другого отличимо. Метка нужна агенту, чтобы
    предложить знакомые репозитории первыми (REQ-018); пустой список профиля
    (профиля нет, ключ не заполнен, --no-profile) даёт пустой результат, и поле
    не печатается вовсе.
    """
    if not repos:
        return []
    listed: Set[str] = set()
    for item in repo_items(payload):
        name = item.get("name") or item.get("repo") or item.get("slug") or item.get("full_name")
        code = str(name or "").strip().casefold()
        if code:
            listed.add(code)
    return [repo for repo in repos if repo.strip().casefold() in listed]


def sc_get(base_url: str, endpoint: str, params: Optional[Dict[str, Any]], timeout: float, *, binary: bool = False, accept: str = "application/json") -> Any:
    headers = build_headers("source_control", require_auth=True, accept=accept)
    query = ""
    if params:
        query = "?" + "&".join(
            f"{quote_path_part(str(k))}={quote_path_part(str(v))}" for k, v in params.items() if v is not None and v != ""
        )
    payload, _, _ = request_http("GET", base_url.rstrip("/") + endpoint + query, headers, timeout=timeout, system="source_control", binary=binary)
    return payload


def list_repos(base_url: str, tenant: str, project: str, *, query: Optional[str], page: int, limit: int, timeout: float) -> Dict[str, Any]:
    endpoint = f"/repos/{quote_path_part(tenant)}/{quote_path_part(project)}"
    params = {"page": page, "limit": limit, "sort": "updated_at:desc,name:asc"}
    if query:
        params["q"] = query
    payload = sc_get(base_url, endpoint, params, timeout)
    return payload if isinstance(payload, dict) else {"raw": payload}


def list_branches(base_url: str, tenant: str, project: str, repo: str, *, query: Optional[str], page: int, limit: int, timeout: float) -> Dict[str, Any]:
    endpoint = f"/repos/{quote_path_part(tenant)}/{quote_path_part(project)}/{quote_path_part(repo)}/branches"
    params = {"page": page, "limit": limit, "sort": "date:desc,name:asc"}
    if query:
        params["q"] = query
    payload = sc_get(base_url, endpoint, params, timeout)
    return payload if isinstance(payload, dict) else {"raw": payload}


def get_repo(base_url: str, tenant: str, project: str, repo: str, timeout: float) -> Dict[str, Any]:
    endpoint = f"/repos/{quote_path_part(tenant)}/{quote_path_part(project)}/{quote_path_part(repo)}"
    payload = sc_get(base_url, endpoint, None, timeout)
    return payload if isinstance(payload, dict) else {"raw": payload}


def get_branch(base_url: str, tenant: str, project: str, repo: str, ref: str, timeout: float) -> Dict[str, Any]:
    endpoint = f"/repos/{quote_path_part(tenant)}/{quote_path_part(project)}/{quote_path_part(repo)}/branches/{quote_path_part(ref)}"
    payload = sc_get(base_url, endpoint, None, timeout)
    return payload if isinstance(payload, dict) else {"raw": payload}


def extract_branch_sha(branch_payload: Dict[str, Any]) -> Optional[str]:
    candidates = [
        branch_payload.get("sha"),
        branch_payload.get("hash"),
        branch_payload.get("commit_sha"),
        branch_payload.get("last_commit_sha"),
    ]
    commit = branch_payload.get("commit")
    if isinstance(commit, dict):
        candidates.extend([commit.get("sha"), commit.get("hash"), commit.get("id")])
    for value in candidates:
        text = value_to_text(value)
        if HEX_RE.match(text):
            return text
    return None


def extract_default_branch(repo_payload: Dict[str, Any]) -> Optional[str]:
    for key in ("default_branch", "defaultBranch", "default_branch_name", "main_branch"):
        value = value_to_text(repo_payload.get(key))
        if value:
            return value
    return None


def is_text_path(path: str) -> bool:
    p = Path(path.lower())
    if p.name in TEXT_FILENAMES:
        return True
    if p.suffix in BINARY_EXTENSIONS:
        return False
    if p.suffix in TEXT_EXTENSIONS:
        return True
    return p.suffix == "" and len(path) < 200


def decode_possible_base64(value: str) -> bytes:
    compact = "".join(value.split())
    try:
        return base64.b64decode(compact, validate=True)
    except Exception:
        return value.encode("utf-8", errors="replace")


def decode_file_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        # Only base64-decode when the API explicitly signals encoding=base64.
        # Guessing corrupts files whose plain content is itself valid base64.
        encoding = str(payload.get("encoding") or "").strip().lower()
        for key in ("content", "raw", "data", "value"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                if encoding == "base64":
                    return decode_possible_base64(value).decode("utf-8", errors="replace")
                return value
        return json.dumps(payload, ensure_ascii=False, indent=2)
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    if isinstance(payload, str):
        # /raw returns the file as-is; never guess base64 here.
        return payload
    return value_to_text(payload)


def search_text_in_file(path: str, text: str, queries: Sequence[str], *, context: int, max_matches: int) -> List[Dict[str, Any]]:
    lower_queries = [q.lower() for q in queries if q.strip()]
    tokens = []
    for q in lower_queries:
        tokens.extend([t for t in re.split(r"\s+", q) if len(t) >= 2])
    if not lower_queries and not tokens:
        return []
    lines = text.splitlines()
    matches: List[Dict[str, Any]] = []
    for idx, line in enumerate(lines, 1):
        low = line.lower()
        phrase_score = sum(4 for q in lower_queries if q and q in low)
        token_score = sum(1 for token in tokens if token in low)
        score = phrase_score + token_score
        if score <= 0:
            continue
        start = max(0, idx - 1 - context)
        end = min(len(lines), idx + context)
        matches.append(
            {
                "path": path,
                "line": idx,
                "score": score,
                "snippet": "\n".join(lines[start:end])[:4000],
            }
        )
        if len(matches) >= max_matches:
            break
    return matches


def get_raw_file(base_url: str, tenant: str, project: str, repo: str, filepath: str, ref: Optional[str], timeout: float) -> str:
    endpoint = f"/repos/{quote_path_part(tenant)}/{quote_path_part(project)}/{quote_path_part(repo)}/raw/{quote_repo_path(filepath)}"
    payload = sc_get(base_url, endpoint, {"ref": ref} if ref else None, timeout)
    return decode_file_payload(payload)


def list_contents(base_url: str, tenant: str, project: str, repo: str, filepath: str, ref: Optional[str], page: int, limit: int, timeout: float) -> Dict[str, Any]:
    encoded_path = quote_repo_path(filepath) if filepath else ""
    endpoint = f"/repos/{quote_path_part(tenant)}/{quote_path_part(project)}/{quote_path_part(repo)}/contents/{encoded_path}"
    payload = sc_get(base_url, endpoint, {"ref": ref, "page": page, "limit": limit}, timeout)
    return payload if isinstance(payload, dict) else {"raw": payload}


def content_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    if payload.get("type") in {"file", "dir", "symlink", "submodule"}:
        return [payload]
    for key in ("data", "content", "items", "files"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def recursive_search(
    base_url: str,
    tenant: str,
    project: str,
    repo: str,
    ref: Optional[str],
    queries: Sequence[str],
    *,
    path_prefix: str,
    timeout: float,
    max_files: int,
    max_matches_per_file: int,
    context: int,
    max_pages: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    queue = [path_prefix.strip("/")]
    visited_dirs = 0
    scanned_files = 0
    matches: List[Dict[str, Any]] = []
    errors: List[str] = []
    notes: List[str] = []
    page_size = 1000
    while queue and scanned_files < max_files:
        current = queue.pop(0)
        visited_dirs += 1
        # Paginate the directory listing so directories with >1000 entries are
        # not silently truncated. A max_pages guard bounds the loop so a huge or
        # misbehaving directory (or an API that never returns a short page) can
        # never spin forever; if the guard trips we record a note rather than
        # silently dropping entries.
        dir_items: List[Dict[str, Any]] = []
        page = 1
        while page <= max_pages:
            try:
                payload = list_contents(base_url, tenant, project, repo, current, ref, page, page_size, timeout)
            except RuntimeError as exc:
                # An auth failure (401/403) is never a benign per-directory error --
                # every listing will fail -- so propagate it out of recursive_search
                # to main's error contract instead of collecting an empty result.
                if is_auth_error(exc):
                    raise
                errors.append(redact_secrets(str(exc)))
                break
            items = content_items(payload)
            if not items:
                break
            dir_items.extend(items)
            if len(items) < page_size:
                break
            page += 1
        else:
            # Loop exited because page exceeded max_pages (never via break), so the
            # listing for this directory may be truncated. Surface it in notes.
            notes.append(
                f"Directory listing for '{current or '/'}' truncated at max_pages={max_pages} "
                f"(page_size={page_size}); some entries in this directory may be unscanned."
            )
        for item in dir_items:
            item_type = value_to_text(item.get("type"))
            path = value_to_text(item.get("path") or item.get("filepath") or item.get("name"))
            if not path:
                continue
            if item_type == "dir":
                queue.append(path)
                continue
            if item_type != "file" or not is_text_path(path):
                continue
            scanned_files += 1
            if scanned_files > max_files:
                break
            raw_content = item.get("content")
            if isinstance(raw_content, str) and raw_content:
                text = decode_file_payload({"content": raw_content, "encoding": item.get("encoding")})
            else:
                try:
                    text = get_raw_file(base_url, tenant, project, repo, path, ref, timeout)
                except RuntimeError as exc:
                    # A single unreadable file (404/timeout) is benign and collected;
                    # an auth failure (401/403) propagates out to the error contract.
                    if is_auth_error(exc):
                        raise
                    errors.append(redact_secrets(str(exc)))
                    continue
            matches.extend(search_text_in_file(path, text, queries, context=context, max_matches=max_matches_per_file))
    # The inner loop breaks at scanned_files == max_files+1, so `> max_files` is the
    # usual truncation signal. It misses the boundary case where the last scanned
    # file lands scanned_files exactly on max_files while directories are still
    # queued: the outer `while ... scanned_files < max_files` then exits without
    # scanning them. Add `== max_files and queue` so that genuine truncation is
    # still reported. (This is additive: every case that fired before still fires.)
    if scanned_files > max_files or (scanned_files == max_files and queue):
        notes.append(
            f"Scanned-file cap reached (max_files={max_files}); the repository was only "
            f"partially traversed. Narrow with --path-prefix or raise --max-files."
        )
    meta = {"method": "recursive_contents", "visitedDirs": visited_dirs, "scannedFiles": scanned_files, "errors": errors[:20]}
    if notes:
        meta["notes"] = notes
    return matches, meta


def resolve_ref_to_sha(base_url: str, tenant: str, project: str, repo: str, ref: Optional[str], timeout: float) -> Tuple[Optional[str], Optional[str], List[str]]:
    errors: List[str] = []
    if ref and HEX_RE.match(ref):
        return ref, ref, errors
    candidate_ref = ref
    if not candidate_ref:
        try:
            repo_payload = get_repo(base_url, tenant, project, repo, timeout)
            candidate_ref = extract_default_branch(repo_payload)
        except RuntimeError as exc:
            errors.append(redact_secrets(str(exc)))
    if not candidate_ref:
        candidate_ref = "main"
    try:
        branch = get_branch(base_url, tenant, project, repo, candidate_ref, timeout)
        sha = extract_branch_sha(branch)
        if sha:
            return sha, candidate_ref, errors
    except RuntimeError as exc:
        errors.append(redact_secrets(str(exc)))
    return None, candidate_ref, errors


def search_archive(
    base_url: str,
    tenant: str,
    project: str,
    repo: str,
    sha: str,
    queries: Sequence[str],
    *,
    path_prefix: str,
    timeout: float,
    max_files: int,
    max_matches_per_file: int,
    context: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    endpoint = f"/repos/{quote_path_part(tenant)}/{quote_path_part(project)}/{quote_path_part(repo)}/archive"
    headers = build_headers("source_control", require_auth=True, accept="application/octet-stream")
    url = base_url.rstrip("/") + endpoint + "?" + f"sha={quote_path_part(sha)}&format=zip"
    payload, _, _ = request_http("GET", url, headers, timeout=timeout, system="source_control", binary=True)
    if not isinstance(payload, (bytes, bytearray)):
        raise RuntimeError("Archive endpoint returned non-binary payload")
    matches: List[Dict[str, Any]] = []
    scanned_files = 0
    skipped_files = 0
    prefix = path_prefix.strip("/")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            raw_name = info.filename
            parts = raw_name.split("/", 1)
            logical_path = parts[1] if len(parts) == 2 else raw_name
            if prefix and not logical_path.startswith(prefix.rstrip("/") + "/") and logical_path != prefix:
                continue
            if not is_text_path(logical_path):
                skipped_files += 1
                continue
            scanned_files += 1
            if scanned_files > max_files:
                break
            try:
                raw = archive.read(info)
            except Exception:
                skipped_files += 1
                continue
            if b"\x00" in raw[:4096]:
                skipped_files += 1
                continue
            text = raw.decode("utf-8", errors="replace")
            matches.extend(search_text_in_file(logical_path, text, queries, context=context, max_matches=max_matches_per_file))
    meta = {"method": "archive", "sha": sha, "archiveBytes": len(payload), "scannedFiles": scanned_files, "skippedFiles": skipped_files}
    if scanned_files > max_files:
        meta["notes"] = [
            f"Scanned-file cap reached (max_files={max_files}); the archive was only "
            f"partially scanned. Narrow with --path-prefix or raise --max-files."
        ]
    return matches, meta


def rank_matches(matches: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    return sorted(matches, key=lambda m: (-int(m.get("score", 0)), str(m.get("path", "")), int(m.get("line", 0))))[:limit]


def cmd_repos(args: argparse.Namespace) -> int:
    payload = list_repos(args.base_url, args.tenant, args.project, query=args.query, page=args.page, limit=args.limit, timeout=args.timeout)
    repos = profile_repos(args.profile)
    result: Dict[str, Any] = {"command": "repos", "tenant": args.tenant, "project": args.project,
                              "query": args.query, "response": payload}
    if repos:
        result["profileRepos"] = repos
        result["profileRepoMatches"] = profile_repo_matches(payload, repos)
    emit(args_with_defaults(result, args))
    return 0


def cmd_branches(args: argparse.Namespace) -> int:
    payload = list_branches(args.base_url, args.tenant, args.project, args.repo, query=args.query, page=args.page, limit=args.limit, timeout=args.timeout)
    emit(args_with_defaults({"command": "branches", "tenant": args.tenant, "project": args.project,
                         "repo": args.repo, "query": args.query, "response": payload}, args))
    return 0


def cmd_file(args: argparse.Namespace) -> int:
    text = get_raw_file(args.base_url, args.tenant, args.project, args.repo, args.filepath, args.ref, args.timeout)
    emit(args_with_defaults({
        "command": "file",
        "tenant": args.tenant,
        "project": args.project,
        "repo": args.repo,
        "ref": args.ref,
        "filepath": args.filepath,
        "chars": len(text),
        "content": text,
    }, args))
    return 0


def search_payload(args: argparse.Namespace) -> Dict[str, Any]:
    """Тело ответа поиска. Отделено от печати, чтобы `grep` переиспользовал его целиком."""
    if not args.repo:
        candidate_query = args.repo_query or (args.query[0] if args.query else None)
        payload = list_repos(args.base_url, args.tenant, args.project, query=candidate_query, page=1, limit=args.repo_limit, timeout=args.timeout)
        # Репозиторий не назван — ровно тот случай, ради которого профиль ведёт
        # список: пересечение считается так же, как в выдаче `repos`.
        repos = profile_repos(args.profile)
        result: Dict[str, Any] = {
            "command": args.command,
            "needsRepository": True,
            "tenant": args.tenant,
            "project": args.project,
            "queries": list(args.query),
            "candidates": payload,
        }
        # Листинг кандидатов отфильтрован поисковым запросом, поэтому знакомый
        # репозиторий в него может не попасть вовсе. Список из профиля идёт в
        # ответ отдельным полем — независимо от того, что вернул API (REQ-018).
        if repos:
            result["profileRepos"] = repos
            result["profileRepoMatches"] = profile_repo_matches(payload, repos)
        return result

    # Deterministic auth precheck. Missing/misconfigured SourceControl auth is the
    # ONE real "total failure": build_headers(require_auth=True) raises RuntimeError,
    # which propagates to main's `except RuntimeError -> emit_error` (unified
    # contract, exit 1). Do it up front, OUTSIDE the archive/recursive traversal
    # (whose per-item RuntimeErrors are deliberately caught and treated as benign),
    # so a genuine no-auth failure is never mis-swallowed as an empty result. The
    # returned headers are unused here -- each traversal builds its own; this is a
    # cheap pure call that fails fast before any network I/O. Every other error
    # (archive unavailable, ref-resolve failure, a single unreadable file during a
    # partial scan) is BENIGN and must never, on its own, fail the command.
    build_headers("source_control", require_auth=True)

    ref_used = args.ref
    archive_errors: List[str] = []
    matches: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {}
    # archive_ok is True ONLY when search_archive() returned without raising
    # (regardless of match count). A completed archive search greps the whole-repo
    # archive and is therefore AUTHORITATIVE: an empty result then legitimately
    # means "zero matches in the repo", NOT "archive unavailable". The recursive
    # walk is capped by max_files/max_pages and is strictly a FALLBACK for when the
    # archive path could not run, so it must never override a completed archive
    # search -- a recursive-fallback failure cannot invalidate a clean, empty
    # archive result.
    archive_ok = False
    if not args.no_archive:
        sha, resolved_ref, errors = resolve_ref_to_sha(args.base_url, args.tenant, args.project, args.repo, args.ref, args.timeout)
        archive_errors.extend(errors)
        ref_used = resolved_ref or ref_used
        if sha:
            try:
                matches, meta = search_archive(
                    args.base_url, args.tenant, args.project, args.repo, sha, args.query,
                    path_prefix=args.path_prefix, timeout=args.timeout,
                    max_files=args.max_files, max_matches_per_file=args.max_matches_per_file, context=args.context,
                )
                archive_ok = True
            except Exception as exc:
                # Archive-unavailable is benign (the recursive fallback runs), but an
                # auth failure (401/403) is fatal: re-raise so it becomes the unified
                # error contract rather than a benign fallback note.
                if is_auth_error(exc):
                    raise
                archive_errors.append(redact_secrets(f"Archive search failed: {exc}"))
    # Only fall back to the recursive traversal when the archive search did NOT
    # complete (unavailable / --no-archive / ref unresolved / raised). A clean but
    # empty archive result (archive_ok) short-circuits the fallback and stays
    # authoritative, so a recursive-fallback failure can never override it.
    if not matches and not archive_ok:
        fallback_matches, fallback_meta = recursive_search(
            args.base_url, args.tenant, args.project, args.repo, args.ref, args.query,
            path_prefix=args.path_prefix, timeout=args.timeout,
            max_files=args.max_files, max_matches_per_file=args.max_matches_per_file, context=args.context,
            max_pages=args.max_pages,
        )
        matches = fallback_matches
        meta = fallback_meta
    meta.setdefault("errors", [])
    # Benign archive/ref-resolution fallback errors are surfaced as NOTES (visible
    # but non-fatal). Adding this only when such an error actually occurred keeps
    # the normal, no-error output byte-identical.
    if archive_errors:
        meta["notes"] = list(meta.get("notes") or []) + archive_errors
    ranked = rank_matches(matches, args.limit)
    result: Dict[str, Any] = {
        "command": args.command,
        "tenant": args.tenant,
        "project": args.project,
        "repo": args.repo,
        "ref": ref_used or args.ref,
        "pathPrefix": args.path_prefix,
        "queries": list(args.query),
        "meta": meta,
        "matches": ranked,
        "matchCount": len(ranked),
    }
    # Only surface a top-level `notes` key when there is actually something to
    # report (pagination-guard truncation or archive/ref-resolution fallback
    # errors). With no notes the emitted field set is byte-identical to main:
    # {command, tenant, project, repo, ref, pathPrefix, queries, meta, matches,
    # matchCount} -- NO top-level `notes` key.
    notes = list(meta.get("notes") or [])
    if notes:
        result["notes"] = notes
    return result


def cmd_search(args: argparse.Namespace) -> int:
    emit(args_with_defaults(search_payload(args), args))
    return 0


# --------------------------------------------------------------------------
# grep: тот же поиск, но вывод нормализован под потребителя, которому нужны
# координаты, а не ранжирование. Нужен разведке `guru-design` для репозиториев,
# которых нет на диске: `rg` там работать не может, а Astra может быть не
# подключена. Читается архив одной ветки по SHA — про это ограничение вывод
# говорит сам, чтобы пустой результат не выдавали за доказанное отсутствие.
# --------------------------------------------------------------------------

# Совпадение без `path` и `line` проверить невозможно, поэтому оно и не находка.
EVIDENCE_FIELDS = ("path", "line")


def normalize_matches(payload: Dict[str, Any], context: int,
                      evidence_only: bool) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Совпадения поиска в виде `{repo, ref, path, line, text, context}`."""
    findings: List[Dict[str, Any]] = []
    dropped = 0
    for match in payload.get("matches") or []:
        path = match.get("path")
        line = match.get("line")
        if not path or not isinstance(line, int) or line < 1:
            dropped += 1
            continue
        snippet = str(match.get("snippet") or "")
        snippet_lines = snippet.splitlines()
        # Позиция строки совпадения внутри сниппета: сниппет начинается за
        # `context` строк до неё, но не раньше начала файла.
        offset = min(line - 1, context)
        text = snippet_lines[offset] if 0 <= offset < len(snippet_lines) else ""
        findings.append(
            {
                "repo": payload.get("repo"),
                "ref": payload.get("ref"),
                "path": path,
                "line": line,
                "text": text,
                "context": snippet,
                "evidence": f"{path}:{line}",
            }
        )
    warnings: List[str] = []
    if dropped:
        warnings.append(
            f"без координат отброшено совпадений: {dropped} — находка без `путь:строка` "
            "непроверяема и в вывод не идёт"
        )
    if evidence_only:
        findings = [f for f in findings if all(f.get(k) for k in EVIDENCE_FIELDS)]
    return findings, warnings


def cmd_grep(args: argparse.Namespace) -> int:
    payload = search_payload(args)
    if payload.get("needsRepository"):
        payload["warnings"] = ["репозиторий не назван — сначала выбери его из candidates"]
        emit(args_with_defaults(payload, args))
        return 1

    findings, warnings = normalize_matches(payload, args.context, args.evidence)
    notes = list(payload.get("notes") or [])
    meta = payload.get("meta") or {}
    scanned = meta.get("filesScanned") or meta.get("scannedFiles")
    if scanned and args.max_files and int(scanned) >= int(args.max_files):
        warnings.append(
            f"просмотрено файлов {scanned} при лимите {args.max_files} — область поиска "
            "исчерпана не полностью"
        )
    if not findings:
        warnings.append(
            "совпадений нет. Это НЕ доказательство отсутствия: просмотрена одна ветка "
            f"({payload.get('ref') or 'по умолчанию'}) одного репозитория "
            f"({payload.get('repo')}) в пределах лимитов"
        )
    emit(args_with_defaults(
        {
            "command": "grep",
            "ok": True,
            "tenant": payload.get("tenant"),
            "project": payload.get("project"),
            "repo": payload.get("repo"),
            "ref": payload.get("ref"),
            "queries": payload.get("queries"),
            "scope": {
                "pathPrefix": payload.get("pathPrefix"),
                "maxFiles": args.max_files,
                "evidenceOnly": bool(args.evidence),
            },
            "findings": findings,
            "findingCount": len(findings),
            "notes": notes,
            "warnings": warnings,
        },
        args,
    ))
    return 0


def add_common_args(parser: argparse.ArgumentParser) -> None:
    # The base URL is resolved in main() via base_url_for("source_control") AFTER
    # load_env_file() runs, so a SOURCE_CONTROL_BASE_URL/SC_BASE_URL set only in
    # ~/.swtr-review.env is honored (the env-override precedence is applied there,
    # not baked in at argparse time). An explicit --base-url still wins.
    parser.add_argument("--base-url", default=None,
                        help="Override SOURCE_CONTROL_BASE_URL/SC_BASE_URL (resolved after the env file loads)")
    parser.add_argument("--env-file", default="~/.swtr-review.env")
    parser.add_argument("--no-profile", action="store_true",
                        help="Do not apply defaults from the user profile (%s): tenant and project of "
                             "SourceControl, the repository list in the output. Behaves as if there were "
                             "no profile." % PROFILE_PATH_HINT)
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds (default 30)")


def add_project_args(parser: argparse.ArgumentParser) -> None:
    # Не required: значение может прийти из окружения или профиля. Отсутствие
    # всюду отдаётся из apply_sc_project_defaults() через сабпарсер команды —
    # прежними словами argparse и его кодом 2.
    parser.add_argument("--tenant", help="SourceControl tenant identifier/UUID")
    parser.add_argument("--project", help="SourceControl project identifier/name")
    parser.set_defaults(subparser=parser)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SourceControl repository listing and code search. Prints JSON to stdout.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_repos = sub.add_parser("repos", help="List repositories in project")
    add_common_args(p_repos)
    add_project_args(p_repos)
    p_repos.add_argument("--query")
    p_repos.add_argument("--page", type=int, default=1,
                         help="Page number, 1-based (SourceControl API-native; SC counts pages from 1)")
    p_repos.add_argument("--limit", type=int, default=30, help="Page size (default 30)")

    p_branches = sub.add_parser("branches", help="List branches in repository")
    add_common_args(p_branches)
    add_project_args(p_branches)
    p_branches.add_argument("--repo", required=True)
    p_branches.add_argument("--query")
    p_branches.add_argument("--page", type=int, default=1,
                            help="Page number, 1-based (SourceControl API-native)")
    p_branches.add_argument("--limit", type=int, default=30, help="Page size (default 30)")

    p_file = sub.add_parser("file", help="Fetch one file by path from repository")
    add_common_args(p_file)
    add_project_args(p_file)
    p_file.add_argument("--repo", required=True)
    p_file.add_argument("--ref")
    p_file.add_argument("--filepath", required=True)

    for name, help_text in (
        ("search", "Search code in repository or list repository candidates"),
        ("grep", "Same search, output normalized to path:line findings for downstream tools"),
    ):
        p = sub.add_parser(name, help=help_text)
        add_common_args(p)
        add_project_args(p)
        p.add_argument("--repo", help="Repository name. If omitted, repo candidates are listed first.")
        p.add_argument("--repo-query", help="Query for repository candidates when --repo is omitted")
        p.add_argument("--repo-limit", type=int, default=20)
        p.add_argument("--ref", help="Branch/tag/SHA to inspect. If omitted, the repository default branch is used when available.")
        p.add_argument("--query", action="append", required=True, help="Function/feature/code text to search; can be repeated")
        p.add_argument("--path-prefix", default="", help="Optional repository path prefix to limit search")
        p.add_argument("--limit", type=int, default=30, help="Maximum returned matches")
        p.add_argument("--max-files", type=int, default=5000, help="Safety cap for scanned files")
        p.add_argument("--max-pages", type=int, default=50,
                       help="Safety cap on directory-listing pages per directory (contents/raw fallback); "
                            "if a directory exceeds it the listing is truncated and a note is added to output notes")
        p.add_argument("--max-matches-per-file", type=int, default=5)
        p.add_argument("--context", type=int, default=2, help="Context lines around each match")
        p.add_argument("--no-archive", action="store_true", help="Skip archive strategy and use contents/raw API only")
        if name == "grep":
            p.add_argument("--evidence", action="store_true",
                           help="Keep only findings that carry path:line (grounding rule)")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    load_env_file(Path(args.env_file).expanduser())
    # Resolve the base URL AFTER the env file is loaded so a SOURCE_CONTROL_BASE_URL
    # (or SC_BASE_URL) set only in ~/.swtr-review.env is honored (explicit --base-url
    # still wins). base_url_for() applies the SOURCE_CONTROL_BASE_URL -> SC_BASE_URL
    # -> default precedence.
    if getattr(args, "base_url", None) in (None, ""):
        args.base_url = base_url_for("source_control")
    # Профиль — одной точкой и ПОСЛЕ env-файла: переменные окружения стоят
    # раньше профиля и к этому моменту уже должны быть видны. --no-profile не
    # читает файл вовсе.
    attach_profile(args)
    apply_sc_project_defaults(args)
    try:
        if args.command == "repos":
            return cmd_repos(args)
        if args.command == "branches":
            return cmd_branches(args)
        if args.command == "file":
            return cmd_file(args)
        if args.command == "search":
            return cmd_search(args)
        if args.command == "grep":
            return cmd_grep(args)
        raise SystemExit(f"Unsupported command: {args.command}")
    except RuntimeError as exc:
        return emit_error(args.command, exc, **args_error_context(args))


if __name__ == "__main__":
    raise SystemExit(main())
