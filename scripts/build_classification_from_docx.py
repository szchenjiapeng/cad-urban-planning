#!/usr/bin/env python
"""维护工具：从 Word 原文抽取用地分类表，生成 assets/config/classification.*.json。

支持两种版式：
- `--layout 2023`：《国土空间调查、规划、用途管制用地用海分类指南（2023）》表 2.1，
  列为 一级类代码/名称、二级类代码/名称、三级类代码/名称；
- `--layout gb50137`：《城市用地分类与规划建设用地标准》GB 50137-2011，
  列为 大类/中类/小类代码、名称、内容（合并单元格，行内单元格数 3/4/5 不等）。

用法：
    python build_classification_from_docx.py --layout 2023 --docx 指南.docx \\
        --out ../assets/config/classification.2023.json --keep-extras
    python build_classification_from_docx.py --layout gb50137 --docx GB50137.docx \\
        --out ../assets/config/classification.gb50137.json

不参与运行时 CLI，只在分类表需要校核/重建时手动执行。
规则：
- 合并单元格用「向下填充」还原上级归属；
- 只认分类表，不从正文或附录推测代码，避免杜撰；
- 抽取后打印各级条目数，与原文声明比对，差异只提示不静默修正。
"""

from __future__ import annotations

import argparse
import json
import re as _re
import sys
import zipfile
from pathlib import Path
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET

NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
TITLE = "国土空间调查、规划、用途管制用地用海分类指南（2023）"
CAVEAT = (
    "本表由官方原文表 2.1 抽取，代码与名称以自然资源部发布的原文件为准。"
    "0709 商住混合用地为使用者扩展节点（官方表未列），已在 source 字段标注。"
    "维护方式：更新原文后重跑 scripts/build_classification_from_docx.py。"
)
GB_LAYOUTS = {
    "title": "城市用地分类与规划建设用地标准 GB 50137-2011",
    "source": "《城市用地分类与规划建设用地标准》GB 50137-2011 表 2.0.1（城市建设用地 R/A/B/M/W/S/U/G 部分）",
    "caveat": (
        "本表由 GB 50137-2011 原文表 2.0.1 抽取，含大类/中类/小类。"
        "H、E 两类城乡用地不在城市建设用地平衡口径内，未纳入，需要时自行补节点。"
        "维护方式：更新原文后重跑 scripts/build_classification_from_docx.py --layout gb50137。"
    ),
}


def _cell_text(tc) -> str:
    return "".join(t.text or "" for t in tc.findall(".//w:t", NS)).strip()


def _table_rows(tbl) -> List[List[str]]:
    rows: List[List[str]] = []
    for tr in tbl.findall("w:tr", NS):
        rows.append([_cell_text(tc) for tc in tr.findall("w:tc", NS)])
    return rows


def _find_main_table(path: Path) -> List[List[str]]:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    tables = root.findall(".//w:tbl", NS)
    best: Optional[List[List[str]]] = None
    for tbl in tables:
        rows = _table_rows(tbl)
        if not rows:
            continue
        header = " ".join(rows[0])
        # 目标是含「一级类/二级类/三级类」三列表头的表
        if "一级类" in header and "二级类" in header and "三级类" in header:
            if best is None or len(rows) > len(best):
                best = rows
    if best is None:
        raise SystemExit("未在文档中找到含一级类/二级类/三级类表头的表格，请确认原文未被改动")
    return best


def parse_nodes(rows: List[List[str]]) -> List[Dict]:
    nodes: List[Dict] = []
    seen: Dict[str, Dict] = {}

    lv1_code = lv1_name = ""
    lv2_code = lv2_name = ""

    for raw in rows:
        cells = list(raw) + [""] * (6 - len(raw))
        c1, n1, c2, n2, c3, n3 = (cells[0].strip(), cells[1].strip(), cells[2].strip(),
                                 cells[3].strip(), cells[4].strip(), cells[5].strip())
        if c1 in {"代码", "一级类"} or n1 in {"名称"}:
            continue

        if c1:
            lv1_code, lv1_name = c1, n1
            lv2_code = lv2_name = ""
            _add(nodes, seen, lv1_code, lv1_name, 1, None)
        if c2:
            if not lv1_code:
                continue
            lv2_code, lv2_name = c2, n2
            _add(nodes, seen, lv2_code, lv2_name, 2, lv1_code)
        if c3:
            if not lv2_code:
                continue
            _add(nodes, seen, c3, n3, 3, lv2_code)
    return nodes


def _add(nodes: List[Dict], seen: Dict[str, Dict], code: str, name: str, level: int, parent: Optional[str]) -> None:
    if not code or not name:
        return
    if code in seen:
        return
    node = {"code": code, "name": name, "level": level, "parent": parent, "in_balance": True}
    seen[code] = node
    nodes.append(node)


def _find_gb_tables(path: Path) -> List[List[str]]:
    """GB 50137 的分类表按大类拆成多张，大/中/小类表头一致，按文档顺序拼接。"""
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    rows: List[List[str]] = []
    for tbl in root.findall(".//w:tbl", NS):
        part = _table_rows(tbl)
        if not part:
            continue
        head = " ".join(part[0] if part else []) + " " + " ".join(part[1] if len(part) > 1 else [])
        # 分类表表头固定为「类别代码 / 类别名称 / 内容」+「大类 / 中类 / 小类」，
        # 用这两组条件同时命中，避免把后面的对照表、面积指标表误当成分类表
        if "类别代码" in head and "类别名称" in head and "大类" in head and "中类" in head and "小类" in head:
            rows.extend(part)
    if not rows:
        raise SystemExit("未找到含「类别代码/类别名称/内容」与「大类/中类/小类」表头的分类表，请确认原文未被改动")
    return rows


