"""维护工具：登记「新增用地代码」，同时更新分类配置与指引表。

出现「形如分类代码、但分类表里没有登记」的图层时（如 0809 / 0909 / 090201），
先向用户确认名称是否写错；名称无误且同意新增时，用本工具登记：

    python scripts/add_classification_node.py --code 0909 --name 商业服务业混合用地 --level 2 --parent 09

会做两件事：
1. 在 assets/config/classification.<standard>.json 的 nodes 中插入节点（已存在则更新）；
2. 在 references/landuse-2023.md 对应的二级类表格中插入一行（--no-reference 可跳过）。

登记后图层映射即可把该代码当作正式类目使用，平衡表会单独列出该类。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Tuple

_SCRIPTS = Path(__file__).resolve().parent.parent
_SKILL = _SCRIPTS
CONFIG_DIR = _SKILL / "assets"
REFERENCE_FILES = {
    "2023": _SKILL / "references" / "landuse-2023.md",
    "gb50137": _SKILL / "references" / "landuse-gb50137.md",
}
CONFIG_FILES = {
    "2023": CONFIG_DIR / "classification.2023.json",
    "gb50137": CONFIG_DIR / "classification.gb50137.json",
}


def add_node_to_config(
    path: Path,
    code: str,
    name: str,
    level: int,
    parent: Optional[str],
    source: str = "",
    in_balance: bool = True,
) -> str:
    """插入/更新分类节点，返回 'added' / 'updated'。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    nodes: List[dict] = data.setdefault("nodes", [])
    node = {
        "code": code,
        "name": name,
        "level": level,
        "parent": parent,
        "in_balance": in_balance,
    }
    if source:
        node["source"] = source

    for index, item in enumerate(nodes):
        if str(item.get("code")) == code:
            merged = dict(item)
            merged.update(node)
            nodes[index] = merged
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return "updated"

    # 作为 parent 的最后一个子节点插入；找不到 parent 时按代码顺序插入
    insert_at = len(nodes)
    if parent:
        last_child = -1
        for index, item in enumerate(nodes):
            item_code = str(item.get("code", ""))
            if item_code.startswith(parent) and len(item_code) > len(parent):
                last_child = index
        if last_child >= 0:
            insert_at = last_child + 1
        else:
            for index, item in enumerate(nodes):
                if str(item.get("code", "")) > code:
                    insert_at = index
                    break
    else:
        for index, item in enumerate(nodes):
            if str(item.get("code", "")) > code:
                insert_at = index
                break
    nodes.insert(insert_at, node)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return "added"


def _table_bounds(lines: List[str], start: int) -> Optional[Tuple[int, int]]:
    """从 start 起找「| 代码 | 名称 |」表头，返回 (表头行, 表格结束行)。"""
    header = None
    for index in range(start, len(lines)):
        text = lines[index].strip()
        if text.startswith("### ") and index > start:
            return None
        if text.startswith("|") and "代码" in text and "名称" in text:
            header = index
            break
    if header is None:
        return None
    end = header + 1
    while end < len(lines) and lines[end].strip().startswith("|"):
        end += 1
    return header, end


def add_node_to_reference(path: Path, code: str, name: str, parent: Optional[str]) -> str:
    """在指引表的二级类表格里插入一行，返回 'added' / 'updated' / 'skipped'。"""
    if not path.exists() or not parent:
        return "skipped"
    lines = path.read_text(encoding="utf-8").splitlines()
    start = None
    for index, text in enumerate(lines):
        if text.strip().startswith(f"### {parent}") or text.strip().startswith(f"### {parent} "):
            start = index
            break
    bounds = _table_bounds(lines, start) if start is not None else None
    if bounds is None:
        return "skipped"
    header, end = bounds
    row = f"| {code} | {name} |"
    for index in range(header + 2, end):  # 跳过分隔行
        cell = lines[index].strip().strip("|").split("|")[0].strip()
        if cell == code:
            lines[index] = row
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return "updated"
        if cell and cell.isdigit() and len(cell) == len(code) and cell > code:
            lines.insert(index, row)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return "added"
    lines.insert(end, row)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return "added"


def register(
    code: str,
    name: str,
    level: int,
    parent: Optional[str],
    standard: str = "2023",
    config_path: Optional[Path] = None,
    reference_path: Optional[Path] = None,
    source: str = "",
    update_reference: bool = True,
) -> dict:
    """登记节点；供主流程 --add-code 与命令行共用。"""
    config = Path(config_path) if config_path else CONFIG_FILES.get(standard)
    if config is None or not config.exists():
        raise FileNotFoundError(f"分类配置不存在：{config}")
    result = {
        "code": code,
        "name": name,
        "level": level,
        "parent": parent,
        "config": str(config),
        "config_action": add_node_to_config(config, code, name, level, parent, source=source),
        "reference": "",
        "reference_action": "skipped",
    }
    if update_reference:
        reference = Path(reference_path) if reference_path else REFERENCE_FILES.get(standard)
        if reference is not None:
            result["reference"] = str(reference)
            result["reference_action"] = add_node_to_reference(reference, code, name, parent)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="登记新增用地代码（同步分类配置与指引表）")
    parser.add_argument("--code", required=True, help="用地代码，如 0909")
    parser.add_argument("--name", required=True, help="用地名称，如 商业服务业混合用地")
    parser.add_argument("--level", type=int, default=None, help="层级 1/2/3，缺省按代码长度推断")
    parser.add_argument("--parent", default=None, help="上级代码，如 09；缺省按代码前缀推断")
    parser.add_argument("--standard", default="2023", choices=["2023", "gb50137"])
    parser.add_argument("--config", help="自定义分类配置路径")
    parser.add_argument("--reference", help="自定义指引表路径")
    parser.add_argument("--source", default="", help="备注（如：用户扩展，官方表未列）")
    parser.add_argument("--no-reference", dest="update_reference", action="store_false", default=True)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    code = str(args.code).replace("-", "").strip()
    level = args.level
    parent = args.parent
    if level is None:
        level = 1 if len(code) <= 2 else (2 if len(code) <= 4 else 3)
    if parent is None and level > 1:
        parent = code[: 2 if level == 2 else 4]
    result = register(
        code=code,
        name=args.name,
        level=level,
        parent=parent,
        standard=args.standard,
        config_path=Path(args.config) if args.config else None,
        reference_path=Path(args.reference) if args.reference else None,
        source=args.source,
        update_reference=args.update_reference,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
