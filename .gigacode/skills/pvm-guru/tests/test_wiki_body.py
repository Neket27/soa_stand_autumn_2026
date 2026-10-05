"""Markdown → тело wiki-страницы (ProseMirror-документ редактора SWTR).

Схема снята с 54 живых страниц пространства PVM: редактор пишет `paragraph` и
`heading` с `attrs {id, indent, textAlign}`, списки и ячейки таблиц — с `id`,
марки называются `bold`/`italic`/`strike`/`code`/`link`. Конвертер обязан давать
ровно такие узлы: трекер принимает ЛЮБОЙ JSON (даже несуществующий тип узла) и
проверяет только то, что это JSON, поэтому «ответ 200» ничего не доказывает —
доказывает совпадение со схемой редактора.
"""

import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import wiki_body  # noqa: E402


def nodes(doc):
    return doc["content"]


def types(doc):
    return [node["type"] for node in doc["content"]]


def plain(node):
    """Текст узла без разметки — как считает трекер в valueAsString."""
    if node.get("type") == "text":
        return node.get("text", "")
    return "".join(plain(child) for child in node.get("content") or [])


class DocumentShape(unittest.TestCase):
    def test_result_is_a_prosemirror_document(self):
        doc = wiki_body.markdown_to_doc("просто строка")
        self.assertEqual(doc["type"], "doc")
        self.assertIsInstance(doc["content"], list)

    def test_paragraph_carries_the_editor_attributes(self):
        doc = wiki_body.markdown_to_doc("просто строка")
        (paragraph,) = nodes(doc)
        self.assertEqual(paragraph["type"], "paragraph")
        self.assertEqual(paragraph["attrs"]["indent"], 0)
        self.assertEqual(paragraph["attrs"]["textAlign"], "justify")
        self.assertRegex(paragraph["attrs"]["id"], r"^[0-9a-f-]{36}$")

    def test_every_node_gets_its_own_id(self):
        doc = wiki_body.markdown_to_doc("первый\n\nвторой")
        first, second = nodes(doc)
        self.assertNotEqual(first["attrs"]["id"], second["attrs"]["id"])

    def test_blank_lines_separate_paragraphs(self):
        doc = wiki_body.markdown_to_doc("первый\n\nвторой")
        self.assertEqual(types(doc), ["paragraph", "paragraph"])
        self.assertEqual([plain(n) for n in nodes(doc)], ["первый", "второй"])

    def test_single_newline_becomes_a_hard_break(self):
        doc = wiki_body.markdown_to_doc("строка\nещё строка")
        (paragraph,) = nodes(doc)
        self.assertEqual([c["type"] for c in paragraph["content"]],
                         ["text", "hardBreak", "text"])

    def test_empty_markdown_gives_an_empty_document(self):
        self.assertEqual(wiki_body.markdown_to_doc("   \n\n  "),
                         {"type": "doc", "content": []})


class Headings(unittest.TestCase):
    def test_hash_becomes_a_heading_with_its_level(self):
        doc = wiki_body.markdown_to_doc("## Второй уровень")
        (heading,) = nodes(doc)
        self.assertEqual(heading["type"], "heading")
        self.assertEqual(heading["attrs"]["level"], 2)
        self.assertEqual(plain(heading), "Второй уровень")

    def test_all_six_levels_are_supported(self):
        doc = wiki_body.markdown_to_doc("\n\n".join(f"{'#' * n} з{n}" for n in range(1, 7)))
        self.assertEqual([h["attrs"]["level"] for h in nodes(doc)], [1, 2, 3, 4, 5, 6])

    def test_seventh_level_is_not_a_heading(self):
        doc = wiki_body.markdown_to_doc("####### не заголовок")
        self.assertEqual(types(doc), ["paragraph"])

    def test_heading_keeps_the_editor_attributes(self):
        (heading,) = nodes(wiki_body.markdown_to_doc("# Заголовок"))
        self.assertEqual(heading["attrs"]["indent"], 0)
        self.assertEqual(heading["attrs"]["textAlign"], "justify")


