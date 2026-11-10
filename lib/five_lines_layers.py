"""五线图层候选：红线复用 `line_layers` 打分，其余按用地代码 + 图层关键词。

硬性：五条线都涉及「确定图层」，线条/文字没有字段映射表可依，
本模块只产出候选，实际指派必须经用户确认（`--confirm-layers`）或 `--layer-map` 显式给出。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from lib import landuse, line_layers


def _keyword_hits(visible: Sequence[str], keywords: Sequence[str]) -> List[str]:
    found: List[str] = []
    for layer in visible:
        name = str(layer or "").lower()
        if not name:
            continue
        for keyword in keywords or []:
            key = str(keyword or "").lower()
            if key and key in name:
                found.append(str(layer))
                break
    return found


def _dedupe(rows: List[Dict]) -> List[Dict]:
    seen = set()
    result: List[Dict] = []
    for row in rows:
        layer = row.get("layer")
        if not layer or layer in seen:
            continue
        seen.add(layer)
        result.append(row)
    result.sort(key=lambda item: (-int(item.get("score", 0)), str(item.get("layer"))))
    return result


def candidates(visible: Sequence[str], classifier, spec: Dict, standard: str) -> Dict[str, List[Dict]]:
    """返回 {线名: [{layer, score, reason}]}，供确认项展示。"""
    config = (spec.get("five_lines") or {})
    keywords = config.get("keywords") or {}
    codes = (config.get("codes") or {}).get(standard) or {}

    red_rows = [
        {"layer": layer, "score": score, "reason": "道路红线命名"}
        for layer, score in line_layers.candidates(visible, spec).get("road_redline", [])
    ]

    def by_codes(code_list, extra_keywords, reason: str) -> List[Dict]:
        rows: List[Dict] = []
        if code_list:
            try:
                matched = landuse.select_layers_by_code(visible, classifier, spec, list(code_list))
            except Exception:
                matched = []
            for layer in matched:
                rows.append({"layer": layer, "score": 10, "reason": f"{reason}（code {','.join(code_list)}）"})
        for layer in _keyword_hits(visible, extra_keywords or []):
            rows.append({"layer": layer, "score": 5, "reason": f"{reason}（图层名）"})
        return rows

    result: Dict[str, List[Dict]] = {
        "红线": _dedupe(red_rows),
        "绿线": _dedupe(by_codes(codes.get("green"), keywords.get("green"), "绿地类用地")),
        "蓝线": _dedupe(by_codes(codes.get("blue"), keywords.get("blue"), "水域/河道")),
        "黄线": _dedupe(by_codes(codes.get("yellow"), keywords.get("yellow"), "公用设施")),
        "紫线": _dedupe(by_codes(None, keywords.get("purple"), "历史保护")),
        "基础设施": _dedupe(
            [{"layer": layer, "score": 5, "reason": "基础设施线命名"} for layer in _keyword_hits(visible, keywords.get("infra") or [])]
        ),
    }
    return result
