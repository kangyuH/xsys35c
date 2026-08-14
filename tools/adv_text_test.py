#!/usr/bin/env python3
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

import adv_text


class AdvTextTests(unittest.TestCase):
    def records(self, source):
        return adv_text.parse_adv(source, "fixture.ADV")

    def test_comments_and_escapes(self):
        source = """; 'ignored'\n// \"ignored\"\n/* $bad$text$ */\n'It\\'s \\<ok>'R\n'次'\n"""
        records = self.records(source)
        self.assertEqual(1, len(records))
        self.assertEqual("It's <ok>⟦ADV:1⟧次", records[0]["source"])
        self.assertEqual("R", records[0]["placeholders"][0]["raw"])

    def test_sjis_codepoint_is_locked(self):
        record = self.records("'<0x82a0>文字'A\n")[0]
        self.assertEqual("⟦ADV:1⟧文字⟦ADV:2⟧", record["source"])
        self.assertEqual(["sjis-codepoint", "A"],
                         [item["kind"] for item in record["placeholders"]])
        record["translation"] = record["source"]
        self.assertEqual("'<0x82a0>文字' A", adv_text.render_record(record))

    def test_simple_and_complex_menu(self):
        source = "$L1$日本語$\n$L2$\nH 0, VAR:\n'階より'\n$\n]\n"
        records = self.records(source)
        menus = [record for record in records if record["type"] == "menu"]
        self.assertEqual(["日本語", "⟦ADV:1⟧階より"],
                         [record["source"] for record in menus])
        self.assertEqual("L2", menus[1]["menu_target"])

    def test_control_only_menu_is_not_translatable(self):
        source = "{RND = 0:\n$L1$\n\\L_make_text:\n$\n}\n]\n"
        diagnostics = Counter()
        records = adv_text.parse_adv(source, "fixture.ADV", diagnostics)
        self.assertFalse([record for record in records if record["type"] == "menu"])
        self.assertEqual(1, diagnostics["excluded dynamic menu without static text"])

    def test_inline_variables_and_duplicate_text(self):
        source = "'値は' H 0, VAR: 'です'R\n'同じ'\n'同じ'A\n"
        records = self.records(source)
        messages = [record for record in records if record["type"] == "message-chain"]
        self.assertEqual(1, len(messages))
        self.assertIn("⟦ADV:1⟧", messages[0]["source"])
        self.assertEqual(["H 0, VAR:", "R", "A"],
                         [item["raw"] for item in messages[0]["placeholders"]])

    def test_command_strings_and_double_candidates(self):
        source = "MT ゲーム名:\nMS 1, 主人公:\nLC 0, 0, \"FILE.PMS\":\n\"表の文字\"\n"
        records = self.records(source)
        by_type = {record["type"]: record for record in records}
        self.assertEqual("ゲーム名", by_type["display-string"]["source"])
        self.assertEqual("主人公", by_type["string-argument"]["source"])
        self.assertEqual("表の文字", by_type["string-data"]["source"])
        self.assertNotIn("FILE.PMS", [record["source"] for record in records])

    def test_blank_string_initializer_is_excluded(self):
        diagnostics = Counter()
        records = adv_text.parse_adv("MS 1, 　:\nMS 2, :\n", "fixture.ADV", diagnostics)
        self.assertFalse(records)
        self.assertEqual(2, diagnostics["excluded blank string argument: MS"])

    def test_blank_double_string_data_is_excluded(self):
        diagnostics = Counter()
        records = adv_text.parse_adv('"\u3000\u3000"\n', "fixture.ADV", diagnostics)
        self.assertFalse(records)
        self.assertEqual(1, diagnostics["excluded blank double-quoted string data"])

    def test_stable_ids_for_duplicate_source(self):
        records = self.records("'同じ'A\n'同じ'A\n")
        self.assertEqual(
            ["fixture.ADV:message-chain:000001", "fixture.ADV:message-chain:000002"],
            [record["id"] for record in records],
        )

    def test_apply_and_placeholder_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "xsys35c.cfg").write_text("encoding = utf8\n", encoding="utf-8")
            (project / "a.ADV").write_text("'前'R\n'後'A\n", encoding="utf-8")
            main, _, _ = adv_text.extract_project(project)
            main[0]["translation"] = "先⟦ADV:1⟧後⟦ADV:2⟧"
            catalog = root / "catalog.jsonl"
            adv_text.write_jsonl(catalog, main)
            output = root / "output"
            adv_text.apply_catalog(project, catalog, output, False)
            self.assertEqual("'先' R '後' A\n", (output / "a.ADV").read_text(encoding="utf-8"))

            main[0]["translation"] = "先⟦ADV:2⟧後⟦ADV:1⟧"
            adv_text.write_jsonl(catalog, main)
            with self.assertRaises(adv_text.AdvTextError):
                adv_text.apply_catalog(project, catalog, root / "bad-output", False)

    def test_candidate_requires_explicit_permission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "xsys35c.cfg").write_text("encoding = utf8\n", encoding="utf-8")
            (project / "a.ADV").write_text('"候補"\n', encoding="utf-8")
            _, candidates, _ = adv_text.extract_project(project)
            candidates[0]["translation"] = "翻訳"
            catalog = root / "candidates.jsonl"
            adv_text.write_jsonl(catalog, candidates)
            with self.assertRaises(adv_text.AdvTextError):
                adv_text.apply_catalog(project, catalog, root / "denied", False)
            adv_text.apply_catalog(project, catalog, root / "allowed", True)
            self.assertEqual('"翻訳"\n',
                             (root / "allowed" / "a.ADV").read_text(encoding="utf-8"))

    def test_sjis_codepoint_stays_inside_double_quotes(self):
        record = self.records('"文字<0x82a0>"\n')[0]
        record["translation"] = "翻訳" + record["placeholders"][0]["token"]
        self.assertEqual('"翻訳<0x82a0>"', adv_text.render_record(record))

    def test_cp932_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "xsys35c.cfg").write_text("encoding = sjis\n", encoding="ascii")
            (project / "a.ADV").write_text("'日本語'A\n", encoding="cp932")
            main, _, report = adv_text.extract_project(project)
            self.assertEqual("cp932", report["encoding"])
            self.assertEqual("日本語⟦ADV:1⟧", main[0]["source"])

    def test_noop_apply_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "xsys35c.cfg").write_text("encoding = utf8\n", encoding="utf-8")
            original = "\t'日本語'R\n\t$L$選択$\n"
            (project / "a.ADV").write_text(original, encoding="utf-8")
            main, _, _ = adv_text.extract_project(project)
            catalog = root / "catalog.jsonl"
            adv_text.write_jsonl(catalog, main)
            output = root / "output"
            adv_text.apply_catalog(project, catalog, output, False)
            self.assertEqual((project / "a.ADV").read_bytes(),
                             (output / "a.ADV").read_bytes())


if __name__ == "__main__":
    unittest.main()
