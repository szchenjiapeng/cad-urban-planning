"""退线成果的环级清理：闭合校验、碎线/内环筛除、同图层去重、共线重叠检查。

硬性（与 references/workflow-building-setback.md 第 9 节一致）：
- **重线只在一个输出图层内部判定**；低多层线与高层线之间的重合（如临绿地水系两侧同为 5m）
  属规则结果，不算重线，不参与任何去重；
- 清理只作用于**写图**的环，不改变面积口径：几何面积照旧进 JSON，清理后写入面积另给一份；
- 任何删除都必须留痕（原因 / 面积 / 最小短边宽度），交报告列明，禁止静默丢弃；
- 判重顺序固定在碎线清理之后，避免「已经是碎片」时误判成重复。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

Point = Tuple[float, float]

DEFAULT_MIN_RING_AREA = 10.0
DEFAULT_MIN_RING_WIDTH = 5.0
DEFAULT_KEEP_HOLE_AREA = 100.0
DEFAULT_COINCIDENT_MIN_LENGTH = 2.0
_PRECISION = 3


def iter_rings(geometry) -> List[Tuple[str, List[Point]]]:
    """几何 → [(kind, ring)]，kind 为 exterior / interior（洞）。丢弃非多边形部件。"""
    if geometry is None or getattr(geometry, "is_empty", True):
        return []
    found: List[Tuple[str, List[Point]]] = []

    def push_polygon(polygon) -> None:
        exterior = getattr(polygon, "exterior", None)
        if exterior is not None:
            found.append(("exterior", [(float(p[0]), float(p[1])) for p in list(exterior.coords)[:-1]]))
        for interior in getattr(polygon, "interiors", []) or []:
            found.append(("interior", [(float(p[0]), float(p[1])) for p in list(interior.coords)[:-1]]))

    geom_type = getattr(geometry, "geom_type", "")
    if geom_type == "Polygon":
        push_polygon(geometry)
    elif geom_type in {"MultiPolygon", "GeometryCollection"}:
        for item in getattr(geometry, "geoms", []):
            if getattr(item, "geom_type", "") == "Polygon":
                push_polygon(item)
    return found


def clean_points(ring: Sequence[Point], tol: float = 1e-6) -> List[Point]:
    """去掉连续重复点（含首尾重复），保证顶点唯一且相邻不重合。"""
    points: List[Point] = []
    for point in ring or []:
        try:
            x, y = float(point[0]), float(point[1])
        except Exception:
            continue
        if points and abs(points[-1][0] - x) <= tol and abs(points[-1][1] - y) <= tol:
            continue
        points.append((x, y))
    while len(points) > 1 and abs(points[0][0] - points[-1][0]) <= tol and abs(points[0][1] - points[-1][1]) <= tol:
        points.pop()
    return points


def ring_polygon(ring: Sequence[Point], tol: float = 1e-6):
    """环 → 有效单多边形；自交用 buffer(0) 修复；修复后仍无面积返回 None。"""
    from shapely.geometry import Polygon

    points = clean_points(ring, tol)
    if len(points) < 3:
        return None
    try:
        polygon = Polygon(points)
    except Exception:
        return None
    if not polygon.is_valid:
        try:
            polygon = polygon.buffer(0)
        except Exception:
            return None
    if polygon is None or polygon.is_empty:
        return None
    if getattr(polygon, "geom_type", "") != "Polygon":
        parts = [item for item in getattr(polygon, "geoms", []) if getattr(item, "geom_type", "") == "Polygon"]
        if not parts:
            return None
        polygon = max(parts, key=lambda item: item.area)
    if float(polygon.area) <= 0:
        return None
    return polygon


def min_width(polygon, tol: float = 0.05, upper: float = 500.0, iterations: int = 14) -> float:
    """最小短边宽度：二分「内缩到多边形消失」的距离 ×2（米/图纸单位取决于坐标）。"""
    if polygon is None or polygon.is_empty:
        return 0.0
    try:
        if polygon.buffer(-upper).is_empty:
            pass
        else:
            return float(upper * 2)
    except Exception:
        pass
    low, high = 0.0, float(upper)
    for _ in range(iterations):
        if high - low <= tol:
            break
        mid = (low + high) / 2
        try:
            empty = polygon.buffer(-mid).is_empty
        except Exception:
            break
        if empty:
            high = mid
        else:
            low = mid
    return low * 2


def ring_metrics(ring: Sequence[Point], unit_factor: float = 1.0) -> Dict:
    """环的度量：面积(㎡)、最小短边宽度(m)、顶点数、是否有效。"""
    polygon = ring_polygon(ring)
    if polygon is None:
        return {"area_m2": 0.0, "width_m": 0.0, "vertices": len(clean_points(ring)), "valid": False}
    width = min_width(polygon) * float(unit_factor)
    return {
        "area_m2": float(polygon.area) * float(unit_factor) ** 2,
        "width_m": float(width),
        "vertices": len(clean_points(ring)),
        "valid": True,
    }


def _key(ring: Sequence[Point]) -> str:
    points = clean_points(ring)
    return "|".join(f"{round(x, _PRECISION)},{round(y, _PRECISION)}" for x, y in points)


def _same_shape(first, second, tolerance: float = 1.0) -> Tuple[bool, str]:
    """判同形状：完全覆盖 / 高度重合（IoU ≥ 0.99）/ 面积几乎相同且包含。"""
    try:
        if first.equals(second):
            return True, "完全重合"
    except Exception:
        pass
    area_first, area_second = float(first.area), float(second.area)
    small, large = (first, second) if area_first <= area_second else (second, first)
    if float(large.area) <= 0:
        return False, ""
    try:
        inter = float(small.intersection(large).area)
    except Exception:
        return False, ""
    if inter <= 0:
        return False, ""
    ratio = inter / float(small.area)
    union = area_first + area_second - inter
    iou = inter / union if union > 0 else 0.0
    if iou >= 0.99:
        return True, "几何重合（IoU ≥ 0.99）"
    if ratio >= 0.99:
        return True, "被另一条环覆盖"
    return False, ""


def _bbox_hit(first, second, pad: float) -> bool:
    (ax0, ay0, ax1, ay1) = first.bounds
    (bx0, by0, bx1, by1) = second.bounds
    return not (
        ax1 < bx0 - pad or bx1 < ax0 - pad or ay1 < by0 - pad or by1 < ay0 - pad
    )


def clean_layer(
    entries: Sequence[Dict],
    min_area: Optional[float] = None,
    min_width_m: Optional[float] = None,
    keep_holes: str = "large",
    keep_hole_area: Optional[float] = None,
    dedupe: bool = True,
    unit_factor: float = 1.0,
) -> Tuple[List[Tuple[Dict, List[Point]]], List[Dict]]:
    """清理一个输出图层的环，返回 (保留的 [(meta, ring)], 删除明细)。

    entries 每项：{"handle","code","layer","kind","ring"}。
    顺序：无效环 → 内环(洞) → 面积 → 最小短边宽度 → 同图层去重。
    """
    min_area = DEFAULT_MIN_RING_AREA if min_area is None else float(min_area)
    min_width_m = DEFAULT_MIN_RING_WIDTH if min_width_m is None else float(min_width_m)
    keep_hole_area = DEFAULT_KEEP_HOLE_AREA if keep_hole_area is None else float(keep_hole_area)
    policy = str(keep_holes or "large").lower()

    kept: List[Tuple[Dict, List[Point]]] = []
    kept_polys: List[object] = []
    dropped: List[Dict] = []

    def drop(meta: Dict, reason: str, area: float, width: float, extra: str = "") -> None:
        dropped.append(
            {
                "reason": reason,
                "parcel": meta.get("handle", ""),
                "parcel_label": meta.get("code", ""),
                "parcel_layer": meta.get("layer", ""),
                "ring_kind": meta.get("kind", "exterior"),
                "area_m2": round(area, 2),
                "width_m": round(width, 2),
                "note": extra,
            }
        )

    for entry in entries:
        meta = dict(entry)
        ring = entry.get("ring") or []
        metrics = ring_metrics(ring, unit_factor)
        if not metrics["valid"]:
            drop(meta, "无效环（自交或顶点不足，无法闭合为面）", 0.0, 0.0, "不写图")
            continue
        if meta.get("kind") == "interior":
            if policy == "all" or (policy == "large" and metrics["area_m2"] >= keep_hole_area):
                meta["kind"] = "interior-kept"
                kept.append((meta, ring))
                kept_polys.append(ring_polygon(ring))
                continue
            drop(meta, "内环碎线（可建范围内部的洞）", metrics["area_m2"], metrics["width_m"], f"策略 {policy}")
            continue
        if metrics["area_m2"] < min_area:
            drop(meta, f"面积 < {min_area:g} ㎡", metrics["area_m2"], metrics["width_m"], "判为杂线")
            continue
        if metrics["width_m"] < min_width_m:
            drop(meta, f"最小短边宽 < {min_width_m:g} m", metrics["area_m2"], metrics["width_m"], "判为杂线")
            continue
        polygon = ring_polygon(ring)
        if dedupe and polygon is not None:
            duplicate = ""
            for index, other in enumerate(kept_polys):
                if other is None:
                    continue
                if not _bbox_hit(polygon, other, 1e-6):
                    continue
                same, why = _same_shape(polygon, other)
                if not same:
                    continue
                smaller = polygon.area <= other.area
                if smaller:
                    duplicate = f"{why}，与 {kept[index][0].get('handle', '')} 重复"
                    break
                # 新环更大：替换旧环，旧环记删
                kept_meta, kept_ring = kept[index]
                drop(kept_meta, f"同图层重线（{why}，被更大的环取代）", float(other.area) * unit_factor ** 2,
                     min_width(other) * unit_factor, f"保留 {meta.get('handle', '')}")
                kept[index] = (meta, ring)
                kept_polys[index] = polygon
                duplicate = "__replaced__"
                break
            if duplicate == "__replaced__":
                continue
            if duplicate:
                drop(meta, f"同图层重线（{duplicate}）", metrics["area_m2"], metrics["width_m"], "保留大环")
                continue
        kept.append((meta, ring))
        kept_polys.append(polygon)
    return kept, dropped


def coincident_pairs(
    kept: Sequence[Tuple[Dict, List[Point]]],
    unit_factor: float = 1.0,
    min_length_m: float = DEFAULT_COINCIDENT_MIN_LENGTH,
) -> List[Dict]:
    """同图层内部分共线的边段：返回清单（不删线，删了会破坏该环闭合）。"""
    polys = []
    for meta, ring in kept:
        polygon = ring_polygon(ring)
        if polygon is None:
            continue
        polys.append((meta, polygon))
    found: List[Dict] = []
    if len(polys) < 2:
        return found
    for index in range(len(polys)):
        meta_a, poly_a = polys[index]
        for jndex in range(index + 1, len(polys)):
            meta_b, poly_b = polys[jndex]
            if not _bbox_hit(poly_a, poly_b, 0.5):
                continue
            try:
                shared = poly_a.boundary.intersection(poly_b.boundary)
            except Exception:
                continue
            if shared is None or shared.is_empty:
                continue
            length = float(getattr(shared, "length", 0.0)) * float(unit_factor)
            if length < min_length_m:
                continue
            found.append(
                {
                    "parcel_a": meta_a.get("handle", ""),
                    "parcel_b": meta_b.get("handle", ""),
                    "layer_a": meta_a.get("code", ""),
                    "layer_b": meta_b.get("code", ""),
                    "shared_length_m": round(length, 2),
                    "note": "共线重叠（同一图层内），未删线；如需合并请用 --coincident-policy merge",
                }
            )
    return found


def merge_rings(kept: Sequence[Tuple[Dict, List[Point]]], unit_factor: float = 1.0):
    """同图层合并为一条外轮廓（仅 --coincident-policy merge 使用，会失去分地块属性）。"""
    from shapely.ops import unary_union

    polys = [ring_polygon(ring) for _meta, ring in kept]
    polys = [item for item in polys if item is not None]
    if not polys:
        return []
    merged = unary_union(polys)
    rings: List[Tuple[Dict, List[Point]]] = []
    for kind, ring in iter_rings(merged):
        if kind != "exterior":
            continue
        rings.append(({"handle": "", "code": "合并外轮廓", "layer": "", "kind": "exterior-merged"}, ring))
    return rings
