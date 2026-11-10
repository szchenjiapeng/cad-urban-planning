"""图层规范：分组、排除、新图层命名。

定位（硬性）：
- 本模块只回答「这个图层属于哪一组」「新图层该叫什么」「哪些图层要排除」；
- **用地代码一律委托 core.classify.LayerMapper 解析**，此处不得维护代码表，
  否则同一张图会出现「平衡表认 070102、编码脚本认 08」的分歧。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SKILL_DIR = _SCRIPTS_DIR
DEFAULT_SPEC_PATH = SKILL_DIR / "assets" / "layer_spec.json"

_GROUP_ORDER = ("landuse", "building", "road", "control", "label", "frame", "existing")


def _norm(text: str) -> str:
    return str(text or "").strip()


def load_spec(path: Optional[str] = None) -> Dict:
    target = Path(path) if path else DEFAULT_SPEC_PATH
    if not target.exists():
        raise FileNotFoundError(f"图层规范不存在：{target}")
    return json.loads(target.read_text(encoding="utf-8"))


def group_def(spec: Dict, group: str) -> Dict:
    return (spec.get("groups") or {}).get(group, {}) or {}


def group_names(spec: Dict) -> List[str]:
    return [name for name in _GROUP_ORDER if name in (spec.get("groups") or {})]


def excluded(layer: str, spec: Dict, group: str) -> bool:
    """命中该组的排除词即不算该组图元（如「用地界线」不算地块）。"""
    name = _norm(layer).lower()
    if not name:
        return True
    for keyword in group_def(spec, group).get("exclude_keywords", []) or []:
        key = _norm(keyword).lower()
        if key and key in name:
            return True
    return False


def layer_in_group(layer: str, spec: Dict, group: str) -> bool:
    """按「排除词 → 前缀 → 别名」判断图层是否属于该组。"""
    name = _norm(layer)
    if not name:
        return False
    if excluded(name, spec, group):
        return False
    definition = group_def(spec, group)
    lowered = name.lower()
    for prefix in definition.get("prefixes", []) or []:
        key = _norm(prefix).lower()
        if key and (lowered == key or lowered.startswith(key + "-") or lowered.startswith(key + "_") or lowered.startswith(key)):
            return True
    for alias in definition.get("aliases", []) or []:
        key = _norm(alias).lower()
        if key and key in lowered:
            return True
    return False


def layers_of(layers: Sequence[str], spec: Dict, group: str) -> List[str]:
    return [name for name in layers if layer_in_group(name, spec, group)]


def is_range_layer(layer: str, spec: Dict) -> bool:
    """规划范围/用地界线只用于裁剪，不得当地块参与编号、面积与退线。"""
    name = _norm(layer).lower()
    if not name:
        return False
    definition = group_def(spec, "landuse")
    for keyword in definition.get("range_keywords", []) or []:
        key = _norm(keyword).lower()
        if key and key in name:
            return True
    return False


def control_def(spec: Dict, key: str) -> Dict:
    return (spec.get("control_lines") or {}).get(key, {}) or {}


def control_layer(spec: Dict, key: str, fallback_prefix: str = "KZX") -> str:
    definition = control_def(spec, key)
    return _norm(definition.get("layer")) or f"{fallback_prefix}-{key}"


def label_layer(spec: Dict, default: str = "BZ-地块编号") -> str:
    return _norm(spec.get("label_layer")) or default


def spec_status(spec: Dict) -> str:
    """报告首屏提醒用：配置还是默认草案时必须提示用户替换。"""
    status = _norm(spec.get("_status"))
    return status or ""


def describe_groups(spec: Dict) -> List[Tuple[str, str, str]]:
    rows: List[Tuple[str, str, str]] = []
    for group in group_names(spec):
        definition = group_def(spec, group)
        rows.append(
            (
                group,
                ",".join(definition.get("prefixes", []) or []),
                ",".join(definition.get("aliases", []) or []),
            )
        )
    return rows
