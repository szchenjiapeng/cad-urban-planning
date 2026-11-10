#!/usr/bin/env python
"""子技能 N4：城市控制线生成（原「城市五线」）。

口径（用户确认）：
- 红线：识别道路红线图层后**原样提取、复制**到 KZX-红线（copy，不外扩、不转面）；
- 绿线：默认 1401 + 1402（1402 先询问，非交互默认画），1403 广场用地不画；
- 蓝线：取河道两侧**河口线**，默认不画（需确认）；道路横穿处删桥位短边并两点直连，
  河流沿道路走的长边判为贴河、保留（见 `lib/blue_line.py`）；
- 黄线：13 类公用设施边界 + 市政设施图层；基础设施线（轨道/水管/电力…）按确认图层提取，
  有保护范围时一并绘制 buffer 边界；
- 紫线：默认不画，由用户指定图层。

所有涉及「确定图层」的步骤都先出候选让用户确认（`--confirm-layers`），
确认结果可用 `--layer-map` 复用。
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from core.geometry import discretize  # noqa: E402
from lib import blue_line, cli, confirm, five_lines_layers, landuse, line_layers, offset, shapes  # noqa: E402
from lib import layer_spec as spec_mod  # noqa: E402
from lib.report import locator_note, md_table, write_markdown  # noqa: E402

DEFAULT_CONFIG = _SCRIPTS_DIR / "assets" / "five_lines.json"
LINE_ORDER = ("红线", "绿线", "蓝线", "黄线", "紫线")
# 桥宽上限的兜底值（配置/参数都缺时用）；真正执行前会先量出实测距离再询问用户
FALLBACK_BRIDGE_GAP = blue_line.DEFAULT_BRIDGE_GAP_M
DEFAULT_SKIP_REASON = {
    "蓝线": "默认不绘制（加 --enable-blue on 或 --blue-layers 指定后重跑）",
    "紫线": "默认不绘制（紫线需用户指定图层：--purple-layers 或 --confirm-layers）",
}


@dataclass
class LineResult:
    name: str
    layer: str
    source_text: str
    mode: str = "outline"
    layers: List[str] = field(default_factory=list)
    geometry: object = None
    face: object = None
    area_m2: float = 0.0
    rings_written: int = 0
    skipped: bool = False
    reason: str = ""
    warnings: List[str] = field(default_factory=list)
    info: Dict = field(default_factory=dict)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="城市控制线生成（子技能 N4，原「城市五线」）")
    cli.add_source_args(parser)
    cli.add_layer_args(parser)
    cli.add_output_args(parser)
    parser.add_argument("--line", action="append", default=None, help="逐条线指定来源，如 '绿线=code:1401,1402'（可重复）")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="控制线配置文件（院内/地方标准请用本参数指定）")
    parser.add_argument(
        "--accept-default-rules",
        action="store_true",
        help="明确同意按内置默认值（five_lines.json）执行；未加且配置仍为默认值时输出 five_lines_rules_source 确认项",
    )
    parser.add_argument("--rules-note", help="控制线口径依据说明（如规范条文号），写入报告与 JSON")
    parser.add_argument("--standard", default="auto", choices=["auto", "2023", "gb50137"], help="分类标准")
    parser.add_argument("--auto", default="on", choices=["on", "off"], help="是否叠加配置里的默认口径")
    parser.add_argument("--buffer", help="逐线缓冲距离，如 '绿线=10,蓝线=30'（米）")
    parser.add_argument("--building-layers", help="建筑图层，用于蓝线内建筑冲突校验")
    parser.add_argument("--min-area", type=float, default=1.0, help="参与生成的最小面积（㎡）")
    parser.add_argument("--check-tolerance", type=float, default=1.0, help="控制线互交判定容差（㎡）")
    parser.add_argument("--confirm-layers", action="store_true", help="只输出控制线图层候选与口径确认（退出码 3）")
    parser.add_argument("--layer-map", help="控制线图层指派表 JSON（确认结果），键：red/green/blue/yellow/purple/infra")
    parser.add_argument("--red-layers", help="红线图层（道路红线），逗号分隔")
    parser.add_argument("--green-layers", help="绿线图层，逗号分隔")
    parser.add_argument("--blue-layers", help="蓝线（水域/河道）图层，逗号分隔")
    parser.add_argument("--yellow-layers", help="黄线（公用设施）图层，逗号分隔")
    parser.add_argument("--purple-layers", help="紫线（历史保护）图层，逗号分隔")
    parser.add_argument("--infra-layers", help="基础设施线图层（轨道/水管/电力等），逗号分隔")
    parser.add_argument("--protect", help="基础设施保护范围，如 'GD-轨道线=15,GX-高压线=10'（米）")
    parser.add_argument("--enable-1402", default="on", choices=["on", "off"], help="绿线是否包含 1402 防护绿地，默认 on")
    parser.add_argument("--enable-blue", default="off", choices=["on", "off"], help="是否绘制蓝线，默认 off")
    parser.add_argument(
        "--bridge-gap",
        type=float,
        help="桥宽上限（米），默认 60；超过该值的桥位上下游不会连接。不指定时会先量出实测最远距离再询问用户",
    )
    parser.add_argument(
        "--bridge-mode",
        default=None,
        choices=["connect", "seal"],
        help="桥位口径：connect=A 贯通（默认）、seal=B 保持断开；不指定时遇歧义先询问用户",
    )
    parser.add_argument("--line-mode", choices=["copy", "outline"], help="红线输出模式覆盖，默认 copy")
    parser.add_argument("--dry-run", action="store_true", help="只出报告不写图")
    return parser.parse_args(argv)


def load_config(path: str) -> Dict:
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"控制线配置不存在：{target}")
    return json.loads(target.read_text(encoding="utf-8"))


def parse_line_args(items: Optional[Sequence[str]]) -> Dict[str, List[str]]:
    """--line "绿线=code:1401,1402;layer:绿地"：多条来源用分号分隔，代码内部逗号不拆。"""
    result: Dict[str, List[str]] = {}
    for item in items or []:
        text = str(item or "")
        if "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        if not key:
            continue
        specs = [part.strip() for part in value.replace("|", ";").split(";") if part.strip()]
        result[key] = specs
    return result


def parse_buffer_map(text: Optional[str]) -> Dict[str, float]:
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


def resolve_source(
    specs: Sequence[str],
    visible: Sequence[str],
    classifier,
    spec: Dict,
) -> Tuple[List[str], List[str], bool]:
    """返回 (命中图层, 说明, 是否 manual)。specs 是已按分号拆开的来源列表。"""
    if not specs:
        return [], [], False
    if any(str(item).strip().lower() in {"manual", "人工", "manual:true"} for item in specs):
        return [], ["人工指定"], True

    layers: List[str] = []
    notes: List[str] = []
    for token in specs:
        text = str(token or "").strip()
        if not text:
            continue
        if text.lower().startswith("layer:"):
            keyword = text.split(":", 1)[1].strip()
            if not keyword:
                continue
            matched = []
            for name in visible:
                target = name.lower()
                if any(ch in keyword for ch in "*?"):
                    if fnmatch.fnmatch(target, keyword.lower()):
                        matched.append(name)
                elif keyword.lower() in target:
                    matched.append(name)
            layers.extend(matched)
            notes.append(f"layer:{keyword} → {len(matched)} 个图层")
        elif text.lower().startswith("code:"):
            codes = [item.strip() for item in text.split(":", 1)[1].split(",") if item.strip()]
            matched = landuse.select_layers_by_code(visible, classifier, spec, codes)
            layers.extend(matched)
            notes.append(f"code:{','.join(codes)} → {len(matched)} 个图层")
        else:
            notes.append(f"无法识别的来源写法：{text}")
    return cli.dedupe(layers), notes, False


def resolve_line_sources(args, visible, classifier, spec: Dict, standard: str, mapping: Dict, config: Dict) -> Dict[str, List[str]]:
    """按 显式参数 > 指派表 > 代码/命名候选 决定每条线的来源图层。"""
    codes = (config.get("codes") or {}).get(standard) or {}
    spec_config = spec.get("five_lines") or {}
    spec_codes = (spec_config.get("codes") or {}).get(standard) or {}
    merged_codes = {**spec_codes, **codes}

    def clean(names: Sequence[str]) -> List[str]:
        return [str(name) for name in names if str(name) in visible]

    def by_map(key: str) -> List[str]:
        return clean(line_layers.layers_of(mapping, key))

    result: Dict[str, List[str]] = {}

    red = cli.parse_list(args.red_layers) or by_map("red")
    if not red:
        red = [layer for layer, _ in line_layers.candidates(visible, spec).get("road_redline", [])]
    result["红线"] = clean(red)

    for name, key, attr in (
        ("绿线", "green", "green_layers"),
        ("蓝线", "blue", "blue_layers"),
        ("黄线", "yellow", "yellow_layers"),
        ("紫线", "purple", "purple_layers"),
    ):
        explicit = cli.parse_list(getattr(args, attr))
        picked = explicit or by_map(key)
        if not picked:
            code_list = list(merged_codes.get(key) or [])
            if name == "绿线" and args.enable_1402 == "off":
                code_list = [item for item in code_list if item not in ("1402", "G2")]
            if code_list:
                picked = landuse.select_layers_by_code(visible, classifier, spec, code_list)
        result[name] = clean(picked)

    result["基础设施"] = clean(cli.parse_list(args.infra_layers) or by_map("infra"))
    return result


def collect_geometry(backend, layers: Sequence[str], factor: float, distance_m: float, min_area: float, unit_scale: float):
    """取闭合面；不足时用线段缝合成面；最后按需缓冲并合并。"""
    if not layers:
        return None, []
    warnings: List[str] = []
    found = shapes.collect_polygons(backend, layers, unit_factor=factor, source="auto", min_area_m2=min_area)
    if not found:
        segments = shapes.collect_segments(backend, layers)
        found = shapes.polygonize(segments, min_area_m2=min_area, unit_factor=factor)
        if found:
            warnings.append("图层仅有开放线，已用 polygonize 缝合成闭合面，请复核")
    if not found:
        return None, warnings
    geoms = [item.geometry for item in found if item.geometry is not None]
    if not geoms:
        return None, warnings
    merged = offset.union(geoms)
    if distance_m > 0 and merged is not None:
        merged = offset.expand(merged, distance_m / unit_scale, join="round")
    return merged, warnings


def collect_lines(backend, layers: Sequence[str]) -> Tuple[Optional[object], int]:
    """收集线实体（多段线 + 直线，圆弧按 2° 离散），返回 (合并几何, 实体数)。"""
    if not layers:
        return None, 0
    try:
        from shapely.geometry import LineString
        from shapely.ops import unary_union
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("缺少依赖 shapely，请先执行：python -m pip install shapely") from exc
    geoms = []
    for record in backend.iter_polylines(list(layers), None):
        points = discretize(record.vertices, record.bulges, close=record.closed_flag)
        if len(points) >= 2:
            geoms.append(LineString(points))
    for record in backend.iter_lines(list(layers), None):
        if len(record.vertices) == 2:
            geoms.append(LineString(record.vertices))
    if not geoms:
        return None, 0
    return unary_union(geoms), len(geoms)


def to_paths(geometry) -> List[Tuple[List[Tuple[float, float]], bool]]:
    """几何 → [(点串, 是否闭合)]，面取边界环，线取线本身。"""
    if geometry is None or getattr(geometry, "is_empty", True):
        return []
    kind = getattr(geometry, "geom_type", "")
    if kind == "Polygon":
        paths = []
        exterior = list(geometry.exterior.coords)[:-1] if geometry.exterior else []
        if len(exterior) >= 3:
            paths.append((exterior, True))
        for interior in geometry.interiors:
            ring = list(interior.coords)[:-1]
            if len(ring) >= 3:
                paths.append((ring, True))
        return paths
    if kind in {"MultiPolygon", "GeometryCollection"}:
        paths: List[Tuple[List[Tuple[float, float]], bool]] = []
        for item in geometry.geoms:
            paths.extend(to_paths(item))
        return paths
    if kind in {"LineString", "LinearRing"}:
        coords = [(float(p[0]), float(p[1])) for p in geometry.coords]
        return [(coords, False)] if len(coords) >= 2 else []
    if kind == "MultiLineString":
        paths = []
        for item in geometry.geoms:
            paths.extend(to_paths(item))
        return paths
    return []


def build_plan(args, config: Dict, standard: str, explicit: Dict[str, List[str]]) -> List[Dict]:
    """返回计划项：[{name, specs, distance, mode, enabled, manual}]。"""
    standard_config = (config.get("by_standard") or {}).get(standard) or {}
    buffer_map = parse_buffer_map(args.buffer)
    plan: List[Dict] = []
    for name in LINE_ORDER:
        default = standard_config.get(name) or {}
        specs = explicit.get(name)
        if specs is None:
            if args.auto == "off":
                continue
            if default.get("manual"):
                specs = ["manual"]
            else:
                specs = [str(item) for item in (default.get("source", []) or [])]
        plan.append(
            {
                "name": name,
                "specs": specs,
                "distance": buffer_map.get(name, float(default.get("buffer", 0.0) or 0.0)),
                "mode": args.line_mode if (name == "红线" and args.line_mode) else str(default.get("mode") or "outline"),
                "enabled": bool(default.get("enabled", True)),
                "manual": bool(default.get("manual")),
                "ask_codes": default.get("ask_codes") or [],
                "cross_road": bool(default.get("cross_road", True)),
                "bridge_gap": float(default.get("bridge_gap", 0.0) or 0.0),
                "bridge_mode": str(default.get("bridge_mode") or "connect"),
            }
        )
    for name, specs in explicit.items():
        if name not in LINE_ORDER:
            plan.append(
                {
                    "name": name,
                    "specs": specs,
                    "distance": buffer_map.get(name, 0.0),
                    "mode": "outline",
                    "enabled": True,
                    "manual": False,
                    "ask_codes": [],
                    "cross_road": True,
                    "bridge_gap": 0.0,
                }
            )
    return plan


def describe_config_source(args, config: Dict) -> Dict:
    """控制线配置来源：five_lines.json 只是草案（_status），口径直接决定线位，必须先确认。"""
    path = Path(str(args.config)).resolve()
    default_path = Path(str(DEFAULT_CONFIG)).resolve()
    status = str(config.get("_status") or "").strip()
    accepted = bool(getattr(args, "accept_default_rules", False))
    line_values: Dict[str, Dict] = {}
    for std, block in (config.get("by_standard") or {}).items():
        rows: Dict[str, Dict] = {}
        for name in LINE_ORDER:
            item = (block or {}).get(name) or {}
            rows[name] = {
                "mode": str(item.get("mode") or "outline"),
                "buffer_m": float(item.get("buffer", 0.0) or 0.0),
                "enabled": bool(item.get("enabled", True)),
                "source": item.get("source") or [],
            }
        line_values[str(std)] = rows
    checks = config.get("checks") or {}
    if path != default_path:
        source = "院内/地方标准文件"
    elif accepted:
        source = "按内置默认值（用户已确认）"
    else:
        source = "内置默认值（未确认）"
    needs_confirm = path == default_path and bool(status) and not accepted
    return {
        "file": str(path),
        "status": status,
        "source": source,
        "needs_confirm": needs_confirm,
        "accepted": accepted,
        "note": str(getattr(args, "rules_note", "") or ""),
        "checks": {
            "面积偏差阈值": f"{float(checks.get('area_deviation', 0.05)) * 100:.0f}%",
            "蓝线内建筑校验": bool(checks.get("building_conflict", True)),
            "控制线互交校验": bool(checks.get("mutual_intersect", True)),
        },
        "line_values": line_values,
        "bridge_defaults": {
            "bridge_gap_m": float(FALLBACK_BRIDGE_GAP),
            "bridge_mode": "A（贯通）",
        },
        "hint": (
            "内置默认值生成的线只能用于方案比较；确认后加 --accept-default-rules，"
            "或用 --config 指定院内文件、--buffer / --line / --protect 逐项覆盖。"
        ),
    }


def run(args) -> int:
    cli.setup_stdout()
    offset.require_shapely()
    spec = spec_mod.load_spec()
    config = load_config(args.config)
    explicit = parse_line_args(args.line)
    mapping = line_layers.load_map(args.layer_map)
    backend, unit, factor, unit_source = cli.open_backend(args)
    args.report_path = cli.resolve_out(args, backend, "控制线成果")
    try:
        visible = [name for name in cli.visible_layers(backend) if name not in set(cli.parse_list(args.exclude_layers))]
        guess = landuse.detect_standard(visible)
        standard = args.standard if args.standard != "auto" else (guess.get("standard") or "2023")
        classifier = landuse.build(standard, None, None)

        # 预检：图层指派与口径来源一次性问完，不要问一个问题重跑一遍
        pending: List[Dict] = []
        if args.confirm_layers:
            candidates = five_lines_layers.candidates(visible, classifier, spec, standard)
            pending.extend(
                [
                    confirm.item(
                        "five_line_assignment",
                        "控制线都涉及确定图层（线条没有字段映射表可依），请一次确认下列指派与口径。",
                        options=[
                            {"label": "按候选确认", "desc": "把选中的图层写入 layer-map.json，用 --layer-map 重跑"},
                            {"label": "逐线显式指定", "desc": "用 --red-layers / --green-layers / --blue-layers / --yellow-layers / --purple-layers"},
                            {"label": "蓝线不画", "desc": "默认不绘制蓝线；要画加 --enable-blue on"},
                            {"label": "紫线不画", "desc": "默认不绘制；要画用 --purple-layers 指定保护范围图层"},
                        ],
                        detail={
                            "candidates": candidates,
                            "questions": {
                                "绿线1402": "防护绿地 1402 是否画绿线？非交互默认画（--enable-1402 off 关闭）",
                                "蓝线": "是否绘制蓝线？默认不画（--enable-blue on 开启）",
                                "黄线设施线": "轨道/水管/电力等基础设施线是否画黄线？用 --infra-layers 指定图层",
                                "黄线保护范围": "基础设施是否有保护范围？有则用 --protect '图层=距离' 一并绘制",
                                "紫线": "紫线默认不画，需要时用 --purple-layers 指定保护范围图层",
                            },
                            "resolved_default": resolve_line_sources(args, visible, classifier, spec, standard, mapping, config),
                        },
                    )
                ]
            )

        config_source = describe_config_source(args, config)
        if config_source["needs_confirm"]:
            pending.extend(
                [
                    confirm.item(
                        "five_lines_rules_source",
                        (
                            f"当前控制线配置仍是「{config_source['status']}」，不是院内/地方标准。"
                            "线位与范围由配置里的来源、缓冲距离、是否成图决定，不同城市做法差异很大，"
                            "请先确认本次按哪套口径执行。"
                        ),
                        options=[
                            {
                                "id": "accept_draft",
                                "label": "按内置默认值执行",
                                "args": ["--accept-default-rules"],
                                "desc": "加 --accept-default-rules 复算，报告注明「按内置默认值，未经院内标准校准」",
                            },
                            {
                                "id": "manual",
                                "label": "逐项指定",
                                "args": [],
                                "requires_input": "line_flags",
                                "desc": "用 --line / --buffer / --protect / --enable-blue / --enable-1402 覆盖具体口径",
                            },
                            {
                                "id": "config_file",
                                "label": "用院内/地方标准文件",
                                "args": [],
                                "requires_input": "config_file",
                                "desc": "用 --config 路径.json 指定（结构同 assets/config/five_lines.json）",
                            },
                        ],
                        detail=config_source,
                    )
                ]
            )

        if pending:
            return confirm.emit(pending, round_no=1)
        plan = build_plan(args, config, standard, explicit)
        if not plan:
            return confirm.fail("empty_plan", "没有可生成的线：请用 --line 指定来源，或把 --auto 打开。")

        sources = resolve_line_sources(args, visible, classifier, spec, standard, mapping, config)
        protect_map = parse_buffer_map(args.protect)
        results: List[LineResult] = []
        manual_lines: List[str] = []
        blue_info: Dict = {}
        blue_allowed = (
            args.enable_blue == "on"
            or bool(cli.parse_list(args.blue_layers))
            or bool(line_layers.layers_of(mapping, "blue"))
            or "蓝线" in explicit
        )

        for item in plan:
            name = item["name"]
            layer_name = spec_mod.control_layer(spec, name)
            source_text = ";".join(item["specs"] or [])
            result = LineResult(name=name, layer=layer_name, source_text=source_text, mode=item["mode"])
            layers, notes, manual = resolve_source(item["specs"], visible, classifier, spec)
            if not layers:
                layers = sources.get(name, [])
            result.layers = layers
            result.warnings.extend(notes)

            if name == "蓝线" and not blue_allowed:
                result.skipped = True
                result.reason = DEFAULT_SKIP_REASON["蓝线"]
                results.append(result)
                continue
            if manual or (name == "紫线" and not item["enabled"] and not layers):
                result.skipped = True
                result.reason = "默认不绘制（紫线需用户指定图层）"
                manual_lines.append(name)
                results.append(result)
                continue
            if not layers:
                result.skipped = True
                result.reason = f"来源未命中任何图层（{source_text or '空'}）；可用 --confirm-layers 查看候选"
                results.append(result)
                continue

            geometry = None
            if name == "红线" and item["mode"] == "copy":
                geometry, count = collect_lines(backend, layers)
                if geometry is not None:
                    result.warnings.append(f"已按 copy 模式提取 {count} 条线实体")
                else:
                    face, warnings = collect_geometry(backend, layers, factor, 0.0, float(args.min_area), factor)
                    result.warnings.extend(warnings)
                    if face is not None:
                        geometry = face.boundary
                        result.warnings.append("红线图层没有独立线实体，已取闭合面边界")
                    result.info["face_fallback"] = True
            elif name == "蓝线" and item["mode"] == "bank":
                water, warnings = collect_geometry(backend, layers, factor, 0.0, float(args.min_area), factor)
                result.warnings.extend(warnings)
                if water is None:
                    result.skipped = True
                    result.reason = "水域图层没有闭合面，无法提取河口线"
                    results.append(result)
                    continue
                roads = None
                if item["cross_road"] and sources.get("红线"):
                    # 桥位判定要的是「道路红线位置」，不是红线围合的面：闭合红线多半是街区面
                    # （等效宽 60~200 m），拿它当路面会把整块水域判成「在道路内」，岸线全被吞掉。
                    road_lines, _line_count = collect_lines(backend, sources["红线"])
                    if road_lines is not None:
                        roads = road_lines
                    else:
                        roads, _ = collect_geometry(backend, sources["红线"], factor, 0.0, float(args.min_area), factor)
                bridge_mode = args.bridge_mode or item.get("bridge_mode") or "connect"
                if args.bridge_gap is None and bridge_mode != "seal" and roads is not None:
                    # 桥宽上限决定「哪些桥位能连起来」，必须由用户给：先量出本图最远配对距离再问。
                    measured = blue_line.measure_bridge_gap(water, roads, unit_scale=factor)
                    if measured.get("candidates"):
                        suggest = float(measured.get("suggest_bridge_gap") or 0.0)
                        return confirm.emit(
                            [
                                confirm.item(
                                    "blue_bridge_gap",
                                    (
                                        "蓝线桥位贯通需要先给一个桥宽上限（米）。本次按水域面实测："
                                        f"{measured['candidates']} 处桥位候选，最远配对距离"
                                        f" {measured['max_distance']:.1f} m，中位 {measured['median_distance']:.1f} m。"
                                        "注意：超过该值的桥位上下游不会连接（两岸各自断开）。"
                                        "按红线间距量取通常比水域面间距大（河岸有绿带），可自行输入更大值。"
                                    ),
                                    options=[
                                        {
                                            "label": f"用建议值 {suggest:.0f} m",
                                            "desc": "实测最远距离向上取整到 10 m，所有桥位都能连上",
                                        },
                                        {
                                            "label": f"用默认 {FALLBACK_BRIDGE_GAP:.0f} m",
                                            "desc": f"小于实测最远距离 {measured['max_distance']:.1f} m，超出部分桥位不会连接",
                                        },
                                        {"label": "我自定义", "desc": "回答一个数值（如 68），用 --bridge-gap 传入后重跑"},
                                    ],
                                    detail={
                                        "default": f"{FALLBACK_BRIDGE_GAP:.0f}",
                                        "max_distance": measured.get("max_distance"),
                                        "median_distance": measured.get("median_distance"),
                                        "candidates": measured.get("candidates"),
                                        "water_pieces": measured.get("water_pieces"),
                                        "distances_top": measured.get("distances"),
                                        "warning": "超过所给桥宽上限的桥位不会连接（上下游岸线各自断开）",
                                        "rerun": f"--bridge-gap {suggest:.0f}",
                                    },
                                )
                            ]
                        )
                gap = args.bridge_gap if args.bridge_gap else (item["bridge_gap"] or FALLBACK_BRIDGE_GAP)
                geometry, info = blue_line.build_bank_lines(
                    water,
                    roads,
                    unit_scale=factor,
                    bridge_gap_m=float(gap),
                    cross_road=bool(item["cross_road"]),
                    bridge_mode=bridge_mode,
                )
                if info.get("ambiguous") and args.bridge_mode is None:
                    # 认不出桥位断口时不替用户决定：默认 A（贯通），交给确认协议
                    suggest = float(info.get("suggest_bridge_gap") or 0.0)
                    return confirm.emit(
                        [
                            confirm.item(
                                "blue_bridge_mode",
                                (
                                    f"蓝线与道路红线共线 {info.get('kept_edges', 0)} 段，"
                                    f"{info.get('short_segments', 0)} 段短边未能配对成桥位断口"
                                    f"（河道宽约 {suggest:.0f} m，当前桥宽上限 {float(gap):.0f} m）。"
                                    "桥位处蓝线按哪种口径画？"
                                ),
                                options=[
                                    {"label": "A 贯通（默认）", "desc": "删桥位横切边，上下游两岸线两点直连，蓝线连续"},
                                    {"label": "B 保持断开", "desc": "按水域边界输出，桥位处两岸各自封口"},
                                ],
                                detail={
                                    "default": "A",
                                    "kept_edges": info.get("kept_edges", 0),
                                    "short_segments": info.get("short_segments", 0),
                                    "water_pieces": info.get("water_pieces", 0),
                                    "suggest_bridge_gap": suggest,
                                    "rerun_A": f'--bridge-mode connect --bridge-gap {suggest:.0f}',
                                    "rerun_B": "--bridge-mode seal",
                                },
                            )
                        ]
                    )
                blue_info = info
                result.info = info
                result.warnings.extend(info.get("notes", []))
                # 蓝线本身是河口线（线串），冲突校验与面积以水域面为准
                result.face = water
            else:
                geometry, warnings = collect_geometry(
                    backend, layers, factor, item["distance"], float(args.min_area), factor
                )
                result.warnings.extend(warnings)
                if geometry is not None and name in {"绿线", "黄线", "紫线"}:
                    result.face = geometry
                    outline = geometry.boundary
                    if name == "黄线" and sources.get("基础设施"):
                        for infra_layer in sources["基础设施"]:
                            line_geom, _ = collect_lines(backend, [infra_layer])
                            if line_geom is None:
                                continue
                            protect = float(protect_map.get(infra_layer, 0.0) or 0.0)
                            if protect > 0:
                                protected = offset.expand(line_geom, protect / factor, join="round")
                                outline = offset.union([outline, protected.boundary]) if outline is not None else protected.boundary
                                result.warnings.append(f"{infra_layer} 已按保护范围 {protect:g}m 绘制")
                            else:
                                outline = offset.union([outline, line_geom]) if outline is not None else line_geom
                    geometry = outline

            if geometry is None or getattr(geometry, "is_empty", True):
                result.skipped = True
                result.reason = "来源图层没有可用几何（面/线均未取到）"
                results.append(result)
                continue
            result.geometry = geometry
            try:
                area_geom = result.face if result.face is not None else geometry
                if getattr(area_geom, "geom_type", "") in {"Polygon", "MultiPolygon"}:
                    result.area_m2 = float(area_geom.area) * factor * factor
            except Exception:
                result.area_m2 = 0.0
            results.append(result)

        written = 0
        copy_path, copy_format, copy_note = None, "none", "未生成副本（--dry-run 或 --no-write-back）"
        if args.write_back and not args.dry_run:
            for result in results:
                if result.skipped or result.geometry is None:
                    continue
                definition = spec_mod.control_def(spec, result.name)
                backend.ensure_layer(result.layer, color=definition.get("color"), linetype=definition.get("linetype"))
                for points, closed in to_paths(result.geometry):
                    if len(points) < 2:
                        continue
                    backend.add_polyline(result.layer, points, closed=closed)
                    result.rings_written += 1
                    written += 1
            copy_path, copy_format, copy_note = cli.export_drawing(backend, args, "控制线")

        problems = check_conflicts(args, backend, results, visible, classifier, spec, factor, standard)
        payload = {
            "status": "ok",
            "config_source": config_source,
            "standard": standard,
            "unit": unit,
            "unit_source": unit_source,
            "layer_map": args.layer_map or "",
            "lines": [
                {
                    "name": result.name,
                    "layer": result.layer,
                    "mode": result.mode,
                    "source": result.source_text,
                    "source_layers": result.layers,
                    "area_m2": round(result.area_m2, 2),
                    "rings": result.rings_written,
                    "skipped": result.skipped,
                    "reason": result.reason,
                    "warnings": result.warnings,
                    "info": result.info,
                }
                for result in results
            ],
            "blue": blue_info,
            "manual_lines": manual_lines,
            "issues": problems,
            "written": written,
            "copy_out": copy_path or "",
            "copy_format": copy_format,
            "copy_note": copy_note,
        }
        cli.write_json(args.json_path, payload)
        _write_report(args, payload, results, problems, spec, unit, unit_source)
        if copy_path:
            print(f"[成果] {copy_note}")
        print(f"[结果] 控制线生成 {len([r for r in results if not r.skipped])} 条，问题 {len(problems)} 条，写入线段 {written} 条。")
        return confirm.EXIT_OK
    finally:
        try:
            backend.close()
        except Exception:
            pass


def _region_of(geometry, tol_m: float = 1.0, factor: float = 1.0):
    """线型管制线 → 围合区域（优先 polygonize 闭合线，失败则按米缓冲）。"""
    if geometry is None or getattr(geometry, "is_empty", True):
        return None
    if getattr(geometry, "geom_type", "") in {"Polygon", "MultiPolygon"}:
        return geometry
    try:
        from shapely.ops import polygonize, unary_union

        faces = list(polygonize(geometry))
        if faces:
            return unary_union(faces)
    except Exception:
        pass
    try:
        tol = tol_m / factor if factor else tol_m
        return geometry.buffer(max(float(tol), 0.0))
    except Exception:
        return None


def check_conflicts(args, backend, results, visible, classifier, spec, factor, standard: str) -> List[Dict]:
    problems: List[Dict] = []
    by_name = {item.name: item for item in results}

    blue = by_name.get("蓝线")
    blue_region = None
    if blue is not None:
        blue_geom = blue.face if blue.face is not None else blue.geometry
        blue_region = _region_of(blue_geom, factor=factor)
    building_layers = [name for name in cli.parse_list(args.building_layers)] if args.building_layers else [
        name for name in visible if spec_mod.layer_in_group(name, spec, "building")
    ]
    # 没有任何建筑图层时不能落进 collect_polygons(backend, [])：空列表会被当成“不过滤”，
    # 把全图地块当成建筑，产生成片的假冲突。
    if blue is not None and blue_region is not None and building_layers:
        # 同层填充与多段线重复描同一栋时只算一次，避免蓝线内建筑重复告警
        buildings = shapes.collect_polygons(
            backend, building_layers, unit_factor=factor, source="both", dedupe=True
        )
        for building in buildings:
            if building.geometry is None:
                continue
            if offset.intersects(blue_region, building.geometry):
                problems.append(
                    {
                        "kind": "building_in_blue",
                        "message": "蓝线（水域控制线）范围内有建筑，需核实是否允许保留",
                        "handle": building.handle,
                        "layer": building.layer,
                    }
                )

    tolerance = float((spec.get("checks") or {}).get("area_deviation", 0.05) or 0.05)
    green_codes = ["1401", "1402"] if standard != "gb50137" else ["G1", "G2"]
    road_codes = ["1207"] if standard != "gb50137" else ["S1"]

    green = by_name.get("绿线")
    if green is not None and green.geometry is not None and green.area_m2 > 0:
        land_area = code_area(backend, visible, classifier, spec, green_codes, factor)
        if land_area > 0:
            deviation = abs(green.area_m2 - land_area) / land_area
            if deviation > tolerance:
                problems.append(
                    {
                        "kind": "green_area_deviation",
                        "message": f"绿线面积与绿地类用地面积偏差 {deviation * 100:.1f}%（绿线 {green.area_m2:.2f} ㎡ / 绿地 {land_area:.2f} ㎡）",
                        "handle": "",
                        "layer": green.layer,
                    }
                )

    red = by_name.get("红线")
    if red is not None and red.geometry is not None and red.area_m2 > 0:
        land_area = code_area(backend, visible, classifier, spec, road_codes, factor)
        if land_area > 0:
            deviation = abs(red.area_m2 - land_area) / land_area
            if deviation > tolerance:
                problems.append(
                    {
                        "kind": "red_area_deviation",
                        "message": f"红线面积与道路用地面积偏差 {deviation * 100:.1f}%（红线 {red.area_m2:.2f} ㎡ / 道路 {land_area:.2f} ㎡）",
                        "handle": "",
                        "layer": red.layer,
                    }
                )

    tol = max(float(args.check_tolerance), 0.0)
    names = [item for item in results if item.geometry is not None and not item.skipped]
    for index in range(len(names)):
        for other in names[index + 1:]:
            area = offset.intersection_area(names[index].geometry, other.geometry)
            if area * factor * factor > tol:
                problems.append(
                    {
                        "kind": "lines_intersect",
                        "message": f"{names[index].name} 与 {other.name} 存在重叠 {area * factor * factor:.2f} ㎡，需核实管制范围口径",
                        "handle": "",
                        "layer": f"{names[index].layer} / {other.layer}",
                    }
                )
    return problems


def code_area(backend, layers: Sequence[str], classifier, spec: Dict, codes: Sequence[str], factor: float) -> float:
    targets = landuse.select_layers_by_code(layers, classifier, spec, list(codes))
    if not targets:
        return 0.0
    found = shapes.collect_polygons(backend, targets, unit_factor=factor, source="auto")
    geoms = [item.geometry for item in found if item.geometry is not None]
    if not geoms:
        return sum(item.area_m2 for item in found)
    # 基准必须先去掉跨图层重叠再求面积：同一片绿地常被「用地填充 + 地块线」重复表达，
    # 直接累加会把基准抬高一半，让正常的控制线报出大面积偏差。
    merged = offset.union(geoms)
    if merged is None:
        return sum(item.area_m2 for item in found)
    return float(merged.area) * factor * factor


def _write_report(args, payload, results, problems, spec, unit, unit_source) -> None:
    out = getattr(args, "report_path", None)
    if not out:
        return
    status = spec_mod.spec_status(spec)
    config_source = payload.get("config_source") or {}
    note_lines: List[str] = []
    if status:
        note_lines.append(f"> 注：{status}。")
    if config_source:
        if str(config_source.get("source", "")).startswith("按内置默认值"):
            note_lines.append(
                "> 口径依据：**按内置默认值（用户已确认）** five_lines.json；"
                "未经院内/地方标准校准，仅可用于方案比较，**不得直接作为报建依据**。"
            )
        elif config_source.get("source") == "院内/地方标准文件":
            note_lines.append(f"> 口径依据：**院内/地方标准文件** `{config_source.get('file')}`。")
        if config_source.get("note"):
            note_lines.append(f"> 依据说明：{config_source.get('note')}")
    note = ("\n".join(note_lines) + "\n") if note_lines else ""
    blue = payload.get("blue") or {}
    sections = [
        (
            "二、控制线成果",
            md_table(
                ["线名", "图层", "模式", "来源", "命中图层", "面积(㎡)", "写入线段", "状态"],
                [
                    (
                        item.name,
                        item.layer,
                        item.mode,
                        item.source_text or "-",
                        len(item.layers),
                        f"{item.area_m2:.2f}" if item.area_m2 else "线",
                        item.rings_written,
                        "已生成" if not item.skipped else f"跳过：{item.reason}",
                    )
                    for item in results
                ],
            ),
        ),
        (
            "三、蓝线断口处理",
            md_table(
                ["桥位口径", "桥宽上限(m)", "桥位断口", "重连处数", "贴河保留段", "水域块数", "说明"],
                [
                    (
                        "A 贯通" if blue.get("bridge_mode") == "connect" else "B 保持断开",
                        f"{float(blue.get('bridge_gap_m') or 0):.0f}",
                        blue.get("short_segments", 0),
                        blue.get("connectors", 0),
                        blue.get("kept_edges", 0),
                        blue.get("water_pieces", 0),
                        "；".join(blue.get("notes", [])) or "无",
                    )
                ],
            )
            if blue
            else "本次未生成蓝线。",
        ),
        (
            "四、校验与冲突",
            md_table(
                ["类型", "说明", "句柄", "图层"],
                [(item.get("kind", ""), item.get("message", ""), item.get("handle", ""), item.get("layer", "")) for item in problems],
            )
            if problems
            else "无。",
        ),
        ("附：句柄定位", locator_note()),
    ]
    meta = [
        (
            "口径依据",
            f"{payload['config_source']['source']}｜{Path(str(payload['config_source']['file'])).name}"
            + (f"｜{payload['config_source']['note']}" if payload["config_source"].get("note") else "")
            if payload.get("config_source")
            else "-",
        ),
        ("分类标准", payload.get("standard")),
        ("图纸单位", f"{unit}（{unit_source}）"),
        ("图层指派表", payload.get("layer_map") or "未使用（建议 --confirm-layers 确认后复用）"),
        ("人工线", "、".join(payload.get("manual_lines", [])) or "无"),
        ("写入结果", f"{payload.get('written', 0)} 条线" if payload.get("written") else "未写入"),
        ("成果副本", payload.get("copy_note") or "未生成（--dry-run 或 --no-write-back）"),
    ]
    write_markdown(out, "城市控制线成果", meta, sections, status_note=note)


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
