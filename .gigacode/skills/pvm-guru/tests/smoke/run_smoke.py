#!/usr/bin/env python3
"""Offline smoke suite for the pvm-guru skill (stdlib-only, no network).

The skill's scripts are stdlib-only; this suite is too. It never opens a socket:
``urllib.request.urlopen`` is replaced by a fixture transport, so every HTTP call
is served from ``fixtures/*.json`` (anonymized API responses, no real employee
data or tokens). Each script's ``main()`` runs in-process; stdout/stderr and the
exit code are captured.

What it checks (Task 1 -- safety net):
  * happy-path: every subcommand of every tool prints valid JSON with the
    expected top-level fields;
  * dry-run write commands (create / comment / update without --execute) perform
    NO mutating network call and print the payload;
  * error-path (Task 3 unified contract): a 401 response AND a network timeout
    each yield the same shape on stderr -- exit 1 + a JSON object
    ``{"ok": false, "command": <name>, "error": <redacted>, ...extra}`` (via
    _swtr_common.emit_error); each of the seven CLI tools is checked;
  * secrets never leak: a token echoed in a 401 body is redacted;
  * ``--help`` exits 0 for every script.

Run: python3 tests/smoke/run_smoke.py
Exit code is non-zero if any case fails.
"""

from __future__ import annotations

import base64
import contextlib
import email.message
import importlib
import io
import json
import os
import re
import socket
import ssl
import sys
import tempfile
import traceback
import urllib.error
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
FIXTURES_DIR = HERE / "fixtures"
SKILL_ROOT = HERE.parent.parent            # .../skills/pvm-guru
SCRIPTS_DIR = SKILL_ROOT / "scripts"

# --------------------------------------------------------------------------
# Hermetic environment: a throwaway HOME so ~/.swtr-review.env and
# ~/.sberworks.env can never resolve to a real user's file, fake credentials so
# build_authorization() succeeds, and test base URLs. Any ambient secret env
# vars are dropped first so the run is fully deterministic and no real
# credential can influence auth or redaction.
# --------------------------------------------------------------------------
_TMP_HOME = tempfile.mkdtemp(prefix="pvm-guru-smoke-home-")
_TMP_DIR = tempfile.mkdtemp(prefix="pvm-guru-smoke-")

_SECRET_ENV_NAMES = (
    "SWTR_AUTHORIZATION", "TASKTRACKER_AUTHORIZATION", "SWTR_BASIC_AUTH",
    "SWTR_BEARER_TOKEN", "SWTR_TOKEN", "TASKTRACKER_TOKEN", "SWTR_COOKIE",
    "SOURCE_CONTROL_AUTHORIZATION", "SC_AUTHORIZATION", "SOURCE_CONTROL_BASIC_AUTH",
    "SC_BASIC_AUTH", "SOURCE_CONTROL_BEARER_TOKEN", "SC_BEARER_TOKEN",
    "SOURCE_CONTROL_TOKEN", "SC_TOKEN", "SOURCE_CONTROL_COOKIE", "SC_COOKIE",
    "CONFLUENCE_AUTHORIZATION", "CONFLUENCE_BEARER_TOKEN", "CONFLUENCE_TOKEN",
    "CONFLUENCE_BASIC_AUTH", "CONFLUENCE_COOKIE", "SBERWORKS_TOKEN",
    "JIRA_AUTHORIZATION", "JIRA_BEARER_TOKEN", "JIRA_TOKEN", "JIRA_BASIC_AUTH", "JIRA_COOKIE",
    "SWTR_BASE_URL", "SOURCE_CONTROL_BASE_URL", "SC_BASE_URL",
    "CONFLUENCE_BASE_URL", "JIRA_BASE_URL",
    "SOURCE_CONTROL_REUSE_SWTR_AUTH", "SC_REUSE_SWTR_AUTH",
    "SWTR_INSECURE_SKIP_TLS_VERIFY", "SOURCE_CONTROL_INSECURE_SKIP_TLS_VERIFY",
    "SC_INSECURE_SKIP_TLS_VERIFY", "CONFLUENCE_INSECURE_SKIP_TLS_VERIFY",
    "JIRA_INSECURE_SKIP_TLS_VERIFY", "SWTR_LOGIN",
)
for _name in _SECRET_ENV_NAMES:
    os.environ.pop(_name, None)

# Distinctive fake tokens so redaction assertions are unambiguous.
SWTR_TOKEN = "SMOKESWTRTOKEN"
os.environ.update({
    # Path.home() читает HOME только на POSIX; на Windows ntpath.expanduser берёт
    # USERPROFILE (или HOMEDRIVE+HOMEPATH). Без всех четырёх «герметичный» набор
    # читал бы настоящий профиль разработчика и применял его логин и
    # пространства ко всем 240+ кейсам.
    "HOME": _TMP_HOME,
    "USERPROFILE": _TMP_HOME,
    "HOMEDRIVE": "",
    "HOMEPATH": _TMP_HOME,
    "SWTR_BEARER_TOKEN": SWTR_TOKEN,
    "SOURCE_CONTROL_BEARER_TOKEN": "SMOKESCTOKEN",
    "CONFLUENCE_BEARER_TOKEN": "SMOKECONFTOKEN",
    "JIRA_BEARER_TOKEN": "SMOKEJIRATOKEN",
    "SWTR_BASE_URL": "https://swtr.smoke.test/swtr",
    "SOURCE_CONTROL_BASE_URL": "https://sc.smoke.test/api/v3",
    "CONFLUENCE_BASE_URL": "https://conf.smoke.test/wiki",
    "JIRA_BASE_URL": "https://jira.smoke.test/jira",
})

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# Files consumed by dry-run write commands.
BODY_FILE = Path(_TMP_DIR) / "comment-body.txt"
BODY_FILE.write_text("Synthetic smoke comment body.\n", encoding="utf-8")
STORAGE_FILE = Path(_TMP_DIR) / "storage.xhtml"
STORAGE_FILE.write_text("<p>Synthetic smoke storage body.</p>\n", encoding="utf-8")

# --------------------------------------------------------------------------
# Fixtures + routing transport
# --------------------------------------------------------------------------
FIX = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in FIXTURES_DIR.glob("*.json")}


def route_fixture(url: str):
    """Return the fixture body for a request URL (most-specific match first)."""
    u = url
    if "/tokens" in u:
        return FIX["sc_token_created"]
    if "/rest/api/unit-comment/" in u:
        return FIX["swtr_comments"]
    if "swtr_tms_plugin" in u:
        return FIX["swtr_tms"]
    if "swtr_wiki_plugin" in u:
        return FIX["swtr_wiki"]
    # Workflow routes go before the unit ones: the status machine lives under
    # /rest/api/workflow/, and its own paths contain "/unit/" too.
    if "/rest/api/workflow/v1/unit/" in u and "/status/get" in u:
        return FIX["swtr_status_get"]
    if "/rest/api/workflow/v2/unit/status/change" in u:
        return FIX["swtr_status_get"]
    if "/rest/api/workflow/v1/workflow/" in u:
        return FIX["swtr_workflow_schema"]
    # Схема по паре (suit, space) — та же форма ответа, что и схема по коду.
    if "/rest/api/workflow/v1/suit/" in u and u.rstrip("/").endswith("/workflow"):
        return FIX["swtr_workflow_schema"]
    if "swtr_task_tracker_plugin" in u and "/version/" in u:
        return FIX["swtr_versions"]
    if "sber_components_plugin" in u:
        return FIX["swtr_components"]
    if "/rest/api/suit/v1/" in u:
        return FIX["swtr_suit_schema"]
    # Ветки ниже стоят перед общими `/rest/api/unit/v2/` и `/rest/api/space/`:
    # иначе список фильтров уедет в фикстуру юнита, а список пространств —
    # в конфлюенсовую.
    if "/rest/api/unit/v2/filters" in u:
        return FIX["swtr_filters"]
    if "/rest/api/unit/v2/update/" in u:
        return FIX["generic"]
    if "/rest/api/space/v3/find" in u:
        return FIX["swtr_spaces"]
    if "/rest/api/user/v1/find" in u:
        return FIX["swtr_user_find"]
    if "/rest/api/unit/v3/count" in u:
        return FIX["swtr_count"]
    if "/rest/api/unit/v3/find" in u:
        return FIX["swtr_find"]
    if "/rest/api/space/v3/" in u and u.rstrip("/").endswith("/suits"):
        return FIX["swtr_suits"]
    if "/rest/api/unit/v1/link/find" in u:
        return FIX["swtr_link_find"]
    if "/rest/api/unit/v1/link" in u:
        return FIX["generic"]
    if "/rest/api/unit/v2/" in u:
        return FIX["swtr_unit"]
    if "/rest/api/2/search" in u:
        return FIX["jira_search"]
    if "/rest/api/2/issue/" in u and u.rstrip("/").endswith("/comment"):
        return FIX["jira_comments"]
    if "/rest/api/2/issue/" in u:
        return FIX["jira_issue"]
    if "/rest/api/2/myself" in u:
        return FIX["jira_myself"]
    if "/rest/api/user/current" in u:
        return FIX["confluence_user_current"]
    if "/rest/api/content/search" in u:
        return FIX["confluence_search"]
    if "/rest/api/content/" in u:
        return FIX["confluence_content"]
    if "/rest/api/space/" in u:
        return FIX["confluence_space"]
    if "/repos/" in u:
        return FIX["source_control_repos"]
    return FIX["generic"]


class _FakeHeaders:
    def __init__(self, d):
        self._d = dict(d)

    def items(self):
        return list(self._d.items())

    def get(self, key, default=None):
        return self._d.get(key, default)


class _FakeResponse:
    def __init__(self, status, payload, extra_headers=None):
        self.status = status
        self._payload = payload
        hdrs = {"Content-Type": "application/json; charset=utf-8"}
        if extra_headers:
            hdrs.update(extra_headers)
        self.headers = _FakeHeaders(hdrs)

    def read(self):
        if isinstance(self._payload, (bytes, bytearray)):
            return bytes(self._payload)
        return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(url: str, status: int, body, content_type="application/json"):
    payload = body if isinstance(body, (bytes, bytearray)) else json.dumps(body, ensure_ascii=False).encode("utf-8")
    hdrs = email.message.Message()
    hdrs["Content-Type"] = content_type
    return urllib.error.HTTPError(url, status, "Smoke error", hdrs, io.BytesIO(payload))


# The portal's IAM gateway (SynGX + direct-auth) answers a failed Basic credential
# with THIS page under HTTP 403 -- the request never reaches SWTR. Captured live on
# 2026-08-05 from https://portal.works.prod.sbt/swtr with a deliberately wrong
# credential. It is the whole reason a stale domain password used to be reported as
# "the backend refuses Basic, get a PAT".
GATEWAY_AUTH_PAGE = (
    "<!DOCTYPE html>\n<html>\n<head>\n"
    '    <meta http-equiv="Content-Type" content="text/html; charset=UTF-8">\n'
    "    <title>Ошибка при аутентификации</title>\n</head>\n<body>\n"
    '<div class="main">\n    <div class="msg">\n'
    "        <h1>Ошибка при аутентификации</h1>\n"
    "        Error auth by direct-auth. response indicates failure, status=401, "
    'body={"error":"invalid_request","error_description":"Missing parameter: username"}\n'
    '        <p>Повторите <a href="/">вход в систему еще раз</a></p>\n'
    "        <hr>\n        <em>Auth Platform team.</em>\n"
    "    </div>\n</div>\n</body>\n</html>\n"
)
GATEWAY_AUTH_PAGE_BYTES = GATEWAY_AUTH_PAGE.encode("utf-8")
GATEWAY_HTML_CONTENT_TYPE = "text/html; charset=utf-8"


class Transport:
    """Callable that stands in for urllib.request.urlopen.

    mode="ok"      -> serve the routed fixture with the given status.
    mode="error"   -> raise an HTTPError with `status` and `error_body`.
    mode="timeout" -> raise socket.timeout on every call (request_http retries
                      then surfaces a redacted RuntimeError -> emit_error).
    ``timeout_methods`` -> raise socket.timeout only for those HTTP methods
                      (e.g. {"PUT"}); everything else serves normally, so a
                      write that follows a successful read can time out on the
                      write alone -- used to prove writes do NOT retry.
    ``response_headers`` -> extra headers merged into the served response (e.g.
                      a Set-Cookie) so header-redaction can be exercised.
    Every call is recorded -- including the ``timeout`` urlopen was invoked with
    -- so tests can assert (no) network activity AND that a custom --timeout is
    actually threaded through to urlopen(timeout=...).
    """

    def __init__(self, mode="ok", status=200, error_body=None, response_headers=None, timeout_methods=None,
                 error_content_type="application/json"):
        self.mode = mode
        self.status = status
        self.error_body = error_body if error_body is not None else FIX["error_401"]
        self.response_headers = response_headers
        self.timeout_methods = set(timeout_methods) if timeout_methods else None
        # An error body is not always JSON: the portal gateway answers a failed
        # credential with an HTML page, and that content type is what tells an
        # auth-failure page apart from an application-level verdict.
        self.error_content_type = error_content_type
        self.calls = []

    def __call__(self, request, timeout=None, context=None):
        method = request.get_method()
        url = request.full_url
        body = None
        if request.data:
            try:
                body = json.loads(request.data.decode("utf-8"))
            except Exception:
                body = "<non-json body>"
        self.calls.append({"method": method, "url": url, "body": body, "timeout": timeout})
        if self.mode == "timeout" or (self.timeout_methods and method in self.timeout_methods):
            raise socket.timeout("timed out")
        if self.mode == "error":
            raise _http_error(url, self.status, self.error_body, self.error_content_type)
        # The doctor's write probe aims at a space that cannot exist, and the live
        # backend answers 404 with a JSON body. Serving it a blanket 200 would have
        # the fixture claim a unit was created in a non-existent space -- the doctor
        # is right to shout at that, so the fixture must not tell it that lie.
        if method == "POST" and url.endswith("/create") and isinstance(body, dict) \
                and str(body.get("space", "")).startswith("__"):
            raise _http_error(url, 404, {"uiErrorMessage": "Элемент 'Space' не найден",
                                         "errorType": "SWTR_BASIC_ERROR"}, "application/json")
        return _FakeResponse(self.status, route_fixture(url), self.response_headers)


NO_ARGV = {"guru_doctor"}  # guru_doctor.main() reads sys.argv (no argv parameter)


def _run_main(module, argv, uses_argv):
    old_argv = sys.argv
    sys.argv = [getattr(module, "__name__", "smoke")] + [str(a) for a in argv]
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                ret = module.main(list(argv)) if uses_argv else module.main()
                rc = ret if isinstance(ret, int) else 0
            except SystemExit as exc:
                code = exc.code
                if isinstance(code, str):
                    # Print the error message to stderr as Python would do at exit
                    err.write(code)
                    if not code.endswith("\n"):
                        err.write("\n")
                    rc = 1
                else:
                    rc = 0 if code is None else (code if isinstance(code, int) else 1)
    finally:
        sys.argv = old_argv
    return rc, out.getvalue(), err.getvalue()


def run(module_name, argv, transport):
    module = importlib.import_module(module_name)
    uses_argv = module_name not in NO_ARGV
    with mock.patch("urllib.request.urlopen", side_effect=transport), \
            mock.patch("time.sleep", lambda *a, **k: None):
        return _run_main(module, argv, uses_argv)


# --------------------------------------------------------------------------
# Test registry
# --------------------------------------------------------------------------
TESTS = []


def register(name, fn):
    TESTS.append((name, fn))


def add_happy(module, argv, expect=None, has_keys=None, label=None):
    expect = expect or {}
    has_keys = has_keys or []

    def _t(module=module, argv=argv, expect=expect, has_keys=has_keys):
        transport = Transport("ok")
        rc, out, err = run(module, argv, transport)
        assert rc == 0, f"expected rc=0, got {rc}; stderr={err[:400]!r}"
        data = json.loads(out)  # must be valid JSON
        for key, val in expect.items():
            assert data.get(key) == val, f"{key}={data.get(key)!r} != {val!r}; stdout={out[:300]}"
        for key in has_keys:
            assert key in data, f"missing key {key!r}; stdout={out[:300]}"

    register(label or f"[happy] {module} {' '.join(str(a) for a in argv[:3])}", _t)


def add_help(module):
    def _t(module=module):
        rc, out, err = run(module, ["--help"], Transport("ok"))
        assert rc == 0, f"--help expected rc=0, got {rc}; stderr={err[:300]!r}"
        assert out.strip(), "--help produced no output"

    register(f"[help] {module} --help", _t)


def _assert_unified_error_contract(out, err, rc, *, kind):
    """A backend failure must exit 1 and print the unified error contract to
    stderr: {"ok": false, "command": <non-empty str>, "error": <non-empty str>}.
    Nothing is printed to stdout.
    """
    assert rc == 1, f"{kind} expected rc=1, got {rc}; stdout={out[:300]!r} stderr={err[:300]!r}"
    assert not out.strip(), f"{kind}: error path must not print to stdout; got {out[:300]!r}"
    payload = json.loads(err.strip())  # error contract is a JSON object on stderr
    assert payload.get("ok") is False, f"{kind}: expected ok=false, got {payload!r}"
    command = payload.get("command")
    assert isinstance(command, str) and command, f"{kind}: expected non-empty command, got {payload!r}"
    error = payload.get("error")
    assert isinstance(error, str) and error, f"{kind}: expected non-empty error string, got {payload!r}"
    assert payload.get("gate") != "sberworks-not-default", (
        f"{kind}: this case must exercise its real failure path, but the sberworks gate "
        f"fired instead -- the call site is missing --confirm-sberworks; payload={payload!r}"
    )
    return payload


def add_error_401(module, argv, label=None):
    def _t(module=module, argv=argv):
        rc, out, err = run(module, argv, Transport("error", status=401))
        _assert_unified_error_contract(out, err, rc, kind="401")

    register(label or f"[401] {module} {' '.join(str(a) for a in argv[:3])}", _t)


def add_error_timeout(module, argv, label=None):
    def _t(module=module, argv=argv):
        rc, out, err = run(module, argv, Transport("timeout"))
        _assert_unified_error_contract(out, err, rc, kind="timeout")

    register(label or f"[timeout] {module} {' '.join(str(a) for a in argv[:3])}", _t)


def add_error_401_token_redacted(module, argv, token, label=None):
    """A 401 whose body echoes the tool's OWN configured token must never leak
    it: the redacted error contract is proven per-tool, not only for one tool."""
    def _t(module=module, argv=argv, token=token):
        body = {"errorCode": "UNAUTHORIZED", "message": f"rejected token Bearer {token}"}
        rc, out, err = run(module, argv, Transport("error", status=401, error_body=body))
        payload = _assert_unified_error_contract(out, err, rc, kind="401-token")
        assert token not in (out + err), f"{module}: secret token leaked into error output: {(out + err)[:300]!r}"
        assert "redacted" in payload.get("error", ""), f"{module}: error field not redacted: {payload!r}"

    register(label or f"[secrets] {module} 401 token redacted", _t)


# ---- happy path: swtr_task_tool ----
add_happy("swtr_task_tool", ["get", "SMOKE-1"], expect={"command": "get", "mode": "code", "code": "SMOKE-1"}, has_keys=["summary", "unit"])
add_happy("swtr_task_tool", ["search", "payment"], expect={"command": "search"}, has_keys=["units", "wiki"])
add_happy("swtr_task_tool", ["count", "payment"], expect={"command": "count", "matched": 7}, has_keys=["request", "response"])
add_happy("swtr_task_tool", ["similar", "payment gateway integration"], expect={"command": "similar"}, has_keys=["terms", "items"])
add_happy("swtr_task_tool", ["assigned", "--user", "user.one"], expect={"command": "assigned"}, has_keys=["users", "response"])
add_happy("swtr_task_tool", ["done-weekly", "--user", "user.one"], expect={"command": "done-weekly"}, has_keys=["users", "response"])
add_happy("swtr_task_tool", ["suits", "PVM"],
          expect={"command": "suits", "space": "PVM"}, has_keys=["suits", "response"],
          label="[happy] swtr_task_tool suits PVM")


def _suits_parses_codes():
    # Real API shape is {"code": <space>, "suits": [{"code","name",...}]} — assert
    # cmd_suits extracts the codes (a bare-list fixture would have hidden this).
    transport = Transport("ok")
    rc, out, err = run("swtr_task_tool", ["suits", "PVM"], transport)
    assert rc == 0, f"rc={rc}; stderr={err[:300]!r}"
    data = json.loads(out)
    codes = {s["code"] for s in data["suits"]}
    assert {"epic", "story", "task"} <= codes, f"expected epic/story/task in parsed suits, got {codes}"


register("[suits-parse] swtr_task_tool suits extracts codes from {suits:[...]}", _suits_parses_codes)

# ---- TMS reference comparison: exact endpoint + request body per kind ----
# Task 5 locks the table-driven cmd_tms output. For each tms-kind we assert the
# emitted {command, kind, endpoint, data} against a hand-written reference: the
# exact endpoint and, for POST kinds, the exact request body (the response half
# comes from the shared swtr_tms fixture). GET kinds emit the raw response.
# These expected values are duplicated here on purpose (independent of the code
# under test) so an accidental change to an endpoint, default attribute set or
# body shape is caught. Kept in sync manually with swtr_task_tool.TMS_KINDS.
_TB = "/extension/plugin/v2/rest/api/swtr_tms_plugin/v1"
_TMS_PLAN_ATTRS = ["test_plan_status", "release", "owner", "plan_date_start", "plan_date_end",
                   "type_of_testing", "folder"]
_TMS_CYCLE_ATTRS = ["test_cycle_status", "plan_date_end", "plan_date_start", "release", "owner",
                    "iteration_type", "cycles_number", "type_of_testing", "test_case_test_type",
                    "old_jira_key", "folder"]
_TMS_CASE_ATTRS = ["status", "test_case_test_type", "priority", "owner", "labels", "folder"]
_PAGE = {"page": 0, "size": 20}
_ROOT_RQ = {"type": "TEST_CASE", "spaceId": {"code": "SMOKE"}}

# (argv, kind, expected_endpoint, expected_request)  -- request is None for GET kinds.
_TMS_REFERENCE = [
    (["tms", "acceptable-spaces"], "acceptable-spaces", _TB + "/test_plans/acceptable_spaces", None),
    (["tms", "test-plans", "--space", "SMOKE"], "test-plans", _TB + "/test_plans",
     {"unitFilters": {"filters": {"spaces": ["SMOKE"]}, "attributes": _TMS_PLAN_ATTRS, "page": _PAGE}}),
    (["tms", "plan-cycles", "--plan", "SMOKE-TP-1"], "plan-cycles", _TB + "/link/test_plan/test_cycles",
     {"unitId": {"code": "SMOKE-TP-1"}, "unitFilters": {"attributes": _TMS_CYCLE_ATTRS, "page": _PAGE}}),
    (["tms", "cycle-cases", "--cycle", "SMOKE-TC-1"], "cycle-cases", _TB + "/link/test_cycle/test_cases",
     {"unitId": {"code": "SMOKE-TC-1"}, "unitFilters": {"attributes": _TMS_CASE_ATTRS, "page": _PAGE}}),
    (["tms", "folder-root", "--space", "SMOKE"], "folder-root", _TB + "/folder/root", dict(_ROOT_RQ)),
    (["tms", "folder-root-units", "--space", "SMOKE"], "folder-root-units", _TB + "/folder/root/units",
     {"getRootFolderRq": dict(_ROOT_RQ), "unitFilters": {"attributes": _TMS_CASE_ATTRS, "page": _PAGE}}),
    (["tms", "folder-root-progress", "--space", "SMOKE"], "folder-root-progress", _TB + "/folder/root/units/progress",
     {"getRootFolderRq": dict(_ROOT_RQ), "unitFilters": {"attributes": _TMS_CASE_ATTRS, "page": _PAGE}}),
    (["tms", "folder-hierarchy", "--folder", "SMOKE-F-1"], "folder-hierarchy", _TB + "/folder/hierarchy/SMOKE-F-1/units",
     {"filters": {}, "attributes": _TMS_CASE_ATTRS, "page": _PAGE}),
    (["tms", "test-case", "--unit", "SMOKE-U-1"], "test-case", _TB + "/test_case/SMOKE-U-1/test_case_data", None),
    (["tms", "run-info", "--run", "SMOKE-R-1"], "run-info", _TB + "/run/SMOKE-R-1/test_step_info", None),
    (["tms", "integration"], "integration", _TB + "/integration/units",
     {"type": "TEST_CASE", "attributes": [], "from": "2000-01-01T00:00:00.000Z", "page": _PAGE}),
]


def _make_tms_reference_test(argv, kind, endpoint, request):
    def _t():
        transport = Transport("ok")
        rc, out, err = run("swtr_task_tool", argv, transport)
        assert rc == 0, f"tms {kind}: expected rc=0, got {rc}; stderr={err[:300]!r}"
        data = json.loads(out)
        assert data.get("command") == "tms", f"tms {kind}: command={data.get('command')!r}"
        assert data.get("kind") == kind, f"tms {kind}: kind={data.get('kind')!r}"
        assert data.get("endpoint") == endpoint, f"tms {kind}: endpoint={data.get('endpoint')!r} != {endpoint!r}"
        payload = data.get("data")
        if request is None:  # GET kind: data is the raw routed fixture response
            assert payload == FIX["swtr_tms"], f"tms {kind}: GET data != fixture; got {payload!r}"
        else:  # POST kind: data is {"request": <exact body>, "response": <fixture>}
            assert payload == {"request": request, "response": FIX["swtr_tms"]}, \
                f"tms {kind}: POST data mismatch; got {payload!r}"

    return _t


