#!/usr/bin/env python
"""按当前版本生成可分发的技能包，并在打包前做一遍完整性自检。

为什么需要它：手工压缩极容易漏文件（漏了 `up.py` 或 `lib/` 就会装上一个跑不起来的技能），
也容易把 `__pycache__`、`.git`、旧 dist 一起塞进去。

用法：
    python scripts/build_package.py            # 自检 → 生成 dist/cad-urban-planning-<version>.zip
    python scripts/build_package.py --check    # 只自检，不打包（CI 用）
    python scripts/build_package.py --include-fixtures   # 连同回归测试 DXF 一起打包

自检项：
1. SKILL.md 里引用的每个 `references/*.md`、`scripts/**.py` 必须真实存在；
2. 包内不得有 `__pycache__` / `*.pyc` / `.git` / `dist`；
3. 全部 `.py` 必须能编译（用 compile()，不落盘字节码）。
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path
from typing import List, Tuple

SKILL_DIR = Path(__file__).resolve().parent.parent
SKILL_MD = SKILL_DIR / "SKILL.md"
DIST_DIR = SKILL_DIR / "dist"
PACKAGE_NAME = "cad-urban-planning"

SKIP_DIRS = {".git", "dist", "__pycache__", ".pytest_cache", ".idea", ".vscode", ".vs", "tests"}
SKIP_SUFFIXES = {".pyc", ".pyo"}
SKIP_NAMES = {".DS_Store", "Thumbs.db"}
# 默认不打包的路径（可由 --include-fixtures 打开）
FIXTURE_PREFIX = "tests/fixtures/"

_REF_PATTERN = re.compile(r"references/([A-Za-z0-9_\-]+\.md)")
_SCRIPT_PATTERN = re.compile(r"scripts/((?:[A-Za-z0-9_\-]+/)*[A-Za-z0-9_\-]+\.py)")


def read_version() -> str:
    """从 SKILL.md frontmatter 读 version；读不到就返回 0.0.0（不阻断打包）。"""
    try:
        text = SKILL_MD.read_text(encoding="utf-8")
    except OSError:
        return "0.0.0"
    if not text.startswith("---"):
        return "0.0.0"
    block = text.split("---", 2)[1]
    match = re.search(r"^version:\s*([0-9][0-9A-Za-z.\-+]*)", block, re.MULTILINE)
    return match.group(1) if match else "0.0.0"


def collect(include_fixtures: bool = False) -> Tuple[List[Path], List[str]]:
    """收集应打包文件，返回 (文件列表, 跳过说明)。"""
    files: List[Path] = []
    skipped: List[str] = []
    for path in sorted(SKILL_DIR.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(SKILL_DIR).as_posix()
        parts = [part for part in rel.split("/") if part]
        if any(part in SKIP_DIRS for part in parts[:-1]):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES or path.name in SKIP_NAMES:
            continue
        # 点文件（.gitignore 等）只服务本地开发，不进安装包
        if path.name.startswith("."):
            continue
        # 只跳过夹具里的 DXF（体积大、可由 make_fixtures.py 现场重建）；
        # make_fixtures.py 必须随包分发，否则 SKILL.md 对它的引用会断。
        if not include_fixtures and rel.startswith(FIXTURE_PREFIX) and path.suffix.lower() in {".dxf", ".dwg"}:
            skipped.append(rel)
            continue
        # 运行期产物：技能目录里出现说明是本地跑出来的，不该随包分发
        if rel in {"land_area_confirm.json", "land_area_report.json", "land_area_report.md", "容积率测算表.md"}:
            skipped.append(rel)
            continue
        if rel.startswith("dist/"):
            continue
        files.append(path)
    return files, skipped


def verify(files: List[Path]) -> List[str]:
    """返回问题清单；空列表表示可以打包。"""
    problems: List[str] = []
    relative = {path.relative_to(SKILL_DIR).as_posix() for path in files}

    if "SKILL.md" not in relative:
        problems.append("包内缺少 SKILL.md（技能入口）")
    else:
        text = SKILL_MD.read_text(encoding="utf-8")
        for name in sorted(set(_REF_PATTERN.findall(text))):
            if f"references/{name}" not in relative:
                problems.append(f"SKILL.md 引用了不存在的文档：references/{name}")
        for name in sorted(set(_SCRIPT_PATTERN.findall(text))):
            if f"scripts/{name}" not in relative:
                problems.append(f"SKILL.md 引用了不存在的脚本：scripts/{name}")

    for rel in sorted(relative):
        parts = rel.split("/")
        if "__pycache__" in parts or rel.endswith((".pyc", ".pyo")):
            problems.append(f"包内混入字节码：{rel}（运行 python scripts/clean_package.py）")
        if parts[0] in {".git", "dist"}:
            problems.append(f"包内混入构建目录：{rel}")
        # 平台只支持「根目录/二级目录/文件」，出现三级目录会被上传拒绝
        if rel.count("/") > 1:
            problems.append(f"目录层级超限（平台仅支持两级目录）：{rel}")

    for path in files:
        if path.suffix != ".py":
            continue
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except SyntaxError as exc:
            problems.append(f"语法错误：{path.relative_to(SKILL_DIR).as_posix()}:{exc.lineno} {exc.msg}")
        except UnicodeDecodeError:
            problems.append(f"非 UTF-8 编码：{path.relative_to(SKILL_DIR).as_posix()}")
    return problems


def build(files: List[Path], version: str) -> Path:
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    target = DIST_DIR / f"{PACKAGE_NAME}-{version}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            rel = path.relative_to(SKILL_DIR).as_posix()
            archive.write(path, f"{PACKAGE_NAME}/{rel}")
    return target


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="技能包自检与打包")
    parser.add_argument("--check", action="store_true", help="只自检，不生成 zip")
    parser.add_argument("--include-fixtures", action="store_true", help="把回归测试 DXF 一起打包")
    args = parser.parse_args(argv)

    version = read_version()
    files, skipped = collect(args.include_fixtures)
    problems = verify(files)

    total = sum(path.stat().st_size for path in files)
    print(f"[信息] 版本 {version}；待打包 {len(files)} 个文件 / {total / 1024:.0f} KB")
    if skipped:
        print(f"[跳过] {len(skipped)} 个文件（测试夹具/运行期产物），可用 --include-fixtures 纳入")

    if problems:
        print(f"[FAIL] 自检发现 {len(problems)} 个问题：")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print("[PASS] 自检通过：引用完整、无字节码、无语法错误")
    if args.check:
        return 0

    target = build(files, version)
    size_kb = target.stat().st_size / 1024
    print(f"[完成] 已生成 {target.relative_to(SKILL_DIR).as_posix()}（{size_kb:.0f} KB，{len(files)} 个文件）")
    print("       安装前建议先解压到临时目录，跑一次 tests/test_docs.py 验证引用完整。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
