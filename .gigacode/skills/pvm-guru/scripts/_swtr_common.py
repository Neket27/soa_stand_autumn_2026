#!/usr/bin/env python3
"""Shared HTTP/auth helpers for SWTR Task Tracker and SourceControl scripts.

The helpers intentionally use only Python standard library modules so the skill
works in restricted corporate environments. They never print or persist secret
values: request metadata must be logged through safe_headers_for_log().
"""

from __future__ import annotations

import argparse
import base64
import copy
import html
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Tuple

def force_utf8_stdio() -> None:
    """Make stdout/stderr UTF-8 so Windows cannot kill a script on its own output.

    On Windows a *piped* stdout (exactly how an agent runs these scripts) is
    encoded with the ANSI code page -- cp1251 on a Russian install -- not UTF-8.
    Any character outside it raises UnicodeEncodeError mid-print, so the script
    dies before saying anything useful, and the traceback blames the wrong thing:
    users read "encoding error" as a problem with ~/.swtr-review.env. Cyrillic
    survives cp1251, so the failures came from single decorative characters.

    errors="replace" is the second half: after this call an unencodable character
    can only ever degrade to "?" -- never abort a diagnostic run.

    Called on import because every script in this directory is a CLI entry point;
    doing it here is the only version that cannot be forgotten in a new script.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # StringIO under test, or a pre-3.7 runtime
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass  # detached or already-closed stream: nothing to fix, never fatal


force_utf8_stdio()

SWTR_DEFAULT_BASE_URL = "https://portal.works.prod.sbt/swtr"
SOURCE_CONTROL_DEFAULT_BASE_URL = "https://portal.works.prod.sbt/ssd/tools/sc/api/v3"
DEFAULT_SWTR_COOKIE = ""
INSECURE_TLS_VALUES = {"1", "true", "yes", "on"}

SWTR_SECRET_ENV_NAMES = (
    "SWTR_AUTHORIZATION",
    "TASKTRACKER_AUTHORIZATION",
    "SWTR_BASIC_AUTH",
    "SWTR_BEARER_TOKEN",
    "SWTR_TOKEN",
    "TASKTRACKER_TOKEN",
    "SWTR_COOKIE",
)
SOURCE_CONTROL_SECRET_ENV_NAMES = (
    "SOURCE_CONTROL_AUTHORIZATION",
    "SC_AUTHORIZATION",
    "SOURCE_CONTROL_BASIC_AUTH",
    "SC_BASIC_AUTH",
    "SOURCE_CONTROL_BEARER_TOKEN",
    "SC_BEARER_TOKEN",
    "SOURCE_CONTROL_TOKEN",
    "SC_TOKEN",
    "SOURCE_CONTROL_COOKIE",
    "SC_COOKIE",
)

# sberworks Data Center backends (second wiki + second tracker, different host).
# Kept isolated from the portal SWTR/SourceControl systems above on purpose.
CONFLUENCE_DEFAULT_BASE_URL = "https://sberworks.ru/wiki"
JIRA_DEFAULT_BASE_URL = "https://sberworks.ru/jira"

CONFLUENCE_SECRET_ENV_NAMES = (
    "CONFLUENCE_AUTHORIZATION",
    "CONFLUENCE_BEARER_TOKEN",
    "CONFLUENCE_TOKEN",
    "CONFLUENCE_BASIC_AUTH",
    "CONFLUENCE_COOKIE",
    "SBERWORKS_TOKEN",  # session-compat alias (PAT used as Bearer)
)
JIRA_SECRET_ENV_NAMES = (
    "JIRA_AUTHORIZATION",
    "JIRA_BEARER_TOKEN",
    "JIRA_TOKEN",
    "JIRA_BASIC_AUTH",
    "JIRA_COOKIE",
)


def _read_env_text(path: Path) -> str:
    """Decode an env file, tolerating the encodings notepad produces.

    notepad's "Unicode" save is UTF-16 (with a BOM); its UTF-8 save may carry
    a BOM. Reading those as plain UTF-8 corrupts the first key (a leading BOM
    becomes part of the name) or crashes on embedded null bytes, so detect the
    BOM and decode accordingly. utf-8-sig also handles BOM-less UTF-8.
    """
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def _unquote_env_value(value: str) -> str:
    """Strip exactly one matching pair of surrounding quotes.

    The inner text is returned verbatim, so a password keeps every character it
    needs: trailing spaces, ``#``, ``%``, ``*``, ``}`` or even a literal quote.
    The previous ``value.strip('"').strip("'")`` was greedy — it ate trailing
    quote characters that belonged to the password. Unquoted values are returned
    as-is (the caller already trimmed surrounding whitespace); an inline ``#`` is
    NOT treated as a comment because it is a legal password character.
    """
    if len(value) >= 2 and value[0] in ('"', "'"):
        quote = value[0]
        end = value.rfind(quote)
        if end > 0:
            return value[1:end]
        return value[1:]  # unterminated opening quote: drop just the quote
    return value


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs from a local env file without overriding env vars.

    Tolerates the files users actually produce: an optional ``export`` prefix,
    CRLF line endings, Windows/notepad encodings (UTF-8 BOM, UTF-16), and
    surrounding quotes around the value.
    """
    if not path.exists() or not path.is_file():
        return
    for raw_line in _read_env_text(path).splitlines():
        line = raw_line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = _unquote_env_value(value.strip())
        if key and key not in os.environ:
            os.environ[key] = value


def _first_env(names: Iterable[str]) -> Optional[str]:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def _reuse_swtr_for_source_control() -> bool:
    """Opt-in: only reuse SWTR auth for SourceControl when explicitly enabled.

    Some installations accept the same SSO token for both APIs, but silently
    sending SWTR credentials to the SourceControl host causes 401/403 storms
    and leaks a secret to a different system. Require an explicit opt-in.
    """
    raw = (_first_env(("SOURCE_CONTROL_REUSE_SWTR_AUTH", "SC_REUSE_SWTR_AUTH")) or "").strip().lower()
    return raw in INSECURE_TLS_VALUES


def _bearer_first_auth(
    authz_names: Iterable[str],
    bearer_names: Iterable[str],
    token_names: Iterable[str],
    basic_names: Iterable[str],
) -> Optional[str]:
    """sberworks DC auth: a PAT used as Bearer is primary; Basic is rejected
    server-side. Order: explicit Authorization -> Bearer token -> raw token (sent
    as Bearer, since a DC PAT is a Bearer credential) -> Basic (last resort).
    """
    direct = _first_env(authz_names)
    if direct:
        return direct
    bearer = _first_env(bearer_names)
    if bearer:
        return "Bearer " + bearer
    token = _first_env(token_names)
    if token:
        return "Bearer " + token  # raw DC PAT is used as a Bearer token
    basic = _first_env(basic_names)
    if basic:
        return "Basic " + base64.b64encode(basic.encode("utf-8")).decode("ascii")
    return None


