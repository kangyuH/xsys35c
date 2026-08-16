#!/usr/bin/env python3
"""Ask DeepSeek to decide glossary fixes one record at a time (batched for API efficiency).

No deterministic string replacement is performed. The model must explicitly return
either the unchanged translation or a revised one for each id.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


ENDPOINT = "https://api.deepseek.com/chat/completions"
PLACEHOLDER_RE = re.compile(r"⟦ADV:\d+⟧")
NOTE_TAG = "glossary-llm-v1"
MODEL_PRICES_CNY = {
    "deepseek-v4-flash": (1.0, 2.0),
    "deepseek-v4-pro": (3.0, 6.0),
}

SYSTEM = """你是《鬼畜王兰斯》简体中文本地化的术语审校编辑。
你只会收到「日文原文 + 现行中文译文 + 本条命中的锁定术语」。请对每条独立现场判断：

1. 先判断日文里的锁定 source 在本句是否真的指该专名。
2. 若是专名，且中文未使用 target 或 alts：必须 action=fix，把专名改成 target（优先）或列出的 alts；不得保留 forbidden_hints 旧译，也不得自造近音异写。
3. 若不是专名（同形异义、普通词、误匹配）：action=keep，translation 必须与输入完全一致。
4. 只允许改专名相关文字；不得改写剧情、语气；⟦ADV:n⟧ 占位符数量、内容、顺序、语义位置必须不变。
5. 只输出 JSON：{"decisions":[{"id":"...","action":"keep|fix","translation":"...","reason":"极短理由"}]}。
6. 每个输入 id 必须且只能出现一次。"""


class FixError(Exception):
    pass


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: Sequence[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")


def placeholders(text: str) -> list[str]:
    return PLACEHOLDER_RE.findall(text)


def load_queue(path: Path, priorities: set[str] | None) -> list[dict]:
    rows = read_jsonl(path)
    if priorities:
        rows = [row for row in rows if row.get("priority") in priorities]
    # high first, then medium, then low; stable by id
    order = {"high": 0, "medium": 1, "low": 2}
    rows.sort(key=lambda row: (order.get(row.get("priority", "low"), 9), row["id"]))
    return rows


def chunked(rows: Sequence[dict], size: int) -> list[list[dict]]:
    return [list(rows[i : i + size]) for i in range(0, len(rows), size)]


class State:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS decisions (
              id TEXT PRIMARY KEY,
              action TEXT NOT NULL,
              translation TEXT NOT NULL,
              reason TEXT,
              model TEXT NOT NULL,
              batch_key TEXT NOT NULL,
              prompt_sha256 TEXT NOT NULL,
              cost REAL NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS batches (
              batch_key TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              error TEXT,
              prompt_tokens INTEGER,
              completion_tokens INTEGER,
              cost REAL,
              updated_at TEXT NOT NULL
            )
            """
        )
        self.db.commit()

    def get(self, identifier: str) -> dict | None:
        row = self.db.execute(
            "SELECT id, action, translation, reason FROM decisions WHERE id=?",
            (identifier,),
        ).fetchone()
        if not row:
            return None
        return {"id": row[0], "action": row[1], "translation": row[2], "reason": row[3]}

    def cost(self) -> float:
        value = self.db.execute("SELECT COALESCE(SUM(cost),0) FROM decisions").fetchone()[0]
        return float(value)

    def save_batch(
        self,
        batch_key: str,
        decisions: list[dict],
        model: str,
        prompt_hash: str,
        usage: dict,
        price: tuple[float, float],
    ) -> None:
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))
        cost = (prompt_tokens * price[0] + completion_tokens * price[1]) / 1_000_000
        per = cost / max(len(decisions), 1)
        with self.db:
            self.db.execute(
                """
                INSERT INTO batches(batch_key,status,error,prompt_tokens,completion_tokens,cost,updated_at)
                VALUES(?,?,NULL,?,?,?,?)
                ON CONFLICT(batch_key) DO UPDATE SET
                  status=excluded.status, error=NULL,
                  prompt_tokens=excluded.prompt_tokens,
                  completion_tokens=excluded.completion_tokens,
                  cost=excluded.cost, updated_at=excluded.updated_at
                """,
                (batch_key, "ok", prompt_tokens, completion_tokens, cost, now),
            )
            for row in decisions:
                self.db.execute(
                    """
                    INSERT INTO decisions(id,action,translation,reason,model,batch_key,prompt_sha256,cost,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      action=excluded.action, translation=excluded.translation,
                      reason=excluded.reason, model=excluded.model,
                      batch_key=excluded.batch_key, prompt_sha256=excluded.prompt_sha256,
                      cost=excluded.cost, updated_at=excluded.updated_at
                    """,
                    (
                        row["id"],
                        row["action"],
                        row["translation"],
                        row.get("reason"),
                        model,
                        batch_key,
                        prompt_hash,
                        per,
                        now,
                    ),
                )

    def fail_batch(self, batch_key: str, error: str) -> None:
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        with self.db:
            self.db.execute(
                """
                INSERT INTO batches(batch_key,status,error,updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(batch_key) DO UPDATE SET status=excluded.status,
                  error=excluded.error, updated_at=excluded.updated_at
                """,
                (batch_key, "error", error[:2000], now),
            )