for _argv, _kind, _endpoint, _request in _TMS_REFERENCE:
    register(f"[tms-ref] swtr_task_tool tms {_kind}", _make_tms_reference_test(_argv, _kind, _endpoint, _request))


# ---- TMS negative cases: a missing required param must raise SystemExit with
# the exact message the pre-refactor per-branch code raised, before any network
# call. Captured as golden and asserted byte-identical during the Task 5 refactor.
_TMS_MISSING = [
    (["tms", "test-plans"], "tms test-plans requires --space SPACE."),
    (["tms", "plan-cycles"], "tms plan-cycles requires --plan TEST-PLAN-CODE."),
    (["tms", "cycle-cases"], "tms cycle-cases requires --cycle TEST-CYCLE-CODE."),
    (["tms", "folder-root"], "tms folder-root requires --space SPACE."),
    (["tms", "folder-root-units"], "tms folder-root-units requires --space SPACE."),
    (["tms", "folder-root-progress"], "tms folder-root-progress requires --space SPACE."),
    (["tms", "folder-hierarchy"], "tms folder-hierarchy requires --folder FOLDER_CODE."),
    (["tms", "test-case"], "tms test-case requires --unit UNIT_KEY."),
    (["tms", "run-info"], "tms run-info requires --run RUN_CODE."),
]


def _make_tms_missing_test(argv, message):
    def _t():
        stt = importlib.import_module("swtr_task_tool")
        transport = Transport("ok")
        ns = stt.parse_args(argv)
        with mock.patch("urllib.request.urlopen", side_effect=transport):
            try:
                stt.cmd_tms(ns)
            except SystemExit as exc:
                got = exc.code
            else:
                raise AssertionError(f"tms {argv}: expected SystemExit, none raised")
        assert got == message, f"tms {argv}: SystemExit code {got!r} != {message!r}"
        assert transport.calls == [], f"tms {argv}: missing-param must not hit network, got {transport.calls}"

    return _t


for _argv, _message in _TMS_MISSING:
    register(f"[tms-neg] swtr_task_tool {' '.join(_argv)} (missing required)", _make_tms_missing_test(_argv, _message))

# ---- happy path: other tools ----
add_happy("fetch_swtr_sources", ["SMOKE-1"], expect={"ok": True}, has_keys=["source", "request"])
add_happy("swtr_unit_search", [], expect={"ok": True}, has_keys=["endpoint", "response"])
add_happy("swtr_unit_comments", ["SMOKE-1"], expect={"ok": True, "unitCode": "SMOKE-1"}, has_keys=["normalized", "response"])
add_happy("source_control_search", ["repos", "--tenant", "smoke-tenant", "--project", "smoke-project"],
          expect={"command": "repos"}, has_keys=["response"])
add_happy("source_control_search", ["search", "--tenant", "smoke-tenant", "--project", "smoke-project", "--query", "foo"],
          expect={"command": "search", "needsRepository": True}, has_keys=["candidates"], label="[happy] source_control_search search (candidates)")
add_happy("source_control_search", ["branches", "--tenant", "smoke-tenant", "--project", "smoke-project", "--repo", "smoke-repo"],
          expect={"command": "branches", "repo": "smoke-repo"}, has_keys=["response"], label="[happy] source_control_search branches")
add_happy("source_control_search", ["file", "--tenant", "smoke-tenant", "--project", "smoke-project", "--repo", "smoke-repo", "--filepath", "src/app.py"],
          expect={"command": "file", "repo": "smoke-repo", "filepath": "src/app.py"}, has_keys=["content", "chars"], label="[happy] source_control_search file")
add_happy("confluence_tool", ["get", "1001", "--confirm-sberworks"], expect={"command": "confluence.get", "id": "1001"}, has_keys=["title", "body"])
add_happy("confluence_tool", ["search", "payment", "--confirm-sberworks"], expect={"command": "confluence.search"}, has_keys=["results"])
add_happy("confluence_tool", ["space", "--key", "SMK", "--confirm-sberworks"], expect={"command": "confluence.space"}, has_keys=["space", "pages"])
add_happy("jira_tool", ["get", "SMOKE-100", "--confirm-sberworks"], expect={"command": "jira.get", "key": "SMOKE-100"}, has_keys=["status", "summary"])
add_happy("jira_tool", ["search", "payment", "--confirm-sberworks"], expect={"command": "jira.search"}, has_keys=["issues"])
add_happy("jira_tool", ["similar", "payment gateway", "--confirm-sberworks"], expect={"command": "jira.similar"}, has_keys=["terms", "results"])
add_happy("jira_tool", ["comments", "SMOKE-100", "--confirm-sberworks"], expect={"command": "jira.comments", "key": "SMOKE-100"}, has_keys=["comments"])
def _update_without_execute_touches_nothing():
    """Дифф печатается, PATCH не уходит: правка мультизначного поля перетирает
    список целиком, и увидеть это надо до отправки."""
    transport = Transport("ok")
    rc, out, err = run("swtr_task_tool", ["update", "SMOKE-1", "--component", "VLOG"], transport)
    assert rc == 0, f"rc={rc}; stderr={err[:300]}"
    data = json.loads(out)
    assert data["dryRun"] is True, data
    assert data["diff"], "дифф обязан быть непустым"
    assert not [c for c in transport.calls if c["method"] == "PATCH"], "PATCH ушёл без --execute"


register("[update] dry run prints the diff and sends no PATCH",
         _update_without_execute_touches_nothing)


def _update_refuses_to_move_the_status():
    """У статуса свой автомат; запись значением даёт 500 без объяснения."""
    transport = Transport("ok")
    rc, out, err = run("swtr_task_tool",
                       ["update", "SMOKE-1", "--attribute", "workflow_status=done", "--execute"],
                       transport)
    assert rc == 1, f"expected refusal, got rc={rc}"
    payload = json.loads(err)
    assert "status set" in json.dumps(payload, ensure_ascii=False), payload
    assert not [c for c in transport.calls if c["method"] == "PATCH"], "статус ушёл в запись"


register("[update] changing workflow_status is refused, not sent",
         _update_refuses_to_move_the_status)


add_happy("swtr_task_tool", ["attributes", "task"],
          expect={"command": "attributes"}, has_keys=["attributes", "namedFields"])
add_happy("swtr_task_tool", ["directory", "--space", "SMOKE", "--kind", "components"],
          expect={"command": "directory", "kind": "components"}, has_keys=["values"])
add_happy("swtr_task_tool", ["directory", "--space", "SMOKE", "--kind", "product", "--suit", "task"],
          expect={"command": "directory", "count": 2},
          label="[happy] swtr_task_tool directory product reads the suit schema")


def _named_fields_resolve_into_tracker_form():
    """Имя из справочника превращается в значение той формы, которую примет трекер:
    таксономия — голым кодом-строкой, ссылочные поля — списком объектов."""
    transport = Transport("ok")
    rc, out, err = run("swtr_task_tool", [
        "create", "--suit", "task", "--space", "SMOKE", "--summary", "s",
        "--fix-version", "1.0.0", "--component", "VLOG", "--product", "OPM"], transport)
    assert rc == 0, f"rc={rc}; stderr={err[:300]}"
    attrs = json.loads(out)["request"]["body"]["attributes"]
    assert attrs["fix_version"] == [{"code": "11111111-1111-1111-1111-111111111111"}], attrs
    assert attrs["sber_component"] == [{"code": "324623d6-0000-0000-0000-000000000001"}], attrs
    assert attrs["customfield_24900"] == "273841", attrs
    assert isinstance(attrs["customfield_24900"], str), "код продукта обязан быть строкой"


register("[fields] named version/component/product become tracker-shaped values",
         _named_fields_resolve_into_tracker_form)


def _unknown_directory_value_never_reaches_the_tracker():
    """Неизвестное имя — отказ до записи, а не HTTP 500 после неё."""
    transport = Transport("ok")
    rc, out, err = run("swtr_task_tool", [
        "create", "--suit", "task", "--space", "SMOKE", "--summary", "s",
        "--component", "НЕТ-ТАКОГО", "--execute"], transport)
    assert rc == 1, f"expected refusal, got rc={rc}; stdout={out[:200]}"
    assert json.loads(err).get("ok") is False, err[:200]
    assert not any(c["method"] == "POST" and "/create" in c["url"] for c in transport.calls), \
        "создание ушло в трекер, хотя значение справочника неизвестно"


register("[fields] unknown directory value is refused before create",
         _unknown_directory_value_never_reaches_the_tracker)


add_happy("swtr_task_tool", ["spaces"], expect={"command": "spaces"}, has_keys=["spaces", "count"])
add_happy("swtr_task_tool", ["spaces", "smoke"], expect={"command": "spaces", "count": 1},
          label="[happy] swtr_task_tool spaces <query> filters by code and name")
add_happy("swtr_task_tool", ["filters"], expect={"command": "filters"}, has_keys=["filters", "count"])
add_happy("swtr_task_tool", ["versions", "SMOKE"], expect={"command": "versions", "count": 1},
          label="[happy] swtr_task_tool versions hides archived by default")
add_happy("swtr_task_tool", ["versions", "SMOKE", "--include-archived"],
          expect={"command": "versions", "count": 2},
          label="[happy] swtr_task_tool versions --include-archived keeps them")
add_happy("swtr_task_tool", ["workflow", "--suit", "task", "--space", "SMOKE"],
          expect={"command": "workflow"}, has_keys=["statuses", "transitions", "schema"])
add_happy("swtr_task_tool", ["tql", 'space = "SMOKE" AND priority = "high"'],
          expect={"command": "tql"}, has_keys=["tql", "response"])


def _tql_refuses_mixed_and_or_before_any_call():
    """Смешение AND и OR бэкенд принимает и молча возвращает не то — значит
    отправлять такой запрос нельзя, и отказ обязан случиться до сети."""
    transport = Transport("ok")
    rc, out, err = run("swtr_task_tool", ["tql", 'a = "1" AND b = "2" OR c = "3"'], transport)
    assert rc == 1, f"expected rc=1, got {rc}; stdout={out[:200]}"
    payload = json.loads(err)
    assert payload.get("ok") is False, f"error contract broken: {err[:200]}"
    assert payload.get("command") == "tql", payload
    assert not transport.calls, "запрос ушёл в сеть, хотя форма заведомо неверна"


register("[tql] mixed AND/OR is refused before the request leaves", _tql_refuses_mixed_and_or_before_any_call)


add_happy("local_context_search", ["--paths", str(FIXTURES_DIR), "--query", "smoke"], expect={"ok": True}, has_keys=["results", "totalMatches"])


# ---- dry-run write commands: no mutating network call ----
def _dryrun_swtr_create():
    transport = Transport("ok")
    rc, out, err = run("swtr_task_tool", ["create", "--summary", "Smoke task", "--space", "SMOKE"], transport)
    assert rc == 0, f"rc={rc}; stderr={err[:300]!r}"
    data = json.loads(out)
    assert data.get("command") == "create" and data.get("dryRun") is True, f"unexpected: {out[:300]}"
    assert data.get("request", {}).get("body"), "dry-run must print the request payload"
    assert transport.calls == [], f"dry-run create must not call the network, got {transport.calls}"


register("[dry-run] swtr_task_tool create (no --execute)", _dryrun_swtr_create)


def _link_find_happy():
    transport = Transport("ok")
    rc, out, err = run("swtr_task_tool", ["link", "find", "--unit", "CRPV-1"], transport)
    assert rc == 0, f"rc={rc}; stderr={err[:300]!r}"
    data = json.loads(out)
    assert data.get("command") == "link.find", f"unexpected: {out[:300]}"
    assert data.get("request", {}).get("unitId") == "CRPV-1", f"request wrong: {out[:300]}"
    assert data.get("response", {}).get("content"), "expected link rows in response"


register("[happy] swtr_task_tool link find", _link_find_happy)


def _spec_sources_happy():
    """Источники постановки: корневая задача плюс дочитанные связи, одним вызовом."""
    transport = Transport("ok")
    rc, out, err = run("spec_sources", ["CRPV-1"], transport)
    assert rc == 0, f"rc={rc}; stderr={err[:300]!r}"
    data = json.loads(out)
    assert data.get("ok") is True and data.get("command") == "spec-sources", f"unexpected: {out[:300]}"
    assert data.get("root", {}).get("code") == "CRPV-1", f"root wrong: {out[:300]}"
    # Корневой источник печатается в форме fetch_swtr_sources: на этом стыке
    # держится `openspec_seed.py --source-file`.
    assert "attributes" in data.get("source", {}), f"source shape broken: {out[:300]}"
    assert data.get("markdown", "").startswith("# Источники постановки: CRPV-1"), f"pack missing: {out[:300]}"
    # Связь из фикстуры (CRPV-1 -> PVM-2) дочитана как отдельный источник.
    assert [row["code"] for row in data.get("linked", [])] == ["PVM-2"], f"linked wrong: {out[:300]}"


register("[happy] spec_sources pack", _spec_sources_happy)


def _spec_sources_never_calls_sberworks():
    """Гейт «Приоритет систем» действует и на обходе связей.

    Внешние ссылки инструмент только перечисляет: сходить за страницей
    Confluence/Jira он не имеет права даже когда она названа в постановке —
    решение идти на sberworks остаётся явным действием человека.

    Утверждение «на sberworks не ходили» имеет смысл только тогда, когда идти
    было куда: фикстура юнита несёт ссылку на sberworks, и тест сначала
    убеждается, что инструмент её ВИДИТ, и лишь потом — что не пошёл по ней.
    """
    transport = Transport("ok")
    rc, out, err = run("spec_sources", ["CRPV-1"], transport)
    assert rc == 0, f"rc={rc}; stderr={err[:300]!r}"
    data = json.loads(out)
    systems = {ref["system"]: ref for ref in data.get("external", [])}
    assert "confluence" in systems, f"ссылка на sberworks не распознана: {data.get('external')}"
    assert "--confirm-sberworks" in (systems["confluence"].get("readCommand") or ""), systems["confluence"]
    assert "other" in systems, f"чужой хост не отнесён к other: {data.get('external')}"
    assert systems["other"].get("readCommand") is None, "для чужого хоста команды быть не должно"
    hosts = {url.split("/")[2] for url in (call["url"] for call in transport.calls)}
    assert not any("sberworks" in host for host in hosts), f"sberworks touched: {sorted(hosts)}"
    assert not any("example.org" in host for host in hosts), f"чужой хост прочитан: {sorted(hosts)}"


register("[gate] spec_sources does not fetch sberworks", _spec_sources_never_calls_sberworks)


def _spec_sources_comments_are_opt_in():
    """Комментарии — отдельный канал: без флага в них не ходят вовсе."""
    quiet = Transport("ok")
    rc, out, err = run("spec_sources", ["CRPV-1"], quiet)
    assert rc == 0, f"rc={rc}; stderr={err[:300]!r}"
    assert not any("/unit-comment/" in call["url"] for call in quiet.calls), \
        "комментарии прочитаны без --with-comments"
    loud = Transport("ok")
    rc, out, err = run("spec_sources", ["CRPV-1", "--with-comments"], loud)
    assert rc == 0, f"rc={rc}; stderr={err[:300]!r}"
    assert any("/unit-comment/" in call["url"] for call in loud.calls), "флаг не дошёл до канала"
    data = json.loads(out)
    assert data["comments"], f"комментарии не попали в вывод: {out[:300]}"
    assert "**Комментарии" in data["markdown"], f"комментарии не попали в пакет: {out[:300]}"


register("[happy] spec_sources --with-comments", _spec_sources_comments_are_opt_in)


def _link_create_dryrun():
    transport = Transport("ok")
    rc, out, err = run("swtr_task_tool",
                       ["link", "create", "--source", "CRPV-1", "--destination", "PVM-2", "--type", "realized_in"],
                       transport)
    assert rc == 0, f"rc={rc}; stderr={err[:300]!r}"
    data = json.loads(out)
    assert data.get("command") == "link.create" and data.get("dryRun") is True, f"unexpected: {out[:300]}"
    assert data["request"]["body"] == {"source": "CRPV-1", "destination": "PVM-2", "type": "realized_in"}, \
        f"body wrong: {out[:300]}"
    assert transport.calls == [], f"dry-run link.create must not hit network, got {transport.calls}"


register("[dry-run] swtr_task_tool link create (no --execute)", _link_create_dryrun)


def _link_create_no_retry_on_timeout():
    transport = Transport("ok", timeout_methods={"POST"})
    rc, out, err = run("swtr_task_tool",
                       ["link", "create", "--source", "CRPV-1", "--destination", "PVM-2",
                        "--type", "realized_in", "--execute"], transport)
    _assert_unified_error_contract(out, err, rc, kind="link create no-retry")
    posts = [c for c in transport.calls if c["method"] == "POST"]
    assert len(posts) == 1, f"link write must not retry; got {[c['method'] for c in transport.calls]}"


register("[no-retry] swtr_task_tool link create (one POST on timeout)", _link_create_no_retry_on_timeout)


def _status_get_happy():
    transport = Transport("ok")
    rc, out, err = run("swtr_task_tool", ["status", "get", "STS-1"], transport)
    assert rc == 0, f"rc={rc}; stderr={err[:300]!r}"
    data = json.loads(out)
    assert data.get("command") == "status.get", f"unexpected: {out[:300]}"
    assert data["currentStatus"]["name"] == "CANCELLED", f"status wrong: {out[:300]}"
    assert [a["name"] for a in data["availableActions"]] == ["Открыто"], f"actions wrong: {out[:300]}"
    # Одного чтения достаточно: граф схемы для `get` не нужен.
    assert len(transport.calls) == 1, f"status get must read once, got {transport.calls}"


register("[happy] swtr_task_tool status get", _status_get_happy)


def _status_set_dryrun():
    """Маршрут в несколько переходов печатается целиком и НИЧЕГО не меняет."""
    transport = Transport("ok")
    rc, out, err = run("swtr_task_tool", ["status", "set", "STS-1", "--to", "RESOLVED"], transport)
    assert rc == 0, f"rc={rc}; stderr={err[:300]!r}"
    data = json.loads(out)
    assert data.get("command") == "status.set" and data.get("dryRun") is True, f"unexpected: {out[:300]}"
    assert data.get("hops") == 3, f"expected a 3-hop route, got {data.get('hops')}: {out[:400]}"
    assert [s["toName"] for s in data["plan"]] == ["OPEN", "PROBLEM ANALYSIS", "RESOLVED"], \
        f"route wrong: {out[:400]}"
    assert data["request"]["bodies"][0] == {"unitCode": "STS-1", "actionCode": "SMKAOPEN_000000000001"}, \
        f"body wrong: {out[:400]}"
    mutating = [c for c in transport.calls if c["method"] != "GET"]
    assert mutating == [], f"dry-run status set must not mutate, got {mutating}"


register("[dry-run] swtr_task_tool status set (no --execute)", _status_set_dryrun)


def _status_set_execute():
    transport = Transport("ok")
    rc, out, err = run("swtr_task_tool", ["status", "set", "STS-1", "--to", "OPEN", "--execute"], transport)
    assert rc == 0, f"rc={rc}; stderr={err[:300]!r}"
    data = json.loads(out)
    assert data.get("dryRun") is False, f"unexpected: {out[:300]}"
    patches = [c for c in transport.calls if c["method"] == "PATCH"]
    assert len(patches) == 1, f"one hop must send one PATCH, got {[c['method'] for c in transport.calls]}"
    assert patches[0]["body"] == {"unitCode": "STS-1", "actionCode": "SMKAOPEN_000000000001"}, \
        f"patch body wrong: {patches[0]}"
    assert patches[0]["url"].endswith("/rest/api/workflow/v2/unit/status/change"), \
        f"wrong endpoint: {patches[0]['url']}"


register("[write] swtr_task_tool status set --execute", _status_set_execute)


def _status_set_blocked_action():
    """Недоступное действие ловится локально: сети не касаемся, контракт ошибки общий."""
    transport = Transport("ok")
    rc, out, err = run("swtr_task_tool",
                       ["status", "set", "STS-1", "--action", "Завершить", "--execute"], transport)
    _assert_unified_error_contract(out, err, rc, kind="status set blocked action")
    mutating = [c for c in transport.calls if c["method"] != "GET"]
    assert mutating == [], f"an unavailable action must not be sent, got {mutating}"


register("[guard] swtr_task_tool status set (action not available)", _status_set_blocked_action)


def _status_set_no_retry_on_timeout():
    transport = Transport("ok", timeout_methods={"PATCH"})
    rc, out, err = run("swtr_task_tool", ["status", "set", "STS-1", "--to", "OPEN", "--execute"], transport)
    _assert_unified_error_contract(out, err, rc, kind="status set no-retry")
    patches = [c for c in transport.calls if c["method"] == "PATCH"]
    assert len(patches) == 1, f"a transition must not retry; got {len(patches)} PATCHes"


register("[no-retry] swtr_task_tool status set (one PATCH on timeout)", _status_set_no_retry_on_timeout)


def _dryrun_jira_comment():
    transport = Transport("ok")
    rc, out, err = run("jira_tool", ["comment", "SMOKE-100", "--body-file", str(BODY_FILE), "--confirm-sberworks"], transport)
    assert rc == 0, f"rc={rc}; stderr={err[:300]!r}"
    data = json.loads(out)
    assert data.get("command") == "jira.comment" and data.get("mode") == "dry-run", f"unexpected: {out[:300]}"
    assert transport.calls == [], f"dry-run comment must not call the network, got {transport.calls}"


register("[dry-run] jira_tool comment (no --execute)", _dryrun_jira_comment)


def _dryrun_jira_create():
    # jira create is --execute-gated: without it, print the request and POST nothing.
    transport = Transport("ok")
    rc, out, err = run("jira_tool", ["create", "--project", "SMK", "--type", "Task", "--summary", "Smoke issue", "--confirm-sberworks"], transport)
    assert rc == 0, f"rc={rc}; stderr={err[:300]!r}"
    data = json.loads(out)
    assert data.get("command") == "jira.create" and data.get("mode") == "dry-run", f"unexpected: {out[:300]}"
    assert data.get("request", {}).get("fields", {}).get("summary") == "Smoke issue", f"dry-run must print request: {out[:300]}"
    assert transport.calls == [], f"dry-run create must not call the network, got {transport.calls}"


register("[dry-run] jira_tool create (no --execute)", _dryrun_jira_create)


def _dryrun_jira_edit():
    # jira edit is --execute-gated: without it, print the PUT body and send nothing.
    transport = Transport("ok")
    rc, out, err = run("jira_tool", ["edit", "SMOKE-100", "--set", "customfield_1=Backlog", "--confirm-sberworks"], transport)
    assert rc == 0, f"rc={rc}; stderr={err[:300]!r}"
    data = json.loads(out)
    assert data.get("command") == "jira.edit" and data.get("mode") == "dry-run", f"unexpected: {out[:300]}"
    assert data.get("request", {}).get("fields", {}).get("customfield_1") == "Backlog", f"dry-run must print request: {out[:300]}"
    assert transport.calls == [], f"dry-run edit must not call the network, got {transport.calls}"


register("[dry-run] jira_tool edit (no --execute)", _dryrun_jira_edit)


def _dryrun_confluence_update():
    # confluence update reads the current version (a GET) before deciding, but
    # must never PUT without --execute.
    transport = Transport("ok")
    rc, out, err = run("confluence_tool", ["update", "--id", "1001", "--storage-file", str(STORAGE_FILE), "--confirm-sberworks"], transport)
    assert rc == 0, f"rc={rc}; stderr={err[:300]!r}"
    data = json.loads(out)
    assert data.get("command") == "confluence.update" and data.get("mode") == "dry-run", f"unexpected: {out[:300]}"
    puts = [c for c in transport.calls if c["method"] == "PUT"]
    assert not puts, f"dry-run update must not PUT, got {puts}"


register("[dry-run] confluence_tool update (no --execute, GET only)", _dryrun_confluence_update)


# ---- sberworks gate: --confirm-sberworks is REQUIRED ----
# sberworks (Jira/Confluence) is the LAST-priority backend: tasks and wiki live in
# SWTR Task Tracker. Without the explicit flag the tool must refuse through the
# unified error contract and open no socket at all -- the refusal is a routing
# guard, not a backend failure, so it carries gate="sberworks-not-default".
_SBERWORKS_GATE_CASES = [
    ("jira_tool", ["get", "SMOKE-100"], "jira.get"),
    ("confluence_tool", ["get", "1001"], "confluence.get"),
]


def _make_sberworks_gate_test(module, argv, command):
    def _t():
        transport = Transport("ok")
        rc, out, err = run(module, argv, transport)
        assert rc == 1, f"{command}: expected rc=1 from the sberworks gate, got {rc}; stdout={out[:300]!r}"
        assert not out.strip(), f"{command}: gate must not print to stdout; got {out[:300]!r}"
        payload = json.loads(err.strip())
        assert payload.get("ok") is False, f"{command}: gate payload must be ok=false: {payload!r}"
        assert payload.get("command") == command, f"{command}: unexpected command: {payload!r}"
        assert payload.get("gate") == "sberworks-not-default", f"{command}: missing gate marker: {payload!r}"
        assert transport.calls == [], f"{command}: gate must fire before any network call, got {transport.calls}"

    return _t