class Lists(unittest.TestCase):
    def test_dash_becomes_a_bullet_list(self):
        doc = wiki_body.markdown_to_doc("- раз\n- два")
        (bullet,) = nodes(doc)
        self.assertEqual(bullet["type"], "bulletList")
        self.assertEqual([i["type"] for i in bullet["content"]], ["listItem", "listItem"])

    def test_star_and_plus_are_bullets_too(self):
        self.assertEqual(types(wiki_body.markdown_to_doc("* раз")), ["bulletList"])
        self.assertEqual(types(wiki_body.markdown_to_doc("+ раз")), ["bulletList"])

    def test_list_item_wraps_its_text_in_a_paragraph(self):
        (bullet,) = nodes(wiki_body.markdown_to_doc("- раз"))
        (item,) = bullet["content"]
        self.assertEqual([c["type"] for c in item["content"]], ["paragraph"])
        self.assertEqual(plain(item), "раз")

    def test_numbered_lines_become_an_ordered_list(self):
        doc = wiki_body.markdown_to_doc("1. раз\n2. два")
        (ordered,) = nodes(doc)
        self.assertEqual(ordered["type"], "orderedList")
        self.assertEqual(ordered["attrs"]["start"], 1)
        self.assertEqual([plain(i) for i in ordered["content"]], ["раз", "два"])

    def test_ordered_list_keeps_its_first_number_as_start(self):
        (ordered,) = nodes(wiki_body.markdown_to_doc("5. пятый\n6. шестой"))
        self.assertEqual(ordered["attrs"]["start"], 5)

    def test_checkbox_lines_become_a_task_list(self):
        doc = wiki_body.markdown_to_doc("- [ ] сделать\n- [x] сделано")
        (tasks,) = nodes(doc)
        self.assertEqual(tasks["type"], "taskList")
        self.assertEqual([i["attrs"]["checked"] for i in tasks["content"]], [False, True])
        self.assertEqual([plain(i) for i in tasks["content"]], ["сделать", "сделано"])

    def test_list_and_paragraph_do_not_merge(self):
        doc = wiki_body.markdown_to_doc("вступление\n\n- пункт\n\nитог")
        self.assertEqual(types(doc), ["paragraph", "bulletList", "paragraph"])

    def test_every_list_node_has_an_id(self):
        (bullet,) = nodes(wiki_body.markdown_to_doc("- раз"))
        self.assertRegex(bullet["attrs"]["id"], r"^[0-9a-f-]{36}$")
        self.assertRegex(bullet["content"][0]["attrs"]["id"], r"^[0-9a-f-]{36}$")


class CodeBlocks(unittest.TestCase):
    def test_fenced_block_becomes_a_code_block(self):
        doc = wiki_body.markdown_to_doc("```bash\necho hi\n```")
        (code,) = nodes(doc)
        self.assertEqual(code["type"], "codeBlock")
        self.assertEqual(code["attrs"]["language"], "bash")
        self.assertEqual(plain(code), "echo hi")

    def test_fence_without_language_leaves_it_empty(self):
        (code,) = nodes(wiki_body.markdown_to_doc("```\nтекст\n```"))
        self.assertIsNone(code["attrs"]["language"])

    def test_code_keeps_its_line_breaks_verbatim(self):
        (code,) = nodes(wiki_body.markdown_to_doc("```\nпервая\nвторая\n```"))
        self.assertEqual(plain(code), "первая\nвторая")
        self.assertNotIn("hardBreak", json.dumps(code))

    def test_blank_line_inside_a_fence_does_not_split_the_block(self):
        (code,) = nodes(wiki_body.markdown_to_doc("```\nпервая\n\nвторая\n```"))
        self.assertEqual(plain(code), "первая\n\nвторая")

    def test_markup_inside_code_is_not_interpreted(self):
        (code,) = nodes(wiki_body.markdown_to_doc("```\n**не жирный**\n```"))
        self.assertEqual(plain(code), "**не жирный**")


