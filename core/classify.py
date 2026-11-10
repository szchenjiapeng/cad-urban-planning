"""分类层：图层名 → 用地分类代码，按分类树逐级汇总。

规则全部来自 assets/config 下的 JSON，脚本不硬编码任何分类代码。
"""

from __future__ import annotations

import fnmatch
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from core.models import (  # noqa: E402
    CheckItem,
    HybridRow,
    ParcelRecord,
    RunMeta,
    Summary,
    SummaryRow,
    UnclassifiedRow,
)

CONFIG_DIR = _SCRIPTS_DIR / "assets"
STANDARD_FILES = {
    "2023": CONFIG_DIR / "classification.2023.json",
    "gb50137": CONFIG_DIR / "classification.gb50137.json",
}
DEFAULT_MAPPING_FILES = {
    "2023": CONFIG_DIR / "mapping.2023.example.json",
    "gb50137": CONFIG_DIR / "mapping.gb50137.example.json",
}
CONVERSION_FILES = {
    ("gb50137", "2023"): CONFIG_DIR / "conversion.gb50137_to_2023.json",
}

# 混合用地尾注，如 0802+0903(30%) 里的括号占比
_HYBRID_TAIL = re.compile(r"[（(][^）)]*[）)]\s*$")

# 混合段里「像代码」的字符：带数字/字母的段解析不出来才算失败，纯中文按名称描述处理
_CODE_HINT = re.compile(r"[0-9A-Za-z]")

# 「点名代码」：从图层名里抠出像分类代码的片段。
# 2023 指南为纯数字（2/4/6 位，允许 07-01-01 写法）；GB 50137 为字母+数字（R/R1/R11）。
_CODE_TOKEN_2023 = re.compile(r"(?<!\d)(\d{2}(?:-?\d{2}){0,2})(?!\d)")
_CODE_TOKEN_GB = re.compile(r"(?<![A-Za-z\d])([A-Z]{1,2}\d{0,3})(?![A-Za-z\d])")


@dataclass
class CategoryNode:
    code: str
    name: str
    level: int
    parent: Optional[str] = None
    in_balance: bool = True
    source: str = ""


class ClassificationTree:
    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"分类配置不存在：{self.path}")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.standard = data.get("standard", "")
        self.title = data.get("title", "")
        self.caveat = data.get("caveat", "")
        self.derived_road: Dict[str, str] = dict(data.get("derived_road") or {})
        self.balance_groups: Dict = dict(data.get("balance_groups") or {})
        self.nodes: Dict[str, CategoryNode] = {}
        for item in data.get("nodes", []):
            code = str(item["code"])
            self.nodes[code] = CategoryNode(
                code=code,
                name=str(item.get("name", code)),
                level=int(item.get("level", 2 if len(code) > 2 else 1)),
                parent=item.get("parent"),
                in_balance=bool(item.get("in_balance", True)),
                source=str(item.get("source", "")),
            )
        self.order: Dict[str, int] = {code: idx for idx, code in enumerate(self.nodes)}
        self._names: Dict[str, List[str]] = {}
        for code, node in self.nodes.items():
            self._names.setdefault(node.name.strip(), []).append(code)

    def level_of(self, code: Optional[str]) -> int:
        node = self.get(code) if code else None
        return node.level if node else 0

    def codes_by_name(self, name: str) -> List[str]:
        """按分类表的中文原名查代码；重名多个时返回多个，由调用方判为歧义。"""
        return list(self._names.get((name or "").strip(), []))

    def register_node(self, code: str, name: str) -> CategoryNode:
        """注册分类表里没有的新代码：层级按「最长可挂接前缀」推断，父级挂在该前缀下。"""
        code = str(code).strip()
        if code in self.nodes:
            return self.nodes[code]
        level, parent = 1, None
        for cut in range(len(code) - 1, 0, -1):
            prefix = code[:cut]
            hit = self.nodes.get(prefix)
            if hit is not None and hit.level < 3:
                level, parent = hit.level + 1, prefix
                break
        node = CategoryNode(
            code=code,
            name=(name or "").strip() or f"代码 {code}",
            level=level,
            parent=parent,
            in_balance=True,
            source="user",
        )
        self.nodes[code] = node
        self.order[code] = len(self.order)
        self._names.setdefault(node.name.strip(), []).append(code)
        return node

    def non_official(self) -> List[CategoryNode]:
        return [node for node in self.nodes.values() if node.source]

    def get(self, code: Optional[str]) -> Optional[CategoryNode]:
        if code is None:
            return None
        return self.nodes.get(str(code))

    def parent_of(self, code: str) -> Optional[str]:
        node = self.get(code)
        return node.parent if node else None

    def root_of(self, code: str) -> str:
        node = self.get(code)
        while node is not None and node.parent:
            node = self.get(node.parent)
        return node.code if node else code

    def roots(self) -> List[CategoryNode]:
        return [node for node in self.nodes.values() if node.level == 1 and node.in_balance]

    def children(self, code: str) -> List[CategoryNode]:
        return [node for node in self.nodes.values() if node.parent == code and node.in_balance]


@dataclass
class MatchRule:
    code: str
    exact: List[str] = field(default_factory=list)
    regex: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)


