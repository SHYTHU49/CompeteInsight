"""
初始化 data/ 目录骨架。

首次启动项目前运行：
    python scripts/init_data.py

也会被 backend FastAPI 的 startup 钩子自动调用一次。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

# Windows 默认 GBK，强制 stdout/stderr 用 UTF-8 输出中文与符号
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

# 顶层 data/ 子目录
DATA_SUBDIRS: list[str] = [
    "projects",
    "runs",
    "cache/llm",
    "cache/search",
    "cache/fetch",
    "cache/embedding",
    "skills",
    "prompts",
    "templates",
    "demo_replays",
    "archive",
    "logs",
]

# 仓库根目录下的"源"目录 → data/ 下的"运行时副本"
SYNC_PAIRS: list[tuple[str, str]] = [
    ("skills", "skills"),
    ("prompts", "prompts"),
    ("templates", "templates"),
]


def ensure_dirs() -> None:
    """创建 data/ 下所有子目录。"""
    for sub in DATA_SUBDIRS:
        path = DATA_DIR / sub
        path.mkdir(parents=True, exist_ok=True)
        print(f"  [ok] {path.relative_to(REPO_ROOT)}")


def sync_sources() -> None:
    """把仓库根目录的 skills/ prompts/ templates/ 内容 sync 到 data/。

    保留已有运行时副本里的改动？目前策略：源文件 *.yaml / *.j2 / *.json 是"权威"，
    覆盖运行时副本。前端 Skill Studio 修改的版本带 .vN 后缀单独存储（W8 实现）。
    """
    for src_rel, dst_rel in SYNC_PAIRS:
        src = REPO_ROOT / src_rel
        dst = DATA_DIR / dst_rel
        if not src.exists():
            # 还没建立源目录，跳过（首次初始化时正常）
            continue
        for item in src.rglob("*"):
            if item.is_dir():
                continue
            rel = item.relative_to(src)
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            print(f"  [sync] {src_rel}/{rel} -> data/{dst_rel}/{rel}")


def main() -> int:
    print(f"初始化数据目录: {DATA_DIR}")
    DATA_DIR.mkdir(exist_ok=True)
    ensure_dirs()
    sync_sources()
    print("完成 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
