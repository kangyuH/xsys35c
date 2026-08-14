#!/usr/bin/env python3
"""Extract and safely apply translations to System 3.x ADV sources.

The parser is deliberately conservative.  It understands ADV quoting,
escaping, comments, menu delimiters and the small set of commands which can
occur inside a displayed text chain.  Ambiguous string data is emitted to a
separate candidate catalog instead of being silently treated as dialogue.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


PLACEHOLDER_RE = re.compile(r"⟦ADV:(\d+)⟧")
CONTROL_LEAK_RE = re.compile(
    r"@L_[0-9a-f]+|\\L_[0-9a-f]+|![A-Z][A-Z0-9_]*\s*:|%#|&#|\$L_",
    re.IGNORECASE,
)
LABEL_RE = re.compile(r"(?m)^[ \t]*\*([^*\s][^\s$,:;]*)\s*:")
COMMAND_RE = re.compile(r"(?m)^[ \t]*([A-Za-z][A-Za-z0-9]*)\b")

INLINE_COMMANDS = ("HH", "MP", "H", "X")

# command -> (argument index, confidence, reason)
DISPLAY_STRING_ARGS = {
    "MT": (0, "high", "window title"),
    "MI": (2, "high", "string input prompt"),
    "NT": (0, "high", "number input prompt"),
    "strInputDlg": (0, "high", "string input dialog title"),
    "strMessageBox": (0, "high", "message box text"),
    "dlgErrorOkCancel": (0, "high", "error dialog text"),
    "sysAddWebMenu": (0, "high", "web menu caption"),
}

CANDIDATE_STRING_ARGS = {
    "MS": (1, "string variable initializer"),
    "LXG": (1, "file dialog title"),
}

# All string arguments of these commands are structural/resource data.
EXCLUDED_STRING_COMMANDS = {
    "LC", "LE", "QE", "UP", "sysOpenShell", "trace", "LXF"
}


class AdvTextError(Exception):
    pass


@dataclass
class Literal:
    start: int
    end: int
    quote: str
    parts: list[tuple[str, str]]  # ("text" | "sjis", decoded/raw value)


@dataclass
class Menu:
    start: int
    end: int
    label: str
    body_start: int
    body_end: int


@dataclass
class Control:
    start: int
    end: int
    raw: str
    kind: str


@dataclass
class Node:
    start: int
    end: int
    kind: str
    source: str
    confidence: str
    reason: str
    placeholders: list[dict] = field(default_factory=list)
    menu_target: str | None = None
    syntax: dict = field(default_factory=dict)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def mask_range(chars: list[str], start: int, end: int) -> None:
    for i in range(start, end):
        if chars[i] not in "\r\n":
            chars[i] = " "


def parse_literal(source: str, start: int, quote: str) -> Literal:
    parts: list[tuple[str, str]] = []
    text: list[str] = []
    i = start + 1
    while i < len(source):
        c = source[i]
        if c == quote:
            if text:
                parts.append(("text", "".join(text)))
            return Literal(start, i + 1, quote, parts)
        if c == "\\":
            if i + 1 >= len(source):
                raise AdvTextError(f"unfinished escape at character {i}")
            text.append(source[i + 1])
            i += 2
            continue
        if c == "<":
            match = re.match(r"<0[xX][0-9a-fA-F]{1,4}>", source[i:])
            if match:
                if text:
                    parts.append(("text", "".join(text)))
                    text = []
                raw = match.group(0)
                parts.append(("sjis", raw))
                i += len(raw)
                continue
        text.append(c)
        i += 1
    raise AdvTextError(f"unfinished {quote} string at character {start}")


def scan_source(source: str) -> tuple[str, list[Literal], list[int]]:
    """Return code mask, literals, and dollar delimiters.

    Comments and literals are blanked in the mask without changing offsets.
    Dollar signs are collected only in ordinary code state.
    """
    mask = list(source)
    literals: list[Literal] = []
    dollars: list[int] = []
    i = 0
    while i < len(source):
        if source.startswith("//", i):
            end = source.find("\n", i + 2)
            end = len(source) if end < 0 else end
            mask_range(mask, i, end)
            i = end
        elif source[i] == ";":
            end = source.find("\n", i + 1)
            end = len(source) if end < 0 else end
            mask_range(mask, i, end)
            i = end
        elif source.startswith("/*", i):
            end = source.find("*/", i + 2)
            if end < 0:
                raise AdvTextError(f"unfinished block comment at character {i}")
            end += 2
            mask_range(mask, i, end)
            i = end
        elif source[i] in "'\"":
            literal = parse_literal(source, i, source[i])
            literals.append(literal)
            mask_range(mask, literal.start, literal.end)
            i = literal.end
        elif source[i] == "$":
            dollars.append(i)
            i += 1
        else:
            i += 1
    return "".join(mask), literals, dollars


def parse_menus(source: str, code_mask: str, dollars: list[int]) -> list[Menu]:
    menus: list[Menu] = []
    i = 0
    while i < len(dollars):
        if i + 2 >= len(dollars):
            raise AdvTextError(f"unfinished menu beginning at character {dollars[i]}")
        first, second, third = dollars[i:i + 3]
        # A closing menu marker can never cross the enclosing ']' command.
        close_bracket = code_mask.find("]", second + 1, third)
        if close_bracket >= 0:
            raise AdvTextError(f"malformed menu near character {first}")
        label = source[first + 1:second].strip()
        menus.append(Menu(first, third + 1, label, second + 1, third))
        i += 3
    return menus


def literal_text(literal: Literal, add_placeholder) -> str:
    output: list[str] = []
    for kind, value in literal.parts:
        if kind == "text":
            output.append(value)
        else:
            output.append(add_placeholder(value, "sjis-codepoint"))
    return "".join(output)


def skip_space(mask: str, i: int, limit: int) -> int:
    while i < limit and mask[i].isspace():
        i += 1
    return i


def parse_inline_control(source: str, mask: str, start: int, limit: int) -> Control | None:
    i = skip_space(mask, start, limit)
    if i >= limit:
        return None
    if mask[i] in "RA":
        if i + 1 == limit or not (mask[i + 1].isalnum() or mask[i + 1] == "_"):
            return Control(i, i + 1, source[i:i + 1], mask[i])
    for command in INLINE_COMMANDS:
        if not mask.startswith(command, i):
            continue
        after = i + len(command)
        if after < limit and (mask[after].isalnum() or mask[after] == "_"):
            continue
        colon = mask.find(":", after, limit)
        if colon >= 0:
            return Control(i, colon + 1, source[i:colon + 1].strip(), command)
    return None


def placeholder_factory(placeholders: list[dict]):
    def add(raw: str, kind: str) -> str:
        number = len(placeholders) + 1
        token = f"⟦ADV:{number}⟧"
        placeholders.append({
            "token": token,
            "raw": raw,
            "kind": kind,
            "locked": True,
        })
        return token
    return add


def interval_contains(start: int, end: int, intervals: Iterable[tuple[int, int]]) -> bool:
    return any(start >= left and end <= right for left, right in intervals)


def interval_overlaps(start: int, end: int, intervals: Iterable[tuple[int, int]]) -> bool:
    return any(start < right and end > left for left, right in intervals)


def build_menu_node(source: str, mask: str, menu: Menu,
                    singles: list[Literal]) -> Node:
    placeholders: list[dict] = []
    add_placeholder = placeholder_factory(placeholders)
    body_literals = [lit for lit in singles
                     if lit.start >= menu.body_start and lit.end <= menu.body_end]
    by_start = {lit.start: lit for lit in body_literals}
    chunks: list[str] = []
    i = menu.body_start
    raw_body = source[menu.body_start:menu.body_end]
    simple_bare = (not body_literals and "\n" not in raw_body
                   and "\r" not in raw_body
                   and not re.search(r"[{}!@\\%&~\[\]:]", raw_body))
    if simple_bare:
        return Node(
            menu.start, menu.end, "menu", raw_body.strip(), "high",
            "menu display text", placeholders, menu.label,
            {"renderer": "menu", "quoted": False, "label": menu.label},
        )

    while i < menu.body_end:
        literal = by_start.get(i)
        if literal:
            chunks.append(literal_text(literal, add_placeholder))
            i = literal.end
            continue
        control = parse_inline_control(source, mask, i, menu.body_end)
        if control and skip_space(mask, i, menu.body_end) == control.start:
            chunks.append(add_placeholder(control.raw, control.kind))
            i = control.end
            continue
        if mask[i].isspace():
            i += 1
            continue
        # Menu bodies may contain arbitrary ADV statements.  Preserve any
        # statement we do not recognize as an opaque locked placeholder; it
        # is code, not text offered to the translator.
        code_start = i
        i += 1
        while i < menu.body_end:
            if i in by_start:
                break
            next_control = parse_inline_control(source, mask, i, menu.body_end)
            if next_control and skip_space(mask, i, menu.body_end) == next_control.start:
                break
            i += 1
        raw_code = source[code_start:i].strip()
        if raw_code:
            chunks.append(add_placeholder(raw_code, "menu-code"))
    quoted = bool(body_literals)
    return Node(
        menu.start, menu.end, "menu", "".join(chunks), "high",
        "menu display text", placeholders, menu.label,
        {"renderer": "menu", "quoted": quoted, "label": menu.label},
    )


def build_message_nodes(source: str, mask: str, singles: list[Literal],
                        excluded: list[tuple[int, int]]) -> list[Node]:
    visible = [lit for lit in singles
               if not interval_contains(lit.start, lit.end, excluded)]
    nodes: list[Node] = []
    index = 0
    while index < len(visible):
        first = visible[index]
        last = first
        placeholders: list[dict] = []
        add_placeholder = placeholder_factory(placeholders)
        chunks = [literal_text(first, add_placeholder)]
        cursor = first.end
        index += 1
        terminated = False
        while True:
            next_literal = visible[index] if index < len(visible) else None
            limit = next_literal.start if next_literal else len(source)
            local = cursor
            controls: list[Control] = []
            while True:
                control = parse_inline_control(source, mask, local, limit)
                if not control:
                    break
                controls.append(control)
                local = control.end
                if control.kind == "A":
                    terminated = True
                    break
            for control in controls:
                chunks.append(add_placeholder(control.raw, control.kind))
                last_end = control.end
            if controls:
                last = Literal(last.start, last_end, last.quote, last.parts)
            only_space_to_next = next_literal is not None and not mask[local:limit].strip()
            if next_literal is not None and only_space_to_next and not terminated:
                chunks.append(literal_text(next_literal, add_placeholder))
                last = next_literal
                cursor = next_literal.end
                index += 1
                continue
            break
        nodes.append(Node(
            first.start, last.end, "message-chain", "".join(chunks),
            "high", "single-quoted display chain", placeholders,
            syntax={"renderer": "message-chain"},
        ))
    return nodes


def find_statement_end(mask: str, start: int) -> int | None:
    colon = mask.find(":", start)
    if colon < 0:
        return None
    newline = mask.find("\n", start)
    if newline >= 0 and newline < colon:
        return None
    return colon


def split_arguments(mask: str, source: str, start: int, end: int) -> list[tuple[int, int]]:
    comma_positions = [i for i in range(start, end) if mask[i] == ","]
    bounds = [start] + [i + 1 for i in comma_positions]
    limits = comma_positions + [end]
    result = []
    for left, right in zip(bounds, limits):
        while left < right and source[left] in " \t\r\n\v\f":
            left += 1
        while right > left and source[right - 1] in " \t\r\n\v\f":
            right -= 1
        result.append((left, right))
    return result


def decode_argument(source: str, literals: list[Literal], start: int, end: int,
                    add_placeholder) -> tuple[str, str]:
    exact = next((lit for lit in literals if lit.start == start and lit.end == end), None)
    if exact:
        return literal_text(exact, add_placeholder), exact.quote
    return source[start:end], "bare"


def command_at(literals: list[Literal], start: int, end: int) -> Literal | None:
    return next((lit for lit in literals if lit.start >= start and lit.end <= end), None)


def build_command_nodes(source: str, mask: str, literals: list[Literal],
                        occupied: list[tuple[int, int]],
                        diagnostics: Counter[str] | None = None) -> tuple[list[Node], list[tuple[int, int]]]:
    nodes: list[Node] = []
    excluded: list[tuple[int, int]] = []
    for match in COMMAND_RE.finditer(mask):
        command = match.group(1)
        if command not in (DISPLAY_STRING_ARGS.keys() | CANDIDATE_STRING_ARGS.keys()
                           | EXCLUDED_STRING_COMMANDS | {"sysAddWebMenu"}):
            continue
        end = find_statement_end(mask, match.end())
        if end is None:
            continue
        args = split_arguments(mask, source, match.end(), end)
        statement_literals = [lit for lit in literals
                              if lit.start >= match.end() and lit.end <= end]
        if command in EXCLUDED_STRING_COMMANDS:
            excluded.extend((lit.start, lit.end) for lit in statement_literals)
            continue
        spec = DISPLAY_STRING_ARGS.get(command)
        confidence = "high"
        if spec:
            arg_index, confidence, reason = spec
        else:
            arg_index, reason = CANDIDATE_STRING_ARGS[command]
            confidence = "candidate"
        if arg_index >= len(args):
            continue
        start, arg_end = args[arg_index]
        if start >= arg_end:
            if confidence == "candidate" and diagnostics is not None:
                diagnostics[f"excluded blank string argument: {command}"] += 1
            continue
        if interval_overlaps(start, arg_end, occupied):
            continue
        placeholders: list[dict] = []
        add_placeholder = placeholder_factory(placeholders)
        text, quote = decode_argument(source, literals, start, arg_end, add_placeholder)
        if confidence == "candidate" and not text.strip():
            if diagnostics is not None:
                diagnostics[f"excluded blank string argument: {command}"] += 1
            continue
        nodes.append(Node(
            start, arg_end, "display-string" if confidence == "high" else "string-argument",
            text, confidence, reason, placeholders,
            syntax={"renderer": "command-string", "command": command,
                    "argument": arg_index, "quote": quote},
        ))
        excluded.extend((lit.start, lit.end) for lit in statement_literals)
    return nodes, excluded


def build_double_candidates(source: str, doubles: list[Literal],
                            occupied: list[tuple[int, int]],
                            diagnostics: Counter[str] | None = None) -> list[Node]:
    nodes: list[Node] = []
    for literal in doubles:
        if interval_overlaps(literal.start, literal.end, occupied):
            continue
        placeholders: list[dict] = []
        add_placeholder = placeholder_factory(placeholders)
        text = literal_text(literal, add_placeholder)
        if not text.strip():
            if diagnostics is not None:
                diagnostics["excluded blank double-quoted string data"] += 1
            continue
        nodes.append(Node(
            literal.start, literal.end, "string-data",
            text, "candidate",
            "standalone or semantically ambiguous double-quoted string data",
            placeholders, syntax={"renderer": "double-string"},
        ))
    return nodes


def line_starts(source: str) -> list[int]:
    starts = [0]
    starts.extend(i + 1 for i, char in enumerate(source) if char == "\n")
    return starts


def offset_location(starts: list[int], offset: int) -> dict:
    import bisect
    line_index = bisect.bisect_right(starts, offset) - 1
    return {"line": line_index + 1, "column": offset - starts[line_index] + 1,
            "offset": offset}


def nearest_label(labels: list[tuple[int, str]], offset: int) -> str | None:
    result = None
    for position, label in labels:
        if position > offset:
            break
        result = label
    return result


def context_lines(source: str, starts: list[int], start: int, end: int) -> tuple[str, str]:
    start_line = offset_location(starts, start)["line"] - 1
    end_line = offset_location(starts, max(start, end - 1))["line"] - 1
    lines = source.splitlines()
    before = "\n".join(lines[max(0, start_line - 2):start_line])
    after = "\n".join(lines[end_line + 1:end_line + 3])
    return before, after


def parse_adv(source: str, relative_path: str,
              diagnostics: Counter[str] | None = None) -> list[dict]:
    mask, literals, dollars = scan_source(source)
    if diagnostics is not None:
        for match in COMMAND_RE.finditer(mask):
            command = match.group(1)
            if command in EXCLUDED_STRING_COMMANDS:
                diagnostics[f"excluded command string: {command}"] += 1
            elif command == "sysAddWebMenu":
                diagnostics["excluded URL: sysAddWebMenu argument 2"] += 1
    menus = parse_menus(source, mask, dollars)
    singles = [lit for lit in literals if lit.quote == "'"]
    doubles = [lit for lit in literals if lit.quote == '"']
    menu_intervals = [(menu.start, menu.end) for menu in menus]
    nodes = [build_menu_node(source, mask, menu, singles) for menu in menus]
    control_only_menus = [node for node in nodes
                          if not PLACEHOLDER_RE.sub("", node.source).strip()]
    if diagnostics is not None and control_only_menus:
        diagnostics["excluded dynamic menu without static text"] += len(control_only_menus)
    nodes = [node for node in nodes if node not in control_only_menus]
    nodes.extend(build_message_nodes(source, mask, singles, menu_intervals))
    occupied = [(node.start, node.end) for node in nodes]
    command_nodes, command_excluded = build_command_nodes(
        source, mask, literals, occupied, diagnostics)
    nodes.extend(command_nodes)
    occupied.extend((node.start, node.end) for node in command_nodes)
    occupied.extend(command_excluded)
    nodes.extend(build_double_candidates(source, doubles, occupied, diagnostics))
    nodes.sort(key=lambda node: (node.start, node.end, node.kind))

    starts = line_starts(source)
    labels = sorted((match.start(), match.group(1)) for match in LABEL_RE.finditer(mask))
    counters: Counter[str] = Counter()
    records: list[dict] = []
    for node in nodes:
        counters[node.kind] += 1
        raw = source[node.start:node.end]
        before, after = context_lines(source, starts, node.start, node.end)
        record = {
            "id": f"{relative_path}:{node.kind}:{counters[node.kind]:06d}",
            "file": relative_path,
            "type": node.kind,
            "source": node.source,
            "translation": "",
            "status": "untranslated",
            "source_hash": sha256_text(raw),
            "raw_source": raw,
            "start": offset_location(starts, node.start),
            "end": offset_location(starts, node.end),
            "label": nearest_label(labels, node.start),
            "menu_target": node.menu_target,
            "context_before": before,
            "context_after": after,
            "confidence": node.confidence,
            "reason": node.reason,
            "placeholders": node.placeholders,
            "syntax": node.syntax,
        }
        records.append(record)
    return records


def read_config_encoding(project: Path) -> str:
    config = project / "xsys35c.cfg"
    if config.exists():
        text = config.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"(?mi)^\s*encoding\s*=\s*(\S+)", text)
        if match:
            value = match.group(1).lower()
            if value in {"utf8", "utf-8"}:
                return "utf-8"
            if value in {"sjis", "shift-jis", "shift_jis", "cp932"}:
                return "cp932"
            raise AdvTextError(f"unsupported ADV encoding in {config}: {value}")
    return "utf-8"


def adv_files(project: Path) -> list[Path]:
    files = [path for path in project.iterdir()
             if path.is_file() and path.suffix.lower() == ".adv"]
    return sorted(files, key=lambda path: path.name.encode("utf-8"))


def extract_project(project: Path) -> tuple[list[dict], list[dict], dict]:
    encoding = read_config_encoding(project)
    main: list[dict] = []
    candidates: list[dict] = []
    errors = []
    excluded_reasons: Counter[str] = Counter()
    for path in adv_files(project):
        try:
            source = path.read_text(encoding=encoding)
            records = parse_adv(source, path.name, excluded_reasons)
        except (UnicodeError, AdvTextError) as exc:
            errors.append({"file": path.name, "error": str(exc)})
            continue
        for record in records:
            (main if record["confidence"] == "high" else candidates).append(record)
    counts = Counter(record["type"] for record in main + candidates)
    candidate_reasons = Counter(record["reason"] for record in candidates)
    placeholder_kinds = Counter(
        placeholder["kind"] for record in main + candidates
        for placeholder in record["placeholders"])
    control_leaks = []
    for record in main:
        visible = PLACEHOLDER_RE.sub("", record["source"])
        if CONTROL_LEAK_RE.search(visible):
            control_leaks.append(record["id"])
    report = {
        "project": str(project),
        "encoding": encoding,
        "adv_files": len(adv_files(project)),
        "main_entries": len(main),
        "candidate_entries": len(candidates),
        "types": dict(sorted(counts.items())),
        "candidate_reasons": dict(sorted(candidate_reasons.items())),
        "excluded_reasons": dict(sorted(excluded_reasons.items())),
        "placeholder_kinds": dict(sorted(placeholder_kinds.items())),
        "empty_entries": sum(not record["source"] for record in main + candidates),
        "placeholder_errors": 0,
        "control_leak_suspects": control_leaks,
        "parse_errors": errors,
    }
    return main, candidates, report


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            stream.write("\n")


def read_jsonl(path: Path) -> list[dict]:
    records = []
    seen = set()
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AdvTextError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            identifier = record.get("id")
            if not identifier or identifier in seen:
                raise AdvTextError(f"{path}:{line_number}: missing or duplicate id {identifier!r}")
            seen.add(identifier)
            records.append(record)
    return records


def placeholder_tokens(text: str) -> list[str]:
    return [match.group(0) for match in PLACEHOLDER_RE.finditer(text)]


def escape_adv(text: str, quote: str) -> str:
    result = []
    for char in text:
        if char in {"\\", quote, "<"}:
            result.append("\\")
        result.append(char)
    return "".join(result)


def split_translation(record: dict) -> tuple[list[str], list[dict]]:
    translation = record["translation"]
    placeholders = record.get("placeholders", [])
    expected = [item["token"] for item in placeholders]
    actual = placeholder_tokens(translation)
    if actual != expected:
        raise AdvTextError(
            f"{record['id']}: placeholder sequence differs; expected {expected}, got {actual}")
    if not expected:
        return [translation], placeholders
    pattern = re.compile("(" + "|".join(re.escape(token) for token in expected) + ")")
    values = pattern.split(translation)
    text_parts = values[::2]
    tokens = values[1::2]
    if tokens != expected:
        raise AdvTextError(f"{record['id']}: placeholders may not be moved or duplicated")
    return text_parts, placeholders


def render_message_chain(record: dict) -> str:
    return " ".join(render_display_parts(record, "'"))


def render_display_parts(record: dict, quote: str) -> list[str]:
    parts, placeholders = split_translation(record)
    output: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            output.append(quote + "".join(current) + quote)
            current.clear()

    for index, text in enumerate(parts):
        if text:
            current.append(escape_adv(text, quote))
        if index < len(placeholders):
            placeholder = placeholders[index]
            if placeholder["kind"] == "sjis-codepoint":
                current.append(placeholder["raw"])
            else:
                flush()
                output.append(placeholder["raw"])
    flush()
    return output


def render_menu(record: dict) -> str:
    parts, placeholders = split_translation(record)
    label = record["syntax"]["label"]
    if not placeholders and not record["syntax"].get("quoted"):
        if "$" in parts[0]:
            raise AdvTextError(f"{record['id']}: menu text may not contain '$'")
        body = parts[0]
    else:
        body = " ".join(render_display_parts(record, "'"))
    return f"${label}${body}$"


def render_record(record: dict) -> str:
    renderer = record["syntax"]["renderer"]
    if renderer == "message-chain":
        return render_message_chain(record)
    if renderer == "menu":
        return render_menu(record)
    if renderer in {"double-string", "command-string"}:
        rendered = render_display_parts(record, '"')
        if len(rendered) != 1 or not rendered[0].startswith('"'):
            raise AdvTextError(f"{record['id']}: control placeholder is invalid in {renderer}")
        return rendered[0]
    raise AdvTextError(f"{record['id']}: unknown renderer {renderer!r}")


def apply_catalog(project: Path, catalog: Path, output: Path,
                  allow_candidates: bool) -> dict:
    if output.exists():
        raise AdvTextError(f"output directory already exists: {output}")
    main, candidates, extract_report = extract_project(project)
    current = {record["id"]: record for record in main + candidates}
    requested = read_jsonl(catalog)
    replacements: dict[str, list[tuple[int, int, str, str]]] = defaultdict(list)
    translated = 0
    skipped = 0
    for record in requested:
        translation = record.get("translation", "")
        if not translation or record.get("status") == "skipped":
            skipped += 1
            continue
        actual = current.get(record["id"])
        if actual is None:
            raise AdvTextError(f"{record['id']}: node does not exist in target project")
        if actual["confidence"] != "high" and not allow_candidates:
            raise AdvTextError(f"{record['id']}: candidate application requires --allow-candidates")
        for field_name in ("source", "source_hash", "raw_source"):
            if record.get(field_name) != actual.get(field_name):
                raise AdvTextError(f"{record['id']}: {field_name} no longer matches baseline")
        expected_tokens = [item["token"] for item in actual.get("placeholders", [])]
        catalog_tokens = [item["token"] for item in record.get("placeholders", [])]
        if catalog_tokens != expected_tokens:
            raise AdvTextError(f"{record['id']}: placeholder metadata no longer matches baseline")
        replacement = render_record(record)
        replacements[actual["file"]].append((
            actual["start"]["offset"], actual["end"]["offset"], replacement,
            actual["id"],
        ))
        translated += 1

    encoding = read_config_encoding(project)
    shutil.copytree(project, output)
    try:
        for name, items in replacements.items():
            path = output / name
            source = path.read_text(encoding=encoding)
            items.sort(reverse=True)
            previous_start = len(source) + 1
            for start, end, replacement, identifier in items:
                if end > previous_start:
                    raise AdvTextError(f"{identifier}: overlapping replacement")
                source = source[:start] + replacement + source[end:]
                previous_start = start
            path.write_text(source, encoding=encoding, newline="")
    except Exception:
        shutil.rmtree(output)
        raise
    return {
        "project": str(project),
        "catalog": str(catalog),
        "output": str(output),
        "translated_entries": translated,
        "skipped_entries": skipped,
        "placeholder_errors": 0,
        "location_errors": 0,
        "baseline": extract_report,
    }


def extract_command(args: argparse.Namespace) -> int:
    main, candidates, report = extract_project(args.project)
    if report["parse_errors"]:
        for error in report["parse_errors"]:
            print(f"ERROR: {error['file']}: {error['error']}", file=sys.stderr)
        return 1
    write_jsonl(args.output, main)
    write_jsonl(args.candidates, candidates)
    report_path = args.report or args.output.with_suffix(args.output.suffix + ".report.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


def apply_command(args: argparse.Namespace) -> int:
    report = apply_catalog(args.project, args.catalog, args.output,
                           args.allow_candidates)
    report_path = args.report or args.output.parent / f"{args.output.name}.apply-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract = subparsers.add_parser("extract", help="extract translation catalogs")
    extract.add_argument("project", type=Path)
    extract.add_argument("--output", type=Path, required=True)
    extract.add_argument("--candidates", type=Path, required=True)
    extract.add_argument("--report", type=Path)
    extract.set_defaults(func=extract_command)

    apply_parser = subparsers.add_parser("apply", help="apply translated catalog safely")
    apply_parser.add_argument("project", type=Path)
    apply_parser.add_argument("catalog", type=Path)
    apply_parser.add_argument("--output", type=Path, required=True)
    apply_parser.add_argument("--allow-candidates", action="store_true")
    apply_parser.add_argument("--report", type=Path)
    apply_parser.set_defaults(func=apply_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (AdvTextError, OSError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