for _mod, _argv, _command in _SBERWORKS_GATE_CASES:
    register(f"[gate] {_mod} without --confirm-sberworks -> refuse, no network",
             _make_sberworks_gate_test(_mod, _argv, _command))


# ---- writes do NOT retry: a first-attempt timeout issues exactly ONE POST/PUT ----
# A non-idempotent write (jira create/comment POST, confluence update PUT) that
# times out AFTER the server may have applied it must NOT be replayed -- a retry
# duplicates the mutation. With max_retries=0 the write is attempted once and the
# failure surfaces as the unified error contract. With the default retry policy
# request_http would replay the write up to 4 times, so len(writes)==1 is the
# discriminator. timeout_methods times out only the write, so any preceding read
# (confluence update's version GET) still succeeds and the write is reached.
_NO_RETRY_WRITES = [
    ("jira_tool", ["create", "--project", "SMK", "--type", "Task", "--summary", "S", "--execute", "--confirm-sberworks"], "POST",
     "jira create no-retry"),
    ("jira_tool", ["comment", "SMOKE-100", "--body-file", str(BODY_FILE), "--execute", "--confirm-sberworks"], "POST",
     "jira comment no-retry"),
    ("confluence_tool", ["update", "--id", "1001", "--storage-file", str(STORAGE_FILE), "--execute", "--confirm-sberworks"], "PUT",
     "confluence update no-retry"),
]


def _make_no_retry_write_test(module, argv, method, kind):
    def _t():
        transport = Transport("ok", timeout_methods={method})
        rc, out, err = run(module, argv, transport)
        _assert_unified_error_contract(out, err, rc, kind=kind)
        writes = [c for c in transport.calls if c["method"] == method]
        assert len(writes) == 1, \
            f"{kind}: write must not retry; expected exactly one {method}, got {[c['method'] for c in transport.calls]}"

    return _t


for _mod, _argv, _method, _kind in _NO_RETRY_WRITES:
    register(f"[no-retry] {_kind} (one {_method} on timeout)", _make_no_retry_write_test(_mod, _argv, _method, _kind))


def _swtr_create_execute_redacts_response_set_cookie():
    # Secret leak (finding 1): swtr create --execute prints the HTTP response
    # headers. A backend Set-Cookie must be redacted via safe_response_headers,
    # never echoed to stdout/logs.
    secret_cookie = "SESSIONID=SUPERSECRETCOOKIEVALUE; Path=/; HttpOnly"
    transport = Transport("ok", response_headers={"Set-Cookie": secret_cookie})
    rc, out, err = run("swtr_task_tool",
                       ["create", "--summary", "Smoke task", "--space", "SMOKE", "--execute"], transport)
    assert rc == 0, f"rc={rc}; stderr={err[:400]!r}"
    data = json.loads(out)
    hdrs = data.get("response", {}).get("headers", {})
    assert hdrs.get("Set-Cookie") == "<redacted>", f"Set-Cookie not redacted: {hdrs!r}"
    assert "SUPERSECRETCOOKIEVALUE" not in out, f"cookie value leaked into --execute output: {out[:400]!r}"
    assert "<redacted>" in out, f"expected a redaction marker in output: {out[:400]!r}"


register("[secrets] swtr create --execute redacts response Set-Cookie", _swtr_create_execute_redacts_response_set_cookie)


def _doctor_probe_timeout_yields_verdict():
    # Finding 4: a live probe timeout must return the documented network/TLS
    # verdict (rc 4), not crash with a traceback. Transport("timeout") raises
    # socket.timeout, which is NOT a URLError; probe() now also catches it.
    transport = Transport("timeout")
    rc, out, err = run("guru_doctor", ["--system", "source_control", "--timeout", "3"], transport)
    assert rc == 4, f"expected network/TLS verdict rc=4, got {rc}; stdout={out[:400]!r} stderr={err[:400]!r}"
    assert "network/TLS" in out, f"expected network/TLS verdict in output, got {out[:400]!r}"
    assert "Traceback" not in (out + err), f"probe timeout must not traceback: {(out + err)[:400]!r}"


register("[doctor] probe timeout -> network/TLS verdict (not traceback)", _doctor_probe_timeout_yields_verdict)


# ---- unified error contract: a missing write-input file must not traceback ----
# The file for --body-file / --description-file / --storage-file is read before
# any network call; a missing/unreadable one becomes a RuntimeError routed
# through emit_error (stderr JSON contract), never a raw OSError traceback.
_MISSING_FILE = str(Path(_TMP_DIR) / "does-not-exist-xyz.txt")
_MISSING_FILE_WRITES = [
    ("jira_tool", ["comment", "SMOKE-100", "--body-file", _MISSING_FILE, "--confirm-sberworks"], "jira comment missing body-file"),
    ("jira_tool", ["create", "--project", "SMK", "--type", "Task", "--summary", "S",
                   "--description-file", _MISSING_FILE, "--confirm-sberworks"], "jira create missing description-file"),
    ("confluence_tool", ["update", "--id", "1001", "--storage-file", _MISSING_FILE, "--confirm-sberworks"],
     "confluence update missing storage-file"),
]


def _make_missing_file_test(module, argv, kind):
    def _t():
        transport = Transport("ok")
        rc, out, err = run(module, argv, transport)
        _assert_unified_error_contract(out, err, rc, kind=kind)
        assert transport.calls == [], f"{kind}: missing file must not hit network, got {transport.calls}"

    return _t


for _mod, _argv, _kind in _MISSING_FILE_WRITES:
    register(f"[robust] {_kind} -> unified error, no network", _make_missing_file_test(_mod, _argv, _kind))


# ---- error path (Task 3): 401 AND timeout -> the same unified error contract ----
# The third field is the tool's OWN configured token, echoed into a 401 body so
# per-tool redaction is exercised for every tool (not just swtr_unit_comments).
_ERROR_CASES = [
    ("swtr_task_tool", ["get", "SMOKE-1"], SWTR_TOKEN),
    ("swtr_task_tool", ["suits", "PVM"], SWTR_TOKEN),
    ("fetch_swtr_sources", ["SMOKE-1"], SWTR_TOKEN),
    ("spec_sources", ["SMOKE-1"], SWTR_TOKEN),
    ("swtr_unit_search", [], SWTR_TOKEN),
    ("swtr_unit_comments", ["SMOKE-1"], SWTR_TOKEN),
    ("source_control_search", ["repos", "--tenant", "smoke-tenant", "--project", "smoke-project"], "SMOKESCTOKEN"),
    ("confluence_tool", ["get", "1001", "--confirm-sberworks"], "SMOKECONFTOKEN"),
    ("jira_tool", ["get", "SMOKE-100", "--confirm-sberworks"], "SMOKEJIRATOKEN"),
]
for _mod, _argv, _token in _ERROR_CASES:
    add_error_401(_mod, _argv)
    add_error_timeout(_mod, _argv)
    add_error_401_token_redacted(_mod, _argv, _token)


def _redaction_on_401():
    # A 401 body that echoes the configured bearer token must not leak it.
    body = {"errorCode": "UNAUTHORIZED", "message": f"rejected token Bearer {SWTR_TOKEN}"}
    transport = Transport("error", status=401, error_body=body)
    rc, out, err = run("swtr_unit_comments", ["SMOKE-1"], transport)
    assert rc == 1, f"rc={rc}"
    combined = out + err
    assert SWTR_TOKEN not in combined, "secret token leaked into error output"
    assert "redacted" in err, f"expected 'redacted' marker in stderr; got {err[:300]!r}"


register("[secrets] swtr_unit_comments 401 token redacted", _redaction_on_401)


def _comments_error_json_field_redacted():
    # Task 2: swtr_unit_comments.py wraps the error text in redact_secrets()
    # before printing the JSON error object. Assert the parsed "error" field of
    # the stderr JSON is redacted (not just absent from the raw stream).
    body = {"errorCode": "UNAUTHORIZED", "message": f"token Bearer {SWTR_TOKEN} rejected"}
    transport = Transport("error", status=401, error_body=body)
    rc, out, err = run("swtr_unit_comments", ["SMOKE-1"], transport)
    assert rc == 1, f"rc={rc}"
    payload = json.loads(err.strip())  # error contract is a JSON object on stderr
    assert payload.get("ok") is False, f"expected ok=false, got {payload!r}"
    assert payload.get("unitCode") == "SMOKE-1", f"unexpected unitCode: {payload!r}"
    error_text = payload.get("error", "")
    assert SWTR_TOKEN not in error_text, f"token leaked in error JSON field: {error_text!r}"
    assert "redacted" in error_text, f"error field not redacted: {error_text!r}"


register("[secrets] swtr_unit_comments 401 error JSON field redacted", _comments_error_json_field_redacted)


def _doctor_sc_probe_hits_sc_endpoint():
    # Task 2 (original) + rework: the source_control probe hits the real repos
    # endpoint under the default pvm tenant/project (project-independent endpoints
    # 404 before auth is checked), NOT the task_tracker unit endpoint.
    transport = Transport("ok")
    rc, out, err = run("guru_doctor", ["--system", "source_control"], transport)
    assert rc == 0, f"rc={rc}; stderr={err[:300]!r}; stdout={out[:300]!r}"
    assert len(transport.calls) == 1, f"expected exactly one probe call, got {transport.calls}"
    call = transport.calls[0]
    assert call["method"] == "GET", f"probe must be GET, got {call}"
    url = call["url"]
    assert url == "https://sc.smoke.test/api/v3/repos/22f434fb-0355-42a1-ac25-011074a20a2d/pvm?limit=1", \
        f"SC probe URL wrong: {url!r}"
    assert "rest/api/unit/v2" not in url, f"SC probe still hits task_tracker endpoint: {url!r}"


register("[doctor] source_control probe hits SC repos endpoint", _doctor_sc_probe_hits_sc_endpoint)


def _doctor_timeout_is_plumbed():
    # Task 2/3: a custom --timeout must actually reach urlopen(timeout=...). The
    # Transport records the timeout it is called with; assert the value threads
    # through the doctor probe rather than being silently dropped.
    transport = Transport("ok")
    rc, out, err = run("guru_doctor", ["--system", "source_control", "--timeout", "7"], transport)
    assert rc == 0, f"rc={rc}; stderr={err[:300]!r}"
    assert len(transport.calls) == 1, f"expected exactly one probe call, got {transport.calls}"
    assert transport.calls[0]["timeout"] == 7.0, f"doctor --timeout not plumbed to urlopen: {transport.calls[0]!r}"


register("[doctor] --timeout threaded to urlopen(timeout=...)", _doctor_timeout_is_plumbed)


def _tool_timeout_is_plumbed():
    # The same --timeout plumbing for a regular tool: request_http must pass the
    # CLI --timeout down to urlopen for every request it makes.
    transport = Transport("ok")
    rc, out, err = run("swtr_task_tool", ["get", "SMOKE-1", "--timeout", "9"], transport)
    assert rc == 0, f"rc={rc}; stderr={err[:300]!r}"
    assert transport.calls, "expected at least one network call"
    assert all(c["timeout"] == 9.0 for c in transport.calls), f"--timeout not plumbed: {transport.calls!r}"


register("[timeout-plumb] swtr_task_tool get --timeout reaches urlopen", _tool_timeout_is_plumbed)


def _build_find_body_operator_and_order():
    # Task 4: build_find_body backs search_units (operator=or, properties first)
    # и режим operator=and со spaces/suits впереди. Pin the operator grouping AND
    # the filter-key insertion order so the emitted request body stays
    # byte-identical for both режимами.
    stt = importlib.import_module("swtr_task_tool")
    props = [{"attribute": "code", "operator": "like", "value": "q"}]
    attrs = [{"attribute": "x", "operator": "like", "value": "q"}]
    search_body = stt.build_find_body(
        operator="or", property_filters=props, attribute_filters=attrs,
        spaces=["PVM"], suits=["S"], return_attributes=["code", "summary", "code"],
        page=0, size=20, sort_attribute="updatedAt", sort_direction="DESC", properties_first=True,
    )
    assert list(search_body["filters"].keys()) == ["properties", "spaces", "suits", "attributes"], \
        f"search key order wrong: {list(search_body['filters'].keys())}"
    assert search_body["filters"]["properties"] == {"or": props}, f"search operator wrong: {search_body['filters']['properties']}"
    assert search_body["filters"]["attributes"] == {"or": attrs}, f"search attr operator wrong: {search_body['filters']['attributes']}"
    assert search_body["attributes"] == ["code", "summary"], f"return-attr dedup wrong: {search_body['attributes']}"
    assert search_body["page"] == {"page": 0, "size": 20}
    assert search_body["sort"] == {"attribute": ["updatedAt"], "direction": "DESC"}

    find_body = stt.build_find_body(
        operator="and", property_filters=props, attribute_filters=attrs,
        spaces=["PVM"], suits=["S"], return_attributes=["code"],
        page=1, size=10, sort_attribute="updatedAt", sort_direction="ASC", properties_first=False,
    )
    assert list(find_body["filters"].keys()) == ["spaces", "suits", "properties", "attributes"], \
        f"find key order wrong: {list(find_body['filters'].keys())}"
    assert find_body["filters"]["properties"] == {"and": props}, f"find operator wrong: {find_body['filters']['properties']}"


register("[unit] swtr_task_tool build_find_body operator + key order", _build_find_body_operator_and_order)


def _sc_pagination_guard_adds_note():
    # Task 3: the recursive contents search must not paginate a single directory
    # forever. When a directory keeps returning full pages, max_pages bounds the
    # loop and a note is surfaced instead of silently dropping entries or spinning.
    mod = importlib.import_module("source_control_search")
    full_page = {"content": [{"type": "file", "path": f"f{i}.py"} for i in range(1000)]}
    with mock.patch.object(mod, "list_contents", return_value=full_page), \
            mock.patch.object(mod, "get_raw_file", return_value="print('x')  # smoke"), \
            mock.patch("time.sleep", lambda *a, **k: None):
        _matches, meta = mod.recursive_search(
            "https://sc.smoke.test/api/v3", "t", "p", "repo", "main", ["smoke"],
            path_prefix="", timeout=1.0, max_files=10, max_matches_per_file=1, context=1,
            max_pages=2,
        )
    notes = meta.get("notes") or []
    assert any("truncated at max_pages=2" in n for n in notes), f"expected max_pages truncation note, got {notes!r}"


register("[pagination] source_control recursive search max_pages note", _sc_pagination_guard_adds_note)


def _sc_scanned_cap_note_on_exact_boundary():
    # Finding 3 (edge): the scanned-file cap note must also fire when the last
    # scanned file lands scanned_files exactly on max_files while a subdirectory
    # is still queued. Here the root listing yields a subdir plus exactly
    # max_files text files, so the outer loop exits (scanned_files == max_files)
    # with 'sub' unvisited -- genuine truncation that previously went unreported.
    mod = importlib.import_module("source_control_search")
    root_page = {"content": [
        {"type": "dir", "path": "sub"},
        {"type": "file", "path": "a.py"},
        {"type": "file", "path": "b.py"},
        {"type": "file", "path": "c.py"},
    ]}
    with mock.patch.object(mod, "list_contents", return_value=root_page), \
            mock.patch.object(mod, "get_raw_file", return_value="print('x')  # smoke"), \
            mock.patch("time.sleep", lambda *a, **k: None):
        _matches, meta = mod.recursive_search(
            "https://sc.smoke.test/api/v3", "t", "p", "repo", "main", ["smoke"],
            path_prefix="", timeout=1.0, max_files=3, max_matches_per_file=1, context=1,
            max_pages=2,
        )
    notes = meta.get("notes") or []
    assert any("Scanned-file cap reached (max_files=3)" in n for n in notes), \
        f"expected scanned-file cap note at exact boundary with queue remaining, got {notes!r}"


register("[pagination] source_control scanned-file cap note at exact boundary", _sc_scanned_cap_note_on_exact_boundary)


def _sc_pagination_guard_e2e_via_cli():
    # Task 3: the same guard, but driven end-to-end through the CLI (parse_args ->
    # cmd_search -> args.max_pages) so the argparse wiring (default/flag) is
    # exercised, not just recursive_search called directly. --no-archive forces
    # the contents/raw fallback; list_contents keeps returning a full page so the
    # per-directory pagination trips --max-pages and the note reaches CLI output.
    mod = importlib.import_module("source_control_search")
    full_page = {"content": [{"type": "file", "path": f"f{i}.py"} for i in range(1000)]}
    argv = ["search", "--tenant", "smoke-tenant", "--project", "smoke-project", "--repo", "smoke-repo",
            "--ref", "main", "--query", "smoke", "--no-archive", "--max-pages", "2"]
    transport = Transport("ok")
    with mock.patch.object(mod, "list_contents", return_value=full_page), \
            mock.patch.object(mod, "get_raw_file", return_value="print('x')  # smoke"), \
            mock.patch("urllib.request.urlopen", side_effect=transport), \
            mock.patch("time.sleep", lambda *a, **k: None):
        rc, out, err = _run_main(mod, argv, uses_argv=True)
    assert rc == 0, f"rc={rc}; stderr={err[:300]!r}"
    data = json.loads(out)
    assert data.get("command") == "search", f"unexpected command: {out[:200]}"
    notes = data.get("notes") or []
    assert any("truncated at max_pages=2" in n for n in notes), f"expected max_pages note in CLI output, got {notes!r}"


register("[pagination] source_control search --max-pages note via CLI", _sc_pagination_guard_e2e_via_cli)


def _sc_search_archive_unavailable_recursive_empty_is_exit0():
    # REGRESSION (the iter-3 total-failure guard was too broad): the archive
    # download is only an OPTIMIZATION over recursive traversal. When it is
    # unavailable the code falls back to recursive_search; if that fallback
    # COMPLETES CLEANLY but legitimately finds ZERO matches, the benign "archive
    # unavailable" error must NOT fail the command -- it is a valid empty result
    # (exit 0) with the archive error surfaced as a NOTE, never the unified error
    # contract (exit 1). Mirrors the codex repro: a hex --ref makes
    # resolve_ref_to_sha short-circuit (no network), search_archive is forced to
    # raise, and recursive_search returns an empty result with a CLEAN meta (no
    # errors of its own).
    mod = importlib.import_module("source_control_search")
    clean_meta = {"method": "recursive_contents", "visitedDirs": 1, "scannedFiles": 0, "errors": []}
    argv = ["search", "--tenant", "smoke-tenant", "--project", "smoke-project",
            "--repo", "smoke-repo", "--ref", "deadbeef", "--query", "foo"]
    transport = Transport("ok")
    with mock.patch.object(mod, "search_archive", side_effect=RuntimeError("Archive endpoint unavailable")), \
            mock.patch.object(mod, "recursive_search", return_value=([], dict(clean_meta))), \
            mock.patch("urllib.request.urlopen", side_effect=transport), \
            mock.patch("time.sleep", lambda *a, **k: None):
        rc, out, err = _run_main(mod, argv, uses_argv=True)
    assert rc == 0, f"benign archive-unavailable + clean empty recursive must exit 0, got rc={rc}; stderr={err[:400]!r}"
    assert not err.strip(), f"exit-0 empty result must not print an error contract to stderr: {err[:300]!r}"
    data = json.loads(out)
    assert data.get("command") == "search", f"unexpected command: {out[:200]}"
    assert data.get("matchCount") == 0 and data.get("matches") == [], f"expected empty result, got {out[:300]}"
    notes = list(data.get("notes") or []) + list((data.get("meta") or {}).get("notes") or [])
    assert any("Archive search failed" in n or "Archive endpoint unavailable" in n for n in notes), \
        f"archive-unavailable must surface as a NOTE, not fail the command; notes={notes!r}"
    # The recursive traversal reported no errors of its own -> no fatal error and
    # the archive error is NOT promoted into meta.errors.
    assert not (data.get("meta") or {}).get("errors"), f"clean recursive meta must have no errors, got {data.get('meta')!r}"


register("[regression] source_control search --repo: archive unavailable + clean empty recursive -> exit 0 (note, not error)",
         _sc_search_archive_unavailable_recursive_empty_is_exit0)


def _sc_search_clean_empty_archive_is_authoritative_no_fallback():
    # REGRESSION (the total-failure guard was too broad the OTHER way): when the
    # archive search COMPLETES CLEANLY and legitimately finds ZERO matches, that
    # empty result is AUTHORITATIVE. The recursive traversal is only a FALLBACK for
    # when the archive path could not run, so it must NOT be invoked at all, and a
    # (hypothetical) recursive failure must never be able to override the valid
    # empty archive result. Here a hex --ref makes resolve_ref_to_sha short-circuit
    # (no network), search_archive returns ([], clean archive meta) WITHOUT raising,
    # and recursive_search is monkeypatched to RAISE if it is ever called -- so a
    # broken guard that ran the fallback would surface as exit 1.
    mod = importlib.import_module("source_control_search")
    archive_meta = {"method": "archive", "sha": "deadbeef", "archiveBytes": 4096,
                    "scannedFiles": 7, "skippedFiles": 1}
    argv = ["search", "--tenant", "smoke-tenant", "--project", "smoke-project",
            "--repo", "smoke-repo", "--ref", "deadbeef", "--query", "foo"]
    transport = Transport("ok")
    with mock.patch.object(mod, "search_archive", return_value=([], dict(archive_meta))), \
            mock.patch.object(mod, "recursive_search",
                              side_effect=RuntimeError("recursive_search must NOT run when archive succeeded")) as rec_mock, \
            mock.patch("urllib.request.urlopen", side_effect=transport), \
            mock.patch("time.sleep", lambda *a, **k: None):
        rc, out, err = _run_main(mod, argv, uses_argv=True)
    assert rc == 0, f"clean empty archive result must be authoritative (exit 0), got rc={rc}; stderr={err[:400]!r}"
    assert not err.strip(), f"authoritative empty result must not print an error contract to stderr: {err[:300]!r}"
    assert rec_mock.call_count == 0, \
        f"recursive_search must NOT be called when the archive search completed (archive_ok); calls={rec_mock.call_count}"
    data = json.loads(out)
    assert data.get("command") == "search", f"unexpected command: {out[:200]}"
    assert data.get("matchCount") == 0 and data.get("matches") == [], f"expected empty authoritative result, got {out[:300]}"
    # The authoritative archive meta (not a recursive fallback) is what is reported.
    assert (data.get("meta") or {}).get("method") == "archive", \
        f"empty result must carry the archive meta, not a recursive fallback meta, got {data.get('meta')!r}"
    assert not (data.get("meta") or {}).get("errors"), f"clean archive meta must have no errors, got {data.get('meta')!r}"


register("[regression] source_control search --repo: clean empty archive is authoritative -> exit 0, recursive NOT called",
         _sc_search_clean_empty_archive_is_authoritative_no_fallback)


def _sc_search_partial_scan_benign_errors_is_exit0():
    # iter-6 #1 (the fragile empty-error guard is gone): a PARTIAL scan -- the
    # recursive traversal COMPLETES, finds 0 matches, but hit a benign per-file/dir
    # error (one unreadable path, a contents 404) -- must EXIT 0 with meta.errors
    # surfaced, NOT the unified error contract. Only missing auth is fatal now; a
    # benign per-item error never fails the command. --no-archive forces the
    # contents/raw fallback (no resolve/archive step); recursive_search returns
    # ([], meta-with-errors). Under the OLD total-failure guard this exited 1.
    mod = importlib.import_module("source_control_search")
    partial_meta = {"method": "recursive_contents", "visitedDirs": 3, "scannedFiles": 5,
                    "errors": ["contents 404 for path src/generated/x.py"]}
    argv = ["search", "--tenant", "smoke-tenant", "--project", "smoke-project",
            "--repo", "smoke-repo", "--query", "foo", "--no-archive"]
    transport = Transport("ok")
    with mock.patch.object(mod, "recursive_search", return_value=([], dict(partial_meta))), \
            mock.patch("urllib.request.urlopen", side_effect=transport), \
            mock.patch("time.sleep", lambda *a, **k: None):
        rc, out, err = _run_main(mod, argv, uses_argv=True)
    assert rc == 0, f"partial scan with benign errors must exit 0, got rc={rc}; stderr={err[:400]!r}"
    assert not err.strip(), f"exit-0 partial result must not print an error contract to stderr: {err[:300]!r}"
    data = json.loads(out)
    assert data.get("command") == "search", f"unexpected command: {out[:200]}"
    assert data.get("matchCount") == 0 and data.get("matches") == [], f"expected empty result, got {out[:300]}"
    assert (data.get("meta") or {}).get("errors") == ["contents 404 for path src/generated/x.py"], \
        f"benign per-item errors must be surfaced in meta.errors (not fatal), got {data.get('meta')!r}"


register("[partial] source_control search --repo: recursive empty + benign meta.errors -> exit 0 (not error)",
         _sc_search_partial_scan_benign_errors_is_exit0)


