#!/usr/bin/env python3
"""Fill ~/.swtr-review.env for the pvm-guru skill without leaking secrets.

guru_doctor.py diagnoses; this script repairs. It is the only component allowed
to write the env file: the agent collects a login and a password in chat and
hands them over through the process environment (never argv -- argv is visible
in ``ps`` to every user on the machine), and this script verifies them, issues a
SourceControl API token, and writes the file.

Secrets are written, never printed. The SourceControl token in particular never
reaches stdout, so it never enters an agent's context.

Usage:
    GURU_SETUP_LOGIN=familia.i.o GURU_SETUP_PASSWORD=... \\
        python3 guru_env_setup.py apply [--systems task_tracker,source_control]
    python3 guru_env_setup.py status
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import tempfile
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple

from _swtr_common import (
    _read_env_text,
    _unquote_env_value,
    http_error_details,
    is_gateway_auth_failure,
)

ENV_FILE_DEFAULT = "~/.swtr-review.env"

# Keys this script is allowed to touch. Anything else in the file is foreign and
# is preserved verbatim.
MANAGED_KEYS = (
    "SWTR_BASIC_AUTH",
    "SWTR_BASE_URL",
    "SWTR_INSECURE_SKIP_TLS_VERIFY",
    "SOURCE_CONTROL_BASIC_AUTH",
)


def _line_key(raw: str) -> Optional[str]:
    """The KEY of an assignment line, or None for blanks, comments and garbage.

    A commented-out assignment (``#SWTR_TOKEN=""``) is deliberately NOT a key:
    users keep those as notes, so they must survive untouched and a new value is
    appended as its own live line.
    """
    line = raw.strip()
    if line.startswith("export "):
        line = line[len("export "):].lstrip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    return line.split("=", 1)[0].strip() or None


def _assign(raw: str, key: str, value: str) -> str:
    """Render ``KEY="value"``, preserving an ``export`` prefix the line already had."""
    prefix = "export " if raw.lstrip().startswith("export ") else ""
    return '%s%s="%s"' % (prefix, key, value)


def read_env_lines(path) -> List[str]:
    """Read the env file as a list of lines, tolerating notepad's encodings.

    Returns [] when the file does not exist, so a first-time setup is just an
    empty starting point rather than a special case.
    """
    path = Path(path).expanduser()
    if not path.exists() or not path.is_file():
        return []
    return _read_env_text(path).splitlines()


def parse_env_pairs(path) -> Dict[str, str]:
    """KEY -> value for the file's live assignments (first occurrence wins).

    First-occurrence-wins mirrors load_env_file: it sets os.environ only when the
    key is unset, so an earlier duplicate is what the runtime tools actually see.
    """
    pairs: Dict[str, str] = {}
    for raw in read_env_lines(path):
        key = _line_key(raw)
        if key is None:
            continue
        pairs.setdefault(key, _unquote_env_value(raw.strip().split("=", 1)[1].strip()))
    return pairs


def merge_env_lines(
    lines: List[str],
    updates: Dict[str, str],
    only_if_missing: Iterable[str] = (),
) -> Tuple[List[str], List[str]]:
    """Apply `updates` to `lines`, returning (new_lines, changed_keys).

    Existing keys are replaced where they stand so the file keeps its order,
    comments and foreign keys; missing keys are appended. Every duplicate of a
    key is refreshed -- leaving a stale second copy would silently win on some
    other read order. Keys in `only_if_missing` are written only when absent:
    an existing SWTR_BASE_URL is a deliberate user choice, not something to
    overwrite.
    """
    optional = set(only_if_missing)
    present = set()
    out: List[str] = []
    changed: List[str] = []
    for raw in lines:
        key = _line_key(raw)
        if key in updates:
            present.add(key)
            if key in optional:
                out.append(raw)
                continue
            out.append(_assign(raw, key, updates[key]))
            if key not in changed:
                changed.append(key)
            continue
        out.append(raw)
    for key, value in updates.items():
        if key not in present:
            out.append('%s="%s"' % (key, value))
            changed.append(key)
    return out, changed


def write_env_file(path, lines: List[str]) -> bool:
    """Write `lines` as UTF-8 with mode 600. Returns True if the file existed.

    The write is atomic (via temp file + os.replace) to ensure a crash or full
    disk never leaves ~/.swtr-review.env truncated, corrupting the credential
    file that every other script reads. Any previous content is copied to
    ``<name>.bak`` first. The rewrite also normalizes a UTF-16/BOM file
    (which the doctor warns about) into plain UTF-8.

    Known limitations: the .bak copy itself is written non-atomically, so a
    crash during the backup step can leave a truncated .bak (the live file is
    untouched until the final atomic replace). Mode 0600 restricts access on
    POSIX; on Windows, st_mode carries only the read-only DOS attribute, so
    the permission is not a real owner-only restriction. A value containing a
    literal newline would split across physical lines and corrupt the file;
    the writer does not escape newlines because env values in this skill never
    contain them.
    """
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    if existed:
        backup = path.parent / (path.name + ".bak")
        backup.write_bytes(path.read_bytes())
        os.chmod(backup, 0o600)
    text = "\n".join(lines).rstrip("\n") + "\n"

    # Write to a temp file in the same directory (same filesystem for atomic rename),
    # created with mode 0o600 from the start to avoid a window where credentials
    # exist under the process umask (typically 644).
    tmp_fd = None
    try:
        tmp_fd, tmp_path_str = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp"
        )
        tmp_path = Path(tmp_path_str)
        # Write using the fd to ensure no intermediate world-readable state.
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            tmp_fd = None  # fdopen takes ownership
            f.write(text)
        # Atomic replace on POSIX and Windows alike.
        os.replace(tmp_path, path)
    except Exception:
        # Clean up temp file on any failure so no stray .tmp droppings are left.
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except Exception:
                pass
        try:
            if 'tmp_path' in locals():
                tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise

    return existed


def env_key_state(path, keys: Iterable[str]) -> Dict[str, str]:
    """Where each key's effective value comes from: 'shell', 'file' or 'missing'.

    Order matters and is not cosmetic: load_env_file never overrides os.environ,
    so a variable exported in the shell makes the file's line invisible. Without
    this distinction a user gets "fixed!" and nothing changes until they open a
    new shell. NOTE: never call load_env_file before this -- it would move the
    file's keys into os.environ and report every one of them as 'shell'.
    """
    in_file = parse_env_pairs(path)
    state: Dict[str, str] = {}
    for key in keys:
        if os.environ.get(key, "").strip():
            state[key] = "shell"
        elif in_file.get(key, "").strip():
            state[key] = "file"
        else:
            state[key] = "missing"
    return state


def _file_note(path: Path) -> str:
    if not path.exists():
        return "missing (will be created)"
    mode = oct(path.stat().st_mode & 0o777)[2:]
    head = path.read_bytes()[:3]
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        encoding = "utf-16 (BOM) — will be rewritten as utf-8"
    elif head == b"\xef\xbb\xbf":
        encoding = "utf-8 (BOM)"
    else:
        encoding = "utf-8"
    return "exists, mode %s, %s" % (mode, encoding)


def cmd_status(args) -> int:
    path = Path(args.env_file).expanduser()
    print("env file  : %s (%s)" % (path, _file_note(path)))
    state = env_key_state(path, MANAGED_KEYS)
    for key in MANAGED_KEYS:
        kind = state[key]
        label = {"file": "set(file)", "shell": "set(SHELL ENV — file is shadowed!)",
                 "missing": "missing"}[kind]
        print("  %-32s %s" % (key, label))
    if any(v == "shell" for v in state.values()):
        print("  !! a shell-exported variable wins over the file; unset it or open a new shell")
    return 0


SWTR_DEFAULT_BASE_URL = "https://portal.works.prod.sbt/swtr"
DEFAULT_PROBE_UNIT = "CRPV-35498"
TLS_ERROR_MARK = "tls error:"
# Marks a 401/403 whose body was the portal gateway's authentication page rather
# than an application response. Carried in the note/detail string, exactly like
# TLS_ERROR_MARK, so no call signature has to grow a flag.
AUTH_PAGE_MARK = "auth failure page:"


def unverified_context() -> ssl.SSLContext:
    """A context that skips certificate verification (corporate CA not in the store)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def is_tls_error(note: str) -> bool:
    return TLS_ERROR_MARK in (note or "")


def is_auth_failure_page(note: str) -> bool:
    """True when `note`/`detail` came from the portal gateway's login page.

    That page means the credential failed to authenticate — for a domain login,
    almost always a password that was changed elsewhere and never updated here.
    """
    return AUTH_PAGE_MARK in (note or "")


def _read_json(resp):
    raw = resp.read()
    if not raw.strip():
        return None
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"rawText": text}