class LayerMapper:
    """图层名 → 分类代码。

    匹配顺序：整名 exact → 混合用地拆段取首段（段内点名代码 → exact → regex → keywords）
    → 整名点名代码 → 整名 regex → 整名 keywords，规则顺序即优先级。
    """

    def __init__(self, path: Path, tree: Optional["ClassificationTree"] = None):
        self.path = Path(path)
        self.tree = tree
        if not self.path.exists():
            raise FileNotFoundError(f"映射配置不存在：{self.path}")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.ignore_layers = [str(x) for x in data.get("ignore_layers", [])]
        self.strip_prefixes = [str(x) for x in data.get("strip_prefixes", [])]
        self.hybrid_separators = [str(x) for x in data.get("hybrid_separators", ["+", "＋", "/", "／", "、", "&", "，"])]
        self.rules = [
            MatchRule(
                code=str(item["code"]),
                exact=[str(x) for x in item.get("exact", [])],
                regex=[str(x) for x in item.get("regex", [])],
                keywords=[str(x) for x in item.get("keywords", [])],
            )
            for item in data.get("rules", [])
        ]
        self._compiled = {id(rule): [re.compile(p, re.IGNORECASE) for p in rule.regex] for rule in self.rules}
        # 用户显式指定的归属：图层名里的代码 → 目标代码（--unregistered-code CODE=map:目标）
        self.code_overrides: Dict[str, str] = {}

    def is_ignored(self, layer: str) -> bool:
        name = layer.strip()
        for pattern in self.ignore_layers:
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(name.lower(), pattern.lower()):
                return True
        return False

    def candidates(self, layer: str) -> List[str]:
        raw = layer.strip()
        names = [raw]
        for prefix in self.strip_prefixes:
            if prefix and raw.lower().startswith(prefix.lower()):
                stripped = raw[len(prefix):].strip()
                if stripped and stripped not in names:
                    names.append(stripped)
        return names

    def code_tokens(self, name: str) -> List[str]:
        """抠出图层名里「像分类代码」的片段，最长者在前（点名优先用）。"""
        text = (name or "").strip()
        if not text:
            return []
        found: List[str] = []
        for match in _CODE_TOKEN_2023.finditer(text):
            raw = match.group(1)
            found.append(raw.replace("-", ""))
            if "-" in raw:
                found.append(raw)
        for match in _CODE_TOKEN_GB.finditer(text.upper()):
            found.append(match.group(1))
        ordered: List[str] = []
        seen = set()
        for token in found:
            if token and token not in seen:
                seen.add(token)
                ordered.append(token)
        ordered.sort(key=lambda item: -len(item))
        return ordered

    def match_code_point(self, name: str) -> Optional[Tuple[str, str]]:
        """点名优先：名字里出现分类表中真实存在的完整代码就直接认它，最长者胜。

        整名就是代码（070101 / R11 / 01）→ 直接认；
        否则只接受长度 ≥ 3 的代码片段，避免「标注-01」被当成耕地 01。
        """
        stripped = (name or "").strip()
        if self.tree is None or not stripped:
            return None
        node = self.tree.get(stripped.upper())
        if node is not None:
            return node.code, stripped
        for token in self.code_tokens(stripped):
            if len(token) < 3:
                continue
            node = self.tree.get(token)
            if node is not None:
                return node.code, token
        return None

    def match_official_name(self, name: str) -> Optional[Tuple[str, str]]:
        """图层名就是分类表的中文原名（图纸里没写代码）时直接映射。

        重名多个视为歧义，返回 None，交给上层向用户确认。
        """
        if self.tree is None:
            return None
        text = (name or "").strip()
        if not text:
            return None
        codes = self.tree.codes_by_name(text)
        if len(codes) == 1:
            return codes[0], text
        return None

    def unregistered_codes(self, name: str) -> List[str]:
        """图层名里「形如分类代码、但分类表中没有登记」的片段，如 090201 / 0909 / 0809。

        这类名字不能静默降级到关键词兜底：调用方必须先向用户确认
        「图层名是否写错 / 是否新增该用地代码」，确认后再归类。
        """
        stripped = (name or "").strip()
        if self.tree is None or not stripped:
            return []
        found: List[str] = []
        for token in self.code_tokens(stripped):
            digits = token.replace("-", "")
            if len(digits) < 4:  # 两位代码（01/09）不在此列，避免「标注-01」误报
                continue
            if self.tree.get(token) is None and token not in self.code_overrides:
                found.append(token)
        return found

    def match(self, layer: str) -> Optional[str]:
        code, _, _ = self.match_detail(layer)
        return code

    def match_detail(self, layer: str) -> Tuple[Optional[str], str, List[str]]:
        """返回 (分类代码, 命中的名称片段, 全部片段)。

        整体名先按 exact → regex → keywords 匹配；都不中且图层名形如 0802+0903 时，
        按分隔符拆段，只取**第一段**命中结果（严格取首段，不用后面的段兜底）。
        """
        if self.is_ignored(layer):
            return None, "", []
        # 用户显式指定的归属优先于任何规则（--unregistered-code CODE=map:目标）
        if self.code_overrides:
            for token in self.code_tokens((layer or "").strip()):
                target = self.code_overrides.get(token)
                if target:
                    return target, token, []
        names = self.candidates(layer)

        # ① 整名 exact：显式写全的图层名最优先
        hit = self.match_names(names, mode="exact")
        if hit:
            return hit[0], hit[1], []

        # ② 混合用地：先拆段，按段顺序取首个命中（首段优先，不用后面的段兜底）
        for name in names:
            segments = self.split_hybrid(name)
            if len(segments) <= 1:
                continue
            for segment in segments:
                hit = (
                    self.match_code_point(segment)
                    or self.match_official_name(segment)
                    or self.match_names(self.candidates(segment))
                )
                if hit:
                    return hit[0], hit[1], segments

        # ③ 单段：点名代码 → 分类表中文名 → regex → keywords
        for name in names:
            hit = self.match_code_point(name) or self.match_official_name(name)
            if hit:
                return hit[0], hit[1], []
        for mode in ("regex", "keywords"):
            hit = self.match_names(names, mode=mode)
            if hit:
                return hit[0], hit[1], []
        return None, "", self.split_hybrid(names[0])

    def match_names(self, names: Sequence[str], mode: str = "all") -> Optional[Tuple[str, str]]:
        """按 exact → regex → keywords 匹配，返回 (代码, 命中的名称)。

        mode 可取 all / exact / regex / keywords，用于把三个阶段拆开调用。
        """
        modes = (mode,) if mode != "all" else ("exact", "regex", "keywords")
        if "exact" in modes:
            for name in names:
                lowered = name.lower()
                for rule in self.rules:
                    if lowered in {item.lower() for item in rule.exact}:
                        return rule.code, name
        if "regex" in modes:
            for name in names:
                for rule in self.rules:
                    for pattern in self._compiled[id(rule)]:
                        if pattern.search(name):
                            return rule.code, name
        if "keywords" in modes:
            for name in names:
                lowered = name.lower()
                for rule in self.rules:
                    for keyword in rule.keywords:
                        if keyword.lower() in lowered:
                            return rule.code, name
        return None

    def split_hybrid(self, name: str) -> List[str]:
        """0802+0903(30%) → ['0802', '0903']：先去括号尾注，再按分隔符拆分。"""
        cleaned = _HYBRID_TAIL.sub("", name).strip()
        if not cleaned:
            return [name]
        pattern = "|".join(re.escape(sep) for sep in self.hybrid_separators if sep)
        parts = re.split(pattern, cleaned) if pattern else [cleaned]
        return [part.strip() for part in parts if part.strip()]

