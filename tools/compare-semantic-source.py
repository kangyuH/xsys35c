#!/usr/bin/env python3
"""Compare ADV trees while normalizing representation-only Unicode changes.

Unicode mode changes byte lengths, which shifts the address labels emitted by
xsys35dc, and turns explicit ``<0xHHHH>`` SJIS codepoints into the equivalent
Unicode characters. Both are representation-only differences, so they are
normalized before the comparison.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import sys
from pathlib import Path

ADDRESS = re.compile(r"L_([0-9a-fA-F]+)")
SJIS_CODEPOINT = re.compile(r"<0x([0-9a-fA-F]{4})>")


def canonicalize(text: str) -> str:
    def decode_sjis(match: re.Match[str]) -> str:
        value = int(match.group(1), 16)
        return bytes((value >> 8, value & 0xFF)).decode("cp932")

    text = SJIS_CODEPOINT.sub(decode_sjis, text)
    addresses = sorted({int(value, 16) for value in ADDRESS.findall(text)})
    address_ids = {value: index for index, value in enumerate(addresses, 1)}
    return ADDRESS.sub(
        lambda match: f"L_ADDR_{address_ids[int(match.group(1), 16)]:06d}", text
    )


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit(
            "usage: compare-semantic-source.py BASELINE REDECOMPILED REPORTS"
        )
    baseline = Path(sys.argv[1])
    redecompiled = Path(sys.argv[2])
    reports = Path(sys.argv[3])
    baseline_names = sorted(path.name for path in baseline.glob("*.ADV"))
    redecompiled_names = sorted(path.name for path in redecompiled.glob("*.ADV"))
    if baseline_names != redecompiled_names:
        raise SystemExit("ADV filename sets differ")

    raw_changed = 0
    failures: list[str] = []
    hashes: list[str] = []
    diff_lines: list[str] = []
    for name in baseline_names:
        original = (baseline / name).read_text(encoding="utf-8", errors="strict")
        migrated = (redecompiled / name).read_text(encoding="utf-8", errors="strict")
        raw_changed += original != migrated
        original_canonical = canonicalize(original)
        migrated_canonical = canonicalize(migrated)
        digest = hashlib.sha256(original_canonical.encode("utf-8")).hexdigest()
        hashes.append(f"{digest}\t{name}")
        if original_canonical != migrated_canonical:
            failures.append(name)
            diff_lines.extend(
                difflib.unified_diff(
                    original_canonical.splitlines(keepends=True),
                    migrated_canonical.splitlines(keepends=True),
                    fromfile=f"baseline/{name}",
                    tofile=f"redecompiled/{name}",
                )
            )

    reports.mkdir(parents=True, exist_ok=True)
    (reports / "semantic-adv.sha256").write_text(
        "\n".join(hashes) + "\n", encoding="utf-8"
    )
    (reports / "semantic-source.diff").write_text("".join(diff_lines), encoding="utf-8")
    (reports / "semantic-summary.tsv").write_text(
        "metric\tvalue\n"
        f"adv_files\t{len(baseline_names)}\n"
        f"raw_changed_by_address_or_escape_representation\t{raw_changed}\n"
        f"semantic_mismatches\t{len(failures)}\n",
        encoding="utf-8",
    )
    if failures:
        print("Semantic ADV differences: " + ", ".join(failures), file=sys.stderr)
        return 1
    print(f"Semantic ADV comparison: PASS ({len(baseline_names)} pages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