def build_authorization(system: str) -> Optional[str]:
    """Return Authorization header value per system.

    task_tracker/source_control are Basic-first (corporate portal). confluence/jira
    (sberworks DC) are Bearer-first -- Basic is rejected server-side -- so a raw PAT
    is sent as a Bearer token. Each system reads only its own secret env (isolation).
    """
    if system == "confluence":
        return _bearer_first_auth(
            ("CONFLUENCE_AUTHORIZATION",),
            ("CONFLUENCE_BEARER_TOKEN",),
            ("CONFLUENCE_TOKEN", "SBERWORKS_TOKEN"),
            ("CONFLUENCE_BASIC_AUTH",),
        )
    if system == "jira":
        return _bearer_first_auth(
            ("JIRA_AUTHORIZATION",),
            ("JIRA_BEARER_TOKEN",),
            ("JIRA_TOKEN",),
            ("JIRA_BASIC_AUTH",),
        )
    if system == "source_control":
        basic = _first_env(("SOURCE_CONTROL_BASIC_AUTH", "SC_BASIC_AUTH"))
        if basic:
            return "Basic " + base64.b64encode(basic.encode("utf-8")).decode("ascii")
        direct = _first_env(("SOURCE_CONTROL_AUTHORIZATION", "SC_AUTHORIZATION"))
        if direct:
            return direct
        bearer = _first_env(("SOURCE_CONTROL_BEARER_TOKEN", "SC_BEARER_TOKEN"))
        if bearer:
            return "Bearer " + bearer
        token = _first_env(("SOURCE_CONTROL_TOKEN", "SC_TOKEN"))
        if token:
            # Same portal-PAT reasoning as the task_tracker branch below.
            if _has_auth_scheme(token):
                return token
            return "Bearer " + token
        # Cross-system reuse is opt-in only (see _reuse_swtr_for_source_control).
        if _reuse_swtr_for_source_control():
            return build_authorization("task_tracker")
        return None

    basic = _first_env(("SWTR_BASIC_AUTH",))
    if basic:
        return "Basic " + base64.b64encode(basic.encode("utf-8")).decode("ascii")
    direct = _first_env(("SWTR_AUTHORIZATION", "TASKTRACKER_AUTHORIZATION"))
    if direct:
        return direct
    bearer = _first_env(("SWTR_BEARER_TOKEN",))
    if bearer:
        return "Bearer " + bearer
    token = _first_env(("SWTR_TOKEN", "TASKTRACKER_TOKEN"))
    if token:
        # A bare SWTR/TaskTracker token is a portal PAT (issued at
        # /ssd/privileges) and belongs behind the Bearer scheme, exactly as the
        # confluence/jira branches treat theirs. Sending it raw produced
        # "Authorization: <token>", which every backend answers with 401 and no
        # explanation. A value that already carries a scheme is passed through so
        # anyone who stored a full header value keeps working.
        if _has_auth_scheme(token):
            return token
        return "Bearer " + token
    return None


def base_url_for(system: str) -> str:
    """Resolve the base URL for a system from its env override or its default."""
    if system == "confluence":
        return os.environ.get("CONFLUENCE_BASE_URL", CONFLUENCE_DEFAULT_BASE_URL).rstrip("/")
    if system == "jira":
        return os.environ.get("JIRA_BASE_URL", JIRA_DEFAULT_BASE_URL).rstrip("/")
    if system == "source_control":
        return os.environ.get(
            "SOURCE_CONTROL_BASE_URL",
            os.environ.get("SC_BASE_URL", SOURCE_CONTROL_DEFAULT_BASE_URL),
        ).rstrip("/")
    return os.environ.get("SWTR_BASE_URL", SWTR_DEFAULT_BASE_URL).rstrip("/")


def _has_auth_scheme(value: str) -> bool:
    """True when the value already starts with an HTTP auth scheme word.

    Lets a raw ``*_TOKEN`` be wrapped in ``Bearer `` without turning a value that
    someone stored as a complete header ("Bearer abc") into "Bearer Bearer abc".
    """
    lowered = value.lstrip().lower()
    return lowered.startswith("bearer ") or lowered.startswith("basic ")


def _bare_auth_token(value: str) -> Optional[str]:
    """Return the token/base64 payload after a ``Bearer ``/``Basic `` prefix.

    A user may set ``*_AUTHORIZATION`` to a full header value ("Bearer abc123",
    "Basic dXNlcjpwdw=="). If a backend echoes only the bare token/payload
    (without the scheme word) in an error body, redacting just the full header
    value would miss it, so we register the remainder too. Trivially short
    remainders (< 4 chars) are skipped: masking a 1-3 char string risks
    corrupting unrelated output, and such a value is not a real credential.
    """
    lowered = value.lower()
    for scheme in ("bearer ", "basic "):
        if lowered.startswith(scheme):
            remainder = value[len(scheme):].strip()
            if len(remainder) >= 4:
                return remainder
            return None
    return None


def _decoded_basic_secrets(value: str) -> List[str]:
    """For a ``Basic <base64>`` value, return the decoded ``user:password`` and
    the bare password (the part after the first ':').

    A backend that echoes a *decoded* credential in an error body would slip past
    redaction if only the base64 form were registered. Best-effort: an undecodable
    payload is skipped, and trivially short remainders (< 4 chars) are ignored so a
    1-3 char string cannot corrupt unrelated output.
    """
    if not value.lower().startswith("basic "):
        return []
    payload = value[len("basic "):].strip()
    try:
        decoded = base64.b64decode(payload).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - malformed base64 is simply left un-decoded
        return []
    out: List[str] = []
    if len(decoded) >= 4:
        out.append(decoded)
    if ":" in decoded:
        password = decoded.split(":", 1)[1]
        if len(password) >= 4:
            out.append(password)
    return out


# Cookie values that are switches, not secrets. A flag cookie carries no session
# and must never be registered: `api_swtr_as21=true` is the documented companion of
# the portal Bearer token (references/backends-and-auth.md), its value is 4 chars --
# long enough for the length rule below -- and registering it makes redact_secrets()
# rewrite every `true` in an echoed JSON body into "redacted".
_NON_SECRET_COOKIE_VALUES = frozenset({"true", "false", "yes", "no", "on", "off"})


def _cookie_value_secrets(raw_cookie: str) -> List[str]:
    """Return each cookie VALUE (the part after '=' in every ';'-separated pair)
    at least 4 chars long, so a partial echo of a single cookie pair is redacted
    even when the full cookie string does not appear verbatim in the output.

    Boolean-flag values are skipped: they are not secrets, and they collide with
    ordinary words in the output the redaction is meant to protect.
    """
    out: List[str] = []
    for pair in raw_cookie.split(";"):
        pair = pair.strip()
        if "=" in pair:
            cookie_value = pair.split("=", 1)[1].strip()
            if len(cookie_value) >= 4 and cookie_value.lower() not in _NON_SECRET_COOKIE_VALUES:
                out.append(cookie_value)
    return out