def http_call(method, url, login, secret, payload, timeout, ctx):
    """Return (status, body, note). Never raises: any failure is status None.

    A certificate failure is marked in `note` so the caller can retry with
    verification off -- that is the whole reason SWTR_INSECURE_SKIP_TLS_VERIFY
    exists, and on a first-time setup the flag is not in the file yet. A
    malformed URL (hand-edited in the env file without a scheme) yields status
    None and a request-error note, never a traceback.
    """
    cred = base64.b64encode(("%s:%s" % (login, secret)).encode("utf-8")).decode("ascii")
    headers = {"Authorization": "Basic " + cred, "accept": "application/json"}
    try:
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url=url, data=data, headers=headers, method=method)
    except Exception as exc:
        # A base URL hand-edited into the env file without a scheme reaches us as a
        # ValueError from Request(). The caller needs a verdict, not a traceback.
        return None, None, "request error: %s" % (exc,)
    kwargs = {"timeout": timeout}
    if ctx is not None:
        kwargs["context"] = ctx
    try:
        with urllib.request.urlopen(req, **kwargs) as resp:
            return resp.status, _read_json(resp), "ok"
    except urllib.error.HTTPError as exc:
        body_text, content_type = http_error_details(exc)
        if is_gateway_auth_failure(exc.code, content_type, body_text):
            return exc.code, None, "%s %s" % (AUTH_PAGE_MARK, exc.reason)
        return exc.code, None, str(exc.reason)
    except urllib.error.URLError as exc:
        reason = exc.reason
        mark = TLS_ERROR_MARK if isinstance(reason, ssl.SSLError) else "network error:"
        return None, None, "%s %s" % (mark, reason)
    except ssl.SSLError as exc:
        return None, None, "%s %s" % (TLS_ERROR_MARK, exc)
    except Exception as exc:
        return None, None, "network error: %s" % (exc,)


