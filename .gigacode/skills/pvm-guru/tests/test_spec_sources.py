"""Источники постановки: связанным считается только то, что названо явно."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import spec_sources as ctx  # noqa: E402

BASE = "https://portal.works.prod.sbt/swtr"


def attribute(name, text, code="a1", type_="text"):
    """Атрибут в той форме, в какой его отдаёт `fetch_swtr_sources.extract_attributes`.

    Ключ `value` обязателен: рендер читает сперва его, и фикстура без `value`
    гоняла бы только запасную ветку, а прод — основную.
    """
    return {"code": code, "name": name, "type": type_, "value": text, "text": text}


def source(code, summary="", description="", attributes=None, wiki=None):
    return {
        "inputType": "swtr_unit",
        "code": code,
        "summary": summary,
        "description": description,
        "url": "%s/units/all/unit/%s" % (BASE, code),
        "fetchedAt": "2026-08-31T10:00:00+00:00",
        "space": "PVM",
        "suit": "task",
        "attributes": attributes or [],
        "wikiTexts": [{"path": "wiki-v2.details", "text": text} for text in (wiki or [])],
    }


def args(*argv):
    return ctx.build_parser().parse_args(list(argv))


class MentionTest(unittest.TestCase):
    def test_finds_codes_in_all_texts_once_and_in_order(self):
        texts = ctx.source_texts(source(
            "PVM-1",
            description="Опирается на PVM-2 и PVM-2 повторно.",
            attributes=[attribute("Состав работ", "Смотри CRPV-9.")],
            wiki=["Регламент описан в PVM-3."],
        ))
        self.assertEqual(ctx.mentioned_codes(texts, ["PVM-1"]), ["PVM-2", "CRPV-9", "PVM-3"])

    def test_root_and_visited_codes_are_excluded(self):
        texts = ["Задача PVM-1 связана с PVM-2."]
        self.assertEqual(ctx.mentioned_codes(texts, ["PVM-1", "PVM-2"]), [])

    def test_requirement_ids_are_not_units(self):
        # REQ-001 совпадает с формой кода юнита, но юнитом не является:
        # без отсева обход уходил бы в трекер за несуществующей задачей.
        self.assertEqual(ctx.mentioned_codes(["Покрывает REQ-001 и REQ-002."], ["PVM-1"]), [])


class UrlTest(unittest.TestCase):
    def test_tracker_link_becomes_a_code_not_an_external_link(self):
        codes, external = ctx.split_urls(
            ["Постановка: %s/units/all/unit/PVM-42" % BASE], BASE, ["PVM-1"])
        self.assertEqual(codes, ["PVM-42"])
        self.assertEqual(external, [])

    def test_sberworks_wiki_is_confluence_with_a_gated_hint(self):
        _, external = ctx.split_urls(["См. https://sberworks.ru/wiki/display/PVM/Page"], BASE, [])
        self.assertEqual(len(external), 1)
        self.assertEqual(external[0]["system"], "confluence")
        self.assertIn("--confirm-sberworks", external[0]["readCommand"])

    def test_sberworks_browse_is_jira_with_the_key(self):
        _, external = ctx.split_urls(["Тикет https://sberworks.ru/jira/browse/ABC-77"], BASE, [])
        self.assertEqual(external[0]["system"], "jira")
        self.assertEqual(external[0]["key"], "ABC-77")
        self.assertIn("ABC-77", external[0]["readCommand"])

    def test_confluence_and_jira_share_one_host_and_are_told_apart_by_path(self):
        # На sberworks обе системы живут на одном хосте: `…/wiki` и `…/jira`.
        # Различение по хосту относило бы каждую вики-страницу к Jira.
        _, external = ctx.split_urls(
            ["Вики https://sberworks.ru/wiki/display/PVM/Page и тикет https://sberworks.ru/jira/browse/ABC-77"],
            BASE, [])
        by_system = {ref["system"]: ref for ref in external}
        self.assertEqual(set(by_system), {"confluence", "jira"})
        self.assertIn("confluence_tool.py", by_system["confluence"]["readCommand"])
        self.assertIn("jira_tool.py", by_system["jira"]["readCommand"])

    def test_foreign_host_is_reported_without_a_command(self):
        _, external = ctx.split_urls(["Документ https://example.org/spec.pdf"], BASE, [])
        self.assertEqual(external[0]["system"], "other")
        self.assertIsNone(external[0]["readCommand"])

    def test_trailing_punctuation_is_not_part_of_the_url(self):
        _, external = ctx.split_urls(["Ссылка https://example.org/a."], BASE, [])
        self.assertEqual(external[0]["url"], "https://example.org/a")


class TargetOrderTest(unittest.TestCase):
    def test_explicit_links_come_before_text_mentions(self):
        links = [{"unit": "PVM-2", "relation": "реализуется в", "unitSummary": "Тема"}]
        merged = ctx.merge_targets(ctx.link_targets(links), ["PVM-3"], ["PVM-1"])
        self.assertEqual([row["code"] for row in merged], ["PVM-2", "PVM-3"])
        self.assertEqual(merged[0]["origin"], "link")
        self.assertEqual(merged[1]["origin"], "mention")

    def test_a_code_that_is_both_linked_and_mentioned_appears_once_as_a_link(self):
        links = [{"unit": "PVM-2", "relation": "зависит от"}]
        merged = ctx.merge_targets(ctx.link_targets(links), ["PVM-2"], [])
        self.assertEqual([row["code"] for row in merged], ["PVM-2"])
        self.assertEqual(merged[0]["origin"], "link")

    def test_deleted_links_never_become_sources(self):
        # `link find` отдаёт и удалённые связи. Отсев живёт в `fetch_tracker_links`,
        # поэтому проверять его надо там: `link_targets` про `deleted` не знает и
        # вернула бы такую связь целью.
        real_post, real_enrich = ctx.tracker.post_json, ctx.tracker.enrich_links
        ctx.tracker.post_json = lambda base, endpoint, body, timeout: {
            "content": [{"source": "PVM-1", "destination": "PVM-2", "type": "x", "deleted": True},
                        {"source": "PVM-1", "destination": "PVM-3", "type": "x", "deleted": False}],
            "hasNext": False, "totalElements": 2}
        ctx.tracker.enrich_links = lambda links, unit, base, timeout: (
            [{"unit": row["destination"], "relation": "x"} for row in links], [])
        try:
            found, notes, ok = ctx.fetch_tracker_links("PVM-1", BASE, 5.0, 50, None)
        finally:
            ctx.tracker.post_json, ctx.tracker.enrich_links = real_post, real_enrich
        self.assertTrue(ok)
        self.assertEqual([row["unit"] for row in found], ["PVM-3"])

    def test_truncated_link_page_is_reported(self):
        # Читается одна страница; молча срезать нельзя — инструмент обещает, что
        # непрочитанное остаётся видимым.
        real_post, real_enrich = ctx.tracker.post_json, ctx.tracker.enrich_links
        ctx.tracker.post_json = lambda base, endpoint, body, timeout: {
            "content": [{"source": "PVM-1", "destination": "PVM-2", "type": "x"}],
            "hasNext": True, "totalElements": 120}
        ctx.tracker.enrich_links = lambda links, unit, base, timeout: ([], [])
        try:
            _, notes, ok = ctx.fetch_tracker_links("PVM-1", BASE, 5.0, 1, None)
        finally:
            ctx.tracker.post_json, ctx.tracker.enrich_links = real_post, real_enrich
        self.assertTrue(ok)
        self.assertTrue(any("120" in note and "--link-page-size" in note for note in notes), notes)


class TraversalTest(unittest.TestCase):
    def setUp(self):
        self.fetched = []
        self.graph = {
            "PVM-1": source("PVM-1", "Корень", "Основной текст."),
            "PVM-2": source("PVM-2", "Связанная", "Текст связанной."),
            "PVM-3": source("PVM-3", "Вторая", "Текст второй."),
            "PVM-4": source("PVM-4", "Внучка", "Текст внучки."),
        }
        self.links = {"PVM-1": [{"unit": "PVM-2", "relation": "уточняется в"},
                                {"unit": "PVM-3", "relation": "зависит от"}],
                      "PVM-2": [{"unit": "PVM-4", "relation": "уточняется в"}]}
        self.comments = {}
        self.comment_calls = []
        self.comment_error = None
        self.link_ok = True
        self.link_notes = []
        self.probed = []

        def fake_collect(input_value, code, base_url, headers, timeout, include_wiki):
            self.fetched.append(code)
            if code not in self.graph:
                raise RuntimeError("HTTP 404 for %s" % code)
            return self.graph[code]

        def fake_links(code, base_url, timeout, page_size, types):
            return list(self.links.get(code, [])), list(self.link_notes), self.link_ok

        def fake_existing(codes, base_url, timeout):
            # Проверку существования играет граф: код, которого в нём нет,
            # трекер бы не вернул — ровно так же, как `ISO-8601`.
            self.probed.append(list(codes))
            return [c for c in codes if c in self.graph], []

        def fake_comments(code, base_url, timeout):
            self.comment_calls.append(code)
            if self.comment_error is not None:
                return [], ["комментарии %s не прочитаны: %s" % (code, self.comment_error)]
            return list(self.comments.get(code, [])), []

        self._real_collect = ctx.fetch.collect_swtr_source
        self._real_links = ctx.fetch_tracker_links
        self._real_comments = ctx.fetch_comments
        self._real_existing = ctx.existing_codes
        ctx.fetch.collect_swtr_source = fake_collect
        ctx.fetch_tracker_links = fake_links
        ctx.fetch_comments = fake_comments
        ctx.existing_codes = fake_existing

    def tearDown(self):
        ctx.fetch.collect_swtr_source = self._real_collect
        ctx.fetch_tracker_links = self._real_links
        ctx.fetch_comments = self._real_comments
        ctx.existing_codes = self._real_existing

    def test_depth_one_reads_the_task_and_its_own_links_only(self):
        payload = ctx.collect(args("PVM-1", "--base-url", BASE), {})
        self.assertEqual([row["code"] for row in payload["linked"]], ["PVM-2", "PVM-3"])
        self.assertNotIn("PVM-4", self.fetched)

    def test_depth_two_follows_links_of_the_linked_pages(self):
        payload = ctx.collect(args("PVM-1", "--depth", "2", "--base-url", BASE), {})
        self.assertEqual([row["code"] for row in payload["linked"]], ["PVM-2", "PVM-3", "PVM-4"])

    def test_max_linked_caps_reading_and_names_what_was_left(self):
        payload = ctx.collect(args("PVM-1", "--max-linked", "1", "--base-url", BASE), {})
        self.assertEqual([row["code"] for row in payload["linked"]], ["PVM-2"])
        self.assertEqual([row["code"] for row in payload["skipped"]], ["PVM-3"])
        self.assertTrue(any("предел" in note for note in payload["notes"]))

    def test_an_unreadable_link_is_recorded_and_the_rest_still_reads(self):
        self.links["PVM-1"] = [{"unit": "PVM-404", "relation": "зависит от"},
                               {"unit": "PVM-2", "relation": "уточняется в"}]
        payload = ctx.collect(args("PVM-1", "--base-url", BASE), {})
        self.assertEqual([row["code"] for row in payload["linked"]], ["PVM-2"])
        self.assertEqual(payload["skipped"][0]["code"], "PVM-404")
        self.assertIn("404", payload["skipped"][0]["reason"])

    def test_no_links_flag_leaves_only_the_root(self):
        payload = ctx.collect(args("PVM-1", "--no-links", "--base-url", BASE), {})
        self.assertEqual(payload["linked"], [])
        self.assertEqual(self.fetched, ["PVM-1"])

    def test_comments_are_off_by_default(self):
        payload = ctx.collect(args("PVM-1", "--base-url", BASE), {})
        self.assertEqual(payload["comments"], [])
        self.assertEqual(self.comment_calls, [])

    def test_a_page_named_only_in_a_comment_is_still_read(self):
        # Уточнение постановки чаще дописывают комментарием, чем правкой описания:
        # обход, который не читает комментарии, теряет названный там источник.
        self.comments["PVM-1"] = [{"text": "Актуальная постановка — PVM-3.", "createdBy": "Автор",
                                   "createdAt": "2026-08-31", "deleted": False}]
        self.links = {}
        payload = ctx.collect(args("PVM-1", "--with-comments", "--base-url", BASE), {})
        self.assertEqual([row["code"] for row in payload["linked"]], ["PVM-3"])
        self.assertEqual(payload["linked"][0]["origin"], "mention")

    def test_an_unreadable_comment_channel_is_a_note_not_a_failure(self):
        # Заметку сочиняет сам скрипт, а не фейк: подменяется транспорт уровнем
        # ниже (`tracker.post_json`), иначе тест проверял бы строку, которую
        # написал сам, и настоящая ветка отказа осталась бы непройденной.
        ctx.fetch_comments = self._real_comments
        real_post = ctx.tracker.post_json

        def failing(base, endpoint, body, timeout):
            raise RuntimeError("HTTP 500 for %s: redacted" % endpoint)

        ctx.tracker.post_json = failing
        try:
            payload = ctx.collect(args("PVM-1", "--with-comments", "--base-url", BASE), {})
        finally:
            ctx.tracker.post_json = real_post
        self.assertEqual([row["code"] for row in payload["linked"]], ["PVM-2", "PVM-3"])
        self.assertTrue(any("комментарии PVM-1 не прочитаны" in note for note in payload["notes"]),
                        payload["notes"])

    def test_comments_of_a_linked_page_fail_on_their_own(self):
        # Отказ у ребёнка не должен выглядеть отказом у корня: в заметке стоит
        # код того юнита, чей канал не прочитался.
        ctx.fetch_comments = self._real_comments
        real_post = ctx.tracker.post_json

        def only_child_fails(base, endpoint, body, timeout):
            if body.get("filters", {}).get("properties", {}).get("and", [{}])[0].get("value") == "PVM-2" \
                    or "PVM-2" in json.dumps(body, ensure_ascii=False):
                raise RuntimeError("HTTP 500 for %s: redacted" % endpoint)
            return {"content": [], "hasNext": False, "totalElements": 0}

        ctx.tracker.post_json = only_child_fails
        try:
            payload = ctx.collect(args("PVM-1", "--with-comments", "--base-url", BASE), {})
        finally:
            ctx.tracker.post_json = real_post
        self.assertTrue(any("комментарии PVM-2 не прочитаны" in note for note in payload["notes"]),
                        payload["notes"])
        self.assertFalse(any("комментарии PVM-1" in note for note in payload["notes"]),
                         payload["notes"])

    def test_an_unauthorized_comment_channel_is_fatal(self):
        # 401 кладёт все каналы разом — глотать его нельзя даже здесь.
        ctx.fetch_comments = self._real_comments
        real_post = ctx.tracker.post_json

        def unauthorized(base, endpoint, body, timeout):
            raise RuntimeError("HTTP 401 for %s: redacted" % endpoint)

        ctx.tracker.post_json = unauthorized
        try:
            with self.assertRaises(RuntimeError):
                ctx.collect(args("PVM-1", "--with-comments", "--base-url", BASE), {})
        finally:
            ctx.tracker.post_json = real_post

    def test_codeshaped_words_never_reach_the_tracker(self):
        # `UNIT_CODE_RE` ловит любой `ABC-123`, и текст постановки полон таких
        # строк: ISO-8601, UTF-8, HTTP-404. Без проверки существования каждая
        # уходила отдельным чтением и оседала в «Не прочитано».
        self.links = {}
        self.graph["PVM-1"] = source("PVM-1", "Корень",
                                     "Формат ISO-8601, кодировка UTF-8, ошибка HTTP-404, но см. PVM-2.")
        payload = ctx.collect(args("PVM-1", "--base-url", BASE), {})
        self.assertEqual([row["code"] for row in payload["linked"]], ["PVM-2"])
        self.assertEqual(payload["skipped"], [])
        self.assertEqual(self.fetched, ["PVM-1", "PVM-2"])
        self.assertIn("ISO-8601", self.probed[0])  # кандидат был, но одним запросом

    def test_a_failed_read_spends_the_limit(self):
        # Иначе предел не ограничивает число запросов вовсе: недоступные юниты
        # бюджета не тратили, и обход шёл по всем кандидатам подряд.
        self.links["PVM-1"] = [{"unit": "PVM-404", "relation": "x"},
                               {"unit": "PVM-405", "relation": "x"},
                               {"unit": "PVM-2", "relation": "x"}]
        payload = ctx.collect(args("PVM-1", "--max-linked", "2", "--base-url", BASE), {})
        self.assertEqual(self.fetched, ["PVM-1", "PVM-404", "PVM-405"])
        self.assertEqual(payload["linked"], [])
        self.assertEqual([row["code"] for row in payload["skipped"]], ["PVM-404", "PVM-405", "PVM-2"])

    def test_auth_failure_is_never_a_per_item_problem(self):
        # 401/403 кладёт все каналы разом: «связей нет» тогда факт о токене, а
        # не о задаче, и глотать его нельзя — правило семейства.
        def unauthorized(*a, **kw):
            raise RuntimeError("HTTP 401 for https://example/unit: redacted")

        ctx.fetch_tracker_links = unauthorized
        with self.assertRaises(RuntimeError):
            ctx.collect(args("PVM-1", "--base-url", BASE), {})

    def test_unread_links_are_not_reported_as_absent_links(self):
        self.link_ok = False
        self.links = {}
        self.link_notes = ["связи PVM-1 не прочитаны: HTTP 500"]
        payload = ctx.collect(args("PVM-1", "--base-url", BASE), {})
        self.assertTrue(payload["linksUnread"])
        text = ctx.render_markdown(payload, 4000)
        self.assertIn("НЕ значит, что их нет у задачи", text)
        self.assertNotIn("Связанных страниц не найдено", text)

    def test_no_mentions_also_stops_links_to_unit_cards(self):
        self.links = {}
        self.graph["PVM-1"] = source("PVM-1", "Корень",
                                     "см. %s/units/all/unit/PVM-2" % BASE)
        payload = ctx.collect(args("PVM-1", "--no-mentions", "--base-url", BASE), {})
        self.assertEqual(payload["linked"], [])
        self.assertEqual(self.fetched, ["PVM-1"])

    def test_external_links_are_collected_from_linked_pages_too(self):
        # Ссылка на Confluence чаще стоит в связанной странице, чем в самой
        # задаче, а глубже корня обход по умолчанию не идёт.
        self.links = {"PVM-1": [{"unit": "PVM-2", "relation": "уточняется в"}]}
        self.graph["PVM-2"] = source("PVM-2", "Связанная",
                                     "Согласование: https://sberworks.ru/wiki/display/PVM/Page")
        payload = ctx.collect(args("PVM-1", "--base-url", BASE), {})
        self.assertEqual([x["url"] for x in payload["external"]],
                         ["https://sberworks.ru/wiki/display/PVM/Page"])
        self.assertEqual(payload["external"][0]["system"], "confluence")

    def test_output_really_feeds_openspec_seed_as_a_source_file(self):
        # Сценарий 26 обещает этот стык буквально командой из двух строк, а
        # прежний тест проверял ключи словаря, который сам же и построил.
        # Здесь вывод уезжает в файл и разбирается настоящим `openspec_seed`.
        import openspec_seed

        self.graph["PVM-1"] = source(
            "PVM-1", "Добавить срок действия счёта", "Счета живут вечно.",
            attributes=[attribute("Состав работ", "Поле expiresAt и фильтрация в выдаче."),
                        attribute("Критерии приёмки", "Просроченный счёт не в выдаче.", code="a2")])
        payload = ctx.collect(args("PVM-1", "--base-url", BASE), {})

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
            path = fh.name
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = openspec_seed.main(["--source-file", path])
            seeded = json.loads(buf.getvalue())
        finally:
            Path(path).unlink()

        self.assertEqual(rc, 0, seeded)
        self.assertTrue(seeded["ok"], seeded)
        self.assertEqual(seeded["source"]["code"], "PVM-1")
        self.assertIn("Добавить срок действия счёта", json.dumps(seeded, ensure_ascii=False))


class MarkdownTest(unittest.TestCase):
    def payload(self, **over):
        base = {
            "root": {"code": "PVM-1", "summary": "Тема", "url": "%s/units/all/unit/PVM-1" % BASE,
                     "space": "PVM", "suit": "task", "fetchedAt": "2026-08-31T10:00:00+00:00"},
            "source": source("PVM-1", "Тема", "Описание корня.",
                             attributes=[attribute("Состав работ", "Сделать."),
                                         attribute("Пусто", "", code="b")]),
            "links": [], "linked": [], "external": [], "skipped": [], "notes": [], "comments": [],
        }
        base.update(over)
        return base

    def test_empty_description_is_named_not_filled_in(self):
        text = ctx.render_markdown(self.payload(source=source("PVM-1", "Тема", "")), 4000)
        self.assertIn("Описание пустое", text)

    def test_empty_attribute_values_are_not_printed(self):
        text = ctx.render_markdown(self.payload(), 4000)
        self.assertIn("**Состав работ**: Сделать.", text)
        self.assertNotIn("**Пусто**", text)

    def test_absence_of_links_is_stated_explicitly(self):
        self.assertIn("Связанных страниц не найдено", ctx.render_markdown(self.payload(), 4000))

    def test_linked_page_carries_its_relation_and_origin(self):
        linked = [{"code": "PVM-2", "origin": "mention", "relation": "упомянута в тексте",
                   "summary": "Вики", "url": "%s/units/all/unit/PVM-2" % BASE,
                   "source": source("PVM-2", "Вики", "Тело страницы.")}]
        text = ctx.render_markdown(self.payload(linked=linked), 4000)
        self.assertIn("### PVM-2 — Вики", text)
        self.assertIn("- Связь: упомянута в тексте", text)
        self.assertNotIn("(упоминание в тексте)", text)
        self.assertIn("Тело страницы.", text)

    def test_comments_are_rendered_with_their_author(self):
        rows = [{"text": "Уточнение по сроку.", "createdBy": "Автор", "createdAt": "2026-08-31"}]
        text = ctx.render_markdown(self.payload(comments=rows), 4000)
        self.assertIn("**Комментарии (1):**", text)
        self.assertIn("Автор, 2026-08-31: Уточнение по сроку.", text)

    def test_space_and_suit_arrive_as_dicts_and_are_printed_as_names(self):
        root = dict(self.payload()["root"], space={"code": "CRPV", "name": "CRPV"},
                    suit={"code": "epic_crpv", "name": "Epic", "icon": "idea"})
        text = ctx.render_markdown(self.payload(root=root), 4000)
        self.assertIn("- Пространство / тип: CRPV / Epic", text)
        self.assertNotIn("'code'", text)

    def test_wiki_echo_of_the_unit_card_is_not_printed_twice(self):
        # Плагин вики отдаёт эхо карточки юнита: без отсева описание попадает в
        # пакет и как «Постановка», и как «Тело вики».
        # Эхо приезжает под путями `…info.summary` / `…info.description` — так его
        # отдаёт плагин вики на живых юнитах, и так его опознаёт предикат.
        src = source("PVM-1", "Тема", "Единственное описание.")
        src["wikiTexts"] = [{"path": "wiki-v2.info.description", "text": "Единственное описание."},
                            {"path": "wiki-v2.info.summary", "text": "Тема"}]
        text = ctx.render_markdown(self.payload(source=src), 4000)
        self.assertEqual(text.count("Единственное описание."), 1)
        self.assertNotIn("**Тело вики:**", text)

    def test_multiline_attribute_stays_a_readable_block(self):
        # Источник отдаёт многострочный текст (развёрнутый из ProseMirror), и в
        # одну строку списка он не лезет.
        src = source("PVM-1", "Тема", "Описание.",
                     attributes=[attribute("Состав работ", "Первый пункт.\nВторой пункт.")])
        text = ctx.render_markdown(self.payload(source=src), 4000)
        self.assertIn("- **Состав работ**:", text)
        self.assertIn("  Первый пункт.", text)
        self.assertIn("  Второй пункт.", text)

    def test_external_links_are_listed_as_unread_with_their_command(self):
        external = [{"url": "https://sberworks.ru/wiki/x", "system": "confluence",
                     "readCommand": "python3 $SKILL_DIR/scripts/confluence_tool.py get 'x' --confirm-sberworks"}]
        text = ctx.render_markdown(self.payload(external=external), 4000)
        self.assertIn("НЕ прочитаны", text)
        self.assertIn("--confirm-sberworks", text)

    def test_clipping_says_where_the_full_text_is(self):
        long_text = "я" * 500
        text = ctx.render_markdown(self.payload(source=source("PVM-1", "Тема", long_text)), 100)
        self.assertIn("срезано", text)
        self.assertIn("source.description", text)


if __name__ == "__main__":
    unittest.main()