def load_standard(standard: str, classification_path: Optional[str] = None) -> ClassificationTree:
    if classification_path:
        return ClassificationTree(Path(classification_path))
    path = STANDARD_FILES.get(standard)
    if path is None:
        raise ValueError(f"未知分类标准：{standard}（可选 2023 / gb50137，或用 --classification 指定文件）")
    return ClassificationTree(path)


def load_mapping(
    mapping_path: Optional[str],
    standard: str,
    tree: Optional["ClassificationTree"] = None,
) -> LayerMapper:
    """加载图层映射；tree 用于「点名代码优先」判断代码是否真实存在。"""
    if mapping_path:
        return LayerMapper(Path(mapping_path), tree=tree)
    path = DEFAULT_MAPPING_FILES.get(standard)
    if path is None or not path.exists():
        raise FileNotFoundError(f"未找到 {standard} 的默认映射配置，请用 --mapping 指定")
    return LayerMapper(path, tree=tree)


def missing_rule_codes(mapper: LayerMapper, tree: Optional["ClassificationTree"]) -> List[str]:
    """映射规则里引用了、但分类表中没有登记的代码。

    典型事故：规则把某图层指向 0809，而分类表没有 0809 节点 —— 面积只会挂到
    一级类合计上，平衡表出现「合计 ≠ 分项和」。这类配置必须向用户确认后修正。
    """
    if tree is None:
        return []
    return sorted({rule.code for rule in mapper.rules if tree.get(rule.code) is None})


def suggest_node(code: str, layer_name: str = "") -> Dict[str, object]:
    """按代码长度推断层级与父级，给出「新增用地代码」的草案，供用户确认。"""
    digits = code.replace("-", "")
    name = (layer_name or "").strip()
    if name.startswith(code):
        name = name[len(code):].strip(" -_　")
    name = name or f"代码 {code}"
    if len(digits) <= 2:
        level, parent = 1, None
    elif len(digits) <= 4:
        level, parent = 2, digits[:2]
    else:
        level, parent = 3, digits[:4]
    return {"code": digits, "name": name, "level": level, "parent": parent}