def _sc_search_with_results_has_no_top_level_notes():
    # iter-6 #2 (byte-identical output): a normal `search --repo` WITH results must
    # emit the SAME top-level field set as main -- {command, tenant, project, repo,
    # ref, pathPrefix, queries, meta, matches, matchCount} -- with NO top-level
    # `notes` key. The top-level notes key is added ONLY when non-empty; a clean
    # archive search with matches has no notes, so it must be ABSENT. A hex --ref
    # short-circuits resolve_ref_to_sha (no network); search_archive returns a match
    # with a clean archive meta; recursive_search must NOT run (matches present).
    mod = importlib.import_module("source_control_search")
    hit = {"path": "src/pay/gateway.py", "line": 42, "score": 4, "snippet": "def charge(): ..."}
    archive_meta = {"method": "archive", "sha": "deadbeef", "archiveBytes": 8192,
                    "scannedFiles": 12, "skippedFiles": 2}
    argv = ["search", "--tenant", "smoke-tenant", "--project", "smoke-project",
            "--repo", "smoke-repo", "--ref", "deadbeef", "--query", "charge"]
    transport = Transport("ok")
    with mock.patch.object(mod, "search_archive", return_value=([dict(hit)], dict(archive_meta))), \
            mock.patch.object(mod, "recursive_search",
                              side_effect=RuntimeError("recursive_search must NOT run when the archive search found matches")), \
            mock.patch("urllib.request.urlopen", side_effect=transport), \
            mock.patch("time.sleep", lambda *a, **k: None):
        rc, out, err = _run_main(mod, argv, uses_argv=True)
    assert rc == 0, f"search with results must exit 0, got rc={rc}; stderr={err[:400]!r}"
    data = json.loads(out)
    assert "notes" not in data, \
        f"a results-bearing search must NOT carry a top-level `notes` key (byte-identical to main); got keys {sorted(data)!r}"
    expected_keys = {"command", "tenant", "project", "repo", "ref", "pathPrefix",
                     "queries", "meta", "matches", "matchCount"}
    assert set(data) == expected_keys, \
        f"top-level field set drifted from main: {sorted(data)!r} != {sorted(expected_keys)!r}"
    assert data.get("matchCount") == 1 and len(data.get("matches") or []) == 1, f"expected one ranked match, got {out[:300]}"
    assert (data.get("meta") or {}).get("method") == "archive", f"expected authoritative archive meta, got {data.get('meta')!r}"


register("[byte-identical] source_control search --repo with results has no top-level notes key",
         _sc_search_with_results_has_no_top_level_notes)


def _malformed_body_json_unified_error():
    # Robustness: malformed local JSON (--body-json / --extra-or) must surface as
    # the unified error contract on stderr, not a raw Python traceback. No network.
    transport = Transport("ok")
    rc, out, err = run("swtr_unit_search", ["--body-json", "{not json"], transport)
    _assert_unified_error_contract(out, err, rc, kind="bad-body-json")
    assert transport.calls == [], f"malformed body must not hit network, got {transport.calls}"


register("[robust] swtr_unit_search --body-json invalid -> unified error", _malformed_body_json_unified_error)


def _malformed_extra_or_unified_error():
    transport = Transport("ok")
    rc, out, err = run("swtr_task_tool", ["count", "payment", "--extra-or", "{not json"], transport)
    _assert_unified_error_contract(out, err, rc, kind="bad-extra-or")
    assert transport.calls == [], f"malformed --extra-or must not hit network, got {transport.calls}"


register("[robust] swtr_task_tool count --extra-or invalid -> unified error", _malformed_extra_or_unified_error)


# ---- base URL present ONLY in a loaded env file must be honored ----
# Regression guard for the argparse-timing bug: --base-url used to bake its
# default in from os.environ AT PARSE TIME (before load_env_file ran), so a base
# URL set only in ~/.swtr-review.env was ignored. Now it is resolved AFTER the
# env file loads, so the recorded request must go to the env-file host.
def _make_base_url_env_file_test(module, argv, env_names, env_line, expected_prefix, kind):
    def _t():
        env_path = Path(_TMP_DIR) / f"base-url-{kind}.env"
        env_path.write_text(env_line + "\n", encoding="utf-8")
        saved = {}
        for name in env_names:
            if name in os.environ:
                saved[name] = os.environ.pop(name)
        transport = Transport("ok")
        try:
            rc, out, err = run(module, list(argv) + ["--env-file", str(env_path)], transport)
        finally:
            os.environ.update(saved)
        assert rc == 0, f"{kind}: expected rc=0, got {rc}; stderr={err[:300]!r}"
        assert transport.calls, f"{kind}: expected at least one request, got none"
        wrong = [c["url"] for c in transport.calls if not c["url"].startswith(expected_prefix)]
        assert not wrong, f"{kind}: request(s) ignored env-file base URL {expected_prefix!r}: {wrong[:3]!r}"

    return _t


for _mod, _argv, _names, _line, _prefix, _kind in (
    ("swtr_task_tool", ["get", "SMOKE-1"], ("SWTR_BASE_URL",),
     "SWTR_BASE_URL=https://swtr.envfile.test/swtr", "https://swtr.envfile.test/swtr", "swtr"),
    ("source_control_search", ["repos", "--tenant", "smoke-tenant", "--project", "smoke-project"],
     ("SOURCE_CONTROL_BASE_URL", "SC_BASE_URL"),
     "SOURCE_CONTROL_BASE_URL=https://sc.envfile.test/api/v3", "https://sc.envfile.test/api/v3", "source_control"),
):
    register(f"[base-url] {_kind}: base URL from env file honored", _make_base_url_env_file_test(
        _mod, _argv, _names, _line, _prefix, _kind))


def _similar_partial_success_stays_exit0():
    # Partial success must be preserved: if AT LEAST ONE term query succeeds, the
    # command emits its aggregate at exit 0 even though another term hit a BENIGN
    # error. Here the first urlopen returns a benign 404 (not an auth failure, not
    # retried), the rest succeed. A 404 is a per-item error and stays collected in
    # rawRuns -- only a 401/403 auth error is fatal (see the [auth-fatal] cases).
    state = {"n": 0}

    def side_effect(request, timeout=None, context=None):
        state["n"] += 1
        url = request.full_url
        if state["n"] == 1:
            raise _http_error(url, 404, {"message": "first term not found"})
        return _FakeResponse(200, route_fixture(url))

    rc, out, err = run("swtr_task_tool", ["similar", "payment gateway integration"], side_effect)
    assert rc == 0, f"partial success must exit 0, got rc={rc}; stderr={err[:300]!r}"
    data = json.loads(out)
    assert data.get("command") == "similar", f"unexpected command: {out[:200]}"
    runs = data.get("rawRuns") or []
    assert any("error" in r for r in runs), f"expected an errored term run, got {runs!r}"
    assert any("payload" in r for r in runs), f"expected a successful term run, got {runs!r}"


register("[partial] swtr_task_tool similar: one term benign-errors, rest succeed -> exit 0",
         _similar_partial_success_stays_exit0)


# ---- iter-7: AUTH errors (401/403) are FATAL across the best-effort multi-item
# commands (search --repo / similar / tms test-case); benign 404/timeout stay
# collected -> exit 0. is_auth_error(exc) walks the RuntimeError->HTTPError cause
# chain so a 401/403 propagates to the unified error contract while a 404 does not.

def _sc_search_repo_401_recursive_is_fatal():
    # search --repo with an invalid token (HTTP 401 on every call). --no-archive
    # forces the contents/raw fallback: recursive_search's list_contents 401 is
    # re-raised (not collected) -> main -> emit_error -> exit 1. Previously this
    # swallowed the 401 into meta.errors and printed an empty result at exit 0.
    argv = ["search", "--tenant", "smoke-tenant", "--project", "smoke-project",
            "--repo", "smoke-repo", "--query", "foo", "--no-archive"]
    rc, out, err = run("source_control_search", argv, Transport("error", status=401))
    _assert_unified_error_contract(out, err, rc, kind="sc-search-401-recursive")


register("[auth-fatal] source_control search --repo (recursive path) 401 -> exit 1",
         _sc_search_repo_401_recursive_is_fatal)


def _sc_search_repo_401_archive_is_fatal():
    # search --repo via the ARCHIVE path (a hex --ref short-circuits resolve, so
    # search_archive runs) with an invalid token: the archive `except Exception`
    # site re-raises the 401 -> unified error contract, exit 1.
    argv = ["search", "--tenant", "smoke-tenant", "--project", "smoke-project",
            "--repo", "smoke-repo", "--ref", "deadbeef", "--query", "foo"]
    rc, out, err = run("source_control_search", argv, Transport("error", status=401))
    _assert_unified_error_contract(out, err, rc, kind="sc-search-401-archive")


register("[auth-fatal] source_control search --repo (archive path) 401 -> exit 1",
         _sc_search_repo_401_archive_is_fatal)


def _sc_search_repo_403_is_fatal():
    # 403 is treated identically to 401 (both are auth failures).
    argv = ["search", "--tenant", "smoke-tenant", "--project", "smoke-project",
            "--repo", "smoke-repo", "--query", "foo", "--no-archive"]
    rc, out, err = run("source_control_search", argv, Transport("error", status=403))
    _assert_unified_error_contract(out, err, rc, kind="sc-search-403")


register("[auth-fatal] source_control search --repo 403 -> exit 1", _sc_search_repo_403_is_fatal)


def _sc_search_repo_benign_404_is_exit0():
    # BENIGN 404 during traversal: a path 404 in recursive_search is NOT an auth
    # failure -- it is collected into meta.errors and the command exits 0 with an
    # empty (best-effort) result. --no-archive forces the recursive path so the
    # 404 surfaces from list_contents.
    argv = ["search", "--tenant", "smoke-tenant", "--project", "smoke-project",
            "--repo", "smoke-repo", "--query", "foo", "--no-archive"]
    rc, out, err = run("source_control_search", argv, Transport("error", status=404))
    assert rc == 0, f"benign 404 during traversal must exit 0, got rc={rc}; stderr={err[:400]!r}"
    assert not err.strip(), f"exit-0 benign result must not print an error contract to stderr: {err[:300]!r}"
    data = json.loads(out)
    assert data.get("command") == "search", f"unexpected command: {out[:200]}"
    assert data.get("matchCount") == 0 and data.get("matches") == [], f"expected empty result, got {out[:300]}"
    errors = (data.get("meta") or {}).get("errors") or []
    assert any("404" in e for e in errors), f"benign 404 must be surfaced in meta.errors, got {data.get('meta')!r}"


register("[benign] source_control search --repo path 404 -> exit 0 (meta.errors)",
         _sc_search_repo_benign_404_is_exit0)


def _sc_recursive_get_raw_file_401_is_fatal():
    # Directly exercise the get_raw_file re-raise inside recursive_search: a
    # directory listing succeeds (one text file), but reading that file raises an
    # AUTH RuntimeError (RuntimeError whose __cause__ is HTTPError 401). It must
    # propagate OUT of recursive_search, not be collected into meta.errors.
    mod = importlib.import_module("source_control_search")
    one_file = {"content": [{"type": "file", "path": "app.py"}]}

    def _raise_auth(*a, **k):
        cause = _http_error("https://sc.smoke.test/api/v3/x", 401, {"m": "no"})
        raise RuntimeError("HTTP 401 for https://sc.smoke.test/api/v3/x: denied") from cause

    with mock.patch.object(mod, "list_contents", return_value=one_file), \
            mock.patch.object(mod, "get_raw_file", side_effect=_raise_auth), \
            mock.patch("time.sleep", lambda *a, **k: None):
        try:
            mod.recursive_search(
                "https://sc.smoke.test/api/v3", "t", "p", "repo", "main", ["smoke"],
                path_prefix="", timeout=1.0, max_files=10, max_matches_per_file=1, context=1,
                max_pages=2,
            )
        except RuntimeError as exc:
            assert mod.is_auth_error(exc), f"propagated error must be the auth error, got {exc!r}"
        else:
            raise AssertionError("recursive_search must re-raise an auth error from get_raw_file, not collect it")


register("[auth-fatal] recursive_search get_raw_file 401 propagates out",
         _sc_recursive_get_raw_file_401_is_fatal)


def _sc_recursive_get_raw_file_404_is_collected():
    # The benign counterpart: a 404 while reading one file is collected into
    # meta.errors and recursive_search returns normally (best-effort, no raise).
    mod = importlib.import_module("source_control_search")
    one_file = {"content": [{"type": "file", "path": "app.py"}]}

    def _raise_benign(*a, **k):
        cause = _http_error("https://sc.smoke.test/api/v3/x", 404, {"m": "gone"})
        raise RuntimeError("HTTP 404 for https://sc.smoke.test/api/v3/x: not found") from cause

    with mock.patch.object(mod, "list_contents", return_value=one_file), \
            mock.patch.object(mod, "get_raw_file", side_effect=_raise_benign), \
            mock.patch("time.sleep", lambda *a, **k: None):
        _matches, meta = mod.recursive_search(
            "https://sc.smoke.test/api/v3", "t", "p", "repo", "main", ["smoke"],
            path_prefix="", timeout=1.0, max_files=10, max_matches_per_file=1, context=1,
            max_pages=2,
        )
    errors = meta.get("errors") or []
    assert any("404" in e for e in errors), f"benign 404 must be collected in meta.errors, got {meta!r}"


register("[benign] recursive_search get_raw_file 404 collected in meta.errors",
         _sc_recursive_get_raw_file_404_is_collected)


def _similar_401_is_fatal():
    # similar with an invalid token: the per-term loop's 401 is re-raised (not
    # collected into rawRuns), so a wholly-unauthorized run yields the unified
    # error contract at exit 1 instead of an empty exit-0 aggregate.
    rc, out, err = run("swtr_task_tool", ["similar", "payment gateway integration"],
                       Transport("error", status=401))
    _assert_unified_error_contract(out, err, rc, kind="similar-401")


register("[auth-fatal] swtr_task_tool similar 401 -> exit 1", _similar_401_is_fatal)


def _similar_403_is_fatal():
    rc, out, err = run("swtr_task_tool", ["similar", "payment gateway integration"],
                       Transport("error", status=403))
    _assert_unified_error_contract(out, err, rc, kind="similar-403")


register("[auth-fatal] swtr_task_tool similar 403 -> exit 1", _similar_403_is_fatal)


def _tms_test_case_401_is_fatal():
    # tms test-case has a catch_errors path that normally wraps a failure as a
    # graceful data.error at exit 0. An AUTH failure (401) must NOT be swallowed:
    # is_auth_error re-raises it to the unified error contract, exit 1.
    rc, out, err = run("swtr_task_tool", ["tms", "test-case", "--unit", "SMOKE-U-1"],
                       Transport("error", status=401))
    _assert_unified_error_contract(out, err, rc, kind="tms-test-case-401")


register("[auth-fatal] swtr_task_tool tms test-case 401 -> exit 1", _tms_test_case_401_is_fatal)


def _tms_test_case_benign_404_is_exit0():
    # BENIGN test-case-not-found (404): the catch_errors path keeps its graceful
    # behavior -- exit 0 with the error surfaced in data.error (unchanged).
    rc, out, err = run("swtr_task_tool", ["tms", "test-case", "--unit", "SMOKE-U-1"],
                       Transport("error", status=404))
    assert rc == 0, f"benign tms test-case 404 must exit 0, got rc={rc}; stderr={err[:400]!r}"
    assert not err.strip(), f"exit-0 benign tms result must not print an error contract: {err[:300]!r}"
    data = json.loads(out)
    assert data.get("command") == "tms" and data.get("kind") == "test-case", f"unexpected: {out[:200]}"
    payload = data.get("data") or {}
    assert isinstance(payload, dict) and "404" in str(payload.get("error", "")), \
        f"benign 404 must be surfaced in data.error, got {payload!r}"


register("[benign] swtr_task_tool tms test-case 404 -> exit 0 (data.error)",
         _tms_test_case_benign_404_is_exit0)


# ---- missing auth -> unified error contract, exit 1, no traceback, no network ----
# build_headers() raises RuntimeError (not SystemExit) on missing/misconfigured
# auth so each CLI main's `except RuntimeError -> emit_error` prints the unified
# {"ok": false, "command", "error"} contract. With every credential env for a
# backend unset, the tool must fail cleanly BEFORE any socket is opened. The auth
# env for the chosen backend is stripped for the duration of the case, then
# restored (the harness pre-seeds fake tokens for the happy path).
_AUTH_ENV_BY_SYSTEM = {
    "task_tracker": ("SWTR_AUTHORIZATION", "TASKTRACKER_AUTHORIZATION", "SWTR_BASIC_AUTH",
                     "SWTR_BEARER_TOKEN", "SWTR_TOKEN", "TASKTRACKER_TOKEN"),
    "source_control": ("SOURCE_CONTROL_AUTHORIZATION", "SC_AUTHORIZATION", "SOURCE_CONTROL_BASIC_AUTH",
                       "SC_BASIC_AUTH", "SOURCE_CONTROL_BEARER_TOKEN", "SC_BEARER_TOKEN",
                       "SOURCE_CONTROL_TOKEN", "SC_TOKEN", "SOURCE_CONTROL_REUSE_SWTR_AUTH", "SC_REUSE_SWTR_AUTH"),
    "confluence": ("CONFLUENCE_AUTHORIZATION", "CONFLUENCE_BEARER_TOKEN", "CONFLUENCE_TOKEN",
                   "CONFLUENCE_BASIC_AUTH", "SBERWORKS_TOKEN"),
    "jira": ("JIRA_AUTHORIZATION", "JIRA_BEARER_TOKEN", "JIRA_TOKEN", "JIRA_BASIC_AUTH"),
}
_NO_AUTH_CASES = [
    ("swtr_task_tool", ["get", "SMOKE-1"], "task_tracker", "swtr get"),
    ("spec_sources", ["SMOKE-1"], "task_tracker", "spec_sources pack"),
    ("jira_tool", ["get", "SMOKE-100", "--confirm-sberworks"], "jira", "jira get"),
    ("confluence_tool", ["get", "1001", "--confirm-sberworks"], "confluence", "confluence get"),
    ("source_control_search", ["repos", "--tenant", "smoke-tenant", "--project", "smoke-project"],
     "source_control", "source_control repos"),
    # Total-failure guard: a free-text `similar` (every term query fails auth) and
    # a `search --repo` (every probe fails auth) must NOT report an exit-0 empty
    # result -- they surface the unified error contract before any socket opens.
    ("swtr_task_tool", ["similar", "payment gateway integration"], "task_tracker", "swtr similar"),
    ("source_control_search",
     ["search", "--tenant", "smoke-tenant", "--project", "smoke-project", "--repo", "smoke-repo", "--query", "foo"],
     "source_control", "source_control search --repo"),
]


def _make_no_auth_test(module, argv, system, kind):
    def _t():
        saved = {}
        for name in _AUTH_ENV_BY_SYSTEM[system]:
            if name in os.environ:
                saved[name] = os.environ.pop(name)
        transport = Transport("ok")
        try:
            rc, out, err = run(module, argv, transport)
        finally:
            os.environ.update(saved)
        _assert_unified_error_contract(out, err, rc, kind=kind)
        assert "Traceback" not in (out + err), f"{kind}: missing auth must not traceback: {(out + err)[:400]!r}"
        assert transport.calls == [], f"{kind}: missing auth must fail before any network call, got {transport.calls}"

    return _t


for _mod, _argv, _system, _kind in _NO_AUTH_CASES:
    register(f"[no-auth] {_kind} -> unified error, no network", _make_no_auth_test(_mod, _argv, _system, _kind))


# ---- --help exits 0 for every script ----
for _mod in (
    "swtr_task_tool", "fetch_swtr_sources", "spec_sources", "swtr_unit_search", "swtr_unit_comments",
    "source_control_search", "confluence_tool", "jira_tool", "guru_doctor", "local_context_search",
    "guru_catalog",
):
    add_help(_mod)


def _doctor_extract_identity():
    d = importlib.import_module("guru_doctor")
    # Confluence: authenticated only when type != anonymous AND username/userKey present.
    known = d.extract_identity("confluence", {"type": "known", "username": "u.one", "userKey": "ff01"})
    assert known.authenticated is True and known.kind == "known", f"confluence known: {known!r}"
    anon = d.extract_identity("confluence", {"type": "anonymous", "displayName": "Anonymous"})
    assert anon.authenticated is False and anon.kind == "anonymous", f"confluence anon: {anon!r}"
    empty = d.extract_identity("confluence", {"type": "known"})  # no username/userKey
    assert empty.authenticated is False, f"confluence no-id must not be authenticated: {empty!r}"
    # Jira: authenticated when name or key present.
    jira_ok = d.extract_identity("jira", {"name": "u.one", "key": "JIRAUSER1"})
    assert jira_ok.authenticated is True, f"jira known: {jira_ok!r}"
    jira_no = d.extract_identity("jira", {})
    assert jira_no.authenticated is False, f"jira empty: {jira_no!r}"
    # Non-dict body is never authenticated.
    assert d.extract_identity("confluence", None).authenticated is False


register("[doctor-unit] extract_identity confluence/jira", _doctor_extract_identity)


def _doctor_classify_verdict():
    d = importlib.import_module("guru_doctor")
    ident_ok = d.Identity(True, "known", "type=known")
    ident_anon = d.Identity(False, "anonymous", "type=anonymous")
    # Confluence/Jira judge identity, not the 200.
    assert d.classify_verdict("confluence", 200, ident_ok).category == "ok"
    v_anon = d.classify_verdict("confluence", 200, ident_anon)
    assert v_anon.ok is False and v_anon.category == "anonymous", f"anon verdict: {v_anon!r}"
    assert d.classify_verdict("jira", 200, ident_ok).category == "ok"
    assert d.classify_verdict("jira", 401, None).category == "rejected"
    assert d.classify_verdict("jira", 403, None).category == "forbidden"
    # Status-based systems.
    assert d.classify_verdict("task_tracker", 200, None).category == "ok"
    assert d.classify_verdict("task_tracker", 403, None).category == "forbidden"
    tt404 = d.classify_verdict("task_tracker", 404, None)
    assert tt404.ok is False and tt404.category == "inconclusive", f"tt 404: {tt404!r}"
    assert d.classify_verdict("task_tracker", 500, None).category == "server_error"
    sc200 = d.classify_verdict("source_control", 200, None)
    assert sc200.ok is True and sc200.category == "ok", f"sc 200: {sc200!r}"
    sc404 = d.classify_verdict("source_control", 404, None)
    assert sc404.ok is False and sc404.category == "inconclusive", f"sc 404: {sc404!r}"
    assert d.classify_verdict("source_control", None, None).category == "network"


register("[doctor-unit] classify_verdict per system", _doctor_classify_verdict)


def _gateway_auth_failure_detection():
    c = importlib.import_module("_swtr_common")
    # The portal gateway's own page under 401 and under 403 alike.
    assert c.is_gateway_auth_failure(403, GATEWAY_HTML_CONTENT_TYPE, GATEWAY_AUTH_PAGE) is True
    assert c.is_gateway_auth_failure(401, GATEWAY_HTML_CONTENT_TYPE, GATEWAY_AUTH_PAGE) is True
    # An HTML body on 401/403 from a JSON API is the gateway even without the
    # Russian markers -- the page text is not a contract we control.
    assert c.is_gateway_auth_failure(403, "text/html", "<html><body>Forbidden</body></html>") is True
    # SourceControl's real token error is a TOKEN problem, not a domain password one.
    sc_token_error = ('{"type":null,"title":"Ошибка токена","status":401,'
                      '"detail":"Проверьте токен доступа"}')
    assert c.is_gateway_auth_failure(401, "application/problem+json", sc_token_error) is False
    # SWTR's own JSON error stays an application-level verdict.
    app_error = '{"uiErrorMessage":"Нет прав","errorType":"SWTR_BASIC_ERROR"}'
    assert c.is_gateway_auth_failure(403, "application/json;charset=UTF-8", app_error) is False
    # Only 401/403 can be an auth failure; a 404 HTML page is not one.
    assert c.is_gateway_auth_failure(404, GATEWAY_HTML_CONTENT_TYPE, GATEWAY_AUTH_PAGE) is False
    assert c.is_gateway_auth_failure(200, GATEWAY_HTML_CONTENT_TYPE, GATEWAY_AUTH_PAGE) is False
    # Missing/empty inputs must never raise.
    assert c.is_gateway_auth_failure(403, None, None) is False
    assert c.is_gateway_auth_failure(None, None, None) is False


register("[doctor-unit] the portal auth-failure page is told apart from app-level errors",
         _gateway_auth_failure_detection)


def _doctor_auth_page_verdict_blames_the_password():
    d = importlib.import_module("guru_doctor")
    for status in (401, 403):
        for system in ("task_tracker", "source_control"):
            v = d.classify_verdict(system, status, None, auth_page=True)
            assert v.ok is False, v
            assert v.category == "stale_password", \
                "%s %s with the gateway page is a stale password: %r" % (system, status, v)
            low = v.detail.lower()
            assert "password" in low, "the verdict must name the password: %r" % (v.detail,)
            for wrong in ("personal access token", "swtr_bearer_token", "swtr_authorization"):
                assert wrong not in low, \
                    "a stale password must never be answered with token advice: %r" % (v.detail,)
    # Without the page, a bare 403 keeps the application-level PAT advice.
    assert d.classify_verdict("task_tracker", 403, None).category == "forbidden"
    assert d.classify_verdict("task_tracker", 401, None).category == "rejected"


register("[doctor-unit] a portal auth-failure page is reported as a stale domain password",
         _doctor_auth_page_verdict_blames_the_password)


def _doctor_probe_surfaces_the_auth_page():
    d = importlib.import_module("guru_doctor")
    transport = Transport("error", status=403, error_body=GATEWAY_AUTH_PAGE_BYTES,
                          error_content_type=GATEWAY_HTML_CONTENT_TYPE)
    tls = d.TlsState(True, None, None)
    with mock.patch("urllib.request.urlopen", side_effect=transport):
        result = d.probe("https://swtr.smoke.test/swtr/rest/api/unit/v2/CRPV-1", "task_tracker", tls, 5.0)
    assert result.status == 403, result
    assert result.auth_page is True, \
        "probe must read the error body -- discarding it is what hid the real cause: %r" % (result,)
    # An application-level JSON 403 is not an auth page.
    json_transport = Transport("error", status=403)
    with mock.patch("urllib.request.urlopen", side_effect=json_transport):
        plain = d.probe("https://swtr.smoke.test/swtr/rest/api/unit/v2/CRPV-1", "task_tracker", tls, 5.0)
    assert plain.status == 403 and plain.auth_page is False, plain