def collect_secret_values() -> List[str]:
    values: List[str] = []
    for name in (
        SWTR_SECRET_ENV_NAMES
        + SOURCE_CONTROL_SECRET_ENV_NAMES
        + CONFLUENCE_SECRET_ENV_NAMES
        + JIRA_SECRET_ENV_NAMES
    ):
        value = os.environ.get(name, "").strip()
        if value:
            values.append(value)
    for raw_name in (
        "SWTR_BASIC_AUTH",
        "SOURCE_CONTROL_BASIC_AUTH",
        "SC_BASIC_AUTH",
        "CONFLUENCE_BASIC_AUTH",
        "JIRA_BASIC_AUTH",
    ):
        raw = os.environ.get(raw_name, "").strip()
        if raw:
            values.append("Basic " + base64.b64encode(raw.encode("utf-8")).decode("ascii"))
    for raw_name in (
        "SWTR_BEARER_TOKEN",
        "SOURCE_CONTROL_BEARER_TOKEN",
        "SC_BEARER_TOKEN",
        "CONFLUENCE_BEARER_TOKEN",
        "CONFLUENCE_TOKEN",
        "SBERWORKS_TOKEN",
        "JIRA_BEARER_TOKEN",
        "JIRA_TOKEN",
    ):
        raw = os.environ.get(raw_name, "").strip()
        if raw:
            values.append("Bearer " + raw)
    # Also register the BARE token/base64 payload for any collected value that
    # carries a Bearer/Basic scheme prefix. The full header value stays in the
    # list too; sorting longest-first means the full form is redacted before the
    # bare form, and a bare token echoed on its own is still caught.
    for value in list(values):
        bare = _bare_auth_token(value)
        if bare:
            values.append(bare)
    # Also register the DECODED "user:password" (and the bare password after the
    # first ':') for every Basic value. A backend that echoes a decoded credential
    # -- not its base64 form -- would otherwise leak past redact_secrets().
    for value in list(values):
        values.extend(_decoded_basic_secrets(value))
    # Register each cookie VALUE. The FULL cookie string is already registered
    # above (via the *_COOKIE names in the *_SECRET_ENV_NAMES tuples); this adds
    # the individual pair values so a partial echo of one cookie is still masked.
    for cookie_name in ("SWTR_COOKIE", "SOURCE_CONTROL_COOKIE", "SC_COOKIE", "CONFLUENCE_COOKIE", "JIRA_COOKIE"):
        raw = os.environ.get(cookie_name, "").strip()
        if raw:
            values.extend(_cookie_value_secrets(raw))
    return sorted({value for value in values if value}, key=len, reverse=True)


def redact_secrets(text: str) -> str:
    redacted = text
    for secret in collect_secret_values():
        redacted = redacted.replace(secret, "redacted")
    return redacted


def emit_error(command: str, error: Any, **extra: Any) -> int:
    """Print the unified CLI error contract to stderr and return exit code 1.

    Shape: ``{"ok": false, "command": <command>, "error": <redacted>, ...extra}``.
    The error text ALWAYS passes through redact_secrets() so a token echoed back
    in an upstream error body (a 401/403 response) can never leak. Caller-supplied
    ``extra`` fields (e.g. ``unitCode``) are preserved after the standard keys and
    cannot override ok/command/error. Every CLI tool routes its backend-error path
    through this helper so the contract is identical across the skill.
    """
    payload: Dict[str, Any] = {
        "ok": False,
        "command": command,
        "error": redact_secrets(str(error)),
    }
    for key, value in extra.items():
        if key not in ("ok", "command", "error"):
            payload[key] = value
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
    return 1


def _cookie_parts(raw_cookie: str) -> List[str]:
    return [part.strip() for part in raw_cookie.split(";") if part.strip()]


def build_cookie(system: str) -> str:
    if system == "source_control":
        parts = _cookie_parts(_first_env(("SOURCE_CONTROL_COOKIE", "SC_COOKIE")) or "")
        return "; ".join(parts)
    if system == "confluence":
        return "; ".join(_cookie_parts(_first_env(("CONFLUENCE_COOKIE",)) or ""))
    if system == "jira":
        return "; ".join(_cookie_parts(_first_env(("JIRA_COOKIE",)) or ""))

    parts = _cookie_parts(os.environ.get("SWTR_COOKIE", ""))
    # Only prepend the default cookie when one is configured; inserting an empty
    # DEFAULT_SWTR_COOKIE would produce a malformed leading "; " segment.
    if DEFAULT_SWTR_COOKIE and not any(
        part.split("=", 1)[0].strip().lower() == "api_swtr_as21" for part in parts
    ):
        parts.insert(0, DEFAULT_SWTR_COOKIE)
    return "; ".join(parts)


def build_ssl_context(system: str = "task_tracker") -> Optional[ssl.SSLContext]:
    names = ["SWTR_INSECURE_SKIP_TLS_VERIFY"]
    if system == "source_control":
        names = ["SOURCE_CONTROL_INSECURE_SKIP_TLS_VERIFY", "SC_INSECURE_SKIP_TLS_VERIFY", "SWTR_INSECURE_SKIP_TLS_VERIFY"]
    elif system == "confluence":
        names = ["CONFLUENCE_INSECURE_SKIP_TLS_VERIFY"]
    elif system == "jira":
        names = ["JIRA_INSECURE_SKIP_TLS_VERIFY"]
    raw_value = (_first_env(names) or "").strip().lower()
    if raw_value not in INSECURE_TLS_VALUES:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def build_headers(system: str, require_auth: bool = True, accept: str = "application/json") -> Dict[str, str]:
    headers = {
        "Accept": accept,
        "User-Agent": "swtr-prd-architecture-review/2.0",
    }
    # Content-Type is intentionally NOT set here. request_http() adds it only
    # when a request actually carries a body, so GET requests stay clean
    # (sending Content-Type on a bodyless GET makes some gateways reject it).
    cookie = build_cookie(system)
    if cookie:
        headers["Cookie"] = cookie
    authorization = build_authorization(system)
    if authorization:
        headers["Authorization"] = authorization
    elif require_auth:
        # Missing/misconfigured auth is raised as RuntimeError (not SystemExit) so
        # each CLI main's `except RuntimeError -> emit_error` prints the unified
        # {"ok": false, "command", "error"} contract (stderr, exit 1). The message
        # text is unchanged — users depend on the env-var hints. These messages
        # list env-var NAMES only, never a secret value.
        if system == "source_control":
            raise RuntimeError(
                "SourceControl authorization is not configured. Set SOURCE_CONTROL_BASIC_AUTH, "
                "SC_BASIC_AUTH, SOURCE_CONTROL_AUTHORIZATION, SC_AUTHORIZATION, "
                "SOURCE_CONTROL_BEARER_TOKEN, SC_BEARER_TOKEN, SOURCE_CONTROL_TOKEN, or SC_TOKEN. "
                "To deliberately reuse SWTR credentials (shared SSO only), set "
                "SOURCE_CONTROL_REUSE_SWTR_AUTH=1."
            )
        if system == "confluence":
            raise RuntimeError(
                "Confluence authorization is not configured. Set CONFLUENCE_BEARER_TOKEN "
                "(sberworks PAT, sent as Bearer), CONFLUENCE_TOKEN, SBERWORKS_TOKEN, or "
                "CONFLUENCE_AUTHORIZATION. sberworks rejects Basic auth — use a Data Center PAT."
            )
        if system == "jira":
            raise RuntimeError(
                "Jira authorization is not configured. Set JIRA_BEARER_TOKEN (sberworks PAT, "
                "sent as Bearer), JIRA_TOKEN, or JIRA_AUTHORIZATION. sberworks rejects Basic auth "
                "— use a Data Center PAT. The Jira PAT differs from the Confluence PAT."
            )
        raise RuntimeError(
            "SWTR authorization is not configured. Set SWTR_BASIC_AUTH, SWTR_AUTHORIZATION, "
            "TASKTRACKER_AUTHORIZATION, SWTR_BEARER_TOKEN, SWTR_TOKEN, or TASKTRACKER_TOKEN locally."
        )
    return headers