class InlineMarks(unittest.TestCase):
    def marks_of(self, markdown):
        (paragraph,) = nodes(wiki_body.markdown_to_doc(markdown))
        return [(part.get("text"), [m["type"] for m in part.get("marks") or []])
                for part in paragraph["content"]]

    def test_double_star_is_bold(self):
        self.assertEqual(self.marks_of("**жирный** обычный"),
                         [("жирный", ["bold"]), (" обычный", [])])

    def test_single_star_and_underscore_are_italic(self):
        self.assertEqual(self.marks_of("*курсив*"), [("курсив", ["italic"])])
        self.assertEqual(self.marks_of("_курсив_"), [("курсив", ["italic"])])

    def test_tilde_is_strike(self):
        self.assertEqual(self.marks_of("~~убрано~~"), [("убрано", ["strike"])])

    def test_backticks_are_inline_code(self):
        self.assertEqual(self.marks_of("`swtr_task_tool.py`"),
                         [("swtr_task_tool.py", ["code"])])

    def test_markup_inside_inline_code_stays_literal(self):
        self.assertEqual(self.marks_of("`**звёздочки**`"),
                         [("**звёздочки**", ["code"])])

    def test_link_gets_the_editor_attributes(self):
        (paragraph,) = nodes(wiki_body.markdown_to_doc("[задача](https://example.test/PVM-1)"))
        (part,) = paragraph["content"]
        self.assertEqual(part["text"], "задача")
        (mark,) = part["marks"]
        self.assertEqual(mark["type"], "link")
        self.assertEqual(mark["attrs"]["href"], "https://example.test/PVM-1")
        self.assertEqual(mark["attrs"]["target"], "_blank")
        self.assertEqual(mark["attrs"]["rel"], "noopener noreferrer nofollow")

    def test_marks_work_inside_headings_and_list_items(self):
        (heading,) = nodes(wiki_body.markdown_to_doc("# **жирный** заголовок"))
        self.assertEqual(heading["content"][0]["marks"][0]["type"], "bold")
        (bullet,) = nodes(wiki_body.markdown_to_doc("- `код` в пункте"))
        item_paragraph = bullet["content"][0]["content"][0]
        self.assertEqual(item_paragraph["content"][0]["marks"][0]["type"], "code")

    def test_unpaired_marker_stays_plain_text(self):
        self.assertEqual(self.marks_of("2 * 2 = 4"), [("2 * 2 = 4", [])])


TABLE = """| Поле | Тип |
|------|-----|
| код | строка |
| срок | дата |"""


class Tables(unittest.TestCase):
    def test_pipe_table_becomes_a_table_node(self):
        (table,) = nodes(wiki_body.markdown_to_doc(TABLE))
        self.assertEqual(table["type"], "table")
        self.assertEqual([r["type"] for r in table["content"]],
                         ["tableRow", "tableRow", "tableRow"])

    def test_separator_line_is_not_a_row(self):
        (table,) = nodes(wiki_body.markdown_to_doc(TABLE))
        texts = [[plain(cell) for cell in row["content"]] for row in table["content"]]
        self.assertEqual(texts, [["Поле", "Тип"], ["код", "строка"], ["срок", "дата"]])

    def test_first_row_is_a_header(self):
        (table,) = nodes(wiki_body.markdown_to_doc(TABLE))
        header, first_body = table["content"][0], table["content"][1]
        self.assertEqual([c["type"] for c in header["content"]],
                         ["tableHeader", "tableHeader"])
        self.assertEqual([c["type"] for c in first_body["content"]],
                         ["tableCell", "tableCell"])
        self.assertTrue(table["attrs"]["hasHeaderRow"])

    def test_cells_wrap_text_in_a_paragraph_and_keep_marks(self):
        (table,) = nodes(wiki_body.markdown_to_doc(
            "| Поле |\n|---|\n| **жирное** |"))
        body_cell = table["content"][1]["content"][0]
        self.assertEqual([c["type"] for c in body_cell["content"]], ["paragraph"])
        mark_types = [m["type"]
                      for m in body_cell["content"][0]["content"][0].get("marks") or []]
        self.assertEqual(mark_types, ["bold"])

    def test_column_widths_match_the_column_count(self):
        (table,) = nodes(wiki_body.markdown_to_doc(TABLE))
        self.assertEqual(len(table["attrs"]["columnWidths"]), 2)

    def test_pipes_without_a_separator_are_not_a_table(self):
        doc = wiki_body.markdown_to_doc("| просто | текст |")
        self.assertEqual(types(doc), ["paragraph"])


class BodyValue(unittest.TestCase):
    """Значение атрибута всегда строка: объект трекер отвергает HTTP 500."""

    def test_markdown_is_converted_and_serialized_to_a_string(self):
        value = wiki_body.body_value("# Заголовок")
        self.assertIsInstance(value, str)
        self.assertEqual(json.loads(value)["content"][0]["type"], "heading")

    def test_ready_prosemirror_json_passes_through(self):
        doc = {"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "готовое"}]}]}
        value = wiki_body.body_value(json.dumps(doc, ensure_ascii=False))
        self.assertEqual(json.loads(value), doc)

    def test_json_that_is_not_a_document_is_treated_as_text(self):
        value = wiki_body.body_value('{"foo": "bar"}')
        self.assertEqual(json.loads(value)["type"], "doc")

    def test_a_dict_is_serialized_rather_than_sent_as_an_object(self):
        doc = {"type": "doc", "content": []}
        self.assertEqual(wiki_body.body_value(doc), json.dumps(doc, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
