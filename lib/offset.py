"""偏移/内缩/求差：退线与五线共用的空间运算。

硬性：
- 退线是合规相关结果，**缺 shapely 必须报错退出，不得静默降级成近似算法**；
- 锐角内缩会产生自交或退化面，统一在这里判定并返回异常，由上层列入清单。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

SHAPELY_HINT = "缺少依赖 shapely，请先执行：python -m pip install shapely"


def require_shapely() -> None:
    try:
        import shapely  # noqa: F401
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(SHAPELY_HINT) from exc


@dataclass
class OffsetResult:
    geometry: Any = None
    ok: bool = True
    reason: str = ""
    area_m2: float = 0.0
    rings: List[List[tuple]] = field(default_factory=list)


def _join_style(name: str):
    from shapely.geometry import JOIN_STYLE

    key = (name or "mitre").lower()
    if key in {"round", "圆弧"}:
        return JOIN_STYLE.round
    if key in {"bevel", "斜切"}:
        return JOIN_STYLE.bevel
    return JOIN_STYLE.mitre


def shrink(geometry, distance: float, join: str = "mitre", mitre_limit: float = 5.0) -> OffsetResult:
    """多边形向内收缩 distance（同一单位）。distance<=0 返回原几何。"""
    require_shapely()
    if geometry is None or getattr(geometry, "is_empty", True):
        return OffsetResult(ok=False, reason="基准几何为空")
    try:
        if distance <= 0:
            result = geometry
        else:
            result = geometry.buffer(
                -float(distance),
                join_style=_join_style(join),
                mitre_limit=float(mitre_limit),
            )
    except Exception as exc:
        return OffsetResult(ok=False, reason=f"内缩失败：{exc}")
    if result is None or result.is_empty:
        return OffsetResult(ok=False, reason=f"内缩 {distance:g} 后退化（距离超过地块宽度）")
    if not result.is_valid:
        try:
            result = result.buffer(0)
        except Exception:
            pass
    if result.is_empty or result.area <= 0:
        return OffsetResult(ok=False, reason=f"内缩 {distance:g} 后面积为 0")
    return OffsetResult(geometry=result, ok=True, area_m2=float(result.area))


def strip(geometry, distance: float, join: str = "mitre", mitre_limit: float = 5.0) -> OffsetResult:
    """取「边界到内缩线之间」的环状带，用于判定建筑是否压退线。"""
    require_shapely()
    inner = shrink(geometry, distance, join=join, mitre_limit=mitre_limit)
    if not inner.ok:
        return inner
    try:
        band = geometry.difference(inner.geometry)
    except Exception as exc:
        return OffsetResult(ok=False, reason=f"退线环带计算失败：{exc}")
    if band is None or band.is_empty:
        return OffsetResult(ok=True, geometry=None, area_m2=0.0)
    return OffsetResult(geometry=band, ok=True, area_m2=float(band.area))


def expand(geometry, distance: float, join: str = "round"):
    require_shapely()
    if geometry is None or getattr(geometry, "is_empty", True):
        return None
    if distance <= 0:
        return geometry
    return geometry.buffer(float(distance), join_style=_join_style(join))


def union(geometries: List[Any]):
    require_shapely()
    from shapely.ops import unary_union

    items = [item for item in geometries if item is not None and not getattr(item, "is_empty", True)]
    if not items:
        return None
    if len(items) == 1:
        return items[0]
    return unary_union(items)


def intersects(a, b) -> bool:
    if a is None or b is None:
        return False
    try:
        return bool(a.intersects(b))
    except Exception:
        return False


def intersection_area(a, b) -> float:
    if a is None or b is None:
        return 0.0
    try:
        return float(a.intersection(b).area)
    except Exception:
        return 0.0


def shrink_except(
    geometry,
    default_distance: float,
    relaxed: Optional[List[Any]] = None,
    join: str = "mitre",
    mitre_limit: float = 5.0,
) -> OffsetResult:
    """整块按 default_distance 内缩；`relaxed` 中的边段按更小距离控制。

    relaxed: [(边段 LineString, 更小退距), ...]，典型场景是「临绿地水系一侧退 5m」。
    做法：先整块内缩到 default_distance，再把这些边段「多退的那一圈」还回来，
    交界处自然形成台阶；用圆头连接避免端点缺口。
    """
    require_shapely()
    inner = shrink(geometry, default_distance, join=join, mitre_limit=mitre_limit)
    if not inner.ok:
        return inner
    buildable = inner.geometry
    for item in relaxed or []:
        try:
            line, distance = item
            target = float(distance)
            default = float(default_distance)
        except Exception:
            continue
        if line is None or target >= default:
            continue
        try:
            outer_band = line.buffer(default, cap_style=1, join_style=1)
            inner_band = line.buffer(target, cap_style=1, join_style=1)
            give_back = outer_band.difference(inner_band)
            # 转角处相邻边段仍按默认退距控制（两条边同时约束时取严），
            # 否则绿地边段会把角部也一并放宽。
            try:
                rest = geometry.boundary.difference(line.buffer(0.01))
                if rest is not None and not rest.is_empty:
                    give_back = give_back.difference(rest.buffer(default, cap_style=1, join_style=1))
            except Exception:
                pass
            give_back = give_back.intersection(geometry)
            if give_back is None or give_back.is_empty:
                continue
            buildable = buildable.union(give_back)
        except Exception:
            continue
    if buildable is None or getattr(buildable, "is_empty", True):
        return OffsetResult(ok=False, reason=f"内缩 {default_distance:g} 后面积为 0")
    if not buildable.is_valid:
        try:
            buildable = buildable.buffer(0)
        except Exception:
            pass
    if getattr(buildable, "is_empty", True) or buildable.area <= 0:
        return OffsetResult(ok=False, reason=f"内缩 {default_distance:g} 后面积为 0")
    return OffsetResult(geometry=buildable, ok=True, area_m2=float(buildable.area))
