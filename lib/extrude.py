"""批量三维拉伸：闭合校验 + 拉伸编排。

EXTRUDE 对轮廓闭合极其敏感，因此先做**三态闭合判定**再决定能不能拉：

| 状态 | 判据 | 处理 |
| --- | --- | --- |
| ready | CAD 自报闭合 / 填充边界 / 线段缝合 | 直接拉伸 |
| review | 首尾容差吸附（原图其实有缺口） | 拉伸，但进「需人工闭合」清单 |
| blocked | 未闭合、自交、点数不足、面积为 0 | 不拉伸，进异常清单给句柄 |

两条产出路线，能力不对等，必须在报告里写清楚：
- COM 通道：`AddRegion` → `AddExtrudedSolid`，产出真正的 `3DSOLID`；
- DXF 通道：顶面 + 侧面 `3DFACE`，产出**体块网格**，只作体块示意，不是 ACIS 实体。
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import shapes  # noqa: E402

READY_KINDS = {"closed_flag", "hatch", "polygonize"}
REVIEW_KINDS = {"snapped"}
STATUS_LABELS = {"ready": "可直接拉伸", "review": "需人工复核闭合", "blocked": "不可拉伸"}


def profile_points(shape: shapes.ShapeRec, tolerance: float = 1e-6) -> List[Tuple[float, float]]:
    """取外环并去掉重复点与零长度边；首尾重合点去掉（拉伸时由 CAD 自动闭合）。"""
    ring = shape.outer or []
    cleaned: List[Tuple[float, float]] = []
    for point in ring:
        try:
            current = (float(point[0]), float(point[1]))
        except Exception:
            continue
        if not cleaned:
            cleaned.append(current)
            continue
        last = cleaned[-1]
        if math.hypot(current[0] - last[0], current[1] - last[1]) > tolerance:
            cleaned.append(current)
    while len(cleaned) > 1:
        first, last = cleaned[0], cleaned[-1]
        if math.hypot(first[0] - last[0], first[1] - last[1]) <= tolerance:
            cleaned.pop()
        else:
            break
    return cleaned


def classify(shape: shapes.ShapeRec) -> Dict:
    """单栋轮廓的闭合三态判定，返回 {status, reason, points, has_hole}。"""
    points = profile_points(shape)
    kind = str(getattr(shape, "close_kind", "closed_flag") or "closed_flag")
    has_hole = len(shape.rings) > 1
    if kind not in READY_KINDS and kind not in REVIEW_KINDS:
        return {"status": "blocked", "reason": f"闭合来源为 {kind}，无法确认闭合", "points": points, "has_hole": has_hole}
    if len(points) < 3:
        return {"status": "blocked", "reason": f"有效顶点仅 {len(points)} 个", "points": points, "has_hole": has_hole}
    geometry = getattr(shape, "geometry", None)
    if geometry is None or getattr(geometry, "is_empty", True) or float(getattr(geometry, "area", 0.0)) <= 0:
        return {"status": "blocked", "reason": "几何无效或面积为 0", "points": points, "has_hole": has_hole}
    try:
        if not geometry.is_valid:
            return {"status": "blocked", "reason": "轮廓自交（EXTRUDE 会失败）", "points": points, "has_hole": has_hole}
    except Exception:
        pass
    if kind in REVIEW_KINDS:
        note = "首尾为容差吸附闭合，原图存在缺口，拉伸前建议人工闭合"
        return {"status": "review", "reason": note, "points": points, "has_hole": has_hole}
    return {"status": "ready", "reason": "", "points": points, "has_hole": has_hole}


def supports_solid(backend) -> bool:
    """通道是否可能产出真 ACIS 实体（只有 COM 通道实现了 AddExtrudedSolid）。"""
    return getattr(backend, "channel", "") == "com" and hasattr(backend, "add_extruded_solid")


def extrude_items(
    backend,
    items: Sequence[Dict],
    layer: str,
    mode: str = "solid",
    chunk: int = 100,
) -> Dict:
    """批量拉伸。items 每项：{handle, points, height, status, extrude_layer?}。

    mode=solid 走 `add_extruded_solid`；mode=mesh 走 `add_face`。
    `extrude_layer` 给了就逐栋落该图层（塔楼/裙房分层），否则统一落 layer。
    单栋失败不中断整批，逐条记录失败原因（含句柄）。
    """
    counts = {"solid": 0, "mesh": 0, "failed": 0}
    failed: List[Dict] = []
    total = len(items)
    for index, item in enumerate(items, start=1):
        points = item.get("points") or []
        height = float(item.get("height") or 0.0)
        handle = str(item.get("handle", ""))
        target_layer = str(item.get("extrude_layer") or layer)
        if height <= 0 or len(points) < 3:
            counts["failed"] += 1
            failed.append({"handle": handle, "reason": f"高度 {height:g}m 或顶点数不足，跳过"})
            continue
        try:
            if mode == "solid":
                ok = bool(backend.add_extruded_solid(target_layer, points, height))
            else:
                ok = bool(backend.add_face(target_layer, points, 0.0, height))
        except Exception as exc:  # 单栋失败不能拖垮整批
            counts["failed"] += 1
            failed.append({"handle": handle, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        if ok:
            counts["solid" if mode == "solid" else "mesh"] += 1
        else:
            counts["failed"] += 1
            failed.append({"handle": handle, "reason": "通道返回失败"})
        if chunk and index % max(1, chunk) == 0 and index < total:
            backend.touch_watchdog()
            print(f"[拉伸] {index}/{total}", file=sys.stderr, flush=True)
    return {"counts": counts, "failed": failed, "mode": mode, "layer": layer}


def candidate_rows(
    shapes_list: Sequence[shapes.ShapeRec],
    heights: Dict[str, float],
) -> Tuple[List[Dict], List[Dict], List[Dict]]:
    """把建筑分成 ready / review / blocked 三组，并附拉伸高度。"""
    ready: List[Dict] = []
    review: List[Dict] = []
    blocked: List[Dict] = []
    for shape in shapes_list:
        verdict = classify(shape)
        row = {
            "handle": shape.handle,
            "layer": shape.layer,
            "area_m2": round(float(shape.area_m2 or 0.0), 3),
            "close_kind": getattr(shape, "close_kind", ""),
            "points": verdict["points"],
            "has_hole": verdict["has_hole"],
            "height": float(heights.get(str(shape.handle), 0.0) or 0.0),
            "status": verdict["status"],
            "reason": verdict["reason"],
        }
        if verdict["status"] == "ready":
            ready.append(row)
        elif verdict["status"] == "review":
            review.append(row)
        else:
            blocked.append(row)
    return ready, review, blocked
