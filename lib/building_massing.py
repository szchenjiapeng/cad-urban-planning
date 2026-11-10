"""建筑量测算：建筑归属、层高策略、容积率与建筑密度、单图层建筑候选识别。

口径（已与用户确认）：
- **建筑量 = 基底面积 × 层数**，只算主体建筑的简单计容建筑面积，不扣阳台、架空层、
  设备层、地下车库，也不做规范折算；成果必须按「简单计容」表述，不得说成报建口径。
- **容积率 FAR = 建筑量 / 用地面积**；**建筑密度 = 基底面积 / 用地面积**。
- **层数缺失默认 1 层**（`砼` 单独也按 1 层），但必须单列「默认 1 层」清单，
  提示容积率可能被低估。
- **层高**：住宅 3.0m / 已识别非住宅 4.5m / 用地代码未识别 3.0m；用户 `--floor-height`
  可直接覆盖，`--floor-height-policy auto` 表示接受上述默认。
- **跨界建筑**：默认按代表点整栋归属（与退线子技能同一判据），但必须进异常清单告知；
  可选按相交面积分摊或整栋排除。
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import building_class, cli, landuse, shapes  # noqa: E402
from lib import layer_spec as spec_mod  # noqa: E402

CROSS_POLICIES = ("point", "clip", "exclude")
# 地块筛选：auto=按用户确认口径（有性质只留建设用地 / 无性质只留有建筑的地块）
PARCEL_FILTER_MODES = ("auto", "all", "construction", "built")
_SKIP_GROUPS = ("building", "frame", "label", "control")
CROSS_RATIO = 0.95


@dataclass
class BuildingItem:
    """一栋建筑参与测算的全部信息。"""

    shape: shapes.ShapeRec
    floors: int = building_class.DEFAULT_FLOORS
    floor_source: str = "无层数信息，按 1 层"
    floor_kind: str = "none"
    height_text: Optional[float] = None
    conflicts: List[str] = field(default_factory=list)
    parcel_handle: str = ""
    parcel_label: str = ""
    share: float = 1.0
    cross: bool = False
    excluded: bool = False
    footprint_m2: float = 0.0
    floor_area_m2: float = 0.0
    height_m: float = 0.0


@dataclass
class ParcelMetric:
    shape: shapes.ShapeRec
    code: str = ""  # 首段主代码，层高与退线按它走
    code_display: str = ""  # 成果显示用：混合用地为合成代码 100104+0909
    name: str = ""
    label: str = ""
    hybrid_parts: List[str] = field(default_factory=list)
    hybrid_unresolved: List[str] = field(default_factory=list)
    area_m2: float = 0.0
    footprint_m2: float = 0.0
    floor_area_m2: float = 0.0
    building_count: int = 0
    floor_height_m: float = 3.0
    floor_height_source: str = ""
    floors_weighted: float = 0.0
    far: float = 0.0
    coverage: float = 0.0
    buildings: List[BuildingItem] = field(default_factory=list)
    cross_handles: List[str] = field(default_factory=list)
    default_floor_handles: List[str] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)


def resolve_layers(visible: Sequence[str], args, spec: Dict, classifier) -> Dict[str, List[str]]:
    """按 显式参数 > layer_spec 分组 > 用地代码图层 决定各角色图层（不自动采纳猜测量）。"""

    def explicit(text: Optional[str]) -> List[str]:
        wanted = cli.parse_list(text)
        return [name for name in wanted if name in visible]

    def by_group(group: str) -> List[str]:
        """按分组取图层；建筑组必须排除标注/图框/控制线组，否则「BZ-建筑层数」会被当建筑。"""
        skip = ("label", "frame", "control")
        return [
            name
            for name in visible
            if spec_mod.layer_in_group(name, spec, group)
            and not any(spec_mod.layer_in_group(name, spec, other) for other in skip)
        ]

    def code_layers() -> Tuple[List[str], List[str]]:
        """返回（可当地块的用地层, 像用地但代码未登记/无法归类的待确认层）。

        未登记代码（如 `202512 塔楼`、`0422 总平面`、用户自定义 `1709 某某用地`）
        一律不当地块，否则建筑会被算成用地面积；但也不能静默丢弃——
        `pending` 交给上层出确认项，由用户判断是写错图层名还是真用地未登记。
        「只有用地前缀、认不出代码」的低置信层仍进地块候选，后面由
        `filter_parcels(auto)` 只留盖了房子的那一批。
        """
        found: List[str] = []
        pending: List[str] = []
        for name in visible:
            if any(spec_mod.layer_in_group(name, spec, group) for group in _SKIP_GROUPS):
                continue
            try:
                info = landuse.inspect_layer(name, classifier, spec)
            except Exception:
                continue
            if not info.is_parcel:
                continue
            if info.code:
                found.append(name)
            elif info.unregistered:
                pending.append(name)
            elif info.source == "prefix":
                found.append(name)
        return found, pending

    height_candidates = [
        layer for layer, _score in building_class.height_layer_candidates(visible, spec)
    ]
    building_layers = explicit(getattr(args, "building_layers", None)) or by_group("building")
    explicit_parcel = explicit(getattr(args, "parcel_layers", None))
    # 单图层/未分层时不做用地猜测：拿整层当用地会把建筑一起算成地块面积，
    # 必须由用户显式 --parcel-layers 指定，或走「只出全图建筑量」的降级口径。
    # 用户显式给了 --parcel-layers 就视为用地图层已定案：不再把没被选中的图层
    # 当成「待确认的用地」，否则 1-202512塔楼 的 202512 会反复打断测算
    auto_parcel, auto_pending = code_layers() if building_layers and not explicit_parcel else ([], [])
    parcel_layers = explicit_parcel or auto_parcel
    return {
        "building": building_layers,
        "parcel": parcel_layers,
        "parcel_unresolved": auto_pending,
        "height": explicit(getattr(args, "height_layers", None)) or height_candidates,
    }


# 名字相似不等于正确：影像底图、空图层、只有线没有面的图层都会被名字骗到
_NEAR_BUILDING_WORDS = (
    "塔楼",
    "裙房",
    "主楼",
    "厂房",
    "写字楼",
    "办公楼",
    "商业楼",
    "住宅楼",
    "楼",
    "栋",
)
_NEAR_SCOPE_WORDS = ("范围", "界线", "边界", "管控线")
_PROFILE_TYPES = "LWPOLYLINE,POLYLINE,HATCH,INSERT,IMAGE,TEXT,MTEXT"

# 成果分层：塔楼/裙房点名（图上层名优先于高度阈值）
TOWER_WORDS = ("塔楼", "主楼", "办公塔楼", "超高层")
PODIUM_WORDS = ("裙房", "裙楼", "商业裙房")


def detect_layer_layering(layers: Sequence[str]) -> Dict:
    """判断建筑图层是否已按塔楼/裙房区分，返回 {mode, tower, podium, flat, reasons}。

    mode=split 表示塔楼层与裙房层同时存在，可「按图层」分体块；flat 表示图上没区分，
    只能按高度阈值分。只命中一半（只有塔楼或只有裙房）仍判 flat，另一半交高度判定。
    """
    tower: List[str] = []
    podium: List[str] = []
    flat: List[str] = []
    for name in layers or ():
        text = str(name)
        is_tower = any(word in text for word in TOWER_WORDS)
        is_podium = any(word in text for word in PODIUM_WORDS)
        if is_tower and not is_podium:
            tower.append(text)
        elif is_podium and not is_tower:
            podium.append(text)
        else:
            flat.append(text)
    split = bool(tower) and bool(podium)
    if split:
        reasons = [f"图上已分塔楼 {len(tower)} 层 / 裙房 {len(podium)} 层"]
    elif tower or podium:
        reasons = ["只命中了塔楼或只命中了裙房，另一半只能靠高度判定"]
    else:
        reasons = ["建筑图层名不含塔楼/裙房，图上未区分"]
    return {
        "mode": "split" if split else "flat",
        "tower": tower,
        "podium": podium,
        "flat": flat,
        "reasons": reasons,
    }


def assign_extrude_layer(
    row: Dict,
    base: str,
    tower_layer: str,
    podium_layer: str,
    tower_height: float,
) -> Tuple[str, str]:
    """判定单栋体块落哪个成果图层，返回 (图层名, 判定依据)。

    优先级：源图层点名（塔楼/裙房）> 拉伸高度阈值 > 裙房兜底。base 仅用于未分层时的兜底，
    这里保留参数以便将来「按地块/按图层前缀」扩展，不改变现有口径。
    """
    source = str(row.get("layer") or "")
    if any(word in source for word in TOWER_WORDS):
        return tower_layer, f"源图层「{source}」点名塔楼"
    if any(word in source for word in PODIUM_WORDS):
        return podium_layer, f"源图层「{source}」点名裙房"
    height = float(row.get("height") or 0.0)
    if height > float(tower_height):
        return tower_layer, f"高度 {height:g}m > {tower_height:g}m，按高度判为塔楼"
    return podium_layer, f"高度 {height:g}m ≤ {tower_height:g}m，按高度判为裙房"


def _profile_row(backend, name: str, unit_factor: float, role: str) -> Dict:
    """单个图层的「能不能用」证据：闭合面数、面积、影像、空图层。"""
    row = {
        "layer": name,
        "role": role,
        "entities": None,
        "closed": 0,
        "area_m2": 0.0,
        "has_image": False,
        "notes": [],
    }
    try:
        counts = backend.count_entities_by_layer([name], _PROFILE_TYPES).get(name) or {}
        row["entities"] = counts.get("entities")
    except Exception:
        pass
    try:
        image_counts = backend.count_entities_by_layer([name], "IMAGE").get(name) or {}
        row["has_image"] = bool(image_counts.get("polylines"))
    except Exception:
        pass
    try:
        records = shapes.collect_polygons(
            backend, [name], unit_factor=unit_factor, source="both", min_area_m2=0.0
        )
        row["closed"] = len(records)
        row["area_m2"] = round(sum(float(record.area_m2 or 0.0) for record in records), 2)
    except Exception as exc:
        row["notes"].append(f"读取失败：{type(exc).__name__}")
    if row["entities"] == 0:
        row["notes"].append("空图层（没有图元），选了会得到 0 个对象")
    if row["has_image"] and row["closed"] == 0:
        row["notes"].append("影像底图（IMAGE），不是矢量轮廓，不能用")
    if row["entities"] and row["closed"] == 0 and not row["has_image"]:
        row["notes"].append("没有闭合面，无法量算/拉伸")
    if role == "near_building":
        row["notes"].append("图层名像建筑，但不在建筑组，需人工判断")
    if role == "near_scope":
        row["notes"].append("图层名像范围线，需人工判断是否本图规划范围")
    return row


def layer_candidates(
    backend,
    visible: Sequence[str],
    spec: Dict,
    classifier,
    unit_factor: float,
) -> Dict:
    """列出建筑/用地/范围候选与「能不能用」的证据，并判断是否存在需要用户拍板的歧义。"""

    # 注意：这里只排除标注/图框/控制线，不能把 building 自己也排除，
    # 否则「建筑组命中」永远为空（_SKIP_GROUPS 是给「用地筛选」用的）。
    non_building_groups = ("frame", "label", "control")

    def skip_non_building(name: str) -> bool:
        return any(spec_mod.layer_in_group(name, spec, group) for group in non_building_groups)

    def skip_for_parcel(name: str) -> bool:
        return any(spec_mod.layer_in_group(name, spec, group) for group in _SKIP_GROUPS)

    building_group = [
        name
        for name in visible
        if spec_mod.layer_in_group(name, spec, "building") and not skip_non_building(name)
    ]
    near_building = [
        name
        for name in visible
        if name not in building_group
        and not skip_non_building(name)
        and any(word in name for word in _NEAR_BUILDING_WORDS)
        and not any(bad in name for bad in ("图例", "图框", "标注", "注记", "道路", "绿化", "水系"))
    ]
    scope_group = default_scope_layers(visible)
    near_scope = [
        name
        for name in visible
        if name not in scope_group
        and any(word in name for word in _NEAR_SCOPE_WORDS)
        and not any(bad in name for bad in ("图例", "图框", "标注", "注记", "图片", "影像"))
    ]

    building_rows = [_profile_row(backend, name, unit_factor, "building") for name in building_group]
    building_rows += [
        _profile_row(backend, name, unit_factor, "near_building") for name in near_building
    ]
    scope_rows = [_profile_row(backend, name, unit_factor, "scope") for name in scope_group]
    scope_rows += [_profile_row(backend, name, unit_factor, "near_scope") for name in near_scope]

    parcel_rows: List[Dict] = []
    for name in visible:
        if skip_for_parcel(name):
            continue
        try:
            info = landuse.inspect_layer(name, classifier, spec)
        except Exception:
            continue
        if info.is_parcel:
            parcel_rows.append(
                {
                    "layer": name,
                    "code": info.code,
                    "name": info.name,
                    "registered": bool(info.code),
                    "reason": info.reason,
                    "unregistered": list(getattr(info, "unregistered", []) or []),
                }
            )

    usable_building = [
        row for row in building_rows if row["closed"] > 0 and not row["has_image"]
    ]
    reasons: List[str] = []
    if len(building_group) == 0:
        reasons.append("没有图层命中建筑组")
    elif len(building_group) > 1:
        reasons.append(f"建筑组命中 {len(building_group)} 个图层")
    if near_building:
        reasons.append(f"还有 {len(near_building)} 个「像建筑」的图层不在建筑组")
    if len(usable_building) > 1:
        reasons.append(f"有 {len(usable_building)} 个图层都含闭合轮廓（可能是塔楼/裙房/主楼分开表达）")
    if any(row["entities"] == 0 for row in building_rows):
        reasons.append("有建筑候选是空图层")
    if len(scope_group) > 1:
        reasons.append(f"范围组命中 {len(scope_group)} 个图层")
    if near_scope:
        reasons.append(f"还有 {len(near_scope)} 个「像范围」的图层")
    if any(row["has_image"] for row in scope_rows):
        reasons.append("范围候选里有影像底图")
    unregistered = [row for row in parcel_rows if not row["registered"]]
    if unregistered:
        reasons.append(
            f"有 {len(unregistered)} 个图层像用地但代码未登记（未计入面积，需确认是否遗漏）"
        )

    return {
        "building": building_rows,
        "parcel": parcel_rows,
        "scope": scope_rows,
        "recommended": {
            "building": [row["layer"] for row in usable_building],
            "parcel": [row["layer"] for row in parcel_rows if row["registered"]],
            "scope": [
                row["layer"] for row in scope_rows if row["role"] == "scope" and row["closed"] > 0
            ],
        },
        "ambiguous": bool(reasons),
        "reasons": reasons,
        "note": (
            "名字相似不代表正确：影像底图（IMAGE）不是矢量建筑，空图层不要选；"
            "塔楼/裙房可能分层表达，需全部选上。"
        ),
    }


def suspicious_parcel_layers(
    visible: Sequence[str], spec: Dict, classifier, names: Optional[Sequence[str]] = None
) -> List[Dict]:
    """像用地、但因代码未登记或缺失被排除的图层详情。

    这类图层不得静默丢弃（例如 `1-202512塔楼` 里的 202512 被当成代码），
    必须进确认项让用户判断是「图层名写错」还是「真用地但未登记」。
    `names` 给定时只回传这些图层的详情（用于确认项 detail）。
    """
    wanted = set(names) if names is not None else None
    found: List[Dict] = []
    for name in visible:
        if wanted is not None and name not in wanted:
            continue
        if any(spec_mod.layer_in_group(name, spec, group) for group in _SKIP_GROUPS):
            continue
        try:
            info = landuse.inspect_layer(name, classifier, spec)
        except Exception:
            continue
        if info.is_parcel and not info.code:
            found.append(
                {
                    "layer": name,
                    "reason": info.reason,
                    "unregistered": list(getattr(info, "unregistered", []) or []),
                }
            )
    return found


def default_scope_layers(visible: Sequence[str]) -> List[str]:
    """默认规划范围图层：含「规划范围/设计范围」，排除图片、影像与图框图层。"""
    keywords = ("规划范围", "规划用地范围", "设计范围")
    bad = ("图片", "影像", "图框", "图例")
    return [
        name
        for name in visible
        if any(key in name for key in keywords) and not any(word in name for word in bad)
    ]


def scope_geometry(backend, layers: Sequence[str], unit_factor: float):
    """取规划范围几何（图纸单位坐标），多面取并集；取不到返回 None。"""
    if not layers:
        return None
    records = shapes.collect_polygons(
        backend, layers, unit_factor=unit_factor, source="auto", min_area_m2=1.0
    )
    geoms = [
        record.geometry
        for record in records
        if record.geometry is not None and not getattr(record.geometry, "is_empty", True)
    ]
    if not geoms:
        return None
    try:
        from shapely.ops import unary_union

        merged = unary_union(geoms)
        return None if getattr(merged, "is_empty", True) else merged
    except Exception:
        return None


def clip_to_scope(parcels, buildings, scope, unit_factor: float) -> Tuple[List, List, int, int]:
    """按规划范围裁剪：地块重算相交面积，建筑按代表点过滤。返回 (地块, 建筑, 丢地块数, 丢建筑数)。"""
    if scope is None:
        return list(parcels), list(buildings), 0, 0
    kept_parcels = []
    dropped_parcels = 0
    for parcel in parcels:
        geometry = getattr(parcel, "geometry", None)
        if geometry is None or getattr(geometry, "is_empty", True):
            dropped_parcels += 1
            continue
        try:
            inter = geometry.intersection(scope)
        except Exception:
            dropped_parcels += 1
            continue
        if inter is None or getattr(inter, "is_empty", True) or float(inter.area) <= 0:
            dropped_parcels += 1
            continue
        if float(inter.area) < float(geometry.area) * 0.999:
            parcel.geometry = inter
            parcel.area_m2 = float(inter.area) * unit_factor * unit_factor
        kept_parcels.append(parcel)

    kept_buildings = []
    dropped_buildings = 0
    buffered = scope
    try:
        buffered = scope.buffer(0.01)
    except Exception:
        pass
    for building in buildings:
        geometry = getattr(building, "geometry", None)
        if geometry is None or getattr(geometry, "is_empty", True):
            dropped_buildings += 1
            continue
        try:
            point = geometry.representative_point()
        except Exception:
            dropped_buildings += 1
            continue
        try:
            inside = bool(buffered.contains(point))
        except Exception:
            inside = False
        if inside:
            kept_buildings.append(building)
        else:
            dropped_buildings += 1
    return kept_parcels, kept_buildings, dropped_parcels, dropped_buildings


# ---------------------------------------------------------------- 单图层建筑候选
def detect_candidates(
    backend,
    layers: Sequence[str],
    unit_factor: float,
    rules: Dict,
    texts: Sequence = (),
    parcels: Sequence[shapes.ShapeRec] = (),
    limit: int = 50,
) -> Dict:
    """非标准图（所有要素同一图层）的建筑轮廓候选打分。

    只出候选与得分，**必须由用户确认后才采用**；不静默把闭合多段线都当建筑。
    判据顺序：文字证据（砼/F/层/H=）→ 父面抑制 → 面积/长宽比 → 是否与地块面重合。
    """
    config = (rules or {}).get("single_layer") or {}
    min_area = float(config.get("min_area_m2", 10.0))
    max_area = float(config.get("max_area_m2", 20000.0))
    max_aspect = float(config.get("max_aspect", 20.0))
    min_children = int(config.get("parent_min_children", 2))
    parent_area_hint = float(config.get("parent_area_hint_m2", 3000.0))
    weights = config.get("weights") or {}
    text_floor_weight = float(weights.get("text_floor", 3))
    text_any_weight = float(weights.get("text_any", 1))
    shape_weight = float(weights.get("shape", 1))
    inside_weight = float(weights.get("inside_parcel", 1))
    boundary_weight = float(weights.get("on_parcel_boundary", -3))

    if not layers:
        return {
            "candidates": [],
            "total_closed": 0,
            "kept": 0,
            "skipped": [],
            "notes": ["没有可用于建筑候选识别的图层（可能全部被排除或全部关闭）"],
            "adopted": [],
            "records": {},
        }
    # 单图层里「填充 + 多段线」重复描同一栋很常见：both 全收后按几何去重，只算一次
    records, skipped = shapes.collect_polygons_detail(
        backend,
        layers,
        unit_factor=unit_factor,
        source="both",
        min_area_m2=min_area,
        dedupe=True,
    )
    notes: List[str] = []
    candidates: List[Dict] = []
    for record in records:
        if record.area_m2 > max_area:
            skipped.append(
                {
                    "handle": record.handle,
                    "layer": record.layer,
                    "source": record.source_kind,
                    "reason": f"面积 {record.area_m2:.1f} ㎡ 超过建筑上限 {max_area:g} ㎡（判为地块/范围/图框）",
                    "area_m2": round(record.area_m2, 3),
                }
            )
            continue
        box = record.bounds()
        width = max(box[2] - box[0], 1e-9)
        height = max(box[3] - box[1], 1e-9)
        aspect = max(width / height, height / width)
        if aspect > max_aspect:
            skipped.append(
                {
                    "handle": record.handle,
                    "layer": record.layer,
                    "source": record.source_kind,
                    "reason": f"长宽比 {aspect:.1f} 超过 {max_aspect:g}（判为线状/带状要素）",
                    "area_m2": round(record.area_m2, 3),
                }
            )
            continue
        geometry = record.geometry
        if geometry is not None and not geometry.is_valid:
            skipped.append(
                {
                    "handle": record.handle,
                    "layer": record.layer,
                    "source": record.source_kind,
                    "reason": "轮廓自交或几何无效",
                    "area_m2": round(record.area_m2, 3),
                }
            )
            continue
        candidates.append(
            {
                "shape": record,
                "score": 0.0,
                "reasons": [],
                "text_hits": 0,
            }
        )

    if len(records) > 4000:
        notes.append(
            f"同图层闭合面 {len(records)} 个，父面抑制按包围盒粗判（未逐点判定），结果需人工复核。"
        )

    _suppress_parents(candidates, min_children, parent_area_hint)
    candidates = [item for item in candidates if not item.get("parent")]

    # 文字预筛：只留短文本（地形图上的层数标注通常 ≤ 6 字），避免拿上万条说明文字做全量点面判定
    floor_texts: List[Tuple[object, Dict]] = []
    other_texts: List[object] = []
    for text in texts or []:
        raw = str(getattr(text, "text", "") or "").strip()
        if not raw or len(raw) > 12:
            continue
        if not getattr(text, "point", None):
            continue
        info = building_class.parse_floor_info(raw)
        if info["floors"] is not None:
            floor_texts.append((text, info))
        else:
            other_texts.append(text)
    scan_other = len(candidates) <= 1500

    for item in candidates:
        shape = item["shape"]
        score = 0.0
        reasons: List[str] = []
        if shape.rings and len(shape.rings[0]) >= 3:
            vertex_count = len(shape.rings[0])
            if 4 <= vertex_count <= 20:
                score += shape_weight
                reasons.append(f"顶点数 {vertex_count} 符合建筑轮廓")
        for text, info in floor_texts:
            if not _point_in_shape(text.point, shape):
                continue
            score += text_floor_weight
            item["text_hits"] += 1
            reasons.append(f"内含层数文字「{getattr(text, 'text', '')}」（{info['floors']} 层）")
            shape.warnings.append(f"层数文字：{getattr(text, 'text', '')}")
        if scan_other:
            for text in other_texts:
                if _point_in_shape(text.point, shape):
                    score += text_any_weight
                    item["text_hits"] += 1
                    break
        for parcel in parcels or ():
            if parcel.geometry is None:
                continue
            ratio = _area_ratio(shape.geometry, parcel.geometry)
            if ratio >= 0.98:
                score += boundary_weight
                reasons.append(f"与用地地块 {parcel.handle} 边界基本重合（判为地块线）")
                break
            if ratio > 0.0 and _point_in_shape(shape.centroid(), parcel):
                score += inside_weight
                reasons.append("落在用地地块内")
                break
        item["score"] = score
        item["reasons"] = reasons

    candidates.sort(key=lambda item: (-item["score"], -item["shape"].area_m2, item["shape"].handle))
    rows = []
    for item in candidates[:limit]:
        shape = item["shape"]
        rows.append(
            {
                "handle": shape.handle,
                "layer": shape.layer,
                "area_m2": round(shape.area_m2, 2),
                "vertices": len(shape.rings[0]) if shape.rings else 0,
                "close_kind": shape.close_kind,
                "score": round(item["score"], 1),
                "reasons": item["reasons"],
            }
        )
    return {
        "candidates": rows,
        "total_closed": len(records),
        "kept": len(candidates),
        "skipped": skipped,
        "notes": notes,
        "adopted": [row["handle"] for row in rows if row["score"] > 0],
        # 供入口按句柄取回 ShapeRec，不进入 JSON 成果
        "records": {item["shape"].handle: item["shape"] for item in candidates},
    }


def _suppress_parents(candidates: List[Dict], min_children: int, area_hint: float = 3000.0) -> None:
    """父面抑制：包含候选面的大面判为地块/范围/图框，不参与建筑。

    两条判据任一成立即剔除，且要求父面面积至少是最大子面的 1.5 倍：
    1. 包含 ≥ min_children 个候选面；
    2. 本身面积 > area_hint（默认 3000 ㎡）且至少包含 1 个候选面——
       只包含 1 栋建筑的地块也要剔除，否则单图层图上「只有一个院落的地块」会被当建筑。

    只做包围盒包含判断（大图上逐点 contains 太慢），并在调用方提示「粗判」。
    """
    if len(candidates) < 2:
        return
    ordered = sorted(candidates, key=lambda item: -item["shape"].area_m2)
    for parent in ordered:
        pbox = parent["shape"].bounds()
        children = 0
        largest_child = 0.0
        for child in candidates:
            if child is parent or child.get("parent"):
                continue
            cbox = child["shape"].bounds()
            if (
                cbox[0] >= pbox[0]
                and cbox[1] >= pbox[1]
                and cbox[2] <= pbox[2]
                and cbox[3] <= pbox[3]
            ):
                children += 1
                largest_child = max(largest_child, child["shape"].area_m2)
        big_enough = parent["shape"].area_m2 > largest_child * 1.5
        if not big_enough:
            continue
        if children >= min_children or (children >= 1 and parent["shape"].area_m2 > area_hint):
            parent["parent"] = True


def _point_in_shape(point, shape: shapes.ShapeRec) -> bool:
    geometry = getattr(shape, "geometry", None)
    if geometry is None or getattr(geometry, "is_empty", True):
        return False
    try:
        from shapely.geometry import Point

        return bool(geometry.buffer(0.01).contains(Point(float(point[0]), float(point[1]))))
    except Exception:
        return False


def _area_ratio(first, second) -> float:
    """近似 IoU：用于判断「候选面是不是地块面本身」。

    不能用 `交集 / 较小面积`：建筑完全落在地块内时该值也等于 1，会把建筑误判成地块线。
    """
    if first is None or second is None:
        return 0.0
    try:
        union = float(first.union(second).area)
        if union <= 0:
            return 0.0
        return float(first.intersection(second).area) / union
    except Exception:
        return 0.0


# ---------------------------------------------------------------- 指标计算
def floor_height_for(code: Optional[str], args, rules: Dict) -> Tuple[float, str]:
    """层高：命令行 > 按用地代码默认（住宅 3.0 / 非住宅 4.5 / 未识别 3.0）。"""
    override = getattr(args, "floor_height", None)
    if override is not None:
        return float(override), "命令行 --floor-height 指定"
    policy = str(getattr(args, "floor_height_policy", "auto") or "auto")
    if policy == "auto":
        value, source = building_class.layer_floor_height_for(code, rules)
        return value, f"默认策略（{source}）"
    try:
        return float(policy), "命令行 --floor-height-policy 指定"
    except Exception:
        value, source = building_class.layer_floor_height_for(code, rules)
        return value, f"默认策略（{source}）"


def construction_classes(tree) -> List[str]:
    """建设用地一级类，直接取分类表 `balance_groups.construction`。

    2023 指南为 06–16（06 农业设施建设用地 … 16 留白用地），
    GB 50137 为 R/A/B/M/W/S/U/G；两种标准都不写死，换表即生效。
    """
    groups = getattr(tree, "balance_groups", None) or {}
    rows = [str(item).strip().upper() for item in (groups.get("construction") or []) if str(item).strip()]
    return rows or [f"{index:02d}" for index in range(6, 17)]


def filter_parcels(
    metrics: Sequence[ParcelMetric],
    tree,
    mode: str = "auto",
    exclude_classes: Optional[Sequence[str]] = None,
    only_classes: Optional[Sequence[str]] = None,
) -> Tuple[List[ParcelMetric], List[Dict], str]:
    """筛出参与编号与容积率/建筑密度指标的地块，返回 (保留, 剔除清单, 实际模式)。

    auto（默认，已与用户确认）：
    - 识别到用地性质 → 只留建设用地（2023 为 06–16），其中**绿地与开敞空间用地
      （14 / G）默认也不编号、不出指标**；01–05、17–24 同样不编号；
    - 识别不到性质 → 只留有建筑落位的地块，没建筑的空地块不编号；
    - 两种情况都必须在报告里说明，并给出剔除清单（含面积与原因）。

    用户另有要求时：`only_classes`（--parcel-classes）白名单优先；
    `exclude_classes`（--parcel-exclude-classes）只作用于 auto；
    all 恢复全量口径，construction / built 为单一规则。
    """
    mode = mode if mode in PARCEL_FILTER_MODES else "auto"
    construction = set(construction_classes(tree))
    excluded = {str(item).strip().upper() for item in (exclude_classes or []) if str(item).strip()}
    only = {str(item).strip().upper() for item in (only_classes or []) if str(item).strip()}
    kept: List[ParcelMetric] = []
    dropped: List[Dict] = []
    for metric in metrics:
        code = (metric.code or "").strip()
        root = tree.root_of(code) if code else ""
        built = int(metric.building_count or 0) > 0
        reason = ""
        if mode == "all":
            reason = ""
        elif only:
            if root not in only:
                reason = f"一级类 {root or '未识别'} 不在 --parcel-classes 白名单内，不编号、不出指标"
        elif mode == "construction":
            if not (code and root in construction):
                reason = f"非建设用地（一级类 {root or '未识别'}），按 construction 口径不编号、不出指标"
        elif mode == "built":
            if not built:
                reason = "无建筑落位，按 built 口径不编号、不出指标"
        elif code and root in excluded:
            label = "绿地与开敞空间用地" if root in ("14", "G") else "按规则排除的用地大类"
            reason = f"{label}（一级类 {root}）默认不编号、不出容积率/建筑密度"
        elif code and root not in construction:
            reason = f"非建设用地（一级类 {root}），不编号、不出容积率/建筑密度"
        elif not code and not built:
            reason = "用地性质未识别且无建筑落位，不编号、不出指标"
        if not reason:
            kept.append(metric)
            continue
        dropped.append(
            {
                "handle": str(metric.shape.handle),
                "layer": metric.shape.layer,
                "code": code,
                "code_display": metric.code_display or code,
                "name": metric.name,
                "area_m2": round(float(metric.area_m2 or 0.0), 3),
                "building_count": int(metric.building_count or 0),
                "reason": reason,
            }
        )
    return kept, dropped, mode


def compute_metrics(
    parcels: Sequence[shapes.ShapeRec],
    buildings: Sequence[shapes.ShapeRec],
    floors: Dict[str, Dict],
    args,
    rules: Dict,
    default_floors: int = building_class.DEFAULT_FLOORS,
    cross_policy: str = "point",
) -> Tuple[List[ParcelMetric], List[BuildingItem], List[Dict]]:
    """把建筑按代表点归属到地块，算基底/建筑量/容积率/密度。

    返回 (地块指标, 全部建筑条目, 异常清单)。未归属建筑单独返回，由上层列清单。
    """
    policy = cross_policy if cross_policy in CROSS_POLICIES else "point"
    issues: List[Dict] = []
    metrics: List[ParcelMetric] = []
    if not parcels:
        # 无用地边界（单图层常见）：只出全图建筑量与层数清单，容积率/密度不适用
        issues.append(
            {
                "kind": "no_parcel",
                "message": "未识别用地地块，本次只出全图建筑量合计，不计算容积率与建筑密度",
                "handle": "",
                "layer": "",
            }
        )
        fallback_height = float(getattr(args, "floor_height", None) or 3.0)
        items_fallback: List[BuildingItem] = []
        for building in buildings:
            geometry = getattr(building, "geometry", None)
            if geometry is None or getattr(geometry, "is_empty", True):
                continue
            info = floors.get(str(building.handle)) or {}
            floors_value = int(info.get("floors") or default_floors)
            if str(info.get("kind") or "") == "height" and info.get("height_m"):
                floors_value = max(1, int(round(float(info["height_m"]) / max(fallback_height, 1e-6))))
            area = float(building.area_m2 or 0.0)
            item = BuildingItem(
                shape=building,
                floors=floors_value,
                floor_source=str(info.get("source") or "无层数信息，按 1 层（可能低估）"),
                floor_kind=str(info.get("kind") or "none"),
                height_text=info.get("height_m"),
                conflicts=list(info.get("conflicts") or []),
                footprint_m2=area,
            )
            item.floor_area_m2 = area * floors_value
            item.height_m = floors_value * fallback_height
            items_fallback.append(item)
        return [], items_fallback, issues
    for parcel in parcels:
        area = float(parcel.area_m2 or 0.0)
        if parcel.geometry is None or area <= 0:
            issues.append(
                {"kind": "parcel_invalid", "message": "地块几何无效，未参与测算", "handle": parcel.handle, "layer": parcel.layer}
            )
            continue
        info = parcel.code or ""
        parts = list(getattr(parcel, "hybrid_parts", []) or [])
        unresolved = list(getattr(parcel, "hybrid_unresolved", []) or [])
        height_m, height_source = floor_height_for(info, args, rules)
        if len(parts) > 1:
            height_source = f"{height_source}（混合用地按首段 {info or '—'}）"
        display = getattr(parcel, "code_display", "") or info
        if unresolved:
            # --ignore-hybrid-unresolved 时用 ? 标出「这段没认出来」，成果里一眼可见需复核
            display = f"{display or info}?"
        metric = ParcelMetric(
            shape=parcel,
            code=info,
            code_display=display,
            name=parcel.name,
            hybrid_parts=parts,
            hybrid_unresolved=unresolved,
            area_m2=area,
            floor_height_m=height_m,
            floor_height_source=height_source,
        )
        metrics.append(metric)

    items: List[BuildingItem] = []

    for building in buildings:
        geometry = building.geometry
        if geometry is None or getattr(geometry, "is_empty", True):
            issues.append(
                {"kind": "building_invalid", "message": "建筑轮廓几何无效", "handle": building.handle, "layer": building.layer}
            )
            continue
        info = floors.get(str(building.handle)) or {}
        floors_value = int(info.get("floors") or default_floors)
        floor_source = str(info.get("source") or "无层数信息，按 1 层")
        height_text = info.get("height_m")
        floor_kind = str(info.get("kind") or "none")
        if not info:
            floor_source = "无层数信息，按 1 层（可能低估）"
        try:
            representative = geometry.representative_point()
        except Exception:
            representative = None

        overlaps: List[Tuple[ParcelMetric, float]] = []
        for metric in metrics:
            parcel_geom = metric.shape.geometry
            if parcel_geom is None:
                continue
            box = parcel_geom.bounds
            point = representative
            if point is None or not (
                box[0] - 0.1 <= point.x <= box[2] + 0.1 and box[1] - 0.1 <= point.y <= box[3] + 0.1
            ):
                continue
            try:
                inter = float(geometry.intersection(parcel_geom).area)
            except Exception:
                inter = 0.0
            if inter <= 0:
                continue
            ratio = inter / max(float(geometry.area), 1e-9)
            overlaps.append((metric, ratio))

        if not overlaps:
            issues.append(
                {
                    "kind": "building_outside_parcel",
                    "message": "建筑不在任何用地地块内，未计入任何地块指标",
                    "handle": building.handle,
                    "layer": building.layer,
                }
            )
            items.append(
                BuildingItem(
                    shape=building,
                    floors=floors_value,
                    floor_source=floor_source,
                    floor_kind=str(info.get("kind") or "none"),
                    height_text=info.get("height_m"),
                    conflicts=list(info.get("conflicts") or []),
                    excluded=True,
                    footprint_m2=float(building.area_m2 or 0.0),
                )
            )
            continue

        overlaps.sort(key=lambda item: (-item[1], item[0].area_m2))
        owner, owner_ratio = overlaps[0]
        cross = owner_ratio < CROSS_RATIO
        shares: List[Tuple[ParcelMetric, float]] = []
        if cross and policy == "clip":
            total = sum(ratio for _metric, ratio in overlaps) or 1.0
            shares = [(metric, ratio / total) for metric, ratio in overlaps]
        elif cross and policy == "exclude":
            issues.append(
                {
                    "kind": "cross_parcel_building",
                    "message": f"建筑跨地块（占本地块 {owner_ratio * 100:.1f}%），按 --cross-parcel exclude 整栋不计入",
                    "handle": building.handle,
                    "layer": building.layer,
                    "parcel_handle": owner.shape.handle,
                }
            )
            items.append(
                BuildingItem(
                    shape=building,
                    floors=floors_value,
                    floor_source=floor_source,
                    floor_kind=str(info.get("kind") or "none"),
                    conflicts=list(info.get("conflicts") or []),
                    cross=True,
                    excluded=True,
                    footprint_m2=float(building.area_m2 or 0.0),
                )
            )
            continue
        else:
            shares = [(owner, 1.0)]
            if cross:
                issues.append(
                    {
                        "kind": "cross_parcel_building",
                        "message": (
                            f"建筑跨地块（代表点落在本地块，仅 {owner_ratio * 100:.1f}% 面积在内），"
                            f"按代表点整栋计入，可能影响相邻地块容积率，请复核"
                        ),
                        "handle": building.handle,
                        "layer": building.layer,
                        "parcel_handle": owner.shape.handle,
                    }
                )

        for metric, share in shares:
            local_floors = floors_value
            local_source = floor_source
            if floor_kind == "height" and height_text:
                # 只有高度文本时，按**该地块**的层高换算，住宅/非住宅不同层高才不会串
                local_floors = max(1, int(round(float(height_text) / max(metric.floor_height_m, 1e-6))))
                local_source = f"{floor_source}（按本地块层高 {metric.floor_height_m:g}m 换算）"
            item = BuildingItem(
                shape=building,
                floors=local_floors,
                floor_source=local_source,
                floor_kind=floor_kind,
                height_text=height_text,
                conflicts=list(info.get("conflicts") or []),
                parcel_handle=metric.shape.handle,
                parcel_label=metric.code_display or metric.code or metric.shape.layer,
                share=share,
                cross=cross,
                footprint_m2=float(building.area_m2 or 0.0) * share,
            )
            item.floor_area_m2 = item.footprint_m2 * local_floors
            item.height_m = local_floors * metric.floor_height_m
            metric.buildings.append(item)
            metric.footprint_m2 += item.footprint_m2
            metric.floor_area_m2 += item.floor_area_m2
            metric.building_count += 1
            if cross:
                metric.cross_handles.append(str(building.handle))
            if not info:
                metric.default_floor_handles.append(str(building.handle))
            items.append(item)

    for metric in metrics:
        if metric.area_m2 > 0:
            metric.far = metric.floor_area_m2 / metric.area_m2
            metric.coverage = metric.footprint_m2 / metric.area_m2
        if metric.footprint_m2 > 0:
            metric.floors_weighted = metric.floor_area_m2 / metric.footprint_m2
        if metric.coverage > 1.0:
            metric.issues.append(
                f"基底面积合计 {metric.footprint_m2:.1f} ㎡ 超过用地面积 {metric.area_m2:.1f} ㎡（建筑密度 >100%），请复核图层或归属"
            )
        if metric.default_floor_handles:
            metric.issues.append(
                f"{len(metric.default_floor_handles)} 栋建筑无层数信息，按 1 层计入（容积率可能被低估）"
            )
        if metric.cross_handles:
            metric.issues.append(f"{len(metric.cross_handles)} 栋建筑跨地块，归属方式需复核")
        for handle in metric.default_floor_handles:
            issues.append(
                {
                    "kind": "default_floor",
                    "message": "无层数/高度信息，按 1 层计入",
                    "handle": handle,
                    "layer": "",
                    "parcel_handle": metric.shape.handle,
                }
            )
    return metrics, items, issues


def summary_totals(metrics: Sequence[ParcelMetric]) -> Dict:
    area = sum(item.area_m2 for item in metrics)
    footprint = sum(item.footprint_m2 for item in metrics)
    floor_area = sum(item.floor_area_m2 for item in metrics)
    return {
        "parcels": len(metrics),
        "area_m2": area,
        "footprint_m2": footprint,
        "floor_area_m2": floor_area,
        "far": (floor_area / area) if area > 0 else 0.0,
        "coverage": (footprint / area) if area > 0 else 0.0,
    }


def fmt(value: float, digits: int = 2) -> str:
    return f"{float(value):.{digits}f}"


def ha(area_m2: float) -> str:
    return f"{float(area_m2) / 10000.0:.4f}"
