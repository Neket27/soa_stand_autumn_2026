"""Вики юнита читается один раз: v1 — это запасной путь, а не второй обязательный.

Оба эндпоинта звались подряд всегда. При живом v2 второй вызов ничего не добавлял:
тот же текст приезжал вторым payload и вторым же уходил в `sources` — лишний
сетевой поход и удвоенный объём источника на каждый юнит. Запасной путь нужен
ровно там, где v2 не ответил или ответил пусто.
"""

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import fetch_swtr_sources as fetch  # noqa: E402

BASE_URL = "https://example.invalid/swtr"
UNIT = {"code": "PVM-1", "summary": "тема", "suit": {"code": "task"},
        "space": {"code": "PVM"}, "attributes": []}
WIKI_V2 = {"body": "текст вики второй версии"}
WIKI_V1 = {"body": "текст вики первой версии"}

# Так v2 отвечает на живом портале: конверт с метаданными юнита, `details` пуст.
# Проверено 27.08.2026 на восьми юнитах PVM и CRPV — при отсутствии вики ответ
# всё равно непустой, поэтому «v2 не ответил» по самому payload не определяется.
WIKI_V2_EMPTY_ENVELOPE = {"details": None, "info": {"summary": "короткая тема", "code": "PVM-1"}}

# Тот же конверт без вики, но `summary` длиннее порога в 80 знаков, на котором
# срабатывает извлекатель длинных строк. На девяти живых юнитах таких оказалось
# четыре (112, 113, 123 и 94 знака) — случай рядовой, а не краевой.
LONG_SUMMARY = "Проверка ограничений на создание конфигурации индекса с генерацией имени по шаблону и валидацией"
WIKI_V2_CARD_ONLY = {"details": None, "info": {"summary": LONG_SUMMARY, "code": "PVM-1"}}


class WikiFallback(unittest.TestCase):
    def collect(self, wiki):
        """`wiki` отвечает на адрес вики: payload, исключение или None."""
        called = []

        def fake_request_json(url, headers, timeout):
            if "/rest/api/unit/v2/" in url:
                return UNIT
            version = "v2" if "/v2/wiki/unit/" in url else "v1"
            called.append(version)
            answer = wiki(version)
            if isinstance(answer, Exception):
                raise answer
            return answer

        original = fetch.request_json
        fetch.request_json = fake_request_json
        try:
            source = fetch.collect_swtr_source("PVM-1", "PVM-1", BASE_URL, {}, 30.0, True)
        finally:
            fetch.request_json = original
        return called, source

    def test_live_v2_does_not_trigger_v1(self):
        called, source = self.collect(lambda v: WIKI_V2 if v == "v2" else WIKI_V1)
        self.assertEqual(called, ["v2"], "v1 позвали при живом v2")
        texts = " ".join(item["text"] for item in source["wikiTexts"])
        self.assertIn("второй версии", texts)
        self.assertNotIn("первой версии", texts, "текст вики приехал дважды")

    def test_empty_v2_falls_back_to_v1(self):
        called, source = self.collect(lambda v: {} if v == "v2" else WIKI_V1)
        self.assertEqual(called, ["v2", "v1"], "пустой v2 не увёл на запасной путь")
        texts = " ".join(item["text"] for item in source["wikiTexts"])
        self.assertIn("первой версии", texts)

    def test_envelope_without_text_falls_back_to_v1(self):
        """Живой v2 всегда отдаёт непустой конверт — откат решается по тексту."""
        called, source = self.collect(
            lambda v: WIKI_V2_EMPTY_ENVELOPE if v == "v2" else WIKI_V1)
        self.assertEqual(called, ["v2", "v1"], "конверт без вики не увёл на запасной путь")
        texts = " ".join(item["text"] for item in source["wikiTexts"])
        self.assertIn("первой версии", texts)

    def test_long_unit_summary_is_not_mistaken_for_wiki(self):
        """Плагин эхом отдаёт карточку юнита; она не доказывает, что вики прочиталась."""
        self.assertGreaterEqual(len(LONG_SUMMARY), 80, "фикстура не переходит порог извлекателя")
        called, source = self.collect(
            lambda v: WIKI_V2_CARD_ONLY if v == "v2" else WIKI_V1)
        self.assertEqual(called, ["v2", "v1"], "эхо карточки сошло за текст вики")
        texts = " ".join(item["text"] for item in source["wikiTexts"])
        self.assertIn("первой версии", texts)

    def test_card_echo_is_not_doubled_when_both_versions_are_read(self):
        """Обе версии отдают одну и ту же карточку — в источник она попадает один раз."""
        card = {"summary": LONG_SUMMARY, "code": "PVM-1"}
        called, source = self.collect(
            lambda v: {"details": None, "info": dict(card)} if v == "v2" else dict(card))
        self.assertEqual(called, ["v2", "v1"])
        texts = [item["text"] for item in source["wikiTexts"]]
        self.assertEqual(texts.count(LONG_SUMMARY), 1, f"карточка продублирована: {texts}")

    def test_foreign_field_inside_the_card_is_not_wiki_either(self):
        """Отсеивается весь подузел карточки, а не два известных ключа под ним.

        `flatten_long_texts` берёт строку любой длины, если её ключ входит в
        `LONG_TEXT_KEYS` — а там `value`, `text`, `content`. Ровно в такой форме
        SWTR отдаёт атрибуты, поэтому любое поле карточки сошло бы за вики.
        """
        card = {"details": None,
                "info": {"summary": "кратко", "code": "PVM-1", "board": {"value": "доска"}}}
        called, source = self.collect(lambda v: card if v == "v2" else WIKI_V1)
        self.assertEqual(called, ["v2", "v1"], "поле карточки сошло за текст вики")
        texts = " ".join(item["text"] for item in source["wikiTexts"])
        self.assertIn("первой версии", texts)

    def test_missing_v2_falls_back_to_v1(self):
        called, _ = self.collect(lambda v: None if v == "v2" else WIKI_V1)
        self.assertEqual(called, ["v2", "v1"])

    def test_broken_v2_falls_back_to_v1_and_keeps_the_reason(self):
        boom = RuntimeError("404 Not Found")
        called, source = self.collect(lambda v: boom if v == "v2" else WIKI_V1)
        self.assertEqual(called, ["v2", "v1"])
        texts = " ".join(item["text"] for item in source["wikiTexts"])
        self.assertIn("первой версии", texts)
        # Причина — половина контракта: по ней человек понимает, почему вики
        # приехала из v1. Свести `except` к `pass` — и она исчезнет молча.
        self.assertIn("404", source["wiki"]["wiki-v2.error"])

    def test_wiki_is_skipped_entirely_when_not_asked(self):
        called = []

        def fake_request_json(url, headers, timeout):
            if "/wiki/unit/" in url:
                called.append(url)
            return UNIT

        original = fetch.request_json
        fetch.request_json = fake_request_json
        try:
            fetch.collect_swtr_source("PVM-1", "PVM-1", BASE_URL, {}, 30.0, False)
        finally:
            fetch.request_json = original
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