register("[doctor-unit] probe reads the error body and flags the auth page",
         _doctor_probe_surfaces_the_auth_page)


def _doctor_end_to_end_stale_password():
    """A whole --system task_tracker run over the gateway page: the printed verdict
    must send the user to their domain password and the system must be repairable."""
    transport = Transport("error", status=403, error_body=GATEWAY_AUTH_PAGE_BYTES,
                          error_content_type=GATEWAY_HTML_CONTENT_TYPE)
    rc, out, err = run("guru_doctor", ["--system", "task_tracker"], transport)
    assert rc != 0, "a rejected credential is not a pass: rc=%s" % rc
    low = out.lower()
    assert "password" in low, "the block must name the password: " + out[-600:]
    assert "swtr_bearer_token" not in low and "personal access token" not in low, \
        "the PAT advice must not appear for a stale password: " + out[-600:]
    assert "FIXABLE: task_tracker" in out, \
        "the doctor must offer to repair a stale password: " + out[-600:]


register("[doctor] a stale password ends in a repairable password verdict",
         _doctor_end_to_end_stale_password)


def _doctor_tls_state():
    d = importlib.import_module("guru_doctor")
    tls_flags = ("SOURCE_CONTROL_INSECURE_SKIP_TLS_VERIFY", "SC_INSECURE_SKIP_TLS_VERIFY",
                 "CONFLUENCE_INSECURE_SKIP_TLS_VERIFY", "JIRA_INSECURE_SKIP_TLS_VERIFY",
                 "SWTR_INSECURE_SKIP_TLS_VERIFY")
    saved = {n: os.environ.pop(n, None) for n in tls_flags}
    try:
        # No flags set -> verified, no context.
        st = d.doctor_tls_state("confluence")
        assert st.verified is True and st.context is None and st.flag is None, f"clean: {st!r}"
        # Global SWTR flag is honored as a fallback for confluence/jira.
        os.environ["SWTR_INSECURE_SKIP_TLS_VERIFY"] = "1"
        st = d.doctor_tls_state("jira")
        assert st.verified is False and st.flag == "SWTR_INSECURE_SKIP_TLS_VERIFY", f"global: {st!r}"
        assert st.context is not None and st.context.verify_mode == __import__("ssl").CERT_NONE
        # A per-system flag takes precedence in the reported name.
        os.environ["CONFLUENCE_INSECURE_SKIP_TLS_VERIFY"] = "1"
        st = d.doctor_tls_state("confluence")
        assert st.verified is False and st.flag == "CONFLUENCE_INSECURE_SKIP_TLS_VERIFY", f"per-system: {st!r}"
    finally:
        for n, v in saved.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v


register("[doctor-unit] doctor_tls_state per-system + global fallback", _doctor_tls_state)


def _doctor_probe_path_for():
    d = importlib.import_module("guru_doctor")
    probe_env = ("SWTR_PROBE_UNIT", "SOURCE_CONTROL_PROBE_TENANT", "SOURCE_CONTROL_PROBE_PROJECT")
    saved = {n: os.environ.pop(n, None) for n in probe_env}
    try:
        assert d.probe_path_for("task_tracker", None) == "rest/api/unit/v2/CRPV-35498"
        assert d.probe_path_for("source_control", None) == \
            "repos/22f434fb-0355-42a1-ac25-011074a20a2d/pvm?limit=1"
        assert d.probe_path_for("confluence", None) == "rest/api/user/current"
        assert d.probe_path_for("jira", None) == "rest/api/2/myself"
        # An explicit --probe-path wins for any system.
        assert d.probe_path_for("source_control", "repos/t/p") == "repos/t/p"
        # Env overrides the defaults.
        os.environ["SWTR_PROBE_UNIT"] = "PVM-42"
        os.environ["SOURCE_CONTROL_PROBE_TENANT"] = "ten"
        os.environ["SOURCE_CONTROL_PROBE_PROJECT"] = "proj"
        assert d.probe_path_for("task_tracker", None) == "rest/api/unit/v2/PVM-42"
        assert d.probe_path_for("source_control", None) == "repos/ten/proj?limit=1"
    finally:
        for n, v in saved.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v


register("[doctor-unit] probe_path_for defaults + env override", _doctor_probe_path_for)


def _doctor_all_systems_happy():
    # All four systems probed in one run: each fixture returns 200 with an
    # authenticated identity, so every block is [OK] and the run exits 0.
    transport = Transport("ok")
    rc, out, err = run("guru_doctor", [], transport)
    assert rc == 0, f"all-systems happy expected rc=0, got {rc}; stderr={err[:400]!r}"
    for system in ("task_tracker", "source_control", "confluence", "jira"):
        assert f"{system} [OK]" in out, f"missing [OK] for {system}; out={out[:600]!r}"
    assert "SUMMARY" in out, f"missing SUMMARY line; out={out[:600]!r}"
    reads = [c for c in transport.calls if c["method"] == "GET"]
    assert len(reads) == 4, f"expected 4 read probes (one per system), got {len(reads)}: {transport.calls}"
    # task_tracker also fires the write probe, and it must stay the only write.
    writes = [c for c in transport.calls if c["method"] != "GET"]
    assert len(writes) == 1 and writes[0]["url"].endswith("/create"), \
        f"expected exactly one write probe, got {writes}"
    # No secret/identity VALUE may leak (tokens seeded by the harness, fixture PII:
    # confluence username/userKey/displayName, jira name/key/displayName/email).
    for secret in ("SMOKECONFTOKEN", "SMOKEJIRATOKEN", "SMOKESCTOKEN", "SMOKESWTRTOKEN",
                   "smoke.user@smoke.test", "smoke.user", "ff8080816smoke", "JIRAUSER10000",
                   "Smoke User"):
        assert secret not in out, f"leaked {secret!r} into doctor output"


register("[doctor] all systems -> four ✅ blocks + summary, exit 0", _doctor_all_systems_happy)


def _doctor_confluence_anonymous_is_not_authenticated():
    # A 200 with an anonymous body must NOT be reported as authenticated. Serve an
    # anonymous user/current body by swapping the fixture for this one case.
    saved = FIX["confluence_user_current"]
    FIX["confluence_user_current"] = {"type": "anonymous", "displayName": "Anonymous"}
    try:
        transport = Transport("ok")
        rc, out, err = run("guru_doctor", ["--system", "confluence"], transport)
    finally:
        FIX["confluence_user_current"] = saved
    assert rc == 1, f"anonymous confluence must be non-zero (rc=1), got {rc}; out={out[:400]!r}"
    assert "NOT authenticated" in out, f"expected anonymous verdict, got {out[:400]!r}"
    assert "[OK]" not in out, f"anonymous must not show [OK]: {out[:400]!r}"


register("[doctor] confluence 200 anonymous -> not authenticated (no [OK])",
         _doctor_confluence_anonymous_is_not_authenticated)


def _doctor_tls_global_fallback_hint():
    # I1: when confluence/jira skip TLS via the GLOBAL SWTR flag (not their own),
    # the block must warn that the runtime tool honors only its own flag.
    os.environ["SWTR_INSECURE_SKIP_TLS_VERIFY"] = "1"
    try:
        rc, out, err = run("guru_doctor", ["--system", "confluence", "--no-network"], Transport("ok"))
    finally:
        os.environ.pop("SWTR_INSECURE_SKIP_TLS_VERIFY", None)
    assert "CONFLUENCE_INSECURE_SKIP_TLS_VERIFY" in out, f"missing cross-tool TLS hint: {out[:500]!r}"
    assert "verification OFF (SWTR_INSECURE_SKIP_TLS_VERIFY=1)" in out, f"missing TLS-off line: {out[:500]!r}"


register("[doctor] global-SWTR TLS skip warns confluence/jira runtime flag", _doctor_tls_global_fallback_hint)


def _doctor_all_systems_one_missing_credential():
    # M4: with jira's credential removed, the other three are still probed and the
    # run completes (isolation). jira shows a no-credential block; exit is 1.
    jira_env = ("JIRA_AUTHORIZATION", "JIRA_BEARER_TOKEN", "JIRA_TOKEN", "JIRA_BASIC_AUTH")
    saved = {n: os.environ.pop(n, None) for n in jira_env}
    try:
        transport = Transport("ok")
        rc, out, err = run("guru_doctor", [], transport)
    finally:
        for n, v in saved.items():
            if v is not None:
                os.environ[n] = v
    assert rc == 1, f"expected exit 1 (jira not ok), got {rc}; out={out[:400]!r}"
    # jira has no cred -> not probed; the other three ARE probed. Read probes are
    # counted on their own: task_tracker also fires a write probe, and folding the
    # two into one number would hide which of them went missing.
    reads = [c for c in transport.calls if c["method"] == "GET"]
    assert len(reads) == 3, f"expected 3 read probes (jira skipped), got {len(reads)}: {transport.calls}"
    assert not any("jira.smoke.test" in c["url"] for c in transport.calls), \
        f"jira must not be probed without a credential: {transport.calls}"
    for system in ("task_tracker", "source_control", "confluence", "jira"):
        assert f"=== {system} ===" in out, f"missing block for {system}; out={out[:600]!r}"
    assert "NONE -- no credential found" in out, f"jira should show no-credential block: {out[:600]!r}"
    assert "SUMMARY" in out, f"missing SUMMARY: {out[:600]!r}"


register("[doctor] all systems, one missing credential -> others still probed, exit 1",
         _doctor_all_systems_one_missing_credential)


# --------------------------------------------------------------------------
# guru_env_setup: env-file writer (Task 1)
# --------------------------------------------------------------------------
def _setup_module():
    return importlib.import_module("guru_env_setup")


def _tmp_env(text=None, encoding="utf-8"):
    """Fresh temp env-file path; write `text` first when given."""
    path = Path(tempfile.mkdtemp(prefix="guru-env-")) / ".swtr-review.env"
    if text is not None:
        path.write_bytes(text.encode(encoding))
    return path


def _writer_creates_missing_file():
    mod = _setup_module()
    path = _tmp_env()
    lines, changed = mod.merge_env_lines([], {"SWTR_BASIC_AUTH": "u:p"})
    existed = mod.write_env_file(path, lines)
    assert existed is False, "file did not exist, write_env_file must report False"
    assert changed == ["SWTR_BASIC_AUTH"], f"changed={changed!r}"
    assert path.read_text(encoding="utf-8") == 'SWTR_BASIC_AUTH="u:p"\n'
    assert oct(path.stat().st_mode & 0o777) == "0o600", f"mode={oct(path.stat().st_mode & 0o777)}"


register("[env-setup] writer creates missing file with mode 600", _writer_creates_missing_file)


def _writer_preserves_foreign_lines():
    mod = _setup_module()
    original = (
        "# my keys\n"
        'JIRA_BEARER_TOKEN="keepme"\n'
        "\n"
        '#SWTR_TOKEN=""\n'
        'SWTR_BASIC_AUTH="old:creds"\n'
    )
    path = _tmp_env(original)
    lines, changed = mod.merge_env_lines(mod.read_env_lines(path), {"SWTR_BASIC_AUTH": "new:pw"})
    mod.write_env_file(path, lines)
    out = path.read_text(encoding="utf-8")
    assert "# my keys" in out, out
    assert 'JIRA_BEARER_TOKEN="keepme"' in out, out
    assert '#SWTR_TOKEN=""' in out, "commented-out line must survive untouched: " + out
    assert 'SWTR_BASIC_AUTH="new:pw"' in out, out
    assert "old:creds" not in out, "stale value must be replaced in place: " + out
    assert out.count("SWTR_BASIC_AUTH=") == 1, "key must not be duplicated: " + out
    assert changed == ["SWTR_BASIC_AUTH"], f"changed={changed!r}"


register("[env-setup] writer replaces in place, keeps comments and foreign keys",
         _writer_preserves_foreign_lines)


def _writer_appends_live_line_beside_commented_key():
    mod = _setup_module()
    path = _tmp_env('#SWTR_BASIC_AUTH="commented:out"\n')
    lines, changed = mod.merge_env_lines(mod.read_env_lines(path), {"SWTR_BASIC_AUTH": "new:pw"})
    mod.write_env_file(path, lines)
    out = path.read_text(encoding="utf-8")
    assert '#SWTR_BASIC_AUTH="commented:out"' in out, "the comment must survive untouched: " + out
    assert 'SWTR_BASIC_AUTH="new:pw"' in out, "a live line must be appended: " + out
    assert changed == ["SWTR_BASIC_AUTH"], f"changed={changed!r}"
    assert mod.parse_env_pairs(path)["SWTR_BASIC_AUTH"] == "new:pw"


register("[env-setup] a commented-out key stays a comment; a live line is appended",
         _writer_appends_live_line_beside_commented_key)


def _writer_backs_up_before_write():
    mod = _setup_module()
    original = 'SWTR_BASIC_AUTH="old:creds"\n'
    path = _tmp_env(original)
    lines, _ = mod.merge_env_lines(mod.read_env_lines(path), {"SWTR_BASIC_AUTH": "new:pw"})
    existed = mod.write_env_file(path, lines)
    backup = path.parent / (path.name + ".bak")
    assert existed is True
    assert backup.exists(), "backup must be created before overwriting"
    assert backup.read_text(encoding="utf-8") == original, backup.read_text(encoding="utf-8")
    assert oct(backup.stat().st_mode & 0o777) == "0o600"


register("[env-setup] writer backs up the previous file", _writer_backs_up_before_write)