class ProbeResult(NamedTuple):
    status: Optional[int]
    ok: bool
    detail: str


def verify_swtr(login, password, base_url, timeout, ctx) -> ProbeResult:
    """One GET against a real unit. ok=True means the credential was accepted.

    200 = accepted and the unit is visible; 404 = the probe unit is gone but the
    Authorization header passed, which is still proof the credential is good.
    401/403 = rejected. Anything else (5xx, transport failure) is NOT proof and
    must not be written -- storing an unverified credential recreates exactly
    the problem the doctor exists to diagnose.
    """
    unit = os.environ.get("SWTR_PROBE_UNIT", "").strip() or DEFAULT_PROBE_UNIT
    url = base_url.rstrip("/") + "/rest/api/unit/v2/" + unit
    status, _body, note = http_call("GET", url, login, password, None, timeout, ctx)
    if status in (200, 404):
        return ProbeResult(status, True, "credential accepted (HTTP %s)" % status)
    if is_auth_failure_page(note):
        # The portal's login gateway answered its authentication page, so the
        # request never reached SWTR: the credential failed to authenticate. For a
        # domain login that is a stale password, whatever status carried it.
        return ProbeResult(status, False,
                           "%s password rejected by the portal's login gateway (HTTP %s)"
                           % (AUTH_PAGE_MARK, status))
    if status == 401:
        return ProbeResult(401, False, "credential rejected (HTTP 401)")
    if status == 403:
        # Not a wrong password: the application answered, and it refused the
        # scheme. Saying "check your password" here sends the user to fix
        # something that is not broken, and the account-lockout warning is noise.
        return ProbeResult(403, False, "credential well-formed but forbidden (HTTP 403)")
    if status is None:
        return ProbeResult(None, False, "could not reach SWTR: %s" % note)
    return ProbeResult(status, False, "cannot confirm the credential (HTTP %s)" % status)


# CI stand (Сборка). The skill's SOURCE_CONTROL_BASE_URL points at .../sc/api/v3,
# so the token must be issued on the same stand -- a CD (sccd) token is rejected here.
SC_TOKEN_API_DEFAULT = "https://portal.works.prod.sbt/ssd/tools/sc/api/v1"
SC_UI_TOKEN_PAGE = "https://portal.works.prod.sbt/ssd/tools/sc/user/settings/applications"


class TokenResult(NamedTuple):
    ok: bool
    token: Optional[str]
    status: Optional[int]
    detail: str


