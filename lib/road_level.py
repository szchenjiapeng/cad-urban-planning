"""道路等级判定与退距取值。

业务约束（硬性）：
- **红线宽度与道路等级没有必然对应关系**，主干路以下尤其如此：存在比主干路更宽的
  次干路，也存在很窄的主干路段。因此本模块**不用宽度推断等级**，宽度只写进报告
  供人工复核（`width_reference` 返回值永不参与退距计算）；
- 等级来源优先级：用户指定（`--road-levels`）> 图层名关键词 > 未判定（需确认或按默认）；
- 未判定时不得静默套用某一等级的标准，只能取规则文件的默认退距并在报告标注。
"""

from __future__ import annotations

import math
import unicodedata
from typing import Dict, Optional, Sequence, Tuple

LEVELS = ("快速路", "主干路", "次干路", "支路")

LEVEL_ALIASES = {
    "快速路": ["快速路", "快速", "快线", "kuaishu", "expressway", "express", "freeway"],
    "主干路": ["主干路", "主干道", "主干", "主路", "zhuganlu", "zhugandao", "zhugan", "arterial", "major road"],
    "次干路": ["次干路", "次干道", "次干", "ciganlu", "cigandao", "cigan", "secondary road"],
    "支路": ["支路", "街坊路", "支路网", "zhilu", "branch road", "local road", "minor road"],
}


def normalize_level(text: str) -> str:
    """把中文/拼音/英文的等级写法归一成标准等级；认不出返回空串。"""
    raw = unicodedata.normalize("NFKC", str(text or "")).strip().lower()
    if not raw:
        return ""
    for level, aliases in LEVEL_ALIASES.items():
        for alias in aliases:
            if alias in raw:
                return level
    return ""


def level_from_layer(layer: str, rules: Dict) -> str:
    """图层名关键词判定（RD-主干路 / DL-次干路 / 支路7 等）。"""
    name = unicodedata.normalize("NFKC", str(layer or "")).lower()
    if not name:
        return ""
    for level, keywords in (rules.get("road_level_keywords") or {}).items():
        for keyword in keywords or []:
            key = str(keyword or "").lower()
            if key and key in name:
                return str(level)
    return ""


def parse_user_levels(text: Optional[str]) -> Dict[str, str]:
    """解析 `--road-levels "RD-解放路=主干路,DL-纬三路=次干路"`。"""
    result: Dict[str, str] = {}
    for item in str(text or "").replace("，", ",").split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        level = normalize_level(value) or value.strip()
        if key and level:
            result[key] = level
    return result


def resolve(layer: str, user_levels: Optional[Dict[str, str]], rules: Dict) -> Tuple[str, str]:
    """返回 (等级, 依据)。依据取值：用户指定 / 图层名 / 未判定。"""
    user_levels = user_levels or {}
    layer_text = str(layer or "")
    for key, level in user_levels.items():
        if key == layer_text:
            return level, "用户指定"
    lowered = layer_text.lower()
    for key, level in user_levels.items():
        if key and key.lower() in lowered:
            return level, f"用户指定（命中 {key}）"
    from_layer = level_from_layer(layer_text, rules)
    if from_layer:
        return from_layer, "图层名"
    return "", "未判定"


def distance_of(level: str, rules: Dict, default_distance: float) -> Tuple[float, str]:
    table = rules.get("road_level_distances") or {}
    if level and level in table:
        try:
            return float(table[level]), f"道路等级（{level}）"
        except Exception:
            pass
    return float(default_distance), "等级未判定/规则未命中，按默认退距"


def width_reference(geometry, unit_factor: float = 1.0) -> Tuple[float, str]:
    """红线宽度参考值（仅写报告，**不参与等级判定**）。

    取「最小外接矩形短边」与「2×面积/周长」中较小者，避免把长条形路口算宽。
    """
    if geometry is None or getattr(geometry, "is_empty", True):
        return 0.0, "无几何"
    try:
        rect = geometry.minimum_rotated_rectangle
        coords = list(getattr(rect, "exterior").coords) if getattr(rect, "exterior", None) else []
        sides = []
        for index in range(len(coords) - 1):
            dx = coords[index + 1][0] - coords[index][0]
            dy = coords[index + 1][1] - coords[index][1]
            length = math.hypot(dx, dy)
            if length > 1e-9:
                sides.append(length)
        short = min(sides) if sides else 0.0
        perimeter = float(getattr(geometry, "length", 0.0) or 0.0)
        average = 2.0 * float(geometry.area) / perimeter if perimeter > 0 else 0.0
        width = (short or average) * float(unit_factor)
        note = f"短边 {short * unit_factor:.2f}m / 平均 {average * unit_factor:.2f}m"
        return round(width, 2), note
    except Exception:
        return 0.0, "测算失败"
