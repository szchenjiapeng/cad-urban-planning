"""几何计算层（纯函数，可单测）：闭合判定、单位解析、含圆弧面积、范围过滤、重叠检测。

不引入 shapely 等重依赖，用鞋带公式 + bulge 圆弧离散化 + 包围盒网格索引实现，
在万级图元规模下保持秒级响应。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

Point = Tuple[float, float]
BBox = Tuple[float, float, float, float]

# $INSUNITS → (单位名, 图纸单位 -> 米 的系数)
_UNIT_TABLE: Dict[int, Tuple[str, float]] = {
    1: ("in", 0.0254),
    2: ("ft", 0.3048),
    4: ("mm", 0.001),
    5: ("cm", 0.01),
    6: ("m", 1.0),
    7: ("km", 1000.0),
}

_UNIT_ALIASES = {
    "mm": ("mm", 0.001),
    "毫米": ("mm", 0.001),
    "cm": ("cm", 0.01),
    "厘米": ("cm", 0.01),
    "m": ("m", 1.0),
    "米": ("m", 1.0),
    "km": ("km", 1000.0),
    "in": ("in", 0.0254),
    "inch": ("in", 0.0254),
    "ft": ("ft", 0.3048),
}


@dataclass
class Shape:
    """用于重叠检测与范围过滤的轻量几何体。"""

    handle: str
    layer: str
    points: List[Point]
    area_m2: float
    bbox: BBox


# ---------------------------------------------------------------- 圆弧与面积
def arc_points(p1: Point, p2: Point, bulge: float, max_step_deg: float = 2.0) -> List[Point]:
    """把 bulge 表示的圆弧离散成中间点（不含首尾）。"""
    if abs(bulge) < 1e-12:
        return []
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    chord = math.hypot(dx, dy)
    if chord < 1e-12:
        return []
    theta = 4.0 * math.atan(bulge)  # 有符号圆心角
    if abs(theta) < 1e-12:
        return []
    radius = chord / (2.0 * math.sin(abs(theta) / 2.0))
    ux, uy = dx / chord, dy / chord
    nx, ny = -uy, ux  # 弦的左法向
    dist = radius * math.cos(theta / 2.0)
    sign = 1.0 if bulge > 0 else -1.0
    cx = (p1[0] + p2[0]) / 2.0 + sign * dist * nx
    cy = (p1[1] + p2[1]) / 2.0 + sign * dist * ny
    start_angle = math.atan2(p1[1] - cy, p1[0] - cx)
    steps = max(2, int(abs(math.degrees(theta)) / max_step_deg) + 1)
    points: List[Point] = []
    for i in range(1, steps):
        angle = start_angle + theta * i / steps
        points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return points


def discretize(
    vertices: Sequence[Point], bulges: Sequence[float], close: bool = True, max_step_deg: float = 2.0
) -> List[Point]:
    """展开多段线的圆弧段，得到可用于鞋带公式的顶点序列。"""
    if not vertices:
        return []
    count = len(vertices)
    result: List[Point] = []
    segments = count if close else max(0, count - 1)
    for i in range(segments):
        p1 = vertices[i]
        p2 = vertices[(i + 1) % count]
        result.append((float(p1[0]), float(p1[1])))
        bulge = float(bulges[i]) if i < len(bulges) else 0.0
        result.extend(arc_points(p1, p2, bulge, max_step_deg))
    if not close:
        result.append((float(vertices[-1][0]), float(vertices[-1][1])))
    return result


def polygon_area(points: Sequence[Point]) -> float:
    """鞋带公式，返回绝对面积（图纸单位²）。"""
    count = len(points)
    if count < 3:
        return 0.0
    total = 0.0
    for i in range(count):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % count]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def is_closed(vertices: Sequence[Point], tolerance: float) -> Tuple[bool, float]:
    """几何闭合判定，返回 (是否闭合, 首尾间距)。"""
    if len(vertices) < 3:
        return False, float("inf")
    gap = math.hypot(vertices[0][0] - vertices[-1][0], vertices[0][1] - vertices[-1][1])
    return gap <= tolerance, gap


# ---------------------------------------------------------------- 单位
def resolve_unit(
    insunits: int,
    span: Optional[float] = None,
    override: Optional[str] = None,
) -> Tuple[str, float, str]:
    """返回 (单位名, 图纸单位->米 系数, 判定依据)。"""
    if override:
        key = override.strip().lower()
        if key not in _UNIT_ALIASES:
            raise ValueError(f"无法识别的单位：{override}（可选 mm/cm/m/km/in/ft）")
        unit, factor = _UNIT_ALIASES[key]
        return unit, factor, "用户通过 --unit 指定"

    if insunits in _UNIT_TABLE:
        unit, factor = _UNIT_TABLE[insunits]
        return unit, factor, f"图纸 $INSUNITS={insunits}"

    if insunits == 0:
        if span is not None and span >= 5000:
            return "mm", 0.001, f"图纸未指定单位，按全图跨度 {span:.0f} 推断为毫米（置信度中）"
        if span is not None:
            return "m", 1.0, f"图纸未指定单位，按全图跨度 {span:.0f} 推断为米（置信度中）"
        return "m", 1.0, "图纸未指定单位且无图形范围，默认按米处理（置信度低）"

    unit, factor = _UNIT_TABLE.get(6, ("m", 1.0))
    return unit, factor, f"未支持的 $INSUNITS={insunits}，按米处理（置信度低）"


# ---------------------------------------------------------------- 包围盒与范围
def bbox_of(points: Sequence[Point]) -> BBox:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def bbox_union(boxes: Iterable[BBox]) -> Optional[BBox]:
    boxes = list(boxes)
    if not boxes:
        return None
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def bbox_center(box: BBox) -> Point:
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
    """射线法，边界上的点判定结果可能不稳定，仅用于重叠/范围粗判。"""
    x, y = point
    inside = False
    count = len(polygon)
    if count < 3:
        return False
    j = count - 1
    for i in range(count):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi + 1e-18) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def in_window(box: BBox, window: BBox) -> bool:
    """整体落在窗口内（按包围盒判定，避免相邻地块被误纳）。"""
    return box[0] >= window[0] and box[1] >= window[1] and box[2] <= window[2] and box[3] <= window[3]


def crosses_window(box: BBox, window: BBox) -> bool:
    return not (box[2] < window[0] or box[0] > window[2] or box[3] < window[1] or box[1] > window[3])


# ---------------------------------------------------------------- 自相交
def _ccw(a: Point, b: Point, c: Point) -> bool:
    return (c[1] - a[1]) * (b[0] - a[0]) > (b[1] - a[1]) * (c[0] - a[0])


def segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    return _ccw(a, c, d) != _ccw(b, c, d) and _ccw(a, b, c) != _ccw(a, b, d)


def self_intersects(points: Sequence[Point], max_vertices: int = 300) -> bool:
    """检测多边形自相交。顶点过多时跳过（避免 O(n²) 拖慢整图）。"""
    count = len(points)
    if count < 4 or count > max_vertices:
        return False
    for i in range(count):
        a, b = points[i], points[(i + 1) % count]
        for j in range(i + 2, count):
            if i == 0 and j == count - 1:
                continue
            c, d = points[j], points[(j + 1) % count]
            if segments_intersect(a, b, c, d):
                return True
    return False


# ---------------------------------------------------------------- 重叠检测
def bbox_overlap_ratio(box1: BBox, box2: BBox) -> float:
    width = min(box1[2], box2[2]) - max(box1[0], box2[0])
    height = min(box1[3], box2[3]) - max(box1[1], box2[1])
    if width <= 0 or height <= 0:
        return 0.0
    inter = width * height
    area1 = max((box1[2] - box1[0]) * (box1[3] - box1[1]), 1e-12)
    area2 = max((box2[2] - box2[0]) * (box2[3] - box2[1]), 1e-12)
    return inter / min(area1, area2)


def detect_overlaps(shapes: Sequence[Shape], min_ratio: float = 0.2) -> List[Tuple[Shape, Shape]]:
    """网格索引 + 包围盒 + 采样点判定，返回疑似重叠对。只告警，不扣减。"""
    if len(shapes) < 2:
        return []

    spans = [max(s.bbox[2] - s.bbox[0], s.bbox[3] - s.bbox[1]) for s in shapes]
    cell = max(sum(spans) / len(spans) * 1.5, 1e-6)

    buckets: Dict[Tuple[int, int], List[int]] = {}
    for index, shape in enumerate(shapes):
        x0 = int(math.floor(shape.bbox[0] / cell))
        x1 = int(math.floor(shape.bbox[2] / cell))
        y0 = int(math.floor(shape.bbox[1] / cell))
        y1 = int(math.floor(shape.bbox[3] / cell))
        if (x1 - x0 + 1) * (y1 - y0 + 1) > 64:  # 超大图元只放中心点，避免桶爆炸
            cx, cy = bbox_center(shape.bbox)
            buckets.setdefault((int(math.floor(cx / cell)), int(math.floor(cy / cell))), []).append(index)
            continue
        for gx in range(x0, x1 + 1):
            for gy in range(y0, y1 + 1):
                buckets.setdefault((gx, gy), []).append(index)

    checked = set()
    overlaps: List[Tuple[Shape, Shape]] = []
    for indices in buckets.values():
        if len(indices) < 2:
            continue
        for i in range(len(indices)):
            for j in range(i + 1, len(indices)):
                a, b = indices[i], indices[j]
                pair = (a, b) if a < b else (b, a)
                if pair in checked:
                    continue
                checked.add(pair)
                sa, sb = shapes[pair[0]], shapes[pair[1]]
                if bbox_overlap_ratio(sa.bbox, sb.bbox) < min_ratio:
                    continue
                small, big = (sa, sb) if sa.area_m2 <= sb.area_m2 else (sb, sa)
                if len(small.points) < 3 or len(big.points) < 3:
                    continue
                sample = small.points[:: max(1, len(small.points) // 5)][:5]
                if any(point_in_polygon(point, big.points) for point in sample):
                    overlaps.append((sa, sb))
    return overlaps