def issue_sc_token(login, secret, api_url, timeout, ctx, name=None) -> TokenResult:
    """Create a full-access SourceControl API token (the Cookbook's create_token.py, inlined).

    `secret` is the password on the primary path; on 401 the backend wants a
    temporary token created in the UI instead, and the caller re-runs with that.
    The issued token is returned for writing -- it is deliberately kept out of
    `detail` so no caller can print it by accident.
    """
    token_name = name or "%s_%s" % (login, datetime.now().strftime("%Y-%m-%d_%H-%M"))
    url = api_url.rstrip("/") + "/users/" + login + "/tokens"
    payload = {"name": token_name, "scopes": ["all"]}
    status, body, note = http_call("POST", url, login, secret, payload, timeout, ctx)
    if status == 201:
        sha1 = str((body or {}).get("sha1") or "").strip() if isinstance(body, dict) else ""
        if sha1:
            return TokenResult(True, sha1, 201, "issued as %s" % token_name)
        return TokenResult(False, None, 201, "server returned 201 without a token value (no sha1)")
    if status == 401:
        return TokenResult(False, None, 401,
                           "credential rejected by the token API — a temporary token created in "
                           "the UI is required (%s)" % SC_UI_TOKEN_PAGE)
    if status == 403:
        return TokenResult(False, None, 403, "not allowed to issue a token for this account")
    if status is None:
        return TokenResult(False, None, None, "could not reach SourceControl: %s" % note)
    return TokenResult(False, None, status, "unexpected HTTP %s from the token API" % status)


EXIT_OK = 0
EXIT_USAGE = 1
EXIT_CRED_REJECTED = 2
EXIT_NEED_SC_TEMP_TOKEN = 3
EXIT_UNVERIFIABLE = 4
EXIT_PARTIAL = 5
EXIT_FORBIDDEN = 6

ALL_SYSTEMS = ("task_tracker", "source_control")


def _resolve_credential(file_pairs):
    """(login, password, source). Env pair or file pair, never mixed.

    Either both env variables are set, or we reuse the file's SWTR line.
    Pairing an env-supplied login with the file's password builds a credential
    that was never valid for anyone, and reports it under a misleading source.

    Reuse matters for the common "only SourceControl is broken" case: the
    password is needed to issue the token, but the user should not be asked for
    something the file already holds.
    """
    login = os.environ.get("GURU_SETUP_LOGIN", "").strip()
    password = os.environ.get("GURU_SETUP_PASSWORD", "")
    if login and password:
        return login, password, "prompt"
    f_login, _, f_password = file_pairs.get("SWTR_BASIC_AUTH", "").partition(":")
    return f_login.strip(), f_password, "env file"