def _writer_cleans_up_temp_file_on_failure():
    mod = _setup_module()
    path = _tmp_env('SWTR_BASIC_AUTH="old:creds"\n')
    lines, _ = mod.merge_env_lines(mod.read_env_lines(path), {"SWTR_BASIC_AUTH": "new:pw"})
    with mock.patch("os.replace", side_effect=OSError("disk full")):
        try:
            mod.write_env_file(path, lines)
        except OSError:
            pass
        else:
            raise AssertionError("write_env_file must not swallow the failure")
    assert path.read_text(encoding="utf-8") == 'SWTR_BASIC_AUTH="old:creds"\n', \
        "a failed write must leave the original credential file intact"
    strays = [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")]
    assert strays == [], f"temp files left behind after a failed write: {strays}"


register("[env-setup] a failed write leaves the original intact and no temp files",
         _writer_cleans_up_temp_file_on_failure)


def _writer_only_if_missing_keeps_user_value():
    mod = _setup_module()
    path = _tmp_env('SWTR_BASE_URL="https://custom.example/swtr"\n')
    lines, changed = mod.merge_env_lines(
        mod.read_env_lines(path),
        {"SWTR_BASE_URL": "https://portal.works.prod.sbt/swtr", "SWTR_INSECURE_SKIP_TLS_VERIFY": "1"},
        only_if_missing=("SWTR_BASE_URL", "SWTR_INSECURE_SKIP_TLS_VERIFY"),
    )
    mod.write_env_file(path, lines)
    out = path.read_text(encoding="utf-8")
    assert "https://custom.example/swtr" in out, "existing value must win: " + out
    assert "portal.works.prod.sbt" not in out, out
    assert 'SWTR_INSECURE_SKIP_TLS_VERIFY="1"' in out, "missing key must be appended: " + out
    assert changed == ["SWTR_INSECURE_SKIP_TLS_VERIFY"], f"changed={changed!r}"


register("[env-setup] only_if_missing keeps an existing value, appends a missing one",
         _writer_only_if_missing_keeps_user_value)


def _writer_rewrites_utf16_as_utf8():
    mod = _setup_module()
    path = _tmp_env('SWTR_BASIC_AUTH="old:creds"\n', encoding="utf-16")
    lines, _ = mod.merge_env_lines(mod.read_env_lines(path), {"SWTR_BASIC_AUTH": "new:pw"})
    mod.write_env_file(path, lines)
    raw = path.read_bytes()
    assert raw[:2] not in (b"\xff\xfe", b"\xfe\xff"), "file must be rewritten without a UTF-16 BOM"
    assert raw.decode("utf-8") == 'SWTR_BASIC_AUTH="new:pw"\n', raw


register("[env-setup] writer rewrites a UTF-16 file as UTF-8", _writer_rewrites_utf16_as_utf8)


def _writer_roundtrips_special_characters():
    mod = _setup_module()
    password = 'Maxi7089261!}%#*&$ "quoted" \\'
    path = _tmp_env()
    lines, _ = mod.merge_env_lines([], {"SWTR_BASIC_AUTH": "login.i.o:" + password})
    mod.write_env_file(path, lines)
    pairs = mod.parse_env_pairs(path)
    assert pairs["SWTR_BASIC_AUTH"] == "login.i.o:" + password, repr(pairs.get("SWTR_BASIC_AUTH"))


register("[env-setup] special characters in a password round-trip byte-for-byte",
         _writer_roundtrips_special_characters)


def _writer_keeps_export_prefix_and_replaces_duplicates():
    mod = _setup_module()
    path = _tmp_env('export SWTR_BASIC_AUTH="a:b"\nSWTR_BASIC_AUTH="c:d"\n')
    lines, _ = mod.merge_env_lines(mod.read_env_lines(path), {"SWTR_BASIC_AUTH": "new:pw"})
    mod.write_env_file(path, lines)
    out = path.read_text(encoding="utf-8")
    assert out == 'export SWTR_BASIC_AUTH="new:pw"\nSWTR_BASIC_AUTH="new:pw"\n', out


register("[env-setup] export prefix kept; every duplicate of the key is refreshed",
         _writer_keeps_export_prefix_and_replaces_duplicates)


def _status_reports_key_sources():
    mod = _setup_module()
    path = _tmp_env('SWTR_BASIC_AUTH="u:p"\nSWTR_BASE_URL="https://x.test/swtr"\n')
    # The harness exports SWTR_BASE_URL for the other suites; drop every managed
    # key first so this test sees only what it sets up itself.
    saved = {n: os.environ.pop(n, None) for n in mod.MANAGED_KEYS}
    os.environ["SOURCE_CONTROL_BASIC_AUTH"] = "shadow:value"
    try:
        state = mod.env_key_state(path, mod.MANAGED_KEYS)
    finally:
        os.environ.pop("SOURCE_CONTROL_BASIC_AUTH", None)
        for _n, _v in saved.items():
            if _v is not None:
                os.environ[_n] = _v
    assert state["SWTR_BASIC_AUTH"] == "file", state
    assert state["SWTR_BASE_URL"] == "file", state
    assert state["SWTR_INSECURE_SKIP_TLS_VERIFY"] == "missing", state
    assert state["SOURCE_CONTROL_BASIC_AUTH"] == "shell", \
        "an exported variable shadows the file and must be reported as such: %r" % (state,)


register("[env-setup] status distinguishes file / shell / missing", _status_reports_key_sources)


def _status_prints_no_secret_values():
    mod = _setup_module()
    path = _tmp_env('SWTR_BASIC_AUTH="login.i.o:SUPERSECRETPW"\n')
    rc, out, err = _run_main(mod, ["status", "--env-file", str(path)], True)
    assert rc == 0, f"rc={rc}; err={err[:300]!r}"
    assert "SUPERSECRETPW" not in out + err, "status must never print a secret value"
    assert "SWTR_BASIC_AUTH" in out, out
    assert "set(file)" in out, out


register("[env-setup] status prints key state, never values", _status_prints_no_secret_values)


def _verify_swtr_accepts_200_and_404():
    mod = _setup_module()
    for status in (200, 404):
        transport = Transport("ok", status=status) if status == 200 else Transport("error", status=404)
        with mock.patch("urllib.request.urlopen", side_effect=transport):
            res = mod.verify_swtr("smoke.user", "pw", "https://swtr.smoke.test/swtr", 5.0, None)
        assert res.ok is True, f"HTTP {status} means the auth header was accepted: {res!r}"
        assert res.status == status, res
        assert len(transport.calls) == 1, f"exactly one attempt allowed, got {transport.calls}"
        assert transport.calls[0]["method"] == "GET", transport.calls


register("[env-setup] verify_swtr treats 200 and 404 as credential accepted",
         _verify_swtr_accepts_200_and_404)


def _verify_swtr_rejects_401_403_once():
    mod = _setup_module()
    for status in (401, 403):
        transport = Transport("error", status=status)
        with mock.patch("urllib.request.urlopen", side_effect=transport):
            res = mod.verify_swtr("smoke.user", "pw", "https://swtr.smoke.test/swtr", 5.0, None)
        assert res.ok is False, res
        assert res.status == status, res
        assert len(transport.calls) == 1, \
            f"a rejected password must NOT be retried (account lockout), got {transport.calls}"


register("[env-setup] verify_swtr rejects 401/403 with exactly one attempt",
         _verify_swtr_rejects_401_403_once)


def _verify_swtr_reads_the_auth_page_as_a_bad_password():
    mod = _setup_module()
    for status in (401, 403):
        transport = Transport("error", status=status, error_body=GATEWAY_AUTH_PAGE_BYTES,
                              error_content_type=GATEWAY_HTML_CONTENT_TYPE)
        with mock.patch("urllib.request.urlopen", side_effect=transport):
            res = mod.verify_swtr("smoke.user", "pw", "https://swtr.smoke.test/swtr", 5.0, None)
        assert res.ok is False, res
        assert res.status == status, res
        assert mod.is_auth_failure_page(res.detail), \
            "the gateway page must be distinguishable from an application 403: %r" % (res.detail,)
        assert len(transport.calls) == 1, \
            "a rejected password must NOT be retried (account lockout): %r" % (transport.calls,)
    # An application-level 403 without the page is NOT a password verdict.
    plain = Transport("error", status=403)
    with mock.patch("urllib.request.urlopen", side_effect=plain):
        res = mod.verify_swtr("smoke.user", "pw", "https://swtr.smoke.test/swtr", 5.0, None)
    assert res.ok is False and not mod.is_auth_failure_page(res.detail), res


register("[env-setup] verify_swtr reads the portal auth page as a rejected password",
         _verify_swtr_reads_the_auth_page_as_a_bad_password)


def _verify_swtr_network_failure_is_not_ok():
    mod = _setup_module()
    transport = Transport("timeout")
    with mock.patch("urllib.request.urlopen", side_effect=transport):
        res = mod.verify_swtr("smoke.user", "pw", "https://swtr.smoke.test/swtr", 5.0, None)
    assert res.ok is False, "an unreachable server must never be read as a valid credential"
    assert res.status is None, res
    assert len(transport.calls) == 1, \
        f"a network failure must not be retried either: {transport.calls}"


register("[env-setup] verify_swtr: unreachable server is not a pass", _verify_swtr_network_failure_is_not_ok)


def _http_call_marks_tls_errors():
    mod = _setup_module()

    def raise_tls(request, timeout=None, context=None):
        raise urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed"))

    with mock.patch("urllib.request.urlopen", side_effect=raise_tls):
        status, body, note = mod.http_call(
            "GET", "https://swtr.smoke.test/swtr/x", "u", "p", None, 5.0, None)
    assert status is None, status
    assert mod.is_tls_error(note), f"a certificate failure must be distinguishable: {note!r}"


register("[env-setup] http_call flags TLS failures separately from network ones",
         _http_call_marks_tls_errors)


def _verify_swtr_malformed_base_url_is_a_verdict_not_a_traceback():
    mod = _setup_module()
    transport = Transport("ok")
    with mock.patch("urllib.request.urlopen", side_effect=transport):
        res = mod.verify_swtr("smoke.user", "pw", "swtr.smoke.test/swtr", 5.0, None)
    assert res.ok is False, res
    assert res.status is None, res
    assert not transport.calls, \
        "a malformed URL must fail before any network call: %r" % (transport.calls,)
    assert not mod.is_tls_error(res.detail), \
        "a malformed URL is not a certificate problem; retrying with TLS off would be wrong"


register("[env-setup] a malformed base URL yields a verdict, not a traceback",
         _verify_swtr_malformed_base_url_is_a_verdict_not_a_traceback)


def _http_call_marks_a_direct_ssl_error():
    mod = _setup_module()

    def raise_direct(request, timeout=None, context=None):
        raise ssl.SSLError("record layer failure")

    with mock.patch("urllib.request.urlopen", side_effect=raise_direct):
        status, body, note = mod.http_call(
            "GET", "https://swtr.smoke.test/swtr/x", "u", "p", None, 5.0, None)
    assert status is None, status
    assert mod.is_tls_error(note), f"a bare ssl.SSLError must be flagged as TLS: {note!r}"


register("[env-setup] a bare ssl.SSLError is flagged as a TLS failure",
         _http_call_marks_a_direct_ssl_error)


def _sc_token_issued_on_201():
    mod = _setup_module()
    transport = Transport("ok", status=201)
    with mock.patch("urllib.request.urlopen", side_effect=transport):
        res = mod.issue_sc_token("smoke.user", "pw", "https://sc.smoke.test/api/v1", 5.0, None,
                                 name="smoke.user_2026-01-01_00-00")
    assert res.ok is True, res
    assert res.token == "SMOKESCISSUEDTOKEN0000000000000000000000", res
    assert len(transport.calls) == 1, transport.calls
    call = transport.calls[0]
    assert call["method"] == "POST", call
    assert call["url"] == "https://sc.smoke.test/api/v1/users/smoke.user/tokens", call
    assert call["body"] == {"name": "smoke.user_2026-01-01_00-00", "scopes": ["all"]}, call


register("[env-setup] issue_sc_token posts to the token API and returns sha1", _sc_token_issued_on_201)


def _sc_token_401_asks_for_ui_token():
    mod = _setup_module()
    transport = Transport("error", status=401)
    with mock.patch("urllib.request.urlopen", side_effect=transport):
        res = mod.issue_sc_token("smoke.user", "pw", "https://sc.smoke.test/api/v1", 5.0, None)
    assert res.ok is False and res.status == 401, res
    assert res.token is None, res
    assert "temporary" in res.detail.lower(), res.detail
    assert len(transport.calls) == 1, f"no retry with the same secret: {transport.calls}"


register("[env-setup] issue_sc_token 401 -> temporary UI token required", _sc_token_401_asks_for_ui_token)


def _sc_token_403_is_a_permission_error():
    mod = _setup_module()
    transport = Transport("error", status=403)
    with mock.patch("urllib.request.urlopen", side_effect=transport):
        res = mod.issue_sc_token("smoke.user", "pw", "https://sc.smoke.test/api/v1", 5.0, None)
    assert res.ok is False and res.status == 403, res
    assert "allow" in res.detail.lower() or "permission" in res.detail.lower(), res.detail


register("[env-setup] issue_sc_token 403 -> permission error", _sc_token_403_is_a_permission_error)


def _sc_token_201_without_sha1_is_a_failure():
    mod = _setup_module()

    class _NoSha1(Transport):
        def __call__(self, request, timeout=None, context=None):
            self.calls.append({"method": request.get_method(), "url": request.full_url,
                               "body": None, "timeout": timeout})
            return _FakeResponse(201, {"id": 7, "name": "x", "scopes": ["all"]})

    transport = _NoSha1("ok", status=201)
    with mock.patch("urllib.request.urlopen", side_effect=transport):
        res = mod.issue_sc_token("smoke.user", "pw", "https://sc.smoke.test/api/v1", 5.0, None)
    assert res.ok is False, "a 201 without a token value must not be reported as success"
    assert res.token is None, res


register("[env-setup] issue_sc_token 201 without sha1 -> failure", _sc_token_201_without_sha1_is_a_failure)


def _sc_token_never_printed():
    mod = _setup_module()
    transport = Transport("ok", status=201)
    out = io.StringIO()
    with mock.patch("urllib.request.urlopen", side_effect=transport), contextlib.redirect_stdout(out):
        res = mod.issue_sc_token("smoke.user", "pw", "https://sc.smoke.test/api/v1", 5.0, None)
    assert res.token, res
    assert res.token not in out.getvalue(), "the issued token must never be printed"
    assert res.token not in res.detail, "the issued token must not leak through the detail line"


register("[secrets] issued SourceControl token is never printed", _sc_token_never_printed)


def _me_falls_back_to_the_login_half_of_the_basic_credential():
    saved = {name: os.environ.get(name) for name in ("SWTR_LOGIN", "USER", "SWTR_BASIC_AUTH")}
    os.environ.pop("SWTR_LOGIN", None)
    os.environ.pop("USER", None)
    os.environ["SWTR_BASIC_AUTH"] = "ivanov.i.i:sekret-pw"
    try:
        rc, out, err = run("swtr_task_tool", ["assigned", "--me"], Transport("ok"))
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    assert rc == 0, f"--me must resolve from SWTR_BASIC_AUTH; rc={rc}, stderr={err[:400]!r}"
    data = json.loads(out)
    assert data["users"] == ["ivanov.i.i"], \
        "the login half must be used verbatim: %r" % (data["users"],)
    assert "sekret-pw" not in out and "sekret-pw" not in err, \
        "the password half must never reach the output"


register("[assigned] --me falls back to the login half of SWTR_BASIC_AUTH",
         _me_falls_back_to_the_login_half_of_the_basic_credential)


def _me_without_any_source_names_all_three_in_the_error():
    saved = {name: os.environ.get(name) for name in ("SWTR_LOGIN", "USER", "SWTR_BASIC_AUTH")}
    for name in saved:
        os.environ.pop(name, None)
    try:
        rc, out, err = run("swtr_task_tool", ["assigned", "--me"], Transport("ok"))
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value
    assert rc != 0, "with no login source at all --me must fail"
    message = out + err
    assert "SWTR_BASIC_AUTH" in message, \
        "the error must name the third source too: %r" % (message[:400],)


register("[assigned] --me with no login source names SWTR_BASIC_AUTH in the error",
         _me_without_any_source_names_all_three_in_the_error)


# --------------------------------------------------------------------------
# Резолв исполнителя. Фильтр `assigned_to` сравнивает `externalId` учётки с
# учётом регистра, а не логин: запрос по `savinov.m.y` возвращал ноль задач при
# десяти реальных. Плюс сам фильтр работает только на TQL-эндпоинте — на
# `/rest/api/unit/v3/find` атрибутное условие по пользователю не матчит ничего
# ни в одном формате значения. Оба факта проверяются здесь, потому что оба
# ломали сценарий «покажи мои задачи» молча, с пустым списком вместо ошибки.
# --------------------------------------------------------------------------


def _assigned_resolves_the_login_to_an_external_id():
    transport = Transport("ok")
    rc, out, err = run("swtr_task_tool", ["assigned", "--user", "user.one"], transport)
    assert rc == 0, f"rc={rc}, stderr={err[:300]!r}"
    data = json.loads(out)
    assert data["users"] == ["user.one"], data["users"]
    assert data["assigneeValues"] == ["User.O.N"], \
        "в фильтр должен уйти externalId из справочника: %r" % (data["assigneeValues"],)
    assert 'assigned_to in ("User.O.N")' in data["tql"], data["tql"]
    assert any("User.O.N" in note for note in data["notes"]), \
        "подстановку externalId нужно объяснить в notes: %r" % (data["notes"],)
    lookups = [c for c in transport.calls if "/rest/api/user/v1/find" in c["url"]]
    assert len(lookups) == 1, "резолв делается одним запросом: %r" % (lookups,)
    assert lookups[0]["body"] == ["user.one"], \
        "endpoint принимает голый массив логинов в нижнем регистре: %r" % (lookups[0]["body"],)


register("[assigned] the login is resolved to the externalId the filter matches",
         _assigned_resolves_the_login_to_an_external_id)


def _assigned_always_uses_the_tql_endpoint():
    transport = Transport("ok")
    rc, out, _ = run("swtr_task_tool", ["assigned", "--user", "user.one", "--space", "PVM"], transport)
    assert rc == 0
    data = json.loads(out)
    assert data["endpoint"] == "/rest/api/unit/v3/find/tql", data["endpoint"]
    attribute_filter_calls = [
        c for c in transport.calls
        if c["url"].rstrip("/").endswith("/rest/api/unit/v3/find")
    ]
    assert not attribute_filter_calls, (
        "атрибутный фильтр v3/find по assigned_to не матчит ничего — ходить туда нельзя: %r"
        % (attribute_filter_calls,))
    assert 'space in ("PVM")' in data["tql"], data["tql"]


register("[assigned] the assignee filter goes through TQL, never the attribute filter",
         _assigned_always_uses_the_tql_endpoint)


def _assigned_keeps_working_when_the_login_is_unknown():
    transport = Transport("ok")
    rc, out, _ = run("swtr_task_tool", ["assigned", "--user", "sa-robot"], transport)
    assert rc == 0
    data = json.loads(out)
    # Фикстура знает только user.one, поэтому логин остаётся как есть — плюс
    # title-case кандидат: у служебных учёток externalId равен логину, у людей — нет.
    assert data["assigneeValues"] == ["sa-robot", "Sa-robot"], data["assigneeValues"]
    assert any("не найден" in note for note in data["notes"]), data["notes"]


register("[assigned] an unknown login still queries, with both candidates and a note",
         _assigned_keeps_working_when_the_login_is_unknown)


def _assigned_survives_a_broken_user_lookup():
    class LookupFails(Transport):
        def __call__(self, request, timeout=None, context=None):
            if "/rest/api/user/v1/find" in request.full_url:
                self.calls.append({"method": request.get_method(), "url": request.full_url,
                                   "body": None, "timeout": timeout})
                raise _http_error(request.full_url, 500, {"error": "boom"})
            return Transport.__call__(self, request, timeout=timeout, context=context)

    rc, out, err = run("swtr_task_tool", ["assigned", "--user", "user.one"], LookupFails("ok"))
    assert rc == 0, f"необязательный резолв не должен ронять команду; rc={rc}, stderr={err[:300]!r}"
    data = json.loads(out)
    # Без справочника остаётся только форма логина и его title-case вариант
    # (`savinov.m.y` -> `Savinov.M.Y` — так выглядит externalId доменной учётки).
    assert data["assigneeValues"] == ["user.one", "User.One"], data["assigneeValues"]
    assert any("Не удалось сопоставить" in note for note in data["notes"]), data["notes"]


register("[assigned] a failed user lookup degrades to candidates instead of failing",
         _assigned_survives_a_broken_user_lookup)


def _done_weekly_sends_a_datetime_boundary():
    rc, out, _ = run("swtr_task_tool",
                     ["done-weekly", "--user", "user.one", "--since", "2026-07-01"], Transport("ok"))
    assert rc == 0
    data = json.loads(out)
    # Голую дату TQL отвергает: «Некорректное значение '2026-07-01' для типа
    # атрибута 'date_time'» — отчёт падал с HTTP 400 на любом --since.
    assert 'updatedAt >= "2026-07-01T00:00:00Z"' in data["tql"], data["tql"]
    assert data["assigneeValues"] == ["User.O.N"], data["assigneeValues"]


register("[done-weekly] the period boundary is sent as a date_time, not a bare date",
         _done_weekly_sends_a_datetime_boundary)


def _done_weekly_keeps_an_explicit_datetime_as_is():
    rc, out, _ = run("swtr_task_tool",
                     ["done-weekly", "--user", "user.one", "--since", "2026-07-01T09:30:00Z"],
                     Transport("ok"))
    assert rc == 0
    data = json.loads(out)
    assert 'updatedAt >= "2026-07-01T09:30:00Z"' in data["tql"], \
        "точную границу, заданную пользователем, менять нельзя: %s" % (data["tql"],)


register("[done-weekly] an explicit datetime boundary is passed through untouched",
         _done_weekly_keeps_an_explicit_datetime_as_is)


def _me_prefers_swtr_login_over_the_basic_credential():
    # The fallback order is SWTR_LOGIN > USER > SWTR_BASIC_AUTH: the basic
    # credential is a last resort, not an override. Pin it so a regression that
    # reversed the precedence -- resolving --me to the wrong login on any machine
    # where both are set -- fails here instead of silently querying someone else.
    saved = {name: os.environ.get(name) for name in ("SWTR_LOGIN", "USER", "SWTR_BASIC_AUTH")}
    os.environ.pop("USER", None)
    os.environ["SWTR_LOGIN"] = "primary.p.p"
    os.environ["SWTR_BASIC_AUTH"] = "fallback.f.f:pw"
    try:
        rc, out, err = run("swtr_task_tool", ["assigned", "--me"], Transport("ok"))
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    assert rc == 0, f"--me must resolve; rc={rc}, stderr={err[:400]!r}"
    data = json.loads(out)
    assert data["users"] == ["primary.p.p"], \
        "SWTR_LOGIN must win over SWTR_BASIC_AUTH: %r" % (data["users"],)


register("[assigned] --me prefers SWTR_LOGIN over the basic credential",
         _me_prefers_swtr_login_over_the_basic_credential)


# Профиль в цепочке логина --me: SWTR_LOGIN -> профиль -> USER -> Basic. Файл
# пишется в общий _TMP_HOME, поэтому убирается в finally: оставшийся профиль
# перекроил бы цепочку логина соседним smoke-кейсам.
_SMOKE_PROFILE = Path(_TMP_HOME) / ".config" / "pvm-guru" / "profile.json"


def _with_smoke_profile(profile, env, argv=("assigned", "--me")):
    """Run swtr_task_tool with a profile file in the smoke HOME and a login env overlay."""
    saved = {name: os.environ.get(name) for name in ("SWTR_LOGIN", "USER", "SWTR_BASIC_AUTH")}
    for name in saved:
        os.environ.pop(name, None)
    os.environ.update(env)
    _SMOKE_PROFILE.parent.mkdir(parents=True, exist_ok=True)
    _SMOKE_PROFILE.write_text(json.dumps(profile), encoding="utf-8")
    try:
        return run("swtr_task_tool", list(argv), Transport("ok"))
    finally:
        # Окружение восстанавливается ПЕРВЫМ и отдельно: сбой уборки файла не
        # имеет права оставить логин выставленным для соседних кейсов набора.
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        _SMOKE_PROFILE.unlink(missing_ok=True)


def _me_prefers_the_profile_over_the_os_account():
    # The OS account name is a guess, the profile login is the user's answer to
    # /guru-start: with both present --me must query the profile login, and the
    # result must say structurally where it came from.
    rc, out, err = _with_smoke_profile({"version": 1, "login": "profile.p.p"},
                                       {"USER": "os.account", "SWTR_BASIC_AUTH": "basic.b.b:pw"})
    assert rc == 0, f"--me must resolve from the profile; rc={rc}, stderr={err[:400]!r}"
    data = json.loads(out)
    assert data["users"] == ["profile.p.p"], \
        "the profile must win over USER and the basic credential: %r" % (data["users"],)
    assert {"key": "login", "value": "profile.p.p", "source": "profile"} in data.get("defaults_applied", []), \
        "the applied login must be reported with source=profile: %r" % (data.get("defaults_applied"),)


register("[assigned] --me prefers the profile login over the OS account name",
         _me_prefers_the_profile_over_the_os_account)


def _me_prefers_swtr_login_over_the_profile():
    # SWTR_LOGIN is the tracker's own explicit variable; the profile only fills
    # gaps and must never override it.
    rc, out, err = _with_smoke_profile({"version": 1, "login": "profile.p.p"},
                                       {"SWTR_LOGIN": "primary.p.p"})
    assert rc == 0, f"--me must resolve; rc={rc}, stderr={err[:400]!r}"
    data = json.loads(out)
    assert data["users"] == ["primary.p.p"], \
        "SWTR_LOGIN must win over the profile: %r" % (data["users"],)
    applied = data.get("defaults_applied", [])
    assert not any(entry.get("source") == "profile" for entry in applied), \
        "nothing may be attributed to the profile when SWTR_LOGIN wins: %r" % (applied,)


register("[assigned] --me prefers SWTR_LOGIN over the profile login",
         _me_prefers_swtr_login_over_the_profile)


# --------------------------------------------------------------------------
# guru_env_setup: apply subcommand (Task 5)
# --------------------------------------------------------------------------
class ApplyTransport(Transport):
    """200 for the SWTR probe (GET), 201 for the token issue (POST).

    A single fixed status cannot serve an apply run: the probe expects 200 and
    the token endpoint answers 201, and both go through the same urlopen patch.
    """

    def __call__(self, request, timeout=None, context=None):
        self.status = 201 if request.get_method() == "POST" else 200
        return Transport.__call__(self, request, timeout=timeout, context=context)


def _sc_post_fails_transport(status):
    """GET succeeds (credential fine), POST /tokens fails with `status`."""

    class _T(Transport):
        def __call__(self, request, timeout=None, context=None):
            if request.get_method() == "POST":
                # Record before raising, exactly as Transport.__call__ does — otherwise
                # a call-count assertion silently misses the call it exists to check.
                body = None
                if request.data:
                    try:
                        body = json.loads(request.data.decode("utf-8"))
                    except Exception:
                        body = "<non-json body>"
                self.calls.append({"method": "POST", "url": request.full_url,
                                   "body": body, "timeout": timeout})
                raise _http_error(request.full_url, status, FIX["error_401"])
            return Transport.__call__(self, request, timeout=timeout, context=context)

    return _T("ok", status=200)


def _apply_env(**extra):
    """Env overrides for an apply run: credentials plus test endpoints."""
    env = {
        "GURU_SETUP_LOGIN": "smoke.user",
        "GURU_SETUP_PASSWORD": "smokepw!",
        "SWTR_BASE_URL": "https://swtr.smoke.test/swtr",
    }
    env.update(extra)
    return env


def _run_apply(argv, transport, **env_extra):
    mod = _setup_module()
    env = _apply_env(**env_extra)
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        with mock.patch("urllib.request.urlopen", side_effect=transport):
            rc, out, err = _run_main(mod, argv, True)
        blob = out + err
        secrets = [env.get("GURU_SETUP_PASSWORD", ""), env.get("GURU_SETUP_SC_TEMP_TOKEN", ""),
                   "SMOKESCISSUEDTOKEN0000000000000000000000"]
        for secret in [s for s in secrets if s]:
            assert secret not in blob, "a secret value reached the output: %r" % (secret,)
            encoded = base64.b64encode(secret.encode("utf-8")).decode("ascii")
            assert encoded not in blob, "a base64 secret reached the output: %r" % (encoded,)
        return rc, out, err
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _apply_writes_all_keys_on_success():
    mod = _setup_module()
    path = _tmp_env()
    transport = ApplyTransport("ok")
    rc, out, err = _run_apply(
        ["apply", "--env-file", str(path), "--sc-token-api", "https://sc.smoke.test/api/v1"],
        transport)
    assert rc == 0, f"rc={rc}; out={out[:500]!r}; err={err[:500]!r}"
    pairs = mod.parse_env_pairs(path)
    assert pairs["SWTR_BASIC_AUTH"] == "smoke.user:smokepw!", pairs
    assert pairs["SWTR_BASE_URL"] == "https://portal.works.prod.sbt/swtr", pairs
    assert pairs["SWTR_INSECURE_SKIP_TLS_VERIFY"] == "1", pairs
    assert pairs["SOURCE_CONTROL_BASIC_AUTH"] == \
        "smoke.user:SMOKESCISSUEDTOKEN0000000000000000000000", pairs
    assert "smokepw!" not in out + err, "the password must never be printed"
    assert "SMOKESCISSUEDTOKEN0000000000000000000000" not in out + err, \
        "the issued token must never be printed"


register("[env-setup] apply writes all four keys and prints no secrets", _apply_writes_all_keys_on_success)


def _apply_writes_nothing_on_401():
    mod = _setup_module()
    path = _tmp_env('JIRA_BEARER_TOKEN="keepme"\n')
    before = path.read_bytes()
    transport = Transport("error", status=401)
    rc, out, err = _run_apply(["apply", "--env-file", str(path)], transport)
    assert rc == 2, f"expected EXIT_CRED_REJECTED=2, got {rc}; out={out[:400]!r}"
    assert path.read_bytes() == before, "a rejected credential must leave the file untouched"
    assert not (path.parent / (path.name + ".bak")).exists(), "nothing was written, so no backup"
    assert len(transport.calls) == 1, \
        f"a rejected credential must be attempted exactly once (account lockout): {transport.calls}"


register("[env-setup] apply on 401 writes nothing and exits 2", _apply_writes_nothing_on_401)


def _apply_on_the_auth_page_blames_the_password():
    """A 403 carrying the gateway auth page is a wrong password, so apply must exit
    EXIT_CRED_REJECTED (2) -- not EXIT_FORBIDDEN (6), whose text tells the user their
    password is fine and sends them after a Personal Access Token instead."""
    path = _tmp_env('JIRA_BEARER_TOKEN="keepme"\n')
    before = path.read_bytes()
    transport = Transport("error", status=403, error_body=GATEWAY_AUTH_PAGE_BYTES,
                          error_content_type=GATEWAY_HTML_CONTENT_TYPE)
    rc, out, err = _run_apply(["apply", "--env-file", str(path)], transport)
    assert rc == 2, f"expected EXIT_CRED_REJECTED=2 for a stale password, got {rc}; out={out[:500]!r}"
    low = (out + err).lower()
    assert "password" in low, "the verdict must name the password: " + out[-500:]
    assert "swtr_bearer_token" not in low and "personal access token" not in low, \
        "a stale password must not be answered with token advice: " + out[-500:]
    assert path.read_bytes() == before, "a rejected credential must leave the file untouched"
    assert len(transport.calls) == 1, \
        f"a rejected password must be attempted exactly once (account lockout): {transport.calls}"


register("[env-setup] apply on the portal auth page exits 2 and blames the password",
         _apply_on_the_auth_page_blames_the_password)


def _apply_on_a_real_application_403_keeps_the_token_advice():
    """An application-level 403 (JSON, no gateway page) still means the backend
    refuses the scheme -- that path must keep exit 6 and the PAT hint."""
    path = _tmp_env()
    transport = Transport("error", status=403)
    rc, out, err = _run_apply(["apply", "--env-file", str(path)], transport)
    assert rc == 6, f"expected EXIT_FORBIDDEN=6 for an application 403, got {rc}; out={out[:400]!r}"
    assert "SWTR_BEARER_TOKEN" in out, "the PAT hint belongs to this path: " + out[-400:]
    assert not path.exists(), "nothing may be written on a forbidden credential"


register("[env-setup] apply on an application-level 403 keeps exit 6 and the PAT hint",
         _apply_on_a_real_application_403_keeps_the_token_advice)


def _apply_unreachable_server_writes_nothing():
    path = _tmp_env()
    transport = Transport("timeout")
    rc, out, err = _run_apply(["apply", "--env-file", str(path)], transport)
    assert rc == 4, f"expected EXIT_UNVERIFIABLE=4, got {rc}; out={out[:400]!r}"
    assert not path.exists(), "an unverifiable credential must not be written"


register("[env-setup] apply on an unreachable server exits 4 and writes nothing",
         _apply_unreachable_server_writes_nothing)


def _apply_force_writes_without_verification():
    mod = _setup_module()
    path = _tmp_env()
    transport = Transport("timeout")
    rc, out, err = _run_apply(
        ["apply", "--env-file", str(path), "--force", "--systems", "task_tracker"], transport)
    assert rc == 0, f"--force must write anyway, got rc={rc}; out={out[:400]!r}"
    assert mod.parse_env_pairs(path)["SWTR_BASIC_AUTH"] == "smoke.user:smokepw!"
    assert "without verification" in out.lower(), "an unverified write must say so: " + out


register("[env-setup] apply --force writes unverified and warns", _apply_force_writes_without_verification)


def _apply_partial_when_sc_token_forbidden():
    mod = _setup_module()
    path = _tmp_env()
    transport = _sc_post_fails_transport(403)
    rc, out, err = _run_apply(
        ["apply", "--env-file", str(path), "--sc-token-api", "https://sc.smoke.test/api/v1"],
        transport)
    assert rc == 5, f"expected EXIT_PARTIAL=5, got {rc}; out={out[:400]!r}"
    pairs = mod.parse_env_pairs(path)
    assert pairs["SWTR_BASIC_AUTH"] == "smoke.user:smokepw!", \
        "a verified SWTR credential must still be written: %r" % (pairs,)
    assert "SOURCE_CONTROL_BASIC_AUTH" not in pairs, \
        "a failed token issue must leave SC unwritten: %r" % (pairs,)
    posts = [c for c in transport.calls if c["method"] == "POST"]
    assert len(posts) == 1, f"the token must be requested exactly once: {transport.calls}"


register("[env-setup] apply writes SWTR and exits 5 when the SC token fails",
         _apply_partial_when_sc_token_forbidden)


def _apply_asks_for_ui_token_on_sc_401():
    mod = _setup_module()
    path = _tmp_env()
    transport = _sc_post_fails_transport(401)
    rc, out, err = _run_apply(
        ["apply", "--env-file", str(path), "--sc-token-api", "https://sc.smoke.test/api/v1"],
        transport)
    assert rc == 3, f"expected EXIT_NEED_SC_TEMP_TOKEN=3, got {rc}; out={out[:400]!r}"
    assert "GURU_SETUP_SC_TEMP_TOKEN" in out, "the next step must name the variable to set: " + out
    assert "/user/settings/applications" in out, "the UI token page must be shown: " + out
    posts = [c for c in transport.calls if c["method"] == "POST"]
    assert len(posts) == 1, f"the token must be requested exactly once: {transport.calls}"
    assert mod.parse_env_pairs(path)["SWTR_BASIC_AUTH"] == "smoke.user:smokepw!", \
        "a verified SWTR credential must be written even when the token step fails"


register("[env-setup] apply exits 3 and points at the UI token page on SC 401",
         _apply_asks_for_ui_token_on_sc_401)


def _apply_systems_scopes_the_write():
    mod = _setup_module()
    path = _tmp_env('SOURCE_CONTROL_BASIC_AUTH="old:token"\n')
    transport = Transport("ok", status=200)
    rc, out, err = _run_apply(
        ["apply", "--env-file", str(path), "--systems", "task_tracker"], transport)
    assert rc == 0, f"rc={rc}; out={out[:400]!r}"
    pairs = mod.parse_env_pairs(path)
    assert pairs["SWTR_BASIC_AUTH"] == "smoke.user:smokepw!", pairs
    assert pairs["SOURCE_CONTROL_BASIC_AUTH"] == "old:token", \
        "a system outside --systems must not be touched: %r" % (pairs,)
    assert all("/tokens" not in c["url"] for c in transport.calls), \
        "no token must be issued when source_control is out of scope: %r" % (transport.calls,)


register("[env-setup] apply --systems scopes which keys are rewritten", _apply_systems_scopes_the_write)


def _apply_reuses_password_from_file_for_sc_only():
    mod = _setup_module()
    path = _tmp_env('SWTR_BASIC_AUTH="file.user:filepw"\n')
    transport = ApplyTransport("ok")
    saved = {k: os.environ.get(k) for k in ("GURU_SETUP_LOGIN", "GURU_SETUP_PASSWORD", "SWTR_BASE_URL")}
    os.environ.pop("GURU_SETUP_LOGIN", None)
    os.environ.pop("GURU_SETUP_PASSWORD", None)
    os.environ["SWTR_BASE_URL"] = "https://swtr.smoke.test/swtr"
    try:
        with mock.patch("urllib.request.urlopen", side_effect=transport):
            rc, out, err = _run_main(
                mod,
                ["apply", "--env-file", str(path), "--systems", "source_control",
                 "--sc-token-api", "https://sc.smoke.test/api/v1"],
                True)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert rc == 0, f"rc={rc}; out={out[:400]!r}; err={err[:400]!r}"
    pairs = mod.parse_env_pairs(path)
    assert pairs["SWTR_BASIC_AUTH"] == "file.user:filepw", "the SWTR line must stay as it was"
    assert pairs["SOURCE_CONTROL_BASIC_AUTH"] == \
        "file.user:SMOKESCISSUEDTOKEN0000000000000000000000", pairs


register("[env-setup] apply reuses the file's credential when only SC is broken",
         _apply_reuses_password_from_file_for_sc_only)


def _resolve_credential_is_all_or_nothing():
    mod = _setup_module()
    names = ("GURU_SETUP_LOGIN", "GURU_SETUP_PASSWORD")
    saved = {n: os.environ.get(n) for n in names}
    os.environ["GURU_SETUP_LOGIN"] = "env.user"
    os.environ.pop("GURU_SETUP_PASSWORD", None)
    try:
        login, password, source = mod._resolve_credential({"SWTR_BASIC_AUTH": "file.user:filepw"})
    finally:
        for _n, _v in saved.items():
            if _v is None:
                os.environ.pop(_n, None)
            else:
                os.environ[_n] = _v
    assert (login, password) == ("file.user", "filepw"), \
        "a half-set env pair must never be mixed with the file's: %r" % ((login, password),)
    assert source == "env file", source


register("[env-setup] a half-set env credential never mixes with the file's",
         _resolve_credential_is_all_or_nothing)


def _doctor_prints_fixable_for_missing_credential():
    import guru_doctor
    saved = {}
    for name in ("SOURCE_CONTROL_BEARER_TOKEN", "SOURCE_CONTROL_BASIC_AUTH", "SC_BEARER_TOKEN"):
        saved[name] = os.environ.pop(name, None)
    try:
        transport = Transport("ok")
        rc, out, err = run("guru_doctor", [], transport)
    finally:
        for n, v in saved.items():
            if v is not None:
                os.environ[n] = v
    line = [l for l in out.splitlines() if l.startswith("FIXABLE:")]
    assert line, "a repairable system must be advertised on a FIXABLE line: " + out[-600:]
    assert "source_control" in line[0], line
    assert "confluence" not in line[0] and "jira" not in line[0], \
        "only SWTR and SourceControl are repairable: %r" % line


register("[doctor] FIXABLE lists a system with no credential", _doctor_prints_fixable_for_missing_credential)


def _doctor_fixable_excludes_404_and_all_ok():
    import guru_doctor
    verdicts = {
        "task_tracker": guru_doctor.Verdict(False, "inconclusive", "404 probe"),
        "source_control": guru_doctor.Verdict(True, "ok", "auth works"),
        "confluence": guru_doctor.Verdict(False, "no_credential", "none"),
        "jira": guru_doctor.Verdict(False, "network", "unreachable"),
    }
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fixable = guru_doctor.print_fixable(verdicts)
    assert fixable == [], \
        "404 is a probe-target verdict, ok needs nothing, confluence/jira are out of scope: %r" % (fixable,)
    assert "FIXABLE" not in out.getvalue(), "nothing to fix -> no FIXABLE line: " + out.getvalue()


register("[doctor] FIXABLE excludes 404/network/ok and out-of-scope systems",
         _doctor_fixable_excludes_404_and_all_ok)


def _doctor_fixable_covers_every_repairable_category():
    import guru_doctor
    # Hardcode the EXPECTED categories, not the actual constant, so typos in the
    # constant are caught (if the constant is "reject" instead of "rejected",
    # the test will pass "rejected" and the code won't find it).
    expected_categories = ("no_credential", "rejected", "forbidden", "stale_password")
    for category in expected_categories:
        verdicts = {
            "task_tracker": guru_doctor.Verdict(False, category, "probe said " + category),
            "source_control": guru_doctor.Verdict(True, "ok", "auth works"),
            "confluence": guru_doctor.Verdict(False, category, "out of scope"),
            "jira": guru_doctor.Verdict(False, category, "out of scope"),
        }
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fixable = guru_doctor.print_fixable(verdicts)
        assert fixable == ["task_tracker"], \
            "category %r must make an in-scope system repairable: %r" % (category, fixable)
        assert "confluence" not in out.getvalue() and "jira" not in out.getvalue(), \
            "confluence/jira need a hand-issued PAT and must never be advertised: " + out.getvalue()


register("[doctor] every FIXABLE_CATEGORIES value makes an in-scope system repairable",
         _doctor_fixable_covers_every_repairable_category)


def _doctor_fixable_excludes_network_for_in_scope_systems():
    import guru_doctor
    verdicts = {
        "task_tracker": guru_doctor.Verdict(False, "network", "unreachable"),
        "source_control": guru_doctor.Verdict(False, "inconclusive", "404 probe"),
    }
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fixable = guru_doctor.print_fixable(verdicts)
    assert fixable == [], \
        "network and 404 are not credential problems; new credentials fix neither: %r" % (fixable,)
    assert "FIXABLE" not in out.getvalue(), out.getvalue()


register("[doctor] network and 404 are not repairable even for in-scope systems",
         _doctor_fixable_excludes_network_for_in_scope_systems)


def _doctor_single_system_mode_prints_fixable():
    names = ("SOURCE_CONTROL_BEARER_TOKEN", "SOURCE_CONTROL_BASIC_AUTH", "SC_BEARER_TOKEN")
    saved = {n: os.environ.pop(n, None) for n in names}
    try:
        transport = Transport("ok")
        rc, out, err = run("guru_doctor", ["--system", "source_control"], transport)
    finally:
        for _n, _v in saved.items():
            if _v is not None:
                os.environ[_n] = _v
    assert rc == 2, f"no credential in single-system mode still exits 2, got {rc}; out={out[-400:]!r}"
    assert "FIXABLE: source_control" in out, \
        "the single-system call site must print the line too: " + out[-400:]


register("[doctor] --system mode prints FIXABLE and keeps its exit code",
         _doctor_single_system_mode_prints_fixable)


def _apply_accepts_the_doctors_space_separated_systems():
    mod = _setup_module()
    path = _tmp_env()
    transport = ApplyTransport("ok")
    rc, out, err = _run_apply(
        ["apply", "--env-file", str(path), "--systems", "task_tracker source_control",
         "--sc-token-api", "https://sc.smoke.test/api/v1"], transport)
    assert rc == 0, \
        f"the doctor's own FIXABLE format must be accepted, got rc={rc}; out={out[:400]!r}"
    pairs = mod.parse_env_pairs(path)
    assert pairs["SWTR_BASIC_AUTH"] == "smoke.user:smokepw!", pairs
    assert pairs["SOURCE_CONTROL_BASIC_AUTH"].startswith("smoke.user:"), pairs


register("[env-setup] apply accepts the doctor's space-separated FIXABLE list",
         _apply_accepts_the_doctors_space_separated_systems)


def _apply_force_does_not_override_a_rejection():
    path = _tmp_env('JIRA_BEARER_TOKEN="keepme"\n')
    before = path.read_bytes()
    transport = Transport("error", status=401)
    rc, out, err = _run_apply(
        ["apply", "--env-file", str(path), "--force", "--systems", "task_tracker"], transport)
    assert rc == 2, \
        f"--force must not turn a rejected credential into a write, got rc={rc}; out={out[:400]!r}"
    assert path.read_bytes() == before, "a rejected credential must never be written, even with --force"


register("[env-setup] --force never writes a credential the server rejected",
         _apply_force_does_not_override_a_rejection)


class _TlsThenOk(Transport):
    """First request fails the certificate check; everything after it succeeds.

    Mirrors a first-time setup on a machine whose Python does not trust the
    corporate CA — the flag that fixes it is exactly what apply is about to write.
    """

    def __init__(self):
        Transport.__init__(self, "ok")
        self.contexts = []

    def __call__(self, request, timeout=None, context=None):
        self.contexts.append(context)
        if len(self.contexts) == 1:
            raise urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed"))
        self.status = 201 if request.get_method() == "POST" else 200
        return Transport.__call__(self, request, timeout=timeout, context=context)


def _apply_retries_once_with_tls_off_after_a_certificate_failure():
    mod = _setup_module()
    path = _tmp_env()
    transport = _TlsThenOk()
    rc, out, err = _run_apply(
        ["apply", "--env-file", str(path), "--sc-token-api", "https://sc.smoke.test/api/v1"],
        transport)
    assert rc == 0, f"the TLS retry must let setup finish, got rc={rc}; out={out[:500]!r}"
    assert len(transport.contexts) == 3, \
        "expected GET(fails) + GET(retry) + POST(token), got %d: %r" % (
            len(transport.contexts), transport.contexts)
    assert transport.contexts[0] is None, "the first attempt must verify the certificate"
    assert transport.contexts[1] is not None, "the retry must disable verification"
    assert transport.contexts[2] is not None, \
        "the unverified context must carry into the token POST, or it fails on the same CA"
    assert "certificate verification OFF" in out, \
        "an unverified connection must be announced: " + out[:500]
    assert mod.parse_env_pairs(path)["SWTR_BASIC_AUTH"] == "smoke.user:smokepw!"


register("[env-setup] a certificate failure retries exactly once with verification off",
         _apply_retries_once_with_tls_off_after_a_certificate_failure)


class _CaptureAuth(Transport):
    """Records the Authorization header of every request, per method."""

    def __init__(self):
        Transport.__init__(self, "ok")
        self.auth = []

    def __call__(self, request, timeout=None, context=None):
        self.auth.append((request.get_method(), request.get_header("Authorization")))
        self.status = 201 if request.get_method() == "POST" else 200
        return Transport.__call__(self, request, timeout=timeout, context=context)


def _apply_uses_the_temp_ui_token_for_the_token_request_only():
    path = _tmp_env()
    transport = _CaptureAuth()
    rc, out, err = _run_apply(
        ["apply", "--env-file", str(path), "--sc-token-api", "https://sc.smoke.test/api/v1"],
        transport, GURU_SETUP_SC_TEMP_TOKEN="TEMPUITOKEN123")
    assert rc == 0, f"rc={rc}; out={out[:400]!r}"
    sent = {}
    for method, header in transport.auth:
        raw = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        sent[method] = raw.partition(":")[2]
    assert sent["POST"] == "TEMPUITOKEN123", \
        "the token request must use the temporary UI token, not the password: %r" % (sent,)
    assert sent["GET"] == "smokepw!", \
        "the SWTR probe must still use the password: %r" % (sent,)


register("[env-setup] the temporary UI token is used for the token request only",
         _apply_uses_the_temp_ui_token_for_the_token_request_only)


def _writer_tightens_permissions_on_an_existing_file():
    mod = _setup_module()
    path = _tmp_env('SWTR_BASIC_AUTH="old:creds"\n')
    os.chmod(path, 0o644)
    lines, _ = mod.merge_env_lines(mod.read_env_lines(path), {"SWTR_BASIC_AUTH": "new:pw"})
    mod.write_env_file(path, lines)
    assert oct(path.stat().st_mode & 0o777) == "0o600", \
        "an existing world-readable credential file must be tightened: %s" % oct(path.stat().st_mode & 0o777)


register("[env-setup] an existing 644 credential file is tightened to 600",
         _writer_tightens_permissions_on_an_existing_file)


# --------------------------------------------------------------------------
# guru_catalog: offline inventory of the installed GURU family
# --------------------------------------------------------------------------
def _catalog_module():
    return importlib.import_module("guru_catalog")


def _frontmatter_reads_the_three_shapes_skill_files_actually_use():
    mod = _catalog_module()
    root = Path(tempfile.mkdtemp(prefix="guru-fm-"))

    plain = root / "plain.md"
    plain.write_text(
        "---\nname: plan\ndescription: Создаёт план реализации в docs/plans.\nlicense: MIT\n---\n\n# Plan\n",
        encoding="utf-8")
    assert mod.read_frontmatter(plain) == {
        "name": "plan",
        "description": "Создаёт план реализации в docs/plans.",
        "license": "MIT",
    }, mod.read_frontmatter(plain)

    quoted = root / "quoted.md"
    quoted.write_text(
        '---\nname: exec\ndescription: "Выполнить план: цикл задач."\n---\n\n# Exec\n',
        encoding="utf-8")
    assert mod.read_frontmatter(quoted)["description"] == "Выполнить план: цикл задач.", \
        "a quoted scalar must lose its quotes and keep the inner colon"

    folded = root / "folded.md"
    folded.write_text(
        "---\nname: pvm-guru\ndescription: >\n  Единый доступ к задачам\n  и вики.\nlicense: MIT\n---\n",
        encoding="utf-8")
    got = mod.read_frontmatter(folded)
    assert got["description"] == "Единый доступ к задачам и вики.", got
    assert got["name"] == "pvm-guru" and got["license"] == "MIT", \
        "a folded block must end at the next top-level key: %r" % (got,)

    assert mod.read_frontmatter(root / "nope.md") == {}, "a missing file yields no fields"
    naked = root / "naked.md"
    naked.write_text("# No frontmatter here\n", encoding="utf-8")
    assert mod.read_frontmatter(naked) == {}, "a file without frontmatter yields no fields"


register("[catalog] frontmatter reader handles plain, quoted and folded scalars",
         _frontmatter_reads_the_three_shapes_skill_files_actually_use)


def _family_registry_is_the_single_source_of_truth():
    mod = _catalog_module()
    assert len(mod.FAMILY) == 17, "the family registry must list 17 skills: %r" % (mod.FAMILY,)
    assert mod.FAMILY[0] == "pvm-guru", "pvm-guru leads the registry"
    assert len(set(mod.FAMILY)) == len(mod.FAMILY), "no duplicates in the registry"
    hint = mod.install_hint("exec")
    assert hint.startswith("npx skills add "), hint
    assert "--skill exec" in hint and mod.FAMILY_REPO in hint, hint
    # Хосты в подсказке перечислены явно и через пробел. Без --agent `npx skills`
    # при нулевом детекте хостов ставит «во все агенты» и печатает пачку
    # `Failed to install … does not support global skill installation` (Eve,
    # PromptScript), а список через запятую его парсер считает одним именем
    # хоста и обрывает установку с `Invalid agents`.
    assert "--agent opencode" in hint, hint
    assert "--agent opencode,gigacode" not in hint, hint


register("[catalog] the family registry holds 13 skills and builds install hints",
         _family_registry_is_the_single_source_of_truth)


_CATALOG_HOST_DIRS = {
    "shared": (".agents", "skills"),
    "opencode": (".config", "opencode", "skills"),
    "gigacode": (".gigacode", "skills"),
}


def _catalog_home(skills=None, manifest=None):
    """Synthetic HOME for the catalog.

    `skills` maps a host id (or "shared") to {skill_name: description}; a
    description of None creates the directory without a SKILL.md. `manifest`
    is written verbatim when a str, JSON-encoded otherwise.
    """
    home = Path(tempfile.mkdtemp(prefix="guru-catalog-"))
    for where, entries in (skills or {}).items():
        root = home.joinpath(*_CATALOG_HOST_DIRS[where])
        for name, description in entries.items():
            skill_dir = root / name
            skill_dir.mkdir(parents=True, exist_ok=True)
            if description is not None:
                skill_dir.joinpath("SKILL.md").write_text(
                    "---\nname: %s\ndescription: %s\n---\n\n# %s\n" % (name, description, name),
                    encoding="utf-8")
    if manifest is not None:
        path = home.joinpath(*(".config", "pvm-guru", "extras-manifest.json"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(manifest if isinstance(manifest, str) else json.dumps(manifest),
                        encoding="utf-8")
    return home


def _discovery_merges_hosts_and_marks_the_family():
    mod = _catalog_module()
    home = _catalog_home(skills={
        "shared": {"pvm-guru": "Единый доступ к задачам."},
        "gigacode": {"pvm-guru": "Единый доступ к задачам.", "atomic": "Компоненты atomic-ui."},
    })
    skills, notes = mod.discover_skills(home)
    by_name = {s["name"]: s for s in skills}
    assert [s["name"] for s in skills] == ["atomic", "pvm-guru"], \
        "skills must be sorted by name: %r" % ([s["name"] for s in skills],)
    assert by_name["pvm-guru"]["hosts"] == ["shared", "gigacode"], \
        "a skill visible in two places must list both: %r" % (by_name["pvm-guru"],)
    assert by_name["pvm-guru"]["family"] is True, "pvm-guru belongs to the family"
    assert by_name["atomic"]["family"] is False, "atomic is not a family skill"
    assert by_name["atomic"]["description"] == "Компоненты atomic-ui.", by_name["atomic"]
    assert notes == [], "a clean tree produces no notes: %r" % (notes,)


def _a_host_is_detected_by_its_config_dir_not_its_skills_dir():
    """Штатная установка (`npx skills add -g`) кладёт скиллы в общий ~/.agents/skills.

    Своего каталога skills у хоста при этом НЕТ. Детект по нему пропускал бы ровно
    ту установку, ради которой /guru-start и существует — свежую: install.mjs
    команды разложил, а каталог отрапортовал бы «не найден ни один известный хост».
    """
    mod = _catalog_module()
    home = _catalog_home(skills={"shared": {"pvm-guru": "Единый доступ к задачам."}})
    # Каталоги конфига есть, каталогов skills у хостов нет — как после npx skills add -g.
    for parts in ((".config", "opencode"), (".gigacode",)):
        home.joinpath(*parts).mkdir(parents=True, exist_ok=True)

    data = mod.build_catalog(home)
    found = sorted(host["id"] for host in data["hosts"])
    assert found == ["gigacode", "opencode"], (
        "хост определяется по каталогу конфига, а не по каталогу скиллов; найдено: %r" % (found,))
    assert not any("не найден ни один" in note for note in data["notes"]), data["notes"]


register("[catalog] a host is detected by its config dir, not its skills dir",
         _a_host_is_detected_by_its_config_dir_not_its_skills_dir)


register("[catalog] discovery merges hosts, sorts by name and marks the family",
         _discovery_merges_hosts_and_marks_the_family)


def _discovery_keeps_a_skill_without_readable_frontmatter_and_notes_it():
    mod = _catalog_module()
    home = _catalog_home(skills={"shared": {"plan": None}})
    skills, notes = mod.discover_skills(home)
    assert [s["name"] for s in skills] == ["plan"], \
        "a skill directory without SKILL.md must still be reported as installed"
    assert skills[0]["description"] == "", "no frontmatter means no description"
    assert any("plan" in note for note in notes), \
        "the unreadable SKILL.md must be explained in notes: %r" % (notes,)


register("[catalog] a skill without a readable SKILL.md is kept and noted",
         _discovery_keeps_a_skill_without_readable_frontmatter_and_notes_it)


def _discovery_honours_an_explicit_skills_root():
    mod = _catalog_module()
    home = _catalog_home(skills={"shared": {"exec": "Выполнить план."}})
    other = Path(tempfile.mkdtemp(prefix="guru-root-"))
    other.joinpath("plan").mkdir(parents=True)
    skills, _ = mod.discover_skills(home, skills_root=other)
    assert [s["name"] for s in skills] == ["plan"], \
        "--skills-root must replace the shared directory, not add to it: %r" % (skills,)


register("[catalog] an explicit skills root replaces the shared directory",
         _discovery_honours_an_explicit_skills_root)


def _discovery_survives_an_unlistable_root_and_notes_it():
    # Finding: is_dir() only stats the path and swallows OSError into False;
    # iterdir() is a separate syscall and does NOT swallow OSError. A root that
    # exists but can't be listed (foreign owner, stale mount, broken symlink)
    # must not raise out of discover_skills and abort every other root -- it
    # becomes a note, exactly like an unreadable SKILL.md already does.
    #
    # Discriminating test: to prove the scan CONTINUES PAST the failure, we seed
    # skills under roots processed both BEFORE (shared) and AFTER (gigacode) the
    # blocked root (opencode). If continue→break, the gigacode skill is lost and the
    # test fails. Thus the test proves roots after the blocked one are still scanned,
    # not just those before it.
    #
    # The blocked root must stay BETWEEN the two seeded ones in _CATALOG_HOST_DIRS
    # order -- block the last root and the property is silently gone.
    mod = _catalog_module()
    home = _catalog_home(skills={
        "shared": {"plan": "Создаёт план.", "exec": "Выполнить план."},
        "gigacode": {"atomic": "Компоненты atomic-ui."},
    })
    blocked = home.joinpath(*_CATALOG_HOST_DIRS["opencode"])
    blocked.mkdir(parents=True, exist_ok=True)
    os.chmod(blocked, 0o000)
    try:
        try:
            list(blocked.iterdir())
        except OSError:
            pass
        else:
            # Running as root (or on a filesystem) where chmod 0o000 does not
            # block listing -- this environment cannot express the condition
            # under test. Skip the assertions but say so loudly, rather than
            # reporting a pass that proved nothing.
            print(f"SKIP  [catalog] unlistable-root case: chmod 0o000 did not block "
                  f"iterdir() on {blocked} (likely running as root); condition not "
                  f"exercised in this environment")
            return
        skills, notes = mod.discover_skills(home)
        names = [s["name"] for s in skills]
        assert names == ["atomic", "exec", "plan"], \
            f"skills from roots before and after the blocked one must be returned: {names!r}"
        assert any(str(blocked) in note for note in notes), \
            f"expected a note naming the unlistable directory {blocked}: {notes!r}"
    finally:
        os.chmod(blocked, 0o755)


register("[catalog] an unlistable root does not abort the scan; other roots still return + note names it",
         _discovery_survives_an_unlistable_root_and_notes_it)


def _manifest_yields_commands_and_agents_per_host():
    mod = _catalog_module()
    home = _catalog_home(manifest={
        "version": 1,
        "files": {
            "/home/u/.gigacode/commands/guru-start.md": {"skill": "pvm-guru", "kind": "commands", "sha256": "a"},
            "/home/u/.config/opencode/commands/guru-start.md": {"skill": "pvm-guru", "kind": "commands", "sha256": "a"},
            "/home/u/.gigacode/commands/exec.md": {"skill": "exec", "kind": "commands", "sha256": "b"},
            "/home/u/.config/opencode/agents/executor.md": {"skill": "exec", "kind": "agents", "sha256": "c"},
        },
    })
    commands, agents, notes = mod.read_extras(home.joinpath(".config", "pvm-guru", "extras-manifest.json"))
    by_name = {c["name"]: c for c in commands}
    assert sorted(by_name) == ["exec", "guru-start"], sorted(by_name)
    # Порядок — по сортировке путей манифеста (`sorted(files.items())` в read_extras),
    # а `.config/opencode` сортируется раньше `.gigacode`.
    assert by_name["guru-start"]["hosts"] == ["opencode", "gigacode"], \
        "one command laid out twice must list both hosts: %r" % (by_name["guru-start"],)
    assert by_name["guru-start"]["skill"] == "pvm-guru", by_name["guru-start"]
    assert by_name["exec"]["hosts"] == ["gigacode"], by_name["exec"]
    assert [a["name"] for a in agents] == ["executor"], agents
    assert agents[0]["hosts"] == ["opencode"], \
        "an agent laid out under .config/opencode/agents must resolve to the opencode host: %r" % (agents[0],)
    assert notes == [], notes


register("[catalog] the manifest yields commands and agents grouped per host",
         _manifest_yields_commands_and_agents_per_host)


def _manifest_missing_or_broken_never_raises():
    mod = _catalog_module()
    absent = Path(tempfile.mkdtemp(prefix="guru-nomanifest-")) / "extras-manifest.json"
    commands, agents, notes = mod.read_extras(absent)
    assert (commands, agents) == ([], []), "a missing manifest yields empty lists"
    assert notes and "install.mjs" in notes[0], \
        "a missing manifest must point at the bootstrap step: %r" % (notes,)

    home = _catalog_home(manifest="{not json at all")
    broken = home.joinpath(".config", "pvm-guru", "extras-manifest.json")
    commands, agents, notes = mod.read_extras(broken)
    assert (commands, agents) == ([], []), "a broken manifest yields empty lists"
    assert notes, "a broken manifest must be explained"

    home2 = _catalog_home(manifest={"version": 1})
    no_files = home2.joinpath(".config", "pvm-guru", "extras-manifest.json")
    commands, agents, notes = mod.read_extras(no_files)
    assert (commands, agents) == ([], []), "a manifest without files yields empty lists"
    assert notes, "a manifest without a files section must be explained"


register("[catalog] a missing, broken or empty manifest never raises",
         _manifest_missing_or_broken_never_raises)


def _host_for_path_is_derived_from_hosts_not_hardcoded():
    """Finding: host_for_path used to be an independent hardcoded chain of
    path-segment checks, entirely decoupled from HOSTS. A host added to HOSTS
    without a matching branch there would have its laid-out commands resolve
    to "" and silently vanish from read_extras' `hosts` list -- no note, no
    error. This proves the two stay in step by building each probe path FROM
    the host's own `commands`/`agents` segments (never hand-writing ".claude"
    etc. again), so the assertion is driven by HOSTS itself and fails for any
    host -- present or future -- that host_for_path cannot resolve.

    Regression covered here: HOSTS carried no `agents` key at all, so every
    subagent manifest entry silently resolved to host "" (the read_extras-level
    symptom of the same bug is asserted separately in
    `_manifest_yields_commands_and_agents_per_host`).
    """
    mod = _catalog_module()
    for host in mod.HOSTS:
        for variant in host["commands"]:
            target = Path("/home/u", *variant, "probe.md")
            got = mod.host_for_path(target)
            assert got == host["id"], (
                "host %r declares commands variant %r in HOSTS but host_for_path "
                "resolved %r for %s" % (host["id"], variant, got, target))
        if host["agents"] is None:
            continue
        for variant in host["agents"]:
            target = Path("/home/u", *variant, "probe.md")
            got = mod.host_for_path(target)
            assert got == host["id"], (
                "host %r declares agents variant %r in HOSTS but host_for_path "
                "resolved %r for %s" % (host["id"], variant, got, target))


register("[catalog] host_for_path resolves every HOSTS commands and agents variant back to its own host id",
         _host_for_path_is_derived_from_hosts_not_hardcoded)


class _CountingTransport(Transport):
    """Counts HTTP calls so an offline script can be proven offline."""

    def __init__(self):
        Transport.__init__(self, "ok")
        self.calls = 0

    def __call__(self, request, timeout=None, context=None):
        self.calls += 1
        return Transport.__call__(self, request, timeout=timeout, context=context)


def _catalog_cli_reports_a_full_tree_and_makes_no_network_call():
    mod = _catalog_module()
    home = _catalog_home(
        skills={
            "shared": {"pvm-guru": "Единый доступ к задачам.", "plan": "Создаёт план."},
            "gigacode": {"atomic": "Компоненты atomic-ui."},
        },
        manifest={
            "version": 1,
            "files": {
                "/home/u/.gigacode/commands/guru-start.md": {
                    "skill": "pvm-guru", "kind": "commands", "sha256": "a"},
            },
        },
    )
    transport = _CountingTransport()
    rc, out, err = run("guru_catalog", ["--home", str(home)], transport)
    assert rc == 0, f"rc={rc}; stderr={err[:400]!r}"
    assert transport.calls == 0, "the catalog must never open a socket"
    data = json.loads(out)
    assert data["ok"] is True and data["command"] == "catalog", data
    assert [s["name"] for s in data["skills"]] == ["atomic", "plan", "pvm-guru"], data["skills"]
    assert [c["name"] for c in data["commands"]] == ["guru-start"], data["commands"]
    assert data["agents"] == [], data["agents"]
    missing = [m["name"] for m in data["missingCore"]]
    assert "pvm-guru" not in missing and "plan" not in missing, missing
    assert "exec" in missing, "an uninstalled family skill must be reported: %r" % (missing,)
    assert all("--skill" in m["install"] for m in data["missingCore"]), data["missingCore"]
    assert [h["id"] for h in data["hosts"]] == ["gigacode"], \
        "only hosts present on disk are listed: %r" % (data["hosts"],)
    assert data["hosts"][0]["commandsDir"].endswith("commands"), data["hosts"]


register("[catalog] the CLI reports a full tree and opens no socket",
         _catalog_cli_reports_a_full_tree_and_makes_no_network_call)


def _catalog_cli_on_an_empty_home_is_still_ok():
    home = Path(tempfile.mkdtemp(prefix="guru-empty-home-"))
    rc, out, err = run("guru_catalog", ["--home", str(home)], _CountingTransport())
    assert rc == 0, f"an empty home is not an error; rc={rc}, stderr={err[:400]!r}"
    data = json.loads(out)
    assert data["ok"] is True, data
    assert data["skills"] == [] and data["commands"] == [] and data["hosts"] == [], data
    assert len(data["missingCore"]) == len(_catalog_module().FAMILY), \
        "nothing installed means the whole family is missing: %r" % (data["missingCore"],)
    assert data["notes"], "an empty home must be explained in notes"


register("[catalog] an empty home yields ok:true with the whole family missing",
         _catalog_cli_on_an_empty_home_is_still_ok)


# Skills the role map may name that are not part of the GURU family: they ship
# from other repositories and are mentioned as "nice to have if installed".
_ROLES_NON_FAMILY_ALLOWED = {"python-docs"}

# CLI verbs/subcommands and JSON-field words the map legitimately backticks
# that are NOT skill names -- scanned from the map's actual backtick spans, not
# guessed. Every other CLI mention in the map embeds an underscore, a `.py`, or
# a space (e.g. `swtr_task_tool.py get`, `tms <kind>`, `assigned --me`), so the
# capture regex below never matches those as a bare token in the first place;
# only a token written ALONE in backticks needs to be listed here.
_ROLES_KNOWN_CLI_TOKENS = {
    "similar",  # swtr_task_tool.py subcommand
    "skills",   # JSON field name in guru_catalog.py's output
}


def _role_map_never_names_a_skill_the_registry_does_not_know():
    """The role map is prose; the registry is code. This is the seam that keeps
    them honest — a renamed or dropped skill must break the suite, not the
    user's first session."""
    mod = _catalog_module()
    path = SKILL_ROOT / "references" / "onboarding-roles.md"
    assert path.is_file(), "the role map must exist at %s" % path
    text = path.read_text(encoding="utf-8")
    captured = set(re.findall(r"`([a-z][a-z0-9-]{2,})`", text))
    # Every captured token is a skill claim UNLESS it is a known non-skill CLI
    # token or an explicitly allowed non-family skill -- checked whether or not
    # it is hyphenated, so single-word FAMILY members (e.g. "plan", "exec") are
    # held to the same standard as hyphenated ones instead of passing through
    # unchecked.
    claims = captured - _ROLES_KNOWN_CLI_TOKENS - _ROLES_NON_FAMILY_ALLOWED
    unknown = sorted(claims - set(mod.FAMILY))
    assert not unknown, (
        "the role map names skills the registry does not know: %r. Either add "
        "them to FAMILY in guru_catalog.py, to _ROLES_NON_FAMILY_ALLOWED (an "
        "external skill), or to _ROLES_KNOWN_CLI_TOKENS (a non-skill CLI token)."
        % unknown)
    assert "pvm-guru" in claims, "the map must name pvm-guru at least once"


register("[roles] the role map only names skills the registry knows",
         _role_map_never_names_a_skill_the_registry_does_not_know)


def _role_map_covers_every_role_the_command_offers():
    """A bare substring check would still pass if a whole role section were
    deleted: section 7's fallback prose also names "Разработчик" and
    "Аналитик" by word. Require each role's own numbered heading instead, so
    removing a section (even if the word survives elsewhere) fails the test."""
    path = SKILL_ROOT / "references" / "onboarding-roles.md"
    text = path.read_text(encoding="utf-8")
    for role in ("Менеджер", "Аналитик", "Архитектор", "Разработчик",
                 "Тестировщик", "Техписатель", "Другое"):
        pattern = r"^## \d+\. " + re.escape(role)
        assert re.search(pattern, text, re.MULTILINE), (
            "the role map must have its own numbered heading for %r "
            "(pattern %r not found)" % (role, pattern))


register("[roles] the role map covers all seven menu entries",
         _role_map_covers_every_role_the_command_offers)


def _role_map_names_every_skill_the_registry_ships():
    """The mirror of the test two above, and the one that actually protects the
    user. That test walks map -> registry and catches a stale name; this one
    walks registry -> map and catches a skill nobody is ever told about.
    `/guru-start` shows the intersection of the role map with what is installed,
    so a FAMILY member absent from every section is invisible through the only
    onboarding path there is -- indistinguishable, for that user, from not being
    installed at all.

    Deliberately without an exemption list: FAMILY is the set of skills we
    promise to explain, so "no role wants it" is an argument about FAMILY
    membership, not about this test."""
    mod = _catalog_module()
    path = SKILL_ROOT / "references" / "onboarding-roles.md"
    text = path.read_text(encoding="utf-8")
    # Same capture as the map -> registry test, so both agree on what "named"
    # means: a token written in backticks.
    named = set(re.findall(r"`([a-z][a-z0-9-]{2,})`", text))
    missing = sorted(name for name in mod.FAMILY if name not in named)
    assert not missing, (
        "the registry ships skills the role map never names: %r. Через "
        "/guru-start про них не узнает никто. Either name each in the role "
        "section that owns it, or drop it from FAMILY in guru_catalog.py."
        % missing)


register("[roles] the role map names every skill the registry ships",
         _role_map_names_every_skill_the_registry_ships)


def _a_skill_installed_twice_in_two_versions_is_reported():
    mod = _catalog_module()
    home = _catalog_home(skills={
        "shared": {"pvm-guru": "Свежая версия."},
        "gigacode": {"pvm-guru": "Версия недельной давности."},
    })
    skills, notes = mod.discover_skills(home)
    assert [s["name"] for s in skills] == ["pvm-guru"], \
        "две копии одного навыка остаются одной записью: %r" % (skills,)
    stale = [note for note in notes if "установлен дважды" in note]
    assert len(stale) == 1, "расхождение копий должно попасть в notes ровно один раз: %r" % (notes,)
    assert ".gigacode" in stale[0] and ".agents" in stale[0], \
        "в заметке должны быть оба каталога, чтобы человек понял, что чинить: %r" % (stale[0],)


register("[catalog] a skill installed twice in two versions is reported in notes",
         _a_skill_installed_twice_in_two_versions_is_reported)


def _identical_copies_of_a_skill_stay_silent():
    mod = _catalog_module()
    home = _catalog_home(skills={
        "shared": {"pvm-guru": "Одна и та же версия."},
        "gigacode": {"pvm-guru": "Одна и та же версия."},
    })
    _, notes = mod.discover_skills(home)
    assert notes == [], \
        "одинаковые копии — безобидный дубль, шуметь об этом нельзя: %r" % (notes,)


register("[catalog] identical copies of a skill produce no note",
         _identical_copies_of_a_skill_stay_silent)


# --------------------------------------------------------------------------
# Пути в поставляемых файлах. Литерал `.gigacode/skills/<name>/` резолвится
# только внутри чекаута PVM-GURU: у установленного пользователя навык лежит в
# домашнем каталоге, а агент запущен в его проекте. Один такой путь в команде
# уже стоил сорванного онбординга, поэтому запрет проверяется, а не
# подразумевается. Исключение — блок «Каталог навыка» и заметки о нём: там этот
# путь упомянут как один из кандидатов резолва и как пример того, чего у
# пользователя нет.
# --------------------------------------------------------------------------
_PATH_LITERAL = ".gigacode/skills/"


def _shipped_roots():
    """pvm-guru всегда, соседи по семейству — когда рядом лежит чекаут репозитория.

    У установленного пользователя в соседях могут оказаться чужие скиллы, за которые
    этот запрет не отвечает, поэтому семейство проверяется только из чекаута.
    """
    roots = [SKILL_ROOT]
    parent = SKILL_ROOT.parent
    if parent.name == "skills" and parent.parent.name == ".gigacode":
        roots.extend(sorted(p for p in parent.iterdir() if p.is_dir() and p != SKILL_ROOT))
    return roots


def _shipped_files_never_hardcode_the_repo_runtime_path():
    offenders = []
    files = [path for root in _shipped_roots() for path in sorted(root.rglob("*.md"))]
    for path in files:
        if path.name == "CONTRIBUTING.md":
            continue  # dev-документ: двухкопийная модель там описывается по имени
        in_resolver = False
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # Раздел «Каталог навыка» — единственное место, где этот путь законен: там он
            # один из кандидатов резолва. Секция кончается следующим заголовком того же уровня.
            if line.startswith("## "):
                in_resolver = line.startswith("## Каталог навыка")
            if in_resolver or _PATH_LITERAL not in line:
                continue
            # Строка, объясняющая саму переменную («$SKILL_DIR — это ~/.agents/… или
            # .gigacode/…»), конвенцию не нарушает: она её и вводит.
            if "SKILL_DIR" in line:
                continue
            offenders.append("%s:%d: %s" % (path.relative_to(SKILL_ROOT.parent), number, line.strip()))
    assert not offenders, (
        "поставляемые файлы адресуют скрипты через $SKILL_DIR (или {{SKILL_DIR}} в командах), "
        "а не через каталог чекаута:\n%s" % "\n".join(offenders))


register("[paths] shipped files never hardcode .gigacode/skills",
         _shipped_files_never_hardcode_the_repo_runtime_path)


# --------------------------------------------------------------------------
# Достижимость референсов. Полные `references/*.md` в контекст сами не попадают:
# модель грузит тот файл, на который её послал SKILL.md. Незарегистрированный
# референс существует на диске, проходит все прочие проверки — и не работает,
# потому что до него никто не доходит. Отсюда правило: файл либо перечислен в
# «Обязательных источниках правил», либо его адресует другой документ поставки,
# и тогда он назван здесь с объяснением.
# --------------------------------------------------------------------------
_REFERENCES_ADDRESSED_ELSEWHERE = {
    # Страница пути пользователя: её показывают README и команда /guru-start,
    # а не роутинг сценариев.
    "user-path.md": "README.md и extras/commands/guru-start.md",
}


def _every_reference_is_reachable():
    references = SKILL_ROOT / "references"
    listed = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    unreachable = []
    for path in sorted(references.glob("*.md")):
        if path.name in _REFERENCES_ADDRESSED_ELSEWHERE:
            continue
        if "references/%s" % path.name not in listed:
            unreachable.append(path.name)
    assert not unreachable, (
        "референс есть на диске, но SKILL.md на него не ссылается — модель его не загрузит.\n"
        "Добавьте файл в «Обязательные источники правил» либо, если его адресует другой\n"
        "документ поставки, впишите его в _REFERENCES_ADDRESSED_ELSEWHERE с причиной:\n%s"
        % "\n".join(unreachable))


register("[docs] every reference is reachable from SKILL.md",
         _every_reference_is_reachable)


# --------------------------------------------------------------------------
# Форма веб-ссылки на юнит. API её не отдаёт — у ответа `/rest/api/unit/v2/{code}`
# нет ни одного поля со ссылкой, — поэтому модель собирает её сама и без правила
# скатывается к форме чужого трекера (`/swtr/task/PVM-…` вместо
# `/swtr/units/all/unit/PVM-…`). Пользователь по такой ссылке попадает в
# никуда. Один пример неверной формы в поставляемом файле модель копирует
# охотнее, чем правило из SKILL.md, поэтому примеры проверяются, а не
# подразумеваются. Правило целиком — в SKILL.md, «Веб-ссылка на юнит трекера».
#
# Ловится только форма с настоящим кодом: в SKILL.md неверные варианты
# перечислены с плейсхолдером `<КОД>` — это документация запрета, а не пример.
# --------------------------------------------------------------------------
_FABRICATED_UNIT_URL = re.compile(r"/swtr/(?:task/|browse/|issue/)?[A-Z][A-Z0-9]+-\d+")
_UNIT_URL_SUFFIXES = (".md", ".py", ".json", ".yaml", ".mjs")


def _shipped_files_never_fabricate_a_unit_url():
    offenders = []
    for root in _shipped_roots():
        for path in sorted(root.rglob("*")):
            if path.suffix not in _UNIT_URL_SUFFIXES or "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                match = _FABRICATED_UNIT_URL.search(line)
                if match:
                    offenders.append("%s:%d: %s" % (
                        path.relative_to(root.parent), number, match.group(0)))
    assert not offenders, (
        "ссылка на юнит собирается только как {SWTR_BASE_URL}/units/all/unit/<КОД>; "
        "формы /swtr/<КОД>, /swtr/task/<КОД> и /swtr/browse/<КОД> в SWTR не существуют:\n%s"
        % "\n".join(offenders))


register("[paths] shipped files never fabricate a unit URL",
         _shipped_files_never_fabricate_a_unit_url)


# --------------------------------------------------------------------------
# Бюджет описания навыка. `description` из frontmatter — единственное, что хост
# показывает модели при выборе навыка: полные SKILL.md он в контекст не грузит.
# Чем длиннее описания, тем хуже различаются близкие навыки семейства («ревью»,
# «оцени», «декомпозируй» есть сразу у нескольких), и роутинг начинает промахи-
# ваться. 500 символов — потолок, в который влезает назначение + границы + список
# триггеров; всё, что сверх, — это пересказ SKILL.md не на своём месте.
# --------------------------------------------------------------------------
_DESCRIPTION_BUDGET = 500


def _every_skill_description_fits_the_routing_budget():
    mod = _catalog_module()
    offenders = []
    for root in _shipped_roots():
        path = root / "SKILL.md"
        if not path.is_file():
            continue
        description = mod.read_frontmatter(path).get("description", "")
        if not description:
            offenders.append("%s: пустой description — хост не сможет выбрать навык" % root.name)
            continue
        if len(description) > _DESCRIPTION_BUDGET:
            offenders.append("%s: %d символов" % (root.name, len(description)))
    assert not offenders, (
        "description навыка не должен превышать %d символов — иначе близкие навыки "
        "семейства перестают различаться при роутинге (арбитраж триггеров — в "
        "docs/guru-family.md):\n%s" % (_DESCRIPTION_BUDGET, "\n".join(offenders)))


register("[frontmatter] every skill description fits the routing budget",
         _every_skill_description_fits_the_routing_budget)


def _every_family_skill_ships_a_readable_frontmatter_name():
    """`name` — то, по чему хост и install.mjs находят навык. Расхождение с именем
    каталога ломает вызов по имени, и заметно это только у пользователя."""
    mod = _catalog_module()
    offenders = []
    for root in _shipped_roots():
        path = root / "SKILL.md"
        if not path.is_file():
            continue
        name = mod.read_frontmatter(path).get("name", "")
        # architecture-diagram-skill исторически назван каталогом с суффиксом -skill,
        # а name у него без него: проверяем совпадение по префиксу каталога.
        if name and root.name not in (name, "%s-skill" % name):
            offenders.append("каталог %s, но name: %s" % (root.name, name))
        elif not name:
            offenders.append("%s: нет поля name" % root.name)
    assert not offenders, (
        "имя в frontmatter должно совпадать с именем каталога навыка:\n%s" % "\n".join(offenders))


register("[frontmatter] every skill name matches its directory",
         _every_family_skill_ships_a_readable_frontmatter_name)


def _every_command_addresses_its_skill_through_the_placeholder():
    commands = sorted((SKILL_ROOT / "extras" / "commands").glob("*.md"))
    assert commands, "у навыка должны быть команды в extras/commands"
    for path in commands:
        text = path.read_text(encoding="utf-8")
        assert "{{SKILL_DIR}}" in text, \
            "%s должна адресовать скрипты через плейсхолдер, который подставит install.mjs" % path.name
        for line in text.splitlines():
            if "python3" not in line:
                continue
            assert "{{SKILL_DIR}}" in line or "scripts/" not in line, \
                "%s: запуск скрипта без плейсхолдера: %s" % (path.name, line.strip())


register("[paths] every command addresses its scripts through {{SKILL_DIR}}",
         _every_command_addresses_its_skill_through_the_placeholder)


def _no_unknown_placeholder_survives_the_rollout():
    """install.mjs подставляет РОВНО один плейсхолдер — {{SKILL_DIR}}.

    Любой другой уезжает к пользователю литералом: команда несёт путь, которого
    не существует, и падает на первом же шаге. Ловится только здесь — проверка
    presence выше пропускает незнакомое имя, а у пользователя это уже инцидент.
    Соседний навык адресуется через {{SKILL_DIR}}/../<имя>: все навыки лежат
    братьями в одном каталоге, и это тот же порядок, которым их ищут скрипты.
    """
    import re as _re

    known = "{{SKILL_DIR}}"
    pattern = _re.compile(r"\{\{[^}]+\}\}")
    offenders = []
    for root in _shipped_roots():
        for kind in ("commands", "agents"):
            for path in sorted((root / "extras" / kind).glob("*.md")):
                text = path.read_text(encoding="utf-8")
                for found in sorted(set(pattern.findall(text))):
                    if found != known:
                        offenders.append("%s/%s/%s: %s" % (root.name, kind, path.name, found))
    assert not offenders, (
        "install.mjs подставляет только {{SKILL_DIR}}; остальное уедет к пользователю "
        "литералом и сломает команду:\n%s" % "\n".join(offenders))


register("[paths] no unknown placeholder survives the rollout",
         _no_unknown_placeholder_survives_the_rollout)


def _guru_start_command_is_installable_and_safe():
    mod = _catalog_module()
    path = SKILL_ROOT / "extras" / "commands" / "guru-start.md"
    assert path.is_file(), "the command must live where install.mjs looks: %s" % path
    meta = mod.read_frontmatter(path)
    assert meta.get("name") == "guru-start", \
        "install.mjs and the host resolve the command by its frontmatter name: %r" % (meta,)
    assert meta.get("description"), "the command needs a description for the host's menu"
    assert meta.get("agent") == "build", "sibling commands run on the build agent"

    text = path.read_text(encoding="utf-8")
    assert "guru_catalog.py" in text, "the command must inventory what is installed"
    assert "guru_doctor" in text, "the command must check access before the demo"
    assert "onboarding-roles.md" in text, "the command must consult the role map"
    # Шаг умолчаний держится на скрипте профиля: без него онбординг снова
    # спрашивает роль каждый раз, а логин и пространства некуда сохранить.
    assert "guru_profile.py" in text, \
        "the command must read and save the defaults profile through its own script"
    assert "не бывает записи" in text, \
        "the command must state that onboarding never writes to corporate systems"
    # Несущие правила шага умолчаний: без них шаг перестаёт быть тем, что описан
    # в REQ-006, а поймать это можно только у пользователя.
    assert "не больше четырёх вопросов" in text, \
        "the defaults step must keep its quota of four questions"
    assert "с\nпервого раза" in text or "с первого раза" in text, \
        "a refusal of the defaults step must be accepted the first time"
    assert "профиль не создаётся, роль не сохраняется" in text, \
        "a refused defaults step must create no profile and save no role"
    assert "не\nзапускать" in text or "не запускать" in text, \
        "the command must explicitly forbid running exec during onboarding"
    # Negative guard: --execute is the exact flag that turns every dry-run write
    # command live. An onboarding script must never carry it, so a future edit
    # that reintroduces a write path is caught here, not in a user's first session.
    assert "--execute" not in text, \
        "onboarding must not invite a write action: --execute must never appear"


register("[guru-start] the command is installable and invites no write action",
         _guru_start_command_is_installable_and_safe)


def _guru_profile_command_is_installable_and_safe():
    """Команда профиля едет к пользователю тем же путём, что и соседние: глоб
    install.mjs по extras/commands. Всё, что ниже, ломается только у него —
    имя не совпало (команда не резолвится), description пуст или длиннее бюджета
    роутинга, agent не тот, скрипт не назван. Плюс два запрета: --execute и любой
    плейсхолдер, кроме {{SKILL_DIR}}."""
    mod = _catalog_module()
    path = SKILL_ROOT / "extras" / "commands" / "guru_profile.md"
    assert path.is_file(), "the command must live where install.mjs looks: %s" % path
    meta = mod.read_frontmatter(path)
    assert meta.get("name") == "guru_profile", \
        "install.mjs and the host resolve the command by its frontmatter name: %r" % (meta,)
    description = meta.get("description", "")
    assert description, "the command needs a description for the host's menu"
    assert len(description) <= _DESCRIPTION_BUDGET, \
        "the command description must fit the routing budget of %d characters, got %d" % (
            _DESCRIPTION_BUDGET, len(description))
    assert meta.get("agent") == "build", "sibling commands run on the build agent"

    text = path.read_text(encoding="utf-8")
    assert "guru_profile.py" in text, \
        "the command must drive the profile through its own script"
    # Negative guard, the same one guru-start carries: --execute is the exact flag
    # that turns a dry-run write command live. The profile command touches one local
    # file and nothing else, so an edit that pulls a write path in is caught here.
    assert "--execute" not in text, \
        "the profile command must not invite a write action: --execute must never appear"
    # Несущее правило команды: сброс удаляет файл целиком, поэтому он делается
    # только после отдельного явного подтверждения (REQ-009).
    assert "только после явного «да»" in text, \
        "reset must require an explicit confirmation of its own"
    unknown = sorted(set(re.findall(r"\{\{[^}]+\}\}", text)) - {"{{SKILL_DIR}}"})
    assert not unknown, \
        "install.mjs substitutes only {{SKILL_DIR}}; the rest reaches the user literally: %s" % unknown


register("[guru_profile] the command is installable and invites no write action",
         _guru_profile_command_is_installable_and_safe)


def main() -> int:
    passed = failed = 0
    failures = []
    for name, fn in TESTS:
        try:
            fn()
        except Exception:  # noqa: BLE001 - a smoke case failing must be reported, not raised
            failed += 1
            failures.append((name, traceback.format_exc()))
            print(f"FAIL  {name}")
        else:
            passed += 1
            print(f"ok    {name}")
    print(f"\n== smoke: {passed} passed, {failed} failed ({len(TESTS)} total)")
    for name, tb in failures:
        print(f"\n----- {name} -----\n{tb}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