def safe_headers_for_log(headers: Dict[str, str]) -> Dict[str, str]:
    safe = dict(headers)
    if "Authorization" in safe:
        safe["Authorization"] = "redacted"
    if "Cookie" in safe:
        safe["Cookie"] = "redacted"
    return safe


# Response headers that carry a session/credential and must never reach stdout
# or a log. Set-Cookie in particular hands out a live session if it leaks.
SENSITIVE_RESPONSE_HEADERS = frozenset({
    "set-cookie",
    "set-cookie2",
    "cookie",
    "authorization",
    "proxy-authorization",
    "www-authenticate",
    "x-auth-token",
    "x-amz-security-token",
})


def safe_response_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """Return a copy of HTTP *response* headers with sensitive ones redacted.

    Matching is case-insensitive (servers vary the casing of Set-Cookie etc.).
    Every place that emits response headers in --execute/write output must route
    them through here so a backend Set-Cookie/session/auth-challenge header can
    never leak into stdout or a captured log. The input dict is not mutated.
    """
    safe: Dict[str, str] = {}
    for name, value in headers.items():
        safe[name] = "<redacted>" if name.lower() in SENSITIVE_RESPONSE_HEADERS else value
    return safe


RETRIABLE_STATUS = {429, 502, 503, 504}
DEFAULT_MAX_RETRIES = 3
MAX_RETRY_SLEEP = 30.0


def _retry_after_seconds(value: Optional[str]) -> Optional[float]:
    """Parse a Retry-After header value (delta-seconds form only)."""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        return None
    if seconds < 0:
        return None
    return min(seconds, MAX_RETRY_SLEEP)


def request_http(
    method: str,
    url: str,
    headers: Dict[str, str],
    *,
    body: Optional[Any] = None,
    timeout: float = 30.0,
    system: str = "task_tracker",
    binary: bool = False,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> Tuple[Any, Dict[str, str], int]:
    data: Optional[bytes] = None
    request_headers = dict(headers)
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url=url, data=data, headers=request_headers, method=method.upper())
    ctx = build_ssl_context(system)

    attempt = 0
    while True:
        try:
            if ctx is None:
                response = urllib.request.urlopen(request, timeout=timeout)
            else:
                response = urllib.request.urlopen(request, timeout=timeout, context=ctx)
            with response:
                status = response.status
                response_headers = {k: v for k, v in response.headers.items()}
                payload = response.read()
            break
        except urllib.error.HTTPError as exc:
            # Retry transient throttling/gateway errors with backoff.
            if exc.code in RETRIABLE_STATUS and attempt < max_retries:
                retry_after = _retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None)
                sleep_for = retry_after if retry_after is not None else min(2.0 ** attempt, MAX_RETRY_SLEEP)
                attempt += 1
                time.sleep(sleep_for)
                continue
            error_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(redact_secrets(f"HTTP {exc.code} for {url}: {error_body[:2000]}")) from exc
        except (urllib.error.URLError, socket.timeout) as exc:
            # Retry connect/read timeouts and transient network errors.
            if attempt < max_retries:
                attempt += 1
                time.sleep(min(2.0 ** (attempt - 1), MAX_RETRY_SLEEP))
                continue
            raise RuntimeError(redact_secrets(f"Network error for {url}: {exc}")) from exc

    if binary:
        return payload, response_headers, status
    if not payload.strip():
        return None, response_headers, status
    charset = "utf-8"
    content_type = response_headers.get("Content-Type") or response_headers.get("content-type") or ""
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            charset = part.split("=", 1)[1].strip()
    text = payload.decode(charset, errors="replace")
    try:
        return json.loads(text), response_headers, status
    except json.JSONDecodeError:
        return {"rawText": text}, response_headers, status


def is_auth_error(exc: BaseException) -> bool:
    """True when ``exc`` (or anything in its ``__cause__`` chain) is an HTTP 401/403.

    request_http() raises ``RuntimeError(f"HTTP {code} for {url}: ...")`` *from* the
    underlying ``urllib.error.HTTPError``, so a caught RuntimeError carries that
    HTTPError as its ``__cause__`` with a ``.code`` attribute. The best-effort
    multi-item commands (search/similar/tms) collect BENIGN per-item errors
    (404 / timeout / network) and still exit 0, but an authentication failure is
    never a per-item problem -- every request to that backend will fail alike -- so
    each catch site re-raises when this returns True, turning a 401/403 into the
    unified error contract (exit 1) instead of a silently swallowed empty result.

    Walking the cause chain (not only string-matching) keeps the check precise; the
    ``"HTTP 401 "``/``"HTTP 403 "`` message-prefix fallback covers a RuntimeError
    re-wrapped without preserving ``__cause__``. The ``seen`` set guards a cyclic
    chain.
    """
    seen: set = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, urllib.error.HTTPError) and getattr(current, "code", None) in (401, 403):
            return True
        if isinstance(current, RuntimeError):
            message = str(current)
            if message.startswith("HTTP 401 ") or message.startswith("HTTP 403 "):
                return True
        current = getattr(current, "__cause__", None)
    return False