def hybrid_parts(
    mapper: LayerMapper,
    first_code: str,
    segments: Sequence[str],
) -> Tuple[List[str], List[str]]:
    """混合用地各段解析成代码：首段用已定案代码，其余段逐个点名 / 中文名 / 规则匹配。

    解析不出的段不猜、也不拼进代码，而是收进 unresolved，由上层先问用户
    「名称写错了，还是新增该代码」。

    平衡表（`classify_parcels`）与容积率测算（`lib.landuse`）共用这一套拆段逻辑，
    两边必须得到同一个合成代码（如 `100104+0909`），不得各自实现一份。
    """
    parts: List[str] = [first_code]
    unresolved: List[str] = []
    for segment in segments:
        text = (segment or "").strip()
        if not text or text == first_code or text in parts:
            continue
        # 段内只认「代码点名」与「分类表中文原名」：混合段必须是真代码或官方名称。
        # 不用 regex/keywords 兜底，否则「研发设计混合用地」会被 0802 的「研发」关键词
        # 抢成第三段，把 0902+100104 拼成 0902+100104+0802。
        hit = mapper.match_code_point(text) or mapper.match_official_name(text)
        if hit and hit[0] in parts:
            # 该段解析出的就是已定案的代码（首段带名称前缀时常见，如
            # 「YD-商业商务用地-0901+0902」的首段），不重复计量、也不算未解析
            continue
        if hit:
            parts.append(hit[0])
            continue
        if not _CODE_HINT.search(text):
            # 纯中文、又不是表内名称：是给前一段配的文字描述（如「商住混合」「公交场站混合用地」），
            # 不构成新的一段，也算不上解析失败
            continue
        unresolved.append(text)
    return parts, unresolved


# 兼容旧引用：历史上叫 _hybrid_parts，现已公共化
_hybrid_parts = hybrid_parts


def classify_parcels(
    parcels: Sequence[ParcelRecord],
    tree: ClassificationTree,
    mapper: LayerMapper,
) -> tuple:
    """就地补全 code/name/parent_code，返回 (已分类, 未分类图层字典, 未知代码集合)。"""
    assigned: List[ParcelRecord] = []
    unclassified: Dict[str, UnclassifiedRow] = {}
    unknown_codes: Dict[str, str] = {}

    for parcel in parcels:
        if mapper.is_ignored(parcel.layer):
            continue
        code, used, segments = mapper.match_detail(parcel.layer)
        node = tree.get(code)
        if node is None:
            if code is not None and code not in unknown_codes:
                unknown_codes[code] = parcel.layer
            row = unclassified.get(parcel.layer)
            if row is None:
                row = UnclassifiedRow(layer=parcel.layer, count=0, area_m2=0.0)
                unclassified[parcel.layer] = row
            row.count += 1
            row.area_m2 += parcel.area_m2
            if len(row.handles) < 5:
                row.handles.append(parcel.handle)
            continue
        parcel.code = node.code
        parcel.name = node.name
        parcel.parent_code = node.parent
        if len(segments) > 1:
            parcel.hybrid_raw = parcel.layer
            parcel.hybrid_used = used
            parts, unresolved = hybrid_parts(mapper, node.code, segments)
            parcel.hybrid_parts = parts
            parcel.hybrid_unresolved = unresolved
        assigned.append(parcel)

    return assigned, unclassified, unknown_codes


