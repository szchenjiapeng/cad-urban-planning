"""建筑高度分级：低多层 / 高层，用于「两条退线」。

判定顺序（按可靠性）：
1. 用户显式 `--high-rise-layers` / `--low-rise-layers`（最可靠）；
2. 图层名关键词（JZ-高层 / 低多层住宅 / 洋房 …）；
3. 都没有 → 取 `default_class`（默认 high，即按严控制，避免漏判）。

阈值（可配置 `high_rise`）：高度 > 24m 或 层数 ≥ 7 视为高层。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

HIGH_KEYWORDS = ["高层", "超高层", "高层住宅", "高层建筑", "gaoceng", "highrise", "high-rise", "high rise", "tower"]
LOW_KEYWORDS = ["低层", "多层", "低多层", "多层住宅", "低层住宅", "洋房", "diceng", "duoceng", "lowrise", "low-rise", "midrise"]

CLASS_LABELS = {"low_mid": "低多层", "high": "高层"}


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).strip().lower()


def class_from_layer(layer: str) -> str:
    name = _norm(layer)
    if not name:
        return ""
    for keyword in HIGH_KEYWORDS:
        key = _norm(keyword)
        if key and key in name:
            return "high"
    for keyword in LOW_KEYWORDS:
        key = _norm(keyword)
        if key and key in name:
            return "low_mid"
    return ""


def resolve_thresholds(rules: Optional[Dict]) -> Dict[str, float]:
    config = (rules or {}).get("high_rise") or {}
    try:
        max_height = float(config.get("max_height", 24.0))
    except Exception:
        max_height = 24.0
    try:
        max_floors = float(config.get("max_floors", 6))
    except Exception:
        max_floors = 6.0
    return {"max_height": max_height, "max_floors": max_floors}


def class_from_metrics(height_m: Optional[float], floors: Optional[float], rules: Optional[Dict]) -> str:
    """按高度/层数判定；两者都没有返回空串。"""
    limits = resolve_thresholds(rules)
    if height_m is not None:
        return "high" if float(height_m) > limits["max_height"] else "low_mid"
    if floors is not None:
        return "high" if float(floors) >= limits["max_floors"] + 1 else "low_mid"
    return ""


def assign(
    buildings: Sequence,
    explicit_high_layers: Optional[Iterable[str]] = None,
    explicit_low_layers: Optional[Iterable[str]] = None,
    default_class: str = "high",
) -> Dict[str, str]:
    """给每个建筑分配等级：{handle: low_mid|high}。未知时取 default_class。"""
    high_layers = {_norm(item) for item in (explicit_high_layers or []) if str(item).strip()}
    low_layers = {_norm(item) for item in (explicit_low_layers or []) if str(item).strip()}
    result: Dict[str, str] = {}
    for building in buildings:
        layer = _norm(getattr(building, "layer", ""))
        kind = ""
        if layer and layer in high_layers:
            kind = "high"
        elif layer and layer in low_layers:
            kind = "low_mid"
        else:
            for candidate in high_layers:
                if candidate and candidate in layer:
                    kind = "high"
                    break
            if not kind:
                for candidate in low_layers:
                    if candidate and candidate in layer:
                        kind = "low_mid"
                        break
        if not kind:
            kind = class_from_layer(getattr(building, "layer", ""))
        if not kind:
            kind = default_class if default_class in CLASS_LABELS else "high"
        result[str(getattr(building, "handle", ""))] = kind
    return result


def summarize(classes: Dict[str, str]) -> Dict[str, int]:
    counts = {"low_mid": 0, "high": 0}
    for kind in classes.values():
        if kind in counts:
            counts[kind] += 1
    return counts


# ---------------------------------------------------------------- 建筑高度提取
# 用户口径：高度文本优先（54m / H=54 / 54米），其次楼层（5F / 7F / 6层 / 砼7 / 砼15 / 砖6 / 框4），
# 楼层 × 3m 换算；都抓不到返回 None，上层按「低多层 + 高层两条线都出」处理。
# 纯数字（54.00）不采集，避免标高、面积、编号被当成高度。
FLOOR_HEIGHT_DEFAULT = 3.0
MIN_HEIGHT_M = 1.0
MAX_HEIGHT_M = 500.0
MAX_FLOORS = 99

_HEIGHT_PATTERNS = (
    re.compile(r"(?:H|h|高度)\s*[=:：]\s*(\d+(?:\.\d+)?)"),
    re.compile(r"(\d+(?:\.\d+)?)\s*(?:m|M|米)(?![mM])"),
)
# 结构前缀 + 层数：砼7 / 砼15 / 砖6 / 砖混5 / 混5 / 钢3 / 框架4 / 底框6 / 木2
# 后面紧跟 m/米 的不算层数（那是高度）
_STRUCT_PREFIX = r"(?:砼|混凝土|砖混|砖|砌体|混|钢|框架|底框|框|木)"
_FLOOR_PATTERNS = (
    re.compile(_STRUCT_PREFIX + r"\s*(\d+)(?!\d)\s*(?![mM](?![mM])|米)"),
    re.compile(r"(\d+)\s*(?:F|f|层)(?![A-Za-z0-9])"),
)


def parse_height_text(text: str, floor_height: float = FLOOR_HEIGHT_DEFAULT) -> Tuple[Optional[float], str]:
    """从一段文字取建筑高度：返回 (高度m, 依据)；取不到返回 (None, '')。"""
    raw = str(text or "").strip()
    if not raw:
        return None, ""
    best: Optional[Tuple[float, str]] = None
    for pattern in _HEIGHT_PATTERNS:
        for match in pattern.finditer(raw):
            try:
                value = float(match.group(1))
            except Exception:
                continue
            if MIN_HEIGHT_M <= value <= MAX_HEIGHT_M and (best is None or value > best[0]):
                best = (value, f"高度文本「{match.group(0)}」")
    for pattern in _FLOOR_PATTERNS:
        for match in pattern.finditer(raw):
            try:
                floors = int(match.group(1))
            except Exception:
                continue
            if 1 <= floors <= MAX_FLOORS:
                value = floors * float(floor_height)
                if best is None or value > best[0]:
                    best = (value, f"楼层「{match.group(0)}」×{floor_height:g}m")
    return best if best else (None, "")


def height_from_layer(layer: str, floor_height: float = FLOOR_HEIGHT_DEFAULT) -> Tuple[Optional[float], str]:
    """图层名里写层数的情况（如 `JZ-18F`、`现状-砼6`）。"""
    value, source = parse_height_text(layer, floor_height)
    if value is None:
        return None, ""
    return value, f"图层名{source}"


def _bounds_hit(bounds, x: float, y: float, pad: float) -> bool:
    return bounds[0] - pad <= x <= bounds[2] + pad and bounds[1] - pad <= y <= bounds[3] + pad


def attach_heights(
    buildings: Sequence,
    texts: Sequence,
    floor_height: float = FLOOR_HEIGHT_DEFAULT,
    tolerance: float = 0.5,
    layer_floor_height: bool = True,
) -> Dict[str, Dict]:
    """给建筑挂高度：{handle: {"height": m, "source": 依据}}。

    文字点落在建筑轮廓（外扩容差）内即归该建筑，同栋取最大值；
    图层名带层数（JZ-18F）先铺底，文字有更高值再覆盖。
    """
    result: Dict[str, Dict] = {}
    if not buildings:
        return result
    try:
        from shapely.geometry import Point
    except Exception:  # pragma: no cover
        Point = None

    if layer_floor_height:
        for building in buildings:
            value, source = height_from_layer(getattr(building, "layer", ""), floor_height)
            if value is not None:
                result[str(building.handle)] = {"height": value, "source": source}

    for text in texts or []:
        value, source = parse_height_text(getattr(text, "text", ""), floor_height)
        if value is None:
            continue
        point = getattr(text, "point", None)
        if not point:
            continue
        x, y = float(point[0]), float(point[1])
        best_handle = ""
        best_area: Optional[float] = None
        for building in buildings:
            geometry = getattr(building, "geometry", None)
            if geometry is None or getattr(geometry, "is_empty", True):
                continue
            if not _bounds_hit(geometry.bounds, x, y, tolerance):
                continue
            if Point is not None:
                try:
                    if not geometry.buffer(float(tolerance)).contains(Point(x, y)):
                        continue
                except Exception:
                    continue
            area = float(getattr(geometry, "area", 0.0) or 0.0)
            if best_area is None or area < best_area:
                best_area = area
                best_handle = str(building.handle)
        if not best_handle:
            continue
        current = result.get(best_handle)
        if current is None or value > float(current.get("height", 0.0)):
            result[best_handle] = {"height": value, "source": source}
    return result


def parcel_height(parcel_geometry, buildings: Sequence, heights: Dict[str, Dict], tolerance: float = 0.1) -> Optional[Dict]:
    """地块内最高建筑：{"height","source","handle","count"}；没有高度信息返回 None。

    建筑归属按「代表点落在该地块内」判定，避免相邻地块建筑串算。
    """
    if parcel_geometry is None or not heights:
        return None
    try:
        zone = parcel_geometry.buffer(float(tolerance))
    except Exception:
        zone = parcel_geometry
    found: List[Dict] = []
    for building in buildings:
        info = heights.get(str(getattr(building, "handle", "")))
        if not info:
            continue
        geometry = getattr(building, "geometry", None)
        if geometry is None or getattr(geometry, "is_empty", True):
            continue
        try:
            if not zone.contains(geometry.representative_point()):
                continue
        except Exception:
            continue
        found.append(
            {
                "height": float(info.get("height", 0.0)),
                "source": str(info.get("source", "")),
                "handle": str(getattr(building, "handle", "")),
            }
        )
    if not found:
        return None
    found.sort(key=lambda item: item["height"], reverse=True)
    top = dict(found[0])
    top["count"] = len(found)
    return top


def class_from_height(height_m: Optional[float], rules: Optional[Dict]) -> str:
    """按高度分级：> max_height（默认 24m）为高层，否则低多层；无高度返回空串。"""
    if height_m is None:
        return ""
    config = (rules or {}).get("high_rise") or {}
    try:
        limit = float(config.get("max_height", 24.0))
    except Exception:
        limit = 24.0
    return "high" if float(height_m) > limit else "low_mid"


# ---------------------------------------------------------------- 层数提取（子技能 N5）
# 与上面的「高度」口径分开：测算与建模要的是**层数原值**，不是高度。
# 优先顺序：层数文本（砼7 / 7F / 7层 / 砼单独=1 层）→ 高度文本（H=54 / 54m）÷ 层高 → 无信息返回 None。
# 注意：`parse_height_text` 的「抓不到就返回 None」语义保持不变，退线仍按原口径执行。
DEFAULT_FLOORS = 1
MAX_FLOORS = 99

_FLOOR_SUFFIX_PATTERNS = (
    re.compile(r"(\d{1,2})\s*(?:F|f|层)(?![A-Za-z0-9])"),
)
_HEIGHT_ONLY_PATTERNS = (
    re.compile(r"(?:H|h|高度)\s*[=:：]\s*(\d+(?:\.\d+)?)"),
    re.compile(r"(\d+(?:\.\d+)?)\s*(?:m|M|米)(?![mM])"),
)
_BARE_NUMBER = re.compile(r"^\s*(\d{1,2})\s*$")


def _scan_struct_floors(raw: str) -> List[Tuple[int, str]]:
    """结构前缀层数：砼7 / 砖混5 / 框架4；`砼` 后面没有数字时按 1 层。

    后面紧跟 m/米 的不算层数（那是高度），与 `parse_height_text` 保持同一判据。
    """
    found: List[Tuple[int, str]] = []
    for match in re.finditer(_STRUCT_PREFIX, raw):
        tail = raw[match.end() :]
        if re.match(r"\s*[mM米]", tail):
            continue
        number = re.match(r"\s*(\d{1,2})(?!\d)", tail)
        if number:
            value = int(number.group(1))
            if 1 <= value <= MAX_FLOORS:
                found.append((value, f"结构层数「{match.group(0)}{number.group(1)}」"))
            continue
        if re.match(r"\s*(?:[^\dA-Za-z]|$)", tail):
            found.append((1, f"结构前缀「{match.group(0)}」未写层数，按 1 层"))
    return found


def parse_floor_info(
    text: str,
    floor_height: float = FLOOR_HEIGHT_DEFAULT,
    bare_number: bool = False,
) -> Dict:
    """从一段文字取建筑层数。

    返回 {"floors": int|None, "height_m": float|None, "source": str, "kind": str, "conflicts": [str]}；
    kind 取值：floor（层数文本）/ struct（结构前缀）/ bare（裸数字，需显式开启）/
    height（只有高度，按层高折算）/ none（无信息）。

    层数与高度同时出现时**以层数为准**：层高是用户指定的，高度只作交叉校验。
    """
    raw = str(text or "").strip()
    empty = {"floors": None, "height_m": None, "source": "", "kind": "none", "conflicts": []}
    if not raw:
        return empty

    floor_hits: List[Tuple[int, str, str]] = []
    for pattern in _FLOOR_SUFFIX_PATTERNS:
        for match in pattern.finditer(raw):
            try:
                value = int(match.group(1))
            except Exception:
                continue
            if 1 <= value <= MAX_FLOORS:
                floor_hits.append((value, f"层数「{match.group(0)}」", "floor"))
    for value, source in _scan_struct_floors(raw):
        floor_hits.append((value, source, "struct"))
    if bare_number:
        match = _BARE_NUMBER.match(raw)
        if match:
            value = int(match.group(1))
            if 1 <= value <= MAX_FLOORS:
                floor_hits.append((value, f"裸数字「{raw}」（低置信）", "bare"))

    height_value: Optional[float] = None
    height_source = ""
    for pattern in _HEIGHT_ONLY_PATTERNS:
        for match in pattern.finditer(raw):
            try:
                value = float(match.group(1))
            except Exception:
                continue
            if MIN_HEIGHT_M <= value <= MAX_HEIGHT_M and (height_value is None or value > height_value):
                height_value = value
                height_source = f"高度「{match.group(0)}」"

    if floor_hits:
        counts: Dict[int, int] = {}
        for value, _source, _kind in floor_hits:
            counts[value] = counts.get(value, 0) + 1
        best = max(counts.items(), key=lambda pair: (pair[1], pair[0]))[0]
        source = next(item[1] for item in floor_hits if item[0] == best)
        kind = next(item[2] for item in floor_hits if item[0] == best)
        conflicts: List[str] = []
        for value, _src, _k in floor_hits:
            if value != best:
                conflicts.append(f"同段文字出现 {value} 层与 {best} 层")
        if height_value is not None:
            implied = float(height_value) / max(float(floor_height), 1e-6)
            if implied > 0 and abs(implied - best) / best > 0.2:
                conflicts.append(
                    f"{height_source}按 {floor_height:g}m/层折算约 {implied:.1f} 层，与层数 {best} 层相差 >20%"
                )
        return {
            "floors": best,
            "height_m": height_value,
            "source": source,
            "kind": kind,
            "conflicts": conflicts,
        }

    if height_value is not None:
        floors = max(1, int(round(float(height_value) / max(float(floor_height), 1e-6))))
        return {
            "floors": min(floors, MAX_FLOORS),
            "height_m": height_value,
            "source": f"{height_source}÷{floor_height:g}m/层",
            "kind": "height",
            "conflicts": [],
        }
    return empty


def attach_floors(
    buildings: Sequence,
    texts: Sequence,
    floor_height: float = FLOOR_HEIGHT_DEFAULT,
    tolerance: float = 0.5,
    bare_number: bool = False,
    layer_floors: bool = True,
) -> Dict[str, Dict]:
    """给建筑挂层数：{handle: {"floors","source","kind","height_m","conflicts"}}。

    与 `attach_heights` 同一套归属判据（文字点落在轮廓内、同栋取最小面积包含面），
    区别是同栋多个文字取**众数**（并列取大）并记录冲突，而不是简单取最大值。
    没有抓到的建筑不出现在返回值里，由上层按 DEFAULT_FLOORS 处理并单列清单。
    """
    result: Dict[str, Dict] = {}
    if not buildings:
        return result
    try:
        from shapely.geometry import Point
    except Exception:  # pragma: no cover
        Point = None

    if layer_floors:
        for building in buildings:
            info = parse_floor_info(getattr(building, "layer", ""), floor_height, bare_number=False)
            if info["floors"] is not None:
                info = dict(info)
                info["source"] = f"图层名{info['source']}"
                result[str(building.handle)] = info

    hits: Dict[str, List[Dict]] = {}
    for text in texts or []:
        info = parse_floor_info(getattr(text, "text", ""), floor_height, bare_number=bare_number)
        if info["floors"] is None:
            continue
        point = getattr(text, "point", None)
        if not point:
            continue
        x, y = float(point[0]), float(point[1])
        best_handle = ""
        best_area: Optional[float] = None
        for building in buildings:
            geometry = getattr(building, "geometry", None)
            if geometry is None or getattr(geometry, "is_empty", True):
                continue
            if not _bounds_hit(geometry.bounds, x, y, tolerance):
                continue
            if Point is not None:
                try:
                    if not geometry.buffer(float(tolerance)).contains(Point(x, y)):
                        continue
                except Exception:
                    continue
            area = float(getattr(geometry, "area", 0.0) or 0.0)
            if best_area is None or area < best_area:
                best_area = area
                best_handle = str(building.handle)
        if not best_handle:
            continue
        hits.setdefault(best_handle, []).append(info)

    for handle, items in hits.items():
        counts: Dict[int, int] = {}
        for item in items:
            value = int(item["floors"])
            counts[value] = counts.get(value, 0) + 1
        best = max(counts.items(), key=lambda pair: (pair[1], pair[0]))[0]
        chosen = next(item for item in items if int(item["floors"]) == best)
        conflicts: List[str] = []
        for item in items:
            if int(item["floors"]) != best:
                conflicts.append(f"{item['source']} 与 {chosen['source']} 层数不一致")
            for note in item.get("conflicts") or []:
                if note not in conflicts:
                    conflicts.append(note)
        merged = dict(chosen)
        merged["floors"] = best
        merged["conflicts"] = conflicts
        result[handle] = merged
    return result


def layer_floor_height_for(code: Optional[str], rules: Optional[Dict]) -> Tuple[float, str]:
    """按用地代码取层高：住宅 3.0m、已识别非住宅 4.5m、未识别 3.0m（默认值可配置）。"""
    config = (rules or {}).get("floor_height") or {}
    try:
        residential = float(config.get("residential", 3.0))
    except Exception:
        residential = 3.0
    try:
        non_residential = float(config.get("non_residential", 4.5))
    except Exception:
        non_residential = 4.5
    try:
        unknown = float(config.get("unknown", 3.0))
    except Exception:
        unknown = 3.0
    text = str(code or "").strip().upper()
    if not text:
        return unknown, "用地代码未识别，按默认层高"
    residential_prefixes = [str(item) for item in (config.get("residential_codes") or ["07", "R"])]
    for prefix in residential_prefixes:
        if prefix and text.startswith(prefix.upper()):
            return residential, f"住宅用地（{text}）"
    return non_residential, f"非住宅用地（{text}）"


def height_layer_candidates(layers: Sequence[str], spec: Optional[Dict]) -> List[Tuple[str, int]]:
    """按命名为高度/层数标注图层打分，只用于排序候选，最终必须由用户确认。

    高度标注常单独放在 `BZ-建筑高度`、`建筑层数`、`标高` 这类图层；
    指定后只扫这些图层，可显著缩短扫描时间。
    """
    config = (spec or {}).get("height_layers") or {}
    strong = [str(item).lower() for item in (config.get("strong") or []) if str(item).strip()]
    weak = [str(item).lower() for item in (config.get("keywords") or []) if str(item).strip()]
    blocked = [str(item).lower() for item in (config.get("exclude") or []) if str(item).strip()]
    found: List[Tuple[str, int]] = []
    for layer in layers or []:
        name = str(layer or "").lower()
        if not name:
            continue
        if any(key and key in name for key in blocked):
            continue
        score = 0
        for key in strong:
            if key and key in name:
                score += 10
        for key in weak:
            if key and key in name:
                score += 3
        if score:
            found.append((str(layer), score))
    found.sort(key=lambda item: (-item[1], item[0]))
    return found