# Markers of the portal's IAM gateway (SynGX + ``direct-auth``) authentication
# page. The gateway sits in front of portal.works.prod.sbt and answers a Basic
# credential it cannot authenticate with an HTML page under HTTP **403** -- the
# request never reaches the backend, so there is no application verdict in it.
GATEWAY_AUTH_FAILURE_MARKERS = (
    "ошибка при аутентификации",
    "error auth by direct-auth",
    "auth platform",
)


def http_error_details(exc: urllib.error.HTTPError) -> Tuple[str, str]:
    """``(body_text, content_type)`` of an HTTPError, never raising.

    The body of an error response is read at most once by urllib, so callers that
    need it must take it here rather than re-reading later.
    """
    text = ""
    try:
        raw = exc.read()
    except Exception:
        raw = b""
    if raw:
        try:
            text = raw.decode("utf-8", "replace")
        except Exception:
            text = ""
    content_type = ""
    try:
        headers = getattr(exc, "headers", None)
        if headers is not None:
            content_type = headers.get("Content-Type") or headers.get("content-type") or ""
    except Exception:
        content_type = ""
    return text, content_type


def is_gateway_auth_failure(status, content_type, body_text) -> bool:
    """True when a 401/403 came from the portal's login gateway, not the application.

    Why this distinction is load-bearing: a stale domain password makes the gateway
    answer **403**, and a bare 403 used to be read as "the backend refuses
    Basic-with-password, get a Personal Access Token". That advice sends a user
    whose password simply expired chasing a token they do not need. The portal does
    accept Basic-with-password -- a current credential returns 200 -- so a 403 that
    never reached the backend means the credential failed to authenticate.

    Two signals, either one is enough:
    - the gateway page's own text (see GATEWAY_AUTH_FAILURE_MARKERS);
    - an HTML body on 401/403 from a JSON API. Every endpoint this skill talks to
      answers ``application/json`` (SWTR errors included), so HTML there is the
      gateway's page whatever it happens to say -- the wording is not our contract.
    """
    if status not in (401, 403):
        return False
    low = (body_text or "").lower()
    if any(marker in low for marker in GATEWAY_AUTH_FAILURE_MARKERS):
        return True
    return "text/html" in (content_type or "").lower()


