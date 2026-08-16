#!/usr/bin/env python3
"""Detect glossary violations in ADV overlays. Detection only — never rewrite text."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence


PLACEHOLDER_RE = re.compile(r"⟦ADV:\d+⟧")
DECORATION_RE = re.compile(r"^[○×●◆■□◎〇✕✖✔✅❌]\s*")


@dataclass(frozen=True)
class Term:
    source: str
    target: str
    category: str
    alts: tuple[str, ...]
    forbidden_hints: tuple[str, ...]
    review_status: str
    confidence: str
    risk: bool


@dataclass
class Hit:
    term: Term
    start: int
    end: int


@dataclass
class Finding:
    kind: str
    term_source: str
    detail: str = ""


@dataclass
class RecordResult:
    identifier: str
    catalog: str
    file: str
    record_type: str
    kind: str
    priority: str
    findings: list[Finding] = field(default_factory=list)
    translation: str = ""
    hits: list[str] = field(default_factory=list)
    required: list[dict] = field(default_factory=list)


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{line_number}: {exc}") from exc
    return rows


def load_glossary(path: Path) -> list[Term]:
    with path.open(encoding="utf-8", newline="") as stream:
        raw_rows = list(csv.DictReader(stream, delimiter="\t"))
    sources = [row["source"] for row in raw_rows]
    nested_shorts: set[str] = set()
    by_len = sorted(sources, key=len, reverse=True)
    for i, short in enumerate(by_len):
        for longer in by_len[:i]:
            if short != longer and short in longer:
                nested_shorts.add(short)
                break
    terms: list[Term] = []
    for row in raw_rows:
        source = row["source"]
        alts = tuple(a for a in (row.get("alts") or "").split("|") if a)
        forbidden = tuple(a for a in (row.get("forbidden_hints") or "").split("|") if a)
        terms.append(
            Term(
                source=source,
                target=row["target"],
                category=row.get("category") or "",
                alts=alts,
                forbidden_hints=forbidden,
                review_status=row.get("review_status") or "",
                confidence=row.get("confidence") or "",
                risk=len(source) <= 2 or source in nested_shorts,
            )
        )
    return terms


def semantic_text(value: str) -> str:
    return PLACEHOLDER_RE.sub("", value)


def char_script(ch: str) -> str | None:
    code = ord(ch)
    if ch in {"ー", "ｰ", "ﾞ", "ﾟ"} or 0x30A0 <= code <= 0x30FF or 0xFF66 <= code <= 0xFF9D:
        return "kata"
    if 0x3040 <= code <= 0x309F:
        return "hira"
    if 0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF:
        return "han"
    if ch.isascii() and ch.isalnum():
        return "latin"
    return None


def has_letter_boundary(source: str, start: int, end: int) -> bool:
    if start > 0:
        left = char_script(source[start - 1])
        here = char_script(source[start])
        if left and here and left == here and left in {"kata", "hira", "latin"}:
            return False
    if end < len(source):
        right = char_script(source[end])
        here = char_script(source[end - 1])
        if right and here and right == here and right in {"kata", "hira", "latin"}:
            return False
    return True


def build_matcher(terms: Sequence[Term]) -> re.Pattern[str]:
    ordered = sorted({term.source for term in terms}, key=len, reverse=True)
    return re.compile("|".join(re.escape(source) for source in ordered))


def find_hits(source: str, matcher: re.Pattern[str], by_source: dict[str, Term]) -> list[Hit]:
    hits: list[Hit] = []
    for match in matcher.finditer(source):
        if not has_letter_boundary(source, match.start(), match.end()):
            continue
        hits.append(Hit(term=by_source[match.group(0)], start=match.start(), end=match.end()))
    return hits


def acceptable_zh(term: Term) -> tuple[str, ...]:
    values = [term.target, *term.alts]
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return tuple(out)


def contains_any(text: str, needles: Sequence[str]) -> list[str]:
    return [needle for needle in needles if needle and needle in text]


def is_hard_record(record: dict) -> bool:
    file_name = record.get("file") or ""
    record_type = record.get("type") or ""
    syntax = record.get("syntax") or {}
    command = syntax.get("command") or ""
    if file_name in {"SET_CHR.ADV", "SET_MAP.ADV", "SET_ITEM.ADV"}:
        return True
    if record_type in {"menu", "display-string"}:
        return True
    if command == "MS":
        return True
    return False


def classify_record(record: dict, translation: str, hits: Sequence[Hit]) -> RecordResult:
    result = RecordResult(
        identifier=record["id"],
        catalog=record.get("catalog") or "main",
        file=record.get("file") or "",
        record_type=record.get("type") or "",
        kind="NO_HIT",
        priority="none",
        translation=translation,
        hits=[hit.term.source for hit in hits],
    )
    if not hits:
        return result

    missing: list[Term] = []
    wrong_hint: list[tuple[Term, str]] = []
    alt_only: list[str] = []
    ok_terms: list[str] = []

    for hit in hits:
        term = hit.term
        acceptable = acceptable_zh(term)
        present = contains_any(translation, acceptable)
        olds = contains_any(translation, term.forbidden_hints)
        if present:
            if term.target in present:
                ok_terms.append(term.source)
            else:
                alt_only.append(term.source)
            for old in olds:
                if old not in acceptable:
                    wrong_hint.append((term, old))
            continue
        if olds:
            for old in olds:
                wrong_hint.append((term, old))
            missing.append(term)
            continue
        missing.append(term)

    required = []
    for term in {t.source: t for t in missing}.values():
        required.append(
            {
                "source": term.source,
                "target": term.target,
                "alts": list(term.alts),
                "forbidden_hints": list(term.forbidden_hints),
                "risk": term.risk,
                "category": term.category,
            }
        )
        result.findings.append(Finding("missing_or_wrong", term.source, term.target))
    for term, old in wrong_hint:
        if not any(item["source"] == term.source for item in required):
            required.append(
                {
                    "source": term.source,
                    "target": term.target,
                    "alts": list(term.alts),
                    "forbidden_hints": list(term.forbidden_hints),
                    "risk": term.risk,
                    "category": term.category,
                }
            )
        result.findings.append(Finding("forbidden_present", term.source, old))

    result.required = required
    if not required:
        result.kind = "OK_ALT" if alt_only and not missing else "OK"
        return result

    # Any potential textual change must be decided by an LLM — never auto-replaced.
    result.kind = "NEEDS_LLM"
    if is_hard_record(record) or any(not item["risk"] for item in required):
        result.priority = "high"
    elif any(item["risk"] for item in required):
        result.priority = "low"
    else:
        result.priority = "medium"
    return result


def load_joined(
    main_catalog: Path,
    candidate_catalog: Path,
    main_overlay: Path,
    candidate_overlay: Path,
) -> list[dict]:
    main = read_jsonl(main_catalog)
    candidates = read_jsonl(candidate_catalog)
    for row in main:
        row["catalog"] = "main"
    for row in candidates:
        row["catalog"] = "candidates"
    overlays = {
        row["id"]: row for row in read_jsonl(main_overlay) + read_jsonl(candidate_overlay)
    }
    joined: list[dict] = []
    for row in main + candidates:
        overlay = overlays.get(row["id"])
        if overlay is None:
            raise SystemExit(f"missing overlay for {row['id']}")
        if overlay["source_hash"] != row["source_hash"]:
            raise SystemExit(f"source_hash mismatch for {row['id']}")
        joined.append({**row, "translation": overlay["translation"], "overlay": overlay})
    return joined


def run_audit(glossary: Sequence[Term], joined: Sequence[dict]) -> tuple[list[RecordResult], dict]:
    by_source = {term.source: term for term in glossary}
    matcher = build_matcher(glossary)
    results: list[RecordResult] = []
    kinds: Counter[str] = Counter()
    priorities: Counter[str] = Counter()
    term_heat: Counter[str] = Counter()
    for record in joined:
        hits = find_hits(record["source"], matcher, by_source)
        result = classify_record(record, record["translation"], hits)
        results.append(result)
        kinds[result.kind] += 1
        if result.kind == "NEEDS_LLM":
            priorities[result.priority] += 1
            for hit in result.hits:
                term_heat[hit] += 1
    summary = {
        "records": len(joined),
        "kinds": dict(kinds),
        "priorities": dict(priorities),
        "term_heat": term_heat.most_common(50),
        "needs_llm": kinds.get("NEEDS_LLM", 0),
        "policy": "detection-only; every textual fix must be LLM-decided",
    }
    return results, summary


def write_reports(
    report_dir: Path,
    summary: dict,
    results: Sequence[RecordResult],
    sources: dict[str, str],
) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    queue = [row for row in results if row.kind == "NEEDS_LLM"]
    with (report_dir / "queue.tsv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(
            ["id", "priority", "catalog", "file", "type", "hits", "required", "source", "translation"]
        )
        for row in queue:
            writer.writerow(
                [
                    row.identifier,
                    row.priority,
                    row.catalog,
                    row.file,
                    row.record_type,
                    "|".join(row.hits),
                    json.dumps(row.required, ensure_ascii=False),
                    sources.get(row.identifier, "").replace("\t", " "),
                    row.translation.replace("\t", " "),
                ]
            )
    with (report_dir / "queue.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
        for row in queue:
            stream.write(
                json.dumps(
                    {
                        "id": row.identifier,
                        "priority": row.priority,
                        "catalog": row.catalog,
                        "file": row.file,
                        "type": row.record_type,
                        "hits": row.hits,
                        "required": row.required,
                        "source": sources.get(row.identifier, ""),
                        "translation": row.translation,
                        "findings": [
                            {"kind": f.kind, "term_source": f.term_source, "detail": f.detail}
                            for f in row.findings
                        ],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    lines = [
        "# Glossary audit summary (detection only)",
        "",
        f"- records scanned: {summary['records']}",
        f"- kinds: `{json.dumps(summary['kinds'], ensure_ascii=False)}`",
        f"- needs LLM decision: {summary['needs_llm']}",
        f"- priorities: `{json.dumps(summary['priorities'], ensure_ascii=False)}`",
        "",
        "Policy: **no deterministic string replacement**. Every change is deferred to DeepSeek.",
        "",
        "## Top terms needing LLM review",
        "",
    ]
    for term, count in summary["term_heat"][:40]:
        lines.append(f"- `{term}`: {count}")
    (report_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--glossary", type=Path, default=Path("workspace/translation/glossary/selected-v1.tsv")
    )
    parser.add_argument(
        "--main-catalog",
        type=Path,
        default=Path("workspace/translation/catalogs/translations.jsonl"),
    )
    parser.add_argument(
        "--candidate-catalog",
        type=Path,
        default=Path("workspace/translation/catalogs/candidates.jsonl"),
    )
    parser.add_argument(
        "--main-overlay",
        type=Path,
        default=Path("workspace/translation/overlays/adv-v1/main.jsonl"),
    )
    parser.add_argument(
        "--candidate-overlay",
        type=Path,
        default=Path("workspace/translation/overlays/adv-v1/candidates.jsonl"),
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=Path("workspace/translation/reports/glossary-audit"),
    )
    args = parser.parse_args()
    if "--apply" in __import__("sys").argv:
        raise SystemExit(
            "error: --apply deterministic rewrite is disabled; "
            "use tools/glossary_deepseek_fix.py so every change is LLM-decided"
        )

    glossary = load_glossary(args.glossary.resolve())
    joined = load_joined(
        args.main_catalog.resolve(),
        args.candidate_catalog.resolve(),
        args.main_overlay.resolve(),
        args.candidate_overlay.resolve(),
    )
    results, summary = run_audit(glossary, joined)
    sources = {row["id"]: row["source"] for row in joined}
    write_reports(args.report_dir.resolve(), summary, results, sources)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
