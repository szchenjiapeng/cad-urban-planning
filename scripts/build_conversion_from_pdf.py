#!/usr/bin/env python
"""维护工具：从《国土空间调查、规划、用途管制用地分类新老代码对照表》PDF
抽取「新代码 ↔ 对应老代码」，生成 assets/config/conversion.gb50137_to_2023.json。

用法：
    python build_conversion_from_pdf.py --pdf 新老代码对照表.pdf \\
        --out ../assets/config/conversion.gb50137_to_2023.json

不参与运行时 CLI。运行时由 land_area_report.py --convert-to 2023 读取生成的 JSON。

依赖：pdfplumber（pip install pdfplumber）。没有该库时退回 pypdf 纯文本解析，并提示需人工抽查。
规则：
- 新代码取整行里第一个纯数字代码（2/4/6/8 位分别对应一/二/三/四级类）；
- 「对应老代码」列可能写成 R1/R2/R3、Rax、E2/Ej、A33a 等，按分隔符拆成单个代码；
- 括号里的限定语（如「不含 Rax、Raj」）保留在 old_raw 里，只解析括号外的代码；
- 同一老代码对应多个新代码时，取层级更深（代码更长）的那个，并保留候选便于复核。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

CODE_RE = re.compile(r"^\d{2,8}$")
OLD_CODE_RE = re.compile(r"^[A-Za-z]{1,2}\d{0,2}[a-z]?$")
SPLIT_RE = re.compile(r"[/／、,，\s]+")
TAIL_RE = re.compile(r"[（(][^）)]*[）)]")


def _clean(value: Optional[str]) -> str:
    return (value or "").replace("\n", "").replace("~", "").strip()


def _parse_old(raw: str) -> List[str]:
    """解析「对应老代码」单元格：'R1/R2/R3/Ra（不含 Rax、Raj）' → [R1,R2,R3,Ra]。"""
    text = _clean(raw)
    if not text:
        return []
    text = text.replace("★", "").replace("☆", "")
    body = TAIL_RE.sub("", text)
    codes: List[str] = []
    for token in SPLIT_RE.split(body):
        token = token.strip().strip("；;）。")
        if not token or not OLD_CODE_RE.match(token):
            continue
        if token not in codes:
            codes.append(token)
    return codes


def _extract_with_pdfplumber(pdf: Path) -> List[Dict]:
    import pdfplumber  # 延迟导入：缺库时给更明确的提示

    records: List[Dict] = []
    with pdfplumber.open(str(pdf)) as doc:
        for page in doc.pages:
            for table in page.extract_tables():
                if not table or len(table[0]) < 8:
                    continue
                for row in table:
                    cells = [_clean(c) for c in row]
                    code = ""
                    for cell in cells[:7]:
                        if CODE_RE.match(cell):
                            code = cell
                            break
                    name = cells[7] if len(cells) > 7 else ""
                    old_raw = cells[9] if len(cells) > 9 else ""
                    if not code and not name and not old_raw:
                        continue
                    if code:
                        records.append({"new": code, "name": name, "old_raw": old_raw})
                    elif old_raw and records and not records[-1]["old_raw"]:
                        # 记录被分页切断时，老代码可能落在下一页首行
                        records[-1]["old_raw"] = old_raw
    return records


def _extract_with_pypdf(pdf: Path) -> List[Dict]:
    from pypdf import PdfReader

    text = "\n".join((p.extract_text() or "") for p in PdfReader(str(pdf)).pages)
    records: List[Dict] = []
    for line in text.split("\n"):
        line = line.strip()
        match = re.match(r"^(\d{2,8})\s*(\S*)", line)
        if not match:
            continue
        code, name = match.group(1), match.group(2)
        tokens = line.split()
        old_raw = tokens[-1] if tokens and OLD_CODE_RE.match(tokens[-1]) else ""
        records.append({"new": code, "name": name, "old_raw": old_raw})
    return records


def _resolve(candidates: List[Dict]) -> Dict:
    """同一老代码对应多个新代码时的收敛规则。

    - 若候选之间是上下位关系（如 07 与 0701），保留最深的一个；
    - 若候选互不相关（如 E2 同时对应耕地、园地、林地……），判为歧义，运行时不换算。
    """
    ordered = sorted(candidates, key=lambda c: len(c["new"]), reverse=True)
    keep: List[Dict] = []
    for cand in ordered:
        if any(k["new"].startswith(cand["new"]) for k in keep):
            continue
        keep.append(cand)
    if len(keep) == 1:
        return {"new": keep[0]["new"], "new_name": keep[0]["new_name"], "confidence": "exact"}
    return {
        "new": None,
        "new_name": "",
        "confidence": "ambiguous",
        "candidates": [f"{c['new']} {c['new_name']}" for c in keep],
    }


def build(records: List[Dict]) -> Dict:
    pairs: List[Dict] = []
    seen_new = set()
    for rec in records:
        code = rec["new"]
        if code in seen_new:
            continue
        seen_new.add(code)
        old_codes = _parse_old(rec["old_raw"])
        pairs.append(
            {
                "new": code,
                "new_name": rec["name"],
                "level": max(1, len(code) // 2),
                "old": old_codes,
                "old_raw": rec["old_raw"],
            }
        )

    buckets: Dict[str, List[Dict]] = {}
    for pair in pairs:
        for old in pair["old"]:
            buckets.setdefault(old.upper(), []).append(
                {"new": pair["new"], "new_name": pair["new_name"], "from": old}
            )

    old_to_new: Dict[str, Dict] = {}
    for key, cands in sorted(buckets.items()):
        resolved = _resolve(cands)
        resolved["from"] = cands[0]["from"]
        old_to_new[key] = resolved

    ambiguous = {k: v for k, v in old_to_new.items() if v["confidence"] == "ambiguous"}
    unresolved = [p for p in pairs if not p["old"]]
    return {
        "from": "gb50137",
        "to": "2023",
        "source": "《国土空间调查、规划、用途管制用地分类新老代码对照表》（征求意见稿）",
        "note": (
            "自动抽取自 PDF，供 --convert-to 2023 使用。"
            "上下位候选（如 07 与 0701）取最深者；互不相关的候选（如 E2 同时对应耕地/园地/林地）"
            "判为歧义，运行时不换算而是列入待确认，避免乱归。"
            "该对照表中的 0704 商住混合用地 与正式版指南的 0704 农村社区服务设施用地 冲突，"
            "本技能不做替换，商住混合仍按扩展节点 0709 处理。"
        ),
        "pairs": [p for p in pairs if p["old"]],
        "ambiguous_old_codes": ambiguous,
        "unresolved_new_codes": [
            {"new": p["new"], "new_name": p["new_name"], "old_raw": p["old_raw"]} for p in unresolved
        ],
        "old_to_new": old_to_new,
    }


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="从新老代码对照表 PDF 生成转换表")
    parser.add_argument("--pdf", required=True, help="对照表 PDF 路径")
    parser.add_argument("--out", required=True, help="输出 conversion JSON 路径")
    parser.add_argument("--engine", default="auto", choices=["auto", "pdfplumber", "pypdf"])
    args = parser.parse_args()

    pdf = Path(args.pdf)
    engine = args.engine
    if engine == "auto":
        try:
            import pdfplumber  # noqa: F401

            engine = "pdfplumber"
        except Exception:
            engine = "pypdf"

    records = _extract_with_pdfplumber(pdf) if engine == "pdfplumber" else _extract_with_pypdf(pdf)
    if not records:
        raise SystemExit("未从 PDF 解析到任何代码行，请检查是否为扫描版或调整 --engine")

    payload = build(records)
    payload["engine"] = engine
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"引擎：{engine}；解析记录 {len(records)} 条")
    print(f"有老代码对应的新代码：{len(payload['pairs'])} 条；无对应：{len(payload['unresolved_new_codes'])} 条")
    print(
        f"老代码条目：{len(payload['old_to_new'])} 个；歧义 {len(payload['ambiguous_old_codes'])} 个"
    )
    print(f"已写入：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
