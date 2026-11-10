"""统一数据模型：后端、几何、分类、渲染四层共用，后续 Excel/Word 渲染器可零改动复用。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

Point = Tuple[float, float]


@dataclass
class PolylineRecord:
    """从 CAD 读到的一条候选用地界线（尚未做闭合与面积判定）。"""

    handle: str
    layer: str
    entity_type: str
    vertices: List[Point] = field(default_factory=list)
    bulges: List[float] = field(default_factory=list)
    closed_flag: bool = False
    cad_area: Optional[float] = None  # CAD 自算面积，单位：图纸单位²


@dataclass
class TextRecord:
    """图纸文字/多行文字。地块编号、建筑层数、标高都靠它，只读不改。"""

    handle: str
    layer: str
    text: str
    point: Point
    height: float = 0.0
    rotation: float = 0.0
    entity_type: str = "TEXT"


@dataclass
class HatchRecord:
    """填充对象。rings 为边界环列表（外环 + 孤岛），按奇偶嵌套规则求净面积。

    COM 通道读 Area 失败时 rings 可能为空、area_drawing 为 None，
    由上层把这些句柄导出 DXF 后重建边界。
    """

    handle: str
    layer: str
    rings: Optional[List[List[Point]]] = None
    area_drawing: Optional[float] = None  # CAD 自算面积，单位：图纸单位²
    area_error: str = ""
    entity_type: str = "HATCH"
    # 弧段离散的备选方案（信任 DXF 的 ccw 标志，保留真正的优弧）。
    # 有 CAD 自算面积时选更接近的那一套，避免「强制劣弧」在个别图上把面积放大。
    rings_alt: Optional[List[List[Point]]] = None


@dataclass
class ParcelRecord:
    """完成闭合判定与面积换算后的地块。"""

    handle: str
    layer: str
    entity_type: str
    area_m2: float
    code: Optional[str] = None
    name: Optional[str] = None
    parent_code: Optional[str] = None
    closed_flag: bool = True
    geometry_closed: bool = True
    source_kind: str = "polyline"  # hatch / polyline
    outer: Optional[List[Point]] = None  # 外轮廓顶点，供写回与包围盒；空间运算请用 rings
    rings: Optional[List[List[Point]]] = None  # 全部边界环（外环 + 孤岛），供带洞空间运算
    clipped: bool = False  # 是否被规划范围裁剪过
    clipped_points: Optional[List[Point]] = None  # 裁剪后的外环，供写回核查
    hybrid_raw: str = ""  # 混合用地原始图层名，如 0802+0903
    hybrid_used: str = ""  # 实际采用的段，如 0802
    hybrid_parts: List[str] = field(default_factory=list)  # 各段解析出的代码，如 ["070101", "0809"]
    hybrid_unresolved: List[str] = field(default_factory=list)  # 解析失败的段原文
    origin_code: str = ""  # 换算前的旧标准代码，如 R2
    warnings: List[str] = field(default_factory=list)


@dataclass
class Issue:
    """异常项，逐条给句柄与图层，便于在 CAD 中定位复核。"""

    kind: str  # unclosed / self_intersect / overlap / zero_area / out_of_scope / backend
    message: str
    handle: str = ""
    layer: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "message": self.message, "handle": self.handle, "layer": self.layer}


@dataclass
class SummaryRow:
    code: str
    name: str
    level: int
    area_m2: float
    count: int = 0
    parent: Optional[str] = None
    ratio: float = 0.0
    per_capita: float = 0.0
    handles: List[str] = field(default_factory=list)
    derived: bool = False  # True = 差额反算得出，非图面实测

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "level": self.level,
            "parent": self.parent,
            "area_m2": round(self.area_m2, 2),
            "count": self.count,
            "ratio": round(self.ratio, 4),
            "per_capita": round(self.per_capita, 2),
            "derived": self.derived,
        }


@dataclass
class HybridRow:
    """混合用地图层的归并记录：0802+0903 只计首段 0802。"""

    layer: str
    used: str
    code: str
    name: str
    count: int = 0
    area_m2: float = 0.0

    def to_dict(self) -> dict:
        return {
            "layer": self.layer,
            "used": self.used,
            "code": self.code,
            "name": self.name,
            "count": self.count,
            "area_m2": round(self.area_m2, 2),
        }


@dataclass
class UnclassifiedRow:
    layer: str
    count: int
    area_m2: float
    handles: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "layer": self.layer,
            "count": self.count,
            "area_m2": round(self.area_m2, 2),
            "sample_handles": self.handles[:5],
        }


@dataclass
class CheckItem:
    label: str
    ok: bool
    message: str = ""

    def to_dict(self) -> dict:
        return {"label": self.label, "ok": self.ok, "message": self.message}


@dataclass
class RunMeta:
    channel: str = ""  # com / dxf
    source: str = ""
    standard: str = ""
    unit: str = ""  # m / mm / cm
    unit_source: str = ""
    unit_factor: float = 1.0  # 图纸单位 -> 米 的换算系数
    scope: str = "all"
    plan_area_m2: Optional[float] = None
    population: Optional[float] = None
    generated_at: str = ""
    mapping_file: str = ""
    classification_file: str = ""
    layer_mode: str = ""
    layout: str = ""
    scope_layer: str = ""
    scope_relation: str = ""
    scope_area_m2: Optional[float] = None
    scope_method: str = ""
    clip_mode: str = ""
    blocks_expanded: int = 0
    blocks_entities: int = 0
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "channel": self.channel,
            "source": self.source,
            "standard": self.standard,
            "unit": self.unit,
            "unit_source": self.unit_source,
            "unit_factor": self.unit_factor,
            "scope": self.scope,
            "plan_area_m2": self.plan_area_m2,
            "population": self.population,
            "generated_at": self.generated_at,
            "mapping_file": self.mapping_file,
            "classification_file": self.classification_file,
            "layer_mode": self.layer_mode,
            "layout": self.layout,
            "scope_layer": self.scope_layer,
            "scope_relation": self.scope_relation,
            "scope_area_m2": self.scope_area_m2,
            "scope_method": self.scope_method,
            "clip_mode": self.clip_mode,
            "blocks_expanded": self.blocks_expanded,
            "blocks_entities": self.blocks_entities,
            "notes": self.notes,
        }


@dataclass
class Summary:
    """一次测算的完整结果，渲染层只依赖这个对象。"""

    meta: RunMeta = field(default_factory=RunMeta)
    level1_rows: List[SummaryRow] = field(default_factory=list)
    level2_rows: List[SummaryRow] = field(default_factory=list)
    level3_rows: List[SummaryRow] = field(default_factory=list)
    hybrid_rows: List[HybridRow] = field(default_factory=list)
    road_derivation: Optional[dict] = None
    conversion: Optional[dict] = None
    overlap: Optional[dict] = None
    balance_spec: Optional[dict] = None
    total_area_m2: float = 0.0
    pre_derived_total_m2: float = 0.0
    entity_count: int = 0
    parcel_count: int = 0
    issues: List[Issue] = field(default_factory=list)
    unclassified: List[UnclassifiedRow] = field(default_factory=list)
    unknown_codes: Dict[str, str] = field(default_factory=dict)  # 表外代码 -> 首次出现的图层
    hybrid_unresolved: List[dict] = field(default_factory=list)  # 混合用地里没解析出来的段
    checks: List[CheckItem] = field(default_factory=list)
    ratio_base_m2: float = 0.0
    layer_report: Optional[dict] = None
    scope_info: Optional[dict] = None
    confirmations: List[dict] = field(default_factory=list)
    # 重叠告警闸门：同图层只提示不追问，跨图层超阈值必须停表四选一
    overlap_gate: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "meta": self.meta.to_dict(),
            "total_area_m2": round(self.total_area_m2, 2),
            "pre_derived_total_m2": round(self.pre_derived_total_m2, 2),
            "ratio_base_m2": round(self.ratio_base_m2, 2),
            "entity_count": self.entity_count,
            "parcel_count": self.parcel_count,
            "level1": [r.to_dict() for r in self.level1_rows],
            "level2": [r.to_dict() for r in self.level2_rows],
            "level3": [r.to_dict() for r in self.level3_rows],
            "hybrid": [h.to_dict() for h in self.hybrid_rows],
            "road_derivation": self.road_derivation,
            "conversion": self.conversion,
            "overlap": self.overlap,
            "balance_spec": self.balance_spec,
            "unclassified": [u.to_dict() for u in self.unclassified],
            "issues": [i.to_dict() for i in self.issues],
            "checks": [c.to_dict() for c in self.checks],
            "layer_report": self.layer_report,
            "scope_info": self.scope_info,
            "confirmations": self.confirmations,
            "overlap_gate": self.overlap_gate,
        }