def parse_gb50137(rows: List[List[str]]) -> List[Dict]:
    """GB 50137 表 2.0.1：同一行可能有 3/4/5 个单元格（合并单元格数不同）。"""
    nodes: List[Dict] = []
    seen: Dict[str, Dict] = {}
    lv1_code = lv1_name = ""
    lv2_code = lv2_name = ""

    for raw in rows:
        cells = [c.strip() for c in raw]
        # 表头：类别代码 / 大类 / 中类 / 小类 / 内容
        if any(c in {"类别代码", "大类", "中类", "小类"} for c in cells[:3]) or not any(cells):
            continue
        if len(cells) >= 5:
            c1, c2, c3, name = cells[0], cells[1], cells[2], cells[3]
        elif len(cells) == 4:
            c1, c2, c3, name = "", cells[0], cells[1], cells[2]
        elif len(cells) == 3:
            c1, c2, c3, name = cells[0], "", "", cells[1]
        else:
            continue
        if not name:
            continue
        # 只要 R/A/B/M/W/S/U/G 八大类，H/E 属城乡用地，不进城市建设用地平衡表
        if c1 and c1 not in {"R", "A", "B", "M", "W", "S", "U", "G"}:
            continue
        # 代码形态校验：中类=字母+1~2位数字，小类=字母+2位数字，形态不符的一律丢弃
        if c1 and not _re.match(r"^[A-Z]$", c1):
            continue
        if not c1 and c2 and not _re.match(r"^[A-Z]\d{1,2}$", c2):
            continue
        if not c1 and not c2 and c3 and not _re.match(r"^[A-Z]\d{2}$", c3):
            continue
        if c1:
            lv1_code, lv1_name = c1, name
            lv2_code = lv2_name = ""
            _add(nodes, seen, lv1_code, lv1_name, 1, None)
        elif c2:
            if not lv1_code:
                continue
            lv2_code, lv2_name = c2, name
            _add(nodes, seen, lv2_code, lv2_name, 2, lv1_code)
        elif c3:
            if not lv2_code:
                continue
            _add(nodes, seen, c3, name, 3, lv2_code)
    return nodes


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="从 Word 原文抽取用地分类表生成配置")
    parser.add_argument("--layout", default="2023", choices=["2023", "gb50137"], help="原文版式：2023 指南表 2.1 / GB 50137 表 2.0.1")
    parser.add_argument("--docx", required=True, help="原文 docx 路径（.doc 请先用 Word 另存为 docx）")
    parser.add_argument("--out", required=True, help="输出 classification JSON 路径")
    parser.add_argument("--keep-extras", action="store_true", help="保留输出文件里已有的扩展节点（如 0709）")
    args = parser.parse_args()

    docx = Path(args.docx)
    if args.layout == "gb50137":
        nodes = parse_gb50137(_find_gb_tables(docx))
        spec = {
            "standard": "gb50137",
            "title": GB_LAYOUTS["title"],
            "source": GB_LAYOUTS["source"],
            "caveat": GB_LAYOUTS["caveat"],
            "levels": {"1": "大类", "2": "中类", "3": "小类"},
            "derived_road": {
                "root": "S",
                "code": "S1",
                "name": "城市道路用地",
                "note": "城市道路通常不填充，按「范围总面积 − 其他大类 − 未识别」后扣除 S 下其他中类反算得出",
            },
        }
        expect = "原文表 2.0.1 八大类"
    else:
        nodes = parse_nodes(_find_main_table(docx))
        spec = {
            "standard": "2023",
            "title": TITLE,
            "source": "自然资源部《国土空间调查、规划、用途管制用地用海分类指南》（2023年11月）表 2.1",
            "caveat": CAVEAT,
            "levels": {"1": "一级类", "2": "二级类", "3": "三级类"},
            # 道路反算口径：12 交通运输用地 → 1207 城镇村道路用地（2023 版编号）
            "derived_road": {
                "root": "12",
                "code": "1207",
                "name": "城镇村道路用地",
                "note": "城镇道路通常不填充，按「范围总面积 − 其他一级类 − 未识别」后扣除 12 下其他二级类反算得出",
            },
        }
        expect = "原文 §2.1 声明 24/113/140"

    lv1 = [n for n in nodes if n["level"] == 1]
    lv2 = [n for n in nodes if n["level"] == 2]
    lv3 = [n for n in nodes if n["level"] == 3]
    print(f"抽取结果：一级 {len(lv1)} 个、二级 {len(lv2)} 个、三级 {len(lv3)} 个（{expect}）")

    out_path = Path(args.out)
    extras: List[Dict] = []
    if args.keep_extras and out_path.exists():
        old = json.loads(out_path.read_text(encoding="utf-8"))
        codes = {n["code"] for n in nodes}
        extras = [n for n in old.get("nodes", []) if n.get("source") and n["code"] not in codes]
        for node in extras:
            nodes.append(node)
        if extras:
            print("保留扩展节点：" + "、".join(f"{n['code']} {n['name']}" for n in extras))

    payload = dict(spec)
    payload["nodes"] = nodes
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写入：{out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
