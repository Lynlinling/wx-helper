"""Create a source-only, scrubbed GitHub release folder and ZIP."""
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {
    ".gitattributes", ".gitignore", "README.md", "CHANGELOG.md", "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md", "LICENSE", "PRIVACY.md", "SECURITY.md", "THIRD_PARTY_NOTICES.md",
    "main.py", "gui.py", "start_bot.ps1", "pytest.ini", "requirements.txt",
    "requirements-build.txt", "bot_config.example.json", "members_config.example.json",
    "llm_config.example.json", "llm_router.example.json",
}
SAFE_DOCS = {
    "docs/AGENT_ADAPTERS.md", "docs/CODEX_INBOX.md", "docs/MEMORY_ARCHITECTURE.md",
    "docs/PROMPT_ARCHITECTURE.md",
}
SKIP_NAMES = {
    ".git", ".runtime", "logs", "backups", "release", ".venv", "venv", "__pycache__",
    ".pytest_cache", "build", "dist", "_internal", "screenshots", ".build",
    "build_text", "build_text_42",
}
SKIP_FILES = {
    "tests/test_private_probe_42.py", "tools/build_private_probe_42.ps1",
    "tools/run_private_probe_42.py", "tools/build_probe_42.ps1", "tools/run_probe_42.py",
}
SKIP_SUFFIXES = {
    ".dll", ".exe", ".pyc", ".pdb", ".obj", ".lib", ".exp", ".ilk", ".dmp",
    ".sqlite", ".sqlite3", ".db", ".bak", ".zip", ".7z", ".rar", ".csv", ".log",
}


def _allowed_tree_file(relative: Path) -> bool:
    label = relative.as_posix()
    if label in SKIP_FILES or any(part in SKIP_NAMES for part in relative.parts):
        return False
    if relative.suffix.lower() in SKIP_SUFFIXES or relative.name.startswith("."):
        return False
    if relative.parts[0] == "docs":
        return label in SAFE_DOCS
    if relative.parts[0] == "codex-inbox":
        return relative.name == "README.md"
    if relative.parts[0] == "native" and relative.name.lower().startswith(("probe", "private_probe")):
        return False
    if relative.parts[0] == "tools" and relative.name.lower().startswith(("probe", "run_probe", "build_probe")):
        return False
    return relative.suffix.lower() in {".py", ".ps1", ".cpp", ".c", ".h", ".json", ".md", ".txt", ".yml", ".yaml"}


def _source_files():
    paths = {ROOT / name for name in ROOT_FILES}
    for dirname in (".github", "wxbot_framework", "plugins", "tests", "tools", "native", "docs", "codex-inbox"):
        directory = ROOT / dirname
        if directory.exists():
            for path in directory.rglob("*"):
                if path.is_file() and _allowed_tree_file(path.relative_to(ROOT)):
                    paths.add(path)
    return sorted(path for path in paths if path.is_file())


def _public_copy(relative: Path, raw: bytes) -> bytes:
    """Remove the opt-in private sender target from the public C++ source copy."""
    if relative.as_posix() != "native/text_sender_42.cpp":
        return raw
    text = raw.decode("utf-8")
    text = re.sub(r"\n#ifdef WX_PRIVATE_PROBE\n#include \"private_probe_target\.generated\.h\"\n#endif\n", "\n", text)
    text = re.sub(r"\n#ifdef WX_PRIVATE_PROBE\n.*?\n#endif\n", "\n", text, flags=re.DOTALL)
    return text.encode("utf-8")


def _private_markers():
    markers = set()

    def add(value):
        if isinstance(value, str) and len(value.strip()) >= 5:
            markers.add(value.strip())

    def read_json(path):
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None

    bot = read_json(ROOT / "bot_config.json") or {}
    for key in ("wechat_account", "uia_chat_name"):
        add(bot.get(key))
    for value in bot.get("chat_allowlist", []):
        add(value)

    members = read_json(ROOT / "members_config.json") or {}
    groups = members.get("groups", {})
    if isinstance(groups, dict):
        for room, people in groups.items():
            add(room)
            if isinstance(people, list):
                for person in people:
                    if isinstance(person, dict):
                        for key in ("wxid", "name", "display_name"):
                            add(person.get(key))
                        for alias in person.get("aliases", []):
                            add(alias)
    labels = members.get("group_labels", {})
    if isinstance(labels, dict):
        for label in labels.values():
            add(label)

    for filename in ("llm_config.json", "llm_router.json"):
        data = read_json(ROOT / filename) or {}
        add(data.get("api_key"))
        add(data.get("base_url"))
        routes = data.get("routes", [])
        if isinstance(routes, list):
            for route in routes:
                if isinstance(route, dict):
                    add(route.get("api_key"))
                    add(route.get("base_url"))

    private_group = read_json(ROOT / ".runtime" / "target_group.private.json") or {}
    add(private_group.get("group_id"))
    return sorted(markers)


def build(destination: Path):
    destination = destination.resolve()
    if destination == ROOT or ROOT in destination.parents:
        if destination == ROOT:
            raise ValueError("输出目录不能覆盖项目根目录")
    if destination.exists():
        raise FileExistsError("输出目录已存在；请先自行移走或改名后再运行")
    destination.mkdir(parents=True)
    for source in _source_files():
        relative = source.relative_to(ROOT)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_public_copy(relative, source.read_bytes()))

    report_path = destination / "GITHUB_RELEASE_REPORT.md"
    report_path.write_text(
        "# GitHub 发布包检查报告\n\n"
        f"生成日期：{date.today().isoformat()}\n\n"
        "此目录为源代码发布副本。已排除本机配置、运行数据库、日志、备份、截图、"
        "编译二进制、私人探针及带本地状态的研究报告。模型配置仅保留示例占位值。\n\n"
        "发布检查：见本文件最后的扫描统计。\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(ROOT))
    from tools.check_publication import inspect

    result = inspect(destination, _private_markers())
    if not result["passed"]:
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    with report_path.open("a", encoding="utf-8") as output:
        output.write(f"\n- 文件扫描数：{result['files_scanned']}\n")
        output.write(f"- 扫描字节数：{result['bytes_scanned']}\n")
        output.write("- 密钥、私有标记、个人绝对路径及运行产物检查：通过\n")
    # Recheck the final report too, then package only the verified folder.
    final = inspect(destination, _private_markers())
    if not final["passed"]:
        raise RuntimeError(json.dumps(final, ensure_ascii=False))
    archive = destination.parent / "wx-helper-github-clean.zip"
    if archive.exists():
        raise FileExistsError("ZIP 已存在；请先自行移走或改名后再运行")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        for path in sorted(destination.rglob("*")):
            if path.is_file():
                zipped.write(path, Path(destination.name) / path.relative_to(destination))
    return {"folder": str(destination), "archive": str(archive), **final}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "release" / "github-clean")
    args = parser.parse_args()
    try:
        result = build(args.output)
    except Exception as error:
        print(json.dumps({"passed": False, "error": str(error)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2))
