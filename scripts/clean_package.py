#!/usr/bin/env python
"""打包前清理与体检：删字节码、统计包体、列出将随包分发的文件。

用法：
    python scripts/clean_package.py            # 清理 __pycache__ / *.pyc 并打印包体统计
    python scripts/clean_package.py --check    # 只检查，发现字节码返回 1（CI 用）
    python scripts/clean_package.py --list     # 额外列出全部将随包分发的文件

为什么要清理：.pyc 是平台相关字节码，随包分发会被装到别的 Python 版本上、
可能加载到与源码不一致的过期代码，还会让杀软对「可执行二进制」报警。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent

# 运行期产物：不该出现在包里的输出文件
RUNTIME_ARTIFACTS = (
    "land_area_confirm.json",
    "land_area_report.json",
    "land_area_report.md",
    "容积率测算表.md",
)

# 测试夹具按需生成，默认不计入包体统计（保留 make_fixtures.py 即可）
EXCLUDE_PREFIXES = ("scripts/tests/fixtures/",)

# 这些目录永远不进包：git 历史、历史构建产物、字节码缓存
SKIP_DIRS = {".git", "dist", "__pycache__", ".pytest_cache", ".idea", ".vscode"}


def is_skipped(rel: str) -> bool:
    """按路径段判断是否属于打包排除项（.git/、dist/ 等）。"""
    parts = [part for part in rel.split("/") if part]
    return any(part in SKIP_DIRS for part in parts[:-1])


def _bytecode() -> tuple[list[Path], list[Path]]:
    dirs = sorted(p for p in SKILL_DIR.rglob("__pycache__") if p.is_dir())
    files = sorted(SKILL_DIR.rglob("*.pyc")) + sorted(SKILL_DIR.rglob("*.pyo"))
    return dirs, files


def _package_files() -> list[Path]:
    out: list[Path] = []
    for path in sorted(SKILL_DIR.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(SKILL_DIR).as_posix()
        if is_skipped(rel) or rel.endswith((".pyc", ".pyo")):
            continue
        out.append(path)
    return out


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB"):
        if value < 1024 or unit == "MB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} MB"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="技能包清理与体积体检")
    parser.add_argument("--check", action="store_true", help="只检查不删除；发现字节码返回 1")
    parser.add_argument("--list", action="store_true", help="列出将随包分发的文件")
    args = parser.parse_args(argv)

    dirs, files = _bytecode()
    if dirs or files:
        if args.check:
            print(f"[FAIL] 发现字节码缓存：{len(dirs)} 个 __pycache__、{len(files)} 个 .pyc/.pyo")
            for path in (dirs + files)[:10]:
                print(f"  - {path.relative_to(SKILL_DIR)}")
            print("请运行：python scripts/clean_package.py")
            return 1
        for directory in dirs:
            shutil.rmtree(directory, ignore_errors=True)
        for path in files:
            try:
                path.unlink()
            except OSError:
                pass
        print(f"[清理] 已删除 {len(dirs)} 个 __pycache__、{len(files)} 个字节码文件")

    packages = _package_files()
    total = sum(p.stat().st_size for p in packages)
    included = [
        p for p in packages
        if not p.relative_to(SKILL_DIR).as_posix().startswith(EXCLUDE_PREFIXES)
    ]
    included_size = sum(p.stat().st_size for p in included)

    buckets: dict[str, list[int]] = {}
    for path in packages:
        buckets.setdefault(path.suffix.lower() or "(无扩展名)", []).append(path.stat().st_size)
    print(f"[包体] 全部文件 {len(packages)} 个 / {_human(total)}；"
          f"计入分发 {len(included)} 个 / {_human(included_size)}")
    for suffix, sizes in sorted(buckets.items(), key=lambda kv: -sum(kv[1])):
        print(f"  {suffix:<8} {len(sizes):>4} 个  {_human(sum(sizes))}")

    drift = [name for name in RUNTIME_ARTIFACTS if (SKILL_DIR / name).exists()]
    if drift:
        print(f"[提示] 技能目录残留运行期产物：{drift}（已列入 .gitignore，打包时请排除）")

    if args.list:
        print("\n将随包分发的文件：")
        for path in included:
            print(f"  {path.relative_to(SKILL_DIR).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
