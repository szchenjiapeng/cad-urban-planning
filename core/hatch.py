"""填充（HATCH）边界重建与净面积计算。

用途：CAD 的填充对象常常是设计人员唯一规范绘制的用地界线；当 AutoCAD 的
ActiveX 接口读不到 HATCH.Area（报“输入无效”、GetLoopAt 返回空）时，改用
ezdxf 解析 DXF 中的边界几何，逐环重建多边形后按奇偶嵌套规则（外环 + 孤岛）
求净面积，避免把孤岛重复计入。
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from core.geometry import bbox_of, discretize, point_in_polygon, polygon_area  # noqa: E402

Point = Tuple[float, float]
_ARC_STEP_DEG = 0.5
_FLATTEN_DIST = 0.5  # ezdxf flattening 的弦高容差（图纸单位；米制图约 0.5 m）
_TOL = 1e-9


# ---------------------------------------------------------------- 边界提取
def rings_from_ezdxf_path(hatch, distance: float = _FLATTEN_DIST) -> List[List[Point]]:
    """用 ezdxf 官方 path.from_hatch + flattening 重建边界环（首选实现）。

    自写的弧段离散在个别填充上会把弧画到反侧（本图 1401 公园绿地 1032D27 外环
    因此自交成 MultiPolygon，面积 637,635 而 CAD 为 589,887），ezdxf 的离散
    与该填充的边界一致（589,738，差 0.03%），故优先使用。
    """
    try:
        from ezdxf import path as ezpath
    except Exception:
        return []
    rings: List[List[Point]] = []
    try:
        paths = list(ezpath.from_hatch(hatch))
    except Exception:
        return []
    for path in paths:
        try:
            pts = [(float(p.x), float(p.y)) for p in path.flattening(distance)]
        except Exception:
            continue
        cleaned = _dedupe_ring(pts)
        if len(cleaned) >= 3:
            rings.append(cleaned)
    return rings


def rings_from_hatch(hatch, prefer_minor: bool = True) -> List[List[Point]]:
    """从 ezdxf HATCH 实体提取全部边界环（外环与孤岛都可能存在）。

    prefer_minor=False 时完全信任 DXF 里的 ccw 标志（保留真正的优弧），
    用于和 CAD 自算面积对账时挑选更准的那一套离散。
    """
    if prefer_minor:
        fast = rings_from_ezdxf_path(hatch)
        if fast:
            return fast
    rings: List[List[Point]] = []
    try:
        paths = hatch.paths
    except Exception:
        return rings
    for path in paths:
        try:
            points = _path_points(path, prefer_minor)
        except Exception:
            continue
        cleaned = _dedupe_ring(points)
        if len(cleaned) >= 3:
            rings.append(cleaned)
    return rings


def _has_curved_boundary(hatch) -> bool:
    """填充边界里是否含弧/椭圆（只有这类才需要两套离散对比）。"""
    try:
        paths = hatch.paths
    except Exception:
        return False
    for path in paths:
        vertices = getattr(path, "vertices", None)
        if vertices:
            for vertex in vertices:
                if isinstance(vertex, (tuple, list)):
                    bulge = float(vertex[2]) if len(vertex) > 2 else 0.0
                else:
                    bulge = float(getattr(vertex, "bulge", 0.0) or 0.0)
                if abs(bulge) > 1e-9:
                    return True
        for edge in getattr(path, "edges", None) or []:
            if int(getattr(edge, "type", 0) or 0) in (2, 3):
                return True
    return False


def rings_variants(hatch) -> Tuple[List[List[Point]], Optional[List[List[Point]]]]:
    """返回 (主用离散, 备选离散)。

    主用 = 现有「弧段取劣弧」策略；备选 = 信任 ccw 标志。只有边界含弧/椭圆时
    才生成备选，调用方拿到 CAD 自算面积后选更接近的那一套——DXF 里 ccw
    标反的情况很多，硬编码某一套会在个别图上放大成倍。
    """
    primary = rings_from_hatch(hatch, prefer_minor=True)
    if not primary or not _has_curved_boundary(hatch):
        return primary, None
    alternative = rings_from_hatch(hatch, prefer_minor=False)
    if not alternative or len(alternative) != len(primary):
        return primary, None
    return primary, alternative


def _path_points(path, prefer_minor: bool = True) -> List[Point]:
    vertices = getattr(path, "vertices", None)
    if vertices:
        verts: List[Point] = []
        bulges: List[float] = []
        for vertex in vertices:
            # ezdxf 的 PolylinePath.vertices 是 (x, y, bulge) 元组；兼容对象形式的旧版本
            if isinstance(vertex, (tuple, list)):
                verts.append((float(vertex[0]), float(vertex[1])))
                bulges.append(float(vertex[2]) if len(vertex) > 2 else 0.0)
            else:
                verts.append((float(vertex.x), float(vertex.y)))
                bulges.append(float(getattr(vertex, "bulge", 0.0) or 0.0))
        return discretize(verts, bulges, close=True, max_step_deg=_ARC_STEP_DEG)

    edges = getattr(path, "edges", None)
    points: List[Point] = []
    if edges:
        for edge in edges:
            points.extend(_edge_points(edge, prefer_minor))
    return points


def _edge_points(edge, prefer_minor: bool = True) -> List[Point]:
    etype = int(getattr(edge, "type", 0) or 0)
    if etype == 1:  # LINE
        return [_vec(edge.start), _vec(edge.end)]
    if etype == 2:  # ARC
        return _arc_points(edge, prefer_minor)
    if etype == 3:  # ELLIPSE
        return _ellipse_points(edge, prefer_minor)
    if etype == 4:  # SPLINE
        return _spline_points(edge)
    return []


def _vec(value) -> Point:
    return (float(value.x), float(value.y))


def _resolve_sweep(start: float, end: float, ccw: bool, prefer_minor: bool = True) -> float:
    """按 ccw 求有符号扫掠角（弧度），并修正「优弧方向被标反」的问题。

    很多导出会把弧边的 ccw 标反：按标记得到 250°~300° 的优弧，而实际是 60°~110° 的短弧
    （HATCH 边界里的弧段几乎都是劣弧，真正的优弧通常被拆成两段）。若按错方向展开，
    环会被拉成近整圈，面积成倍放大（本图 1032D27 因此从 63.8 ha 涨到 186.9 ha）。

    因此 |sweep| > 180° 时回退到另一方向（短弧）。
    """
    sweep = end - start
    if ccw:
        while sweep <= 0:
            sweep += 2 * math.pi
    else:
        while sweep >= 0:
            sweep -= 2 * math.pi
    if prefer_minor and abs(sweep) > math.pi:
        alternative = sweep - math.copysign(2 * math.pi, sweep)
        if abs(alternative) < abs(sweep):
            sweep = alternative
    return sweep


def _arc_points(edge, prefer_minor: bool = True) -> List[Point]:
    cx, cy = float(edge.center.x), float(edge.center.y)
    radius = float(edge.radius)
    start = math.radians(float(edge.start_angle))
    end = math.radians(float(edge.end_angle))
    sweep = _resolve_sweep(start, end, bool(edge.ccw), prefer_minor)
    steps = max(2, int(abs(math.degrees(sweep)) / _ARC_STEP_DEG) + 1)
    points: List[Point] = []
    for i in range(steps + 1):
        angle = start + sweep * i / steps
        points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return points


def _ellipse_points(edge, prefer_minor: bool = True) -> List[Point]:
    cx, cy = float(edge.center.x), float(edge.center.y)
    mx, my = float(edge.major_axis.x), float(edge.major_axis.y)
    ratio = float(edge.ratio)
    nx, ny = -my * ratio, mx * ratio  # 次轴 = 主轴逆时针旋转 90° 后按比率缩放
    start = float(edge.start_param)
    end = float(edge.end_param)
    sweep = _resolve_sweep(start, end, bool(edge.ccw), prefer_minor)
    steps = max(2, int(abs(math.degrees(sweep)) / _ARC_STEP_DEG) + 1)
    points: List[Point] = []
    for i in range(steps + 1):
        t = start + sweep * i / steps
        points.append(
            (
                cx + mx * math.cos(t) + nx * math.sin(t),
                cy + my * math.cos(t) + ny * math.sin(t),
            )
        )
    return points


def _spline_points(edge) -> List[Point]:
    try:
        tool = edge.construction_tool()
        return [(float(p.x), float(p.y)) for p in tool.flattening(0.05)]
    except Exception:
        return []


def _dedupe_ring(points: Sequence[Point], tolerance: float = _TOL) -> List[Point]:
    ring: List[Point] = []
    for point in points:
        if not ring or abs(point[0] - ring[-1][0]) > tolerance or abs(point[1] - ring[-1][1]) > tolerance:
            ring.append((float(point[0]), float(point[1])))
    if len(ring) > 1 and abs(ring[0][0] - ring[-1][0]) <= tolerance and abs(ring[0][1] - ring[-1][1]) <= tolerance:
        ring.pop()
    return ring


# ---------------------------------------------------------------- 嵌套与净面积
def interior_point(points: Sequence[Point]) -> Optional[Point]:
    """取一个确实落在多边形内部的点，用于判定环的嵌套层级。"""
    if len(points) < 3:
        return None
    box = bbox_of(points)
    candidates_y = [
        (box[1] + box[3]) / 2.0,
        box[1] + (box[3] - box[1]) * 0.25,
        box[1] + (box[3] - box[1]) * 0.75,
    ]
    center = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
    if point_in_polygon(center, points):
        return center

    count = len(points)
    for y in candidates_y:
        crossings: List[float] = []
        for i in range(count):
            x1, y1 = points[i]
            x2, y2 = points[(i + 1) % count]
            if (y1 > y) != (y2 > y):
                crossings.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
        crossings.sort()
        for index in range(0, len(crossings) - 1, 2):
            middle = (crossings[index] + crossings[index + 1]) / 2.0
            if point_in_polygon((middle, y), points):
                return (middle, y)
    return None


def _net_area_geom(rings: Sequence[Sequence[Point]], min_ring_area: float):
    """多环填充的精确净面积；shapely 不可用或单环时返回 None（交给纯 python 分支）。"""
    try:
        from shapely.geometry import Polygon
        from shapely.ops import unary_union
    except Exception:
        return None
    if len(rings) < 2:
        return None
    valid: List[Tuple] = []
    for ring in rings:
        if len(ring) < 3:
            continue
        area = polygon_area(ring)
        if area <= min_ring_area:
            continue
        pts = [(float(p[0]), float(p[1])) for p in ring]
        try:
            geom = Polygon(pts)
            if not geom.is_valid:
                geom = geom.buffer(0)
        except Exception:
            continue
        if geom.is_empty or geom.area <= 0:
            continue
        valid.append((geom, pts, area))
    if len(valid) < 2:
        return None
    return _layered_net_area(valid, unary_union, min_ring_area)


def _compute_depths(valid: List[Tuple]) -> List[int]:
    """按包含关系算嵌套深度：深度 1=实体，2=孤岛，3=岛中岛。

    严�� covers() 对「贴边/共点」的孤岛会返回 False（本图 1032D27 就因此漏扣一个
    约 1.46 ha 的孤岛），因此加入面积占比兜底：交叠达到 98% 即视为嵌套。
    """
    depths: List[int] = []
    for i, (_geom, _pts, area) in enumerate(valid):
        depth = 1
        for j, (other, _opts, other_area) in enumerate(valid):
            if i == j or other_area <= area:
                continue
            try:
                if other.covers(valid[i][0]):
                    depth += 1
                    continue
            except Exception:
                pass
            try:
                if other.intersection(valid[i][0]).area >= area * 0.98:
                    depth += 1
            except Exception:
                continue
        depths.append(depth)
    return depths


def _layered_net_area(valid: List[Tuple], unary_union, min_ring_area: float):
    """按嵌套深度分层求净面积：深度奇数并集、偶数扣除，等价 A−B−C−D−E、A−B+C。"""
    depths = _compute_depths(valid)

    # 同深度且互相重叠 = 非纯嵌套（如 B、C 都压在 A 里且彼此交叉），需按并集只扣一次
    overlap_same_depth = False
    for i in range(len(valid)):
        for j in range(i + 1, len(valid)):
            if depths[i] != depths[j]:
                continue
            try:
                if valid[i][0].intersection(valid[j][0]).area > max(min_ring_area, 0.5):
                    overlap_same_depth = True
            except Exception:
                continue
    return _assemble_net(valid, depths, overlap_same_depth, unary_union)


def _assemble_geometry(valid: List[Tuple], depths: List[int], unary_union):
    """分层并集几何：depth1 − depth2 + depth3 …，同深度重叠按并集只扣一次。"""
    groups: dict = {}
    for idx, depth in enumerate(depths):
        groups.setdefault(depth, []).append(valid[idx][0])
    unions = {}
    for depth, geoms in groups.items():
        try:
            unions[depth] = geoms[0] if len(geoms) == 1 else unary_union(geoms)
        except Exception:
            return None
    result = None
    depth = 1
    while depth in unions:
        layer = unions[depth]
        if (depth + 1) in unions:
            try:
                layer = layer.difference(unions[depth + 1])
            except Exception:
                return None
        try:
            result = layer if result is None else result.union(layer)
        except Exception:
            return None
        depth += 2
    return result


def _assemble_net(valid: List[Tuple], depths: List[int], overlap_same_depth: bool, unary_union):
    """在分层几何上求面积，并输出每个环的深度信息。"""
    result = _assemble_geometry(valid, depths, unary_union)
    total = result.area if result is not None else 0.0
    infos = [
        {
            "index": idx,
            "area": valid[idx][2],
            "depth": depths[idx],
            "sign": 1 if depths[idx] % 2 == 1 else -1,
            "ring": valid[idx][1],
            "overlap_same_depth": overlap_same_depth,
        }
        for idx in range(len(depths))
    ]
    return max(total, 0.0), infos


def net_area(rings: Sequence[Sequence[Point]], min_ring_area: float = 1e-9) -> Tuple[float, List[dict]]:
    """按奇偶嵌套规则求净面积：深度 1 计入，深度 2 扣除（孤岛），深度 3 再加回。"""
    fast = _net_area_geom(rings, min_ring_area)
    if fast is not None:
        return fast
    valid: List[List[Point]] = []
    areas: List[float] = []
    for ring in rings:
        if len(ring) < 3:
            continue
        area = polygon_area(ring)
        if area <= min_ring_area:
            continue
        valid.append([(float(p[0]), float(p[1])) for p in ring])
        areas.append(area)

    infos: List[dict] = []
    for index, ring in enumerate(valid):
        area = areas[index]
        anchor = interior_point(ring)
        depth = 1
        if anchor is not None:
            for other_index, other in enumerate(valid):
                if other_index == index:
                    continue
                if areas[other_index] > area and point_in_polygon(anchor, other):
                    depth += 1
        infos.append(
            {
                "index": index,
                "area": area,
                "depth": depth,
                "sign": 1 if depth % 2 == 1 else -1,
                "ring": ring,
            }
        )

    total = sum(item["area"] * item["sign"] for item in infos)
    return max(total, 0.0), infos


def net_geometry(rings: Sequence[Sequence[Point]], min_ring_area: float = 1e-9):
    """净几何：外环并集减孤岛，供重叠与裁剪判定。无 shapely 时返回 None。"""
    try:
        from shapely.geometry import Polygon
        from shapely.ops import unary_union
    except Exception:
        return None
    valid = _valid_rings(rings, min_ring_area)
    if not valid:
        return None
    if len(valid) == 1:
        return valid[0][0]
    return _assemble_geometry(valid, _compute_depths(valid), unary_union)


def _valid_rings(rings: Sequence[Sequence[Point]], min_ring_area: float = 1e-9) -> List[Tuple]:
    """环转 shapely 多边形并过滤退化环，返回 [(geom, pts, area)]。"""
    from shapely.geometry import Polygon

    valid: List[Tuple] = []
    for ring in rings or []:
        if len(ring) < 3:
            continue
        area = polygon_area(ring)
        if area <= min_ring_area:
            continue
        pts = [(float(p[0]), float(p[1])) for p in ring]
        try:
            geom = Polygon(pts)
            if not geom.is_valid:
                geom = geom.buffer(0)
        except Exception:
            continue
        if geom.is_empty or geom.area <= 0:
            continue
        valid.append((geom, pts, area))
    return valid


def outer_ring(infos: Sequence[dict]) -> Optional[List[Point]]:
    """取面积最大的外环（深度 1）。

    仅用于写回、包围盒等场景；**重叠/裁剪等空间运算必须用 net_geometry()**，
    否则带洞填充的孤岛会丢失，洞内的小地块会被判成整块重叠。
    """
    candidates = [item for item in infos if item["depth"] == 1] or list(infos)
    if not candidates:
        return None
    best = max(candidates, key=lambda item: item["area"])
    return best["ring"]


def hatch_geometry(hatch) -> Tuple[List[List[Point]], float, List[dict]]:
    """一站式：HATCH 实体 → (全部环, 净面积, 层级信息)。"""
    rings = rings_from_hatch(hatch)
    if not rings:
        return [], 0.0, []
    total, infos = net_area(rings)
    return rings, total, infos
