"""离线风格测试集工具，不调用网络、不读取 Codex 历史记录。

输入/输出均为 JSONL，便于先人工脱敏，再把少量样本交给模型或人工评审。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path


def read_jsonl(path: Path):
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"{path}:{line_number} 不是 JSONL") from error
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{line_number} 必须是 JSON 对象")
        yield row


def validate_dataset(path: Path):
    rows = list(read_jsonl(path))
    ids = set()
    categories = Counter()
    for index, row in enumerate(rows, 1):
        row_id = row.get("id")
        if not isinstance(row_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", row_id):
            raise ValueError(f"第 {index} 行 id 不合法")
        if row_id in ids:
            raise ValueError(f"第 {index} 行 id 重复：{row_id}")
        ids.add(row_id)
        if not isinstance(row.get("category"), str) or not row["category"].strip():
            raise ValueError(f"第 {index} 行 category 不合法")
        if not isinstance(row.get("prompt"), str) or not row["prompt"].strip():
            raise ValueError(f"第 {index} 行 prompt 不合法")
        checks = row.get("checks")
        if not isinstance(checks, list) or not 1 <= len(checks) <= 8 or not all(isinstance(x, str) for x in checks):
            raise ValueError(f"第 {index} 行 checks 不合法")
        categories[row["category"]] += 1
    if len(rows) < 12:
        raise ValueError("测试集至少需要 12 个场景")
    return rows, categories


def new_session(dataset: Path, output: Path):
    rows, _ = validate_dataset(dataset)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps({
                "id": row["id"], "category": row["category"], "prompt": row["prompt"],
                "checks": row["checks"], "response_a": "", "response_b": "",
                "preferred": "", "notes": ""
            }, ensure_ascii=False) + "\n")


def score(session: Path):
    rows = list(read_jsonl(session))
    preferred = Counter()
    categories = Counter()
    undecided = 0
    for row in rows:
        value = row.get("preferred", "")
        if value not in ("a", "b", "tie", "skip"):
            undecided += 1
        else:
            preferred[value] += 1
        categories[row.get("category", "unknown")] += 1
    print(json.dumps({"items": len(rows), "preferred": dict(preferred),
                      "undecided": undecided, "categories": dict(categories)},
                     ensure_ascii=False, indent=2))


def export_preference_pairs(session: Path, output: Path):
    """导出已标注的 A/B 选择，供后续 DPO/KTO 实验使用。

    这里只导出题目和两个可见候选，不导出评语、原始聊天或隐藏推理。
    tie、skip 和缺少候选的行会被跳过，并在命令结果中报告数量。
    """
    rows = list(read_jsonl(session))
    output.parent.mkdir(parents=True, exist_ok=True)
    pairs = 0
    skipped = Counter()
    with output.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            preferred = row.get("preferred", "")
            if preferred not in ("a", "b"):
                skipped[preferred or "undecided"] += 1
                continue
            rejected_key = "response_b" if preferred == "a" else "response_a"
            chosen = row.get(f"response_{preferred}")
            rejected = row.get(rejected_key)
            if not isinstance(chosen, str) or not chosen.strip() or not isinstance(rejected, str) or not rejected.strip():
                skipped["missing_response"] += 1
                continue
            # metadata 只保留评测题自身的公开标签，避免把人工评语或私密上下文带入训练集。
            stream.write(json.dumps({
                "prompt": row.get("prompt", ""),
                "chosen": chosen.strip(),
                "rejected": rejected.strip(),
                "metadata": {"id": row.get("id", ""), "category": row.get("category", "")},
            }, ensure_ascii=False) + "\n")
            pairs += 1
    return {"pairs": pairs, "skipped": dict(skipped), "output": str(output)}


def extract_users(source: Path, output: Path):
    """从已人工导出的通用 JSONL 中只保留 user 行，并做最小脱敏。"""
    path_pattern = re.compile(r"(?i)(?:[A-Z]:)?\\(?:[^\\\r\n]+\\)+[^\\\r\n]*")
    secret_pattern = re.compile(r"(?i)\b(?:sk|key|token)[-_]?[A-Za-z0-9]{12,}\b")
    credential_pattern = re.compile(r"(?i)\b(?:api[_-]?key|authorization|bearer)\s*[:=]\s*[^\s,;]+")
    wxid_pattern = re.compile(r"(?i)\bwxid_[A-Za-z0-9_-]+\b")
    room_pattern = re.compile(r"\b[0-9A-Za-z_-]{5,}@chatroom\b")
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with output.open("w", encoding="utf-8", newline="\n") as stream:
        for row in read_jsonl(source):
            if row.get("role") != "user" or not isinstance(row.get("text"), str):
                continue
            text = row["text"]
            text = path_pattern.sub("[LOCAL_PATH]", text)
            text = credential_pattern.sub("[REDACTED_CREDENTIAL]", text)
            text = secret_pattern.sub("[REDACTED_SECRET]", text)
            text = wxid_pattern.sub("[WXID]", text)
            text = room_pattern.sub("[GROUP_ID]", text)
            stream.write(json.dumps({"source": str(row.get("source", "manual")),
                                     "role": "user", "text": text[:8000]}, ensure_ascii=False) + "\n")
            count += 1
    return count


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("validate")
    check.add_argument("dataset", type=Path)
    session = sub.add_parser("new-session")
    session.add_argument("dataset", type=Path)
    session.add_argument("output", type=Path)
    report = sub.add_parser("score")
    report.add_argument("session", type=Path)
    pairs = sub.add_parser("export-pairs")
    pairs.add_argument("session", type=Path)
    pairs.add_argument("output", type=Path)
    extract = sub.add_parser("extract-users")
    extract.add_argument("source", type=Path)
    extract.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            rows, categories = validate_dataset(args.dataset)
            print(json.dumps({"items": len(rows), "categories": dict(categories)}, ensure_ascii=False))
        elif args.command == "new-session":
            new_session(args.dataset, args.output)
            print(f"已创建离线评测表：{args.output}")
        elif args.command == "score":
            score(args.session)
        elif args.command == "export-pairs":
            print(json.dumps(export_preference_pairs(args.session, args.output), ensure_ascii=False))
        else:
            print(json.dumps({"user_rows": extract_users(args.source, args.output),
                              "output": str(args.output)}, ensure_ascii=False))
        return 0
    except (OSError, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