class ConversionTable:
    """旧标准代码 → 新标准代码。

    数据来自 assets/config/conversion.gb50137_to_2023.json（由新老代码对照表 PDF 抽取）。
    查不到精确对应时按上位类回退（R21 → R2 → R），回退还查不到就判为未换算，
    绝不猜测到某个具体二级类。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"换算表不存在：{self.path}")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.source = data.get("source", "")
        self.note = data.get("note", "")
        self.from_standard = str(data.get("from") or "")
        self.to_standard = str(data.get("to") or "")
        self.table: Dict[str, dict] = {
            str(key).upper(): value for key, value in (data.get("old_to_new") or {}).items()
        }

    def lookup(self, code: str) -> Tuple[Optional[str], str]:
        """返回 (新代码, 依据)；依据 ∈ exact / parent / ambiguous / missing。"""
        if not code:
            return None, "missing"
        key = code.upper()
        entry = self.table.get(key)
        if entry:
            if entry.get("confidence") == "ambiguous":
                return None, "ambiguous"
            new = entry.get("new")
            if new:
                return str(new), "exact"
        # 上位类回退：R21 → R2 → R
        for cut in range(len(key) - 1, 0, -1):
            prefix = key[:cut]
            entry = self.table.get(prefix)
            if not entry or entry.get("confidence") == "ambiguous":
                continue
            new = entry.get("new")
            if new:
                return str(new), "parent"
        return None, "missing"

    def ambiguous_candidates(self, code: str) -> List[str]:
        entry = self.table.get((code or "").upper()) or {}
        return list(entry.get("candidates") or [])


def load_conversion(
    from_standard: str, to_standard: str, path: Optional[str] = None
) -> ConversionTable:
    target = Path(path) if path else CONVERSION_FILES.get((from_standard, to_standard))
    if target is None:
        raise FileNotFoundError(f"没有内置 {from_standard} → {to_standard} 的换算表，请用 --conversion 指定")
    return ConversionTable(target)


OLD_CODE_RE = re.compile(r"^[A-Z]\d{0,2}$")
NEW_CODE_RE = re.compile(r"^\d{2,4}$")


def guess_standard(layers: Sequence[str], mapper: Optional[LayerMapper] = None) -> Dict[str, object]:
    """从图层名粗略判断图纸用的是新标准还是旧标准，用于提示是否需要询问用户。"""
    old_hits: List[str] = []
    new_hits: List[str] = []
    for layer in layers:
        candidates = [layer]
        if mapper is not None:
            candidates = mapper.candidates(layer)
        for name in candidates:
            token = name.strip().upper().replace("-", "").replace("_", "")
            if OLD_CODE_RE.match(token):
                old_hits.append(name)
                break
            if NEW_CODE_RE.match(token):
                new_hits.append(name)
                break
    if len(old_hits) >= 3 and len(old_hits) > len(new_hits):
        return {"standard": "gb50137", "confidence": "high", "old": old_hits[:8], "new": new_hits[:8]}
    if old_hits and len(old_hits) >= len(new_hits):
        return {"standard": "gb50137", "confidence": "medium", "old": old_hits[:8], "new": new_hits[:8]}
    if new_hits:
        return {"standard": "2023", "confidence": "high", "old": old_hits[:8], "new": new_hits[:8]}
    return {"standard": "", "confidence": "none", "old": old_hits[:8], "new": new_hits[:8]}


def _convert_codes(
    assigned: List[ParcelRecord],
    unclassified: Dict[str, UnclassifiedRow],
    convert_tree: ClassificationTree,
    conversion: ConversionTable,
) -> Tuple[List[ParcelRecord], dict]:
    """旧标准代码 → 新标准代码；换不了的并入待确认，面积不被静默吞掉。"""
    converted: List[ParcelRecord] = []
    failed: Dict[str, Dict] = {}
    converted_area = 0.0
    failed_area = 0.0

    for parcel in assigned:
        old = parcel.code or ""
        new_code, how = conversion.lookup(old)
        node = convert_tree.get(new_code) if new_code else None
        if node is None:
            failed_area += parcel.area_m2
            key = old or "(空)"
            entry = failed.setdefault(
                key,
                {
                    "old": old,
                    "reason": how,
                    "candidates": conversion.ambiguous_candidates(old)[:6],
                    "count": 0,
                    "area_m2": 0.0,
                },
            )
            entry["count"] += 1
            entry["area_m2"] += parcel.area_m2
            label = f"旧代码 {old}（{'歧义' if how == 'ambiguous' else '无对应'}）"
            row = unclassified.get(label)
            if row is None:
                row = UnclassifiedRow(layer=label, count=0, area_m2=0.0)
                unclassified[label] = row
            row.count += 1
            row.area_m2 += parcel.area_m2
            if len(row.handles) < 5:
                row.handles.append(parcel.handle)
            continue
        parcel.origin_code = old
        parcel.code = node.code
        parcel.name = node.name
        parcel.parent_code = node.parent
        if parcel.hybrid_parts:
            converted_parts: List[str] = []
            for part in parcel.hybrid_parts:
                new_part, _ = conversion.lookup(part)
                converted_parts.append(new_part or part)
            parcel.hybrid_parts = converted_parts
            if converted_parts:
                parcel.hybrid_used = converted_parts[0]
        converted_area += parcel.area_m2
        converted.append(parcel)

    info = {
        "from_standard": conversion.from_standard,
        "to_standard": convert_tree.standard,
        "source": conversion.source,
        "converted_count": len(converted),
        "converted_area_m2": round(converted_area, 2),
        "failed_count": sum(item["count"] for item in failed.values()),
        "failed_area_m2": round(failed_area, 2),
        "failed": [
            {**item, "area_m2": round(item["area_m2"], 2)}
            for item in sorted(failed.values(), key=lambda x: x["area_m2"], reverse=True)
        ],
    }
    return converted, info


def build_summary(
    parcels: Sequence[ParcelRecord],
    tree: ClassificationTree,
    mapper: LayerMapper,
    meta: RunMeta,
    plan_area_m2: Optional[float] = None,
    population: Optional[float] = None,
    include_codes: Optional[Sequence[str]] = None,
    exclude_codes: Optional[Sequence[str]] = None,
    issues: Optional[list] = None,
    entity_count: int = 0,
    derive_road: str = "auto",
    road_tolerance: float = 1.0,
    road_max_share: float = 40.0,
    convert_tree: Optional[ClassificationTree] = None,
    conversion: Optional[ConversionTable] = None,
) -> Summary:
    assigned, unclassified, unknown_codes = classify_parcels(parcels, tree, mapper)

    conversion_info: Optional[dict] = None
    if convert_tree is not None and conversion is not None:
        assigned, conversion_info = _convert_codes(assigned, unclassified, convert_tree, conversion)
        tree = convert_tree
        meta.notes.append(
            f"分类口径已由旧标准换算为 {convert_tree.standard}：{conversion_info['converted_count']} 个地块换算成功，"
            f"{conversion_info['failed_count']} 个无法换算（已列入待确认）。"
        )

    include_set = {str(c) for c in include_codes} if include_codes else None
    exclude_set = {str(c) for c in exclude_codes} if exclude_codes else set()

    level1_area: Dict[str, float] = {}
    level1_count: Dict[str, int] = {}
    level2_area: Dict[str, float] = {}
    level2_count: Dict[str, int] = {}
    level2_handles: Dict[str, List[str]] = {}
    level3_area: Dict[str, float] = {}
    level3_count: Dict[str, int] = {}
    level3_handles: Dict[str, List[str]] = {}
    hybrid: Dict[str, HybridRow] = {}
    excluded_area = 0.0
    names: Dict[str, str] = {}  # 混合用地合成代码 -> 拼接名称
    parents: Dict[str, str] = {}  # 混合用地合成代码 -> 所属上级

    for parcel in assigned:
        code = parcel.code or ""
        parts = parcel.hybrid_parts or []
        display = "+".join(parts) if len(parts) > 1 else ""
        root = tree.root_of(code)
        hit_set = {code, *parts} if display else {code}
        if exclude_set and (hit_set & exclude_set or root in exclude_set):
            excluded_area += parcel.area_m2
            continue
        if include_set and not (hit_set & include_set or root in include_set):
            excluded_area += parcel.area_m2
            continue
        level1_area[root] = level1_area.get(root, 0.0) + parcel.area_m2
        level1_count[root] = level1_count.get(root, 0) + 1
        node = tree.get(code)
        if node is not None and node.level >= 2:
            if display:
                # 混合用地单独成行：层级与父级跟随首段代码，面积独立计量后逐级加和
                lv2_code = display if node.level == 2 else (node.parent or "")
                if lv2_code:
                    level2_area[lv2_code] = level2_area.get(lv2_code, 0.0) + parcel.area_m2
                    level2_count[lv2_code] = level2_count.get(lv2_code, 0) + 1
                    level2_handles.setdefault(lv2_code, []).append(parcel.handle)
                if node.level == 3:
                    level3_area[display] = level3_area.get(display, 0.0) + parcel.area_m2
                    level3_count[display] = level3_count.get(display, 0) + 1
                    level3_handles.setdefault(display, []).append(parcel.handle)
                names[display] = "+".join(
                    (tree.get(part).name if tree.get(part) else part) for part in parts
                )
                parents[display] = node.parent or ""
            else:
                # 三级类地块也要落到其二级类名下，否则二级类明细会漏计
                lv2_code = code if node.level == 2 else (node.parent or "")
                if lv2_code:
                    level2_area[lv2_code] = level2_area.get(lv2_code, 0.0) + parcel.area_m2
                    level2_count[lv2_code] = level2_count.get(lv2_code, 0) + 1
                    level2_handles.setdefault(lv2_code, []).append(parcel.handle)
                if node.level == 3:
                    level3_area[code] = level3_area.get(code, 0.0) + parcel.area_m2
                    level3_count[code] = level3_count.get(code, 0) + 1
                    level3_handles.setdefault(code, []).append(parcel.handle)
        if display:
            key = f"{parcel.hybrid_raw}|{display}"
            row = hybrid.get(key)
            if row is None:
                row = HybridRow(
                    layer=parcel.hybrid_raw,
                    used=code,
                    code=display,
                    name=names[display],
                )
                hybrid[key] = row
            row.count += 1
            row.area_m2 += parcel.area_m2

    unresolved = sum(row.area_m2 for row in unclassified.values())
    pre_total = sum(level1_area.values())
    road_info: Optional[dict] = None
    road_note = ""
    road_status = ""
    if derive_road != "off" and tree.derived_road:
        road_info, road_note, road_status = _derive_road(
            tree=tree,
            level1_area=level1_area,
            level2_area=level2_area,
            plan_area_m2=plan_area_m2,
            unresolved_area=unresolved,
            tolerance=road_tolerance,
            max_share=road_max_share,
        )
        if road_note:
            meta.notes.append(road_note)

    total_area = sum(level1_area.values())
    ratio_base = plan_area_m2 if plan_area_m2 else total_area

    summary = Summary(meta=meta, total_area_m2=total_area, ratio_base_m2=ratio_base, entity_count=entity_count)
    summary.pre_derived_total_m2 = pre_total
    summary.road_derivation = road_info
    summary.conversion = conversion_info
    summary.parcel_count = len(parcels)
    summary.issues = list(issues or [])
    summary.unclassified = sorted(unclassified.values(), key=lambda r: r.area_m2, reverse=True)
    summary.unknown_codes = dict(unknown_codes)
    summary.hybrid_unresolved = [
        {"layer": parcel.hybrid_raw, "segments": list(parcel.hybrid_unresolved)}
        for parcel in assigned
        if parcel.hybrid_unresolved
    ]
    summary.hybrid_rows = sorted(hybrid.values(), key=lambda r: r.area_m2, reverse=True)

    def _row(code: str, level: int, area: float, count: int, parent: Optional[str], handles: List[str],
             derived: bool = False) -> SummaryRow:
        node = tree.get(code)
        return SummaryRow(
            code=code,
            name=names.get(code) or (node.name if node else code),
            level=level,
            parent=parent,
            area_m2=area,
            count=count,
            ratio=area / ratio_base * 100.0 if ratio_base else 0.0,
            per_capita=area / population if population else 0.0,
            handles=handles[:5],
            derived=derived,
        )

    for root_code in sorted(level1_area, key=lambda c: tree.order.get(c, 999)):
        if level1_area[root_code] <= 0:
            continue
        root_derived = bool(
            road_info
            and root_code == road_info.get("root")
            and level1_count.get(root_code, 0) == 0
        )
        summary.level1_rows.append(
            _row(root_code, 1, level1_area[root_code], level1_count.get(root_code, 0), None, [],
                 derived=root_derived)
        )

    def _sort_key(code: str) -> tuple:
        """混合用地合成代码按首段代码定位，紧跟其后再按是否混合排序。"""
        first = code.split("+")[0]
        return (
            tree.order.get(tree.parent_of(first) or "", 999),
            tree.order.get(first, 999),
            1 if "+" in code else 0,
            code,
        )

    for code in sorted(level2_area, key=_sort_key):
        if level2_area[code] <= 0:
            continue
        node = tree.get(code)
        is_derived = bool(road_info and code == road_info.get("code"))
        summary.level2_rows.append(
            _row(
                code,
                2,
                level2_area[code],
                level2_count.get(code, 0),
                parents.get(code) or (node.parent if node else None),
                level2_handles.get(code, []),
                derived=is_derived,
            )
        )

    for code in sorted(level3_area, key=_sort_key):
        if level3_area[code] <= 0:
            continue
        node = tree.get(code)
        summary.level3_rows.append(
            _row(code, 3, level3_area[code], level3_count.get(code, 0),
                 parents.get(code) or (node.parent if node else None),
                 level3_handles.get(code, []))
        )

    summary.checks = build_checks(
        summary,
        plan_area_m2=plan_area_m2,
        unresolved_area=unresolved,
        excluded_area=excluded_area,
        unknown_codes=unknown_codes,
        population=population,
        road_info=road_info,
        road_note=road_note,
        road_status=road_status,
        road_tolerance=road_tolerance,
    )
    if unknown_codes:
        meta.notes.append(
            "映射中有代码未收录于当前分类表：" + "、".join(f"{c}(图层示例：{l})" for c, l in unknown_codes.items())
        )
    for node in tree.non_official():
        kind = "非官方扩展节点" if "官方表" in node.source or "非官方" in node.source else "新增类目"
        meta.notes.append(f"分类表中 {node.code} {node.name} 为{kind}：{node.source}")
    return summary


def _derive_road(
    tree: ClassificationTree,
    level1_area: Dict[str, float],
    level2_area: Dict[str, float],
    plan_area_m2: Optional[float],
    unresolved_area: float,
    tolerance: float,
    max_share: float = 40.0,
) -> Tuple[Optional[dict], str, str]:
    """按差额反算道路用地面积，返回 (反算信息, 说明, 状态 ok/blocked/skipped)。

    口径：12 总量 = 范围面积 − 其他一级类 − 未识别；目标二级类 = 12 总量 − 12 下其他二级类。
    道路本来就不填充，所以「规划范围 − 已识别」的差额正是道路面积，不能拿它当阻断条件；
    真正的阻断条件是有未识别图层（未识别占规划范围超过容差）——那部分差额归属不明。
    """
    spec = tree.derived_road or {}
    root = str(spec.get("root") or "")
    code = str(spec.get("code") or "")
    if not root or not code or tree.get(code) is None or tree.get(root) is None:
        return None, "分类配置里的道路反算目标无效，已跳过反算。", "blocked"
    target_name = tree.get(code).name

    if not plan_area_m2:
        return None, "未提供规划范围总面积（且未识别到规划范围），本次未做道路反算。", "skipped"

    if not any(area > 0 for area in level1_area.values()):
        return (
            None,
            "未识别到任何已分类的填充地块，无法反算道路用地；请先确认统计口径（--primitive / --layers）与图层映射。",
            "blocked",
        )

    unresolved_ratio = unresolved_area / plan_area_m2 * 100.0
    if unresolved_ratio > tolerance:
        return (
            None,
            f"未识别图层面积 {unresolved_area / 1e4:.4f} ha，占规划范围 {unresolved_ratio:.2f}%，"
            f"超过容差 {tolerance:g}%；差额归属不明，已阻断 {code} {target_name} 反算。"
            "请先补齐图层映射（或把这些图层 --exclude-layers 排除）后重跑。",
            "blocked",
        )

    others = sum(area for key, area in level1_area.items() if key != root)
    root_total = plan_area_m2 - others - unresolved_area
    if root_total <= 0:
        return None, f"反算得到的 {root} 用地总量为负，已阻断；请复核其他一级类面积是否超出规划范围。", "blocked"

    other_level2 = sum(area for key, area in level2_area.items() if tree.root_of(key) == root and key != code)
    code_total = root_total - other_level2
    clamped = False
    if code_total < 0:
        code_total = 0.0
        clamped = True

    level1_area[root] = root_total
    level2_area[code] = code_total

    resolved = plan_area_m2 - unresolved_area - root_total
    share = code_total / plan_area_m2 * 100.0 if plan_area_m2 else 0.0
    info = {
        "root": root,
        "root_name": tree.get(root).name,
        "code": code,
        "code_name": target_name,
        "root_area_m2": round(root_total, 2),
        "code_area_m2": round(code_total, 2),
        "plan_area_m2": round(plan_area_m2, 2),
        "resolved_other_m2": round(resolved, 2),
        "other_roots_m2": round(others, 2),
        "unresolved_m2": round(unresolved_area, 2),
        "other_level2_m2": round(other_level2, 2),
        "share_pct": round(share, 2),
        "max_share_pct": round(max_share, 2),
        "share_warn": share > max_share,
        "formula": (
            f"{root} = 范围 {plan_area_m2 / 1e4:.4f} − 其他一级类 {others / 1e4:.4f} − 未识别 {unresolved_area / 1e4:.4f} = "
            f"{root_total / 1e4:.4f} ha；{code} = {root_total / 1e4:.4f} − {root} 下其他二级类 {other_level2 / 1e4:.4f} = "
            f"{code_total / 1e4:.4f} ha（占规划范围 {share:.2f}%）"
        ),
        "clamped": clamped,
    }
    note = f"{code} {target_name} 为差额反算值（非图面实测）：{info['formula']}。"
    if clamped:
        note += " 反算结果为负，已按 0 计，需人工复核。"
    if info["share_warn"]:
        note += f" 反算占比超过 {max_share:g}% 的经验上限，请复核是否有整类用地未填充或漏层。"
    return info, note, "ok"


def build_checks(
    summary: Summary,
    plan_area_m2: Optional[float],
    unresolved_area: float,
    excluded_area: float,
    unknown_codes: Dict[str, str],
    population: Optional[float],
    road_info: Optional[dict] = None,
    road_note: str = "",
    road_status: str = "",
    road_tolerance: float = 1.0,
) -> List[CheckItem]:
    checks: List[CheckItem] = []
    total = summary.total_area_m2
    pre_total = summary.pre_derived_total_m2 or total

    if plan_area_m2:
        gap = plan_area_m2 - pre_total - unresolved_area
        gap_ratio = gap / plan_area_m2 * 100.0 if plan_area_m2 else 0.0
        if road_info:
            checks.append(
                CheckItem(
                    label="分类合计与规划范围",
                    ok=True,
                    message=(
                        f"已识别 {pre_total / 1e4:.4f} ha + 未识别 {unresolved_area / 1e4:.4f} ha，"
                        f"规划范围 {plan_area_m2 / 1e4:.4f} ha；差额 {gap / 1e4:+.4f} ha（{gap_ratio:.2f}%）"
                        f"为不填充的道路，已反算归入 {road_info.get('root')} 类"
                    ),
                )
            )
        else:
            ratio = abs(gap) / plan_area_m2 * 100.0
            checks.append(
                CheckItem(
                    label="分类合计与规划范围",
                    ok=ratio <= road_tolerance,
                    message=(
                        f"已识别 {pre_total / 1e4:.4f} ha + 未识别 {unresolved_area / 1e4:.4f} ha，"
                        f"规划范围 {plan_area_m2 / 1e4:.4f} ha，差额 {gap / 1e4:+.4f} ha（{ratio:.2f}%），未做道路反算"
                    ),
                )
            )
    else:
        checks.append(
            CheckItem(
                label="分类合计",
                ok=True,
                message=f"合计 {total / 1e4:.4f} ha（未提供规划范围总面积，跳过差额校验）",
            )
        )

    if road_info:
        warn = []
        if road_info.get("clamped"):
            warn.append("结果为负，已按 0 计")
        if road_info.get("share_warn"):
            warn.append(f"占比超过 {road_info.get('max_share_pct', 40):g}% 经验上限")
        checks.append(
            CheckItem(
                label="道路反算",
                ok=not warn,
                message=road_info.get("formula", "") + ("；需复核：" + "、".join(warn) if warn else ""),
            )
        )
    elif road_status == "blocked" and road_note:
        checks.append(CheckItem(label="道路反算", ok=False, message=road_note))
    elif road_note:
        checks.append(CheckItem(label="道路反算（未执行）", ok=True, message=road_note))

    covered = total + unresolved_area
    unresolved_ratio = unresolved_area / covered * 100.0 if covered else 0.0
    checks.append(
        CheckItem(
            label="未识别图层面积占比",
            ok=unresolved_ratio <= 5.0,
            message=f"未识别 {unresolved_area / 1e4:.4f} ha，占已识别与未识别合计的 {unresolved_ratio:.2f}%",
        )
    )

    issue_kinds = {}
    for issue in summary.issues:
        issue_kinds[issue.kind] = issue_kinds.get(issue.kind, 0) + 1
    abnormal = sum(count for kind, count in issue_kinds.items() if kind in {"unclosed", "self_intersect", "zero_area"})
    checks.append(
        CheckItem(
            label="异常图元",
            ok=abnormal == 0,
            message=(
                "、".join(f"{kind} {count} 个" for kind, count in sorted(issue_kinds.items())) or "无异常图元"
            ),
        )
    )

    if excluded_area > 0:
        checks.append(
            CheckItem(
                label="被过滤类目",
                ok=True,
                message=f"按 --include-codes/--exclude-codes 过滤掉 {excluded_area / 1e4:.4f} ha，未计入本表",
            )
        )

    if population:
        checks.append(
            CheckItem(
                label="人均用地折算",
                ok=True,
                message=f"按人口 {population:,.0f} 人折算，人均建设用地 {total / population:.2f} m²/人",
            )
        )
    else:
        checks.append(CheckItem(label="人均用地折算", ok=True, message="未提供 --population，人均列留空"))

    return checks
