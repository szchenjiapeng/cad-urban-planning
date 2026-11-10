"""用地识别桥：把图层名解析成分类代码，全部复用 core.classify。

设计原则（硬性）：
1. 「是不是地块」与「是哪个代码」分两步判定 —— 代码认不出仍然算地块，
   只是在成果里留空并进确认清单，不能因此让整张图跑不动；
2. 代码解析顺序完全继承 LayerMapper：整名 exact → 混合拆段 → 点名代码 →
   分类表中文原名 → regex → keywords；
3. 旧标准不自动换算：检测到成片 R/A/B/M/W/S/U/G 时先问用户。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from core.classify import (  # noqa: E402
    ClassificationTree,
    LayerMapper,
    guess_standard,
    hybrid_parts as split_hybrid_parts,
    load_mapping,
    load_standard,
)
from lib import layer_spec as spec_mod  # noqa: E402

_SHORT_CODE = re.compile(r"^\d{2}$")
_CODE_LIKE = re.compile(r"^\d{4,6}$")


@dataclass
class LayerInfo:
    layer: str
    is_parcel: bool = False
    code: Optional[str] = None
    name: str = ""
    matched: str = ""
    source: str = ""
    unregistered: List[str] = field(default_factory=list)
    low_confidence: bool = False
    reason: str = ""
    # 混合用地（如 100104+0909）：code 恒为首段主代码，合成代码见 display_code。
    # 面积始终按整块几何计量一次，不按段拆分，避免重复计容。
    primary_code: Optional[str] = None
    hybrid_raw: str = ""
    hybrid_parts: List[str] = field(default_factory=list)
    hybrid_unresolved: List[str] = field(default_factory=list)

    @property
    def display_code(self) -> str:
        """成果里显示的用地代码：混合用地拼成 100104+0909，单段仍是原代码。"""
        if len(self.hybrid_parts) > 1:
            return "+".join(self.hybrid_parts)
        return self.code or ""

    @property
    def is_hybrid(self) -> bool:
        return len(self.hybrid_parts) > 1


@dataclass
class Classifier:
    standard: str
    tree: ClassificationTree
    mapper: LayerMapper


def build(standard: str = "2023", classification_path: Optional[str] = None, mapping_path: Optional[str] = None) -> Classifier:
    tree = load_standard(standard, classification_path)
    mapper = load_mapping(mapping_path, standard, tree=tree)
    return Classifier(standard=standard, tree=tree, mapper=mapper)


def detect_standard(layers: Sequence[str], mapper: Optional[LayerMapper] = None) -> Dict:
    """沿用平衡表的判标逻辑；返回 {standard, confidence, old, new}。"""
    return guess_standard(layers, mapper)


def _code_matches(layer_code: Optional[str], wanted: Sequence[str]) -> bool:
    if not layer_code:
        return False
    for item in wanted:
        target = str(item or "").strip().upper().replace("-", "")
        if not target:
            continue
        candidate = layer_code.upper().replace("-", "")
        if candidate == target or candidate.startswith(target):
            return True
    return False


def inspect_layer(layer: str, classifier: Classifier, spec: Dict, allow_short_code: bool = False) -> LayerInfo:
    """判定单个图层的归属、代码与置信度（不抛异常）。"""
    info = LayerInfo(layer=layer)
    name = str(layer or "").strip()
    if not name:
        info.reason = "图层名为空"
        return info

    if name.lower() in {"0", "defpoints"}:
        info.reason = "CAD 默认图层，不参与用地识别"
        return info
    try:
        if classifier.mapper.is_ignored(name):
            info.reason = "映射配置 ignore_layers 命中（图框/标注/轴线等）"
            return info
    except Exception:
        pass

    if spec_mod.excluded(name, spec, "landuse"):
        info.reason = "命中用地组排除词（范围线/界线/标注/图框等）"
        return info

    # 纯两位数字：可能是图号/编号，默认不当地块
    if _SHORT_CODE.match(name) and not allow_short_code:
        info.reason = "图层名只有两位数字，按低置信度排除（可用 --short-code on 纳入）"
        info.low_confidence = True
        return info

    unregistered = classifier.mapper.unregistered_codes(name)
    code, matched, segments = classifier.mapper.match_detail(name)
    prefix_hit = spec_mod.layer_in_group(name, spec, "landuse")

    if code:
        info.is_parcel = True
        info.code = code
        info.primary_code = code
        info.matched = matched or name
        node = classifier.tree.get(code)
        info.name = node.name if node is not None else ""
        info.source = "code" if _CODE_LIKE.match(code) or re.match(r"^[A-Z]{1,2}\d{0,3}$", code.upper()) else "mapping"
        info.unregistered = [token for token in unregistered if token != code]
        if len(segments) > 1:
            # 混合用地：沿用平衡表同一套拆段逻辑，产出 100104+0909。
            # 这里只做「标签」，面积仍由调用方按整块几何计一次。
            parts, unresolved = split_hybrid_parts(classifier.mapper, code, segments)
            info.hybrid_raw = name
            info.hybrid_parts = parts
            info.hybrid_unresolved = unresolved
        return info

    if unregistered:
        # 形似代码但分类表没有：算地块，但代码必须由用户确认
        info.is_parcel = True
        info.unregistered = unregistered
        info.source = "unregistered"
        info.reason = "图层名含未登记代码，需确认后归类"
        return info

    if prefix_hit:
        # 只命中「用地」前缀/别名，没有可识别代码：仍算地块，代码留空
        info.is_parcel = True
        info.source = "prefix"
        info.low_confidence = True
        info.reason = "按用地图层前缀识别，但名称未命中分类表，代码待确认"
        return info

    info.reason = "未命中用地代码、中文原名、混合规则或前缀关键词"
    return info


def inspect_layers(
    layers: Sequence[str],
    classifier: Classifier,
    spec: Dict,
    allow_short_code: bool = False,
) -> List[LayerInfo]:
    return [inspect_layer(name, classifier, spec, allow_short_code=allow_short_code) for name in layers]


def select_layers_by_code(layers: Sequence[str], classifier: Classifier, spec: Dict, codes: Sequence[str]) -> List[str]:
    """按分类代码（支持一级/二级前缀，如 13 命中 1301）挑图层。

    这里是**用户显式点名代码**的场景（五线派生、按代码取图层），
    所以允许命中纯两位的一级类代码（17 陆地水域、13 公用设施）；
    自动地块编号仍走 inspect_layer 的默认 short_code 策略。
    """
    wanted = [str(code).strip() for code in codes if str(code).strip()]
    if not wanted:
        return []
    matched: List[str] = []
    for name in layers:
        info = inspect_layer(name, classifier, spec, allow_short_code=True)
        if not info.is_parcel:
            continue
        # 混合用地的任一段被点名都应命中：100104+0909 同时属于 100104 与 0909
        candidates = [info.code] + list(info.hybrid_parts)
        if any(_code_matches(candidate, wanted) for candidate in candidates):
            matched.append(name)
    return matched


def summarize_parcel_layers(infos: Sequence[LayerInfo]) -> Tuple[List[LayerInfo], List[LayerInfo]]:
    """拆成（可作为地块, 待确认/忽略）两组。"""
    ok: List[LayerInfo] = []
    other: List[LayerInfo] = []
    for info in infos:
        if info.is_parcel:
            ok.append(info)
        else:
            other.append(info)
    return ok, other


def hybrid_rows(infos: Sequence[LayerInfo]) -> List[Dict]:
    """混合用地图层汇总：图层 → 合成代码 → 未解析段（供报告与 JSON）。"""
    rows: List[Dict] = []
    for info in infos:
        if not info.is_hybrid and not info.hybrid_unresolved:
            continue
        rows.append(
            {
                "layer": info.hybrid_raw or info.layer,
                "code": info.display_code,
                "primary": info.primary_code or info.code or "",
                "parts": list(info.hybrid_parts),
                "unresolved": list(info.hybrid_unresolved),
            }
        )
    rows.sort(key=lambda row: (len(row["unresolved"]) == 0, row["code"], row["layer"]))
    return rows


def hybrid_unresolved_groups(infos: Sequence[LayerInfo]) -> List[Dict]:
    """混合段既非表内代码也非表内名称的图层，按图层聚合成待确认数据。

    不猜、不静默降级成首段：由上层输出确认项（退出码 3）后带参数重跑。
    """
    groups: List[Dict] = []
    for info in infos:
        if not info.hybrid_unresolved:
            continue
        groups.append(
            {
                "layer": info.hybrid_raw or info.layer,
                "segments": list(info.hybrid_unresolved),
                "code": info.display_code,
            }
        )
    groups.sort(key=lambda row: row["layer"])
    return groups
