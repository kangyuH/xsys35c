#!/usr/bin/env python3
"""Import Sheet3「入选术语」from the research workbook into a locked glossary TSV."""

from __future__ import annotations

import argparse
import csv
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path


NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    out: list[str] = []
    for si in root.findall("m:si", NS):
        texts = [
            t.text or ""
            for t in si.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")
        ]
        out.append("".join(texts))
    return out


def sheet_targets(zf: zipfile.ZipFile) -> dict[str, str]:
    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rid_to_target = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
    sheets: dict[str, str] = {}
    for sh in wb.find("m:sheets", NS):
        name = sh.attrib["name"]
        rid = sh.attrib[f"{REL_NS}id"]
        target = "xl/" + rid_to_target[rid].lstrip("/")
        if target.startswith("xl/xl/"):
            target = target[3:]
        sheets[name] = target
    return sheets


def col_row(cell_ref: str) -> tuple[int, int]:
    match = re.match(r"([A-Z]+)(\d+)", cell_ref)
    if not match:
        raise ValueError(cell_ref)
    col_letters, row = match.group(1), int(match.group(2))
    col = 0
    for ch in col_letters:
        col = col * 26 + (ord(ch) - 64)
    return col, row


def read_sheet(zf: zipfile.ZipFile, target: str, shared: list[str]) -> list[list[str | None]]:
    root = ET.fromstring(zf.read(target))
    rows: dict[int, dict[int, str | None]] = {}
    for cell in root.findall(".//m:c", NS):
        ref = cell.attrib.get("r")
        if not ref:
            continue
        col, row = col_row(ref)
        kind = cell.attrib.get("t")
        value_node = cell.find("m:v", NS)
        inline = cell.find("m:is", NS)
        if kind == "s" and value_node is not None and value_node.text is not None:
            val: str | None = shared[int(value_node.text)]
        elif kind == "inlineStr" and inline is not None:
            texts = [
                t.text or ""
                for t in inline.iter(
                    "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"
                )
            ]
            val = "".join(texts)
        elif value_node is not None:
            val = value_node.text
        else:
            val = None
        rows.setdefault(row, {})[col] = val
    out: list[list[str | None]] = []
    for row in sorted(rows):
        width = max(rows[row])
        out.append([rows[row].get(c) for c in range(1, width + 1)])
    return out


def split_alts(raw: str | None) -> list[str]:
    if not raw:
        return []
    parts = re.split(r"[/／|｜,，;；]+", raw)
    return [part.strip() for part in parts if part and part.strip()]


def load_old_glossary(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        for row in reader:
            source = (row.get("source") or "").strip()
            target = (row.get("target") or "").strip()
            if source and target:
                out[source] = target
    return out


def import_selected(xlsx: Path, old_glossary: Path) -> tuple[list[dict], dict]:
    with zipfile.ZipFile(xlsx) as zf:
        shared = shared_strings(zf)
        sheets = sheet_targets(zf)
        if "入选术语" not in sheets:
            raise SystemExit("sheet「入选术语」not found")
        data = read_sheet(zf, sheets["入选术语"], shared)
    if not data:
        raise SystemExit("empty sheet")
    headers = [h or "" for h in data[0]]
    required = {"日语原文", "中文译名"}
    if not required.issubset(set(headers)):
        raise SystemExit(f"missing columns: {required - set(headers)}")

    old = load_old_glossary(old_glossary)
    rows: list[dict] = []
    seen: set[str] = set()
    for raw in data[1:]:
        record = {
            headers[i]: (raw[i] if i < len(raw) else None) for i in range(len(headers))
        }
        source = (record.get("日语原文") or "").strip()
        target = (record.get("中文译名") or "").strip()
        if not source or not target:
            raise SystemExit(f"empty source/target in row: {record}")
        if source in seen:
            raise SystemExit(f"duplicate source: {source}")
        seen.add(source)
        alts = split_alts(record.get("备选/异译"))
        old_target = old.get(source)
        forbidden = []
        if old_target and old_target != target and old_target not in alts:
            forbidden.append(old_target)
        rows.append(
            {
                "source": source,
                "target": target,
                "category": (record.get("名称类型") or "").strip(),
                "alts": "|".join(alts),
                "review_status": (record.get("复核状态") or "").strip(),
                "confidence": (record.get("翻译置信度") or "").strip(),
                "notes": (record.get("译名备注") or "").strip(),
                "action": "lock",
                "forbidden_hints": "|".join(forbidden),
                "occurrence": (record.get("出现次数") or "").strip(),
                "origin_type": (record.get("译名来源类型") or "").strip(),
            }
        )

    sources = [row["source"] for row in rows]
    nested: list[tuple[str, str]] = []
    by_len = sorted(sources, key=len, reverse=True)
    for i, short in enumerate(by_len):
        for longer in by_len[:i]:
            if short != longer and short in longer:
                nested.append((short, longer))
                break
    short_risk = sorted(
        {
            row["source"]
            for row in rows
            if len(row["source"]) <= 2
            or any(row["source"] == s for s, _ in nested)
        }
    )
    report = {
        "rows": len(rows),
        "unique_targets": len({row["target"] for row in rows}),
        "with_alts": sum(1 for row in rows if row["alts"]),
        "with_forbidden_hints": sum(1 for row in rows if row["forbidden_hints"]),
        "short_or_nested_risk": short_risk,
        "nested_pairs": nested,
        "old_target_diffs": sum(1 for row in rows if row["forbidden_hints"]),
    }
    return rows, report


def write_tsv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "source",
        "target",
        "category",
        "alts",
        "review_status",
        "confidence",
        "notes",
        "action",
        "forbidden_hints",
        "occurrence",
        "origin_type",
    ]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_report(path: Path, report: dict, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# selected-v1 glossary import",
        "",
        f"- rows: {report['rows']}",
        f"- unique Chinese targets: {report['unique_targets']}",
        f"- with alternate spellings: {report['with_alts']}",
        f"- differing from old glossary.tsv: {report['old_target_diffs']}",
        f"- short/nested risk terms: {len(report['short_or_nested_risk'])}",
        "",
        "## Short / nested risk sources",
        "",
    ]
    for source in report["short_or_nested_risk"]:
        lines.append(f"- `{source}`")
    lines.extend(["", "## Nested pairs (shorter inside longer)", ""])
    for short, longer in report["nested_pairs"]:
        lines.append(f"- `{short}` ⊂ `{longer}`")
    lines.extend(["", "## Old-target diffs used as forbidden hints", ""])
    for row in rows:
        if row["forbidden_hints"]:
            lines.append(
                f"- `{row['source']}`: `{row['forbidden_hints']}` → `{row['target']}`"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--xlsx",
        type=Path,
        default=Path("workspace/鬼畜王兰斯_术语筛选与译名调研.xlsx"),
    )
    parser.add_argument(
        "--old-glossary",
        type=Path,
        default=Path("../xsys35c_backup/translations/glossary.tsv"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("workspace/translation/glossary/selected-v1.tsv"),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("workspace/translation/reports/glossary-import.md"),
    )
    args = parser.parse_args()
    rows, report = import_selected(args.xlsx.resolve(), args.old_glossary.resolve())
    write_tsv(args.output.resolve(), rows)
    write_report(args.report.resolve(), report, rows)
    print(
        f"wrote {len(rows)} terms to {args.output}; "
        f"risk={len(report['short_or_nested_risk'])} "
        f"old_diffs={report['old_target_diffs']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