def cmd_apply(args) -> int:
    path = Path(args.env_file).expanduser()
    # The doctor prints "FIXABLE: task_tracker source_control" (space separated) while
    # this flag documents a comma separated list. Accept either, so copying the doctor's
    # own line verbatim cannot become an argparse usage error — which exits 2, the same
    # code this script uses for "credential rejected", and would be reported as such.
    raw = args.systems or ",".join(ALL_SYSTEMS)
    systems = [s for s in raw.replace(",", " ").split() if s]
    unknown = [s for s in systems if s not in ALL_SYSTEMS]
    if unknown:
        print("ERROR: unknown system(s): %s (known: %s)" % (", ".join(unknown), ", ".join(ALL_SYSTEMS)))
        return EXIT_USAGE

    file_pairs = parse_env_pairs(path)
    login, password, source = _resolve_credential(file_pairs)
    if not login or not password:
        print("ERROR: no credential available. Set GURU_SETUP_LOGIN and GURU_SETUP_PASSWORD.")
        return EXIT_USAGE

    print("env file  : %s (%s)" % (path, _file_note(path)))
    print("login     : len=%d, from %s" % (len(login), source))

    base_url = (os.environ.get("SWTR_BASE_URL", "").strip()
                or file_pairs.get("SWTR_BASE_URL", "").strip()
                or SWTR_DEFAULT_BASE_URL)
    ctx = None
    probe = verify_swtr(login, password, base_url, args.timeout, ctx)
    if not probe.ok and probe.status is None and is_tls_error(probe.detail):
        # First-time setup on a machine without the corporate CA: the flag that
        # fixes this is exactly what we are about to write, so retry once with
        # verification off and say so out loud.
        ctx = unverified_context()
        probe = verify_swtr(login, password, base_url, args.timeout, ctx)
        print("tls       : certificate verification OFF (corporate CA not trusted by Python)")
    print("swtr      : %s" % probe.detail)

    # --force covers "could not confirm" (5xx, network, TLS) only. A 401/403 is a
    # definitive answer, not a missing one: writing a credential the server has
    # already rejected recreates the exact broken state this tool exists to repair.
    definitive = probe.status in (401, 403)
    if not probe.ok and (definitive or not args.force):
        if is_auth_failure_page(probe.detail):
            print("VERDICT   : nothing written. The portal did not accept this password. If the "
                  "domain password was changed recently, the one on file is stale — re-run with "
                  "the new password (do NOT retry the old one: repeated failures lock the "
                  "domain account).")
            return EXIT_CRED_REJECTED
        if probe.status == 401:
            print("VERDICT   : nothing written. Check the login and password, then re-run "
                  "(do NOT retry the same password — repeated failures lock the account).")
            return EXIT_CRED_REJECTED
        if probe.status == 403:
            print("VERDICT   : nothing written. The credential is well-formed but the backend "
                  "forbids it — this is NOT a wrong password, so changing it will not help. "
                  "The portal most likely refuses Basic-with-password: get a Personal Access "
                  "Token and set SWTR_BEARER_TOKEN (or SWTR_AUTHORIZATION) in the env file.")
            return EXIT_FORBIDDEN
        print("VERDICT   : nothing written — the credential could not be confirmed. "
              "Re-run when the server answers, or pass --force to write it unverified.")
        return EXIT_UNVERIFIABLE
    if not probe.ok:
        print("swtr      : !! written WITHOUT verification (--force)")

    sc_token = None
    sc_failure = None
    if "source_control" in systems:
        secret = os.environ.get("GURU_SETUP_SC_TEMP_TOKEN", "").strip() or password
        result = issue_sc_token(login, secret, args.sc_token_api, args.timeout, ctx)
        print("sc token  : %s" % result.detail)
        if result.ok:
            sc_token = result.token
        else:
            sc_failure = result

    updates = {}
    if "task_tracker" in systems:
        updates["SWTR_BASIC_AUTH"] = "%s:%s" % (login, password)
    if sc_token:
        updates["SOURCE_CONTROL_BASIC_AUTH"] = "%s:%s" % (login, sc_token)
    updates["SWTR_BASE_URL"] = SWTR_DEFAULT_BASE_URL
    updates["SWTR_INSECURE_SKIP_TLS_VERIFY"] = "1"

    lines, changed = merge_env_lines(
        read_env_lines(path), updates,
        only_if_missing=("SWTR_BASE_URL", "SWTR_INSECURE_SKIP_TLS_VERIFY"))
    try:
        write_env_file(path, lines)
    except OSError as exc:
        print("VERDICT   : could not write %s — %s" % (path, exc))
        if sc_token:
            print("            a SourceControl token WAS issued but could not be saved; "
                  "re-run after fixing the file to issue a fresh one.")
        return EXIT_USAGE
    print("written   : %s" % (", ".join(changed) if changed else "nothing (already up to date)"))

    shadowed = [k for k, v in env_key_state(path, changed).items() if v == "shell"]
    if shadowed:
        print("  !! %s is exported in your shell and wins over the file — unset it or open a "
              "new shell" % ", ".join(shadowed))

    if sc_failure is not None:
        if sc_failure.status == 401:
            print("NEXT      : create a temporary token at %s, then re-run with "
                  "GURU_SETUP_SC_TEMP_TOKEN set to it." % SC_UI_TOKEN_PAGE)
            return EXIT_NEED_SC_TEMP_TOKEN
        return EXIT_PARTIAL
    return EXIT_OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Fill ~/.swtr-review.env for pvm-guru (no secrets printed).")
    sub = ap.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="show which keys are set and where they come from")
    p_status.add_argument("--env-file", default=ENV_FILE_DEFAULT)
    p_status.set_defaults(func=cmd_status)

    p_apply = sub.add_parser("apply", help="verify credentials, issue the SC token, write the env file")
    p_apply.add_argument("--env-file", default=ENV_FILE_DEFAULT)
    p_apply.add_argument("--systems", default=None,
                         help="comma-separated subset of %s (default: all)" % ",".join(ALL_SYSTEMS))
    p_apply.add_argument("--sc-token-api", default=os.environ.get("GURU_SETUP_SC_TOKEN_API", "").strip()
                         or SC_TOKEN_API_DEFAULT)
    p_apply.add_argument("--timeout", type=float, default=20.0)
    p_apply.add_argument("--force", action="store_true",
                         help="write the credential when verification was inconclusive "
                              "(5xx/network/TLS); a rejected credential is never written")
    p_apply.set_defaults(func=cmd_apply)

    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        # argparse exits 2 on a usage error, which collides with EXIT_CRED_REJECTED.
        # A caller reading exit codes would report a mistyped flag as a rejected
        # password. --help still exits 0.
        return EXIT_OK if exc.code in (0, None) else EXIT_USAGE
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
