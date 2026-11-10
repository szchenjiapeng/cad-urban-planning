"""线条类图层识别：命名归一 → 角色打分 → 候选清单 → 指派表读写。

硬性（业务约束）：
- 规划范围线、用地红线、地块线、道路红线、道路中心线**没有字段映射表可依**，
  命名习惯因院而异（中文、拼音全拼、拼音首字母、英文缩写混用），
  因此本模块只产出**候选与打分**，绝不替用户决定；
- 最终指派必须来自 `--layer-map` 显式文件或用户确认（退出码 3）；
- 打分只用于给候选排序：分低不代表不是，分高也不代表就是。
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROLES = ("scope", "parcel_redline", "parcel_line", "road_redline", "road_center")
DEFAULT_LABELS = {
    "scope": "规划范围线",
    "parcel_redline": "用地红线",
    "parcel_line": "地块线（细分）",
    "road_redline": "道路红线",
    "road_center": "道路中心线",
}

_SEPARATORS = re.compile(r"[\s_.·—–－]+")
_REPEATED_DASH = re.compile(r"-{2,}")
_BRACKETS = re.compile(r"\([^)]*\)|\[[^\]]*\]")


def normalize(name: str) -> str:
    """命名归一：全角转半角、转小写、去括号尾注、分隔符统一为 `-`。"""
    text = unicodedata.normalize("NFKC", str(name or "")).strip().lower()
    text = _BRACKETS.sub("", text)
    text = text.replace("／", "/").replace("、", "-")
    text = _SEPARATORS.sub("-", text)
    text = _REPEATED_DASH.sub("-", text)
    return text.strip("-")


def _groups(spec: Dict) -> Dict:
    return spec.get("line_groups") or {}


def _filter(spec: Dict) -> Dict:
    return spec.get("line_filter") or {}


def role_label(spec: Dict, role: str) -> str:
    definition = _groups(spec).get(role) or {}
    return str(definition.get("label") or DEFAULT_LABELS.get(role, role))


def is_excluded(layer: str, spec: Dict) -> bool:
    name = normalize(layer)
    if not name:
        return True
    for keyword in _filter(spec).get("exclude", []) or []:
        key = normalize(keyword)
        if key and key in name:
            return True
    return False


def score_layer(layer: str, spec: Dict) -> Dict[str, int]:
    """给图层在各类线条角色上打分；strong 命中 +10，weak 命中 +3。"""
    if is_excluded(layer, spec):
        return {}
    name = normalize(layer)
    scores: Dict[str, int] = {}
    for role, definition in _groups(spec).items():
        if str(role).startswith("_") or not isinstance(definition, dict):
            continue
        score = 0
        for keyword in definition.get("strong", []) or []:
            key = normalize(keyword)
            if key and key in name:
                score += 10
        for keyword in definition.get("weak", []) or []:
            key = normalize(keyword)
            if key and key in name:
                score += 3
        if score > 0:
            scores[str(role)] = score
    return scores


def best_role(layer: str, spec: Dict) -> Tuple[str, int]:
    """最高分角色；最高分并列时返回 ('', 分数)，表示必须由用户确认。"""
    scores = score_layer(layer, spec)
    if not scores:
        return "", 0
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return "", ranked[0][1]
    return ranked[0][0], ranked[0][1]


def candidates(layers: Sequence[str], spec: Dict, min_score: Optional[int] = None) -> Dict[str, List[Tuple[str, int]]]:
    """按角色给出候选图层，已按置信度排序。"""
    threshold = _filter(spec).get("min_score", 3) if min_score is None else min_score
    result: Dict[str, List[Tuple[str, int]]] = {role: [] for role in ROLES}
    for layer in layers:
        role, score = best_role(layer, spec)
        if role and score >= int(threshold):
            result.setdefault(role, []).append((str(layer), int(score)))
    for role in list(result):
        result[role].sort(key=lambda item: (-item[1], item[0]))
    return result


def ambiguous(layers: Sequence[str], spec: Dict) -> List[Dict]:
    """同分冲突（如裸 `红线`/`HX` 既像用地红线又像道路红线）必须问用户。"""
    items: List[Dict] = []
    for layer in layers:
        scores = score_layer(layer, spec)
        if len(scores) < 2:
            continue
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        if ranked[0][1] == ranked[1][1]:
            items.append({"layer": str(layer), "scores": dict(ranked)})
    return items


def load_map(path: Optional[str]) -> Dict[str, List[str]]:
    """读取指派表：{role: [layer, ...]}。文件不存在时返回空表，不报错。"""
    if not path:
        return {}
    target = Path(path)
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    mapping: Dict[str, List[str]] = {}
    for role, value in data.items():
        if str(role).startswith("_"):
            continue
        if isinstance(value, str):
            mapping[str(role)] = [item.strip() for item in value.split(",") if item.strip()]
        elif isinstance(value, (list, tuple)):
            mapping[str(role)] = [str(item).strip() for item in value if str(item).strip()]
    return mapping


def save_map(path: Optional[str], mapping: Dict[str, Sequence[str]]) -> Optional[str]:
    if not path:
        return None
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    clean = {
        str(role): [str(item) for item in layers if str(item)]
        for role, layers in (mapping or {}).items()
        if not str(role).startswith("_")
    }
    clean = {role: layers for role, layers in clean.items() if layers}
    target.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(target)


def layers_of(mapping: Dict[str, Sequence[str]], role: str) -> List[str]:
    return [str(item) for item in (mapping.get(role) or []) if str(item)]


def describe_candidates(layers: Sequence[str], spec: Dict) -> List[Dict]:
    """给确认项用的候选摘要：角色 → {label, 候选图层（含分数）}。"""
    found = candidates(layers, spec)
    rows: List[Dict] = []
    for role in ROLES:
        rows.append(
            {
                "role": role,
                "label": role_label(spec, role),
                "candidates": [{"layer": layer, "score": score} for layer, score in found.get(role, [])],
            }
        )
    return rows
