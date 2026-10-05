"""Доктор: под 403 живут три разных отказа, и запись проверяется отдельно.

До этого доктор зондировал только чтение и на любой приложенческий 403 советовал
персональный токен. Для записи такой 403 почти всегда означает нехватку прав в
пространстве, и токен там не помогает — совет уводил чинить не сломанное. Тесты
держат разделение трёх случаев и то, что write-зонд ничего не создаёт.
"""

import contextlib
import io
import sys
import unittest
import urllib.error
from email.message import Message
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import guru_doctor as d  # noqa: E402

GATEWAY_HTML = "<html><body>Ошибка при аутентификации</body></html>"
BACKEND_JSON = '{"errorType":"SWTR_BASIC_ERROR","uiErrorMessage":"нет прав"}'


def headers_with(content_type):
    message = Message()
    message["Content-Type"] = content_type
    return message


def http_error(code, body, content_type):
    return urllib.error.HTTPError(
        "https://swtr.test/x", code, "err", headers_with(content_type),
        io.BytesIO(body.encode("utf-8")),
    )


class FakeResponse:
    def __init__(self, status=200, payload=b"{}"):
        self.status = status
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Urlopen:
    """Записывает каждый Request и отдаёт заранее заданные ответы по порядку."""

    def __init__(self, *responses):
        self.requests = []
        self.responses = list(responses)

    def __call__(self, req, timeout=None, context=None):
        self.requests.append(req)
        result = self.responses.pop(0) if self.responses else FakeResponse()
        if isinstance(result, Exception):
            raise result
        return result

    @property
    def writes(self):
        return [r for r in self.requests if r.get_method() == "POST"]


@contextlib.contextmanager
def doctor_env(urlopen):
    """Подменяет сеть и авторизацию: тесты не ходят наружу и не читают креды."""
    import urllib.request

    original = (urllib.request.urlopen, d.build_authorization, d.build_headers)
    urllib.request.urlopen = urlopen
    d.build_authorization = lambda system: "Basic dXNlcjpwYXNz"
    d.build_headers = lambda *a, **kw: {"Accept": "application/json"}
    try:
        yield
    finally:
        urllib.request.urlopen, d.build_authorization, d.build_headers = original


def run_check(urlopen, **kwargs):
    buf = io.StringIO()
    with doctor_env(urlopen), contextlib.redirect_stdout(buf):
        verdict = d.check_system("task_tracker", "https://swtr.test", None, 5.0, False, **kwargs)
    return verdict, buf.getvalue()


class ReadVerdictTest(unittest.TestCase):
    def test_gateway_html_403_is_a_stale_password(self):
        verdict = d.classify_verdict("task_tracker", 403, None, auth_page=True)
        self.assertEqual(verdict.category, "stale_password")

    def test_backend_json_403_on_read_stays_forbidden(self):
        verdict = d.classify_verdict("task_tracker", 403, None, auth_page=False)
        self.assertEqual(verdict.category, "forbidden")


class WriteVerdictTest(unittest.TestCase):
    def test_404_means_writes_are_not_blocked(self):
        verdict = d.classify_write_verdict(404)
        self.assertTrue(verdict.ok)
        self.assertIn("NOT blocked", verdict.detail)

    def test_404_never_claims_more_than_it_proved(self):
        """Зонд бьёт в несуществующий space и про права в реальном молчит."""
        self.assertIn("NOT covered", d.classify_write_verdict(404).detail)

    def test_backend_403_on_write_is_about_rights(self):
        verdict = d.classify_write_verdict(403)
        self.assertEqual(verdict.category, "no_write_permission")
        self.assertIn("permissions", verdict.detail)

    def test_403_on_write_does_not_advertise_a_token(self):
        """Ровно та ошибка, которую чинил cee948c, только со стороны записи."""
        detail = d.classify_write_verdict(403).detail
        self.assertNotIn("SWTR_BEARER_TOKEN", detail)
        self.assertIn("will not change it", detail)

    def test_gateway_page_on_write_is_a_stale_password(self):
        self.assertEqual(d.classify_write_verdict(403, auth_page=True).category, "stale_password")

    def test_success_is_loud_because_nothing_should_be_created(self):
        verdict = d.classify_write_verdict(200)
        self.assertFalse(verdict.ok)
        self.assertIn("must never succeed", verdict.detail)

    def test_noise_never_downgrades_the_read_verdict(self):
        for status in (None, 500, 418):
            self.assertTrue(d.classify_write_verdict(status).ok, status)


