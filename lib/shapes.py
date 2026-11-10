"""闭合轮廓收集：地块、建筑、控制线的统一取数入口。

规则：
- 填充（HATCH）优先，且一块地的净几何必须用 net_geometry（外环 − 孤岛），
  只取外环会把洞内小地块误判成重叠（现有平衡表已踩过这个坑）；
- 多段线只在「该图层没有填充」时参与（`source="auto"`，图层级跳过）；
- 但同一图层「部分用填充、部分用多段线」时，图层级跳过会整层漏掉多段线表达的图元，
  因此新增 `dedupe=True`：`source="both"` 全收后按几何重合去重，同一轮廓只算一次
  （保留填充优先）。建筑类图层必须用 `both + dedupe`，地块仍可沿用 auto；
- 面积统一换算成平方米（图纸单位 × unit_factor）。
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from core.geometry import bbox_of, discretize, is_closed, polygon_area  # noqa: E402
from core.hatch import net_area, net_geometry  # noqa: E402
from core.models import HatchRecord, PolylineRecord  # noqa: E402

Point = Tuple[float, float]


@dataclass
class ShapeRec:
    handle: str
    layer: str
    rings: List[List[Point]] = field(default_factory=list)
    geometry: Any = None
    area_m2: float = 0.0
    source_kind: str = "polyline"
    closed: bool = True
    # 闭合来源：closed_flag=CAD 自报闭合 / snapped=首尾容差吸附（EXTRUDE 前需人工复核）/
    # hatch=填充边界天然闭合 / polygonize=线段缝合成面
    close_kind: str = "closed_flag"
    code: Optional[str] = None
    name: str = ""
    # 混合用地：code 恒为首段（主代码），code_display 是给成果看的合成代码（100104+0909）。
    # 面积仍按整块几何计量一次，绝不按段拆开重复计。
    code_display: str = ""
    hybrid_raw: str = ""
    hybrid_parts: List[str] = field(default_factory=list)
    hybrid_unresolved: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def outer(self) -> List[Point]:
        return self.rings[0] if self.rings else []

    def centroid(self) -> Point:
        if self.geometry is not None and not self.geometry.is_empty:
            point = self.geometry.representative_point()
            return (float(point.x), float(point.y))
        if self.rings:
            xs = [p[0] for p in self.rings[0]]
            ys = [p[1] for p in self.rings[0]]
            return (sum(xs) / len(xs), sum(ys) / len(ys))
        return (0.0, 0.0)

    def bounds(self) -> Tuple[float, float, float, float]:
        if self.geometry is not None and not self.geometry.is_empty:
            box = self.geometry.bounds
            return (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
        return bbox_of(self.rings[0]) if self.rings else (0.0, 0.0, 0.0, 0.0)


def _scale_tolerance(vertices: Sequence[Point]) -> float:
    box = bbox_of(vertices)
    span = max(box[2] - box[0], box[3] - box[1], 1e-9)
    return max(span * 1e-4, 1e-9)


def _polygon_from_rings(rings: Sequence[Sequence[Point]]):
    try:
        from shapely.geometry import Polygon
        from shapely.ops import unary_union
    except Exception:
        return None
    if not rings:
        return None
    built = []
    for ring in rings:
        if len(ring) < 3:
            continue
        try:
            geom = Polygon([(float(p[0]), float(p[1])) for p in ring])
            if not geom.is_valid:
                geom = geom.buffer(0)
            if not geom.is_empty and geom.area > 0:
                built.append(geom)
        except Exception:
            continue
    if not built:
        return None
    if len(built) == 1:
        return built[0]
    try:
        return unary_union(built)
    except Exception:
        return built[0]


def _from_hatch(record: HatchRecord, unit_factor: float) -> Optional[ShapeRec]:
    rings = [list(ring) for ring in (record.rings or []) if len(ring) >= 3]
    if not rings:
        return None
    geometry = net_geometry(rings)
    area_units = None
    if geometry is not None and not geometry.is_empty:
        area_units = float(geometry.area)
    else:
        area_units, _infos = net_area(rings)
        geometry = _polygon_from_rings(rings)
    if area_units is None or area_units <= 0:
        return None
    return ShapeRec(
        handle=record.handle,
        layer=record.layer,
        rings=rings,
        geometry=geometry,
        area_m2=area_units * unit_factor * unit_factor,
        source_kind="hatch",
        close_kind="hatch",
    )


def _from_polyline(record: PolylineRecord, unit_factor: float, tolerance: Optional[float] = None) -> Optional[ShapeRec]:
    if len(record.vertices) < 3:
        return None
    points = discretize(record.vertices, record.bulges, close=True)
    if len(points) < 3:
        return None
    declared = bool(record.closed_flag)
    gap = 0.0
    close_kind = "closed_flag"
    if not declared:
        tol = tolerance if tolerance is not None else _scale_tolerance(points)
        declared, gap = is_closed(points, tol)
        close_kind = "snapped" if declared else "open"
    if not declared:
        return None
    if points[0] == points[-1]:
        points = points[:-1]
    area_units = polygon_area(points)
    if area_units <= 0:
        return None
    geometry = _polygon_from_rings([points])
    return ShapeRec(
        handle=record.handle,
        layer=record.layer,
        rings=[points],
        geometry=geometry,
        area_m2=area_units * unit_factor * unit_factor,
        source_kind="polyline",
        closed=True,
        close_kind=close_kind,
        warnings=[f"几何闭合容差吸附，首尾间距 {gap:.4f}"] if gap else [],
    )


# ---------------------------------------------------------------- 重复表达去重
def _cells_of(box, cell: float) -> List[Tuple[int, int]]:
    x0 = int(math.floor(box[0] / cell))
    x1 = int(math.floor(box[2] / cell))
    y0 = int(math.floor(box[1] / cell))
    y1 = int(math.floor(box[3] / cell))
    if (x1 - x0 + 1) * (y1 - y0 + 1) > 64:  # 超大图元只放中心，避免桶爆炸
        cx = int(math.floor((box[0] + box[2]) / 2.0 / cell))
        cy = int(math.floor((box[1] + box[3]) / 2.0 / cell))
        return [(cx, cy)]
    return [(gx, gy) for gx in range(x0, x1 + 1) for gy in range(y0, y1 + 1)]


def _same_profile(first, second, min_coverage: float, min_area_ratio: float) -> bool:
    a, b = first.geometry, second.geometry
    if a is None or b is None or getattr(a, "is_empty", True) or getattr(b, "is_empty", True):
        return False
    try:
        small, large = (a, b) if a.area <= b.area else (b, a)
        small_area = float(small.area)
        large_area = float(large.area)
        if small_area <= 0 or large_area <= 0:
            return False
        if small_area / large_area < min_area_ratio:
            return False
        inter = float(small.intersection(large).area)
    except Exception:
        return False
    return inter > 0 and inter / small_area >= min_coverage


def dedupe_profiles(
    records: Sequence[ShapeRec],
    min_coverage: float = 0.9,
    min_area_ratio: float = 0.6,
) -> Tuple[List[ShapeRec], List[Dict]]:
    """同一图层里同一轮廓被「填充 + 多段线」重复表达时只保留一份。

    判据（同时成立）：
    - 小面积轮廓被大轮廓覆盖 ≥ min_coverage（默认 0.9）；
    - 面积比 ≥ min_area_ratio（默认 0.6），允许填充扣了内院导致面积偏小。

    保留优先级：填充（hatch）> 多段线；同类型保留面积大的。
    返回 (保留清单, 重复清单)；重复清单字段与 skipped 一致，可直接并入异常表。
    """
    items = [
        rec
        for rec in records
        if rec.geometry is not None
        and not getattr(rec.geometry, "is_empty", True)
        and float(rec.area_m2 or 0.0) > 0
    ]
    if len(items) < 2:
        return list(records), []

    spans = []
    for rec in items:
        box = rec.bounds()
        spans.append(max(box[2] - box[0], box[3] - box[1]))
    cell = max(sum(spans) / len(spans) * 1.5, 1e-6)

    ordered = sorted(
        items, key=lambda rec: (0 if rec.source_kind == "hatch" else 1, -float(rec.area_m2))
    )
    kept: List[ShapeRec] = []
    buckets: Dict[Tuple[int, int], List[int]] = {}
    duplicates: List[Dict] = []
    for rec in ordered:
        hit = None
        for key in _cells_of(rec.bounds(), cell):
            for index in buckets.get(key, ()):
                if _same_profile(rec, kept[index], min_coverage, min_area_ratio):
                    hit = kept[index]
                    break
            if hit is not None:
                break
        if hit is None:
            kept.append(rec)
            for key in _cells_of(rec.bounds(), cell):
                buckets.setdefault(key, []).append(len(kept) - 1)
            continue
        duplicates.append(
            {
                "handle": rec.handle,
                "layer": rec.layer,
                "source": rec.source_kind,
                "kept_handle": hit.handle,
                "kept_source": hit.source_kind,
                "area_m2": round(float(rec.area_m2 or 0.0), 4),
                "reason": f"与 {hit.source_kind}「{hit.handle}」重复表达同一轮廓，未重复计入",
            }
        )

    order = {id(rec): index for index, rec in enumerate(records)}
    kept.sort(key=lambda rec: order.get(id(rec), 0))
    return kept, duplicates


def collect_polygons(
    backend,
    layers: Sequence[str],
    layer_patterns: Optional[Sequence[str]] = None,
    unit_factor: float = 1.0,
    source: str = "auto",
    min_area_m2: float = 0.0,
    tolerance: Optional[float] = None,
    dedupe: bool = False,
) -> List[ShapeRec]:
    """收集指定图层的闭合轮廓。

    source: auto（填充优先，无填充的图层回退多段线）/ hatch / polyline / both；
    dedupe=True 时对「填充 + 多段线」的重复表达做几何级去重（同一轮廓只算一次）。
    注意：auto 的图层级跳过会漏掉「同图层部分建筑只用多段线表达」的情况，
    需要混合表达的建筑类图层请用 source="both" + dedupe=True。
    """
    results, _skipped = collect_polygons_detail(
        backend,
        layers,
        layer_patterns=layer_patterns,
        unit_factor=unit_factor,
        source=source,
        min_area_m2=min_area_m2,
        tolerance=tolerance,
        dedupe=dedupe,
    )
    return results


def collect_polygons_detail(
    backend,
    layers: Sequence[str],
    layer_patterns: Optional[Sequence[str]] = None,
    unit_factor: float = 1.0,
    source: str = "auto",
    min_area_m2: float = 0.0,
    tolerance: Optional[float] = None,
    dedupe: bool = False,
) -> Tuple[List[ShapeRec], List[dict]]:
    """同 collect_polygons，额外返回**未参与**的图元清单（给用户解释数量差）。

    skipped 每项：{handle, layer, source, reason, area_m2}；
    dedupe=True 时重复表达的那一条也会进 skipped（带 kept_handle / kept_source）。
    """
    wanted = set(layers) if layers else None
    if wanted is not None and not wanted:
        return [], []
    mode = (source or "auto").lower()
    results: List[ShapeRec] = []
    skipped: List[dict] = []
    hatch_layers = set()

    if mode in {"auto", "hatch", "both"}:
        for record in backend.iter_hatches(None, layer_patterns):
            if wanted is not None and record.layer not in wanted:
                continue
            shape = _from_hatch(record, unit_factor)
            if shape is None:
                skipped.append(
                    {
                        "handle": record.handle,
                        "layer": record.layer,
                        "source": "hatch",
                        "reason": "填充边界退化（面积算不出来），需 HATCHEDIT 重画",
                        "area_m2": 0.0,
                    }
                )
                hatch_layers.add(record.layer)
                continue
            if shape.area_m2 < min_area_m2:
                skipped.append(
                    {
                        "handle": record.handle,
                        "layer": record.layer,
                        "source": "hatch",
                        "reason": f"面积 {shape.area_m2:.4f} ㎡ 低于阈值 {min_area_m2:g} ㎡",
                        "area_m2": round(shape.area_m2, 4),
                    }
                )
            else:
                results.append(shape)
            hatch_layers.add(record.layer)

    if mode in {"auto", "polyline", "both"}:
        for record in backend.iter_polylines(None, layer_patterns):
            if wanted is not None and record.layer not in wanted:
                continue
            if mode == "auto" and record.layer in hatch_layers:
                # 同一图层已用填充表达，多段线视为重复描图，不重复计数
                continue
            shape = _from_polyline(record, unit_factor, tolerance=tolerance)
            if shape is None:
                skipped.append(
                    {
                        "handle": record.handle,
                        "layer": record.layer,
                        "source": "polyline",
                        "reason": "未闭合或面积为 0",
                        "area_m2": 0.0,
                    }
                )
                continue
            if shape.area_m2 < min_area_m2:
                skipped.append(
                    {
                        "handle": record.handle,
                        "layer": record.layer,
                        "source": "polyline",
                        "reason": f"面积 {shape.area_m2:.4f} ㎡ 低于阈值 {min_area_m2:g} ㎡",
                        "area_m2": round(shape.area_m2, 4),
                    }
                )
                continue
            results.append(shape)
    if dedupe and len(results) > 1:
        results, duplicates = dedupe_profiles(results)
        skipped.extend(duplicates)
    return results, skipped


def collect_segments(
    backend,
    layers: Sequence[str],
    layer_patterns: Optional[Sequence[str]] = None,
) -> List[Tuple[Point, Point]]:
    """收集线段（LINE + 开放/闭合多段线的边），供 polygonize 成面。"""
    wanted = set(layers) if layers else None
    if wanted is not None and not wanted:
        return []
    segments: List[Tuple[Point, Point]] = []
    for record in backend.iter_lines(None, layer_patterns):
        if wanted is not None and record.layer not in wanted:
            continue
        if len(record.vertices) == 2:
            segments.append((record.vertices[0], record.vertices[1]))
    for record in backend.iter_polylines(None, layer_patterns):
        if wanted is not None and record.layer not in wanted:
            continue
        points = discretize(record.vertices, record.bulges, close=True)
        for index in range(len(points) - 1):
            segments.append((points[index], points[index + 1]))
        if record.closed_flag and len(points) > 2 and points[0] != points[-1]:
            segments.append((points[-1], points[0]))
    return segments


def polygonize(segments: Sequence[Tuple[Point, Point]], min_area_m2: float = 0.0, unit_factor: float = 1.0) -> List[ShapeRec]:
    """把线段缝合成闭合面（红线常画成四条 LINE）。需要 shapely。"""
    if not segments:
        return []
    try:
        from shapely.geometry import LineString
        from shapely.ops import polygonize as _polygonize
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("缺少依赖 shapely，请先执行：python -m pip install shapely") from exc
    lines = []
    for start, end in segments:
        if math.hypot(end[0] - start[0], end[1] - start[1]) < 1e-12:
            continue
        lines.append(LineString([start, end]))
    results: List[ShapeRec] = []
    for index, polygon in enumerate(_polygonize(lines)):
        if polygon.is_empty or polygon.area <= 0:
            continue
        area_m2 = float(polygon.area) * unit_factor * unit_factor
        if area_m2 < min_area_m2:
            continue
        rings = [list(polygon.exterior.coords)[:-1]] if polygon.exterior else []
        for interior in polygon.interiors:
            rings.append(list(interior.coords)[:-1])
        results.append(
            ShapeRec(
                handle=f"polygonize-{index + 1}",
                layer="",
                rings=[[(float(p[0]), float(p[1])) for p in ring] for ring in rings],
                geometry=polygon,
                area_m2=area_m2,
                source_kind="polygonize",
                close_kind="polygonize",
            )
        )
    return results


def to_rings(geometry) -> List[List[Point]]:
    """shapely 几何 → 环列表（外环 + 全部内环），用于写回多段线。"""
    if geometry is None or getattr(geometry, "is_empty", True):
        return []
    polygons = []
    geom_type = getattr(geometry, "geom_type", "")
    if geom_type == "Polygon":
        polygons = [geometry]
    elif geom_type in {"MultiPolygon", "GeometryCollection"}:
        polygons = [item for item in getattr(geometry, "geoms", []) if getattr(item, "geom_type", "") == "Polygon"]
    rings: List[List[Point]] = []
    for polygon in polygons:
        if polygon.exterior:
            rings.append([(float(p[0]), float(p[1])) for p in list(polygon.exterior.coords)[:-1]])
        for interior in polygon.interiors:
            rings.append([(float(p[0]), float(p[1])) for p in list(interior.coords)[:-1]])
    return rings


def polygons_only(geometry) -> List:
    """shapely 任意几何 → 多边形列表（丢弃线/点）。"""
    if geometry is None or getattr(geometry, "is_empty", True):
        return []
    geom_type = getattr(geometry, "geom_type", "")
    if geom_type == "Polygon":
        return [geometry]
    if geom_type == "MultiPolygon":
        return list(geometry.geoms)
    if geom_type == "GeometryCollection":
        found = []
        for item in geometry.geoms:
            found.extend(polygons_only(item))
        return found
    return []
