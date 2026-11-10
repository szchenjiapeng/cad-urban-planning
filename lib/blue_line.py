"""蓝线（河口线）生成：水域边界 → 桥位断口识别与重连。

业务口径（用户确认）：
- 蓝线取河道两侧的**河口线**（水域面的边界线），不是水域面本身；
- 道路横穿河道时，总图上水域填充被切断（1701 在路段不填），水域边界出现落在
  道路内的**短边**（桥头断口）；
- 蓝线必须连续：删掉桥位短边，把短边两端各自连接的河口线端点重连（**两点直连**）；
- **河流沿道路走**时，水域长边与道路红线重合，那不是断口，必须保留，不能删也不能连。

断口判据（v1.2 起改为「端点跨路配对」，不再依赖固定的短边长度）：
1. 该段长度 ≤ bridge_gap → 候选断口（贴河长边远长于桥宽，直接保留）；
2. 候选段从边界中扣除后，若它的两个端点能与**另一块水域**的岸线端点在 bridge_gap 内
   配对、且连线穿过道路走廊，才算真断口并两点直连；
3. 配不到对 → 不猜，原样保留边界，并置 `ambiguous=True` 交给上层询问用户。
   （水域面被切成 1 块时同样保守不处理，只在报告里提示。）

bridge_mode：
- `connect`（A 口径，默认）：删桥位横切边，上下游两岸线端点直连，蓝线连续；
- `seal`（B 口径）：保持断开，按水域边界输出，桥位处两岸各自封口。

注意：v1.1 用「段长 ≤ bridge_gap 且法向两侧都在水域内」判断口，遇到「水域填充截到
道路红线为止」的图（断口边压在红线上、一侧是道路）会一条都认不出来，且宽河道（>30 m）
会被误判成贴河长边；v1.2 的配对判据同时解决这两个问题。
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

DEFAULT_BRIDGE_GAP_M = 60.0
DEFAULT_TOLERANCE_M = 0.5
# 短于该值的「与红线重合段」是岸线斜穿红线路的切片，不是桥位横切边
MIN_CUT_EDGE_M = 3.0


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


def _polygon_parts(geometry) -> List:
    if geometry is None or getattr(geometry, "is_empty", True):
        return []
    kind = getattr(geometry, "geom_type", "")
    if kind == "Polygon":
        return [geometry]
    if kind in {"MultiPolygon", "GeometryCollection"}:
        parts: List = []
        for item in getattr(geometry, "geoms", []):
            parts.extend(_polygon_parts(item))
        return parts
    return []


def _same_line(line_a, line_b, eps: float) -> bool:
    """两条线是否重复（端点互在 eps 内，含首尾颠倒）。桥位两侧各判一次会给出几乎重合的连线。"""
    a = list(getattr(line_a, "coords", []))
    b = list(getattr(line_b, "coords", []))
    if len(a) < 2 or len(b) < 2:
        return False
    a0, a1, b0, b1 = a[0], a[-1], b[0], b[-1]
    straight = max(math.hypot(a0[0] - b0[0], a0[1] - b0[1]), math.hypot(a1[0] - b1[0], a1[1] - b1[1]))
    reversed_ = max(math.hypot(a0[0] - b1[0], a0[1] - b1[1]), math.hypot(a1[0] - b0[0], a1[1] - b0[1]))
    return min(straight, reversed_) <= eps


def _is_transverse(part, connector) -> bool:
    """横切边与配对连线的夹角是否够大（桥位：横切边横跨河口、连线沿水流方向，接近垂直）。

    贴河边与沿路连线是同向的（夹角接近 0），用这条排除「沿红线错误连线」。
    """
    a = list(getattr(part, "coords", []))
    b = list(getattr(connector, "coords", []))
    if len(a) < 2 or len(b) < 2:
        return False
    ax, ay = a[-1][0] - a[0][0], a[-1][1] - a[0][1]
    bx, by = b[-1][0] - b[0][0], b[-1][1] - b[0][1]
    na = math.hypot(ax, ay)
    nb = math.hypot(bx, by)
    if na < 1e-9 or nb < 1e-9:
        return False
    return abs((ax * bx + ay * by) / (na * nb)) <= 0.5


def _both_sides_in_water(part, water_zone, tol: float) -> bool:
    """短边法向两侧是否都落在水域内（仅作为统计信号，不参与断口判定）。"""
    try:
        from shapely.geometry import Point
    except Exception:  # pragma: no cover
        return False
    coords = list(getattr(part, "coords", []))
    if len(coords) < 2:
        return False
    (ax, ay), (bx, by) = coords[0], coords[-1]
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return False
    nx, ny = -dy / length, dx / length
    mx, my = (ax + bx) / 2.0, (ay + by) / 2.0
    step = max(float(tol) * 2.0, 1e-6)
    try:
        p1 = Point(mx + nx * step, my + ny * step)
        p2 = Point(mx - nx * step, my - ny * step)
        return bool(water_zone.contains(p1) and water_zone.contains(p2))
    except Exception:
        return False


def _rings_of(pieces) -> List[Tuple[int, object]]:
    """水域块 → [(块号, 环线)]，含内环。"""
    from shapely.geometry import LineString

    rings: List[Tuple[int, object]] = []
    for piece_index, piece in enumerate(pieces):
        for ring in [piece.exterior] + list(piece.interiors):
            if ring is None:
                continue
            coords = [(float(p[0]), float(p[1])) for p in ring.coords]
            if len(coords) < 4:
                continue
            rings.append((piece_index, LineString(coords)))
    return rings


def _bank_endpoints(rings, road_zone) -> List[Tuple[object, int]]:
    """道路走廊之外的岸线端点（配对端点来源）。"""
    ends: List[Tuple[object, int]] = []
    for piece_index, ring in rings:
        for seg in _line_parts(ring.difference(road_zone)):
            coords = list(getattr(seg, "coords", []))
            if len(coords) < 2 or float(seg.length) <= 0:
                continue
            ends.append((coords[0], piece_index))
            ends.append((coords[-1], piece_index))
    return ends


def measure_bridge_gap(
    water,
    roads,
    unit_scale: float = 1.0,
    tolerance_m: float = DEFAULT_TOLERANCE_M,
    scan_limit_m: float = 200.0,
) -> Dict:
    """执行前扫描桥位候选边的「最近配对距离」，用于询问用户桥宽上限。

    返回 {"water_pieces", "candidates", "max_distance", "median_distance",
    "suggest_bridge_gap", "distances"}，距离单位一律为米。
    桥宽上限小于某条候选边的配对距离时，该桥位上下游**不会连接**，必须让用户知情后决定。
    """
    result: Dict = {
        "water_pieces": 0,
        "candidates": 0,
        "max_distance": 0.0,
        "median_distance": 0.0,
        "suggest_bridge_gap": 0.0,
        "distances": [],
    }
    if water is None or getattr(water, "is_empty", True):
        return result
    if roads is None or getattr(roads, "is_empty", True):
        return result
    try:
        from shapely.geometry import LineString, Point
    except Exception:  # pragma: no cover
        return result

    scale = float(unit_scale) or 1.0
    tol = float(tolerance_m) / scale
    limit = float(scan_limit_m) / scale
    road_zone = roads.buffer(tol)
    water_zone = water.buffer(tol)
    pieces = _polygon_parts(water)
    result["water_pieces"] = len(pieces)
    if len(pieces) < 2:
        return result

    rings = _rings_of(pieces)
    ends = _bank_endpoints(rings, road_zone)
    if not ends:
        return result
    min_cut = max(MIN_CUT_EDGE_M / scale, tol * 2.0)

    distances: List[float] = []
    for piece_index, ring in rings:
        for part in _line_parts(ring.intersection(road_zone)):
            coords = list(getattr(part, "coords", []))
            if len(coords) < 2 or float(getattr(part, "length", 0.0)) < min_cut:
                continue
            found = []
            for raw in (coords[0], coords[-1]):
                anchor = Point(raw)
                best = None
                for other_raw, other_piece in ends:
                    if other_piece == piece_index:
                        continue
                    distance = anchor.distance(Point(other_raw))
                    if distance > limit:
                        continue
                    if best is None or distance < best[0]:
                        best = (distance, other_raw)
                if best is None:
                    break
                mid = Point((raw[0] + best[1][0]) / 2.0, (raw[1] + best[1][1]) / 2.0)
                if water_zone.contains(mid):
                    break
                try:
                    connector = LineString([raw, best[1]])
                except Exception:
                    break
                if not _is_transverse(part, connector):
                    break
                found.append(best[0] * scale)
            if len(found) == 2:
                distances.append(max(found))

    if not distances:
        return result
    distances.sort()
    result["candidates"] = len(distances)
    result["max_distance"] = round(distances[-1], 1)
    result["median_distance"] = round(distances[len(distances) // 2], 1)
    # 建议值留 20% 余量：按红线间距量取通常比水域面间距大（河岸还压着绿带）
    result["suggest_bridge_gap"] = float(math.ceil(distances[-1] * 1.2 / 10.0) * 10.0)
    result["distances"] = [round(value, 1) for value in distances[-8:]]
    return result


def build_bank_lines(
    water,
    roads=None,
    unit_scale: float = 1.0,
    bridge_gap_m: float = DEFAULT_BRIDGE_GAP_M,
    tolerance_m: float = DEFAULT_TOLERANCE_M,
    cross_road: bool = True,
    bridge_mode: str = "connect",
) -> Tuple[Optional[object], Dict]:
    """水域几何 → 连续河口线。

    unit_scale: 图纸单位 → 米 的系数（与 shapes 的 unit_factor 一致）。
    bridge_mode: connect（A 贯通）/ seal（B 保持断开）。
    返回 (几何, 统计信息)：{"short_segments", "connectors", "kept_edges",
    "bridge_mode", "ambiguous", "suggest_bridge_gap", "notes", ...}。
    """
    mode = (bridge_mode or "connect").strip().lower()
    info: Dict = {
        "short_segments": 0,
        "connectors": 0,
        "kept_edges": 0,
        "bridge_mode": mode,
        "bridge_gap_m": float(bridge_gap_m),
        "ambiguous": False,
        "suggest_bridge_gap": 0.0,
        "both_sides_water": 0,
        "notes": [],
    }
    if water is None or getattr(water, "is_empty", True):
        return None, info
    try:
        from shapely.geometry import LineString, MultiLineString, Point
        from shapely.ops import unary_union
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("缺少依赖 shapely，请先执行：python -m pip install shapely") from exc

    boundary = water.boundary
    if roads is None or getattr(roads, "is_empty", True) or not cross_road:
        info["notes"].append("未提供道路范围或已关闭跨路连接，蓝线按水域边界输出")
        return boundary, info

    scale = float(unit_scale) or 1.0
    gap = float(bridge_gap_m) / scale
    tol = float(tolerance_m) / scale
    road_zone = roads.buffer(tol)
    water_zone = water.buffer(tol)

    pieces = _polygon_parts(water)
    if len(pieces) < 2:
        info["kept_edges"] = len(_line_parts(boundary.intersection(road_zone)))
        info["notes"].append(
            "水域填充连续（1 块），未检测到桥位断口，与道路重合的河口线全部保留；如桥位处填充未断开请手动处理"
        )
        return boundary, info
    info["water_pieces"] = len(pieces)

    if mode == "seal":
        info["kept_edges"] = len(_line_parts(boundary.intersection(road_zone)))
        info["notes"].append("按 B 口径（保持断开）输出，桥位处两岸各自封口，未做跨路连接")
        return boundary, info

    # 逐块切边界：不与道路重叠的是确定岸线；与道路重叠的段，用「端点能否跨路配对」
    # 判定断口还是贴河——桥位横切边的两端正好接上下游对岸岸线，贴河长边对岸没有水域。
    rings = _rings_of(pieces)
    ends = _bank_endpoints(rings, road_zone)
    if not ends:
        info["notes"].append("水域边界几乎全部与道路重合，无法提取河口线，请复核水域与道路图层")
        return boundary, info

    def _partner(raw, piece_index, limit):
        """在**另一块水域**的岸线端点里找配对端点，返回 (候选连线, 距离)。limit=None 表示不限距离。

        配对条件：不同水域块、距离在限值内、连线中点在**水域之外**（说明中间隔着桥位而不是
        同一片水面），且中点离道路红线不远（否则会把两个无关水塘连起来）。
        """
        anchor = Point(raw)
        best = None
        for other_raw, other_piece in ends:
            if other_piece == piece_index:
                continue
            try:
                distance = anchor.distance(Point(other_raw))
            except Exception:
                continue
            if limit is not None and distance > limit:
                continue
            if best is None or distance < best[0]:
                best = (distance, other_raw)
        if best is None:
            return None
        try:
            connector = LineString([raw, best[1]])
        except Exception:
            return None
        mid = Point((raw[0] + best[1][0]) / 2.0, (raw[1] + best[1][1]) / 2.0)
        if water_zone.contains(mid):
            return None
        if mid.distance(roads) > gap:
            return None
        return connector, float(best[0])

    cut_parts: List = []
    cut_lengths: List[float] = []
    connectors: List = []
    along_road = 0
    near_miss: List[float] = []
    dup_eps = max(tol * 3.0, 1.5 / scale)
    scan_limit = max(gap * 5.0, 150.0 / scale)
    min_cut = max(MIN_CUT_EDGE_M / scale, tol * 2.0)
    for piece_index, ring in rings:
        for part in _line_parts(ring.intersection(road_zone)):
            coords = list(getattr(part, "coords", []))
            if len(coords) < 2:
                continue
            length = float(getattr(part, "length", 0.0))
            if length <= 0:
                continue
            if length < min_cut:
                # 岸线斜穿 0.5 m 红线路的切片，不是桥位横切边
                continue
            hits = []
            misses = []
            for raw in (coords[0], coords[-1]):
                found = _partner(raw, piece_index, gap)
                if found is not None:
                    hits.append(found)
                    continue
                far = _partner(raw, piece_index, scan_limit)
                if far is not None:
                    misses.append(far[1])
            if len(hits) == 2 and all(_is_transverse(part, connector) for connector, _dist in hits):
                cut_parts.append(part)
                cut_lengths.append(length * scale)
                if _both_sides_in_water(part, water_zone, tol):
                    info["both_sides_water"] += 1
                for connector, _distance in hits:
                    if any(_same_line(connector, existing, dup_eps) for existing in connectors):
                        continue
                    connectors.append(connector)
            else:
                # 贴河长边：河流沿道路走，对岸没有水域，保留
                along_road += 1
                near_miss.extend(misses)

    info["kept_edges"] = along_road
    info["short_segments"] = len(cut_lengths)
    info["connectors"] = len(connectors)
    if cut_lengths:
        info["suggest_bridge_gap"] = math.ceil(max(cut_lengths) / 10.0) * 10.0
    if along_road:
        info["notes"].append(f"另有 {along_road} 段与道路重合，判为贴河保留")

    if not cut_lengths:
        if near_miss:
            # 认得出「对岸有水域」，但超过了当前桥宽上限 —— 不替用户决定，交给确认协议
            info["ambiguous"] = True
            info["suggest_bridge_gap"] = math.ceil(max(near_miss) / 10.0) * 10.0
            info["notes"].append(
                f"{along_road} 段与道路重合的河口线对岸存在水域，但配对距离超过桥宽上限 "
                f"{float(bridge_gap_m):.0f} m（实测约 {max(near_miss):.1f} m），已原样保留，需确认桥位口径"
            )
        else:
            info["notes"].append(f"与道路重合的河口线 {along_road} 段均判为贴河，保留未删")
        return boundary, info

    if not connectors:
        info["ambiguous"] = True
        info["notes"].append("识别到桥位横切边，但岸线端点配对未成功，已原样保留，需确认桥位口径")
        return boundary, info

    # 只删掉桥位横切边，其余岸线保持原样（不按红线路走廊切碎，避免输出一堆碎线）
    holes = [part.buffer(max(tol * 0.02, 1e-9)) for part in cut_parts]
    removed = unary_union(holes)
    min_draw = max(2.0 / scale, 1e-9)  # 去掉切除与配对产生的碎片（<2 m）
    kept = [seg for seg in _line_parts(boundary.difference(removed)) if float(seg.length) >= min_draw]
    if not kept:
        info["notes"].append("删除桥位横切边后无剩余河口线，请复核水域图层")
        return None, info
    connectors = [seg for seg in connectors if float(seg.length) >= min_draw]
    # 不做 unary_union：它会在每处交点把线打断，输出一堆碎段（甚至 0 长度段）；
    # 岸线之间本来就不需要拓扑融合，直接拼成 MultiLineString 写图即可。
    parts = kept + connectors
    if len(parts) == 1:
        return parts[0], info
    return MultiLineString(parts), info