class WriteProbeWiringTest(unittest.TestCase):
    def test_probe_targets_a_space_that_cannot_exist(self):
        urlopen = Urlopen(FakeResponse(200), http_error(404, BACKEND_JSON, "application/json"))
        verdict, out = run_check(urlopen)
        self.assertTrue(verdict.ok)
        self.assertEqual(len(urlopen.writes), 1)
        write = urlopen.writes[0]
        self.assertIn("/rest/api/unit/v2/task/create", write.full_url)
        self.assertIn(d.WRITE_PROBE_SPACE.encode(), write.data)
        self.assertIn("probe (write)", out)

    def test_write_403_becomes_the_system_verdict(self):
        urlopen = Urlopen(FakeResponse(200), http_error(403, BACKEND_JSON, "application/json"))
        verdict, out = run_check(urlopen)
        self.assertEqual(verdict.category, "no_write_permission")
        self.assertIn("WRITE VERDICT", out)

    def test_flag_skips_the_write_call(self):
        urlopen = Urlopen(FakeResponse(200))
        verdict, out = run_check(urlopen, no_write_probe=True)
        self.assertTrue(verdict.ok)
        self.assertEqual(urlopen.writes, [])
        self.assertNotIn("probe (write)", out)

    def test_no_network_makes_no_call_at_all(self):
        urlopen = Urlopen()
        buf = io.StringIO()
        with doctor_env(urlopen), contextlib.redirect_stdout(buf):
            d.check_system("task_tracker", "https://swtr.test", None, 5.0, True)
        self.assertEqual(urlopen.requests, [])

    def test_failed_read_skips_the_write_probe(self):
        """Зондировать запись, когда чтение уже отказало, нечего диагностировать."""
        urlopen = Urlopen(http_error(403, GATEWAY_HTML, "text/html"))
        verdict, _out = run_check(urlopen)
        self.assertEqual(verdict.category, "stale_password")
        self.assertEqual(urlopen.writes, [])

    def test_other_systems_get_no_write_probe(self):
        urlopen = Urlopen(FakeResponse(200))
        buf = io.StringIO()
        with doctor_env(urlopen), contextlib.redirect_stdout(buf):
            d.check_system("source_control", "https://sc.test", None, 5.0, False)
        self.assertEqual(urlopen.writes, [])


class RepairRoutingTest(unittest.TestCase):
    def test_no_write_permission_is_not_fixable_by_new_credentials(self):
        """FIXABLE читает команда /guru_doctor и предлагает перезаписать кред.
        Права новым паролем не появляются — строка отправила бы чинить не то."""
        self.assertNotIn("no_write_permission", d.FIXABLE_CATEGORIES)

    def test_no_write_permission_has_its_own_exit_code(self):
        self.assertEqual(d.SINGLE_EXIT["no_write_permission"], 6)
        self.assertNotIn(6, [v for k, v in d.SINGLE_EXIT.items() if k != "no_write_permission"])

    def test_fixable_line_stays_empty_for_a_rights_problem(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fixable = d.print_fixable({"task_tracker": d.classify_write_verdict(403)})
        self.assertEqual(fixable, [])
        self.assertNotIn("FIXABLE", buf.getvalue())


class WindowsOutputTest(unittest.TestCase):
    """Под Windows stdout в пайпе кодируется ANSI-страницей (cp1251 на русской
    сборке), а не UTF-8. Любой символ вне неё роняет печать на UnicodeEncodeError
    ещё до полезного вывода — доктор умирал на своей же рамке `═══`. Диагностика
    обязана переживать сломанное окружение, поэтому её вывод чисто ASCII."""

    def test_doctor_prints_only_ascii(self):
        source = (SCRIPTS / "guru_doctor.py").read_text(encoding="utf-8")
        offenders = sorted({c for c in source if ord(c) > 127})
        self.assertEqual(offenders, [], "guru_doctor.py must stay ASCII-only: %r" % offenders)

    def test_every_verdict_detail_survives_a_russian_windows_pipe(self):
        details = [d.STALE_PASSWORD_DETAIL % 403, d.NO_WRITE_PERMISSION_DETAIL % 403]
        details += [d.classify_verdict(s, code, None).detail
                    for s in d.ALL_SYSTEMS for code in (200, 401, 403, 404, 500, 418)]
        details += [d.classify_write_verdict(code).detail
                    for code in (200, 401, 403, 404, 500, 418, None)]
        details += list(d.PRIMARY_HINT.values())
        for detail in details:
            detail.encode("cp1251")  # raises UnicodeEncodeError if it would break


class PortalTokenAdviceTest(unittest.TestCase):
    """Bearer для портала работал в коде, но нигде не предлагался, поэтому все
    сидели на пароле и ловили gateway-403 после каждой смены."""

    def test_missing_credential_names_the_token_option(self):
        hint = d.PRIMARY_HINT["task_tracker"]
        self.assertIn("SWTR_BEARER_TOKEN", hint)
        self.assertIn(d.PORTAL_TOKEN_URL, hint)

    def test_scheme_refusal_points_at_where_to_get_the_token(self):
        """403 от бэкенда на чтении — единственный вердикт, где PAT по делу."""
        detail = d.classify_verdict("task_tracker", 403, None, auth_page=False).detail
        self.assertIn("SWTR_BEARER_TOKEN", detail)
        self.assertIn(d.PORTAL_TOKEN_URL, detail)

    def test_token_advice_explains_where_the_token_actually_lies(self):
        """Одного адреса мало: страница не показывает токен, а скачивает файл,
        и внутри него токен стоит после строки `[Authorization] header: Bearer`.
        Без этих шагов человек открывает ссылку и не понимает, что копировать."""
        texts = [d.PRIMARY_HINT["task_tracker"],
                 d.classify_verdict("task_tracker", 403, None, auth_page=False).detail]
        for text in texts:
            self.assertIn("[Authorization] header: Bearer", text)
            self.assertIn("without", text.lower(), "надо сказать, что Bearer копировать не нужно")

    def test_stale_password_still_refuses_to_advertise_a_token(self):
        """Токен не лечит протухший пароль: шлюз отверг сам кред, а не схему."""
        detail = d.classify_verdict("task_tracker", 403, None, auth_page=True).detail
        self.assertIn("update the password", detail)
        self.assertNotIn("SWTR_BEARER_TOKEN", detail)

    def test_rights_problem_still_refuses_to_advertise_a_token(self):
        """Граница с cee948c: прав новым кредом не появится."""
        self.assertNotIn("SWTR_BEARER_TOKEN", d.classify_write_verdict(403).detail)


if __name__ == "__main__":
    unittest.main()
