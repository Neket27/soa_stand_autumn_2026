#!/usr/bin/env python3
"""Round-trip tests for load_env_file()/build_authorization().

Run against any copy of the loader:
    python3 _test_swtr_env.py [path/to/_swtr_common.py]

A credential written to ~/.swtr-review.env must reach the Authorization header
byte-for-byte. These tests pin that contract for the cases real users hit:
special characters, quotes, trailing spaces, and Windows/notepad encodings
(UTF-8 BOM, UTF-16). Exit code is non-zero if any case corrupts the secret.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

DEFAULT_MODULE = Path(__file__).with_name("_swtr_common.py")


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location("swtr_common_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclass in the target module can resolve its
    # __module__ via sys.modules (guru-pr-review.py uses dataclasses).
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def make_line(secret: str, quote: str = '"') -> str:
    return f"SWTR_BASIC_AUTH={quote}{secret}{quote}"


# (label, raw file bytes, expected secret that must reach the header)
def cases():
    s = "Sodeplkin-I.V:p4ss"
    yield ("normal control",            (make_line("Sodeplkin-I.V:p4ssword") + "\n").encode("utf-8"), "Sodeplkin-I.V:p4ssword")
    yield ("special }*#% quoted",       (make_line(s + "}*#%") + "\n").encode("utf-8"),               s + "}*#%")
    yield ("special }*#% unquoted",     (f"SWTR_BASIC_AUTH={s}}}*#%" + "\n").encode("utf-8"),         s + "}*#%")
    yield ("trailing } quoted",         (make_line(s + "}") + "\r\n").encode("utf-8"),                s + "}")
    yield ("trailing apostrophe",       (make_line(s + "'", '"') + "\n").encode("utf-8"),             s + "'")
    yield ("trailing double-quote",     (make_line(s + '"', "'") + "\n").encode("utf-8"),             s + '"')
    # Surrounding whitespace is trimmed from credentials at use-time (_first_env),
    # so a quoted trailing space does not reach the header. Documented behavior.
    yield ("trailing space (trimmed)",  (make_line(s + " ") + "\n").encode("utf-8"),                  s)
    yield ("export prefix",             ("export " + make_line(s + "}") + "\n").encode("utf-8"),      s + "}")
    yield ("UTF-8 BOM + CRLF (notepad)", b"\xef\xbb\xbf" + (make_line(s + "}*") + "\r\n").encode("utf-8"), s + "}*")
    yield ("UTF-16-LE BOM (notepad)",   (make_line(s + "}*") + "\r\n").encode("utf-16"),              s + "}*")


SECRET_ENV_NAMES = (
    "SWTR_BASIC_AUTH", "SWTR_AUTHORIZATION", "TASKTRACKER_AUTHORIZATION",
    "SWTR_BEARER_TOKEN", "SWTR_TOKEN", "TASKTRACKER_TOKEN", "SWTR_COOKIE",
    "SOURCE_CONTROL_BASIC_AUTH", "SC_BASIC_AUTH", "SOURCE_CONTROL_AUTHORIZATION",
    "SC_AUTHORIZATION", "SOURCE_CONTROL_BEARER_TOKEN", "SC_BEARER_TOKEN",
    "SOURCE_CONTROL_TOKEN", "SC_TOKEN", "SOURCE_CONTROL_REUSE_SWTR_AUTH", "SC_REUSE_SWTR_AUTH",
    "SOURCE_CONTROL_COOKIE", "SC_COOKIE",
    "CONFLUENCE_AUTHORIZATION", "CONFLUENCE_BEARER_TOKEN", "CONFLUENCE_TOKEN",
    "CONFLUENCE_BASIC_AUTH", "SBERWORKS_TOKEN", "CONFLUENCE_COOKIE",
    "JIRA_AUTHORIZATION", "JIRA_BEARER_TOKEN", "JIRA_TOKEN", "JIRA_BASIC_AUTH", "JIRA_COOKIE",
)


def _reset_secret_env() -> None:
    for name in SECRET_ENV_NAMES:
        os.environ.pop(name, None)


def run_auth_tests(module_path: Path) -> int:
    """Pin the auth resolution contract that build_headers() relies on.

    Covers what the round-trip test above does not: the Bearer path, the
    per-system ordering (task_tracker/source_control are Basic-first; the
    sberworks DC confluence/jira are Bearer-first and read only their own env),
    the opt-in SWTR->SourceControl reuse, plus redact_secrets() and
    safe_headers_for_log(). A regression here would either send the wrong
    credential to a host or leak a secret into logs.
    """
    mod = load_module(module_path)
    failures = 0
    print(f"\n== auth ordering / bearer / redaction :: {module_path}")

    def check(label: str, got, expected) -> None:
        nonlocal failures
        ok = got == expected
        print(f"  {'OK  ' if ok else 'FAIL'}  {label:52} got={got!r}")
        if not ok:
            print(f"        expected={expected!r}")
            failures += 1

    def check_true(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        print(f"  {'OK  ' if ok else 'FAIL'}  {label:52} {detail}")
        if not ok:
            failures += 1

    b64 = lambda raw: "Basic " + base64.b64encode(raw.encode("utf-8")).decode("ascii")

    # -- task_tracker (portal): Basic-first, then Authorization, Bearer, raw token.
    _reset_secret_env(); os.environ["SWTR_BEARER_TOKEN"] = "TKN1"
    check("swtr bearer token -> 'Bearer TKN1'", mod.build_authorization("task_tracker"), "Bearer TKN1")

    # A bare portal token is a PAT and needs the Bearer scheme: sent raw it became
    # "Authorization: RAW1", which every backend answers 401 to without saying why.
    _reset_secret_env(); os.environ["SWTR_TOKEN"] = "RAW1"
    check("swtr raw token -> 'Bearer RAW1'", mod.build_authorization("task_tracker"), "Bearer RAW1")

    _reset_secret_env(); os.environ["SWTR_TOKEN"] = "Bearer RAW2"
    check("swtr token with a scheme is not double-prefixed",
          mod.build_authorization("task_tracker"), "Bearer RAW2")

    _reset_secret_env(); os.environ["SWTR_BASIC_AUTH"] = "user:pw"; os.environ["SWTR_BEARER_TOKEN"] = "TKN2"
    check("swtr Basic wins over Bearer", mod.build_authorization("task_tracker"), b64("user:pw"))

    # -- confluence (sberworks DC): Bearer-first; raw PAT sent as Bearer; own env only.
    _reset_secret_env(); os.environ["CONFLUENCE_TOKEN"] = "CT1"
    check("confluence raw PAT -> 'Bearer CT1'", mod.build_authorization("confluence"), "Bearer CT1")

    _reset_secret_env(); os.environ["SBERWORKS_TOKEN"] = "SW1"
    check("confluence SBERWORKS_TOKEN alias -> Bearer", mod.build_authorization("confluence"), "Bearer SW1")

    _reset_secret_env(); os.environ["CONFLUENCE_BEARER_TOKEN"] = "CB1"; os.environ["CONFLUENCE_TOKEN"] = "CT2"
    check("confluence Bearer token beats raw token", mod.build_authorization("confluence"), "Bearer CB1")

    # -- jira (sberworks DC): Bearer-first, isolated from confluence secrets.
    _reset_secret_env(); os.environ["JIRA_BEARER_TOKEN"] = "JB1"
    check("jira bearer -> 'Bearer JB1'", mod.build_authorization("jira"), "Bearer JB1")

    _reset_secret_env(); os.environ["CONFLUENCE_BEARER_TOKEN"] = "CB2"
    check("jira has no confluence bleed-through", mod.build_authorization("jira"), None)

    # -- source_control (portal): Basic-first, then Authorization, Bearer, raw token; reuse opt-in.
    _reset_secret_env(); os.environ["SOURCE_CONTROL_BEARER_TOKEN"] = "SCB1"
    check("sc bearer -> 'Bearer SCB1'", mod.build_authorization("source_control"), "Bearer SCB1")

    _reset_secret_env(); os.environ["SC_BASIC_AUTH"] = "u:p"; os.environ["SOURCE_CONTROL_BEARER_TOKEN"] = "SCB2"
    check("sc Basic (SC_BASIC_AUTH) wins over Bearer", mod.build_authorization("source_control"), b64("u:p"))

    _reset_secret_env(); os.environ["SC_TOKEN"] = "SCRAW1"
    check("sc raw token -> 'Bearer SCRAW1'", mod.build_authorization("source_control"), "Bearer SCRAW1")

    _reset_secret_env(); os.environ["SWTR_BASIC_AUTH"] = "u:p"
    check("sc does NOT reuse SWTR without opt-in", mod.build_authorization("source_control"), None)

    _reset_secret_env(); os.environ["SWTR_BEARER_TOKEN"] = "SHARED"; os.environ["SOURCE_CONTROL_REUSE_SWTR_AUTH"] = "1"
    check("sc reuses SWTR when opt-in set", mod.build_authorization("source_control"), "Bearer SHARED")

    # -- redact_secrets(): configured secret vanishes in both raw and Bearer form.
    _reset_secret_env(); os.environ["SWTR_BEARER_TOKEN"] = "SUPERSECRET"
    red = mod.redact_secrets("HTTP 401: token Bearer SUPERSECRET rejected; raw SUPERSECRET too")
    check_true("redact_secrets removes token + Bearer form",
               "SUPERSECRET" not in red and "redacted" in red, f"got={red!r}")

    # -- redact_secrets(): the BARE token from a full *_AUTHORIZATION header value
    #    ("Bearer <token>") is masked even when the backend echoes only the token.
    _reset_secret_env(); os.environ["SWTR_AUTHORIZATION"] = "Bearer abc123secret"
    red = mod.redact_secrets("HTTP 401 body: {\"token\": \"abc123secret\"} was rejected")
    check_true("redact_secrets masks bare Bearer token from *_AUTHORIZATION",
               "abc123secret" not in red and "redacted" in red, f"got={red!r}")

    # -- redact_secrets(): the BARE base64 payload from a Basic header is masked.
    _reset_secret_env()
    basic_payload = base64.b64encode(b"user:pw12345").decode("ascii")
    os.environ["JIRA_AUTHORIZATION"] = "Basic " + basic_payload
    red = mod.redact_secrets("resp: creds " + basic_payload + " denied")
    check_true("redact_secrets masks bare Basic base64 payload",
               basic_payload not in red and "redacted" in red, f"got={red!r}")

    # -- short-remainder guard: a < 4 char remainder must NOT be registered as a
    #    bare secret (masking a 1-3 char string would corrupt unrelated output).
    #    The full header value stays redactable; only the bare short token is spared.
    _reset_secret_env(); os.environ["SWTR_AUTHORIZATION"] = "Bearer abc"
    red = mod.redact_secrets("harmless abc token in unrelated text")
    check_true("redact_secrets does not over-mask a short (<4) bare remainder",
               "abc" in red, f"got={red!r}")

    # -- redact_secrets(): a "Basic <base64>" *_AUTHORIZATION is masked in its
    #    DECODED form too -- both "user:password" and the bare password -- so a
    #    backend echoing the decoded credential (not the base64) cannot leak it.
    _reset_secret_env()
    os.environ["SWTR_AUTHORIZATION"] = "Basic " + base64.b64encode(b"alice:s3cretpw").decode("ascii")
    red = mod.redact_secrets("resp: login alice:s3cretpw failed; bare pw s3cretpw rejected")
    check_true("redact_secrets masks decoded Basic user:password + bare password",
               "alice:s3cretpw" not in red and "s3cretpw" not in red and "redacted" in red, f"got={red!r}")

    # -- short-value guard: a decoded Basic password shorter than 4 chars must NOT
    #    be registered (masking a 1-3 char string would corrupt unrelated output).
    #    The decoded "user:pw" (>=4) is still masked; only the bare "pw" is spared.
    _reset_secret_env()
    os.environ["JIRA_AUTHORIZATION"] = "Basic " + base64.b64encode(b"bob:pw").decode("ascii")
    red = mod.redact_secrets("an unrelated pw appears in prose about qa")
    check_true("redact_secrets does not over-mask a short (<4) decoded Basic password",
               "pw" in red, f"got={red!r}")

    # -- redact_secrets(): an individual cookie VALUE (the part after '=') is
    #    masked so a partial echo of one cookie pair does not leak the session.
    _reset_secret_env()
    os.environ["SWTR_COOKIE"] = "JSESSIONID=abc123session; p=1"
    red = mod.redact_secrets("body echoed the cookie value abc123session verbatim")
    check_true("redact_secrets masks an individual cookie value",
               "abc123session" not in red and "redacted" in red, f"got={red!r}")

    # -- redact_secrets(): a FLAG cookie carries no secret. api_swtr_as21=true is
    #    the documented companion of the portal Bearer token (backends-and-auth.md),
    #    and its value is 4 chars -- long enough for the cookie-value rule above.
    #    Registering it would rewrite every JSON `true` in an echoed error body.
    _reset_secret_env()
    os.environ["SWTR_COOKIE"] = "api_swtr_as21=true"
    body = '{"ok": true, "skipTlsVerify": true, "draft": false}'
    red = mod.redact_secrets(body)
    check_true("redact_secrets does not mask a boolean flag cookie value",
               red == body, f"got={red!r}")

    # -- safe_headers_for_log(): Authorization/Cookie masked, others intact, input unmutated.
    _reset_secret_env()
    original = {"Authorization": "Bearer abc", "Cookie": "s=1", "Accept": "application/json"}
    safe = mod.safe_headers_for_log(original)
    check_true("safe_headers_for_log masks Authorization/Cookie",
               safe.get("Authorization") == "redacted" and safe.get("Cookie") == "redacted"
               and safe.get("Accept") == "application/json" and original["Authorization"] == "Bearer abc",
               f"got={safe!r}")

    # -- safe_response_headers(): sensitive RESPONSE headers redacted case-insensitively,
    #    others intact, input unmutated. Guards the create --execute header leak.
    resp = {"Set-Cookie": "SESSIONID=SECRET", "WWW-Authenticate": "Basic realm=x",
            "content-type": "application/json"}
    safe_resp = mod.safe_response_headers(resp)
    check_true("safe_response_headers redacts Set-Cookie/WWW-Authenticate, keeps others",
               safe_resp.get("Set-Cookie") == "<redacted>"
               and safe_resp.get("WWW-Authenticate") == "<redacted>"
               and safe_resp.get("content-type") == "application/json"
               and resp["Set-Cookie"] == "SESSIONID=SECRET",
               f"got={safe_resp!r}")

    # -- is_auth_error(): distinguishes fatal 401/403 auth failures from benign
    #    per-item errors (404/timeout/network). request_http raises RuntimeError
    #    *from* an HTTPError, so the check must walk the __cause__ chain; a plain
    #    "HTTP 401 "/"HTTP 403 " message is the string fallback. This is what the
    #    best-effort commands (search/similar/tms) use to re-raise vs collect.
    iae = mod.is_auth_error

    def _wrapped(code: int):
        cause = urllib.error.HTTPError("https://x", code, "no", None, io.BytesIO(b"{}"))
        try:
            raise RuntimeError(f"HTTP {code} for https://x: denied") from cause
        except RuntimeError as exc:
            return exc

    check_true("is_auth_error: RuntimeError<-HTTPError 401 is auth", iae(_wrapped(401)))
    check_true("is_auth_error: RuntimeError<-HTTPError 403 is auth", iae(_wrapped(403)))
    check_true("is_auth_error: RuntimeError<-HTTPError 404 is NOT auth", not iae(_wrapped(404)))
    check_true("is_auth_error: bare HTTPError 401 is auth",
               iae(urllib.error.HTTPError("https://x", 401, "no", None, io.BytesIO(b"{}"))))
    check_true("is_auth_error: 'HTTP 401 ' message fallback (no cause)",
               iae(RuntimeError("HTTP 401 for https://x: denied")))
    check_true("is_auth_error: 'HTTP 403 ' message fallback (no cause)",
               iae(RuntimeError("HTTP 403 for https://x: forbidden")))
    check_true("is_auth_error: 'HTTP 404 ' message is NOT auth",
               not iae(RuntimeError("HTTP 404 for https://x: not found")))
    check_true("is_auth_error: network-error RuntimeError is NOT auth",
               not iae(RuntimeError("Network error for https://x: timed out")))

    _reset_secret_env()
    print(f"== {'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
    return 1 if failures else 0


def run_helper_tests(module_path: Path) -> int:
    """Pin the shared text helpers consolidated into _swtr_common (Task 4).

    value_to_text / significant_terms / normalize_text / extract_unit_code are
    reused across every tool, but the happy-path smoke checks only assert
    top-level output shape, so a regression in a helper would pass silently.
    These direct unit tests lock the merged dict-key precedence (incl. the new
    ``text`` key), the strip_html branch, stopword/frequency ranking, HTML
    normalization, and unit-code extraction.
    """
    mod = load_module(module_path)
    failures = 0
    print(f"\n== shared text helpers :: {module_path}")

    def check(label: str, got, expected) -> None:
        nonlocal failures
        ok = got == expected
        print(f"  {'OK  ' if ok else 'FAIL'}  {label:52} got={got!r}")
        if not ok:
            print(f"        expected={expected!r}")
            failures += 1

    vtt = mod.value_to_text
    # -- value_to_text: default branch collapses whitespace, keeps HTML/entities.
    check("value_to_text collapses whitespace", vtt("  a   b\n c "), "a b c")
    check("value_to_text default keeps tags/entities",
          vtt("<p>Hello  &amp; bye</p>"), "<p>Hello &amp; bye</p>")
    # -- value_to_text: strip_html unescapes entities and removes tags.
    check("value_to_text strip_html removes tags",
          vtt("<p>Hello  &amp; bye</p>", strip_html=True), "Hello & bye")
    # -- dict-key precedence: name > summary > title > code > login > value > text.
    check("value_to_text dict name wins", vtt({"name": "N", "summary": "S", "value": "V"}), "N")
    check("value_to_text dict code before value", vtt({"code": "CRPV-1", "value": "V"}), "CRPV-1")
    # -- the merged 'text' key resolves the inner text when earlier keys are empty
    #    (this is the intended behavior of the consolidated helper; finding #5).
    check("value_to_text dict falls through empty value to text",
          vtt({"value": "", "text": "inner"}), "inner")
    # -- HTML-bearing dict + strip_html strips within the resolved inner value.
    check("value_to_text strip_html on dict text key",
          vtt({"text": "<i>X</i>"}, strip_html=True), "X")

    st = mod.significant_terms
    # -- significant_terms: rank by frequency, drop <4 / numeric / stopwords, ties A->Z.
    check("significant_terms ranks by frequency",
          st("integration integration integration payment payment gateway", set()),
          ["integration", "payment", "gateway"])
    check("significant_terms drops stopword + short + numeric",
          st("alpha alpha cat 2024 house", {"alpha"}), ["house"])
    check("significant_terms ties broken A->Z", st("zebra alpha", set()), ["alpha", "zebra"])
    check("significant_terms honors limit", st("aaaa bbbb cccc dddd", set(), limit=2), ["aaaa", "bbbb"])

    nt = mod.normalize_text
    check("normalize_text unescapes + strips tags", nt("<b>A</b> &amp; B"), "A & B")
    check("normalize_text on dict value key", nt({"value": "<i>Z</i>"}), "Z")

    euc = mod.extract_unit_code
    check("extract_unit_code lowercases input", euc("см. crpv-35498 тут"), "CRPV-35498")
    check("extract_unit_code url-decodes", euc("path%2FPVM-53979"), "PVM-53979")
    check("extract_unit_code none when absent", euc("no code here"), None)

    print(f"== {'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
    return 1 if failures else 0


def run(module_path: Path) -> int:
    mod = load_module(module_path)
    failures = 0
    print(f"== load_env_file round-trip :: {module_path}")
    with tempfile.TemporaryDirectory() as d:
        env_path = Path(d) / "t.env"
        for label, raw, expected in cases():
            os.environ.pop("SWTR_BASIC_AUTH", None)
            env_path.write_bytes(raw)
            try:
                mod.load_env_file(env_path)
            except Exception as exc:  # a parser must never crash on a user's file
                print(f"  FAIL  {label:28} raised {type(exc).__name__}: {exc}")
                failures += 1
                continue
            got = os.environ.get("SWTR_BASIC_AUTH")
            auth = mod.build_authorization("task_tracker")
            decoded = (
                base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8", "replace")
                if auth and auth.startswith("Basic ")
                else None
            )
            # The contract is what reaches the Authorization header.
            ok = decoded == expected
            print(f"  {'OK  ' if ok else 'FAIL'}  {label:28} env={got!r} decoded={decoded!r}")
            if not ok:
                failures += 1
    os.environ.pop("SWTR_BASIC_AUTH", None)
    print(f"== {'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else DEFAULT_MODULE
    rc_roundtrip = run(path)
    rc_auth = run_auth_tests(path)
    rc_helpers = run_helper_tests(path)
    raise SystemExit(1 if (rc_roundtrip or rc_auth or rc_helpers) else 0)
