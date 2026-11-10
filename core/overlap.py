"""重叠处理：同图层重叠扣减面积，跨图层重叠保留各自统计并单独记录。

口径（与 SKILL.md 硬性规则一致）：
- 同一图层内的色块重叠，通常是绘图不规范，统计时只算一次：从较小地块扣减重叠部分；
- 不同图层之间的重叠，可能是分类口径差异（如「道路」层与「绿地」层各画了一块），
  保留各图层统计结果先出平衡表，重叠情况单独告知用户，是否要看明细由用户决定。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

try:  # shapely 为可选依赖
    from shapely.geometry import Polygon as _Polygon

    HAS_SHAPELY = True
except Exception:  # pragma: no cover
    _Polygon = None  # type: ignore[assignment]
    HAS_SHAPELY = False

from core.geometry import Shape, bbox_of, bbox_overlap_ratio, detect_overlaps  # noqa: E402
from core.hatch import net_geometry  # noqa: E402
from core.models import ParcelRecord  # noqa: E402


@dataclass
class DedupRecord:
    keep_handle: str
    drop_handle: str
    layer: str
    code: str
    overlap_m2: float
    deducted_from: str

    def to_dict(self) -> dict:
        return {
            "keep": self.keep_handle,
            "drop": self.drop_handle,
            "layer": self.layer,
            "code": self.code,
            "overlap_m2": round(self.overlap_m2, 2),
            "deducted_from": self.deducted_from,
        }


@dataclass
class CrossOverlap:
    layer_a: str
    handle_a: str
    code_a: str
    area_a_m2: float
    layer_b: str
    handle_b: str
    code_b: str
    area_b_m2: float
    overlap_m2: float
    ratio: float  # 占较小地块的比例（按 100% 封顶）
    approximate: bool = False
    oversize: bool = False  # 重叠面积超过较小地块面积（多为裁剪残片/重复描图）

    def to_dict(self) -> dict:
        return {
            "layer_a": self.layer_a,
            "handle_a": self.handle_a,
            "code_a": self.code_a,
            "area_a_m2": round(self.area_a_m2, 2),
            "layer_b": self.layer_b,
            "handle_b": self.handle_b,
            "code_b": self.code_b,
            "area_b_m2": round(self.area_b_m2, 2),
            "overlap_m2": round(self.overlap_m2, 2),
            "ratio": round(self.ratio, 4),
            "approximate": self.approximate,
            "oversize": self.oversize,
        }


def _polygon(points: Optional[Sequence[Tuple[float, float]]]):
    if not points or len(points) < 3 or _Polygon is None:
        return None
    try:
        geom = _Polygon(points)
        if not geom.is_valid:
            geom = geom.buffer(0)
        return geom if geom.area > 0 else None
    except Exception:
        return None


def _parcel_geometry(parcel: ParcelRecord, cache: Optional[Dict[int, object]] = None):
    """地块净几何：优先按全部环带洞重建，退化到外环。

    带洞填充（孤岛）如果只取外环，会把洞里的小地块判成整块重叠。
    """
    key = id(parcel)
    if cache is not None and key in cache:
        return cache[key]
    geom = None
    if parcel.rings:
        geom = net_geometry(parcel.rings)
    if geom is None:
        geom = _polygon(parcel.outer)
    if cache is not None:
        cache[key] = geom
    return geom


def _intersection_area(
    first: ParcelRecord,
    second: ParcelRecord,
    factor: float,
    bbox_ratio: float,
    cache: Optional[Dict[int, object]] = None,
) -> Tuple[float, bool]:
    """返回 (重叠面积 m², 是否为包围盒估算值)。"""
    geom_a = _parcel_geometry(first, cache)
    geom_b = _parcel_geometry(second, cache)
    if geom_a is not None and geom_b is not None:
        try:
            overlap = geom_a.intersection(geom_b).area * factor * factor
            return max(overlap, 0.0), False
        except Exception:
            pass
    # 无 shapely / 几何非法：用包围盒面积 × 估比重叠，量级参考
    small = min(first.area_m2, second.area_m2)
    return max(small * bbox_ratio * 0.5, 0.0), True


def resolve_overlaps(
    parcels: Sequence[ParcelRecord],
    factor: float,
    min_area_m2: float = 0.5,
    min_ratio: float = 0.01,
) -> Tuple[List[DedupRecord], List[CrossOverlap]]:
    """处理重叠对：同图层扣减、跨图层记录。

    同图层：从面积较小的地块里扣掉重叠部分（保持图层合计只算一次）。
    跨图层：不扣减，保留各图层原面积，只记录明细供用户判断。
    """
    dedup: List[DedupRecord] = []
    cross: List[CrossOverlap] = []

    shapes = [
        Shape(parcel.handle, parcel.layer, parcel.outer, parcel.area_m2, bbox_of(parcel.outer))
        for parcel in parcels
        if parcel.outer
    ]
    if len(shapes) < 2:
        return dedup, cross

    lookup: Dict[str, ParcelRecord] = {parcel.handle: parcel for parcel in parcels}
    geom_cache: Dict[int, object] = {}

    for first, second in detect_overlaps(shapes):
        parcel_a = lookup.get(first.handle)
        parcel_b = lookup.get(second.handle)
        if parcel_a is None or parcel_b is None:
            continue
        if parcel_a.area_m2 <= 0 or parcel_b.area_m2 <= 0:
            continue
        ratio_hint = bbox_overlap_ratio(bbox_of(parcel_a.outer), bbox_of(parcel_b.outer)) if parcel_a.outer and parcel_b.outer else 0.0
        overlap_m2, approximate = _intersection_area(parcel_a, parcel_b, factor, ratio_hint, geom_cache)
        if overlap_m2 < min_area_m2:
            continue
        small = min(parcel_a.area_m2, parcel_b.area_m2)
        # 裁剪后残片的 area_m2 可能远小于其原始几何，重叠按原始几何求交会算出 %>100 的荒唐比例，
        # 这里按 100% 封顶，并记录 oversize 供报告说明（不改变是否计入重叠对的判定）
        raw_ratio = overlap_m2 / small if small else 0.0
        ratio = min(raw_ratio, 1.0)
        oversize = raw_ratio > 1.02
        if ratio < min_ratio:
            continue

        if parcel_a.layer == parcel_b.layer:
            # 同图层：从较小的那个扣减，避免重复计面积
            target, other = (parcel_a, parcel_b) if parcel_a.area_m2 <= parcel_b.area_m2 else (parcel_b, parcel_a)
            cut = min(overlap_m2, target.area_m2)
            target.area_m2 = max(target.area_m2 - cut, 0.0)
            target.warnings.append(
                f"与同图层句柄 {other.handle} 重叠 {cut:.2f} m²，已扣减（图层合计只算一次）"
            )
            dedup.append(
                DedupRecord(
                    keep_handle=other.handle,
                    drop_handle=target.handle,
                    layer=target.layer,
                    code=target.code or "",
                    overlap_m2=cut,
                    deducted_from=target.handle,
                )
            )
        else:
            cross.append(
                CrossOverlap(
                    layer_a=parcel_a.layer,
                    handle_a=parcel_a.handle,
                    code_a=parcel_a.code or "",
                    area_a_m2=parcel_a.area_m2,
                    layer_b=parcel_b.layer,
                    handle_b=parcel_b.handle,
                    code_b=parcel_b.code or "",
                    area_b_m2=parcel_b.area_m2,
                    overlap_m2=overlap_m2,
                    ratio=ratio,
                    approximate=approximate,
                    oversize=oversize,
                )
            )

    cross.sort(key=lambda item: item.overlap_m2, reverse=True)
    dedup.sort(key=lambda item: item.overlap_m2, reverse=True)
    return dedup, cross


def summarize(dedup: Sequence[DedupRecord], cross: Sequence[CrossOverlap]) -> dict:
    """给报告/JSON 用的汇总块。"""
    cross_area = sum(item.overlap_m2 for item in cross)
    layers = sorted({item.layer_a for item in cross} | {item.layer_b for item in cross})
    return {
        "same_layer": {
            "count": len(dedup),
            "deducted_m2": round(sum(item.overlap_m2 for item in dedup), 2),
            "items": [item.to_dict() for item in dedup[:20]],
        },
        "cross_layer": {
            "count": len(cross),
            "area_m2": round(cross_area, 2),
            "layers": layers,
            "approximate": any(item.approximate for item in cross),
            "items": [item.to_dict() for item in cross[:100]],
        },
    }


def compute_gate(
    overlap_info: dict,
    base_m2: float,
    threshold_ratio: float = 0.01,
    base_note: str = "规划范围总面积",
) -> dict:
    """重叠告警闸门：同图层只提示不追问，跨图层超阈值必须停表四选一。

    base_m2 为基数（有规划范围用规划范围总面积，无规划范围用分类合计）；
    threshold_ratio 为阈值比例（0.01 = 1%），0 表示关闭告警；
    判定为严格大于阈值（等于阈值不算超）。
    """
    info = overlap_info or {}
    same = info.get("same_layer") or {}
    cross = info.get("cross_layer") or {}
    base = float(base_m2 or 0.0)
    pct = max(float(threshold_ratio or 0.0), 0.0) * 100.0

    same_deducted = float(same.get("deducted_m2") or 0.0)
    cross_area = float(cross.get("area_m2") or 0.0)
    same_share = same_deducted / base * 100.0 if base else 0.0
    cross_share = cross_area / base * 100.0 if base else 0.0
    active = bool(pct and base)

    return {
        "base_m2": round(base, 2),
        "base_note": base_note,
        "threshold_pct": round(pct, 4),
        "same_layer": {
            "count": int(same.get("count") or 0),
            "deducted_m2": round(same_deducted, 2),
            "share_pct": round(same_share, 4),
            "over": active and same_share > pct,
        },
        "cross_layer": {
            "count": int(cross.get("count") or 0),
            "area_m2": round(cross_area, 2),
            "share_pct": round(cross_share, 4),
            "over": active and cross_share > pct,
            "layers": list(cross.get("layers") or [])[:8],
        },
    }
