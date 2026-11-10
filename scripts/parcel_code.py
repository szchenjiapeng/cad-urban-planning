#!/usr/bin/env python
"""子技能 N2：地块编号批量生成。

流程：取数 → 识别用地图层与代码（委托 core.classify）→ 收集闭合地块 →
排序 → 生成编号 → 写回标注（副本）→ 校验重号/漏号 → 出编码对照表。

硬性：
- 代码解析不另写规则，全部走 LayerMapper；未登记代码必须确认；
- 编号必须可复现：同一输入两次运行结果完全一致；
- 不覆盖原图，DXF 通道只写 `*_coded.dxf` 副本。
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import cli, confirm, labels  # noqa: E402
from lib import landuse, shapes  # noqa: E402
from lib import layer_spec as spec_mod  # noqa: E402
from lib.report import locator_note, md_table, write_markdown  # noqa: E402

_SKIP_GROUPS = ("label", "frame", "control", "existing")

# 默认编号规则：序号在前、用地代码在后（如 001-1402）
DEFAULT_TEMPLATE = "{seq:03d}-{code}"
CODE_TEMPLATE = "{code}"
DEFAULT_LABEL_LAYER_SEQ = "BZ-地块编号"
DEFAULT_LABEL_LAYER_CODE = "BZ-用地代码"


@dataclass
class CodedParcel:
    shape: shapes.ShapeRec
    code: str
    name: str
    order: int = 0
    block: int = 0
    label: str = ""
    point: Tuple[float, float] = (0.0, 0.0)
    warnings: List[str] = field(default_factory=list)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="地块编号批量生成（子技能 N2）")
    cli.add_source_args(parser)
    cli.add_layer_args(parser)
    cli.add_output_args(parser)
    parser.add_argument("--standard", default="auto", choices=["auto", "2023", "gb50137"], help="分类标准")
    parser.add_argument("--mapping", help="自定义图层映射 JSON")
    parser.add_argument("--classification", help="自定义分类树 JSON")
    parser.add_argument("--mode", default="seq", choices=["seq", "code", "none"], help="seq=编号；code=只写用地代码不编号；none=不写标注")
    parser.add_argument("--layer-mode", default="keep", choices=["keep", "code"], help="keep=保持原图层；code=把地块图元移到按代码命名的新图层")
    parser.add_argument("--code-layer-template", default="YD-{code}", help="--layer-mode code 时的图层模板，支持 {code}{name}")
    parser.add_argument("--template", default=None, help="编号模板，支持 {code}{name}{seq}{block}{prefix}；不给则先询问")
    parser.add_argument("--prefix", default="", help="编号统一前缀")
    parser.add_argument("--sort", default="row", choices=["row", "block", "area", "handle"], help="排序规则")
    parser.add_argument("--block-grid", type=float, default=200.0, help="街坊网格边长（米），sort=block 时用于分区")
    parser.add_argument("--start", type=int, default=1, help="起始序号")
    parser.add_argument("--label-layer", help="标注图层；mode=seq 默认 BZ-地块编号，mode=code 默认 BZ-用地代码")
    parser.add_argument("--label-height", type=float, default=3.0, help="编号字高（米）")
    parser.add_argument("--label-gap", type=float, default=0.0, help="编号之间最小间距（米），0 不避让")
    parser.add_argument("--text-style", help="文字样式名；不给则沿用图纸默认样式")
    parser.add_argument("--font", help="字体文件，如 simfang.ttf 或 gbcbig.shx；给了字体会自动建样式")
    parser.add_argument("--bigfont", help="SHX 大字体，如 gbcbig.shx（配合 --font）")
    parser.add_argument("--width-factor", type=float, default=None, help="文字宽度因子，如 0.7 可让窄地块放下")
    parser.add_argument("--min-area", type=float, default=1.0, help="小地块判定阈值（㎡）；低于此值会先询问")
    parser.add_argument("--small-parcel-policy", default="ask", choices=["ask", "number", "skip"], help="小于阈值地块：ask 询问 / number 照常编号 / skip 跳过")
    parser.add_argument("--short-code", default="off", choices=["on", "off"], help="是否把纯两位数字图层当用地图层")
    parser.add_argument("--unknown-code", default="ask", choices=["ask", "seq_only", "skip"], help="代码未识别时的处理")
    parser.add_argument("--unregistered-code", action="append", default=None, help="未登记代码处理：CODE=skip 或 CODE=map:目标")
    parser.add_argument("--unknown-placeholder", default="X", help="代码缺失时编号里的占位符")
    parser.add_argument("--scope-layer", help="只对该图层围合的规划范围编号")
    parser.add_argument("--codes-file", help="已有编码表（CSV），用于续编避免重号")
    parser.add_argument("--dry-run", action="store_true", help="只出表，不写图、不导出")
    return parser.parse_args(argv)


def _parse_overrides(items: Optional[Sequence[str]]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for item in items or []:
        text = str(item or "")
        if "=" not in text:
            continue
        key, value = text.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def _pick_layers(args, backend, spec, classifier=None) -> List[str]:
    visible = cli.visible_layers(backend)
    exclude = set(cli.parse_list(args.exclude_layers))

    def ignored(name: str) -> bool:
        if name in exclude or name.lower() in {"0", "defpoints"}:
            return True
        if any(spec_mod.layer_in_group(name, spec, group) for group in _SKIP_GROUPS):
            return True
        if classifier is not None:
            try:
                return bool(classifier.mapper.is_ignored(name))
            except Exception:
                return False
        return False

    if args.layers:
        wanted = [name for name in cli.parse_list(args.layers)]
        return [name for name in wanted if name in visible and not ignored(name)]
    return [name for name in visible if not ignored(name)]


def _standard_items(args, backend) -> List[Dict]:
    """旧标准识别：与编号规范一起在扫描前问，合并成一轮。"""
    if args.standard != "auto":
        return []
    layers = cli.visible_layers(backend)
    guess = landuse.detect_standard(layers)
    if guess.get("standard") != "gb50137" or guess.get("confidence") not in {"high", "medium"}:
        return []
    return [
        confirm.item(
            "standard_choice",
            "图纸图层出现成片 GB 50137 旧代码（如 R2/A33/B1），请确认按哪套标准编号。",
            options=[
                {
                    "id": "gb50137",
                    "label": "按 GB50137 统计（编号用旧代码）",
                    "desc": "沿用旧标准，不换算",
                    "args": ["--standard", "gb50137"],
                },
                {
                    "id": "convert",
                    "label": "换算为 2023 指南后再编号",
                    "desc": "需 --standard gb50137 并指定换算表",
                    "args": [],
                    "requires_input": "classification",
                },
                {
                    "id": "guide2023",
                    "label": "按 2023 指南（图层名可能需修正）",
                    "desc": "坚持新标准",
                    "args": ["--standard", "2023"],
                },
            ],
            detail={"old": guess.get("old", []), "new": guess.get("new", [])},
            title="用地分类标准",
            why="两套标准的代码不同，代码列与编号结果会整体不同。",
            recommended="gb50137",
        )
    ]


def _number_rule_items(args) -> List[Dict]:
    """编号规范不依赖几何，必须在扫描前问，不许等算完再回头问。"""
    if args.mode != "seq" or args.template is not None:
        return []
    return [
        confirm.item(
            "number_rule",
            f"图纸有没有地块编号规范？技能默认是 {DEFAULT_TEMPLATE}，例如 001-1402。",
            options=[
                {
                    "id": "default",
                    "label": f"用默认 {DEFAULT_TEMPLATE}（001-1402）",
                    "desc": '加 --template "{seq:03d}-{code}"',
                    "args": ["--template", DEFAULT_TEMPLATE],
                },
                {
                    "id": "custom",
                    "label": "我提供编号规则",
                    "desc": "告知前缀/街坊号/位数，用 --template 指定",
                    "args": [],
                    "requires_input": "template",
                },
                {
                    "id": "code_only",
                    "label": "不编号，只写用地代码",
                    "desc": "加 --mode code",
                    "args": ["--mode", "code"],
                },
            ],
            detail={"default_template": DEFAULT_TEMPLATE},
            title="编号规范",
            recommended="default",
        )
    ]


def _collect(args, backend, spec, classifier, unit_factor) -> Tuple[List[str], List[Dict], List[Dict]]:
    """返回 (采用的地块图层, 未识别图层, 需确认项)。"""
    candidate_layers = _pick_layers(args, backend, spec, classifier)
    infos = landuse.inspect_layers(candidate_layers, classifier, spec, allow_short_code=args.short_code == "on")
    overrides = _parse_overrides(args.unregistered_code)

    pending: List[Dict] = []
    accepted: List[str] = []
    unregistered_rows: List[Dict] = []
    unknown_rows: List[Dict] = []
    unparceled_rows: List[Dict] = []
    override_skip: set = set()

    for info in infos:
        if info.unregistered:
            targets = [code for code in info.unregistered if code not in overrides]
            for code in targets:
                unregistered_rows.append({"layer": info.layer, "code": code})
            handled = [code for code in info.unregistered if code in overrides]
            for code in handled:
                value = overrides[code]
                if value.startswith("map:"):
                    info.code = value.split(":", 1)[1].strip()
                    info.is_parcel = True
                else:
                    info.is_parcel = True
                    info.code = None
                    info.reason = "未登记代码，本次按需复核处理"
                    override_skip.add(info.layer)
            if targets:
                continue
            if not info.is_parcel:
                unparceled_rows.append({"layer": info.layer, "reason": info.reason or "未识别为用地图层"})
                continue
        if not info.is_parcel:
            unparceled_rows.append({"layer": info.layer, "reason": info.reason or "未识别为用地图层"})
            continue
        if not info.code and info.layer not in override_skip:
            if args.unknown_code in {"skip", "ask"}:
                unknown_rows.append({"layer": info.layer, "reason": info.reason or "代码未识别"})
                continue
        accepted.append(info.layer)
        if not info.code and info.layer not in override_skip:
            unknown_rows.append({"layer": info.layer, "reason": "代码未识别，按 seq_only 只编号"})

    if unregistered_rows:
        options = []
        for row in unregistered_rows[:8]:
            options.append(
                {
                    "label": f"新增用地代码 {row['code']}",
                    "desc": "先执行 python scripts/add_classification_node.py 登记，再重跑",
                }
            )
        options.append({"label": "图层名写错，先回 CAD 改图层名", "desc": "改完重跑"})
        options.append(
            {
                "label": "归入已有代码",
                "desc": "用 --unregistered-code 0905=map:0902 指定",
            }
        )
        options.append(
            {
                "label": "本次跳过该图层不编号",
                "desc": "用 --unregistered-code 0905=skip，报告中标注需复核",
            }
        )
        pending.append(
            confirm.item(
                "unregistered_code",
                "以下图层名含形似分类代码、但分类表里没有登记的片段，请确认如何处理。",
                options=options,
                detail={"rows": unregistered_rows[:20]},
            )
        )
    if unknown_rows and args.unknown_code == "ask":
        pending.append(
            confirm.item(
                "unknown_code",
                "以下用地图层已识别为地块，但无法确定用地代码（编号仍可生成，代码列留空）。",
                options=[
                    {"label": "继续编号，代码列留空", "desc": "加 --unknown-code seq_only"},
                    {"label": "跳过这些图层", "desc": "加 --unknown-code skip"},
                    {"label": "先修正图层名再重跑", "desc": "推荐：图层名直接写分类代码"},
                ],
                detail={"layers": unknown_rows[:20]},
            )
        )
    return accepted, unparceled_rows, pending


def _scope_geometry(backend, spec, unit_factor, scope_layer: Optional[str]):
    """规划范围：只取指定图层的闭合轮廓并取并集。"""
    if not scope_layer:
        return None
    scope_shapes = shapes.collect_polygons(backend, [scope_layer], unit_factor=unit_factor, source="both")
    if not scope_shapes:
        raise RuntimeError(f"范围图层 {scope_layer} 没有闭合轮廓，无法按范围编号")
    geoms = [item.geometry for item in scope_shapes if item.geometry is not None]
    if not geoms:
        return None
    return landuse_union(geoms)


def landuse_union(geoms):
    try:
        from shapely.ops import unary_union

        return unary_union(geoms)
    except Exception:
        return geoms[0]


def order_records(records: List[CodedParcel], mode: str, grid: float) -> None:
    """排序必须可复现：所有键都带 handle 兜底。"""
    if mode in {"row", "block"}:
        cell = max(float(grid), 1e-6)

        def block_key(item: CodedParcel):
            cx, cy = item.shape.centroid()
            return (math.floor(cy / cell), math.floor(cx / cell))

        blocks = {}
        for item in records:
            blocks.setdefault(block_key(item), []).append(item)
        ordered_blocks = sorted(blocks.items(), key=lambda pair: (-pair[0][0], pair[0][1]))
        flat: List[CodedParcel] = []
        for index, (_key, items) in enumerate(ordered_blocks, start=1):
            items.sort(key=lambda item: (-item.shape.centroid()[1], item.shape.centroid()[0], item.shape.handle))
            for item in items:
                item.block = index
            flat.extend(items)
        records[:] = flat
        return
    if mode == "area":
        records.sort(key=lambda item: (-item.shape.area_m2, item.shape.handle))
        return
    records.sort(key=lambda item: item.shape.handle)
    for index, item in enumerate(records, start=1):
        item.block = index


def build_codes(records: Sequence[CodedParcel], args, template: str) -> List[Dict]:
    counter = max(1, int(args.start))
    per_block: Dict[int, int] = {}
    existing = _load_existing_codes(args.codes_file)
    used = set(existing)
    issues: List[Dict] = []
    pure_code_mode = args.mode == "code"

    for item in records:
        code_text = item.code or args.unknown_placeholder
        if args.sort == "block":
            seq = per_block.get(item.block, max(1, int(args.start)))
            per_block[item.block] = seq + 1
        else:
            seq = counter
            counter += 1
        try:
            label = template.format(
                code=code_text,
                name=item.name or item.shape.layer,
                seq=seq,
                block=item.block,
                prefix=args.prefix,
            )
        except Exception as exc:
            raise RuntimeError(f"编号模板无法解析：{template}（{exc}）") from exc
        if args.prefix and args.prefix not in label:
            label = f"{args.prefix}{label}"
        # 只写代码模式下，同代码重复是正常的，不做重号判定
        if not pure_code_mode and label in used:
            issues.append({"kind": "duplicate", "label": label, "handle": item.shape.handle, "layer": item.shape.layer})
        used.add(label)
        item.label = label
        item.order = seq
    return issues


def _load_existing_codes(path: Optional[str]) -> List[str]:
    if not path:
        return []
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"编码表不存在：{target}")
    codes: List[str] = []
    lines = target.read_text(encoding="utf-8-sig").splitlines()
    for index, line in enumerate(lines):
        cells = [cell.strip() for cell in line.split(",")]
        if not cells:
            continue
        if index == 0 and cells[0].lower() in {"编号", "code", "label", "地块编号"}:
            continue
        if cells[0]:
            codes.append(cells[0])
    return codes


def validate(records: Sequence[CodedParcel], issues: List[Dict], mode: str) -> List[Dict]:
    problems: List[Dict] = list(issues)
    seen: Dict[str, str] = {}
    for item in records:
        if mode != "code":
            if item.label in seen:
                problems.append(
                    {
                        "kind": "duplicate",
                        "message": f"编号重复：{item.label}",
                        "handle": item.shape.handle,
                        "layer": item.shape.layer,
                    }
                )
            seen[item.label] = item.shape.handle
        else:
            if not item.code:
                problems.append(
                    {
                        "kind": "missing_code",
                        "message": "地块无法确定用地代码，无法只写代码",
                        "handle": item.shape.handle,
                        "layer": item.shape.layer,
                    }
                )
        if item.shape.geometry is not None and not item.shape.geometry.is_valid:
            problems.append(
                {
                    "kind": "invalid_geometry",
                    "message": "多段线自交或几何无效",
                    "handle": item.shape.handle,
                    "layer": item.shape.layer,
                }
            )
    return problems


def _export_result(backend, args, spec) -> Tuple[Optional[str], str, str]:
    """按「默认 DWG、无 CAD 回退 DXF、永不覆盖原图」导出成果副本。"""
    suffix = "地块编号" if args.mode == "seq" else ("用地代码" if args.mode == "code" else "用地分图层")
    return cli.export_drawing(backend, args, suffix)


def run(args) -> int:
    cli.setup_stdout()
    spec = spec_mod.load_spec()
    backend, unit, factor, unit_source = cli.open_backend(args)
    args.report_path = cli.resolve_out(args, backend, "地块编码成果")
    try:
        # 预检：分类标准与编号规范都不依赖几何，合并成一轮一起问，
        # 避免「先问编号规范 → 重跑 → 算完再问标准」的来回。
        pending: List[Dict] = []
        pending.extend(_standard_items(args, backend))
        pending.extend(_number_rule_items(args))
        if pending:
            return confirm.emit(pending, round_no=1)

        standard = args.standard
        if standard == "auto":
            guess = landuse.detect_standard(cli.visible_layers(backend))
            standard = guess.get("standard") or "2023"
        classifier = landuse.build(standard, args.classification, args.mapping)

        accepted, unparceled, layer_issues = _collect(args, backend, spec, classifier, factor)
        # 不在这里立即返回：继续扫几何，把「图层未登记」与「小地块」合并成同一轮，
        # 否则同一份图会被打断两次、多跑两遍扫描。
        pending.extend(layer_issues)

        scope = _scope_geometry(backend, spec, factor, args.scope_layer)
        raw, skipped = shapes.collect_polygons_detail(
            backend,
            accepted,
            layer_patterns=args.layer_pattern or None,
            unit_factor=factor,
            source="auto",
            min_area_m2=float(args.min_area),
        )
        if not raw and not skipped:
            # 有疑问优先问，别用「没找到地块」掩盖「图层代码没登记」这个真实原因
            if pending:
                return confirm.emit(pending, round_no=2)
            return confirm.fail(
                "no_parcel",
                "没有找到可编号的闭合地块。请确认图层名是否含用地代码，或地块是否用闭合多段线/填充表达。",
            )

        # 小于阈值的地块必须由用户决定，不能默认丢弃
        small = [item for item in skipped if float(item.get("area_m2") or 0) > 0]
        if small and args.small_parcel_policy == "ask":
            options = [
                {
                    "id": "number",
                    "label": "照常编号",
                    "desc": "加 --small-parcel-policy number，小地块一并编号",
                    "args": ["--small-parcel-policy", "number"],
                },
                {
                    "id": "skip",
                    "label": "不编号，仅列入清单",
                    "desc": "加 --small-parcel-policy skip",
                    "args": ["--small-parcel-policy", "skip"],
                },
                {
                    "id": "adjust",
                    "label": "调整阈值后重跑",
                    "desc": "改 --min-area，例如 --min-area 0.1",
                    "args": [],
                    "requires_input": "min_area",
                },
            ]
            pending.append(
                confirm.item(
                    "small_parcels",
                    f"有 {len(small)} 个地块面积小于 {args.min_area:g} ㎡，是否参与编号？",
                    options=options,
                    detail={"threshold": args.min_area, "items": small[:30]},
                    title="小地块处理",
                    why="不静默丢弃：面积异常小的地块可能是边角地或线宽误差，取决于出图口径。",
                    recommended="number",
                )
            )

        if pending:
            return confirm.emit(pending, round_no=2)
        if small and args.small_parcel_policy == "number":
            raw, skipped = shapes.collect_polygons_detail(
                backend,
                accepted,
                layer_patterns=args.layer_pattern or None,
                unit_factor=factor,
                source="auto",
                min_area_m2=0.0,
            )
            small = []

        # 编号规范已在扫描前与分类标准合并询问，此处不再重复打断

        # 只写代码 / 不写标注时忽略编号模板，避免把序号带进去
        template = (args.template or DEFAULT_TEMPLATE) if args.mode == "seq" else CODE_TEMPLATE

        layer_code = {}
        for name in accepted:
            info = landuse.inspect_layer(name, classifier, spec, allow_short_code=args.short_code == "on")
            layer_code[name] = info

        records: List[CodedParcel] = []
        excluded_out_of_scope = 0
        for shape in raw:
            info = layer_code.get(shape.layer)
            code = info.code if info and info.code else ""
            display = info.name if info and info.name else ""
            shape.code = code or None
            shape.name = display
            if scope is not None and shape.geometry is not None:
                if not scope.intersects(shape.geometry):
                    excluded_out_of_scope += 1
                    continue
            records.append(CodedParcel(shape=shape, code=code, name=display))

        if not records:
            return confirm.fail("empty_scope", "规划范围内没有地块，请检查 --scope-layer 是否选错。")

        order_records(records, args.sort, args.block_grid)
        code_issues = build_codes(records, args, template)
        problems = validate(records, code_issues, args.mode)

        if args.mode == "seq":
            label_layer = args.label_layer or spec_mod.label_layer(spec) or DEFAULT_LABEL_LAYER_SEQ
        else:
            label_layer = args.label_layer or DEFAULT_LABEL_LAYER_CODE

        occupied: List[Tuple[float, float]] = []
        for item in records:
            if item.shape.geometry is not None:
                point = labels.place(item.shape.geometry, occupied, min_gap=float(args.label_gap))
            else:
                point = labels.fallback_point(item.shape.rings)
            item.point = point
            occupied.append(point)

        moved = 0
        move_failed: List[Dict] = []
        if args.layer_mode == "code" and not args.dry_run:
            for item in records:
                target_layer = args.code_layer_template.format(
                    code=item.code or args.unknown_placeholder,
                    name=item.name or "",
                ).strip()
                try:
                    ok = backend.set_entity_layer(item.shape.handle, target_layer)
                except NotImplementedError:
                    ok = False
                if ok:
                    moved += 1
                else:
                    move_failed.append(
                        {"kind": "move_failed", "message": f"无法移到图层 {target_layer}", "handle": item.shape.handle, "layer": item.shape.layer}
                    )
            problems.extend(move_failed)

        written = None
        export_format = "none"
        export_note = "未导出（--dry-run / --no-write-back / --export none）"
        style_name = args.text_style
        if args.font and not style_name:
            style_name = "地块编号" if args.mode == "seq" else "用地代码"
        style_applied = False
        if style_name and (args.font or args.bigfont):
            try:
                style_applied = backend.ensure_text_style(style_name, args.font, args.bigfont)
            except Exception:
                style_applied = False

        if not args.dry_run and args.write_back and args.export != "none":
            if args.mode != "none":
                backend.ensure_layer(label_layer)
                for item in records:
                    backend.add_text(
                        label_layer,
                        item.point,
                        item.label,
                        height=float(args.label_height),
                        style=style_name,
                        width_factor=args.width_factor,
                    )
            written, export_format, export_note = _export_result(backend, args, spec)

        payload = {
            "status": "ok",
            "standard": standard,
            "unit": unit,
            "unit_source": unit_source,
            "unit_factor": factor,
            "mode": args.mode,
            "template": template,
            "layer_mode": args.layer_mode,
            "moved": moved,
            "export_format": export_format,
            "export_note": export_note,
            "count": len(records),
            "excluded_out_of_scope": excluded_out_of_scope,
            "label_layer": label_layer,
            "text_style": style_name or "",
            "font": args.font or "",
            "bigfont": args.bigfont or "",
            "width_factor": args.width_factor,
            "style_applied": style_applied,
            "written": written,
            "unparceled": unparceled,
            "skipped": skipped,
            "parcels": [
                {
                    "label": item.label,
                    "handle": item.shape.handle,
                    "layer": item.shape.layer,
                    "code": item.code,
                    "name": item.name,
                    "area_m2": round(item.shape.area_m2, 3),
                    "point": [round(item.point[0], 3), round(item.point[1], 3)],
                    "block": item.block,
                    "source": item.shape.source_kind,
                }
                for item in records
            ],
            "issues": problems,
        }
        cli.write_json(args.json_path, payload)
        _write_reports(args, payload, records, problems, spec, standard, unit, unit_source)

        if getattr(args, "report_path", None):
            print(f"[完成] 已生成报告：{args.report_path}")
        if written:
            print(f"[成果] {export_note}")
        action = {"seq": "编号", "code": "写代码", "none": "仅分图层"}.get(args.mode, "处理")
        print(f"[结果] 共{action} {len(records)} 个地块，移动图层 {moved} 个，问题 {len(problems)} 条。")
        return confirm.EXIT_OK
    finally:
        try:
            backend.close()
        except Exception:
            pass


def _style_text(payload) -> str:
    style = payload.get("text_style") or "图纸默认样式（Standard）"
    parts = [style]
    if payload.get("font"):
        parts.append(f"字体 {payload['font']}")
    if payload.get("bigfont"):
        parts.append(f"大字体 {payload['bigfont']}")
    if payload.get("width_factor"):
        parts.append(f"宽度因子 {payload['width_factor']}")
    if payload.get("font") and not payload.get("style_applied"):
        parts.append("样式创建失败，已回退默认样式")
    return "，".join(parts)


def _write_reports(args, payload, records, problems, spec, standard, unit, unit_source) -> None:
    out = getattr(args, "report_path", None)
    if not out:
        return
    note = ""
    status = spec_mod.spec_status(spec)
    if status:
        note = f"> 注：{status}。图层规范使用内置通用默认值，替换 assets/config/layer_spec.json 后重跑即可。\n"
    rows = [
        (item.label, item.code or "（未识别）", item.name or item.shape.layer, f"{item.shape.area_m2:.2f}", item.shape.handle, item.shape.source_kind)
        for item in records
    ]
    unknown = [item for item in records if not item.code]
    mode = payload.get("mode", "seq")
    first_header = "编号" if mode == "seq" else ("用地代码" if mode == "code" else "图层")
    sections = [
        ("二、地块编码清单", ([first_header, "用地代码", "用地名称", "面积(㎡)", "句柄", "来源"], rows)),
        (
            "三、校验",
            md_table(
                ["校验项", "结果"],
                [
                    ["地块总数", len(records)],
                    ["代码未识别", len(unknown)],
                    ["重复编号", sum(1 for item in problems if item.get("kind") == "duplicate")],
                    ["几何无效", sum(1 for item in problems if item.get("kind") == "invalid_geometry")],
                    ["面积过小", sum(1 for item in problems if item.get("kind") == "tiny_area")],
                    ["未参与（退化/小于阈值）", len(payload.get("skipped", []))],
                ],
            ),
        ),
        (
            "四、异常清单",
            md_table(
                ["类型", "说明", "句柄", "图层"],
                [(item.get("kind", ""), item.get("message", ""), item.get("handle", ""), item.get("layer", "")) for item in problems],
            )
            if problems
            else "无。",
        ),
        (
            "五、未识别图层",
            md_table(
                ["图层", "原因"],
                [(item.get("layer", ""), item.get("reason", "")) for item in payload.get("unparceled", [])],
            )
            if payload.get("unparceled")
            else "无。",
        ),
        (
            "六、未参与编号的图元",
            md_table(
                ["句柄", "图层", "类型", "面积(㎡)", "原因"],
                [
                    (
                        item.get("handle", ""),
                        item.get("layer", ""),
                        item.get("source", ""),
                        f"{item.get('area_m2', 0):.4f}",
                        item.get("reason", ""),
                    )
                    for item in payload.get("skipped", [])
                ],
            )
            if payload.get("skipped")
            else "无。",
        ),
        ("附：句柄定位", locator_note()),
    ]
    mode_label = {"seq": "编号（序号+代码）", "code": "只写用地代码", "none": "不写标注"}.get(mode, mode)
    meta = [
        ("分类标准", standard),
        ("图纸单位", f"{unit}（{unit_source}）"),
        ("上图模式", mode_label),
        ("编号规则", payload.get("template", "")),
        ("图层处理", "按用地代码分图层" if payload.get("layer_mode") == "code" else "保持原图层"),
        ("排序规则", args.sort),
        ("标注图层", payload.get("label_layer", "") if mode != "none" else "不写标注"),
        ("文字样式", _style_text(payload)),
        ("成果导出", payload.get("export_note", "未导出")),
    ]
    title = "地块编码成果" if mode == "seq" else ("用地代码上图成果" if mode == "code" else "用地分图层成果")
    write_markdown(out, title, meta, sections, status_note=note)


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