def quote_path_part(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


# Единственная рабочая форма веб-ссылки на юнит. Ни один ответ API ссылки не
# отдаёт, поэтому собрать её больше неоткуда — и раньше её досочиняли по образцу
# других трекеров (`/swtr/task/<КОД>`, `/swtr/browse/<КОД>`), уводя человека в
# никуда. Форма голая: `tenant` и `space` привязали бы ссылку к контексту.
UNIT_WEB_URL_TEMPLATE = "{base}/units/all/unit/{code}"


def unit_url(base_url: str, code: Optional[str]) -> Optional[str]:
    """Веб-ссылка на карточку юнита. Без кода — None, а не битый адрес."""
    if not code:
        return None
    return UNIT_WEB_URL_TEMPLATE.format(base=str(base_url).rstrip("/"),
                                        code=quote_path_part(code))


def unit_urls_in(base_url: str, payload: Any) -> Dict[str, str]:
    """Карта «код юнита → веб-ссылка» по сырому ответу трекера.

    Ответ не трогаем: он должен остаться тем, что прислал сервер. Коды берём
    по форме `PVM-175194`, а не по всякому полю `code`: у сюита, пространства и
    атрибута оно тоже есть, и ссылка на `task` или `PVM` — мусор.
    """
    found: Dict[str, str] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "code" and isinstance(value, str) and UNIT_CODE_RE.fullmatch(value):
                    found.setdefault(value, unit_url(base_url, value))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


def quote_repo_path(value: str) -> str:
    return urllib.parse.quote(str(value).strip("/"), safe="/")


# The user profile: the defaults (login, spaces, tenant, ...) the scripts
# substitute where a value is otherwise required by a flag. It lives next to the
# installer manifest, because that
# directory already belongs to the family. A profile never holds a secret --
# secrets live only in ~/.swtr-review.env, and collect_secret_values() does not
# read the profile. The single writer is guru_profile.py; here there is only
# reading, in one place for every CLI of the skill.
PROFILE_REL = (".config", "pvm-guru", "profile.json")
PROFILE_SCHEMA_VERSION = 1
PROFILE_SECTIONS = ("task_tracker", "source_control", "sberworks")
# The path as the user reads it, for the hint every CLI prints next to its
# profile flags. One constant, not five copies: PROFILE_REL is the machine form.
PROFILE_PATH_HINT = "~/.config/pvm-guru/profile.json"
# Keys the family stamps itself; a caller may not set them.
PROFILE_MANAGED_KEYS = ("version", "updated_at")

# Closed list of role codes (REQ-002). The Russian labels are what the dialog
# shows; the code is what the file stores.
PROFILE_ROLE_CODES = ("manager", "analyst", "architect", "developer", "tester", "writer", "other")
PROFILE_ROLE_LABELS = {
    "manager": "менеджер / владелец продукта / тимлид",
    "analyst": "аналитик",
    "architect": "архитектор",
    "developer": "разработчик",
    "tester": "тестировщик",
    "writer": "техписатель",
    "other": "другое или несколько сразу",
}

PROFILE_SPACE_CODE_RULE = "код пространства — заглавные латинские буквы и цифры, от 2 до 20 символов"
_SPACE_CODE_RE = re.compile(r"^[A-Z0-9]{2,20}$")
_LOGIN_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SUIT_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_SC_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CONFLUENCE_KEY_RE = re.compile(r"^[A-Z0-9]{1,32}$")
PROFILE_MAX_LIST_ITEMS = 20


def _profile_rule_re(pattern, reason: str):
    def check(value: str) -> Optional[str]:
        return None if pattern.match(value) else reason
    return check


def _profile_rule_role(value: str) -> Optional[str]:
    if value in PROFILE_ROLE_CODES:
        return None
    return "код роли — один из: %s" % ", ".join(
        "%s (%s)" % (code, PROFILE_ROLE_LABELS[code]) for code in PROFILE_ROLE_CODES)


def _profile_rule_text_64(value: str) -> Optional[str]:
    return None if len(value) <= 64 else "непустая строка до 64 символов"


def profile_schema() -> Dict[str, Dict[str, Any]]:
    """Known keys of schema version 1: type, normalization and the check rule.

    ``type`` is ``str`` or ``list`` (of strings); ``upper`` says the value is a
    code that is upper-cased before the check; ``lower`` does the same for the
    closed role list; ``check(value)`` returns a reason or None for one string
    (every list item is checked with the same rule); ``max_items`` caps a list.
    Unknown keys are not in the schema on purpose: they are preserved on rewrite
    and never applied by the operations (REQ-002).

    The schema lives in the shared layer, not in the writer, because the reader
    enforces it too: load_profile() rejects a hand-edited file whose values do
    not pass (REQ-001), so every CLI behaves as if there were no profile instead
    of pushing garbage into a TQL query or an endpoint path.
    """
    return {
        "login": {"type": "str", "check": _profile_rule_re(
            _LOGIN_RE, "логин — латиница, цифры, точки, дефис и подчёркивание, до 64 символов")},
        "role": {"type": "str", "lower": True, "check": _profile_rule_role},
        "task_tracker.primary_space": {"type": "str", "upper": True,
                                       "check": _profile_rule_re(_SPACE_CODE_RE, PROFILE_SPACE_CODE_RULE)},
        "task_tracker.spaces": {"type": "list", "upper": True, "max_items": PROFILE_MAX_LIST_ITEMS,
                                "check": _profile_rule_re(_SPACE_CODE_RE, PROFILE_SPACE_CODE_RULE)},
        "task_tracker.default_suit": {"type": "str", "check": _profile_rule_re(
            _SUIT_RE, "код типа задачи — латиница, цифры, подчёркивание, до 32 символов")},
        "task_tracker.product": {"type": "str", "check": _profile_rule_text_64},
        "task_tracker.components": {"type": "list", "max_items": PROFILE_MAX_LIST_ITEMS,
                                    "check": _profile_rule_text_64},
        "source_control.tenant": {"type": "str", "check": _profile_rule_re(
            _UUID_RE, "tenant SourceControl — идентификатор в формате UUID")},
        "source_control.project": {"type": "str", "check": _profile_rule_re(
            _SC_NAME_RE, "проект SourceControl — латиница, цифры, дефис, подчёркивание, до 64 символов")},
        "source_control.repos": {"type": "list", "max_items": PROFILE_MAX_LIST_ITEMS, "check": _profile_rule_re(
            _SC_NAME_RE, "репозиторий — латиница, цифры, дефис, подчёркивание, до 64 символов")},
        "sberworks.jira_project": {"type": "str", "upper": True, "check": _profile_rule_re(
            _SPACE_CODE_RE, "ключ проекта Jira — заглавные латинские буквы и цифры, от 2 до 20 символов")},
        "sberworks.confluence_space": {"type": "str", "upper": True, "check": _profile_rule_re(
            _CONFLUENCE_KEY_RE, "ключ пространства Confluence — латиница и цифры, до 32 символов")},
    }


def profile_split_key(dotted_key: str) -> List[str]:
    return [part for part in dotted_key.split(".") if part]


def profile_get_path(data: Dict[str, Any], dotted_key: str) -> Tuple[bool, Any]:
    """``(present, value)`` without treating blanks as absent (profile_get does)."""
    node: Any = data
    for part in profile_split_key(dotted_key):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def profile_set_path(data: Dict[str, Any], dotted_key: str, value: Any) -> None:
    parts = profile_split_key(dotted_key)
    node = data
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def profile_delete_path(data: Dict[str, Any], dotted_key: str) -> bool:
    """Remove the key; an emptied section goes with it. True when something was removed."""
    parts = profile_split_key(dotted_key)
    node: Any = data
    trail: List[Tuple[Dict[str, Any], str]] = []
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return False
        trail.append((node, part))
        node = node[part]
    if not isinstance(node, dict) or parts[-1] not in node:
        return False
    del node[parts[-1]]
    for parent, name in reversed(trail):
        if isinstance(parent.get(name), dict) and not parent[name]:
            del parent[name]
        else:
            break
    return True


def _profile_dedupe(items: List[Any]) -> List[Any]:
    """Drop duplicates keeping the first appearance (as _flatten_user_logins does)."""
    seen: List[Any] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def normalize_profile(data: Dict[str, Any]) -> Dict[str, Any]:
    """Trim, drop blanks, upper-case codes, dedupe lists -- before validation.

    Returns a deep copy; unknown keys and values of the wrong type are carried
    over untouched so validate_profile() can name them. A list key given a
    single string is split on commas (how ``set`` receives lists).
    """
    out = copy.deepcopy(data)
    for key, spec in profile_schema().items():
        present, value = profile_get_path(out, key)
        if not present:
            continue
        if spec["type"] == "list":
            if isinstance(value, str):
                value = value.split(",")
            if not isinstance(value, list):
                continue
            items: List[Any] = []
            for item in value:
                if not isinstance(item, str):
                    items.append(item)
                    continue
                text = item.strip()
                if not text:
                    continue
                if spec.get("upper"):
                    text = text.upper()
                items.append(text)
            normalized: Any = _profile_dedupe(items)
        else:
            if not isinstance(value, str):
                continue
            text = value.strip()
            if spec.get("upper"):
                text = text.upper()
            elif spec.get("lower"):
                text = text.lower()
            normalized = text
        if normalized == "" or normalized == [] or normalized is None:
            profile_delete_path(out, key)
        else:
            profile_set_path(out, key, normalized)
    return out


def validate_profile(data: Dict[str, Any]) -> List[Dict[str, str]]:
    """Errors as ``[{key, reason}]`` (empty when the profile is valid).

    Checks the managed stamps, the section shape and every known key against
    profile_schema(); unknown keys are not errors. Call on normalized data: the
    rules assume trimmed, upper-cased values.
    """
    errors: List[Dict[str, str]] = []
    if "version" in data and (isinstance(data["version"], bool) or not isinstance(data["version"], int)):
        errors.append({"key": "version", "reason": "версия схемы — целое число"})
    if "updated_at" in data and not isinstance(data["updated_at"], str):
        errors.append({"key": "updated_at", "reason": "момент сохранения — строка"})
    for section in PROFILE_SECTIONS:
        if section in data and not isinstance(data[section], dict):
            errors.append({"key": section, "reason": "раздел должен быть JSON-объектом"})
    for key, spec in profile_schema().items():
        present, value = profile_get_path(data, key)
        if not present:
            continue
        if spec["type"] == "list":
            if not isinstance(value, list):
                errors.append({"key": key, "reason": "ожидается список строк"})
                continue
            if len(value) > spec["max_items"]:
                errors.append({"key": key, "reason": "не больше %d элементов" % spec["max_items"]})
                continue
            for item in value:
                if not isinstance(item, str):
                    errors.append({"key": key, "reason": "ожидается список строк"})
                    break
                reason = spec["check"](item)
                if reason:
                    errors.append({"key": key, "reason": "%r: %s" % (item, reason)})
                    break
            continue
        if not isinstance(value, str):
            errors.append({"key": key, "reason": "ожидается строка"})
            continue
        reason = spec["check"](value)
        if reason:
            errors.append({"key": key, "reason": reason})
    return errors


class LoadedProfile(NamedTuple):
    """Result of load_profile(): what the file says, and whether it is usable.

    ``status`` is one of ``ok`` (data is usable), ``missing`` (no file),
    ``corrupt`` (file exists but is not a well-formed profile) and
    ``unsupported`` (schema version newer than this family knows). ``data`` is
    ``{}`` unless the status is ``ok``, so a consumer that only reads through
    profile_get() never has to branch on the status itself. ``reason`` explains
    a non-``ok`` status in one line for the profile command and the doctor.

    ``raw`` is the parsed JSON object whenever the file parsed into one, even
    when the values do not pass the schema check. Only the single writer
    (guru_profile.py) reads it: it must be able to repair a hand-edited file
    without losing the keys that are fine. Every other consumer reads ``data``
    through profile_get(), which is empty for anything but ``ok``.
    """

    status: str
    path: str
    data: Dict[str, Any]
    version: Optional[int]
    reason: Optional[str]
    raw: Optional[Dict[str, Any]] = None


def profile_path(home: Optional[Path] = None) -> Path:
    """``~/.config/pvm-guru/profile.json`` (under ``home`` when it is given).

    ``home`` exists for test isolation and the ``--home`` flag, exactly like
    guru_catalog.py: no environment variable is consulted.
    """
    base = Path(home).expanduser() if home is not None else Path.home()
    return base.joinpath(*PROFILE_REL)


def load_profile(home: Optional[Path] = None) -> LoadedProfile:
    """Read the profile without ever raising.

    A broken profile must not take a tracker or SourceControl call down with it:
    every non-``ok`` status simply means "behave as if there were no profile",
    and the reason is kept for the commands whose job is to show it. The file may
    have been opened in notepad, so it is read with the same BOM/UTF-16 tolerance
    as the env file. A version above PROFILE_SCHEMA_VERSION is ``unsupported``
    (never ``corrupt``): the file is fine, the family is old, and only an explicit
    reset may overwrite it. A missing version is tolerated as the current schema
    so a hand-made file is not rejected for the stamp the writer adds anyway.

    Values are checked too, not only the shape: a file edited by hand into
    ``primary_space: "PVM PROJECT"`` is ``corrupt``, because REQ-001 asks for
    "behaves as if there were no profile" whenever the file "does not pass the
    composition check". Without that, a value the writer would never accept
    would still travel into a TQL query, an endpoint path or a JQL clause. The
    reason names the offending keys (ASCII, so the doctor can print it); the
    per-key wording comes from validate_profile() where a human reads it.
    """
    path = profile_path(home)
    where = str(path)
    if not path.exists():
        return LoadedProfile("missing", where, {}, None, None)
    try:
        raw = json.loads(_read_env_text(path))
    except OSError as exc:
        return LoadedProfile("corrupt", where, {}, None, "cannot read file: %s" % exc)
    except ValueError as exc:
        return LoadedProfile("corrupt", where, {}, None, "not valid JSON: %s" % exc)
    if not isinstance(raw, dict):
        return LoadedProfile("corrupt", where, {}, None, "top-level value is not a JSON object")
    version = raw.get("version", PROFILE_SCHEMA_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        return LoadedProfile("corrupt", where, {}, None, "`version` must be an integer", raw)
    if version > PROFILE_SCHEMA_VERSION:
        return LoadedProfile(
            "unsupported", where, {}, version,
            "schema version %d is newer than the supported %d: update the family or reset the profile"
            % (version, PROFILE_SCHEMA_VERSION), raw)
    for section in PROFILE_SECTIONS:
        if section in raw and not isinstance(raw[section], dict):
            return LoadedProfile("corrupt", where, {}, version, "`%s` must be a JSON object" % section, raw)
    normalized = normalize_profile(raw)
    errors = validate_profile(normalized)
    if errors:
        return LoadedProfile(
            "corrupt", where, {}, version,
            "these values fail the profile schema check: %s" % ", ".join(item["key"] for item in errors), raw)
    return LoadedProfile("ok", where, normalized, version, None, raw)


def profile_get(profile: Optional[LoadedProfile], dotted_key: str) -> Any:
    """Value behind a dotted key (``task_tracker.primary_space``), or ``None``.

    Accepts the LoadedProfile handle (anything but ``ok`` yields ``None``, so
    callers never branch on the status) or ``None`` (the ``--no-profile`` case).
    Empty strings and empty lists count as absent: an explicitly blanked key
    must not shadow the next source in resolve_default().
    """
    if not isinstance(profile, LoadedProfile) or profile.status != "ok":
        return None
    node: Any = profile.data
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    if node is None or (isinstance(node, (str, list)) and not node) or (isinstance(node, str) and not node.strip()):
        return None
    return node


def resolve_default(
    explicit: Optional[str],
    env_names: Iterable[str],
    profile: Optional[LoadedProfile],
    key: str,
) -> Tuple[Any, Optional[str]]:
    """``(value, source)`` in the order explicit -> environment -> profile.

    ``source`` is ``explicit``, ``env`` or ``profile`` -- or ``None`` together
    with a ``None`` value when nothing supplied one, so the caller can keep its
    existing "argument required" error verbatim. An explicit blank does not
    count as given; the environment is consulted in ``env_names`` order, as
    _first_env() does.
    """
    if explicit is not None and explicit.strip():
        return explicit, "explicit"
    env_value = _first_env(env_names)
    if env_value is not None:
        return env_value, "env"
    value = profile_get(profile, key)
    if value is not None:
        return value, "profile"
    return None, None


class DefaultsApplied:
    """Accumulator for the ``defaults_applied`` field of a command result.

    Every command that can take a value from somewhere other than its own flags
    records ``{key, value, source}`` here, so the JSON says structurally where a
    space or a login came from (``profile``, ``env``, ``os_user``,
    ``credential``). Empty accumulators are not printed: ``bool(applied)`` tells
    the caller whether there is anything to add.
    """

    def __init__(self) -> None:
        self._entries: List[Dict[str, Any]] = []

    def add(self, key: str, value: Any, source: str) -> None:
        self._entries.append({"key": key, "value": value, "source": source})

    def as_payload(self) -> List[Dict[str, Any]]:
        return self._entries

    def __bool__(self) -> bool:
        return bool(self._entries)


def with_defaults(payload: Dict[str, Any], applied: Optional[DefaultsApplied]) -> Dict[str, Any]:
    """Add ``defaults_applied`` to a command result; an empty list is not printed.

    One helper for every CLI that can take a value from somewhere other than its
    own flags. The accumulator is the argument, so a caller that keeps no
    namespace (a test, guru_profile) can use it too; the CLIs go through
    args_with_defaults() below.
    """
    if applied:
        payload["defaults_applied"] = applied.as_payload()
    return payload


def profile_error_context(applied: Optional[DefaultsApplied], hint: str) -> Dict[str, Any]:
    """Extra error fields when part of the call came from the profile.

    A space, product, tenant or project taken from the profile can be rejected
    by the backend, and that refusal has to be visible together with where the
    value came from -- otherwise the user looks for the mistake in the request
    while it sits in the profile (REQ-016..REQ-019). ``hint`` is the one
    sentence each CLI words for its own values.
    """
    if not applied:
        return {}
    entries = applied.as_payload()
    extra: Dict[str, Any] = {"defaults_applied": entries}
    if any(entry["source"] == "profile" for entry in entries):
        extra["hint"] = hint
    return extra


# From here to write_json() the profile talks to argparse on purpose: every CLI
# that reads the profile keeps it and the accumulator on its namespace, and the
# plumbing that puts them there and reads them back has to live in one place --
# copied into each main() it turned every change of profile behaviour into five
# identical edits. Everything above this line knows nothing about argparse and
# stays usable without it.

# The hint three CLIs word identically (swtr_task_tool, both SourceControl
# tools): their values are the same kind of thing, so the sentence is the same.
# jira_tool and confluence_tool name their single value and keep their own text.
PROFILE_ERROR_HINT_GENERIC = (
    "Часть значений вызова взята из профиля (см. defaults_applied). Если отказ вызван ими — "
    "поправь профиль (/guru_profile) или назови значения явно; без профиля — повтори с --no-profile.")


def attach_profile(args: argparse.Namespace) -> None:
    """Put the profile and an empty accumulator on the namespace, once per run.

    ``--no-profile`` leaves ``args.profile`` at ``None``, and every resolver
    then behaves exactly as it did for a user without a profile. The two lines
    are the same everywhere the profile is read -- the five CLIs of this skill
    and ``crpv_decompose.py prepare`` -- so they live here and not in each
    main().
    """
    args.profile = None if getattr(args, "no_profile", False) else load_profile()
    args.defaults_applied = DefaultsApplied()


def args_with_defaults(payload: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    """``with_defaults()`` for a CLI that keeps the accumulator on its namespace."""
    return with_defaults(payload, args.defaults_applied)


def args_error_context(args: argparse.Namespace, hint: str = PROFILE_ERROR_HINT_GENERIC) -> Dict[str, Any]:
    """``profile_error_context()`` for a CLI; ``hint`` defaults to the shared text."""
    return profile_error_context(args.defaults_applied, hint)


# tenant/project of SourceControl: the same table for both SourceControl CLIs.
# The environment names are the ones guru-pr-review.py::resolve_tenant reads --
# one environment must not give two scripts of the family a different tenant --
# and a single table is the only way they cannot drift apart.
SC_PROJECT_DEFAULTS = (
    ("tenant", ("SOURCE_CONTROL_TENANT", "SC_TENANT", "SOURCE_CONTROL_TENANT_UUID", "SC_TENANT_UUID"),
     "source_control.tenant", "--tenant"),
    ("project", ("SOURCE_CONTROL_PROJECT", "SC_PROJECT"), "source_control.project", "--project"),
)


def apply_sc_project_defaults(args: argparse.Namespace, extra_required: Iterable[str] = ()) -> None:
    """tenant and project: explicit flag, else environment, else profile.

    Reads ``args.tenant``/``args.project``/``args.profile``/``args.defaults_applied``
    and the ``subparser`` both CLIs stamp on the namespace. Missing everywhere is
    refused in argparse's own words and with its exit code 2, through the
    subparser of the command: a user without a profile must not notice that the
    argument stopped being ``required``. ``extra_required`` names flags that have
    no default at all (``--repo`` of the PR tool): they are reported in the SAME
    refusal, so nothing is asked for twice.
    """
    profile = args.profile
    applied = args.defaults_applied
    missing: List[str] = []
    for name, env_names, key, flag in SC_PROJECT_DEFAULTS:
        value, source = resolve_default(getattr(args, name, None), env_names, profile, key)
        if source is None:
            missing.append(flag)
            continue
        setattr(args, name, value)
        if source != "explicit":
            applied.add(name, value, source)
    for flag in extra_required:
        if not getattr(args, flag.lstrip("-").replace("-", "_"), None):
            missing.append(flag)
    if missing:
        args.subparser.error("the following arguments are required: %s" % ", ".join(missing))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


UNIT_CODE_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,20}-\d+\b")
TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9_][A-Za-zА-Яа-яЁё0-9_\-]{2,}")
# Ссылка в тексте юнита. Закрывающие скобки и кавычки в адрес не входят: ссылку
# в постановке почти всегда обрамляют — скобкой, кавычкой или запятой, — и без
# отсечки хвост обрамления уезжает в адрес.
URL_RE = re.compile(r"https?://[^\s<>\"'`)\]]+")


def value_to_text(value: Any, *, strip_html: bool = False) -> str:
    """Flatten an arbitrary SWTR/JSON value to a single plain-text line.

    The dict-key search order includes both ``value`` and ``text`` so a wrapped
    ``{"value": ...}`` / ``{"text": ...}`` field resolves to its inner text
    (merged from the two former copies of this helper). With ``strip_html=True``
    the string branch additionally HTML-unescapes and removes tags — the behavior
    fetch_swtr_sources needs for wiki/markup payloads; the default keeps the
    lighter whitespace-collapse used by the task/comment/source-control tools.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        if strip_html:
            unescaped = html.unescape(value)
            without_tags = re.sub(r"<[^>]+>", " ", unescaped)
            return re.sub(r"\s+", " ", without_tags).strip()
        return " ".join(value.split())
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        for key in ("name", "summary", "title", "code", "login", "value", "text"):
            if key in value:
                text = value_to_text(value[key], strip_html=strip_html)
                if text:
                    return text
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, list):
        parts = [value_to_text(item, strip_html=strip_html) for item in value]
        return ", ".join(part for part in parts if part)
    return str(value)


def normalize_text(value: Any) -> str:
    """value_to_text() plus HTML unescape + tag strip (SWTR unit/attribute text)."""
    text = value_to_text(value)
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_unit_code(value: str) -> Optional[str]:
    """Return the first SWTR unit code (e.g. CRPV-35498) in URL-decoded, upper text."""
    match = UNIT_CODE_RE.search(urllib.parse.unquote(value).upper())
    return match.group(0) if match else None


def significant_terms(text: str, stopwords: Iterable[str], limit: int = 8) -> List[str]:
    """Rank tokens by frequency, dropping short/numeric/stop words.

    ``stopwords`` is a parameter (not a module global) so each caller keeps its
    own list and current behavior. Returns the top ``limit`` terms, ties A→Z.
    """
    stop = set(stopwords)
    counts: Dict[str, int] = {}
    for token in TOKEN_RE.findall(text):
        low = token.lower().strip("-_")
        if len(low) < 4 or low in stop or low.isdigit():
            continue
        counts[low] = counts.get(low, 0) + 1
    return [term for term, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]
