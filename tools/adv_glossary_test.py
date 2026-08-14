import json
import tempfile
import unittest
from pathlib import Path

import adv_glossary


class GlossaryTest(unittest.TestCase):
    def test_build_classifies_structured_and_mined_terms(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            (project / "SET_CHR.ADV").write_text(
                "MS 1, ランス:\nMS 2, 盗賊:\n!VAR154 : 1!\n"
                "MS 4, エクス:\nMS 5, リーザス兵:\n!VAR156 : 2!\n",
                encoding="utf-8",
            )
            (project / "SET_MAP.ADV").write_text(
                "!VAR158 : 1!\nMS 8, リーザス城:\n", encoding="utf-8"
            )
            (project / "SET_ITEM.ADV").write_text(
                "!VAR160 : 1!\nMS 7, 魔剣　カオス:\n", encoding="utf-8"
            )
            (project / "C.ADV").write_text(
                "MS 8, 雪の迷宮:\nMS 10, ＡＬ教団:\n", encoding="utf-8"
            )
            (project / "Gﾒﾘﾑ.ADV").write_text("", encoding="utf-8")
            catalog = root / "translations.jsonl"
            records = [
                {"file": "C.ADV", "source": "［魔法研究ビル］　建設期間　３ヶ月"},
                {"file": "G.ADV", "source": "アスマーゼ……アスマーゼ……"},
                {"file": "G2.ADV", "source": "アスマーゼ"},
            ]
            catalog.write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
                encoding="utf-8",
            )
            output = root / "output"
            report = adv_glossary.build_glossary(project, catalog, output)
            rows = [
                json.loads(line)
                for line in (output / "glossary-source.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            pairs = {(row["category"], row["source"]) for row in rows}
            self.assertIn(("character", "ランス"), pairs)
            self.assertIn(("unit_or_commander", "エクス"), pairs)
            self.assertIn(("location", "リーザス城"), pairs)
            self.assertIn(("location", "雪の迷宮"), pairs)
            self.assertIn(("item", "魔剣 カオス"), pairs)
            self.assertIn(("organization", "ＡＬ教団"), pairs)
            self.assertIn(("character_candidate", "メリム"), pairs)
            self.assertIn(("facility_candidate", "魔法研究ビル"), pairs)
            self.assertIn(("katakana_candidate", "アスマーゼ"), pairs)
            self.assertEqual(report["total"], len(rows))
            self.assertTrue((output / "glossary-source.csv").is_file())
            self.assertTrue((output / "glossary-seed.csv").is_file())
            self.assertTrue((output / "review-candidates.csv").is_file())

    def test_bracket_filter_rejects_sentences_and_formatting(self):
        self.assertFalse(
            adv_glossary.plausible_bracket_term(
                "１回何でも聞く", "named_phrase_candidate", ""
            )
        )
        self.assertFalse(
            adv_glossary.plausible_bracket_term("－－／後衛", "named_phrase_candidate", "")
        )
        self.assertTrue(
            adv_glossary.plausible_bracket_term(
                "女子士官学校", "facility_candidate", "建設期間"
            )
        )


if __name__ == "__main__":
    unittest.main()
