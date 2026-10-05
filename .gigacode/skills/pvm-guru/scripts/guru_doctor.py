#!/usr/bin/env python3
"""Diagnose SWTR/SourceControl auth without leaking secrets.

Why this exists: passwords with special characters (``}`` ``%`` ``#`` ``*``)
were blamed for 403s. The client transmits them correctly (this tool proves the
credential round-trips byte-for-byte), so a 403 is about the credential itself,
not about how it was typed. This tool shows exactly which layer fails.

Reading a 403 needs care, because two different failures share that status:

- The portal's login gateway answers an HTML authentication page under 403 when
  it cannot authenticate the credential. On this path the request never reaches
  the backend, and by far the most common cause is a **domain password that was
  changed** and not updated in ``~/.swtr-review.env``. The fix is a new password,
  and ``guru_env_setup.py`` can write it.
- A 403 from the application itself (JSON) means the credential authenticated but
  the backend refuses the scheme -- that is where a Personal Access Token helps.

Conflating the two used to send users whose password had merely expired after a
PAT they did not need, so ``is_gateway_auth_failure`` tells them apart.

Usage:
    python3 guru_doctor.py [--env-file ~/.swtr-review.env] [--system task_tracker]
                           [--base-url URL] [--probe-path /...] [--timeout 20]
                           [--no-network]

It prints a structural fingerprint of the credential (length and character
classes only -- never the value) and, unless --no-network, the live HTTP status.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import NamedTuple, Optional

from _swtr_common import (
    INSECURE_TLS_VALUES,
    base_url_for,
    build_authorization,
    build_headers,
    http_error_details,
    is_gateway_auth_failure,
    load_env_file,
    load_profile,
    profile_get,
    redact_secrets,
)

# Where a portal token comes from. The portal issues one token that both
# task_tracker and source_control accept; sberworks (confluence/jira) is a
# different domain with its own per-application PATs.
PORTAL_TOKEN_URL = "https://portal.works.prod.sbt/ssd/privileges"
# The address alone is not enough: the page shows no token, it downloads a file,
# and the token sits inside it after a marker line. Users who got just the URL
# opened it and had nothing to copy.
PORTAL_TOKEN_HOWTO = (
    "open " + PORTAL_TOKEN_URL + " -- it downloads a file named 'privileges'; open the file, "
    "scroll to the line '[Authorization] header: Bearer' and copy the token after it, "
    "without the word Bearer"
)

# Primary credential env var to suggest when none is found, per system. Both
# schemes are named: Basic keeps working, but a token is the one that survives a
# domain password rotation, and users could not choose it while it went unmentioned.
PRIMARY_HINT = {
    "task_tracker": 'SWTR_BASIC_AUTH="login:password" or SWTR_BEARER_TOKEN="<token>" '
                    "(token: " + PORTAL_TOKEN_HOWTO + ")",
    "source_control": 'SOURCE_CONTROL_BASIC_AUTH="login:password" or '
                      'SOURCE_CONTROL_BEARER_TOKEN="<token>"',
    "confluence": 'CONFLUENCE_BEARER_TOKEN="<PAT>"  (or SBERWORKS_TOKEN)',
    "jira": 'JIRA_BEARER_TOKEN="<PAT>"',
}
DEFAULT_SC_PROBE_TENANT = "22f434fb-0355-42a1-ac25-011074a20a2d"
DEFAULT_SC_PROBE_PROJECT = "pvm"
DEFAULT_SWTR_PROBE_UNIT = "CRPV-35498"


def probe_path_for(system: str, probe_path_arg: Optional[str]) -> str:
    """Resolve the path appended to the base URL for a system's probe.

    An explicit --probe-path wins. Otherwise the two portal systems need a
    concrete resource for a meaningful 200 (there is no project-independent
    endpoint that returns 200), so they fall back to env-overridable pvm defaults;
    the sberworks systems use their whoami endpoints.
    """
    if probe_path_arg is not None:
        return probe_path_arg
    if system == "task_tracker":
        unit = os.environ.get("SWTR_PROBE_UNIT", "").strip() or DEFAULT_SWTR_PROBE_UNIT
        return "rest/api/unit/v2/" + unit
    if system == "source_control":
        tenant = os.environ.get("SOURCE_CONTROL_PROBE_TENANT", "").strip() or DEFAULT_SC_PROBE_TENANT
        project = os.environ.get("SOURCE_CONTROL_PROBE_PROJECT", "").strip() or DEFAULT_SC_PROBE_PROJECT
        return "repos/" + tenant + "/" + project + "?limit=1"
    if system == "confluence":
        return "rest/api/user/current"
    if system == "jira":
        return "rest/api/2/myself"
    return ""


def detect_encoding(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return "missing"
    head = path.read_bytes()[:3]
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "utf-16 (BOM)"
    if head == b"\xef\xbb\xbf":
        return "utf-8 (BOM)"
    return "utf-8"


def classify(password: str) -> str:
    has = lambda pred: any(pred(c) for c in password)
    classes = []
    if has(str.isalpha):
        classes.append("letters")
    if has(str.isdigit):
        classes.append("digits")
    specials = [c for c in password if not c.isalnum()]
    if specials:
        classes.append(f"special({len(specials)})")
    tail = password[-1] if password else ""
    # Report only the class of the last character, never the literal value --
    # users screenshot this output, so leak nothing identifying.
    tail_kind = "alnum" if tail.isalnum() else ("special" if tail else "empty")
    return f"len={len(password)} [{', '.join(classes) or 'none'}] last={tail_kind}"


IDENTITY_SYSTEMS = ("confluence", "jira")


class Identity(NamedTuple):
    authenticated: bool
    kind: str
    display: str


class Verdict(NamedTuple):
    ok: bool
    category: str
    detail: str


def extract_identity(system: str, body) -> Identity:
    """Decide whether a Confluence/Jira 200 body proves an authenticated user.

    Anonymous read is allowed on both backends, so a 200 alone is not proof.
    Confluence: authenticated iff type != 'anonymous' AND username/userKey set.
    Jira: authenticated iff name or key present.
    """
    if not isinstance(body, dict):
        return Identity(False, "none", "no identity in response")
    if system == "confluence":
        kind = str(body.get("type") or "").strip().lower()
        has_id = bool(str(body.get("username") or "").strip() or str(body.get("userKey") or "").strip())
        return Identity(kind != "anonymous" and has_id, kind or "none", "type=" + (kind or "none"))
    if system == "jira":
        has_id = bool(str(body.get("name") or "").strip() or str(body.get("key") or "").strip())
        return Identity(has_id, "known" if has_id else "none", "identity present" if has_id else "no identity")
    return Identity(False, "n/a", "n/a")


class TlsState(NamedTuple):
    verified: bool
    context: object
    flag: Optional[str]


# Per-system insecure-skip flags, checked before the SWTR_* global fallback.
# This mapping lives in the doctor ONLY; the shared build_ssl_context is untouched
# so the runtime confluence/jira tools keep their own per-system TLS behavior.
DOCTOR_TLS_FLAGS = {
    "task_tracker": (),
    "source_control": ("SOURCE_CONTROL_INSECURE_SKIP_TLS_VERIFY", "SC_INSECURE_SKIP_TLS_VERIFY"),
    "confluence": ("CONFLUENCE_INSECURE_SKIP_TLS_VERIFY",),
    "jira": ("JIRA_INSECURE_SKIP_TLS_VERIFY",),
}

# Runtime tool file + its own insecure-skip-TLS flag, for the cross-tool TLS
# hint (I1): the doctor is more permissive than the runtime tools because it
# also honors the SWTR_* global fallback for confluence/jira. When that global
# fallback (not the system's own flag) is what skipped verification, the
# runtime confluence_tool.py / jira_tool.py will NOT skip verification unless
# its own flag is also set -- the doctor's OK would then be misleading about
# what the runtime tool will actually do.
RUNTIME_TLS_FLAG = {
    "confluence": ("confluence_tool.py", "CONFLUENCE_INSECURE_SKIP_TLS_VERIFY"),
    "jira": ("jira_tool.py", "JIRA_INSECURE_SKIP_TLS_VERIFY"),
}


def doctor_tls_state(system: str) -> TlsState:
    """TLS policy for the doctor's own probe. Honors the per-system insecure flag,
    then SWTR_INSECURE_SKIP_TLS_VERIFY as a global fallback for every system. When
    verification is skipped, the returned flag names exactly which var caused it so
    the printed block can label an OK over an unverified connection."""
    names = list(DOCTOR_TLS_FLAGS.get(system, ())) + ["SWTR_INSECURE_SKIP_TLS_VERIFY"]
    for name in names:
        if os.environ.get(name, "").strip().lower() in INSECURE_TLS_VALUES:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return TlsState(False, ctx, name)
    return TlsState(True, None, None)


STALE_PASSWORD_DETAIL = (
    "%s -- the portal's login gateway rejected the credential (it never reached the "
    "backend). The domain password most likely changed and the one in "
    "~/.swtr-review.env is stale -- update the password. Do NOT retry the old one: "
    "repeated failures lock the domain account."
)
# Deliberately no token advice here: the gateway rejected the credential, so a
# different credential type is not the diagnosis, and offering one sent users
# after a PAT they did not need. Token advice belongs to the application-level
# 403 below (the backend refused the *scheme*) and to the no-credential hint.


def classify_verdict(system: str, status: Optional[int], identity: Optional[Identity],
                     auth_page: bool = False) -> Verdict:
    """Map (system, HTTP status, identity) to a truthful verdict. Never treats a
    bare 200 as success for the identity-based backends.

    ``auth_page`` is set by probe() when the response body was the portal gateway's
    authentication page: that turns a 401/403 from "the backend forbids this scheme"
    into "the credential failed to authenticate", which for a domain login means a
    stale password. The two need opposite advice, so they are separate categories.
    """
    if auth_page and status in (401, 403) and system not in IDENTITY_SYSTEMS:
        return Verdict(False, "stale_password", STALE_PASSWORD_DETAIL % status)
    if status is None:
        return Verdict(False, "network", "could not reach server (network/TLS). For an untrusted "
                       "corporate CA set the per-system *_INSECURE_SKIP_TLS_VERIFY (or "
                       "SWTR_INSECURE_SKIP_TLS_VERIFY) and re-run.")
    if system in IDENTITY_SYSTEMS:
        if status == 200:
            if identity is not None and identity.authenticated:
                return Verdict(True, "ok", "auth works.")
            token = "CONFLUENCE_BEARER_TOKEN" if system == "confluence" else "JIRA_BEARER_TOKEN"
            return Verdict(False, "anonymous", "reachable but NOT authenticated (anonymous or invalid "
                           "token). Set " + token + ".")
        if status == 401:
            return Verdict(False, "rejected", "401 -- credential rejected. Check the token is correct.")
        if status == 403:
            hint = " (a Jira PAT differs from a Confluence PAT)" if system == "jira" else ""
            return Verdict(False, "forbidden", "403 -- credential forbidden" + hint + ".")
        return Verdict(False, "inconclusive", "unexpected status %s; try --probe-path with a known read "
                       "endpoint." % status)
    # status-based systems: task_tracker, source_control
    if status == 200:
        return Verdict(True, "ok", "auth works.")
    if status == 401:
        return Verdict(False, "rejected", "401 -- credential rejected. Check login/password.")
    if status == 403:
        return Verdict(False, "forbidden", "403 -- credential well-formed but forbidden; the backend "
                       "likely rejects Basic-with-password -- switch to a Personal Access Token "
                       "(SWTR_BEARER_TOKEN / SWTR_AUTHORIZATION): " + PORTAL_TOKEN_HOWTO + ".")
    if status == 404:
        if system == "task_tracker":
            return Verdict(False, "inconclusive", "404 -- probe unit not found (the auth header was "
                           "accepted). Set SWTR_PROBE_UNIT to a real unit to confirm; NOT an auth verdict.")
        return Verdict(False, "inconclusive", "404 -- probe project not visible. Set "
                       "SOURCE_CONTROL_PROBE_TENANT / _PROJECT to a project you can see; NOT an auth verdict.")
    if 500 <= status < 600:
        return Verdict(False, "server_error", "%s -- server error, not an auth verdict." % status)
    return Verdict(False, "inconclusive", "unexpected status %s; try --probe-path with a real read "
                   "endpoint." % status)


def classify_write_verdict(status: Optional[int], auth_page: bool = False) -> Verdict:
    """Verdict for the write probe alone.

    The probe aims at a space that does not exist, so the *interesting* outcome is
    404: the POST was authenticated, routed and answered by the application, which
    is precisely what "the write path is not blocked" means. A 403 from the
    application is the case this skill used to mis-name as "Basic is forbidden" and
    answer with "get a PAT" -- a token changes nothing when the problem is rights.

    Anything that says nothing about writes stays ``ok`` so it cannot downgrade the
    read verdict: the read probe has already established that auth works.
    """
    if status is None:
        return Verdict(True, "ok", "write path not checked (network/TLS error on the write probe).")
    if auth_page:
        return Verdict(False, "stale_password", STALE_PASSWORD_DETAIL % status)
    if status == 404:
        return Verdict(True, "ok", "writes are NOT blocked -- the backend answered the POST. " +
                       WRITE_PROBE_CAVEAT)
    if status == 403:
        return Verdict(False, "no_write_permission", NO_WRITE_PERMISSION_DETAIL % status)
    if status == 401:
        return Verdict(False, "rejected", "401 on the write path -- credential rejected for writes.")
    if status == 200:
        return Verdict(False, "inconclusive", "200 from a probe that must never succeed: space %s "
                       "is not supposed to exist. Check the tracker for a stray unit and report this."
                       % WRITE_PROBE_SPACE)
    if 500 <= status < 600:
        return Verdict(True, "ok", "write path inconclusive (%s -- server error, not an auth verdict)."
                       % status)
    return Verdict(True, "ok", "write path inconclusive (unexpected status %s)." % status)


def _read_json(resp):
    raw = resp.read()
    if not raw.strip():
        return None
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"rawText": text}


class ProbeResult(NamedTuple):
    status: Optional[int]
    body: object
    note: str
    auth_page: bool


def probe(url: str, system: str, tls: TlsState, timeout: float = 20.0,
          method: str = "GET", body: Optional[dict] = None) -> ProbeResult:
    """Probe one endpoint. Captures status+body cleanly without raising; a
    network/TLS failure returns status None.

    The error body is read, not discarded: it is the only thing that tells the
    portal gateway's authentication page apart from an application-level 403, and
    those two need opposite advice (new password vs. Personal Access Token).

    ``method``/``body`` exist for the write probe. Content-Type is attached only
    when there is a body: sending it on a bodyless GET makes some gateways reject
    the request outright.
    """
    headers = build_headers(system, require_auth=True)
    data = None
    if body is not None:
        headers = dict(headers)
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url=url, headers=headers, method=method, data=data)
    try:
        if tls.context is None:
            resp = urllib.request.urlopen(req, timeout=timeout)
        else:
            resp = urllib.request.urlopen(req, timeout=timeout, context=tls.context)
        with resp:
            return ProbeResult(resp.status, _read_json(resp), "reachable", False)
    except urllib.error.HTTPError as exc:
        body_text, content_type = http_error_details(exc)
        auth_page = is_gateway_auth_failure(exc.code, content_type, body_text)
        # An empty reason is common on the portal's error responses; "404  ()" reads
        # like the probe itself broke, so say who answered instead.
        note = ("portal login gateway rejected the credential" if auth_page
                else (str(exc.reason) or "answered by the application"))
        return ProbeResult(exc.code, None, note, auth_page)
    except urllib.error.URLError as exc:
        return ProbeResult(None, None, "network/TLS error: %s" % (exc.reason,), False)
    except (socket.timeout, TimeoutError, OSError) as exc:
        return ProbeResult(None, None, "network/TLS error: %s" % (exc,), False)
    except Exception as exc:
        # Catch-all: e.g. http.client.IncompleteRead on a truncated body is
        # neither an OSError nor a URLError. Per the isolation guarantee, a
        # probe failure must always become a clean network verdict, never an
        # uncaught traceback.
        return ProbeResult(None, None, "network/TLS error: %s" % (exc,), False)


ALL_SYSTEMS = ("task_tracker", "source_control", "confluence", "jira")
SINGLE_EXIT = {"ok": 0, "forbidden": 3, "network": 4, "stale_password": 5,
               "no_write_permission": 6}

# The write probe aims at a space that cannot exist. The backend resolves the
# space before anything is persisted, so the request proves the write path is
# reachable while leaving no trace in the tracker: diagnostics must never create
# objects in a corporate system to find out whether they can.
WRITE_PROBE_SPACE = "__guru_doctor_probe__"
WRITE_PROBE_SUIT = "task"
WRITE_PROBE_PATH = "/rest/api/unit/v2/%s/create" % WRITE_PROBE_SUIT
WRITE_PROBE_CAVEAT = ("rights to write into a REAL space are NOT covered by this probe -- "
                      "it only shows the write path is not blocked before the application.")

NO_WRITE_PERMISSION_DETAIL = (
    "%s -- the credential authenticated, but the backend refuses the write itself. "
    "That is a permissions problem in the target space, NOT an auth-scheme problem: "
    "a Personal Access Token will not change it. Ask for write rights in the space."
)

# Systems guru_env_setup.py can repair, and the verdicts worth repairing.
# 404 ("inconclusive") is deliberately absent: it is a verdict about the probe
# target, not about the credential, so new credentials would change nothing.
# "stale_password" is the most repairable of all -- a new password is exactly
# what guru_env_setup.py writes.
FIXABLE_SYSTEMS = ("task_tracker", "source_control")
FIXABLE_CATEGORIES = ("no_credential", "rejected", "forbidden", "stale_password")


def print_fixable(verdicts) -> list:
    """Print the FIXABLE line (when non-empty) and return the systems it names.

    The /guru_doctor command reads this line to decide whether to offer repair.
    """
    fixable = [s for s in FIXABLE_SYSTEMS
               if s in verdicts and verdicts[s].category in FIXABLE_CATEGORIES]
    if fixable:
        print("FIXABLE: " + " ".join(fixable))
    return fixable


def _print_auth_line(system: str, auth: str) -> None:
    scheme = auth.split(" ", 1)[0] if " " in auth else "Raw"
    if scheme == "Basic":
        import base64
        try:
            decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8", "replace")
        except Exception:
            print("  auth scheme  : Basic  (credential not decodable)")
            return
        login, _, pwd = decoded.partition(":")
        print("  auth scheme  : Basic  (login len=%d; password %s)" % (len(login), classify(pwd)))
    else:
        print("  auth scheme  : %s" % scheme)


def check_system(system: str, base_url_arg, probe_path_arg, timeout: float, no_network: bool,
                 no_write_probe: bool = False) -> Verdict:
    """Print one integration-point block and return its Verdict. Never raises on a
    missing credential: that becomes a 'no credential' block."""
    print("=== %s ===" % system)
    base_url = base_url_arg or base_url_for(system)
    print("  base url     : %s" % base_url)
    auth = build_authorization(system)
    if not auth:
        print("  auth scheme  : NONE -- no credential found")
        print("  VERDICT      : no credential. Set %s" % PRIMARY_HINT.get(system, "the system's auth env"))
        return Verdict(False, "no_credential", "no credential")
    _print_auth_line(system, auth)

    tls = doctor_tls_state(system)
    print("  tls          : %s" % ("verified" if tls.verified else "verification OFF (%s=1)" % tls.flag))
    if not tls.verified and system in IDENTITY_SYSTEMS and tls.flag == "SWTR_INSECURE_SKIP_TLS_VERIFY":
        tool_file, own_flag = RUNTIME_TLS_FLAG[system]
        print("  note         : TLS skipped via global SWTR_INSECURE_SKIP_TLS_VERIFY; the runtime "
              "%s honors only %s -- set it there too if that tool must skip verification."
              % (tool_file, own_flag))

    if no_network:
        print("  network      : skipped (--no-network)")
        return Verdict(True, "skipped", "network skipped")

    probe_path = probe_path_for(system, probe_path_arg)
    url = base_url.rstrip("/") + ("/" + probe_path.lstrip("/") if probe_path else "")
    status, body, note, auth_page = probe(url, system, tls, timeout)
    print("  probe (read) : GET %s" % url)
    print("  http status  : %s  (%s)" % (status, note))

    identity = extract_identity(system, body) if (system in IDENTITY_SYSTEMS and status == 200) else None
    if system in IDENTITY_SYSTEMS:
        if identity is not None and identity.authenticated:
            print("  identity     : authenticated (%s)" % identity.display)
        elif status == 200:
            print("  identity     : ANONYMOUS / none")
        else:
            print("  identity     : --")
    else:
        print("  identity     : --")

    verdict = classify_verdict(system, status, identity, auth_page)
    print("  VERDICT      : %s" % verdict.detail)

    # The read probe alone reported "auth works" while a write could still be
    # refused -- which reads as "the scheme forbids writing" and sends people to
    # reissue credentials. Only worth asking once reading is known to work.
    if system == "task_tracker" and verdict.ok and not no_write_probe:
        write_url = base_url.rstrip("/") + WRITE_PROBE_PATH
        w_status, _w_body, w_note, w_auth_page = probe(
            write_url, system, tls, timeout, method="POST",
            body={"summary": "guru_doctor write probe", "description": "",
                  "space": WRITE_PROBE_SPACE, "attributes": {}},
        )
        print("  probe (write): POST %s" % write_url)
        print("  http status  : %s  (%s)" % (w_status, w_note))
        write_verdict = classify_write_verdict(w_status, w_auth_page)
        print("  WRITE VERDICT: %s" % write_verdict.detail)
        if not write_verdict.ok:
            verdict = write_verdict
    print("")
    return verdict


def _ascii_fold(text: str) -> str:
    """Replace every non-ASCII character with ``?`` so the line survives a Windows pipe."""
    return text.encode("ascii", "replace").decode("ascii")


def _one_line(value: str, limit: int = 60) -> str:
    """Collapse whitespace and cut to ``limit`` -- a printed value stays ONE line.

    The block is read back line by line, so a value carrying a newline could
    forge lines of its own ("login : root"). Nothing typed by a user is printed
    without going through here first.
    """
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "..."


def _path_line(path: Path) -> str:
    """A file path made safe for a line of this ASCII-only report."""
    return _ascii_fold(_one_line(str(path), 160))


def profile_state_block(home: Optional[Path] = None) -> None:
    """User profile (defaults) status: ``~/.config/pvm-guru/profile.json``.

    Printed after the update block and, like it, never touches the access
    SUMMARY, the FIXABLE line or the exit code: without a usable profile the
    tools take spaces, logins and tenants from flags and environment -- exactly
    what they did before the profile existed -- so this is a separate diagnosis.
    Reads through load_profile(), so the doctor and the tools agree on what
    counts as usable. Works with --no-network: it is a local file.

    ASCII only, like the rest of this file. Profile values are typed by the
    user, so only ASCII-safe facts are printed: the state, the path, the save
    time, whether login and primary space are filled in (yes/no) and the primary
    space code, which the schema keeps to upper-case ASCII. The login itself is
    never echoed: the doctor does not print identities. Every value that DOES
    reach a line goes through _ascii_fold(_one_line(...)) first -- the block is
    parsed line by line, so a value with a newline in it must not be able to
    forge one, and the path is no exception: it carries the home directory of
    the user, and that name is not ours to assume ASCII.
    """
    loaded = load_profile(home)

    print("")
    print("profile state")

    # A state without the next step is useless, so every branch names one:
    # /guru-start fills the file in, /guru_profile shows and changes it.
    if loaded.status == "missing":
        print("  file          : missing -- no defaults saved, every tool behaves as before")
        print("  path          : %s" % _path_line(loaded.path))
        print("  fill in       : run /guru-start (a few questions, then the file is saved); "
              "/guru_profile shows the map")
        return

    if loaded.status != "ok":
        # corrupt or unsupported: the tools silently ignore the file, and the
        # user who filled it in sees only "my defaults do not apply". That is
        # worth a loud line, with the reason, the path and the way out.
        print("  !! file       : %s -- %s"
              % (loaded.status, _ascii_fold(_one_line(loaded.reason or "no reason recorded", 160))))
        print("     path       : %s" % _path_line(loaded.path))
        if loaded.status == "unsupported":
            print("     fix        : the file is ignored until the skills are updated "
                  "(npx skills update -g) or the profile is reset via /guru_profile")
        else:
            print("     fix        : the file is ignored until repaired -- fix the JSON by hand "
                  "or reset it via /guru_profile, then run /guru-start again")
        return

    # Three different facts, three different lines. validate_profile() checks
    # only the TYPE of the stamp, so a value this report cannot print reaches
    # here with status ok, and calling it "no stamp" would report as fact
    # something that is not so -- the file does carry one.
    stamp = loaded.data.get("updated_at")
    if not isinstance(stamp, str) or not stamp.strip():
        updated_at = "unknown (no updated_at stamp)"
    elif not stamp.isascii():
        updated_at = "present (non-ascii, not shown)"
    else:
        updated_at = _one_line(stamp)
    primary_space = profile_get(loaded, "task_tracker.primary_space")
    if primary_space is None:
        space_line = "no"
    elif isinstance(primary_space, str) and primary_space.isascii():
        space_line = "yes (%s)" % _one_line(primary_space)
    else:
        space_line = "yes"

    print("  file          : present")
    print("  path          : %s" % _path_line(loaded.path))
    print("  saved at      : %s" % updated_at)
    print("  login         : %s" % ("yes" if profile_get(loaded, "login") is not None else "no"))
    print("  primary space : %s" % space_line)
    print("  edit          : /guru_profile shows and changes the map; /guru-start walks through it again")


def main() -> int:
    ap = argparse.ArgumentParser(description="Diagnose SWTR access safely (no secrets printed).")
    ap.add_argument("--env-file", default="~/.swtr-review.env")
    ap.add_argument("--system", choices=ALL_SYSTEMS, default=None,
                    help="check one system; omit to check all four")
    ap.add_argument("--base-url", default=None, help="override base URL (single --system only)")
    ap.add_argument("--probe-path", default=None, help="override probe path (single --system only)")
    ap.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout seconds (default 20)")
    ap.add_argument("--no-network", action="store_true", help="skip the live HTTP probe")
    ap.add_argument("--no-write-probe", action="store_true",
                    help="skip the task_tracker write probe (it creates nothing: the POST targets "
                         "a space that does not exist)")
    args = ap.parse_args()

    env_path = Path(args.env_file).expanduser()
    print("env file        : %s" % env_path)
    print("  encoding      : %s" % detect_encoding(env_path))
    if detect_encoding(env_path).startswith("utf-16"):
        print("  !! save as UTF-8 (notepad: 'Save as' -> Encoding: UTF-8), not Unicode/UTF-16")
    load_env_file(env_path)
    load_env_file(Path("~/.sberworks.env").expanduser())
    print("")

    if args.system is not None:
        verdict = check_system(args.system, args.base_url, args.probe_path, args.timeout,
                               args.no_network, args.no_write_probe)
        print_fixable({args.system: verdict})
        if verdict.category == "no_credential":
            return 2
        return SINGLE_EXIT.get(verdict.category, 0 if verdict.ok else 1)

    # All-systems mode: --base-url / --probe-path are single-system only. Per
    # the design guarantee, one system's problem must never abort the whole
    # run, so an unexpected exception from check_system() is caught, redacted,
    # and turned into an "error" verdict for that system alone; the loop moves
    # on to the next system. (Single --system mode has no such promise and is
    # intentionally left unwrapped.)
    verdicts = {}
    for system in ALL_SYSTEMS:
        try:
            verdicts[system] = check_system(system, None, None, args.timeout, args.no_network,
                                            args.no_write_probe)
        except Exception as exc:
            print("  VERDICT      : error -- %s" % redact_secrets(str(exc)))
            print("")
            verdicts[system] = Verdict(False, "error", "unexpected error: %s" % redact_secrets(str(exc)))
    summary = "   ".join(
        "%s %s" % (s, "[OK]" if verdicts[s].ok else "[FAIL:%s]" % verdicts[s].category)
        for s in ALL_SYSTEMS
    )
    print("SUMMARY  " + summary)
    print_fixable(verdicts)
    # Printed after the access summary and never changes the exit code: stale
    # skills, an empty profile and a broken credential are different diagnoses.
    profile_state_block()
    return 0 if all(v.ok for v in verdicts.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
