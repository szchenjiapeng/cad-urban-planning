"""标注布点：尽量把编号放在地块「最像内部」的位置，并做简单避让。

实现顺序：polylabel（若 shapely 提供）→ representative_point → 网格回退；
避让只做同批次已布点之间的距离控制，不处理与既有图面文字的重叠（避免误判）。
"""

from __future__ import annotations

import math
from typing import Any, List, Optional, Sequence, Tuple

Point = Tuple[float, float]


def _polylabel_label(geometry) -> Optional[Point]:
    try:
        from shapely.ops import polylabel

        point = polylabel(geometry, tolerance=0.5)
        if point is not None and not point.is_empty:
            return (float(point.x), float(point.y))
    except Exception:
        pass
    return None


def _representative(geometry) -> Optional[Point]:
    try:
        point = geometry.representative_point()
        if point is not None and not point.is_empty:
            return (float(point.x), float(point.y))
    except Exception:
        pass
    return None


def _inside(geometry, point: Point) -> bool:
    try:
        from shapely.geometry import Point as ShapelyPoint

        return bool(geometry.contains(ShapelyPoint(point)))
    except Exception:
        return True


def place(geometry, occupied: Sequence[Point], min_gap: float = 0.0) -> Point:
    """返回标注插入点；occupied 为已占用点位，min_gap 为最小间距（图纸单位）。"""
    base = _polylabel_label(geometry) or _representative(geometry) or (0.0, 0.0)
    if min_gap <= 0 or not occupied:
        return base
    if all(math.hypot(base[0] - x, base[1] - y) >= min_gap for x, y in occupied):
        return base
    # 螺旋回退：8 个方向 × 3 圈
    for ring in (1, 2, 3):
        radius = min_gap * ring
        for index in range(8):
            angle = math.pi / 4.0 * index
            candidate = (base[0] + radius * math.cos(angle), base[1] + radius * math.sin(angle))
            if not _inside(geometry, candidate):
                continue
            if all(math.hypot(candidate[0] - x, candidate[1] - y) >= min_gap for x, y in occupied):
                return candidate
    return base


def fallback_point(rings: Sequence[Sequence[Point]]) -> Point:
    """没有 shapely 时的兜底：取外环包围盒中心。"""
    if not rings:
        return (0.0, 0.0)
    xs = [p[0] for p in rings[0]]
    ys = [p[1] for p in rings[0]]
    return ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)
