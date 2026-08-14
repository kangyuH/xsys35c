#!/usr/bin/env python3
"""Build a source-language glossary from decompiled System 3.x ADV files.

The extractor deliberately separates ID-backed entity tables from heuristically
mined terms.  Its output is a review queue, not a claim that every candidate is
a proper noun.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path


MS_RE = re.compile(r"^\s*MS\s+(\d+)\s*,\s*(.*?):\s*$")
VAR_RE = re.compile(r"^\s*!VAR(154|156|158|160)\s*:\s*(\d+)!\s*$")
PLACEHOLDER_RE = re.compile(r"⟦ADV:\d+⟧")
KATAKANA_RE = re.compile(r"[ァ-ヺ][ァ-ヺー・]{1,19}")
BRACKET_RE = re.compile(r"［([^］]{1,40})］")

TERM_CHARS = r"一-龯々〆ヶァ-ヺー・Ａ-Ｚａ-ｚA-Za-z０-９0-9"
SUFFIXES = (
    "反乱軍", "共和国", "研究所", "士官学校", "魔法学校", "教団本部",
    "王国", "帝国", "軍団", "兵団", "部隊", "教団", "同盟", "本部",
    "司令室", "神殿", "迷宮", "洞窟", "遺跡", "アジト", "都市", "基地",
    "学校", "研究室", "大陸", "軍", "隊", "城", "街", "村", "町", "砦",
    "塔", "森", "山", "湖", "島", "宮", "館", "ビル", "王", "皇帝",
    "大統領", "将軍", "隊長", "司令", "提督", "大臣", "騎士", "魔人",
    "魔王", "女神", "族",
)
SUFFIX_RE = re.compile(
    rf"(?<![{TERM_CHARS}])"
    rf"([{TERM_CHARS}]{{1,18}}(?:の[{TERM_CHARS}]{{1,12}})?(?:{'|'.join(SUFFIXES)}))"
)

# These frequent loanwords are clearly ordinary dialogue/UI vocabulary.  The
# list is intentionally conservative: uncertain words remain review candidates.
KATAKANA_STOPWORDS = {
    "アイテム", "ゲーム", "キャラ", "データ", "テスト", "ベッド", "メイド",
    "パンツ", "ショーツ", "スカート", "ドレス", "プレゼント", "スピード",
    "キス", "エッチ", "ボール", "バック", "カーテン", "バルコニー", "シーツ",
    "コンバート", "ビジョン", "ルート", "ノック", "ハーレム", "スパイ",
    "モンスター", "テント", "ピンク", "グッド", "ダーリン", "ユー", "ミー",
    "ボク", "ワシ", "ガキ", "ブス", "マヌケ", "メシ", "パン", "ズボン",
}

MS10_STOPWORDS = {"魔物", "ならず者", "落ち武者", "砂漠の兵", "某国の民", "日"}
MS8_STOPWORDS = {"土地の名前", "年"}
BRACKET_STOPWORDS = {"装備可能", "装備不可"}

CATEGORY_ORDER = {
    "character": 10,
    "character_role": 20,
    "unit_or_commander": 30,
    "unit_class": 40,
    "location": 50,
    "item": 60,
    "organization": 70,
    "title_or_world_term": 80,
    "character_candidate": 90,
    "facility_candidate": 100,
    "item_candidate": 110,
    "organization_candidate": 120,
    "location_candidate": 130,
    "named_phrase_candidate": 140,
    "katakana_candidate": 150,
}


@dataclass
class Term:
    category: str
    source: str
    confidence: str
    status: str
    origin: str
    reason: str
    entity_ids: set[int] = field(default_factory=set)
    occurrences: int = 0
    source_refs: set[str] = field(default_factory=set)
    examples: list[str] = field(default_factory=list)

    def merge(self, other: "Term") -> None:
        self.entity_ids.update(other.entity_ids)
        self.occurrences += other.occurrences
        self.source_refs.update(other.source_refs)
        for example in other.examples:
            if example not in self.examples and len(self.examples) < 3:
                self.examples.append(example)


def clean_term(value: str) -> str:
    value = PLACEHOLDER_RE.sub("", value)
    value = value.replace("\r", " ").replace("\n", " ")
    return re.sub(r"[ \t\u3000]+", " ", value).strip(" \t\u3000:：")


def stable_id(category: str, source: str) -> str:
    digest = hashlib.sha256(f"{category}\0{source}".encode()).hexdigest()[:12]
    return f"{category}:{digest}"


def add_term(terms: dict[tuple[str, str], Term], term: Term) -> None:
    term.source = clean_term(term.source)
    if not term.source or term.source in {"未発見", "終了", "停止"}:
        return
    key = (term.category, term.source)
    if key in terms:
        terms[key].merge(term)
    else:
        terms[key] = term


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def extract_set_chr(project: Path, terms: dict[tuple[str, str], Term]) -> None:
    path = project / "SET_CHR.ADV"
    pending: dict[int, tuple[str, int]] = {}
    for line_no, line in enumerate(read_lines(path), 1):
        if match := MS_RE.match(line):
            index = int(match.group(1))
            if index in {1, 2, 4, 5}:
                pending[index] = (match.group(2), line_no)
            continue
        match = VAR_RE.match(line)
        if not match:
            continue
        var, entity_id = match.groups()
        entity_id = int(entity_id)
        if var == "154" and 1 in pending:
            fields = ((1, "character", "main character name"),
                      (2, "character_role", "main character role/class"))
        elif var == "156" and 4 in pending:
            fields = ((4, "unit_or_commander", "unit/commander display name"),
                      (5, "unit_class", "unit class or affiliation"))
        else:
            continue
        for index, category, reason in fields:
            if index not in pending:
                continue
            value, source_line = pending[index]
            add_term(terms, Term(
                category, value, "high", "extracted", "id_table", reason,
                {entity_id}, 0, {f"SET_CHR.ADV:{source_line}"}, []
            ))
        pending.clear()


def extract_id_table(
    project: Path,
    filename: str,
    ms_index: int,
    var_number: str,
    category: str,
) -> list[Term]:
    result: list[Term] = []
    entity_id: int | None = None
    for line_no, line in enumerate(read_lines(project / filename), 1):
        if match := VAR_RE.match(line):
            if match.group(1) == var_number:
                entity_id = int(match.group(2))
            continue
        match = MS_RE.match(line)
        if match and int(match.group(1)) == ms_index and entity_id is not None:
            result.append(Term(
                category, match.group(2), "high", "extracted", "id_table",
                f"ID-backed {category}", {entity_id}, 0,
                {f"{filename}:{line_no}"}, []
            ))
            entity_id = None
    return result


def classify_ms10(value: str) -> str:
    if re.search(r"(?:軍|部隊|隊|教団|国)$", value):
        return "organization"
    return "title_or_world_term"


def extract_structured_supplements(
    project: Path, terms: dict[tuple[str, str], Term]
) -> None:
    """Extract useful runtime strings outside the three authoritative tables."""
    for path in sorted(project.glob("*.ADV"), key=lambda p: p.name):
        for line_no, line in enumerate(read_lines(path), 1):
            match = MS_RE.match(line)
            if not match:
                continue
            index, value = int(match.group(1)), clean_term(match.group(2))
            if not value:
                continue
            if index == 8 and path.name != "SET_MAP.ADV":
                if value in MS8_STOPWORDS:
                    continue
                add_term(terms, Term(
                    "location", value, "medium", "candidate", "runtime_string",
                    "location slot MS 8 outside SET_MAP", set(), 1,
                    {f"{path.name}:{line_no}"}, []
                ))
            elif index == 10:
                if value in MS10_STOPWORDS:
                    continue
                add_term(terms, Term(
                    classify_ms10(value), value, "medium", "candidate",
                    "runtime_string", "battle/faction display slot MS 10", set(), 1,
                    {f"{path.name}:{line_no}"}, []
                ))


def extract_character_filenames(
    project: Path, terms: dict[tuple[str, str], Term], known_sources: set[str]
) -> None:
    for path in sorted(project.glob("G*.ADV"), key=lambda p: p.name):
        stem = unicodedata.normalize("NFKC", path.stem[1:])
        stem = re.sub(r"\d+$", "", stem)
        if stem in {"", "AL", "DX"} or stem.isascii():
            continue
        if stem in known_sources:
            continue
        add_term(terms, Term(
            "character_candidate", stem, "medium", "candidate", "character_file",
            "G-prefixed scenario filename", set(), 1, {path.name}, []
        ))


def classify_suffix(value: str) -> str:
    if re.search(r"(?:王|皇帝|大統領|将軍|隊長|司令|提督|大臣|騎士|魔人|魔王|女神|族)$", value):
        return "title_or_world_term"
    if re.search(r"(?:軍|軍団|兵団|部隊|隊|教団|同盟|本部|王国|帝国|共和国)$", value):
        return "organization_candidate"
    if re.search(r"(?:研究所|学校|研究室|基地|ビル)$", value):
        return "facility_candidate"
    if re.search(r"(?:城|街|村|町|砦|塔|神殿|迷宮|洞窟|遺跡|アジト|都市|大陸|森|山|湖|島|宮|館)$", value):
        return "location_candidate"
    return "named_phrase_candidate"


def classify_bracket(value: str, context: str) -> str:
    suffix_category = classify_suffix(value)
    if suffix_category != "named_phrase_candidate":
        return suffix_category
    if "建設期間" in context or "建設費" in context:
        return "facility_candidate"
    if any(marker in context for marker in ("価格", "特価", "装備", "万ＧＯＬＤ")):
        return "item_candidate"
    return classify_suffix(value)


def plausible_bracket_term(value: str, category: str, context: str) -> bool:
    """Reject bracketed dialogue, counters and debug/UI formatting."""
    if (not value or value in BRACKET_STOPWORDS or len(value) > 24
            or re.search(r"[。！？!?：:]", value)):
        return False
    if re.fullmatch(r"[\d０-９\s\-－/／。、・]+", value):
        return False
    if "－－" in value or value.startswith(("/", "／", "￥")):
        return False
    if re.search(r"\s", value):
        return False
    if category in {"item_candidate", "facility_candidate", "organization_candidate",
                    "location_candidate", "title_or_world_term"}:
        return True
    # Unclassified bracketed strings are retained only when they look like a
    # compact name, not a quoted Japanese sentence or command.
    return not re.search(r"[ぁ-ゖ]", value)


def mine_catalog(
    catalog: Path,
    terms: dict[tuple[str, str], Term],
    known_sources: set[str],
) -> None:
    suffix_hits: dict[tuple[str, str], Term] = {}
    katakana_counts: Counter[str] = Counter()
    katakana_refs: defaultdict[str, set[str]] = defaultdict(set)
    katakana_examples: defaultdict[str, list[str]] = defaultdict(list)

    with catalog.open(encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            source = record.get("source", "")
            filename = record.get("file", "")
            clean_source = PLACEHOLDER_RE.sub(" ", source)
            # ADV display lines often split a katakana name at a fixed-width
            # line boundary.  Rejoin only katakana-to-katakana whitespace.
            clean_source = re.sub(r"(?<=[ァ-ヺー])\s+(?=[ァ-ヺー])", "", clean_source)
            example = clean_term(clean_source)[:120]

            for match in BRACKET_RE.finditer(clean_source):
                value = clean_term(match.group(1))
                if not value or "ADV:" in value or value in known_sources:
                    continue
                category = classify_bracket(value, clean_source)
                if not plausible_bracket_term(value, category, clean_source):
                    continue
                add_term(terms, Term(
                    category, value,
                    "medium" if category != "named_phrase_candidate" else "low",
                    "candidate", "bracketed_text",
                    "explicit full-width square-bracket term", set(), 1,
                    {filename}, [example]
                ))

            for match in SUFFIX_RE.finditer(clean_source):
                value = clean_term(match.group(1))
                if value in known_sources or len(value) < 2:
                    continue
                category = classify_suffix(value)
                key = (category, value)
                hit = Term(
                    category, value, "low", "candidate", "suffix_mining",
                    "short noun phrase with a proper-term suffix", set(), 1,
                    {filename}, [example]
                )
                if key in suffix_hits:
                    suffix_hits[key].merge(hit)
                else:
                    suffix_hits[key] = hit

            for value in KATAKANA_RE.findall(clean_source):
                if value in known_sources or value in KATAKANA_STOPWORDS:
                    continue
                katakana_counts[value] += 1
                katakana_refs[value].add(filename)
                if example not in katakana_examples[value] and len(katakana_examples[value]) < 3:
                    katakana_examples[value].append(example)

    # Suffix phrases require repetition unless another extractor already found
    # the same category/source (for example a bracketed facility name).
    for key, term in suffix_hits.items():
        if term.occurrences >= 2 or key in terms:
            add_term(terms, term)

    # Katakana-only tokens are deliberately the lowest-confidence queue.
    for value, count in katakana_counts.items():
        if count < 3:
            continue
        add_term(terms, Term(
            "katakana_candidate", value, "low", "candidate", "frequency_mining",
            "katakana token occurring at least three times", set(), count,
            katakana_refs[value], katakana_examples[value]
        ))


def write_outputs(output_dir: Path, terms: dict[tuple[str, str], Term]) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = sorted(
        terms.values(),
        key=lambda t: (CATEGORY_ORDER.get(t.category, 999),
                       unicodedata.normalize("NFKC", t.source), t.source),
    )

    jsonl_path = output_dir / "glossary-source.jsonl"
    csv_path = output_dir / "glossary-source.csv"
    fields = [
        "id", "category", "source", "confidence", "status", "origin", "reason",
        "entity_ids", "occurrences", "file_count", "source_refs", "examples",
    ]

    def serialize(term: Term) -> dict:
        return {
            "id": stable_id(term.category, term.source),
            "category": term.category,
            "source": term.source,
            "confidence": term.confidence,
            "status": term.status,
            "origin": term.origin,
            "reason": term.reason,
            "entity_ids": sorted(term.entity_ids),
            "occurrences": term.occurrences,
            "file_count": len({ref.split(":", 1)[0] for ref in term.source_refs}),
            "source_refs": sorted(term.source_refs),
            "examples": term.examples,
        }

    serialized = [serialize(term) for term in rows]
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in serialized:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=False) + "\n")
    def write_csv(path: Path, selected: list[dict]) -> None:
        # UTF-8 BOM lets desktop Excel recognize Japanese text without an
        # import wizard; JSONL remains plain UTF-8.
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in selected:
                writer.writerow({
                    **row,
                    "entity_ids": ",".join(map(str, row["entity_ids"])),
                    "source_refs": " | ".join(row["source_refs"]),
                    "examples": " | ".join(row["examples"]),
                })

    write_csv(csv_path, serialized)
    seed_path = output_dir / "glossary-seed.csv"
    candidates_path = output_dir / "review-candidates.csv"
    seed = [row for row in serialized if row["confidence"] in {"high", "medium"}]
    candidates = [row for row in serialized if row["status"] == "candidate"]
    write_csv(seed_path, seed)
    write_csv(candidates_path, candidates)

    category_dir = output_dir / "categories"
    category_dir.mkdir(exist_ok=True)
    by_category: defaultdict[str, list[dict]] = defaultdict(list)
    for row in serialized:
        by_category[row["category"]].append(row)
    for category, category_rows in by_category.items():
        write_csv(category_dir / f"{category}.csv", category_rows)

    report = {
        "total": len(serialized),
        "by_category": dict(sorted(Counter(r["category"] for r in serialized).items())),
        "by_confidence": dict(sorted(Counter(r["confidence"] for r in serialized).items())),
        "by_status": dict(sorted(Counter(r["status"] for r in serialized).items())),
        "outputs": {
            "jsonl": jsonl_path.name,
            "csv": csv_path.name,
            "seed_csv": seed_path.name,
            "candidate_csv": candidates_path.name,
            "category_directory": category_dir.name,
        },
    }
    (output_dir / "extraction-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def build_glossary(project: Path, catalog: Path, output_dir: Path) -> dict:
    terms: dict[tuple[str, str], Term] = {}
    extract_set_chr(project, terms)
    for term in extract_id_table(project, "SET_MAP.ADV", 8, "158", "location"):
        add_term(terms, term)
    for term in extract_id_table(project, "SET_ITEM.ADV", 7, "160", "item"):
        add_term(terms, term)
    extract_structured_supplements(project, terms)
    known_sources = {source for _, source in terms}
    extract_character_filenames(project, terms, known_sources)
    known_sources = {source for _, source in terms}
    mine_catalog(catalog, terms, known_sources)
    return write_outputs(output_dir, terms)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path, help="decompiled Japanese ADV directory")
    parser.add_argument("--catalog", required=True, type=Path,
                        help="UTF-8 translations.jsonl produced by adv_text.py")
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    for required in ("SET_CHR.ADV", "SET_MAP.ADV", "SET_ITEM.ADV"):
        if not (args.project / required).is_file():
            parser.error(f"missing required file: {args.project / required}")
    if not args.catalog.is_file():
        parser.error(f"catalog does not exist: {args.catalog}")
    report = build_glossary(args.project, args.catalog, args.output_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
