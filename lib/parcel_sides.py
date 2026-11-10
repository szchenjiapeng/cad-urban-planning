"""临绿地水系边段识别：地块边界 ∩ 相邻用地图斑。

业务口径（用户确认）：
- 用地红线一侧只要**直接相邻**绿地或水系，该边段退距按 5m 控制；
- 绿地水系范围：01 耕地 / 02 园地 / 03 林地 / 04 草地 / 14 绿地与开敞空间用地 / 17 陆地水域；
- 14 整类包含（公园绿地 1401、防护绿地 1402、广场用地 1403 一视同仁）；
- **不再判断绿地宽度**，只要相邻就算；
- 仍需两条保护：必须直接相邻（间距 ≤ 容差）、接触长度不小于阈值（防止角点相接）。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

GREEN_WATER_PREFIXES = ("01", "02", "03", "04", "14", "17")
GREEN_WATER_LABELS = {
    "01": "耕地",
    "02": "园地",
    "03": "林地",
    "04": "草地",
    "14": "绿地与开敞空间用地",
    "17": "陆地水域",
}


def is_green_water(code: Optional[str]) -> bool:
    text = str(code or "").strip()
    if not text:
        return False
    return text.startswith(GREEN_WATER_PREFIXES)


def green_water_label(code: Optional[str]) -> str:
    text = str(code or "").strip()
    if not text:
        return ""
    for prefix, label in GREEN_WATER_LABELS.items():
        if text.startswith(prefix):
            return label
    return ""


def _line_parts(geometry) -> List:
    if geometry is None or getattr(geometry, "is_empty", True):
        return []
    kind = getattr(geometry, "geom_type", "")
    if kind in {"LineString", "LinearRing"}:
        return [geometry]
    if kind in {"MultiLineString", "GeometryCollection"}:
        parts: List = []
        for item in getattr(geometry, "geoms", []):
            parts.extend(_line_parts(item))
        return parts
    return []


def contact_lines(parcel_geometry, neighbor_geometry, tolerance: float = 0.1) -> List:
    """地块边界落在相邻图斑（外扩容差）内的线段。"""
    if parcel_geometry is None or neighbor_geometry is None:
        return []
    try:
        zone = neighbor_geometry.buffer(float(tolerance))
        contact = parcel_geometry.boundary.intersection(zone)
    except Exception:
        return []
    return _line_parts(contact)


def merge_lines(lines: Sequence) -> List:
    if not lines:
        return []
    if len(lines) == 1:
        return [lines[0]]
    try:
        from shapely.ops import linemerge, unary_union

        merged = linemerge(unary_union(list(lines)))
    except Exception:
        return list(lines)
    parts = _line_parts(merged)
    return parts or list(lines)


def green_sides(
    parcel_geometry,
    neighbors: Sequence[Tuple[object, Optional[str]]],
    unit_factor: float = 1.0,
    tolerance: float = 0.1,
    min_contact_length: float = 2.0,
) -> List[Dict]:
    """返回临绿地水系的边段：[{line, code, label, length_m}]。

    neighbors: [(相邻图斑几何, 用地代码), ...]
    """
    grouped: Dict[str, List] = {}
    if parcel_geometry is None:
        return []
    for geometry, code in neighbors or []:
        if geometry is None or not is_green_water(code):
            continue
        for line in contact_lines(parcel_geometry, geometry, tolerance):
            length_m = float(getattr(line, "length", 0.0)) * float(unit_factor)
            if length_m + 1e-9 < float(min_contact_length):
                continue
            grouped.setdefault(str(code), []).append(line)
    sides: List[Dict] = []
    for code, lines in grouped.items():
        for line in merge_lines(lines):
            length_m = float(getattr(line, "length", 0.0)) * float(unit_factor)
            if length_m + 1e-9 < float(min_contact_length):
                continue
            sides.append(
                {
                    "line": line,
                    "code": code,
                    "label": green_water_label(code),
                    "length_m": round(length_m, 2),
                }
            )
    sides.sort(key=lambda item: -item["length_m"])
    return sides
