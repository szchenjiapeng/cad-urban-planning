#!/usr/bin/env python
"""子技能 N5：建筑量/容积率测算与批量建筑建模。

流程：确认图层 → 取建筑轮廓 → 取层数（砼 / 7F / 层 / 高度）→ 层高（住宅 3.0 / 非住宅 4.5）
→ 建筑归属地块 → 算基底、建筑量、容积率、建筑密度 → 写指标标注图层 + 闭合校验 + 批量拉伸。

硬性口径（已与用户确认）：
- 只做「基底面积 × 层数」的**简单计容建筑面积**，只含主体建筑，不扣阳台/架空/地下/设备层；
- 层数缺失默认 1 层，但必须单列清单并提示容积率可能被低估；
- 建筑量只用于本子技能的「计容容积率」，不得表述为报建口径；
- 所有要素同一图层时，建筑轮廓只能靠几何+文字启发式推断，**必须先告知偏差风险并由用户确认**；
- 只有闭合轮廓才拉伸；容差吸附闭合的单列「需人工闭合」清单；
- 无 CAD 时不做沉默降级：出体块网格还是只出清单，由用户选。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional, Sequence, Tuple

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import parcel_code  # noqa: E402
from lib import (  # noqa: E402
    building_class,
    building_massing,
    cli,
    confirm,
    extrude,
    labels,
    landuse,
    offset,
    shapes,
)
from lib import layer_spec as spec_mod  # noqa: E402
from lib.report import locator_note, md_table, write_markdown  # noqa: E402

DEFAULT_RULES = _SCRIPTS_DIR / "assets" / "massing_rules.json"
SUFFIX = {"measure": "容积率", "extrude": "体块", "all": "容积率与体块"}
SINGLE_LAYER_WARNING = (
    "本图未识别到建筑图层，所有要素可能位于同一图层。建筑轮廓只能靠几何形态与文字标注"
    "启发式推断，提取结果可能存在较大偏差，必须人工核对后再使用。"
)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="建筑量/容积率测算与批量建筑建模（子技能 N5）")
    cli.add_source_args(parser)
    cli.add_layer_args(parser)
    cli.add_output_args(parser)
    parser.add_argument(
        "--task",
        default="all",
        choices=["measure", "extrude", "all"],
        help="measure=只测算容积率；extrude=只批量建模；all=一次取数出两份成果（默认）",
    )
    parser.add_argument("--building-layers", help="建筑图层，逗号分隔；缺省按 layer_spec 建筑组识别")
    parser.add_argument("--parcel-layers", help="用地/地块图层，逗号分隔；缺省按用地代码图层识别")
    parser.add_argument("--height-layers", help="层数/高度标注图层，逗号分隔；指定后只扫这些图层（推荐）")
    parser.add_argument("--layer-map", help="图层指派表 JSON（{\"building\":[],\"parcel\":[],\"height\":[]}）")
    parser.add_argument("--confirm-layers", action="store_true", help="只输出图层指派与建筑候选（退出码 3）")
    parser.add_argument(
        "--auto-detect-buildings",
        action="store_true",
        help="确认后启用：单图层/非标准图按几何+文字启发式识别建筑候选（默认关闭，结果需人工复核）",
    )
    parser.add_argument("--floor-height", type=float, default=None, help="统一层高（米）；不给则按用地性质默认")
    parser.add_argument(
        "--floor-height-policy",
        default=None,
        help="auto=住宅 3.0 / 已识别非住宅 4.5 / 未识别 3.0；也可直接给数值",
    )
    parser.add_argument(
        "--cross-parcel",
        default=None,
        choices=list(building_massing.CROSS_POLICIES),
        help="跨界建筑：point=代表点整栋归属（默认）/ clip=按相交面积分摊 / exclude=整栋排除",
    )
    parser.add_argument(
        "--parcel-filter",
        default="auto",
        choices=list(building_massing.PARCEL_FILTER_MODES),
        help=(
            "地块筛选：auto=有性质只留建设用地（06-16）、未识别只留有建筑落位的地块（默认）；"
            "all=全部地块都编号出指标；construction=只按建设用地；built=只按有建筑落位"
        ),
    )
    parser.add_argument(
        "--parcel-exclude-classes",
        metavar="CODES",
        help="默认不出容积率/建筑密度的用地大类（逗号分隔）；缺省取配置 14,G（绿地与开敞空间用地）",
    )
    parser.add_argument(
        "--parcel-classes",
        metavar="CODES",
        help="只对这些一级类编号并出指标（白名单，优先于 --parcel-filter），如 07,08,09",
    )
    parser.add_argument("--bare-number-floors", action="store_true", help="把建筑内裸数字当层数（低置信，默认关闭）")
    parser.add_argument(
        "--floors",
        type=int,
        default=None,
        help="统一层数：图上没有层数标注时，给所有未识别建筑用的层数",
    )
    parser.add_argument(
        "--floors-by-layer",
        help="按图层指定层数，如 \"1-202512塔楼=18,1-202512裙房=3\"；优先于 --floors",
    )
    parser.add_argument(
        "--far-limit",
        type=float,
        default=6.0,
        help="容积率超限提示阈值，默认 6.0；给 0 关闭超限清单",
    )
    parser.add_argument("--min-building-area", type=float, default=None, help="建筑最小基底面积（㎡）")
    parser.add_argument("--max-building-area", type=float, default=None, help="建筑最大基底面积（㎡）")
    parser.add_argument(
        "--no-parcel",
        action="store_true",
        help="确认无用地边界后使用：只出全图建筑量合计，不计算容积率与建筑密度",
    )
    parser.add_argument(
        "--scope-layers",
        help="规划范围图层，逗号分隔；缺省自动识别含「规划范围/设计范围」的图层",
    )
    parser.add_argument(
        "--scope-mode",
        default="auto",
        choices=["auto", "none"],
        help="auto=按规划范围裁剪地块与建筑（默认）/ none=整图测算",
    )
    parser.add_argument(
        "--accept-layers",
        action="store_true",
        help="接受自动识别的图层，不再询问；有歧义时默认必须由用户确认",
    )
    parser.add_argument(
        "--ignore-hybrid-unresolved",
        action="store_true",
        help="混合用地里有段既非表内代码也非表内名称时不再追问，只按首段代码统计（报告标需复核）",
    )
    parser.add_argument(
        "--unregistered-code",
        action="append",
        metavar="CODE=ACTION",
        help="未登记/自定义用地代码的处理：skip=该层面积进表但代码留空并标需复核／map:目标代码=归入已有代码（可重复）",
    )
    parser.add_argument("--mapping", help="自定义图层映射 JSON 路径（缺省用技能内置映射）")
    parser.add_argument("--classification", help="自定义分类树 JSON 路径（缺省按 --standard 取内置分类表）")
    parser.add_argument("--rules", default=str(DEFAULT_RULES), help="测算规则 JSON")
    parser.add_argument("--label-layer", help="指标标注图层，默认取规则文件 label.layer")
    parser.add_argument(
        "--label-template",
        help="标注模板，默认 {seq}-{code}-FAR{far:.2f}；可用 {seq}{code}{code_first}{name}{far}{coverage}{area}{size}",
    )
    parser.add_argument("--label-height", type=float, default=None, help="标注字高（米）")
    parser.add_argument("--label-gap", type=float, default=None, help="标注最小间距（米）")
    parser.add_argument("--text-style", help="文字样式名；不给则沿用图纸默认样式")
    parser.add_argument("--extrude-layer", help="体块图层，默认取规则文件 extrude.layer")
    parser.add_argument(
        "--extrude-layering",
        default="ask",
        choices=["ask", "keep", "by-height", "none"],
        help=(
            "体块成果分层：ask=执行前询问（默认）/ keep=按图上塔楼/裙房图层分开 / "
            "by-height=按高度阈值分开 / none=全部放同一体块图层"
        ),
    )
    parser.add_argument(
        "--tower-height",
        type=float,
        default=None,
        help="按高度分层时的塔楼阈值（米），默认取规则文件 extrude.tower_height_m（24）",
    )
    parser.add_argument("--extrude-layer-tower", help="塔楼体块图层名，默认 <体块层>-塔楼")
    parser.add_argument("--extrude-layer-podium", help="裙房体块图层名，默认 <体块层>-裙房")
    parser.add_argument(
        "--extrude-format",
        default="ask",
        choices=["ask", "solid", "mesh", "none"],
        help="solid=真 3DSOLID（需 CAD）；mesh=体块网格；none=只出清单；ask=无 CAD 时先问用户",
    )
    parser.add_argument("--dry-run", action="store_true", help="只出报告，不写图、不导出")
    return parser.parse_args(argv)


def load_rules(path: str) -> Dict:
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"测算规则不存在：{target}")
    return json.loads(target.read_text(encoding="utf-8"))


def load_layer_map(path: Optional[str]) -> Dict:
    if not path:
        return {}
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"图层指派表不存在：{target}")
    return json.loads(target.read_text(encoding="utf-8"))


def _apply_layer_map(layers: Dict[str, List[str]], mapping: Dict, visible: Sequence[str]) -> Dict[str, List[str]]:
    if not mapping:
        return layers
    visible_set = set(visible)
    for role in ("building", "parcel", "height"):
        names = [str(name) for name in (mapping.get(role) or [])]
        names = [name for name in names if name in visible_set]
        if names:
            layers[role] = names
    return layers


def _rule_float(rules: Dict, section: str, key: str, fallback: float) -> float:
    try:
        return float((rules.get(section) or {}).get(key, fallback))
    except Exception:
        return fallback


def _split_codes(text) -> List[str]:
    """把 "07,08,09" / "14，G" 拆成一级类代码列表。"""
    raw = str(text or "").replace("，", ",")
    return [token.strip().upper() for token in raw.split(",") if token.strip()]


def _rule_codes(rules: Dict, section: str, key: str, fallback: Sequence[str]) -> List[str]:
    value = (rules.get(section) or {}).get(key)
    if isinstance(value, (list, tuple)) and value:
        return [str(item).strip().upper() for item in value if str(item).strip()]
    return [str(item).strip().upper() for item in fallback]


def _collect_parcels(
    backend, layers, classifier, spec, factor
) -> Tuple[List[shapes.ShapeRec], Dict[str, landuse.LayerInfo]]:
    """收地块并标注用地代码，返回 (地块, 图层→LayerInfo)。

    混合用地（如 `100104+0909新型工业+商业服务业混合用地`）同时带出合成代码：
    `code` 仍是首段主代码（层高、退线等口径按它走），`code_display` 才是成果里显示的
    `100104+0909`。面积始终按整块几何计量一次，绝不按段拆分重复计容。
    """
    # 空列表必须直接返回：collect_polygons 把空 layers 当作「不过滤」，会收集全图
    if not layers.get("parcel"):
        return [], {}
    parcels = shapes.collect_polygons(
        backend,
        layers.get("parcel") or [],
        unit_factor=factor,
        source="auto",
        min_area_m2=1.0,
    )
    info_map: Dict[str, landuse.LayerInfo] = {}
    for parcel in parcels:
        info = info_map.get(parcel.layer)
        if info is None:
            info = landuse.inspect_layer(parcel.layer, classifier, spec)
            info_map[parcel.layer] = info
        parcel.code = info.code
        parcel.name = info.name
        parcel.code_display = info.display_code or (info.code or "")
        parcel.hybrid_raw = info.hybrid_raw
        parcel.hybrid_parts = list(info.hybrid_parts)
        parcel.hybrid_unresolved = list(info.hybrid_unresolved)
    return parcels, info_map


def _unregistered_parcel_items(rows: Sequence[Dict]) -> List[Dict]:
    """未登记/自定义用地代码的用地图层：先问再算，不静默剔除也不静默归类。

    与平衡表同口径：图层名里的代码不在分类表里时，既不能当地块静默吃面积，
    也不能按关键词兜底归到别的大类，必须由用户拍板。
    """
    merged: Dict[str, List[str]] = {}
    for row in rows:
        for code in row.get("unregistered") or []:
            bucket = merged.setdefault(str(code), [])
            if row.get("layer") and row["layer"] not in bucket:
                bucket.append(row["layer"])

    items: List[Dict] = []
    for code, layers in sorted(merged.items()):
        text = "、".join(layers[:5])
        items.append(
            confirm.item(
                "unregistered_code",
                f"用地图层「{text}」里的代码 {code} 不在分类表中（可能是用户自定义用地性质，或图层名写错）。"
                "这些地块直接剔除会让容积率分母凭空变小，请先确认：",
                options=[
                    {"label": "改图层名后重跑", "desc": "名称写错，在 CAD 里改正图层名，不写入配置"},
                    {"label": "登记为新增代码", "desc": f"先登记 {code}（scripts/add_classification_node.py），再重跑"},
                    {"label": "归入已有代码", "desc": f"--unregistered-code \"{code}=map:目标代码\"，在说明里记录"},
                    {"label": "本次不处理", "desc": f"--unregistered-code \"{code}=skip\"：面积进表、代码留空，报告标需复核"},
                ],
                detail={"code": code, "layers": layers, "rows": list(rows)},
            )
        )
    return items


def _hybrid_unresolved_items(groups: Sequence[Dict]) -> List[Dict]:
    """混合用地里有段对不上分类表：问用户「写错 / 新增代码 / 只按首段统计」。"""
    items: List[Dict] = []
    for group in groups:
        layer = group.get("layer", "")
        segments = list(group.get("segments") or [])
        text = "、".join(segments[:5])
        items.append(
            confirm.item(
                "hybrid_unresolved",
                f"混合用地图层「{layer}」里的「{text}」既不是分类表里的用地代码，也不是表内的用地名称。"
                "请确认：是名称写错了，还是要把它登记为新增用地代码？",
                options=[
                    {"label": "改图层名后重跑", "desc": "不写入配置，改完重跑即可"},
                    {"label": "新增代码", "desc": "登记后可该段自动按中文名命中"},
                    {"label": "只按首段统计", "desc": "加 --ignore-hybrid-unresolved，其余段不计，报告标需复核"},
                ],
                detail={"layer": layer, "segments": segments[:20], "code": group.get("code", "")},
            )
        )
    return items


def _apply_unregistered_codes(classifier, specs: Optional[Sequence[str]]) -> Tuple[Dict[str, str], List[str]]:
    """解析 --unregistered-code：返回 (代码→目标代码映射, 本次跳过的代码)。"""
    explicit: Dict[str, str] = {}
    skipped: List[str] = []
    for raw in specs or []:
        token, _, action = str(raw).partition("=")
        token, action = token.strip(), action.strip()
        if not token or not action:
            raise ValueError(f"--unregistered-code 格式应为 CODE=skip 或 CODE=map:目标代码，收到：{raw}")
        if action.startswith("map:"):
            target = action.split(":", 1)[1].strip()
            if classifier.tree.get(target) is None:
                raise ValueError(
                    f"--unregistered-code {token}=map:{target}：目标代码 {target} 不在分类表中，请先确认归属"
                )
            explicit[token] = target
        elif action == "skip":
            skipped.append(token)
        else:
            raise ValueError(f"--unregistered-code 只支持 skip 或 map:目标代码，收到：{action}")
    for token, target in explicit.items():
        classifier.mapper.code_overrides[token] = target
    return explicit, skipped


def _collect_texts(backend, layers) -> Tuple[List, Dict]:
    height_layers = layers.get("height") or []
    if height_layers:
        try:
            texts = list(backend.iter_texts(height_layers, None))
        except Exception:
            texts = []
        return texts, {"mode": "targeted", "layers": height_layers, "count": len(texts)}
    print(
        "[提示] 未指定层数/高度标注图层，将扫描全图文字；标注分散或文字量大时耗时会明显变长，"
        "建议先 --confirm-layers 确认后加 --height-layers。",
        file=sys.stderr,
    )
    try:
        texts = list(backend.iter_texts(None, None))
    except Exception:
        texts = []
    return texts, {"mode": "full", "layers": [], "count": len(texts)}


def _filter_buildings(records: Sequence[shapes.ShapeRec], min_area: float, max_area: float) -> Tuple[List, List[Dict]]:
    kept: List[shapes.ShapeRec] = []
    skipped: List[Dict] = []
    for record in records:
        area = float(record.area_m2 or 0.0)
        if area < min_area:
            skipped.append(
                {
                    "handle": record.handle,
                    "layer": record.layer,
                    "reason": f"基底面积 {area:.2f} ㎡ 小于阈值 {min_area:g} ㎡",
                    "area_m2": round(area, 3),
                }
            )
            continue
        if area > max_area:
            skipped.append(
                {
                    "handle": record.handle,
                    "layer": record.layer,
                    "reason": f"基底面积 {area:.2f} ㎡ 大于上限 {max_area:g} ㎡，判为非建筑",
                    "area_m2": round(area, 3),
                }
            )
            continue
        kept.append(record)
    return kept, skipped


def _parse_layer_floors(text: Optional[str]) -> Dict[str, int]:
    """解析「图层=层数」列表；用于图上完全没有层数标注时按图层指定。"""
    result: Dict[str, int] = {}
    for chunk in re.split(r"[,，;；]", str(text or "")):
        if "=" not in chunk:
            continue
        name, _sep, value = chunk.partition("=")
        name = name.strip()
        try:
            floors = int(float(value.strip()))
        except Exception:
            continue
        if name and 1 <= floors <= 99:
            result[name] = floors
    return result


def _label_text(template: str, metric, seq: str) -> str:
    # {code} 用合成代码（100104+0909），与平衡表、报告表一致；
    # 需要图面简洁时模板里改用 {code_first}
    code = getattr(metric, "code_display", "") or metric.code or "X"
    code_first = metric.code or "X"
    try:
        return str(template).format(
            seq=seq,
            label=seq,
            code=code,
            code_first=code_first,
            name=metric.name or metric.shape.layer,
            far=metric.far,
            coverage=metric.coverage,
            area=metric.area_m2,
            size=metric.floor_area_m2,
        )
    except Exception:
        return f"{seq}-{code}-FAR{metric.far:.2f}"


def _candidate_layers(visible: Sequence[str], spec) -> List[str]:
    """单图层候选扫描范围：排除图框、标注、控制线，避免把图例当建筑。"""
    skip = ("frame", "label", "control")
    return [
        name
        for name in visible
        if not any(spec_mod.layer_in_group(name, spec, group) for group in skip)
    ]


def _extrude_format(args, backend, task: str) -> Tuple[str, Optional[int]]:
    """决定拉伸产出形态；无 CAD 时按用户口径询问，不静默降级。"""
    if task == "measure":
        return "none", None
    if args.dry_run:
        return "none", None
    if args.extrude_format == "solid":
        return ("solid" if extrude.supports_solid(backend) else "mesh"), None
    if args.extrude_format in {"mesh", "none"}:
        return args.extrude_format, None
    if extrude.supports_solid(backend):
        return "solid", None
    code = confirm.emit(
        [
            confirm.item(
                "extrude_format",
                "本机未检测到可执行的 AutoCAD，无法生成真正的 3DSOLID 实体。请确认建模成果的交付口径。",
                options=[
                    {
                        "label": "输出体块网格（DXF 3DFACE）",
                        "desc": "顶面 + 侧面体块示意，可看体积关系，但不是 ACIS 实体，不能直接做布尔运算",
                    },
                    {"label": "只出清单，不出模型", "desc": "只交付层数/高度清单与测算表，模型在有 CAD 的机器上重跑"},
                    {"label": "在有 CAD 的机器上重跑", "desc": "本次不建模，改用 --source com 或安装 AutoCAD 后重跑"},
                ],
                detail={
                    "channel": getattr(backend, "channel", ""),
                    "reason": "当前通道不支持 AddExtrudedSolid",
                },
            )
        ]
    )
    return "none", code


def _layer_stats(records: Sequence[shapes.ShapeRec], heights: Dict[str, float]) -> List[Dict]:
    """按建筑图层汇总栋数/基底/高度区间，作为成果分层确认项的证据。"""
    groups: Dict[str, Dict] = {}
    for record in records:
        row = groups.setdefault(
            record.layer,
            {"layer": record.layer, "count": 0, "footprint_m2": 0.0, "heights": []},
        )
        row["count"] += 1
        row["footprint_m2"] += float(record.area_m2 or 0.0)
        height = float(heights.get(str(record.handle), 0.0) or 0.0)
        if height > 0:
            row["heights"].append(height)
    stats: List[Dict] = []
    for row in groups.values():
        values = row.pop("heights")
        row["footprint_m2"] = round(row["footprint_m2"], 2)
        row["height_range_m"] = (
            f"{min(values):g}–{max(values):g}" if values else "未知（高度为 0）"
        )
        stats.append(row)
    return sorted(stats, key=lambda item: -item["count"])


def _extrude_layering_item(
    plan: Dict,
    stats: Sequence[Dict],
    tower_layer: str,
    podium_layer: str,
    base_layer: str,
    tower_height: float,
) -> Dict:
    """构造「体块要不要按塔楼/裙房分层」的确认项。

    图上已分塔楼/裙房时问「是否沿用图层分」；未区分时问「是否按高度阈值分」。
    两个分支的问题文案、选项与默认口径都不同，不能合成一句话。
    """
    detail = {
        "detected": plan,
        "building_layers": list(stats),
        "tower_height_m": round(float(tower_height), 2),
        "proposed_layers": {
            "base": base_layer,
            "tower": tower_layer,
            "podium": podium_layer,
        },
        "note": (
            "归层优先级：源图层点名（塔楼/裙房）> 拉伸高度阈值 > 裙房兜底。"
            "高度 = 层数 × 层高（简单计容），阈值按此换算，不是报建高度。"
        ),
    }
    if plan.get("mode") == "split":
        names = "、".join(list(plan.get("tower") or []) + list(plan.get("podium") or []))
        return confirm.item(
            "extrude_layering",
            f"本图建筑图层已区分塔楼/裙房（{names}）。体块是否也按塔楼/裙房分图层输出？",
            options=[
                {
                    "label": "按塔楼/裙房分两个图层",
                    "desc": f"{tower_layer} / {podium_layer}，按源图层点名归层，加 --extrude-layering keep",
                },
                {
                    "label": f"全部合并到 {base_layer}",
                    "desc": "沿用单图层现状，加 --extrude-layering none",
                },
                {
                    "label": f"按 {tower_height:g}m 高度重新分层",
                    "desc": f"忽略图层名，>{tower_height:g}m 塔楼 / ≤{tower_height:g}m 裙房，加 --extrude-layering by-height",
                },
            ],
            detail=detail,
        )
    height_hint = ""
    for row in stats:
        height_hint = row.get("height_range_m") or height_hint
    return confirm.item(
        "extrude_layering",
        (
            "本图建筑图层未区分塔楼/裙房"
            + (f"（实测高度 {height_hint} m）" if height_hint else "")
            + f"。体块是否按高度分层（>{tower_height:g}m 塔楼 / ≤{tower_height:g}m 裙房）？"
        ),
        options=[
            {
                "label": "按高度分层",
                "desc": f"{tower_layer} / {podium_layer}，加 --extrude-layering by-height",
            },
            {
                "label": f"不分层，全部放 {base_layer}",
                "desc": "沿用单图层现状，加 --extrude-layering none",
            },
            {
                "label": f"自定义阈值再分层",
                "desc": f"加 --extrude-layering by-height --tower-height <米>，默认 {tower_height:g}m",
            },
        ],
        detail=detail,
    )


def run(args) -> int:
    cli.setup_stdout()
    offset.require_shapely()
    spec = spec_mod.load_spec()
    rules = load_rules(args.rules)
    layer_map = load_layer_map(args.layer_map)
    backend, unit, factor, unit_source = cli.open_backend(args)
    args.report_path = cli.resolve_out(args, backend, "容积率测算")
    try:
        visible = cli.visible_layers(backend)
        guess = landuse.detect_standard(visible)
        standard = guess.get("standard") or "2023"
        classifier = landuse.build(standard, args.classification, args.mapping)
        # 用户自定义/未登记代码的显式归属必须先于挑图层生效，否则这些层仍会被当成「无代码」剔除
        _, skipped_codes = _apply_unregistered_codes(classifier, args.unregistered_code)
        layers = building_massing.resolve_layers(visible, args, spec, classifier)
        layers = _apply_layer_map(layers, layer_map, visible)
        # 用户选「本次不处理」的未登记代码：地块面积仍进表（代码留空标未识别），
        # 不能让整层地块凭空消失、把容积率分母算小
        if skipped_codes:
            for name in list(layers.get("parcel_unresolved") or []):
                codes = set(classifier.mapper.unregistered_codes(name))
                if codes and codes <= set(skipped_codes):
                    if name not in layers["parcel"]:
                        layers["parcel"].append(name)
                    layers["parcel_unresolved"].remove(name)

        # 第一级确认：选图层 + 层高。两者都不依赖几何，必须合并成一次问完——
        # 否则「问图层 → 重跑 → 扫完几何再问层高」，用户要白等两次全量扫描。
        explicit_layers = bool(
            args.building_layers or args.parcel_layers or args.scope_layers or args.layer_map
        )
        pending: List[Dict] = []
        if args.confirm_layers:
            info = building_massing.layer_candidates(backend, visible, spec, classifier, factor)
            pending.append(
                confirm.item(
                    "layer_assignment",
                    "请确认建筑、用地与规划范围图层。下面是候选图层及「能不能用」的证据："
                    "影像底图（IMAGE）不是矢量建筑，空图层选了会得到 0 个对象，"
                    "塔楼/裙房可能分层表达需全部选上。",
                    options=[
                        {
                            "id": "accept_recommended",
                            "label": "按推荐图层执行",
                            "args": [],
                            "desc": "把 recommended 写入 layer-map.json 后重跑",
                        },
                        {
                            "id": "manual",
                            "label": "手动指定图层",
                            "args": [],
                            "requires_input": "layers",
                            "desc": "用 --building-layers / --parcel-layers / --scope-layers 指定",
                        },
                        {
                            "id": "accept_layers",
                            "label": "接受自动识别",
                            "args": ["--accept-layers"],
                            "desc": "加 --accept-layers，不再询问（仅在核对候选后使用）",
                        },
                    ],
                    detail=info,
                )
            )
        elif not explicit_layers and not args.accept_layers:
            info = building_massing.layer_candidates(backend, visible, spec, classifier, factor)
            if info["ambiguous"]:
                pending.append(
                    confirm.item(
                        "layer_assignment",
                        "图层存在歧义，请先确认再测算：" + "；".join(info["reasons"]),
                        options=[
                            {
                                "id": "accept_recommended",
                                "label": "按推荐图层执行",
                                "args": [],
                                "desc": "确认后写入 layer-map.json 重跑",
                            },
                            {
                                "id": "manual",
                                "label": "手动指定图层",
                                "args": [],
                                "requires_input": "layers",
                                "desc": "用参数指定，避免误选",
                            },
                            {
                                "id": "accept_layers",
                                "label": "接受自动识别",
                                "args": ["--accept-layers"],
                                "desc": "加 --accept-layers 跳过本确认",
                            },
                        ],
                        detail=info,
                    )
                )

        if args.floor_height is None and args.floor_height_policy is None:
            residential = _rule_float(rules, "floor_height", "residential", 3.0)
            non_residential = _rule_float(rules, "floor_height", "non_residential", 4.5)
            unknown = _rule_float(rules, "floor_height", "unknown", 3.0)
            pending.append(
                confirm.item(
                    "floor_height",
                    "请确认层高取值：默认住宅 3.0m、已识别非住宅 4.5m、用地代码未识别 3.0m。",
                    options=[
                        {
                            "id": "policy_auto",
                            "label": f"接受默认（住宅 {residential:g} / 非住宅 {non_residential:g}）",
                            "args": ["--floor-height-policy", "auto"],
                            "desc": "加 --floor-height-policy auto",
                        },
                        {
                            "id": "uniform",
                            "label": "统一指定层高",
                            "args": [],
                            "requires_input": "floor_height",
                            "desc": "加 --floor-height 3.0 对全部地块生效",
                        },
                        {
                            "id": "per_parcel",
                            "label": "按地块分别指定",
                            "args": [],
                            "desc": "先用默认策略出表，再按地块核对并重跑",
                        },
                    ],
                    detail={
                        "defaults": {
                            "residential": residential,
                            "non_residential": non_residential,
                            "unknown": unknown,
                        },
                        "residential_codes": (rules.get("floor_height") or {}).get("residential_codes") or ["07", "R"],
                        "note": "层数 × 层高 = 建筑高度，同时用于测算与拉伸，改层高会同时改容积率与体块高度。",
                    },
                )
            )

        if pending:
            return confirm.emit(pending, round_no=1)

        min_area = args.min_building_area
        if min_area is None:
            min_area = _rule_float(rules, "building_filter", "min_area_m2", 10.0)
        max_area = args.max_building_area
        if max_area is None:
            max_area = _rule_float(rules, "building_filter", "max_area_m2", 20000.0)

        # 用地性质确认：像用地但代码未登记的图层不得静默剔除（会让分母变小），
        # 也不得按关键词兜底归到别的大类；只建模时不需要用地性质，不打断用户
        pending_parcels = building_massing.suspicious_parcel_layers(
            visible, spec, classifier, layers.get("parcel_unresolved") or []
        )
        if pending_parcels and args.task != "extrude":
            return confirm.emit(_unregistered_parcel_items(pending_parcels))

        parcels, parcel_info = _collect_parcels(backend, layers, classifier, spec, factor)
        # 混合用地确认：某段既非表内代码也非表内名称时，不得静默按首段统计
        hybrid_groups = landuse.hybrid_unresolved_groups(parcel_info.values())
        if hybrid_groups and not args.ignore_hybrid_unresolved and args.task != "extrude":
            return confirm.emit(_hybrid_unresolved_items(hybrid_groups))
        hybrid_rows = landuse.hybrid_rows(parcel_info.values())
        texts, height_scan = _collect_texts(backend, layers)

        single_layer: Dict = {}
        building_records: List[shapes.ShapeRec] = []
        skipped_buildings: List[Dict] = []
        if layers.get("building"):
            # 同一图层可能既有填充又有多段线：both 全收后按几何去重，避免同一栋算两次；
            # 也避免 auto 的图层级跳过把「只用多段线表达」的建筑整批漏掉。
            raw, collect_skipped = shapes.collect_polygons_detail(
                backend,
                layers["building"],
                unit_factor=factor,
                source="both",
                min_area_m2=min_area,
                dedupe=True,
            )
            building_records, skipped_buildings = _filter_buildings(raw, min_area, max_area)
            skipped_buildings = list(collect_skipped) + list(skipped_buildings)
        else:
            single_layer = building_massing.detect_candidates(
                backend,
                _candidate_layers(visible, spec),
                factor,
                rules,
                texts=texts,
                parcels=parcels,
            )

        if not building_records:
            if not args.auto_detect_buildings:
                return confirm.emit(
                    [
                        confirm.item(
                            "single_layer_scene",
                            SINGLE_LAYER_WARNING + "\n请确认是否按几何+文字启发式识别建筑轮廓。",
                            options=[
                                {"label": "启用自动识别", "desc": "用 --auto-detect-buildings 按候选得分采用，需人工复核"},
                                {"label": "指定建筑图层", "desc": "用 --building-layers \"JZ-建筑\" 指定"},
                                {"label": "先修图", "desc": "回 CAD 把建筑整理到独立图层后重跑"},
                            ],
                            detail={
                                "candidates": single_layer.get("candidates") or [],
                                "total_closed": single_layer.get("total_closed", 0),
                                "skipped": single_layer.get("skipped") or [],
                                "notes": single_layer.get("notes") or [],
                            },
                        )
                    ]
                )
            handles = set(single_layer.get("adopted") or [])
            candidates_map = single_layer.get("records") or {}
            for handle in handles:
                record = candidates_map.get(handle)
                if record is not None and min_area <= record.area_m2 <= max_area:
                    building_records.append(record)
            if not building_records:
                return confirm.fail(
                    "no_building_candidate",
                    "自动识别没有得到可用建筑候选。请指定 --building-layers，或先修图把建筑单独分层。",
                )

        if not building_records:
            return confirm.fail(
                "no_building",
                "没有找到建筑轮廓。请用 --building-layers 指定建筑图层，或确认建筑是闭合多段线/填充。",
            )

        # 规划范围裁剪：容积率只对范围内用地有意义
        scope_info = {"used": False, "layers": [], "dropped_parcels": 0, "dropped_buildings": 0}
        if args.scope_mode != "none":
            visible_set = set(visible)
            scope_layers = (
                cli.parse_list(args.scope_layers)
                if args.scope_layers
                else building_massing.default_scope_layers(visible)
            )
            scope_layers = [name for name in scope_layers if name in visible_set]
            scope_info["layers"] = scope_layers
            scope_shape = (
                building_massing.scope_geometry(backend, scope_layers, factor) if scope_layers else None
            )
            if scope_shape is not None:
                parcels, building_records, dropped_p, dropped_b = building_massing.clip_to_scope(
                    parcels, building_records, scope_shape, factor
                )
                scope_info.update(
                    {"used": True, "dropped_parcels": dropped_p, "dropped_buildings": dropped_b}
                )
            elif scope_layers:
                print(
                    f"[提示] 规划范围图层 {scope_layers} 未取到闭合边界，本次按整图测算。",
                    file=sys.stderr,
                )

        # 只建模时不需要用地边界（高度只取层数 × 层高），不因此打断用户
        if not parcels and not args.no_parcel and args.task != "extrude":
            return confirm.emit(
                [
                    confirm.item(
                        "no_parcel",
                        "没有找到用地地块，无法计算容积率与建筑密度。请确认交付口径。",
                        options=[
                            {"label": "指定用地图层", "desc": "用 --parcel-layers \"YD-用地红线\" 指定后重跑"},
                            {"label": "只出全图建筑量合计", "desc": "加 --no-parcel：出建筑量、层数、高度清单，不计算容积率"},
                            {"label": "先修图", "desc": "回 CAD 把用地边界单独分层后重跑"},
                        ],
                        detail={
                            "parcel_layers": layers.get("parcel") or [],
                            "building_layers": layers.get("building") or [],
                            "note": "单图层/未分层图纸无法区分用地与建筑，容积率必须依赖可用地边界。",
                        },
                    )
                ]
            )

        # 层高已在扫描前与图层问题合并确认（见上），此处不再重复追问

        floor_reference = args.floor_height if args.floor_height is not None else 3.0
        floors = building_class.attach_floors(
            building_records,
            texts,
            floor_height=float(floor_reference),
            bare_number=bool(args.bare_number_floors),
        )
        # 图上没有层数标注时，按图层/全局指定层数（文字层数优先，命令行只补空缺）
        by_layer_floors = _parse_layer_floors(args.floors_by_layer)
        manual_floors = 0
        for record in building_records:
            key = str(record.handle)
            if key in floors:
                continue
            value = by_layer_floors.get(record.layer) or args.floors
            if not value:
                continue
            floors[key] = {
                "floors": int(value),
                "height_m": None,
                "source": "命令行指定（图上无层数标注）",
                "kind": "manual",
                "conflicts": [],
            }
            manual_floors += 1

        cross_policy = args.cross_parcel or str((rules.get("cross_parcel") or {}).get("policy") or "point")
        metrics, items, problems = building_massing.compute_metrics(
            parcels,
            building_records,
            floors,
            args,
            rules,
            cross_policy=cross_policy,
        )

        # 地块筛选：识别到性质只留建设用地（06-16），未识别性质只留有建筑落位的地块。
        # 编号与容积率/建筑密度只对保留地块做，剔除项必须进异常清单交代清楚。
        all_metrics = list(metrics)
        all_area_m2 = sum(float(metric.area_m2 or 0.0) for metric in all_metrics)
        exclude_classes = (
            _split_codes(args.parcel_exclude_classes)
            if args.parcel_exclude_classes is not None
            else _rule_codes(rules, "parcel_filter", "exclude_classes", ["14", "G"])
        )
        only_classes = _split_codes(args.parcel_classes)
        metrics, dropped_parcels, parcel_filter_mode = building_massing.filter_parcels(
            all_metrics,
            classifier.tree,
            args.parcel_filter,
            exclude_classes=exclude_classes,
            only_classes=only_classes,
        )
        dropped_handles = {row["handle"] for row in dropped_parcels}
        for item in items:
            if str(item.parcel_handle) in dropped_handles:
                item.excluded = True
        for row in dropped_parcels:
            problems.append(
                {
                    "kind": "parcel_filtered",
                    "message": (
                        f"地块 {row['code_display'] or row['code'] or '未识别'} 不参与计容："
                        f"{row['reason']}（{row['area_m2']:.2f} ㎡）"
                    ),
                    "handle": row["handle"],
                    "layer": row["layer"],
                }
            )
        for item in items:
            if str(item.parcel_handle) in dropped_handles and float(item.footprint_m2 or 0.0) > 0:
                problems.append(
                    {
                        "kind": "building_on_filtered_parcel",
                        "message": "建筑所落地块不参与计容，该栋未计入任何地块指标",
                        "handle": str(item.shape.handle),
                        "layer": item.shape.layer,
                    }
                )

        for record in skipped_buildings:
            problems.append(
                {
                    "kind": "building_duplicate" if record.get("kept_handle") else "building_skipped",
                    "message": record.get("reason", ""),
                    "handle": record.get("handle", ""),
                    "layer": record.get("layer", ""),
                }
            )

        problems.extend(
            {
                "kind": "floor_conflict",
                "message": note,
                "handle": str(item.shape.handle),
                "layer": item.shape.layer,
            }
            for item in items
            for note in (item.conflicts or [])
        )

        # 编号：与地块编号子技能同一套排序与模板引擎，避免两套编号
        records = [
            parcel_code.CodedParcel(shape=metric.shape, code=metric.code, name=metric.name)
            for metric in metrics
        ]
        parcel_code.order_records(records, "row", 200.0)
        ns = SimpleNamespace(
            start=1, sort="row", mode="seq", prefix="", codes_file=None, unknown_placeholder="X"
        )
        parcel_code.build_codes(records, ns, "{seq:03d}")
        seq_map = {item.shape.handle: item.label for item in records}

        # 成果二：批量拉伸的口径与分层先算清 —— 确认项必须发生在写标注之前，
        # 否则用户答「分层」后重跑会把 BZ-容积率 标注重复写一遍。
        extrude_cfg = rules.get("extrude") or {}
        extrude_layer = args.extrude_layer or str(extrude_cfg.get("layer") or "JZ-体块")
        mode, blocked_code = _extrude_format(args, backend, args.task)
        if blocked_code is not None:
            return blocked_code
        heights: Dict[str, float] = {}
        for item in items:
            key = str(item.shape.handle)
            heights[key] = max(heights.get(key, 0.0), float(item.height_m or 0.0))
        ready, review, blocked = extrude.candidate_rows(building_records, heights)
        extrude_result = {"counts": {"solid": 0, "mesh": 0, "failed": 0}, "failed": [], "mode": mode}

        # 成果分层：图上已分塔楼/裙房就问「是否沿用图层分」，未区分就问「是否按高度阈值分」。
        # 归层优先级：源图层点名 > 高度阈值 > 裙房兜底；用户不答不落层，避免把塔楼裙房混成一坨。
        suffix_cfg = extrude_cfg.get("layer_suffix") or {}
        tower_layer = args.extrude_layer_tower or f"{extrude_layer}-{suffix_cfg.get('tower') or '塔楼'}"
        podium_layer = args.extrude_layer_podium or f"{extrude_layer}-{suffix_cfg.get('podium') or '裙房'}"
        tower_height = (
            float(args.tower_height)
            if args.tower_height is not None
            else _rule_float(rules, "extrude", "tower_height_m", 24.0)
        )
        layer_plan = building_massing.detect_layer_layering(layers.get("building") or [])
        layering_mode = str(args.extrude_layering or "ask")
        layering_note = ""
        if layering_mode == "keep" and layer_plan["mode"] != "split":
            layering_mode = "by-height"
            layering_note = "图上未分塔楼/裙房，keep 已回退为按高度分层（不能凭空按图层分）"
        if layering_mode == "ask" and mode != "none" and not args.dry_run:
            return confirm.emit(
                [
                    _extrude_layering_item(
                        layer_plan,
                        _layer_stats(building_records, heights),
                        tower_layer,
                        podium_layer,
                        extrude_layer,
                        tower_height,
                    )
                ]
            )
        split_layers = layering_mode in {"keep", "by-height"}
        layering_info = {
            "mode": layering_mode if (split_layers and mode != "none") else "none",
            "asked": str(args.extrude_layering or "ask") == "ask",
            "tower_height_m": round(tower_height, 2),
            "detected": layer_plan,
            "layers": {"base": extrude_layer, "tower": tower_layer, "podium": podium_layer},
            "counts": {"tower": 0, "podium": 0, "flat": 0},
            "groups": [],
            "note": layering_note,
        }
        payload_items = [row for row in (ready + review) if float(row.get("height") or 0.0) > 0]
        if split_layers and payload_items:
            for row in payload_items:
                name, reason = building_massing.assign_extrude_layer(
                    row, extrude_layer, tower_layer, podium_layer, tower_height
                )
                row["extrude_layer"] = name
                row["layer_reason"] = reason
                layering_info["counts"]["tower" if name == tower_layer else "podium"] += 1
            grouped: Dict[str, List[Dict]] = {}
            for row in payload_items:
                grouped.setdefault(str(row["extrude_layer"]), []).append(row)
            layering_info["groups"] = [
                {
                    "layer": name,
                    "type": "塔楼" if name == tower_layer else "裙房",
                    "count": len(rows),
                    "height_min": round(min(float(r["height"]) for r in rows), 2),
                    "height_max": round(max(float(r["height"]) for r in rows), 2),
                    "reasons": sorted({str(r.get("layer_reason", "")) for r in rows}),
                }
                for name, rows in grouped.items()
            ]
        else:
            layering_info["counts"]["flat"] = len(payload_items)

        # 成果一：指标标注图层
        label_cfg = rules.get("label") or {}
        label_layer = args.label_layer or str(label_cfg.get("layer") or "BZ-容积率")
        template = args.label_template or str(label_cfg.get("template") or "{seq}-{code}-FAR{far:.2f}")
        label_height = args.label_height if args.label_height is not None else float(label_cfg.get("height_m", 3.0))
        label_gap = args.label_gap if args.label_gap is not None else float(label_cfg.get("gap_m", 0.0))
        label_color = int(label_cfg.get("color", 6) or 6)
        label_count = 0
        occupied: List[Tuple[float, float]] = []
        if args.task in {"measure", "all"} and not args.dry_run:
            backend.ensure_layer(label_layer, color=label_color)
            for metric in metrics:
                text = _label_text(template, metric, seq_map.get(metric.shape.handle, ""))
                point = labels.place(metric.shape.geometry, occupied, label_gap) if metric.shape.geometry else (0.0, 0.0)
                occupied.append(point)
                backend.add_text(label_layer, point, text, height=float(label_height), style=args.text_style)
                label_count += 1

        if mode != "none" and not args.dry_run:
            targets = (
                sorted({str(row["extrude_layer"]) for row in payload_items})
                if split_layers and payload_items
                else [extrude_layer]
            )
            for name in targets:
                backend.ensure_layer(name, color=3)
            extrude_result = extrude.extrude_items(
                backend,
                payload_items,
                extrude_layer,
                mode=mode,
                chunk=int(extrude_cfg.get("chunk", 100) or 100),
            )
            extrude_result["layers"] = targets

        copy_path, copy_format, copy_note = None, "none", "未生成副本（--dry-run）"
        if not args.dry_run:
            copy_path, copy_format, copy_note = cli.export_drawing(backend, args, SUFFIX[args.task])

        totals = building_massing.summary_totals(metrics)
        if not metrics:
            footprint = sum(float(item.footprint_m2 or 0.0) for item in items if not item.excluded)
            floor_area = sum(float(item.floor_area_m2 or 0.0) for item in items if not item.excluded)
            totals = {
                "parcels": 0,
                "area_m2": 0.0,
                "footprint_m2": footprint,
                "floor_area_m2": floor_area,
                "far": 0.0,
                "coverage": 0.0,
            }
        payload = {
            "status": "ok",
            "task": args.task,
            "no_parcel": not metrics,
            "standard": standard,
            "unit": unit,
            "unit_source": unit_source,
            "channel": getattr(backend, "channel", ""),
            "layers": {
                "building": layers.get("building") or [],
                "parcel": layers.get("parcel") or [],
                "height": layers.get("height") or [],
                "height_scan": height_scan,
            },
            "layer_map": args.layer_map or "",
            "scope": scope_info,
            "parcel_filter": {
                "mode": parcel_filter_mode,
                "construction_classes": building_massing.construction_classes(classifier.tree),
                "exclude_classes": exclude_classes,
                "only_classes": only_classes,
                "parcels_total": len(all_metrics),
                "area_total_m2": round(all_area_m2, 3),
                "parcels_kept": len(metrics),
                "area_kept_m2": round(sum(float(metric.area_m2 or 0.0) for metric in metrics), 3),
                "dropped_count": len(dropped_parcels),
                "dropped_area_m2": round(sum(float(row["area_m2"] or 0.0) for row in dropped_parcels), 3),
                "dropped": dropped_parcels,
                "note": (
                    "auto：识别到用地性质时只对建设用地（06-16）编号并出容积率/建筑密度，"
                    "其中绿地与开敞空间用地（14）默认不编号、不出指标；"
                    "非建设用地（01-05、17-24）同样不编号；"
                    "识别不到性质时只对有建筑落位的地块编号。"
                    "可用 --parcel-filter all 恢复全量，--parcel-classes / --parcel-exclude-classes 作细调。"
                ),
            },
            "layer_confirm": {
                "explicit": explicit_layers,
                "asked": bool(args.confirm_layers),
                "accepted": bool(args.accept_layers),
            },
            "single_layer": {
                "used": bool(single_layer),
                "auto_detect": bool(args.auto_detect_buildings),
                "total_closed": single_layer.get("total_closed", 0),
                "adopted": len(building_records) if single_layer else 0,
                "candidates": (single_layer.get("candidates") or [])[:20],
                "notes": single_layer.get("notes") or [],
            },
            "floor_height": {
                "override": args.floor_height,
                "policy": args.floor_height_policy or ("auto" if args.floor_height is None else "override"),
                "reference_for_height_text": floor_reference,
            },
            "floors_override": {
                "global": args.floors,
                "by_layer": by_layer_floors,
                "applied": manual_floors,
            },
            "cross_parcel": cross_policy,
            "parcels": [
                {
                    "handle": metric.shape.handle,
                    "layer": metric.shape.layer,
                    "label": seq_map.get(metric.shape.handle, ""),
                    "code": metric.code,
                    "code_display": metric.code_display or metric.code,
                    "hybrid_parts": list(metric.hybrid_parts),
                    "hybrid_unresolved": list(metric.hybrid_unresolved),
                    "name": metric.name,
                    "area_m2": round(metric.area_m2, 3),
                    "footprint_m2": round(metric.footprint_m2, 3),
                    "floor_area_m2": round(metric.floor_area_m2, 3),
                    "far": round(metric.far, 4),
                    "coverage": round(metric.coverage, 4),
                    "floor_height_m": metric.floor_height_m,
                    "floor_height_source": metric.floor_height_source,
                    "floors_weighted": round(metric.floors_weighted, 2),
                    "building_count": metric.building_count,
                    "default_floor_count": len(metric.default_floor_handles),
                    "cross_count": len(metric.cross_handles),
                    "issues": metric.issues,
                }
                for metric in metrics
            ],
            "dedupe": {
                "count": len([row for row in skipped_buildings if row.get("kept_handle")]),
                "rows": [
                    {
                        "handle": row.get("handle", ""),
                        "layer": row.get("layer", ""),
                        "kept_handle": row.get("kept_handle", ""),
                        "reason": row.get("reason", ""),
                    }
                    for row in skipped_buildings
                    if row.get("kept_handle")
                ][:50],
            },
            "buildings": [
                {
                    "handle": str(item.shape.handle),
                    "layer": item.shape.layer,
                    "parcel_handle": item.parcel_handle,
                    "parcel_label": item.parcel_label,
                    "footprint_m2": round(item.footprint_m2, 3),
                    "floors": item.floors,
                    "floor_source": item.floor_source,
                    "floor_kind": item.floor_kind,
                    "height_m": round(item.height_m, 2),
                    "share": round(item.share, 4),
                    "cross": item.cross,
                    "excluded": item.excluded,
                }
                for item in items
            ],
            "totals": {
                "parcels": totals["parcels"],
                "area_m2": round(totals["area_m2"], 3),
                "area_ha": building_massing.ha(totals["area_m2"]),
                "footprint_m2": round(totals["footprint_m2"], 3),
                "floor_area_m2": round(totals["floor_area_m2"], 3),
                "far": round(totals["far"], 4),
                "coverage": round(totals["coverage"], 4),
            },
            "label": {
                "layer": label_layer,
                "template": template,
                "count": label_count,
                "height_m": label_height,
            },
            "extrude": {
                "mode": extrude_result.get("mode"),
                "layer": extrude_layer,
                "ready": len(ready),
                "review": len(review),
                "blocked": len(blocked),
                "counts": extrude_result.get("counts"),
                "review_rows": [
                    {"handle": row["handle"], "reason": row["reason"]} for row in review
                ],
                "blocked_rows": [
                    {"handle": row["handle"], "reason": row["reason"]} for row in blocked
                ],
                "failed": extrude_result.get("failed") or [],
                "layers": extrude_result.get("layers") or [extrude_layer],
                "layering": layering_info,
            },
            "issues": problems,
            "copy_out": copy_path or "",
            "copy_format": copy_format,
            "copy_note": copy_note,
        }
        limit = float(args.far_limit or 0.0)
        payload["far_limit"] = limit
        payload["far_exceed"] = sorted(
            (
                {
                    "label": row["label"],
                    "code": row["code"],
                    "code_display": row.get("code_display") or row["code"],
                    "name": row["name"],
                    "far": row["far"],
                    "coverage": row["coverage"],
                    "area_m2": row["area_m2"],
                    "floor_area_m2": row["floor_area_m2"],
                }
                for row in payload["parcels"]
                if limit > 0 and row["far"] > limit
            ),
            key=lambda row: -row["far"],
        )
        payload["hybrid"] = {
            "rows": hybrid_rows,
            "unresolved": hybrid_groups,
            "ignored": bool(args.ignore_hybrid_unresolved and hybrid_groups),
        }
        payload["parcel_pending"] = pending_parcels
        cli.write_json(args.json_path, payload)
        _write_report(args, payload, metrics, items, problems, spec, unit, unit_source)
        if copy_path:
            print(f"[成果] {copy_note}")
        layering_counts = (payload["extrude"].get("layering") or {}).get("counts") or {}
        layering_text = (
            f"分层 {layering_counts.get('tower', 0)} 塔楼 / {layering_counts.get('podium', 0)} 裙房；"
            if (payload["extrude"].get("layering") or {}).get("mode") not in (None, "none")
            else ""
        )
        print(
            f"[结果] 地块 {len(metrics)} 个，建筑 {len(building_records)} 栋；"
            f"建筑量 {payload['totals']['floor_area_m2']:.2f} ㎡，综合容积率 {payload['totals']['far']:.2f}，"
            f"建筑密度 {payload['totals']['coverage'] * 100:.1f}%；"
            f"拉伸 solid {payload['extrude']['counts'].get('solid', 0)} / mesh {payload['extrude']['counts'].get('mesh', 0)} / "
            f"失败 {payload['extrude']['counts'].get('failed', 0)}；{layering_text}"
            f"成果图层 {'、'.join(payload['extrude'].get('layers') or [])}。"
        )
        return confirm.EXIT_OK
    finally:
        try:
            backend.close()
        except Exception:
            pass


def _extrude_layer_text(extrude_info: Dict) -> str:
    """报告口径行：单层还是塔楼/裙房分层，写明判定依据与阈值。"""
    layering = extrude_info.get("layering") or {}
    mode = extrude_info.get("mode")
    groups = layering.get("groups") or []
    if layering.get("mode") in (None, "none") or not groups:
        return f"{extrude_info.get('layer')}（单层，模式 {mode}）"
    parts = [
        f"{row.get('layer')} {row.get('count')} 栋（{float(row.get('height_min') or 0):g}–{float(row.get('height_max') or 0):g} m）"
        for row in groups
    ]
    if layering.get("mode") == "keep":
        basis = "按源图层塔楼/裙房"
    else:
        basis = f"按 {float(layering.get('tower_height_m') or 0):g} m 高度阈值"
    return " + ".join(parts) + f"（模式 {mode}，{basis}）"


def _extrude_section(extrude_info: Dict, extrude_rows: Sequence) -> str:
    """第七节：拉伸模式 + 成果分层表 + 失败/待复核清单。"""
    layering = extrude_info.get("layering") or {}
    groups = layering.get("groups") or []
    layers = extrude_info.get("layers") or []
    blocks: List[str] = [
        f"拉伸模式 {extrude_info.get('mode')}；成果图层 {'、'.join(layers) or extrude_info.get('layer')}"
    ]
    if groups:
        rows = [
            (
                row.get("layer", ""),
                row.get("type", ""),
                row.get("count", 0),
                f"{float(row.get('height_min') or 0):g}–{float(row.get('height_max') or 0):g}",
                "；".join((row.get("reasons") or [])[:2]),
            )
            for row in groups
        ]
        blocks.append(md_table(["成果图层", "类型", "栋数", "高度区间(m)", "判定依据"], rows))
        if layering.get("mode") == "keep":
            basis = "按源图层点名（塔楼/裙房）"
        else:
            basis = f"按 {float(layering.get('tower_height_m') or 0):g} m 高度阈值"
        blocks.append(f"> 分层口径：{basis}；高度 = 层数 × 层高（简单计容），不是报建高度。")
    if layering.get("note"):
        blocks.append(f"> {layering['note']}")
    if extrude_rows:
        blocks.append(md_table(["类型", "句柄", "说明"], extrude_rows))
    else:
        blocks.append("无异常。")
    return "\n\n".join(blocks)


def _write_report(args, payload, metrics, items, problems, spec, unit, unit_source) -> None:
    out = getattr(args, "report_path", None)
    if not out:
        return
    status = spec_mod.spec_status(spec)
    note = (
        f"> 注：{status}。层高默认值、单图层候选判据与成果图层取 assets/config/massing_rules.json，"
        "替换院内标准后重跑。\n"
    )
    if payload["single_layer"]["used"]:
        note += f"> **单图层偏差提示**：{SINGLE_LAYER_WARNING}\n"

    # 地块筛选：规则必须写在报告开头，用户不用翻异常清单才知道哪些地块被剔除
    parcel_filter = payload.get("parcel_filter") or {}
    filter_mode = parcel_filter.get("mode") or "auto"
    dropped_count = int(parcel_filter.get("dropped_count") or 0)
    filter_text = {
        "auto": (
            "识别到用地性质时只对建设用地（06-16）编号并出容积率/建筑密度，"
            "其中绿地与开敞空间用地（14）默认不编号、不出指标；"
            "非建设用地（01-05、17-24）同样不编号；"
            "识别不到性质时只对有建筑落位的地块编号并出指标"
        ),
        "all": "全部地块都编号并出指标（--parcel-filter all，旧口径）",
        "construction": "只对建设用地（06-16）编号并出指标（--parcel-filter construction）",
        "built": "只对有建筑落位的地块编号并出指标（--parcel-filter built）",
    }.get(filter_mode, "")
    filter_text += (
        f"；本次参与 {parcel_filter.get('parcels_kept', 0)}/{parcel_filter.get('parcels_total', 0)} 个地块，"
        f"剔除 {dropped_count} 个（{parcel_filter.get('dropped_area_m2', 0.0):.2f} ㎡），"
        "明细见异常清单 parcel_filtered"
        if dropped_count
        else f"；本次 {parcel_filter.get('parcels_kept', 0)} 个地块全部参与"
    )
    note += f"> **地块筛选**：{filter_text}\n"

    parcel_rows = [
        (
            row["label"],
            row.get("code_display") or row["code"] or "未识别",
            f"{row['area_m2']:.2f}",
            f"{row['footprint_m2']:.2f}",
            f"{row['floor_area_m2']:.2f}",
            f"{row['far']:.2f}",
            f"{row['coverage'] * 100:.1f}%",
            f"{row['coverage'] * 100:.1f}%",
            f"{row['floors_weighted']:.2f}",
            f"{row['floor_height_m']:g}",
            row["building_count"],
        )
        for row in payload["parcels"]
    ]
    totals = payload["totals"]
    summary_row = (
        "合计",
        "-",
        f"{totals['area_m2']:.2f}",
        f"{totals['footprint_m2']:.2f}",
        f"{totals['floor_area_m2']:.2f}",
        f"{totals['far']:.2f}",
        f"{totals['coverage'] * 100:.1f}%",
        f"{totals['coverage'] * 100:.1f}%",
        f"{(totals['floor_area_m2'] / totals['footprint_m2']) if totals['footprint_m2'] else 0:.2f}",
        "-",
        sum(row["building_count"] for row in payload["parcels"]),
    )

    building_rows = [
        (
            row["parcel_label"] or "-",
            row["handle"],
            row["layer"],
            f"{row['footprint_m2']:.2f}",
            row["floors"],
            row["floor_source"],
            f"{row['height_m']:.2f}",
            "不参与计容" if row.get("excluded") else ("跨界" if row["cross"] else ""),
        )
        for row in payload["buildings"]
    ]
    default_rows = [row for row in payload["buildings"] if row["floor_kind"] == "none"]
    conflict_rows = [row for row in problems if row.get("kind") == "floor_conflict"]
    issue_rows = [
        (item.get("kind", ""), item.get("message", ""), item.get("handle", ""), item.get("layer", ""))
        for item in problems
        if item.get("kind") != "floor_conflict"
    ]
    extrude_info = payload["extrude"]
    extrude_rows = (
        [("需人工闭合", row["handle"], row["reason"]) for row in extrude_info.get("review_rows") or []]
        + [("不可拉伸", row["handle"], row["reason"]) for row in extrude_info.get("blocked_rows") or []]
        + [("拉伸失败", row["handle"], row["reason"]) for row in extrude_info.get("failed") or []]
    )

    hybrid_info = payload.get("hybrid") or {}
    hybrid_table_rows = [
        (
            row.get("layer", ""),
            row.get("code", ""),
            "、".join(row.get("parts") or []),
            "、".join(row.get("unresolved") or []) or "-",
        )
        for row in hybrid_info.get("rows") or []
    ]
    pending_rows = [
        (row.get("layer", ""), "、".join(row.get("unregistered") or []), row.get("reason", ""))
        for row in payload.get("parcel_pending") or []
    ]
    hybrid_blocks: List[str] = []
    if hybrid_table_rows:
        hybrid_blocks.append(
            md_table(["混合用地图层", "合成代码", "各段代码", "未解析段"], hybrid_table_rows)
        )
        hybrid_blocks.append(
            "> 混合用地面积按整块几何只计量一次，不按段拆分重复计容；层高按首段代码判定。"
        )
    if pending_rows:
        hybrid_blocks.append("**用地性质待确认图层**（未进表，确认后重跑才会算入）：")
        hybrid_blocks.append(md_table(["图层", "未登记代码", "原因"], pending_rows))
    if hybrid_info.get("ignored"):
        hybrid_blocks.append(
            "> 混合用地有段未解析，本次按 `--ignore-hybrid-unresolved` 只按首段统计，"
            "**结果需复核，不得直接交付**。"
        )
    hybrid_section = "\n\n".join(hybrid_blocks) or "无混合用地，用地代码全部命中分类表。"

    sections = [
        (
            "二、地块容积率与建筑密度",
            md_table(
                [
                    "编号",
                    "用地代码",
                    "用地面积(㎡)",
                    "基底面积(㎡)",
                    "建筑量(㎡)",
                    "容积率",
                    "建筑密度",
                    "建筑覆盖率",
                    "平均层数",
                    "层高(m)",
                    "建筑数",
                ],
                parcel_rows + [summary_row],
            )
            if parcel_rows
            else (
                (
                    f"按当前地块筛选口径没有可计容地块（范围内 {parcel_filter.get('parcels_total', 0)} 个，"
                    f"剔除 {dropped_count} 个，明细见异常清单 parcel_filtered），"
                    f"本次只出全图建筑量合计（基底 {totals['footprint_m2']:.2f} ㎡ / "
                    f"建筑量 {totals['floor_area_m2']:.2f} ㎡）。"
                )
                if parcel_filter.get("parcels_total")
                else (
                    f"未识别用地地块：本次只出全图建筑量合计（基底 {totals['footprint_m2']:.2f} ㎡ / "
                    f"建筑量 {totals['floor_area_m2']:.2f} ㎡），容积率与建筑密度不适用。"
                )
            ),
        ),
        (
            "三、建筑清单（基底 / 层数 / 高度）",
            md_table(
                ["所属地块", "句柄", "图层", "基底面积(㎡)", "层数", "层数依据", "高度(m)", "备注"],
                building_rows,
            )
            if building_rows
            else "无可归属建筑。",
        ),
        (
            "四、校验",
            md_table(
                ["校验项", "结果"],
                [
                    (
                        "用地面积合计",
                        f"{totals['area_m2']:.2f} ㎡（{totals['area_ha']} ha）" if parcel_rows else "不适用（无用地边界）",
                    ),
                    (
                        "参与测算地块",
                        (
                            f"{parcel_filter.get('parcels_kept', 0)} 个 / 范围内 {parcel_filter.get('parcels_total', 0)} 个；"
                            f"用地面积 {parcel_filter.get('area_kept_m2', 0.0):.2f} ㎡"
                            f"（范围内合计 {parcel_filter.get('area_total_m2', 0.0):.2f} ㎡）"
                        )
                        if parcel_filter.get("parcels_total")
                        else "不适用",
                    ),
                    (
                        "剔除地块",
                        (
                            f"{dropped_count} 个、{parcel_filter.get('dropped_area_m2', 0.0):.2f} ㎡；"
                            "原因与句柄见异常清单 parcel_filtered"
                        )
                        if dropped_count
                        else "无",
                    ),
                    ("基底面积合计", f"{totals['footprint_m2']:.2f} ㎡"),
                    ("建筑量合计", f"{totals['floor_area_m2']:.2f} ㎡"),
                    ("综合容积率", f"{totals['far']:.2f}" if parcel_rows else "不适用（无用地边界）"),
                    ("综合建筑密度", f"{totals['coverage'] * 100:.1f}%" if parcel_rows else "不适用（无用地边界）"),
                    (
                        "综合建筑覆盖率",
                        f"{totals['coverage'] * 100:.1f}%（与建筑密度同义）"
                        if parcel_rows
                        else "不适用（无用地边界）",
                    ),
                    (
                        "建筑密度异常",
                        "有地块建筑密度 >100%，见异常清单"
                        if any(row["coverage"] > 1.0 for row in payload["parcels"])
                        else "无",
                    ),
                    (
                        "无层数建筑",
                        f"{len(default_rows)} 栋按 1 层计入（容积率可能被低估）" if default_rows else "无",
                    ),
                    ("层数冲突", f"{len(conflict_rows)} 条，见异常清单" if conflict_rows else "无"),
                    (
                        "重复表达去重",
                        f"{payload['dedupe']['count']} 处（同层填充与多段线重复描同一轮廓，只算一次）"
                        if payload["dedupe"]["count"]
                        else "无",
                    ),
                    ("闭合性", f"可拉伸 {extrude_info['ready']} 栋 / 需复核 {extrude_info['review']} 栋 / 不可拉伸 {extrude_info['blocked']} 栋"),
                ],
            ),
        ),
        (
            "五、混合用地与用地性质确认",
            hybrid_section,
        ),
        (
            "六、异常与待复核清单",
            md_table(["类型", "说明", "句柄", "图层"], issue_rows) if issue_rows else "无。",
        ),
        (
            "七、三维拉伸成果",
            _extrude_section(extrude_info, extrude_rows),
        ),
        (
            f"八、容积率超限地块（> {payload['far_limit']:g}）",
            md_table(
                ["编号", "用地代码", "容积率", "建筑覆盖率", "用地面积(㎡)", "建筑量(㎡)"],
                [
                    (
                        row["label"],
                        row.get("code_display") or row["code"] or "未识别",
                        f"{row['far']:.2f}",
                        f"{row['coverage'] * 100:.1f}%",
                        f"{row['area_m2']:.2f}",
                        f"{row['floor_area_m2']:.2f}",
                    )
                    for row in payload["far_exceed"]
                ],
            )
            if payload["far_exceed"]
            else (
                f"无超过 {payload['far_limit']:g} 的地块。"
                if payload["far_limit"] > 0
                else "未设超限阈值。"
            ),
        ),
        ("附：句柄定位", locator_note()),
    ]

    meta = [
        ("测算任务", {"measure": "容积率测算", "extrude": "批量建模", "all": "测算 + 建模"}.get(payload["task"], payload["task"])),
        ("建筑量口径", "基底面积 × 层数，只含主体建筑的简单计容建筑面积；未扣阳台、架空层、设备层、地下车库"),
        (
            "容积率口径",
            "地块建筑量 ÷ 用地面积；建筑密度 = 建筑覆盖率 = 基底面积 ÷ 用地面积（本表两列并列，数值相同）",
        ),
        (
            "超限提示",
            f"容积率 > {payload['far_limit']:g} 的地块单列第八节" if payload["far_limit"] > 0 else "未设阈值",
        ),
        ("层数口径", "砼7 / 7F / 7层 → 对应层数；砼单独 → 1 层；无任何层数/高度信息 → 1 层（单列清单）"),
        (
            "层数来源",
            (
                "；".join(
                    ([f"统一 {payload['floors_override']['global']} 层"] if payload["floors_override"]["global"] else [])
                    + [
                        f"{name} = {value} 层"
                        for name, value in (payload["floors_override"]["by_layer"] or {}).items()
                    ]
                )
                + f"（命令行指定 {payload['floors_override']['applied']} 栋）"
            )
            if (payload["floors_override"]["global"] or payload["floors_override"]["by_layer"])
            else "仅取图上文字标注；未标注建筑按 1 层",
        ),
        (
            "层高",
            (
                f"命令行统一 {payload['floor_height']['override']:g}m"
                if payload["floor_height"]["override"] is not None
                else "按用地性质默认（住宅 3.0 / 已识别非住宅 4.5 / 未识别 3.0）"
            )
            + f"；高度文本换算参考 {payload['floor_height']['reference_for_height_text']:g}m",
        ),
        ("跨界建筑", {"point": "代表点整栋归属", "clip": "按相交面积分摊", "exclude": "整栋排除"}.get(payload["cross_parcel"], payload["cross_parcel"])),
        (
            "规划范围",
            (
                f"{','.join(payload['scope']['layers'])}（剔除范围外地块 "
                f"{payload['scope']['dropped_parcels']} 个、建筑 {payload['scope']['dropped_buildings']} 栋）"
                if payload["scope"]["used"]
                else "未裁剪（整图测算）"
            ),
        ),
        (
            "图层确认",
            (
                "命令行显式指定"
                if payload["layer_confirm"]["explicit"]
                else (
                    "已输出候选供确认"
                    if payload["layer_confirm"]["asked"]
                    else "自动识别（--accept-layers）"
                )
            ),
        ),
        (
            "用地性质口径",
            (
                f"混合用地按合成代码显示（共 {len(hybrid_table_rows)} 个图层），面积只计量一次，层高按首段；"
                "未登记/自定义代码先确认再算，不静默剔除也不按关键词兜底"
            ),
        ),
        ("建筑图层", ",".join(payload["layers"]["building"]) or "未分层（单图层候选识别）"),
        ("用地图层", ",".join(payload["layers"]["parcel"]) or "-"),
        (
            "层数标注图层",
            ",".join(payload["layers"]["height"]) + "（仅扫这些图层）"
            if payload["layers"]["height"]
            else "全图扫描文字（未指定图层，标注分散时较慢）",
        ),
        ("单图层识别", "已启用（结果需人工复核）" if payload["single_layer"]["used"] else "未启用（建筑已单独分层）"),
        ("指标标注图层", f"{payload['label']['layer']}（模板 {payload['label']['template']}，{payload['label']['count']} 处）"),
        ("体块图层", _extrude_layer_text(payload["extrude"])),
        ("图纸单位", f"{unit}（{unit_source}）"),
        ("成果副本", payload.get("copy_note") or "未生成（--dry-run）"),
    ]
    write_markdown(out, "批量建筑量 / 容积率测算表", meta, sections, status_note=note)


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
