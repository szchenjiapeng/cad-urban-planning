"""规划范围识别、环关系分析与面积裁剪。

识别：按图层名关键词（规划范围/范围/range/红线…）找候选，多候选或多环关系有歧义时
     不自动决定，交给上层生成确认项。
分析：环之间关系分三类 —— 相离（并集）、包含（挖空）、相交（需确认）。
裁剪：shapely 可用时做精确求交；不可用时退化为“整块纳入/排除”并标注误差。

所有几何运算在图纸单位下进行，面积由上层乘以单位换算系数的平方。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from core.geometry import (  # noqa: E402
    bbox_center,
    bbox_of,
    bbox_overlap_ratio,
    discretize,
    is_closed,
    point_in_polygon,
    polygon_area,
)
from core.hatch import interior_point  # noqa: E402
from core.models import HatchRecord, PolylineRecord  # noqa: E402

Point = Tuple[float, float]

SCOPE_KEYWORDS: Tuple[str, ...] = (
    # 长词在前：match_layers_by_keyword 首个命中即停，长词让确认项标签更准确、排序更靠前
    "规划范围线",
    "规划范围",
    "用地范围",
    "规划界线",
    "用地界线",
    "总用地",
    "红线",
    "范围线",
    "范围",
    # 拼音缩写（匹配时已统一转小写，不必重复大写形式）
    "ghfw",
    "fwx",
    "fw",
    "range",
    "boundary",
)

# 索取外部范围线文件前必须先完整转述给用户的前置要求（脚本只能粗检，不做坐标纠偏）
SCOPE_FILE_REMINDER: Tuple[str, ...] = (
    "在提供范围线文件前，请先确认以下几点（脚本只能粗检，无法自动纠正）：",
    "1. 与总图同一坐标系、同一原点：不要平移、旋转、缩放，另存时不要改插入基点；",
    "2. 图纸单位一致：总图是 mm 就用 mm，总图是 m 就用 m；",
    "3. 范围线须为闭合多段线，最好单独放在一个图层（如 SCOPE-范围线），不要混入图框、道路红线、轴线等其它线；",
    "4. 格式：DXF 最稳；DWG 需本机有 CAD（脚本只读打开、读完立即关闭、不保存原文件）。",
    "若第 1、2 条无法满足，请先在 CAD 里把范围线对齐到总图原位（INSERT/ALIGN），另存 DXF 后再提供；"
    "实在无法对齐，可改用「无规划范围」口径（--scope none）。",
)
SCOPE_FILE_REMINDER_TEXT = "\n".join(SCOPE_FILE_REMINDER)

# 范围边界弧段离散步长：比填充（2°）细一档，30 m 半径圆弧的弦高约 0.03 m。
# 面积本身优先取 CAD 自算 .Area，这里只决定裁剪边界的精度，不作用于面积口径。
_SCOPE_ARC_STEP_DEG = 0.05

try:  # shapely 为可选依赖
    from shapely.geometry import Point as ShapelyPoint
    from shapely.geometry import Polygon as ShapelyPolygon
    from shapely.ops import unary_union

    HAS_SHAPELY = True
except Exception:  # pragma: no cover - 环境缺 shapely 时走降级
    ShapelyPoint = None  # type: ignore[assignment]
    ShapelyPolygon = None  # type: ignore[assignment]
    unary_union = None  # type: ignore[assignment]
    HAS_SHAPELY = False


@dataclass
class ScopeCandidate:
    handle: str
    layer: str
    entity_type: str
    points: List[Point]
    area_drawing: float
    closed: bool = True
    issue: str = ""
    # CAD 自算面积（图纸单位²，多段线 .Area 与 AREA 命令同源）。有值时一律以它为准：
    # 圆弧无法用弦多边形准确表达，本图曾因此少算 0.08%（1510.96 ha vs 1512.22 ha）。
    cad_area: Optional[float] = None

    @property
    def area_source(self) -> str:
        return "cad" if (self.cad_area or 0) > 0 else "geometry"

    def to_dict(self) -> dict:
        return {
            "handle": self.handle,
            "layer": self.layer,
            "entity_type": self.entity_type,
            "closed": self.closed,
            "area_m2_hint": round(self.area_drawing, 2),
            "area_source": self.area_source,
            "issue": self.issue,
        }


@dataclass
class ScopeRegion:
    polygons: List[List[Point]] = field(default_factory=list)
    geom: object = None
    method: str = "geometric"
    relation: str = "single"
    area_drawing: float = 0.0
    area_source: str = "geometric"  # cad=CAD 自算 .Area / geometric=几何离散
    area_geom_drawing: Optional[float] = None  # 几何离散面积，供「与 CAD 偏差」校验

    @property
    def exact(self) -> bool:
        return self.geom is not None

    def contains(self, point: Point) -> bool:
        if self.geom is not None:
            try:
                return bool(self.geom.contains(ShapelyPoint(point[0], point[1])))
            except Exception:
                return False
        if self.relation == "difference" and self.polygons:
            areas = [polygon_area(p) for p in self.polygons]
            outer_index = areas.index(max(areas))
            for index, polygon in enumerate(self.polygons):
                if index == outer_index:
                    continue
                if point_in_polygon(point, polygon):
                    return False
            return point_in_polygon(point, self.polygons[outer_index])
        return any(point_in_polygon(point, polygon) for polygon in self.polygons)

    def clip(self, points: Sequence[Point]) -> Tuple[float, Optional[List[Point]], str]:
        """返回 (相交面积, 裁剪后外环顶点, 状态)。

        状态：full 全在范围内 / partial 部分相交 / none 完全在范围外 /
             fallback 无 shapely 时的近似 / invalid 边界无效。
        """
        if self.geom is not None:
            polygon = _as_polygon(points)
            if polygon is None:
                return 0.0, None, "invalid"
            try:
                inter = polygon.intersection(self.geom)
            except Exception:
                return 0.0, None, "invalid"
            if inter.is_empty:
                return 0.0, None, "none"
            area = float(inter.area)
            if polygon.area > 0 and area >= polygon.area * 0.9999:
                return area, None, "full"
            return area, _largest_polygon_points(inter), "partial"

        area = polygon_area(points)
        anchor = interior_point(points) or bbox_center(bbox_of(points))
        if self.contains(anchor):
            return area, None, "full"
        sampled = [p for p in points[:: max(1, len(points) // 8)]]
        if any(self.contains(point) for point in sampled):
            return area, None, "fallback"
        return 0.0, None, "none"


def match_layer(layer: str, keywords: Sequence[str] = SCOPE_KEYWORDS) -> bool:
    name = (layer or "").strip().lower()
    if not name:
        return False
    return any(keyword.lower() in name for keyword in keywords if keyword)


def collect_candidates(
    polylines: Sequence[PolylineRecord],
    hatches: Sequence[HatchRecord],
    tolerance: float,
    keywords: Sequence[str] = SCOPE_KEYWORDS,
    layer_filter: Optional[Sequence[str]] = None,
) -> List[ScopeCandidate]:
    """收集规划范围候选。

    - 默认按图层名关键词匹配；
    - 给了 layer_filter（如 --scope-layer）时改为图层名精确匹配（忽略大小写），
      用于用户否认自动判断后直接指定图层，或范围线文件里图层名不含关键词的情况。
    未闭合的候选带 issue，不静默丢弃。
    """
    wanted = {str(name).strip().lower() for name in layer_filter if str(name).strip()} if layer_filter else None

    def hit(layer: str) -> bool:
        if wanted is None:
            return match_layer(layer, keywords)
        return (layer or "").strip().lower() in wanted

    candidates: List[ScopeCandidate] = []

    for record in polylines:
        if not hit(record.layer):
            continue
        if len(record.vertices) < 3:
            candidates.append(
                ScopeCandidate(record.handle, record.layer, record.entity_type, [], 0.0, False, "顶点不足，无法围合")
            )
            continue
        closed, gap = is_closed(record.vertices, tolerance)
        closed = bool(record.closed_flag or closed)
        points = discretize(record.vertices, record.bulges, close=True, max_step_deg=_SCOPE_ARC_STEP_DEG)
        issue = "" if closed else f"未闭合（首尾间距 {gap:.3f}），请先转成闭合二维多段线"
        cad_area = record.cad_area if (record.cad_area or 0) > 0 else None
        candidates.append(
            ScopeCandidate(
                handle=record.handle,
                layer=record.layer,
                entity_type=record.entity_type,
                points=points,
                area_drawing=float(cad_area) if cad_area else polygon_area(points),
                closed=closed,
                issue=issue,
                cad_area=cad_area,
            )
        )

    for hatch in hatches:
        if not hit(hatch.layer):
            continue
        if hatch.rings:
            ring = _outer_ring_of(hatch.rings)
            if ring:
                cad_area = hatch.area_drawing if (hatch.area_drawing or 0) > 0 else None
                candidates.append(
                    ScopeCandidate(
                        handle=hatch.handle,
                        layer=hatch.layer,
                        entity_type="HATCH",
                        points=ring,
                        area_drawing=float(cad_area) if cad_area else polygon_area(ring),
                        closed=True,
                        cad_area=cad_area,
                    )
                )
                continue
        candidates.append(
            ScopeCandidate(hatch.handle, hatch.layer, "HATCH", [], 0.0, False, "填充边界无法解析")
        )

    return candidates


def group_by_layer(candidates: Sequence[ScopeCandidate]) -> Dict[str, List[ScopeCandidate]]:
    grouped: Dict[str, List[ScopeCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.layer, []).append(candidate)
    return grouped


def analyze_relation(
    candidates: Sequence[ScopeCandidate],
    prefer_shapely: bool = True,
) -> Tuple[str, List[str], List[str], str]:
    """返回 (关系, 需确认的问题, 备注, kind)。

    关系：single / union / difference / intersect；
    kind：single / empty / disjoint / contains / adjacent（仅共边）/ cross（有面积重叠）。
    """
    usable = [item for item in candidates if item.points and item.closed]
    if not usable:
        return "single", [], ["规划范围没有可用闭合边界"], "empty"
    if len(usable) == 1:
        return "single", [], [], "single"

    notes: List[str] = []
    if prefer_shapely and HAS_SHAPELY:
        shapes = [_as_polygon(item.points) for item in usable]
        shapes = [shape for shape in shapes if shape is not None]
        has_contains = False
        has_cross = False
        has_adjacent = False
        for i in range(len(shapes)):
            for j in range(i + 1, len(shapes)):
                a, b = shapes[i], shapes[j]
                try:
                    if a.disjoint(b):
                        continue
                    if a.contains(b) or b.contains(a):
                        has_contains = True
                        continue
                    inter = a.intersection(b)
                    overlap = float(getattr(inter, "area", 0.0) or 0.0)
                    if overlap <= max(a.area, b.area) * 1e-9:
                        has_adjacent = True  # 仅共边/首尾相接，没有面积重叠
                    else:
                        has_cross = True
                except Exception:
                    continue
        if has_cross or has_adjacent:
            kind = "cross" if has_cross else "adjacent"
            return (
                "intersect",
                ["同一规划范围图层内存在相交但不包含的边界，需确认取并集还是交集"],
                notes,
                kind,
            )
        if has_contains:
            # A 在 B 内：按挖空处理，统计两环之间（B - A），不再二次询问
            return "difference", [], notes, "contains"
        return "union", [], notes, "disjoint"

    # 降级：无 shapely 时用内点与包围盒近似
    notes.append("未安装 shapely，环关系按包围盒与内点近似判定")
    has_contains = False
    has_possible_intersect = False
    for i in range(len(usable)):
        for j in range(i + 1, len(usable)):
            a, b = usable[i], usable[j]
            box_a, box_b = bbox_of(a.points), bbox_of(b.points)
            if bbox_overlap_ratio(box_a, box_b) <= 0.0:
                continue
            anchor_a = interior_point(a.points) or bbox_center(box_a)
            anchor_b = interior_point(b.points) or bbox_center(box_b)
            if point_in_polygon(anchor_a, b.points) or point_in_polygon(anchor_b, a.points):
                has_contains = True
            else:
                has_possible_intersect = True
    if has_possible_intersect:
        return (
            "intersect",
            ["边界包围盒相交但关系不明确，需确认取并集还是交集（建议安装 shapely 后重跑以获得精确判定）"],
            notes,
            "cross",
        )
    if has_contains:
        return "difference", [], notes, "contains"
    return "union", [], notes, "disjoint"


def build_region(
    candidates: Sequence[ScopeCandidate],
    relation: str,
    prefer_shapely: bool = True,
) -> ScopeRegion:
    usable = [item for item in candidates if item.points and item.closed]
    polygons = [list(item.points) for item in usable]
    region = ScopeRegion(polygons=polygons, relation=relation)

    if not polygons:
        return region

    if prefer_shapely and HAS_SHAPELY:
        shapes = [_as_polygon(points) for points in polygons]
        shapes = [shape for shape in shapes if shape is not None]
        if shapes:
            try:
                if relation == "union":
                    geom = unary_union(shapes) if len(shapes) > 1 else shapes[0]
                elif relation == "difference":
                    outer = max(shapes, key=lambda item: item.area)
                    geom = outer
                    for shape in shapes:
                        if shape is outer:
                            continue
                        geom = geom.difference(shape)
                elif relation == "intersect":
                    geom = shapes[0]
                    for shape in shapes[1:]:
                        geom = geom.intersection(shape)
                else:
                    geom = shapes[0] if len(shapes) == 1 else unary_union(shapes)
                if geom is not None and not geom.is_empty:
                    region.geom = geom
                    region.method = "shapely"
                    region.area_drawing = float(geom.area)
                    return _prefer_cad_area(region, usable)
            except Exception:
                region.geom = None

    # 降级面积：挖空按“最大环减其余”，其余按求和
    if relation == "difference" and len(polygons) > 1:
        areas = sorted((polygon_area(p) for p in polygons), reverse=True)
        region.area_drawing = max(areas[0] - sum(areas[1:]), 0.0)
    else:
        region.area_drawing = sum(polygon_area(p) for p in polygons)
    region.method = "geometric"
    return _prefer_cad_area(region, usable)


def _prefer_cad_area(region: ScopeRegion, usable: Sequence[ScopeCandidate]) -> ScopeRegion:
    """范围面积优先取 CAD 自算 .Area。

    多段线的 .Area 与 AREA 命令同源，含圆弧是精确值；几何离散只能得到弦多边形，
    圆弧被当弦时面积偏小（本图 1510.9635 ha vs CAD 1512.2180 ha，差 0.08%）。
    只有单环能直接采用 CAD 值；多环（并集/挖空/交集）仍用几何面积，偏差交给上层校验。
    几何面积一律保留在 area_geom_drawing 里，用于「与 CAD 偏差」校验行。
    """
    region.area_geom_drawing = region.area_drawing
    if len(usable) == 1:
        cad_area = float(usable[0].cad_area or 0.0)
        if cad_area > 0:
            region.area_drawing = cad_area
            region.area_source = "cad"
    return region


def layer_inventory(
    polylines: Sequence[PolylineRecord],
    hatches: Sequence[HatchRecord],
    tolerance: float,
    keywords: Sequence[str] = SCOPE_KEYWORDS,
) -> List[dict]:
    """按图层汇总可用边界，作为「请指定范围图层」类确认项的选择依据。

    只统计数量与面积（图纸单位²），不判断哪个才是真范围——那要用户确认。
    """
    stats: Dict[str, dict] = {}

    def entry(layer: str) -> dict:
        row = stats.get(layer)
        if row is None:
            row = {
                "layer": layer,
                "polylines": 0,
                "closed_polylines": 0,
                "hatches": 0,
                "area_drawing": 0.0,
                "keyword_match": match_layer(layer, keywords),
            }
            stats[layer] = row
        return row

    for record in polylines:
        row = entry(record.layer)
        row["polylines"] += 1
        if len(record.vertices) >= 3:
            closed, _gap = is_closed(record.vertices, tolerance)
            if record.closed_flag or closed:
                row["closed_polylines"] += 1
                if (record.cad_area or 0) > 0:
                    row["area_drawing"] += float(record.cad_area)
                else:
                    row["area_drawing"] += polygon_area(
                        discretize(
                            record.vertices, record.bulges, close=True, max_step_deg=_SCOPE_ARC_STEP_DEG
                        )
                    )

    for hatch in hatches:
        entry(hatch.layer)["hatches"] += 1

    rows = list(stats.values())
    for row in rows:
        row["area_drawing"] = round(row["area_drawing"], 2)
    rows.sort(key=lambda row: (row["closed_polylines"], row["area_drawing"], row["hatches"]), reverse=True)
    return rows


def _file_and_none_options(extra_args: Optional[Sequence[str]] = None) -> List[dict]:
    """「提供范围线文件 / 无规划范围」两个兜底选项，各类确认项共用。"""
    base = list(extra_args or [])
    return [
        {
            "id": "provide_file",
            "label": "我提供范围线文件（需同坐标系原位）",
            "args": base + ["--scope-file", "<路径>"],
            "requires_input": "scope_file",
            "reminder": list(SCOPE_FILE_REMINDER),
            "description": "脚本只做单位与包围盒粗检，不做坐标纠偏",
        },
        {
            "id": "none",
            "label": "无规划范围，按全部可见图层填充合计",
            "args": base + ["--scope", "none"],
            "description": "不裁剪、不反算道路、不做差额校验",
        },
    ]


def _choice_options(
    inventory: Sequence[dict],
    extra_args: Optional[Sequence[str]] = None,
    limit: int = 12,
) -> List[dict]:
    """由图层清单生成「用某图层 / 提供文件 / 无范围」三类选项。"""
    base = list(extra_args or [])
    options: List[dict] = []
    for row in [item for item in inventory if item.get("closed_polylines")][:limit]:
        detail = f"{row['closed_polylines']} 个闭合多段线"
        if row.get("polylines", 0) > row["closed_polylines"]:
            detail += f"（共 {row['polylines']} 个多段线，其余未闭合）"
        options.append(
            {
                "id": f"layer:{row['layer']}",
                "label": f"用图层「{row['layer']}」作为规划范围（{detail}）",
                "args": base + ["--scope-layer", row["layer"]],
                "description": "其它图层不参与范围裁剪",
            }
        )
    options.extend(_file_and_none_options(base))
    return options


def match_layers_by_keyword(
    layer_names: Sequence[str],
    keywords: Sequence[str] = SCOPE_KEYWORDS,
) -> List[dict]:
    """只看图层名，挑出可能承载规划范围的图层（不读任何图元，秒级）。"""
    matched: List[dict] = []
    seen = set()
    order = {str(kw).strip().lower(): index for index, kw in enumerate(keywords) if str(kw).strip()}
    for name in layer_names:
        text = (name or "").strip()
        low = text.lower()
        if not low or low in seen:
            continue
        for keyword in keywords:
            key = str(keyword).strip().lower()
            if not key or key not in low:
                continue
            seen.add(low)
            matched.append({"layer": text, "keyword": str(keyword)})
            break
    matched.sort(key=lambda row: (order.get(row["keyword"].lower(), 99), row["layer"].lower()))
    return matched


def scope_layer_choice_item(
    matched: Sequence[dict],
    keywords: Sequence[str] = SCOPE_KEYWORDS,
    all_layers: Sequence[str] = (),
    extra_args: Optional[Sequence[str]] = None,
) -> dict:
    """只按图层名识别出的范围候选：选项直接带 --scope-layer，选定后再读几何。"""
    base = list(extra_args or [])

    def describe(row: dict) -> str:
        count = row.get("polylines")
        if isinstance(count, int) and count >= 0:
            return f"命中「{row.get('keyword', '')}」，{count} 个多段线"
        return f"命中「{row.get('keyword', '')}」"

    options = [
        {
            "id": f"layer:{row['layer']}",
            "label": f"用图层「{row['layer']}」作为规划范围（{describe(row)}）",
            "args": base + ["--scope-layer", row["layer"], "--list-scope"],
            "description": "只读该图层，先确认闭合情况与面积，再出平衡表",
        }
        for row in matched
    ]
    options.extend(_file_and_none_options(base))
    layer_text = "、".join(f"{row['layer']}（{describe(row)}）" for row in matched)
    return {
        "type": "scope_layer_choice",
        "question": (
            f"图层名命中规划范围关键词的有：{layer_text}。"
            "请让用户告知哪一个是规划范围线所在图层，确认前不要读图元、不要出平衡表。"
        ),
        "options": options,
        "reminder": list(SCOPE_FILE_REMINDER),
        "detail": {
            "keywords": list(keywords),
            "matched_layers": list(matched),
            "layer_count": len(all_layers),
            "all_layers": [str(name) for name in all_layers][:200],
            "reminder_text": SCOPE_FILE_REMINDER_TEXT,
        },
    }


def scope_missing_item(
    inventory: Sequence[dict],
    keywords: Sequence[str] = SCOPE_KEYWORDS,
    extra_args: Optional[Sequence[str]] = None,
    reason: str = "未识别到规划范围图层",
) -> dict:
    """识别不到范围图层时的确认项：列出图层清单，并给出文件 / 无范围两条路。"""
    return {
        "type": "scope_not_found",
        "question": (
            f"{reason}（已按关键词 {'、'.join(keywords)} 检索）。"
            "请提供规划范围线所在的图层名；若图纸确实没有规划范围，可选择「无规划范围」按全部可见图层填充合计，"
            "或提供同坐标系原位的范围线文件。"
        ),
        "options": _choice_options(inventory, extra_args),
        "reminder": list(SCOPE_FILE_REMINDER),
        "detail": {
            "keywords": list(keywords),
            "layers": list(inventory)[:100],
            "reminder_text": SCOPE_FILE_REMINDER_TEXT,
        },
    }


def scope_layer_invalid_item(
    layer: str,
    inventory: Sequence[dict],
    reason: str,
    extra_args: Optional[Sequence[str]] = None,
) -> dict:
    """用户指定的图层不存在或没有闭合边界时的确认项。"""
    return {
        "type": "scope_layer_invalid",
        "question": (
            f"指定的规划范围图层「{layer}」{reason}。请改用其它图层，或提供范围线文件；"
            "若该图确实没有规划范围，可按「无规划范围」口径统计。"
        ),
        "options": _choice_options(inventory, extra_args),
        "reminder": list(SCOPE_FILE_REMINDER),
        "detail": {"layer": layer, "reason": reason, "layers": list(inventory)[:100]},
    }


def closed_polyline_candidates(candidates: Sequence[ScopeCandidate]) -> List[ScopeCandidate]:
    """只取闭合的多段线：判断「多条规划范围线」时不能把填充边界算进来。"""
    return [item for item in candidates if item.entity_type != "HATCH" and item.points and item.closed]


SCOPE_DRAFT_QUESTION = (
    "图层「{layer}」里有 {count} 条闭合多段线，而规划范围一般只有 1 条。"
    "多出来的常见是没删干净的草稿或旧版范围线。请确认：这些边界里有草稿吗？"
    "有草稿就只保留面积最大的一条；都不是草稿，再按它们的空间关系合成一个规划范围。"
)


def scope_draft_check_item(
    layer: str,
    candidates: Sequence[ScopeCandidate],
    relation: str = "union",
    extra_args: Optional[Sequence[str]] = None,
    kind: str = "",
) -> dict:
    """多条闭合范围线时的「先问草稿」确认项。

    relation 只用于把「不是草稿」那个选项的后续参数写好，不代表已经定案；
    kind=adjacent/cross 时额外提示这是绘图问题（共边 / 压盖）。
    """
    base = list(extra_args or [])
    ranked = sorted(closed_polyline_candidates(candidates), key=lambda item: -item.area_drawing)
    biggest = ranked[0] if ranked else None

    options: List[dict] = [
        {
            "id": "draft_outer_only",
            "label": "有草稿：只保留面积最大的那条边界",
            "args": base + ["--scope-outer-only"],
            "description": (
                f"最大边界句柄 {biggest.handle}，面积约 {biggest.area_drawing:.2f} 图纸单位²"
                if biggest
                else "按面积最大的一条作为规划范围"
            ),
        },
        {
            "id": "draft_manual",
            "label": "有草稿：要保留的不是最大那条（我给句柄 / 先在 CAD 删掉再跑）",
            "args": base,
            "requires_input": "scope_boundary_handles",
            "description": "可在 CAD 中删除草稿线后重跑，或用 --scope handles 指定边界",
        },
    ]

    if relation == "difference":
        options.append(
            {
                "id": "real_multi_difference",
                "label": "没有草稿：外环减内环（挖空，统计两环之间）",
                "args": base + ["--scope-relation", "difference"],
                "description": "被包住的那条视为内部镂空，不计入统计",
            }
        )
    elif relation == "intersect":
        options.append(
            {
                "id": "real_multi_union",
                "label": "没有草稿：取并集",
                "args": base + ["--scope-relation", "union"],
            }
        )
        options.append(
            {
                "id": "real_multi_intersect",
                "label": "没有草稿：取交集",
                "args": base + ["--scope-relation", "intersect"],
            }
        )
    else:
        options.append(
            {
                "id": "real_multi_union",
                "label": "没有草稿：多条边界合并为一个规划范围（并集）",
                "args": base + ["--scope-relation", "union"],
                "description": "适用于一个片区被拆成几块互不相连的范围",
            }
        )
    options.extend(_file_and_none_options(base))

    hint = ""
    if kind == "adjacent":
        hint = "另外检测到这些边界首尾相接/共边（没有面积重叠），多为绘制时没合并成一条闭合线。"
    elif kind == "cross":
        hint = "另外检测到这些边界互相压盖（有面积重叠），多为草稿残留或重复描图。"

    return {
        "type": "scope_draft_check",
        "question": SCOPE_DRAFT_QUESTION.format(layer=layer, count=len(ranked)) + hint,
        "options": options,
        "reminder": list(SCOPE_FILE_REMINDER),
        "detail": {
            "layer": layer,
            "closed_polylines": len(ranked),
            "relation_guess": relation,
            "boundaries": [
                {
                    "handle": item.handle,
                    "area_drawing": round(item.area_drawing, 2),
                    "issue": item.issue,
                }
                for item in ranked
            ],
            "note": "规划范围一般只有 1 条；多环时必须先排除草稿，再决定并集 / 挖空 / 交集",
        },
    }


def confirmation_items(
    candidates: Sequence[ScopeCandidate],
    layer_groups: Dict[str, List[ScopeCandidate]],
    relation: str,
    extra_args: Optional[Sequence[str]] = None,
    inventory: Optional[Sequence[dict]] = None,
    kind: str = "",
) -> List[dict]:
    """生成规划范围相关的结构化确认项。"""
    items: List[dict] = []

    if len(layer_groups) > 1:
        base = list(extra_args or [])
        options = [
            {
                "id": f"layer:{layer}",
                "label": f"用图层「{layer}」作为规划范围（{len(group)} 个闭合边界）",
                "args": base + ["--scope-layer", layer],
                "description": "其余同名候选图层忽略",
            }
            for layer, group in sorted(layer_groups.items(), key=lambda kv: -len(kv[1]))
        ]
        options.append(
            {
                "id": "other",
                "label": "都不是，我另外提供图层名",
                "args": base,
                "requires_input": "scope_layer",
            }
        )
        options.append(
            {
                "id": "provide_file",
                "label": "我提供范围线文件（需同坐标系原位）",
                "args": base + ["--scope-file", "<路径>"],
                "requires_input": "scope_file",
                "reminder": list(SCOPE_FILE_REMINDER),
            }
        )
        items.append(
            {
                "type": "scope_layer",
                "question": (
                    "命中多个可能的规划范围图层，以哪个为准？"
                    "若都不是真实范围线，可直接给图层名或提供范围线文件。"
                ),
                "options": options,
                "reminder": list(SCOPE_FILE_REMINDER),
                "detail": {
                    "layers": list(inventory)[:100] if inventory else [],
                    "candidate_layers": sorted(layer_groups),
                    "reminder_text": SCOPE_FILE_REMINDER_TEXT,
                },
            }
        )

    if relation == "intersect":
        # 相邻（仅共边）与交叉（真有面积重叠）是两种绘图问题，分开给修图指引
        if kind == "adjacent":
            question = (
                "同一范围图层内的边界首尾相接/共边（没有面积重叠），多为绘制时没合并成一条闭合线。"
                "按问题流程处理：合并成一条范围、只取最大的一条，还是回 CAD 修好再跑？"
            )
        else:
            question = (
                "同一范围图层内的边界互相压盖（有面积重叠），多为草稿残留或重复描图。"
                "按问题流程处理：取并集、取交集，还是回 CAD 修好再跑？"
            )
        items.append(
            {
                "type": "scope_relation",
                "question": question,
                "options": [
                    {"label": "取并集（合并为一个范围）", "args": ["--scope-relation", "union"]},
                    {"label": "取交集（只统计重叠部分）", "args": ["--scope-relation", "intersect"]},
                    {"label": "只取面积最大的一条", "args": ["--scope-outer-only"]},
                    {
                        "label": "我回 CAD 修好（合并/删除多余线）后重跑",
                        "args": [],
                        "requires_input": "none",
                    },
                ],
                "detail": {"relation_kind": kind or "cross"},
            }
        )
    # 包含关系（A 在 B 内）默认直接挖空、统计两环之间，不再二次询问：
    # 是否为草稿已在上一步「多条范围线先问草稿」里确认过。
    return items


# ---------------------------------------------------------------- 内部工具
def _as_polygon(points: Sequence[Point]):
    if not HAS_SHAPELY or len(points) < 3:
        return None
    try:
        polygon = ShapelyPolygon(points)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if polygon.is_empty:
            return None
        return polygon
    except Exception:
        return None


def _largest_polygon_points(geometry) -> Optional[List[Point]]:
    if geometry is None or geometry.is_empty:
        return None
    geom_type = getattr(geometry, "geom_type", "")
    if geom_type == "Polygon":
        polygon = geometry
    elif geom_type == "MultiPolygon":
        try:
            polygon = max(geometry.geoms, key=lambda item: item.area)
        except Exception:
            return None
    else:
        return None
    try:
        return [(float(x), float(y)) for x, y in polygon.exterior.coords]
    except Exception:
        return None


def _outer_ring_of(rings: Sequence[Sequence[Point]]) -> Optional[List[Point]]:
    best: Optional[List[Point]] = None
    best_area = 0.0
    for ring in rings:
        area = polygon_area(ring)
        if area > best_area:
            best_area = area
            best = list(ring)
    return best
