#!/usr/bin/env python
"""子技能 N3：建筑退线。

流程：确认/指派线条图层 → 分级建筑（低多层 / 高层）→ 取退线基准（用地红线 / 道路红线）
→ 按规则定退距（临绿地水系归 5m）→ 求两级可建范围 → 写回退线图层 → 校验压线 → 出清单。

硬性：
- 退线是合规相关成果，缺 shapely 必须报错退出，不得降级成近似算法；
- 线条类图层（范围线/用地红线/地块线/道路红线/中心线）没有字段映射表可依，
  只出候选与打分，**必须由用户确认**（--layer-map / --confirm-layers），不得自动采纳；
- 道路等级**不用宽度推断**（宽度与等级无必然对应关系），宽度仅写进报告供复核；
- 用地红线一侧只要直接相邻绿地/水系（01/02/03/04/14/17），该边段退 5m；
- 退线基准必须是闭合面；只有开放线时用 polygonize 缝合成面，缝不出来就确认；
- 不覆盖原图，DXF 通道只写 `*_setback.dxf` 副本。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import (  # noqa: E402
    building_class,
    building_massing,
    cli,
    confirm,
    landuse,
    line_layers,
    offset,
    parcel_sides,
    ring_clean,
    road_level,
    shapes,
)
from lib import layer_spec as spec_mod  # noqa: E402
from lib.report import locator_note, md_table, write_markdown  # noqa: E402

DEFAULT_RULES = _SCRIPTS_DIR / "assets" / "setback_rules.json"
GREEN_WATER_DEFAULT = "01,02,03,04,14,17"

# 三种退线基准的口径差异：**必须先讲清再让用户选**，不能只丢三个词。
BASIS_OPTIONS = [
    {
        "value": "parcel",
        "id": "parcel",
        "args": ["--basis", "parcel"],
        "label": "只退用地红线（parcel）",
        "desc": (
            "按每块地自身边界向内缩：低多层 5m / 高层 10m。反映地块自身的退让，"
            "不管界线外面是道路、绿地还是其他地块。适用：地块内可建范围、拿地强排、出让条件核算。"
        ),
    },
    {
        "value": "road",
        "id": "road",
        "args": ["--basis", "road"],
        "label": "只退道路红线（road）",
        "desc": (
            "从可建范围里扣掉沿道路红线的退让带，退距按道路等级：快速 20m / 主干 15m / 次干 10m / 支路 5m。"
            "反映临路界面的退让，同一条路两侧地块退距一致。适用：道路两侧界面与街墙控制。"
        ),
    },
    {
        "value": "both",
        "id": "both",
        "args": ["--basis", "both"],
        "label": "两者叠加（both，最严）",
        "desc": (
            "先按用地红线内缩，再扣掉临路退让带，取交集。同时满足自身退让与临路退让，"
            "适用：出具正式建筑控制线成果、报建审查。"
        ),
    },
]


@dataclass
class ParcelSetback:
    parcel: shapes.ShapeRec
    buildable: object = None
    buildable_low: object = None
    buildable_high: object = None
    apply: List[Dict] = field(default_factory=list)
    sides: List[Dict] = field(default_factory=list)
    height_mode: str = "both"
    target_class: str = ""
    height_info: Dict = field(default_factory=dict)
    area_parcel_m2: float = 0.0
    geometry_factor: float = 1.0
    area_buildable_m2: float = 0.0
    area_buildable_low_m2: float = 0.0
    area_buildable_high_m2: float = 0.0
    issues: List[str] = field(default_factory=list)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="建筑退线（子技能 N3）")
    cli.add_source_args(parser)
    cli.add_layer_args(parser)
    cli.add_output_args(parser)
    parser.add_argument("--building-layers", help="建筑图层，逗号分隔；缺省按 layer_spec 建筑组识别")
    parser.add_argument("--parcel-layers", help="用地红线/地块线图层，逗号分隔；缺省按 line_groups 候选识别")
    parser.add_argument("--road-layers", help="道路红线图层，逗号分隔；缺省按 line_groups 候选识别")
    parser.add_argument("--layer-map", help="图层指派表 JSON（由 --confirm-layers 生成的确认结果），用于复用")
    parser.add_argument("--confirm-layers", action="store_true", help="只输出线条图层指派表（退出码 3），不产出退线成果")
    parser.add_argument(
        "--basis",
        default=None,
        choices=["parcel", "road", "both"],
        help=(
            "退线基准：parcel=只退用地红线（低多层5m/高层10m）；road=只退道路红线（按等级20/15/10/5）；"
            "both=两者叠加取交集。**不填则输出 setback_basis 确认项**，先把三者差别讲给用户再选"
        ),
    )
    parser.add_argument("--rules", default=str(DEFAULT_RULES), help="退线规则 JSON（院内/地方标准请用本参数指定）")
    parser.add_argument(
        "--accept-default-rules",
        action="store_true",
        help="明确同意按内置默认值退距执行；未加本参数且规则文件仍为默认值时，输出 setback_rules_source 确认项",
    )
    parser.add_argument("--rules-note", help="退距依据说明，如「XX市城市规划管理技术规定 表4.2」，写入报告与 JSON")
    parser.add_argument("--default-distance", type=float, help="规则未命中时的退距（米）；缺省取规则文件 default_distance")
    parser.add_argument("--distance-override", help="命令行覆盖，如 '低多层=6,高层=12,临绿地=5,主干路=15'")
    parser.add_argument("--offset-join", default="mitre", choices=["mitre", "round", "bevel"], help="锐角处理方式")
    parser.add_argument("--mitre-limit", type=float, default=5.0, help="mitre 尖角限制")
    parser.add_argument("--out-layer", help="退线图层前缀（兼容旧参数）；缺省取 layer_spec control_lines.退线")
    parser.add_argument("--out-layer-low", help="低多层退线图层，默认 <前缀>-低多层")
    parser.add_argument("--out-layer-high", help="高层退线图层，默认 <前缀>-高层")
    parser.add_argument("--min-area", type=float, default=1.0, help="参与计算的最小面积（㎡）")
    parser.add_argument("--scope-mode", default=None, choices=["none", "auto"], help="规划范围裁剪：none=不裁剪 / auto=按规划范围图层裁掉范围外地块与建筑；不填则进 setback_options 确认项")
    parser.add_argument("--scope-layers", help="规划范围图层，逗号分隔；配合 --scope-mode auto，缺省按「规划范围/设计范围」命名识别")
    parser.add_argument("--tolerance", type=float, default=0.01, help="压线判定容差（占建筑面积比例）")
    parser.add_argument("--min-building-area", type=float, default=10.0, help="建筑最小基底面积（㎡），过滤图例/家具")
    parser.add_argument("--road-levels", help="道路等级指定，如 'RD-解放路=主干路,DL-纬三路=次干路'")
    parser.add_argument("--level-policy", default="default", choices=["default", "ask"], help="道路等级未判定时：default 按默认退距并在报告标注 / ask 输出确认项")
    parser.add_argument("--high-rise-layers", help="高层建筑图层，逗号分隔")
    parser.add_argument("--low-rise-layers", help="低多层建筑图层，逗号分隔")
    parser.add_argument(
        "--height-class",
        default="auto",
        choices=["auto", "low_mid", "high"],
        help="auto=按图纸高度/层数自动判定，抓不到则两条线都出；low_mid/high=强制只出该条线",
    )
    parser.add_argument("--floor-height", type=float, default=3.0, help="楼层换算高度（米/层），默认 3")
    parser.add_argument("--height-tolerance", type=float, default=0.5, help="高度文字归属建筑的容差（米）")
    parser.add_argument("--height-layers", help="高度/层数标注图层，逗号分隔；指定后只扫这些图层（推荐，显著提速）")
    parser.add_argument(
        "--no-height-text",
        dest="height_text",
        action="store_false",
        default=True,
        help="不扫描文字，只用图层名与 --high-rise-layers 判定高度",
    )
    parser.add_argument("--green-landuse", default=GREEN_WATER_DEFAULT, help="视作绿地水系的一级类代码，逗号分隔")
    parser.add_argument("--min-contact-length", type=float, help="临绿地水系的最小接触长度（米），缺省取规则文件")
    parser.add_argument("--side-tolerance", type=float, help="相邻判定容差（米），缺省取规则文件")
    parser.add_argument("--no-report-road-width", dest="report_road_width", action="store_false", default=True, help="报告里不测算红线宽度参考值（宽度不参与等级判定）")
    parser.add_argument("--min-ring-area", type=float, default=None, help="退线环的最小面积（㎡），低于判杂线不写图；不填则进 setback_options 确认项")
    parser.add_argument("--min-ring-width", type=float, default=None, help="退线环的最小短边宽度（m），低于判杂线不写图；不填则进 setback_options 确认项")
    parser.add_argument(
        "--keep-holes",
        default=None,
        choices=["all", "large", "none"],
        help="可建范围内部的内环（洞）：large=面积≥100㎡ 视为真实内院保留 / all=全保留 / none=全删；不填则进确认项",
    )
    parser.add_argument("--keep-hole-area", type=float, default=100.0, help="--keep-holes large 时的内环面积阈值（㎡），默认 100")
    parser.add_argument(
        "--ring-dedupe",
        default=None,
        choices=["on", "off"],
        help="同一输出图层内的重线去重（完全重合/被覆盖）；跨图层重合（两级退线）一律不处理；不填则进确认项",
    )
    parser.add_argument(
        "--coincident-policy",
        default=None,
        choices=["report", "merge", "ignore"],
        help="同图层部分共线重叠边段：report=只列清单不删线 / merge=合并成一条外轮廓（失去分地块属性）/ ignore=不检查；不填则进确认项",
    )
    parser.add_argument(
        "--green-parcels",
        default=None,
        choices=["include", "exclude"],
        help="绿地水系地块是否参与退线：exclude=不设建筑退线 / include=照常出线；不填则进 setback_options 确认项",
    )
    parser.add_argument(
        "--accept-default-options",
        action="store_true",
        help="口径未逐项指定时，一次性接受 recommended 里的建议值（报告会记录「按推荐值执行」）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只出报告不写图")
    return parser.parse_args(argv)


def load_rules(path: str) -> Dict:
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"退线规则不存在：{target}")
    return json.loads(target.read_text(encoding="utf-8"))


def parse_overrides(text: Optional[str]) -> Dict[str, float]:
    result: Dict[str, float] = {}
    for item in cli.parse_list(text):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        try:
            result[key.strip()] = float(value.strip())
        except ValueError:
            continue
    return result


def _override(overrides: Dict[str, float], *keys: str) -> Optional[float]:
    for key in keys:
        if key in overrides:
            return overrides[key]
    return None


def landuse_tokens(info: Optional[landuse.LayerInfo]) -> List[str]:
    tokens: List[str] = []
    if info is None:
        return tokens
    if info.code:
        tokens.append(str(info.code))
    if info.name:
        tokens.append(str(info.name))
    tokens.append(str(info.layer))
    return tokens


def match_rule_distance(
    rules: Dict,
    basis: str,
    tokens: Sequence[str],
    road_level_name: str,
    default_distance: float,
    overrides: Dict[str, float],
) -> Tuple[float, str]:
    """旧规则文件（只有 rules 数组）时的兼容取距逻辑。"""
    for key, value in overrides.items():
        if any(key and key.lower() in str(token).lower() for token in tokens):
            return value, f"命令行覆盖（命中 {key}）"
        if road_level_name and key == road_level_name:
            return value, f"命令行覆盖（道路等级 {road_level_name}）"
    for rule in rules.get("rules", []) or []:
        if str(rule.get("basis", "")) != basis:
            continue
        if basis == "road":
            if road_level_name and str(rule.get("road_level", "")) == road_level_name:
                return float(rule.get("distance", default_distance)), f"规则表（{road_level_name}）"
            continue
        wanted = [str(item) for item in (rule.get("landuse") or [])]
        for item in wanted:
            if any(item.lower() in str(token).lower() for token in tokens):
                return float(rule.get("distance", default_distance)), f"规则表（命中 {item}）"
    return float(default_distance), "规则未命中，按默认退距"


def parcel_distances(rules: Dict, args) -> Dict[str, object]:
    """两级退距 + 临绿地水系退距；旧规则文件回退为单值。"""
    heights = rules.get("parcel_height_rules") or []
    low = None
    high = None
    for item in heights:
        kind = str(item.get("height_class", ""))
        try:
            value = float(item.get("distance"))
        except Exception:
            continue
        if kind == "low_mid":
            low = value
        elif kind == "high":
            high = value
    legacy = low is None and high is None
    fallback = float(rules.get("default_distance", 5.0))
    if legacy:
        return {"low_mid": fallback, "high": fallback, "green": fallback, "legacy": True}
    low = fallback if low is None else low
    high = low if high is None else high
    side_rules = rules.get("parcel_side_rules") or []
    green = low
    if side_rules:
        try:
            green = float(side_rules[0].get("distance", low))
        except Exception:
            green = low
    overrides = parse_overrides(args.distance_override)
    low = _override(overrides, "低多层", "low_mid", "low") or low
    high = _override(overrides, "高层", "high") or high
    green = _override(overrides, "临绿地", "绿地", "green") or green
    return {"low_mid": float(low), "high": float(high), "green": float(green), "legacy": False}


def describe_rules_source(args, rules: Dict) -> Dict:
    """退距规则来源：默认草案必须先让用户点头，院内文件或用户给定数值才算确认。

    退距直接决定建筑控制线位置，仓库里的 setback_rules.json 只是草案（_status 字段），
    各城市低多层常见 3~6m、高层 8~15m，不允许照搬默认值出成果。
    """
    rules_path = Path(str(args.rules)).resolve()
    default_path = Path(str(DEFAULT_RULES)).resolve()
    status = str(rules.get("_status") or "").strip()
    override = parse_overrides(getattr(args, "distance_override", None))
    values = {
        "低多层": _parcel_rule(rules, "low_mid"),
        "高层": _parcel_rule(rules, "high"),
        "临绿地水系": _parcel_green_rule(rules),
        "快速路": float((rules.get("road_level_distances") or {}).get("快速路", 20.0)),
        "主干路": float((rules.get("road_level_distances") or {}).get("主干路", 15.0)),
        "次干路": float((rules.get("road_level_distances") or {}).get("次干路", 10.0)),
        "支路": float((rules.get("road_level_distances") or {}).get("支路", 5.0)),
    }
    values.update(override)
    accepts = bool(getattr(args, "accept_default_rules", False))
    if rules_path != default_path:
        source = "院内/地方标准文件"
    elif override:
        source = "命令行覆盖（用户提供）"
    elif accepts:
        source = "按内置默认值（用户已确认）"
    else:
        source = "内置默认值（未确认）"
    needs_confirm = rules_path == default_path and bool(status) and not accepts and not override
    return {
        "file": str(rules_path),
        "status": status,
        "source": source,
        "needs_confirm": needs_confirm,
        "accepted": accepts,
        "note": str(getattr(args, "rules_note", "") or ""),
        "values": values,
        "high_rise": rules.get("high_rise") or {},
        "inactive_rules_note": (
            "规则文件 rules 里按用地性质的条目（如「商业 3m」）在当前 parcel_height_rules 口径下不生效，"
            "实际按建筑高度分低多层/高层取距，避免误读"
        ),
        "hint": "内置默认值未经院内标准校准时只能用于方案比较，不得作为报建依据。",
    }


OPTION_DEFAULTS = {
    "scope_mode": "none",
    "green_parcels": "exclude",
    "min_ring_width": 5.0,
    "min_ring_area": 10.0,
    "keep_holes": "large",
    "ring_dedupe": "on",
    "coincident_policy": "report",
}

_BUILDING_NAME_WORDS = ("塔楼", "裙房", "主楼", "楼", "栋")
_BUILDING_BAD_WORDS = ("图例", "图框", "标注", "注记", "索引", "道路", "绿化", "水系", "指标", "说明")


def _building_name_candidates(visible: Sequence[str], spec: Dict) -> List[str]:
    """按命名找建筑候选（含塔楼/裙房这类不含「建筑」二字的图层），用于识别「空图层陷阱」。"""
    found: List[str] = []
    for name in visible:
        if spec_mod.layer_in_group(name, spec, "building"):
            found.append(name)
            continue
        if any(word in name for word in _BUILDING_NAME_WORDS) and not any(
            bad in name for bad in _BUILDING_BAD_WORDS
        ):
            found.append(name)
    return cli.dedupe(found)


def describe_options(
    args,
    visible: Sequence[str],
    spec: Dict,
    layers: Dict[str, List[str]],
    classifier,
) -> Dict:
    """六项口径确认：建筑图层 / 绿地水系 / 高度分级 / 杂线阈值 / 环清理策略 / 规划范围。

    这些口径都会实质改变成果（压线清单、控制线数量、地块取舍），不允许「脚本有默认值就直接算」。
    """
    items: List[Dict] = []

    def add(key: str, question: str, evidence, recommended: str, flag: str) -> None:
        items.append(
            {
                "key": key,
                "question": question,
                "evidence": evidence,
                "recommended": recommended,
                "flag": flag,
            }
        )

    building_candidates = _building_name_candidates(visible, spec)
    if not args.building_layers and len(building_candidates) != 1:
        add(
            "building_layers",
            "建筑图层候选不止一个（或为空图层）：退线要用建筑做压线校验，选错会得到 0 栋或重复计入。",
            building_candidates,
            "用 --building-layers 显式指定；空图层不能用，塔楼/裙房分层要一起选",
            '--building-layers "图层A,图层B"',
        )

    green_prefixes = tuple(cli.parse_list(args.green_landuse)) or tuple(parcel_sides.GREEN_WATER_PREFIXES)
    green_layers = []
    for name in layers.get("landuse") or []:
        try:
            info = landuse.inspect_layer(name, classifier, spec)
        except Exception:
            continue
        if info.code and str(info.code).startswith(green_prefixes):
            green_layers.append(f"{name}（{info.code}）")
    if args.green_parcels is None and green_layers:
        add(
            "green_parcels",
            "图上有绿地水系用地图层：绿地、水域本身不设建筑退线，是否让它们也出控制线？",
            green_layers,
            "exclude（绿地水系不出退线），报告会列出被剔除的地块数",
            "--green-parcels include|exclude",
        )

    height_candidates = building_class.height_layer_candidates(visible, spec)
    height_decided = bool(
        args.high_rise_layers
        or args.low_rise_layers
        or args.height_layers
        or (not args.height_text)
        or args.height_class != "auto"
    )
    if not height_decided:
        add(
            "height_policy",
            "建筑高度分级口径未定：抓得到高度就只出一条线，抓不到就两条线都出，两种成果差别很大。",
            [{"layer": layer, "score": score} for layer, score in height_candidates]
            or ["图上未识别到高度/层数标注图层"],
            "auto：抓得到高度只出对应一条，抓不到两条线都出（推荐）；"
            "要强制两条都出用 --no-height-text，按图层分级用 --high-rise-layers/--low-rise-layers",
            "--no-height-text / --high-rise-layers A / --low-rise-layers B / --height-class low_mid|high",
        )

    if args.min_ring_width is None or args.min_ring_area is None:
        add(
            "ring_thresholds",
            "内部杂线的判定阈值未定：阈值决定哪些碎线被删，直接影响图纸干净程度与可建面积。",
            [
                f"最小短边宽 {OPTION_DEFAULTS['min_ring_width']:g}m",
                f"最小面积 {OPTION_DEFAULTS['min_ring_area']:g}㎡",
            ],
            f"{OPTION_DEFAULTS['min_ring_width']:g}m / {OPTION_DEFAULTS['min_ring_area']:g}㎡",
            "--min-ring-width 5 --min-ring-area 10",
        )

    if args.keep_holes is None or args.ring_dedupe is None or args.coincident_policy is None:
        add(
            "ring_cleanup",
            "图形清理策略未定：内环（内院/洞）留不留、同层重线怎么去、部分共线重叠怎么处理。",
            [
                f"内环策略 {OPTION_DEFAULTS['keep_holes']}（≥100㎡ 视为真实内院）",
                f"同层去重 {OPTION_DEFAULTS['ring_dedupe']}",
                f"共线策略 {OPTION_DEFAULTS['coincident_policy']}（只列清单不删线）",
            ],
            "large / on / report",
            "--keep-holes large --ring-dedupe on --coincident-policy report",
        )

    scope_candidates = building_massing.default_scope_layers(visible)
    if args.scope_mode is None:
        add(
            "scope_mode",
            "规划范围裁剪未定：不裁剪会把范围外的地块一起算进来，成果数量与压线清单都会变。",
            scope_candidates or ["未识别到规划范围图层"],
            f"{'auto（裁到 ' + scope_candidates[0] + '）' if scope_candidates else 'none（图上没有规划范围图层）'}",
            "--scope-mode auto --scope-layers \"图层名\"",
        )

    return {
        "items": items,
        "count": len(items),
        "unresolved": [item["key"] for item in items],
        "defaults": OPTION_DEFAULTS,
        "scope_candidates": scope_candidates,
        "green_layers": green_layers,
        "building_candidates": building_candidates,
        "height_candidates": [{"layer": layer, "score": score} for layer, score in height_candidates],
        "note": (
            "未逐项指定时可用 --accept-default-options 一次性接受建议值（建筑图层仍按识别结果），"
            "报告会记录「按推荐值执行」；合规成果建议逐项显式指定。"
        ),
    }


def apply_option_defaults(args, options: Dict) -> None:
    explicit_scope = args.scope_mode
    for key, value in (options.get("defaults") or {}).items():
        if getattr(args, key, None) is None:
            setattr(args, key, value)
    if explicit_scope is None and options.get("scope_candidates"):
        # 用户没显式拒绝范围图层时，按推荐裁到第一个候选
        args.scope_mode = "auto"
        if not args.scope_layers:
            args.scope_layers = options["scope_candidates"][0]


def _parcel_rule(rules: Dict, kind: str) -> float:
    """读规则表里某一高度等级的退距（用于确认项展示，不参与计算）。"""
    for item in rules.get("parcel_height_rules") or []:
        if str(item.get("height_class", "")) == kind:
            try:
                return float(item.get("distance"))
            except Exception:
                continue
    return float(rules.get("default_distance", 5.0))


def _parcel_green_rule(rules: Dict) -> float:
    for item in rules.get("parcel_side_rules") or []:
        try:
            return float(item.get("distance"))
        except Exception:
            continue
    return _parcel_rule(rules, "low_mid")


def resolve_layer_names(
    visible: Sequence[str],
    args,
    spec: Dict,
    mapping: Dict[str, List[str]],
    classifier,
) -> Dict[str, List[str]]:
    """按 显式参数 > 指派表 > 命名候选 决定各角色图层。"""

    def explicit(text: Optional[str]) -> List[str]:
        wanted = cli.parse_list(text)
        return [name for name in wanted if name in visible]

    def from_map(roles: Sequence[str]) -> List[str]:
        names: List[str] = []
        for role in roles:
            names.extend(line_layers.layers_of(mapping, role))
        return [name for name in names if name in visible]

    def by_roles(roles: Sequence[str]) -> List[str]:
        threshold = int((spec.get("line_filter") or {}).get("min_score", 3))
        found: List[str] = []
        for name in visible:
            role, score = line_layers.best_role(name, spec)
            if role in roles and score >= threshold:
                found.append(name)
        return found

    def by_group(group: str) -> List[str]:
        return [name for name in visible if spec_mod.layer_in_group(name, spec, group)]

    def code_layers() -> List[str]:
        """能解析出用地代码的图层（含纯代码命名，如 070102-二类城镇住宅用地、03-林地）。"""
        found: List[str] = []
        skip_groups = ("building", "frame", "label", "control")
        for name in visible:
            if any(spec_mod.layer_in_group(name, spec, group) for group in skip_groups):
                continue
            try:
                info = landuse.inspect_layer(name, classifier, spec)
            except Exception:
                continue
            if info.is_parcel:
                found.append(name)
        return found

    landuse_layers = code_layers()
    result: Dict[str, List[str]] = {}
    result["building"] = explicit(args.building_layers) or from_map(["building"]) or by_group("building")
    result["parcel"] = (
        explicit(args.parcel_layers)
        or from_map(["parcel_redline", "parcel_line"])
        or cli.dedupe(by_roles(["parcel_redline", "parcel_line"]) + landuse_layers)
    )
    result["road_redline"] = (
        explicit(args.road_layers)
        or from_map(["road_redline"])
        or cli.dedupe(by_roles(["road_redline"]) + by_group("road"))
    )
    result["road_center"] = from_map(["road_center"]) or by_roles(["road_center"])
    result["height"] = explicit(args.height_layers) or from_map(["height"])
    result["landuse"] = landuse_layers
    return result


def _collect(backend, args, spec, classifier, factor, layers: Dict[str, List[str]], code_filter: Sequence[str]):
    visible = cli.visible_layers(backend)
    exclude = set(cli.parse_list(args.exclude_layers))

    def clean(names: Sequence[str]) -> List[str]:
        return [name for name in names if name in visible and name not in exclude]

    building_layers = clean(layers.get("building") or [])
    parcel_layers = clean(layers.get("parcel") or [])
    road_layers = clean(layers.get("road_redline") or [])
    landuse_layers = clean(layers.get("landuse") or [])

    # 建筑同层可能既有填充又有多段线：both 全收后几何去重，避免重复计数与重复压线告警
    buildings = shapes.collect_polygons(
        backend,
        building_layers,
        unit_factor=factor,
        source="both",
        min_area_m2=float(args.min_building_area),
        dedupe=True,
    )

    neighbors: List[Dict] = []
    if landuse_layers:
        for shape in shapes.collect_polygons(backend, landuse_layers, unit_factor=factor, source="auto"):
            info = landuse.inspect_layer(shape.layer, classifier, spec)
            shape.code = info.code
            shape.name = info.name
            if info.code and str(info.code).startswith(tuple(code_filter)):
                neighbors.append(
                    {
                        "geometry": shape.geometry,
                        "code": info.code,
                        "name": info.name,
                        "handle": shape.handle,
                        "layer": shape.layer,
                    }
                )

    parcels: List[shapes.ShapeRec] = []
    if args.basis in {"parcel", "both"}:
        wanted = list(parcel_layers)
        if not wanted:
            wanted = [name for name in landuse_layers if name not in {n["layer"] for n in neighbors}]
        parcels = shapes.collect_polygons(
            backend, wanted, unit_factor=factor, source="auto", min_area_m2=float(args.min_area)
        )
        filtered: List[shapes.ShapeRec] = []
        for item in parcels:
            info = landuse.inspect_layer(item.layer, classifier, spec)
            item.code = info.code
            item.name = info.name
            if info.code and str(info.code).startswith(tuple(code_filter)) and not args.parcel_layers:
                continue
            filtered.append(item)
        parcels = filtered

    redlines: List[shapes.ShapeRec] = []
    if args.basis in {"road", "both"}:
        redlines = shapes.collect_polygons(backend, road_layers, unit_factor=factor, source="auto")
        if not redlines and road_layers:
            segments = shapes.collect_segments(backend, road_layers)
            redlines = shapes.polygonize(segments, min_area_m2=float(args.min_area), unit_factor=factor)
    texts: List = []
    height_scan = {"mode": "skipped", "layers": [], "count": 0}
    if getattr(args, "height_text", True):
        height_layers = clean(layers.get("height") or [])
        if height_layers:
            try:
                texts = list(backend.iter_texts(height_layers, None))
            except Exception:
                texts = []
            height_scan = {"mode": "targeted", "layers": height_layers, "count": len(texts)}
        else:
            print(
                "[提示] 未指定高度标注图层，将扫描全图文字；若高度标注分散在多个图层，耗时会明显变长，"
                "建议先 --confirm-layers 确认后加 --height-layers。",
                file=sys.stderr,
            )
            try:
                texts = list(backend.iter_texts(None, None))
            except Exception:
                texts = []
            height_scan = {"mode": "full", "layers": [], "count": len(texts)}
    return buildings, parcels, neighbors, redlines, texts, {
        "building": building_layers,
        "parcel": parcel_layers,
        "road": road_layers,
        "landuse": landuse_layers,
        "height_scan": height_scan,
    }


def _apply_roads(
    buildable,
    parcel_geometry,
    redlines: Sequence[shapes.ShapeRec],
    rules: Dict,
    overrides: Dict[str, float],
    user_levels: Dict[str, str],
    default_distance: float,
    geometry_factor: float,
    level_rows: List[Dict],
    factor: float,
    report_width: bool,
) -> Tuple[object, List[Dict], List[str]]:
    """按临路关系扣除道路退线带；返回 (可建范围, 依据列表, 问题列表)。"""
    related = [line for line in redlines if offset.intersects(parcel_geometry, line.geometry)]
    applied: List[Dict] = []
    issues: List[str] = []
    if not related:
        applied.append({"basis": "road", "distance": 0.0, "reason": "地块未与任何红线相交"})
        return buildable, applied, issues
    for line in related:
        level, source = road_level.resolve(line.layer, user_levels, rules)
        override = _override(overrides, level) if level else None
        if override is not None:
            distance, reason = override, f"命令行覆盖（道路等级 {level}）"
        else:
            distance, reason = road_level.distance_of(level, rules, default_distance)
        if report_width:
            width, note = road_level.width_reference(line.geometry, factor)
        else:
            width, note = 0.0, ""
        level_rows.append(
            {
                "layer": line.layer,
                "level": level or "未判定",
                "source": source,
                "width_ref": width,
                "width_note": note,
                "distance": distance,
            }
        )
        buffered = offset.expand(line.geometry, distance / geometry_factor, join="round")
        if buffered is None:
            continue
        buildable = buildable.difference(buffered)
        applied.append(
            {
                "basis": "road",
                "distance": distance,
                "reason": reason,
                "road_layer": line.layer,
                "road_level": level,
                "level_source": source,
                "width_ref": width,
            }
        )
        if buildable is None or buildable.is_empty:
            issues.append("道路退线后已无可建范围")
            break
    return buildable, applied, issues


def compute(
    buildings,
    parcels,
    neighbors,
    redlines,
    heights: Dict[str, Dict],
    args,
    rules: Dict,
    classifier,
    spec: Dict,
    factor: float,
    unit: str,
) -> Tuple[List[ParcelSetback], List[Dict], List[Dict]]:
    results: List[ParcelSetback] = []
    issues: List[Dict] = []
    level_rows: List[Dict] = []
    distances = parcel_distances(rules, args)
    overrides = parse_overrides(args.distance_override)
    user_levels = road_level.parse_user_levels(args.road_levels)
    default_distance = args.default_distance
    if default_distance is None:
        default_distance = float(rules.get("default_distance", 5.0))
    side_cfg = (rules.get("parcel_side_rules") or [{}])[0]
    tolerance_side = args.side_tolerance if args.side_tolerance is not None else float(side_cfg.get("touch_tolerance", 0.1))
    min_contact = (
        args.min_contact_length
        if args.min_contact_length is not None
        else float(side_cfg.get("min_contact_length", 2.0))
    )
    code_filter = tuple(item.strip() for item in cli.parse_list(args.green_landuse)) or ("01", "02", "03", "04", "14", "17")

    for parcel in parcels:
        if parcel.geometry is None:
            issues.append({"kind": "no_geometry", "message": "地块缺少可运算几何", "handle": parcel.handle, "layer": parcel.layer})
            continue
        info = landuse.inspect_layer(parcel.layer, classifier, spec)
        record = ParcelSetback(parcel=parcel, area_parcel_m2=parcel.area_m2)
        geometry_factor = 1.0
        try:
            scale = (parcel.area_m2 / parcel.geometry.area) ** 0.5 if parcel.geometry.area > 0 else 1.0
            geometry_factor = scale if scale > 0 else 1.0
        except Exception:
            geometry_factor = 1.0
        record.geometry_factor = geometry_factor

        # 地块级高度判定：有高度→只出对应一条线；抓不到→两条线都出
        if args.height_class in ("low_mid", "high"):
            record.height_mode = "single"
            record.target_class = args.height_class
            record.height_info = {"height": None, "source": "命令行强制", "count": 0}
        else:
            height_hit = building_class.parcel_height(parcel.geometry, buildings, heights, tolerance=0.1)
            if height_hit:
                record.height_mode = "single"
                record.target_class = building_class.class_from_height(height_hit["height"], rules)
                record.height_info = height_hit
            else:
                record.height_mode = "both"
                record.target_class = ""
                record.height_info = {"height": None, "source": "未抓取到高度或层数", "count": 0}

        sides: List[Dict] = []
        if args.basis in {"parcel", "both"}:
            usable = [
                (item["geometry"], item["code"])
                for item in neighbors
                if item.get("handle") != parcel.handle
            ]
            usable = [(geom, code) for geom, code in usable if str(code or "").startswith(code_filter)]
            sides = parcel_sides.green_sides(
                parcel.geometry,
                usable,
                unit_factor=factor,
                tolerance=tolerance_side,
                min_contact_length=min_contact,
            )
        record.sides = sides
        relaxed = [(item["line"], distances["green"] / geometry_factor) for item in sides]

        low_geom = high_geom = parcel.geometry
        if args.basis in {"parcel", "both"}:
            if distances["legacy"]:
                distance, reason = match_rule_distance(
                    rules, "parcel", landuse_tokens(info), "", default_distance, overrides
                )
                low_distance = high_distance = distance
                low_reason = high_reason = reason
            else:
                low_distance = float(distances["low_mid"])
                high_distance = float(distances["high"])
                low_reason = "退用地红线（低多层）"
                high_reason = "退用地红线（高层）"
            low_res = offset.shrink_except(
                parcel.geometry,
                low_distance / geometry_factor,
                relaxed,
                join=args.offset_join,
                mitre_limit=args.mitre_limit,
            )
            high_res = offset.shrink_except(
                parcel.geometry,
                high_distance / geometry_factor,
                relaxed,
                join=args.offset_join,
                mitre_limit=args.mitre_limit,
            )
            record.apply.append(
                {"basis": "parcel", "height_class": "low_mid", "distance": low_distance, "reason": low_reason}
            )
            record.apply.append(
                {"basis": "parcel", "height_class": "high", "distance": high_distance, "reason": high_reason}
            )
            for item in sides:
                record.apply.append(
                    {
                        "basis": "parcel_green",
                        "distance": float(distances["green"]),
                        "reason": f"临绿地水系（{item['label']} {item['code']}）",
                        "adjacent_code": item["code"],
                        "contact_m": item["length_m"],
                    }
                )
            if not low_res.ok:
                record.issues.append(low_res.reason)
                issues.append(
                    {"kind": "offset_failed", "message": low_res.reason, "handle": parcel.handle, "layer": parcel.layer}
                )
                low_geom = None
            else:
                low_geom = low_res.geometry
            if not high_res.ok:
                record.issues.append(high_res.reason)
                issues.append(
                    {"kind": "offset_failed", "message": high_res.reason, "handle": parcel.handle, "layer": parcel.layer}
                )
                continue
            high_geom = high_res.geometry
            if low_geom is None:
                # 低多层线退化时按高层线出，避免把整块地误当成低多层可建范围
                low_geom = high_geom
                record.issues.append("低多层退线退化，已按高层控制线输出")

        if args.basis in {"road", "both"}:
            low_geom, low_applied, low_issues = _apply_roads(
                low_geom,
                parcel.geometry,
                redlines,
                rules,
                overrides,
                user_levels,
                default_distance,
                geometry_factor,
                [],
                factor,
                args.report_road_width,
            )
            high_geom, high_applied, high_issues = _apply_roads(
                high_geom,
                parcel.geometry,
                redlines,
                rules,
                overrides,
                user_levels,
                default_distance,
                geometry_factor,
                level_rows,
                factor,
                args.report_road_width,
            )
            record.apply.extend(high_applied)
            for message in low_issues + high_issues:
                record.issues.append(message)
                issues.append(
                    {"kind": "offset_failed", "message": message, "handle": parcel.handle, "layer": parcel.layer}
                )

        if low_geom is None or getattr(low_geom, "is_empty", True):
            low_geom = None
        if high_geom is None or getattr(high_geom, "is_empty", True):
            record.issues.append("道路退线后已无可建范围")
            issues.append(
                {
                    "kind": "offset_failed",
                    "message": "道路退线后已无可建范围，退距可能过大",
                    "handle": parcel.handle,
                    "layer": parcel.layer,
                }
            )
            continue

        record.buildable_low = low_geom
        record.buildable_high = high_geom
        record.buildable = high_geom
        try:
            record.area_buildable_low_m2 = float(low_geom.area) * geometry_factor * geometry_factor
        except Exception:
            record.area_buildable_low_m2 = 0.0
        try:
            record.area_buildable_high_m2 = float(high_geom.area) * geometry_factor * geometry_factor
        except Exception:
            record.area_buildable_high_m2 = 0.0
        record.area_buildable_m2 = record.area_buildable_high_m2
        results.append(record)

    conflicts: List[Dict] = []
    for record in results:
        kinds = [record.target_class] if record.height_mode == "single" and record.target_class else ["low_mid", "high"]
        for building in buildings:
            if building.geometry is None:
                continue
            if not offset.intersects(record.parcel.geometry, building.geometry):
                continue
            for kind in kinds:
                target = record.buildable_low if kind == "low_mid" else record.buildable_high
                if target is None:
                    continue
                try:
                    excluded = record.parcel.geometry.difference(target)
                except Exception:
                    continue
                hit = offset.intersection_area(building.geometry, excluded)
                if hit <= 0:
                    continue
                area = max(building.geometry.area, 1e-9)
                ratio = hit / area
                if ratio <= args.tolerance:
                    continue
                label = building_class.CLASS_LABELS.get(kind, kind)
                mode_note = "" if record.height_mode == "single" else "（高度未知，两条线分别校验）"
                conflicts.append(
                    {
                        "kind": "building_in_setback",
                        "message": (
                            f"建筑压退线（{label}{mode_note}）：压线面积 {hit * record.geometry_factor ** 2:.2f} ㎡，"
                            f"占基底 {ratio * 100:.1f}%"
                        ),
                        "handle": building.handle,
                        "layer": building.layer,
                        "parcel_handle": record.parcel.handle,
                        "parcel_label": record.parcel.code or record.parcel.layer,
                    }
                )
    return results, issues + conflicts, level_rows


def write_back(
    backend,
    results: Sequence[ParcelSetback],
    low_layer: str,
    high_layer: str,
    spec,
    args=None,
    unit_factor: float = 1.0,
    draw: bool = True,
) -> Dict:
    """写退线：环级清理（碎线/内环/无效环）→ 同图层去重 → 共线重叠检查 → 画闭合多段线。"""
    definition = spec_mod.control_def(spec, "退线")
    min_area = float(getattr(args, "min_ring_area", ring_clean.DEFAULT_MIN_RING_AREA) or 0.0)
    min_width = float(getattr(args, "min_ring_width", ring_clean.DEFAULT_MIN_RING_WIDTH) or 0.0)
    keep_holes = str(getattr(args, "keep_holes", "large") or "large")
    keep_hole_area = float(getattr(args, "keep_hole_area", ring_clean.DEFAULT_KEEP_HOLE_AREA) or 0.0)
    dedupe = str(getattr(args, "ring_dedupe", "on") or "on").lower() != "off"
    policy = str(getattr(args, "coincident_policy", "report") or "report").lower()

    summary: Dict[str, Dict] = {}
    for key, layer in (("low", low_layer), ("high", high_layer)):
        if draw:
            backend.ensure_layer(layer, color=definition.get("color"), linetype=definition.get("linetype"))
        entries: List[Dict] = []
        for record in results:
            if record.height_mode == "single":
                wanted = "low" if record.target_class == "low_mid" else "high"
                if key != wanted:
                    continue
            geometry = record.buildable_low if key == "low" else record.buildable_high
            for kind, ring in ring_clean.iter_rings(geometry):
                entries.append(
                    {
                        "handle": record.parcel.handle,
                        "code": record.parcel.code or record.parcel.layer,
                        "layer": record.parcel.layer,
                        "kind": kind,
                        "ring": ring,
                    }
                )
        kept, dropped = ring_clean.clean_layer(
            entries,
            min_area=min_area,
            min_width_m=min_width,
            keep_holes=keep_holes,
            keep_hole_area=keep_hole_area,
            dedupe=dedupe,
            unit_factor=unit_factor,
        )
        written = 0
        coincident: List[Dict] = []
        merged = False
        if policy == "merge" and kept:
            merged = True
            for _meta, ring in ring_clean.merge_rings(kept, unit_factor=unit_factor):
                if draw:
                    backend.add_polyline(layer, ring, closed=True)
                written += 1
        else:
            for _meta, ring in kept:
                if draw:
                    backend.add_polyline(layer, ring, closed=True)
                written += 1
            if policy == "report":
                coincident = ring_clean.coincident_pairs(kept, unit_factor=unit_factor)
        summary[key] = {
            "layer": layer,
            "written": written,
            "closed": written,
            "dropped": len(dropped),
            "dropped_area_m2": round(sum(float(item.get("area_m2") or 0.0) for item in dropped), 2),
            "dropped_detail": dropped,
            "coincident": coincident,
            "merged": merged,
            "input_rings": len(entries),
            "drawn": draw,
        }
    return summary


def run(args) -> int:
    cli.setup_stdout()
    offset.require_shapely()
    spec = spec_mod.load_spec()
    rules = load_rules(args.rules)
    mapping = line_layers.load_map(args.layer_map)
    backend, unit, factor, unit_source = cli.open_backend(args)
    args.report_path = cli.resolve_out(args, backend, "退线成果")
    try:
        guess = landuse.detect_standard(cli.visible_layers(backend))
        standard = guess.get("standard") or "2023"
        classifier = landuse.build(standard, None, None)
        visible = cli.visible_layers(backend)
        layers = resolve_layer_names(visible, args, spec, mapping, classifier)

        # 预检：把「口径类」确认项一次性收集，扫描几何之前只问一轮。
        # 原来每碰到一个问题就 return 一次，用户要重跑 4 遍才能把口径答完。
        pending: List[Dict] = []
        if args.confirm_layers:
            rows = line_layers.describe_candidates(visible, spec)
            ambiguous = line_layers.ambiguous(visible, spec)
            height_rows = building_class.height_layer_candidates(visible, spec)
            pending.extend(
                [
                    confirm.item(
                        "line_layer_assignment",
                        "线条类图层没有字段映射表可依，制图习惯因院而异：请确认规划范围线、用地红线、地块线、道路红线、道路中心线的图层指派。",
                        options=[
                            {"label": "按候选确认", "desc": "把下面 candidates 里选中的图层写入 layer-map.json，用 --layer-map 重跑"},
                            {"label": "指定其他图层", "desc": "用 --parcel-layers / --road-layers / --building-layers 显式指定"},
                            {"label": "同分冲突需人工指定", "desc": "ambiguous 中的图层（如裸「红线」「HX」）必须由人指定归属"},
                        ],
                        detail={
                            "candidates": rows,
                            "ambiguous": ambiguous,
                            "height_candidates": [
                                {"layer": layer, "score": score} for layer, score in height_rows
                            ],
                            "resolved_default": layers,
                            "basis_options": BASIS_OPTIONS,
                            "note": "候选与打分仅用于排序，不代表已采纳；确认后请用 --layer-map 复用。",
                            "basis_note": (
                                "退线基准必须先把 parcel / road / both 三种口径的差别讲给用户听，"
                                "再由用户选定；不要只给三个词就让人选。"
                            ),
                            "height_note": (
                                "高度/层数标注图层未指定时会扫描全图文字；标注分散在多个图层（或图纸文字量大）时耗时会明显变长，"
                                "建议一并确认后用 --height-layers 只扫这些图层。"
                            ),
                        },
                    )
                ]
            )

        rules_source = describe_rules_source(args, rules)
        if rules_source["needs_confirm"]:
            pending.extend(
                [
                    confirm.item(
                        "setback_rules_source",
                        (
                            f"当前退距规则文件仍是「{rules_source['status']}」，不是院内/地方标准。"
                            "退距直接决定建筑控制线位置，不同城市差异很大（低多层常见 3~6m、高层 8~15m），"
                            "请先确认本次按哪套规则执行，不要默认照搬内置默认值。"
                        ),
                        options=[
                            {
                                "id": "accept_draft",
                                "label": "按内置默认值执行",
                                "args": ["--accept-default-rules"],
                                "desc": (
                                    "低多层 5m / 高层 10m / 临绿地水系 5m / 道路 快速20 主干15 次干10 支路5；"
                                    "加 --accept-default-rules 复算，报告会注明「未经院内标准校准」"
                                ),
                            },
                            {
                                "id": "custom_values",
                                "label": "我提供数值",
                                "args": [],
                                "requires_input": "distance_override",
                                "desc": (
                                    "用 --distance-override \"低多层=6,高层=12,临绿地=5,主干路=18\" 指定，"
                                    "报告记录为「用户提供」"
                                ),
                            },
                            {
                                "id": "rules_file",
                                "label": "用院内/地方标准文件",
                                "args": [],
                                "requires_input": "rules_file",
                                "desc": "用 --rules 路径.json 指定（结构同 assets/config/setback_rules.json），报告记录文件路径",
                            },
                        ],
                        detail=rules_source,
                    )
                ]
            )

        if args.basis is None:
            user_levels_probe = road_level.parse_user_levels(args.road_levels)
            road_rows = []
            for name in layers.get("road_redline") or []:
                level, source = road_level.resolve(name, user_levels_probe, rules)
                distance, _reason = road_level.distance_of(
                    level, rules, float(rules.get("default_distance", 5.0))
                )
                road_rows.append(
                    {
                        "layer": name,
                        "level": level or "未判定",
                        "source": source,
                        "distance": distance,
                    }
                )
            pending.extend(
                [
                    confirm.item(
                        "setback_basis",
                        (
                            "退线基准有三种口径，差别在于「退的是什么界线」，请先看差异再选："
                            "① parcel 只退用地红线，反映地块自身退让（低多层 5m / 高层 10m）；"
                            "② road 只退道路红线，按道路等级退（快速 20 / 主干 15 / 次干 10 / 支路 5），反映临路界面退让；"
                            "③ both 两者叠加取交集，最严，用于正式建筑控制线成果。"
                        ),
                        options=[
                            {
                                "id": item["value"],
                                "label": item["label"],
                                "desc": item["desc"],
                                "args": ["--basis", item["value"]],
                            }
                            for item in BASIS_OPTIONS
                        ],
                        detail={
                            "basis_options": BASIS_OPTIONS,
                            "parcel_layers": layers.get("parcel") or [],
                            "landuse_layers": layers.get("landuse") or [],
                            "road_redline_layers": layers.get("road_redline") or [],
                            "road_levels": road_rows,
                            "distance_rules": {
                                "parcel_low_m": _parcel_rule(rules, "low_mid"),
                                "parcel_high_m": _parcel_rule(rules, "high"),
                                "green_m": _parcel_green_rule(rules),
                            },
                            "hint": (
                                "没有道路红线图层时 road / both 不可用；临时用地块内可建范围请选 parcel。"
                                "选定后用 --basis <parcel|road|both> 重跑。"
                            ),
                        },
                    )
                ]
            )

        options = describe_options(args, visible, spec, layers, classifier)
        if options["unresolved"] and not args.accept_default_options:
            pending.extend(
                [
                    confirm.item(
                        "setback_options",
                        (
                            "以下口径会实质改变成果（控制线数量、地取舍、压线清单），本次未逐项指定："
                            + "、".join(item["key"] for item in options["items"])
                            + "。请确认按建议值执行，或逐项用 flag 指定。"
                        ),
                        options=[
                            {
                                "id": "accept",
                                "label": "按建议值执行",
                                "args": ["--accept-default-options"],
                                "desc": "加 --accept-default-options，报告会记录「按推荐值执行」与每项取值",
                            },
                            {
                                "id": "manual",
                                "label": "逐项指定",
                                "args": [],
                                "requires_input": "flags",
                                "desc": "按 detail.items 里每项的 flag 逐个指定（合规成果建议这样做）",
                            },
                        ],
                        detail=options,
                    )
                ]
            )
        if pending:
            return confirm.emit(pending, round_no=1)
        apply_option_defaults(args, options)

        buildings, parcels, neighbors, redlines, texts, used = _collect(
            backend, args, spec, classifier, factor, layers, cli.parse_list(args.green_landuse) or list(parcel_sides.GREEN_WATER_PREFIXES)
        )
        green_skipped = 0
        if str(args.green_parcels or "exclude") == "exclude" and parcels:
            prefixes = tuple(cli.parse_list(args.green_landuse)) or tuple(parcel_sides.GREEN_WATER_PREFIXES)
            before = len(parcels)
            parcels = [item for item in parcels if not str(item.code or "").startswith(prefixes)]
            green_skipped = before - len(parcels)

        heights = building_class.attach_heights(
            buildings, texts, args.floor_height, args.height_tolerance
        )

        scope_info = {
            "mode": args.scope_mode,
            "used": False,
            "layers": [],
            "dropped_parcels": 0,
            "dropped_buildings": 0,
        }
        if args.scope_mode == "auto":
            scope_layers = cli.parse_list(args.scope_layers) or building_massing.default_scope_layers(visible)
            scope_layers = [name for name in scope_layers if name in visible]
            scope_info["layers"] = scope_layers
            if scope_layers:
                scope_shape = building_massing.scope_geometry(backend, scope_layers, factor)
                if scope_shape is not None:
                    parcels, buildings, dropped_p, dropped_b = building_massing.clip_to_scope(
                        parcels, buildings, scope_shape, factor
                    )
                    scope_info.update(
                        {
                            "used": True,
                            "dropped_parcels": dropped_p,
                            "dropped_buildings": dropped_b,
                        }
                    )
                else:
                    print(
                        f"[提示] 规划范围图层 {scope_layers} 未取到闭合边界，本次不裁剪。",
                        file=sys.stderr,
                    )
            else:
                print("[提示] --scope-mode auto 未识别到规划范围图层，本次按整图计算。", file=sys.stderr)

        if args.basis in {"parcel", "both"} and not parcels:
            return confirm.emit(
                [
                    confirm.item(
                        "no_basis_parcel",
                        "没有找到可用的用地边界（退线基准）。请指定 --parcel-layers，或确认地块是否闭合。",
                        options=[
                            {"label": "指定用地红线图层", "desc": "用 --parcel-layers \"YD-用地红线\" 指定"},
                            {"label": "查看候选", "desc": "加 --confirm-layers 输出线条图层候选与打分"},
                            {"label": "改用道路红线为基准", "desc": "加 --basis road --road-layers ..."},
                            {"label": "先修图（地块未闭合）", "desc": "回 CAD 闭合后重跑"},
                        ],
                        detail={"parcel_layers": used["parcel"], "road_layers": used["road"]},
                    )
                ]
            )
        if args.basis in {"road", "both"} and not redlines:
            return confirm.emit(
                [
                    confirm.item(
                        "no_basis_road",
                        "没有找到可用的道路红线（闭合面或可缝合成面的线）。",
                        options=[
                            {"label": "指定红线图层", "desc": "用 --road-layers 指定"},
                            {"label": "查看候选", "desc": "加 --confirm-layers 输出线条图层候选与打分"},
                            {"label": "改用用地边界为基准", "desc": "加 --basis parcel"},
                            {"label": "先修图（红线未闭合）", "desc": "回 CAD 闭合后重跑"},
                        ],
                        detail={"road_layers": used["road"]},
                    )
                ]
            )
        if not buildings:
            return confirm.fail(
                "no_building",
                "没有找到建筑轮廓。请用 --building-layers 指定建筑图层，或确认建筑是闭合多段线/填充。",
            )

        user_levels = road_level.parse_user_levels(args.road_levels)
        if args.level_policy == "ask" and redlines:
            unknown = [
                line.layer
                for line in redlines
                if not road_level.resolve(line.layer, user_levels, rules)[0]
            ]
            if unknown:
                return confirm.emit(
                    [
                        confirm.item(
                            "road_level_unknown",
                            "有道路红线图层判定不出等级。注意：红线宽度与道路等级没有必然对应关系（主干路以下尤其如此），本工具不按宽度推定等级，请直接指定。",
                            options=[
                                {"label": "指定等级", "desc": "用 --road-levels \"图层名=主干路\" 指定"},
                                {"label": "按默认退距执行", "desc": "加 --level-policy default（报告会标注等级未判定）"},
                            ],
                            detail={"unknown_layers": sorted(set(unknown)), "levels": list(road_level.LEVELS)},
                        )
                    ]
                )

        # 显式指定的高层/低多层图层覆盖文字抓取结果（最可靠，人工指定优先）
        for building in buildings:
            layer_name = str(getattr(building, "layer", "")).lower()
            for key in cli.parse_list(args.high_rise_layers):
                if key and key.lower() in layer_name:
                    heights[str(building.handle)] = {"height": 999.0, "source": "用户指定高层图层"}
                    break
            for key in cli.parse_list(args.low_rise_layers):
                if key and key.lower() in layer_name:
                    heights[str(building.handle)] = {"height": 6.0, "source": "用户指定低多层图层"}
                    break
        results, problems, level_rows = compute(
            buildings, parcels, neighbors, redlines, heights, args, rules, classifier, spec, factor, unit
        )

        if args.out_layer and not args.out_layer_high:
            high_layer = args.out_layer
            low_layer = args.out_layer_low or f"{args.out_layer}-低多层"
        else:
            base = spec_mod.control_layer(spec, "退线")
            high_layer = args.out_layer_high or f"{base}-高层"
            low_layer = args.out_layer_low or f"{base}-低多层"

        draw = bool(args.write_back) and not bool(args.dry_run)
        rings = write_back(backend, results, low_layer, high_layer, spec, args=args, unit_factor=factor, draw=draw)
        counts = {"low": rings["low"]["written"], "high": rings["high"]["written"]}
        copy_path, copy_format, copy_note = None, "none", "未生成副本（--dry-run 或 --no-write-back）"
        if draw:
            copy_path, copy_format, copy_note = cli.export_drawing(backend, args, "退线")

        distances = parcel_distances(rules, args)
        class_counts = {"low_mid": 0, "high": 0}
        for info in heights.values():
            kind = building_class.class_from_height(info.get("height"), rules)
            if kind in class_counts:
                class_counts[kind] += 1
        payload = {
            "status": "ok",
            "standard": standard,
            "unit": unit,
            "unit_source": unit_source,
            "basis": args.basis,
            "rules_source": rules_source,
            "options": {
                "accepted_defaults": bool(args.accept_default_options),
                "resolved": {
                    "scope_mode": args.scope_mode,
                    "scope_layers": args.scope_layers or "",
                    "green_parcels": args.green_parcels,
                    "green_parcels_skipped": green_skipped,
                    "height_text": bool(args.height_text),
                    "min_ring_width_m": float(args.min_ring_width),
                    "min_ring_area_m2": float(args.min_ring_area),
                    "keep_holes": args.keep_holes,
                    "ring_dedupe": args.ring_dedupe,
                    "coincident_policy": args.coincident_policy,
                },
                "warnings": options.get("unresolved") or [],
            },
            "layers": used,
            "scope": scope_info,
            "layer_map": args.layer_map or "",
            "layers_written": counts,
            "rings": rings,
            "ring_rules": {
                "min_ring_area_m2": float(args.min_ring_area),
                "min_ring_width_m": float(args.min_ring_width),
                "keep_holes": args.keep_holes,
                "keep_hole_area_m2": float(args.keep_hole_area),
                "ring_dedupe": args.ring_dedupe,
                "coincident_policy": args.coincident_policy,
                "note": "重线只在同一输出图层内判定；低多层线与高层线之间的重合属规则结果，不参与去重",
            },
            "setback_layer": {"low": low_layer, "high": high_layer},
            "buildings": len(buildings),
            "building_class": class_counts,
            "height_class_default": args.height_class,
            "height_source": {
                "buildings_with_height": len(heights),
                "floor_height_m": args.floor_height,
                "scan": used.get("height_scan", {}),
            },
            "rules": {
                "parcel_low_m": distances["low_mid"],
                "parcel_high_m": distances["high"],
                "green_m": distances["green"],
                "legacy": distances["legacy"],
            },
            "road_levels": level_rows,
            "parcels": [
                {
                    "handle": item.parcel.handle,
                    "layer": item.parcel.layer,
                    "code": item.parcel.code,
                    "area_m2": round(item.area_parcel_m2, 3),
                    "buildable_m2": round(item.area_buildable_m2, 3),
                    "buildable_low_m2": round(item.area_buildable_low_m2, 3),
                    "buildable_high_m2": round(item.area_buildable_high_m2, 3),
                    "height_mode": item.height_mode,
                    "height_target": item.target_class,
                    "height": item.height_info,
                    "green_sides": [
                        {"code": side["code"], "label": side["label"], "length_m": side["length_m"]} for side in item.sides
                    ],
                    "apply": item.apply,
                    "issues": item.issues,
                }
                for item in results
            ],
            "issues": problems,
            "copy_out": copy_path or "",
            "copy_format": copy_format,
            "copy_note": copy_note,
        }
        cli.write_json(args.json_path, payload)
        _write_report(args, payload, results, problems, level_rows, spec, unit, unit_source)
        if copy_path:
            print(f"[成果] {copy_note}")
        print(
            f"[结果] 退线地块 {len(results)} 个，问题 {len(problems)} 条；"
            f"写入低多层线 {counts['low']} 条、高层线 {counts['high']} 条。"
        )
        return confirm.EXIT_OK
    finally:
        try:
            backend.close()
        except Exception:
            pass


def _ring_section(payload: Dict) -> str:
    """报告「八、图形核查与清理」：闭合性、碎线/重线删除明细、同图层共线重叠清单。"""
    rings = payload.get("rings") or {}
    if not rings:
        return "未生成退线环（--no-write-back 或没有可计算地块）。"
    lines: List[str] = [
        "**核查结论**：写出的每一条退线都是闭合多段线（DXF `close=True` / COM `Closed=True`）；"
        "已剔除无效环（自交或顶点不足）、内环碎线、面积或最小短边宽度不足的杂线。"
        "重线只在**同一输出图层内**判定（完全重合 / 被覆盖），低多层线与高层线之间的重合属规则结果，不参与去重。",
        "",
    ]
    summary_rows = []
    for key, label in (("low", "低多层"), ("high", "高层")):
        item = rings.get(key) or {}
        summary_rows.append(
            (
                label,
                item.get("layer", ""),
                item.get("input_rings", 0),
                item.get("written", 0),
                item.get("dropped", 0),
                f"{float(item.get('dropped_area_m2') or 0.0):.2f}",
                "全部闭合（合并输出）" if item.get("merged") else "全部闭合",
                len(item.get("coincident") or []) if not item.get("merged") else "-",
            )
        )
    lines.append(
        md_table(
            ["层级", "图层", "输入环数", "写出环数", "删除环数", "删除面积(㎡)", "闭合性", "共线重叠组"],
            summary_rows,
        )
    )
    if any((rings.get(key) or {}).get("merged") for key in ("low", "high")):
        lines.append("")
        lines.append(
            "> 本次按 `--coincident-policy merge` 输出：同图层结果已 union 合并为外轮廓，"
            "写出环不再逐一对应单个地块句柄（下表的删除明细仍保留原地块句柄）。"
        )

    detail_rows = []
    for key, label in (("low", "低多层"), ("high", "高层")):
        for item in (rings.get(key) or {}).get("dropped_detail") or []:
            detail_rows.append(
                (
                    label,
                    item.get("reason", ""),
                    item.get("parcel", ""),
                    item.get("parcel_label", ""),
                    f"{float(item.get('area_m2') or 0.0):.2f}",
                    f"{float(item.get('width_m') or 0.0):.2f}",
                    item.get("note", ""),
                )
            )
    lines.append("")
    if detail_rows:
        lines.append("**删除明细**（逐条留痕，便于复核是否误删）：")
        lines.append("")
        lines.append(
            md_table(["层级", "原因", "地块句柄", "用地", "面积(㎡)", "最小短边宽(m)", "备注"], detail_rows)
        )
    else:
        lines.append("**删除明细**：无（本次没有触发碎线/重线/无效环清理）。")

    coin_rows = []
    for key, label in (("low", "低多层"), ("high", "高层")):
        for item in (rings.get(key) or {}).get("coincident") or []:
            coin_rows.append(
                (
                    label,
                    item.get("parcel_a", ""),
                    item.get("layer_a", ""),
                    item.get("parcel_b", ""),
                    item.get("layer_b", ""),
                    f"{float(item.get('shared_length_m') or 0.0):.2f}",
                    item.get("note", ""),
                )
            )
    if coin_rows:
        lines.append("")
        lines.append(
            "**同图层共线重叠清单**（默认不删线，删了会破坏该环闭合；确需合并可加 `--coincident-policy merge`）："
        )
        lines.append("")
        lines.append(
            md_table(["层级", "地块A", "用地A", "地块B", "用地B", "重叠长度(m)", "说明"], coin_rows)
        )

    rules = payload.get("ring_rules") or {}
    if rules:
        lines.append("")
        lines.append(
            f"判定阈值：最小短边宽 {rules.get('min_ring_width_m')}m、最小面积 {rules.get('min_ring_area_m2')}㎡、"
            f"内环策略 {rules.get('keep_holes')}（≥{rules.get('keep_hole_area_m2')}㎡ 视为真实内院）、"
            f"同层去重 {rules.get('ring_dedupe')}、共线策略 {rules.get('coincident_policy')}。"
        )
    return "\n".join(lines)


def _write_report(args, payload, results, problems, level_rows, spec, unit, unit_source) -> None:
    out = getattr(args, "report_path", None)
    if not out:
        return
    status = spec_mod.spec_status(spec)
    source = payload.get("rules_source") or {}
    rules_distance = (
        f"临绿地水系 {payload['rules']['green_m']:.0f}m / 低多层 {payload['rules']['parcel_low_m']:.0f}m / "
        f"高层 {payload['rules']['parcel_high_m']:.0f}m"
    )
    note_lines: List[str] = []
    if status:
        note_lines.append(f"> 注：{status}。")
    if source:
        if str(source.get("source", "")).startswith("按内置默认值"):
            note_lines.append(
                f"> 退距依据：**按内置默认值（用户已确认）** {rules_distance}；"
                "未经院内/地方标准校准，仅可用于方案比较，**不得直接作为报建依据**。"
                f"规则文件 `{Path(str(source.get('file', ''))).name}`（{source.get('status', '')}）。"
            )
        elif source.get("source") == "院内/地方标准文件":
            note_lines.append(
                f"> 退距依据：**院内/地方标准文件** `{source.get('file')}`，取值 {rules_distance}。"
            )
        elif str(source.get("source", "")).startswith("命令行覆盖"):
            note_lines.append(f"> 退距依据：**用户提供数值**（命令行覆盖），取值 {rules_distance}。")
        if source.get("note"):
            note_lines.append(f"> 依据说明：{source.get('note')}")
    note = ("\n".join(note_lines) + "\n") if note_lines else ""
    rule_rows = []
    side_rows = []
    for item in results:
        for apply in item.apply:
            rule_rows.append(
                (
                    item.parcel.code or item.parcel.layer,
                    item.parcel.handle,
                    apply.get("basis", ""),
                    {"low_mid": "低多层", "high": "高层"}.get(
                        str(apply.get("height_class", "") or ""), str(apply.get("height_class", "") or "")
                    )
                    or apply.get("road_level", "")
                    or "-",
                    f"{apply.get('distance', 0):.2f}",
                    apply.get("reason", ""),
                )
            )
        for side in item.sides:
            side_rows.append(
                (
                    item.parcel.code or item.parcel.layer,
                    item.parcel.handle,
                    f"{side['code']} {side['label']}",
                    f"{side['length_m']:.2f}",
                    f"{payload['rules']['green_m']:.2f}",
                    "临绿地水系",
                )
            )
    height_rows = []
    for item in results:
        info = item.height_info or {}
        height_value = info.get("height")
        if item.target_class == "high":
            output = "只出高层线"
        elif item.target_class == "low_mid":
            output = "只出低多层线"
        else:
            output = "两条线都出"
        height_rows.append(
            (
                item.parcel.code or item.parcel.layer,
                item.parcel.handle,
                str(info.get("count", 0) or 0),
                f"{float(height_value):.2f}" if height_value else "-",
                info.get("source", "-"),
                output,
            )
        )
    level_table = [
        (
            row.get("layer", ""),
            row.get("level", ""),
            row.get("source", ""),
            f"{row.get('width_ref', 0):.2f}" if row.get("width_ref") else "-",
            row.get("width_note", "") or "-",
            f"{row.get('distance', 0):.2f}",
        )
        for row in level_rows
    ]
    sections = [
        (
            "二、退线计算结果",
            md_table(["用地/图层", "句柄", "基准", "等级/高度", "退距(m)", "依据"], rule_rows) if rule_rows else "无可计算的退线。",
        ),
        (
            "三、可建范围（两级）",
            md_table(
                ["用地/图层", "句柄", "地块面积(㎡)", "低多层可建(㎡)", "高层可建(㎡)", "退让比例(高层)"],
                [
                    (
                        item.parcel.code or item.parcel.layer,
                        item.parcel.handle,
                        f"{item.area_parcel_m2:.2f}",
                        f"{item.area_buildable_low_m2:.2f}",
                        f"{item.area_buildable_high_m2:.2f}",
                        f"{(1 - item.area_buildable_high_m2 / item.area_parcel_m2) * 100:.1f}%"
                        if item.area_parcel_m2 > 0
                        else "-",
                    )
                    for item in results
                ],
            ),
        ),
        (
            "四、建筑高度判定与出线口径",
            md_table(
                ["用地/图层", "句柄", "有高度建筑数", "最高高度(m)", "依据", "输出"],
                height_rows,
            )
            if height_rows
            else "无可计算地块。",
        ),
        (
            "五、临绿地水系边段（退 5m）",
            md_table(["用地/图层", "句柄", "相邻用地", "接触长度(m)", "退距(m)", "依据"], side_rows)
            if side_rows
            else "本地块周边未发现直接相邻的绿地或水系。",
        ),
        (
            "六、道路等级判定",
            md_table(["红线图层", "采用等级", "依据", "宽度参考(m)", "宽度测算", "退距(m)"], level_table)
            if level_table
            else "本次未按道路红线退线。",
        ),
        (
            "七、异常与压线清单",
            md_table(
                ["类型", "说明", "句柄", "图层"],
                [(item.get("kind", ""), item.get("message", ""), item.get("handle", ""), item.get("layer", "")) for item in problems],
            )
            if problems
            else "无。",
        ),
        ("八、图形核查与清理", _ring_section(payload)),
        ("附：句柄定位", locator_note()),
    ]
    meta = [
        ("退线基准", payload.get("basis")),
        (
            "退距依据",
            f"{payload['rules_source']['source']}｜{Path(str(payload['rules_source']['file'])).name}"
            + (f"｜{payload['rules_source']['note']}" if payload["rules_source"].get("note") else "")
            if payload.get("rules_source")
            else "-",
        ),
        ("建筑图层", ",".join(payload.get("layers", {}).get("building", [])) or "-"),
        ("用地图层", ",".join(payload.get("layers", {}).get("parcel", [])) or "-"),
        ("红线图层", ",".join(payload.get("layers", {}).get("road", [])) or "-"),
        ("图层指派表", payload.get("layer_map") or "未使用（建议 --confirm-layers 确认后复用）"),
        (
            "规划范围",
            f"{','.join(payload['scope']['layers'])}（剔除范围外地块 "
            f"{payload['scope']['dropped_parcels']} 个、建筑 {payload['scope']['dropped_buildings']} 栋）"
            if payload.get("scope", {}).get("used")
            else f"未裁剪（--scope-mode {payload.get('scope', {}).get('mode', 'none')}）",
        ),
        (
            "建筑高度",
            f"识别到高度 {payload['height_source']['buildings_with_height']} 栋，楼层按 {payload['height_source']['floor_height_m']:g}m/层 换算；"
            f"出线口径 {payload['height_class_default']}（auto=抓不到高度时两条线都出）",
        ),
        (
            "高度标注图层",
            f"{','.join(payload['height_source']['scan'].get('layers') or [])}（仅扫这些图层）"
            if payload["height_source"]["scan"].get("mode") == "targeted"
            else (
                "全图扫描文字（未指定图层，标注分散时较慢）"
                if payload["height_source"]["scan"].get("mode") == "full"
                else "未扫描文字"
            ),
        ),
        ("退线图层", f"低多层 {payload['setback_layer']['low']}；高层 {payload['setback_layer']['high']}"),
        (
            "图形清理",
            f"最小短边宽 {payload['ring_rules']['min_ring_width_m']:g}m / 最小面积 {payload['ring_rules']['min_ring_area_m2']:g}㎡ / "
            f"内环 {payload['ring_rules']['keep_holes']} / 同层去重 {payload['ring_rules']['ring_dedupe']} / "
            f"共线 {payload['ring_rules']['coincident_policy']}（跨图层两级退线重合不去重）",
        ),
        (
            "口径确认",
            (
                "按推荐值执行（--accept-default-options）"
                if payload.get("options", {}).get("accepted_defaults")
                else "逐项指定"
            )
            + f"：范围 {payload['options']['resolved']['scope_mode']}、"
            f"绿地水系 {payload['options']['resolved']['green_parcels']}"
            f"（剔除 {payload['options']['resolved']['green_parcels_skipped']} 个地块）、"
            f"高度 {'两条线都出' if not payload['options']['resolved']['height_text'] else '按图抓取'}、"
            f"杂线 {payload['options']['resolved']['min_ring_width_m']:g}m/{payload['options']['resolved']['min_ring_area_m2']:g}㎡、"
            f"内环 {payload['options']['resolved']['keep_holes']}、去重 {payload['options']['resolved']['ring_dedupe']}、"
            f"共线 {payload['options']['resolved']['coincident_policy']}",
        ),
        ("图纸单位", f"{unit}（{unit_source}）"),
        (
            "写入结果",
            f"低多层 {payload['layers_written']['low']} 条 / 高层 {payload['layers_written']['high']} 条"
            if any(payload.get("layers_written", {}).values())
            else "未写入",
        ),
        ("成果副本", payload.get("copy_note") or "未生成（--dry-run 或 --no-write-back）"),
    ]
    write_markdown(out, "建筑退线成果", meta, sections, status_note=note)


def main(argv=None) -> int:
    argv = cli.preprocess_argv(argv)
    args = parse_args(argv)
    try:
        return run(args)
    except Exception as exc:
        print(f"[错误] {type(exc).__name__}: {exc}", file=sys.stderr)
        return confirm.EXIT_DATA


if __name__ == "__main__":
    raise SystemExit(main())