def build_messages(batch: Sequence[dict]) -> list[dict]:
    payload = {
        "task": "glossary_term_review",
        "entries": [
            {
                "id": row["id"],
                "source_ja": row["source"],
                "translation": row["translation"],
                "required_terms": row.get("required") or [],
                "priority": row.get("priority"),
                "file": row.get("file"),
                "type": row.get("type"),
            }
            for row in batch
        ],
    }
    return [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": "请逐条现场判断，不要做机械替换。\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def call_api(api_key: str, model: str, messages: list[dict], timeout: int) -> tuple[str, dict]:
    body = json.dumps(
        {
            "model": model,
            "messages": messages,
            "thinking": {"type": "disabled"},
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "max_tokens": 8192,
        },
        ensure_ascii=False,
    ).encode()
    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.load(response)
    choices = result.get("choices")
    if not isinstance(choices, list) or not choices:
        raise FixError("API response has no choices")
    choice = choices[0]
    if choice.get("finish_reason") != "stop":
        raise FixError(f"finish_reason={choice.get('finish_reason')!r}")
    content = choice.get("message", {}).get("content")
    if not isinstance(content, str) or not content.strip():
        raise FixError("empty content")
    return content, result.get("usage") or {}


def validate_decisions(batch: Sequence[dict], text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FixError(f"invalid JSON: {exc}") from exc
    rows = data.get("decisions") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise FixError("missing decisions array")
    by_id = {row["id"]: row for row in batch}
    output: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise FixError("decision row is not an object")
        identifier = row.get("id")
        action = row.get("action")
        translation = row.get("translation")
        reason = row.get("reason")
        if identifier not in by_id:
            raise FixError(f"unexpected id {identifier!r}")
        if action not in {"keep", "fix"}:
            raise FixError(f"invalid action for {identifier}")
        if not isinstance(translation, str) or not translation:
            raise FixError(f"empty translation for {identifier}")
        original = by_id[identifier]["translation"]
        if placeholders(translation) != placeholders(original):
            raise FixError(f"placeholder mismatch for {identifier}")
        if action == "keep" and translation != original:
            # Force keep semantics: model must not silently rewrite.
            raise FixError(f"keep action changed text for {identifier}")
        if action == "fix" and translation == original:
            # Model often says fix when it actually judged the current wording OK.
            action = "keep"
            reason = (reason or "") + "|coerced_keep_unchanged"
        if identifier in output:
            raise FixError(f"duplicate id {identifier}")
        output[identifier] = {
            "id": identifier,
            "action": action,
            "translation": translation,
            "reason": reason if isinstance(reason, str) else "",
        }
    if set(output) != set(by_id):
        missing = sorted(set(by_id) - set(output))
        extra = sorted(set(output) - set(by_id))
        raise FixError(f"id set mismatch missing={missing[:3]} extra={extra[:3]}")
    return [output[row["id"]] for row in batch]


def process_batch(
    state: State,
    batch: Sequence[dict],
    model: str,
    api_key: str,
    retries: int,
    timeout: int,
    hard_budget: float,
) -> None:
    pending = [row for row in batch if state.get(row["id"]) is None]
    if not pending:
        return
    batch_key = hashlib.sha1("|".join(row["id"] for row in pending).encode()).hexdigest()[:16]
    messages = build_messages(pending)
    prompt_hash = hashlib.sha256(
        json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    price = MODEL_PRICES_CNY[model]
    last_error = ""
    for attempt in range(retries + 1):
        if state.cost() >= hard_budget:
            raise FixError(f"hard budget reached: {hard_budget:.2f} CNY")
        try:
            text, usage = call_api(api_key, model, messages, timeout)
            decisions = validate_decisions(pending, text)
            projected = state.cost() + (
                int(usage.get("prompt_tokens", 0)) * price[0]
                + int(usage.get("completion_tokens", 0)) * price[1]
            ) / 1_000_000
            if projected > hard_budget + 1e-9:
                raise FixError(f"response would exceed budget: {projected:.4f}")
            state.save_batch(batch_key, decisions, model, prompt_hash, usage, price)
            return
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, FixError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            state.fail_batch(batch_key, last_error)
            if attempt < retries:
                time.sleep(min(2 ** attempt, 8))
    raise FixError(f"{batch_key}: {last_error}")


def merge_note(existing: str | None, addition: str) -> str:
    if not existing:
        return addition
    if addition in existing:
        return existing
    return f"{existing}; {addition}"


def apply_decisions_to_overlays(
    main_path: Path,
    candidate_path: Path,
    state: State,
    queue: Sequence[dict],
) -> dict:
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    decisions = {}
    for row in queue:
        saved = state.get(row["id"])
        if saved is not None:
            decisions[row["id"]] = saved
    changed = 0
    kept = 0
    pending = 0

    def patch(path: Path) -> list[dict]:
        nonlocal changed, kept, pending
        rows = read_jsonl(path)
        output = []
        for row in rows:
            decision = decisions.get(row["id"])
            if decision is None:
                # Not in completed LLM set.
                if any(item["id"] == row["id"] for item in queue):
                    pending += 1
                output.append(row)
                continue
            if decision["action"] == "keep":
                kept += 1
                output.append(row)
                continue
            if decision["translation"] == row["translation"]:
                kept += 1
                output.append(row)
                continue
            if placeholders(decision["translation"]) != placeholders(row["translation"]):
                raise FixError(f"refusing unsafe overlay write for {row['id']}")
            updated = dict(row)
            updated["translation"] = decision["translation"]
            updated["notes"] = merge_note(updated.get("notes"), f"{NOTE_TAG}:fix")
            updated["updated_at"] = now
            updated["reviewer"] = updated.get("reviewer") or "deepseek-glossary"
            # Keep original approved status for build profile compatibility.
            changed += 1
            output.append(updated)
        return output

    write_jsonl(main_path, patch(main_path))
    write_jsonl(candidate_path, patch(candidate_path))
    return {"changed": changed, "kept": kept, "pending": pending, "decided": len(decisions)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--queue",
        type=Path,
        default=Path("workspace/translation/reports/glossary-audit/queue.jsonl"),
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=Path("workspace/translation/reports/glossary-llm/state.sqlite"),
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
    parser.add_argument("--model", choices=tuple(MODEL_PRICES_CNY), default="deepseek-v4-flash")
    parser.add_argument("--batch-size", type=int, default=12)
    parser.add_argument("--budget", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument(
        "--priorities",
        default="high,medium,low",
        help="comma-separated priorities to process",
    )
    parser.add_argument("--limit", type=int, default=0, help="optional max queue rows")
    parser.add_argument("--apply-only", action="store_true", help="only write completed decisions")
    parser.add_argument(
        "--reset-queue-decisions",
        action="store_true",
        help="delete existing decisions for ids in this queue before re-asking the LLM",
    )
    parser.add_argument("--env", type=Path, default=Path(".env"))
    args = parser.parse_args()

    load_env_file(args.env.resolve())
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key and not args.apply_only:
        raise SystemExit("DEEPSEEK_API_KEY missing")

    priorities = {item.strip() for item in args.priorities.split(",") if item.strip()}
    queue = load_queue(args.queue.resolve(), priorities)
    if args.limit > 0:
        queue = queue[: args.limit]
    state = State(args.state.resolve())
    if args.reset_queue_decisions:
        ids = [row["id"] for row in queue]
        with state.db:
            state.db.executemany("DELETE FROM decisions WHERE id=?", [(i,) for i in ids])
        print(json.dumps({"reset_decisions": len(ids)}, ensure_ascii=False), flush=True)

    if not args.apply_only:
        batches = chunked(queue, max(1, args.batch_size))
        print(
            json.dumps(
                {
                    "queue": len(queue),
                    "batches": len(batches),
                    "already_cost": state.cost(),
                    "budget": args.budget,
                    "model": args.model,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        for index, batch in enumerate(batches, 1):
            unfinished = [row for row in batch if state.get(row["id"]) is None]
            if not unfinished:
                continue
            if state.cost() >= args.budget:
                print(json.dumps({"stop": "budget", "cost": state.cost()}, ensure_ascii=False))
                break
            try:
                process_batch(
                    state,
                    unfinished,
                    args.model,
                    api_key,
                    args.retries,
                    args.timeout,
                    args.budget,
                )
            except FixError as exc:
                print(
                    json.dumps(
                        {
                            "batch_error": str(exc),
                            "batch_index": index,
                            "ids": [row["id"] for row in unfinished[:3]],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                continue
            if index % 10 == 0 or index == len(batches):
                decided = sum(1 for row in queue if state.get(row["id"]) is not None)
                print(
                    json.dumps(
                        {
                            "progress_batch": index,
                            "total_batches": len(batches),
                            "decided": decided,
                            "cost": round(state.cost(), 4),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

    stats = apply_decisions_to_overlays(
        args.main_overlay.resolve(),
        args.candidate_overlay.resolve(),
        state,
        queue,
    )
    report = {
        "queue": len(queue),
        "cost_cny": round(state.cost(), 4),
        **stats,
    }
    out = Path("workspace/translation/reports/glossary-llm/apply-summary.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FixError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
