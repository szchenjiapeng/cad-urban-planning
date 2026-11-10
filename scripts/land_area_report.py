#!/usr/bin/env python
"""CAD 用地面积测算 + 土地利用规划平衡表 CLI。

处理实际图纸的常见不规范情况：
- 关闭/冻结图层（草稿）默认不统计，布局与模型状态不一致时中断并询问；
- 块参照在临时副本中递归展开，绝不修改原图；
- 按图层关键词自动识别规划范围，多候选/多环关系有歧义时中断并询问；
- 识别不到范围时中断询问（指定图层 / 提供同坐标系范围线文件 / 无范围），不静默按全图统计；
- 规划范围与地块相交时几何求交（shapely），不改图，写回只生成副本；
- 无范围口径（--scope none）只统计可见图层填充，不做裁剪、范围相交判定、差额校验与道路反算。

需要用户确认时：输出 confirmations JSON、退出码 3，由会话向用户提问后带参数重跑。

用法示例：
    python land_area_report.py --check
    python land_area_report.py --file 总图.dxf --total-area 128.6 --population 32000
    python land_area_report.py --source com --write-back
    python land_area_report.py --file 总图.dxf --layer-state layout --scope-layer "规划范围"
    python land_area_report.py --file 总图.dxf --scope-file 范围线.dxf
    python land_area_report.py --file 总图.dxf --scope none
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import pickle
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from backends import create_backend, probe_environment  # noqa: E402
from core.classify import (  # noqa: E402
    MatchRule,
    build_summary,
    guess_standard,
    load_conversion,
    load_mapping,
    load_standard,
    missing_rule_codes,
    suggest_node,
)
from core.geometry import (  # noqa: E402
    bbox_center,
    bbox_of,
    bbox_overlap_ratio,
    crosses_window,
    discretize,
    in_window,
    is_closed,
    point_in_polygon,
    polygon_area,
    resolve_unit,
    self_intersects,
)
from core.hatch import interior_point, net_area, outer_ring  # noqa: E402
from core.layer_state import (  # noqa: E402
    MODE_ALL,
    MODE_AUTO,
    MODE_LAYOUT,
    MODE_MODEL,
    build_report,
    confirmation_item,
)
from core.models import (  # noqa: E402
    CheckItem,
    HatchRecord,
    Issue,
    ParcelRecord,
    PolylineRecord,
    RunMeta,
    Summary,
)
from core.overlap import compute_gate, resolve_overlaps, summarize as summarize_overlaps  # noqa: E402
from core.scope_file import (  # noqa: E402
    check_alignment,
    load_scope_file,
    scope_file_mismatch_item,
    scope_file_units_item,
)
from core.scope_region import (  # noqa: E402
    HAS_SHAPELY,
    SCOPE_KEYWORDS,
    ScopeCandidate,
    ScopeRegion,
    analyze_relation,
    build_region,
    closed_polyline_candidates,
    collect_candidates,
    confirmation_items,
    group_by_layer,
    layer_inventory,
    match_layers_by_keyword,
    scope_draft_check_item,
    scope_layer_choice_item,
    scope_layer_invalid_item,
    scope_missing_item,
)
from render.balance import BalanceSpec  # noqa: E402
from render.markdown_report import render_markdown, render_overlap_report  # noqa: E402

_ARC_STEP_DEG = 2.0
EXIT_NEED_CONFIRM = 3


def _setup_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


# ---------------------------------------------------------------- 参数解析
def _expand_codes(value: str) -> List[str]:
    """支持 06-16 这类区间写法，也支持 07,08,09 逐项写法。"""
    codes: List[str] = []
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, _, end = part.partition("-")
            start, end = start.strip(), end.strip()
            if start.isdigit() and end.isdigit() and len(end) == 2:
                codes.extend(f"{i:02d}" for i in range(int(start), int(end) + 1))
                continue
        codes.append(part)
    return codes


def _split_list(value: Optional[str]) -> Optional[List[str]]:
    if not value:
        return None
    items = [item.strip() for item in value.replace("；", ",").replace(";", ",").split(",")]
    return [item for item in items if item]


def _parse_window(value: str) -> Tuple[float, float, float, float]:
    parts = [p.strip() for p in value.replace("，", ",").split(",") if p.strip()]
    if len(parts) != 4:
        raise ValueError("--window 需要 4 个数值：xmin,ymin,xmax,ymax")
    xmin, ymin, xmax, ymax = (float(p) for p in parts)
    if xmax <= xmin or ymax <= ymin:
        raise ValueError("--window 需满足 xmax>xmin 且 ymax>ymin")
    return xmin, ymin, xmax, ymax


def _parse_polygon(value: str) -> List[Tuple[float, float]]:
    tokens = value.replace("；", ";").replace("，", ",").replace(",", " ").split()
    if len(tokens) < 6 or len(tokens) % 2 != 0:
        raise ValueError('--polygon 需要成对坐标，例如 "0,0 100,0 100,100 0,100"')
    numbers = [float(t) for t in tokens]
    return [(numbers[i], numbers[i + 1]) for i in range(0, len(numbers), 2)]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="读取 CAD 图纸，测算用地面积并生成土地利用规划平衡表（填充优先，支持规划范围裁剪）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--source", default="auto", choices=["auto", "com", "dxf"], help="数据通道；auto=有 --file 走 DXF，否则连 CAD（活动→启动）")
    parser.add_argument("--file", help="DXF 文件路径（DWG 需先另存为 DXF）")
    parser.add_argument("--progid", help="CAD COM ProgID，默认 AutoCAD.Application，可用环境变量 CAD_PROGID 覆盖")
    parser.add_argument("--no-start-cad", dest="start_cad", action="store_false", default=True, help="CAD 未运行时不要自动启动已安装的 CAD")
    parser.add_argument("--primitive", default="hatch", choices=["hatch", "auto", "polyline", "both"], help="识别对象；默认 hatch=只算填充（城镇道路等不填充类目靠反算），auto=整图无填充时整体回退多段线")
    parser.add_argument("--allow-polyline-fill", action="store_true", help="允许用多段线补充填充缺失区域（默认关闭，会与道路反算重复计面积）")
    parser.add_argument("--derive-road", default="auto", choices=["auto", "on", "off"], help="是否按差额反算道路用地；off 关闭，auto/on 均需有规划范围面积且覆盖差额在容差内（on 仅表示强制尝试）")
    parser.add_argument("--derive-road-tolerance", type=float, default=1.0, help="允许反算的未识别面积上限（占规划范围百分比），默认 1%%，超过则阻断反算")
    parser.add_argument(
        "--accept-default-params",
        action="store_true",
        help="单位未显式指定（--unit auto）时，直接接受由 $INSUNITS 自动判定的单位，不再抛确认项",
    )
    parser.add_argument("--params-note", help="口径依据说明（如单位/反算的院内规定），写入报告")
    parser.add_argument("--derive-road-max-share", type=float, default=40.0, help="反算道路占规划范围的经验上限（百分比），超过则标需复核，默认 40%%")
    parser.add_argument("--no-hatch-fallback", dest="hatch_fallback", action="store_false", default=True, help="关闭填充 Area 读不到时的 DXF 导出兜底")
    parser.add_argument(
        "--cad-areas",
        help="CAD 自算面积清单（JSON：{句柄: 面积m²} 或 [{handle, area}]），DXF 通道用它把面积基准换成 CAD 的 .Area",
    )
    parser.add_argument(
        "--no-scan-cache",
        dest="no_scan_cache",
        action="store_true",
        help="禁用图元扫描缓存（默认按图纸指纹复用上次扫描结果，重跑秒级）",
    )
    parser.add_argument(
        "--arc-variant",
        default="off",
        choices=["off", "on"],
        help="填充边界的弧段离散：off=默认策略（推荐）；on=额外尝试信任 ccw 的还原，仅在与 CAD 面积吻合且包围盒正常时采用，用于排查",
    )
    parser.add_argument("--overlap-report", action="store_true", help="输出重叠明细报告（默认只在报告里提示数量与面积，先问用户再生成）")
    parser.add_argument(
        "--overlap-threshold",
        type=float,
        default=0.01,
        help="重叠告警阈值：重叠面积占规划范围总面积的比例，默认 0.01（1%%），0=关闭。跨图层超阈值时中断出四选一确认项；同图层超阈值只在报告与摘要里着重提示，不追问",
    )
    parser.add_argument(
        "--overlap-ack",
        action="store_true",
        help="已确认跨图层重叠：按当前阈值放行，出平衡表但不出明细报告（对应四选一的选项 1）",
    )
    parser.add_argument(
        "--diagnose-only",
        action="store_true",
        help="只输出排查清单（未识别/异常/重叠/道路反算状态），不生成平衡表（对应四选一的选项 3）",
    )
    parser.add_argument("--construction-codes", help="覆盖建设用地口径，如 06-16 或 07,08,09；缺省读分类表 balance_groups")
    parser.add_argument("--overlap-min-area", type=float, default=0.5, help="重叠面积下限（m²），低于此值忽略，默认 0.5")
    parser.add_argument("--overlap-min-ratio", type=float, default=0.01, help="重叠占较小地块的比例下限，默认 1%%")
    parser.add_argument("--blocks", default="expand", choices=["expand", "skip"], help="块参照是否展开；expand=在副本中展开，不动原图")
    parser.add_argument("--standard", default="2023", choices=["2023", "gb50137"], help="分类标准，默认 2023 指南")
    parser.add_argument("--convert-to", default="none", choices=["none", "2023"], help="把旧标准统计结果换算为新标准（如 gb50137 → 2023）")
    parser.add_argument("--conversion", help="自定义新旧代码换算表 JSON")
    parser.add_argument("--classification", help="自定义分类树 JSON")
    parser.add_argument("--mapping", help="自定义图层映射 JSON")
    parser.add_argument(
        "--add-code",
        action="append",
        metavar="CODE:名称[:层级:父级]",
        help="确认新增用地代码：先登记进分类配置与指引表，再按该代码归类（可重复）",
    )
    parser.add_argument(
        "--unregistered-code",
        action="append",
        metavar="CODE=ACTION",
        help="未登记代码的处理：skip=本次沿用兜底归类（报告标注需复核）／map:目标代码=归入已有代码（可重复）",
    )
    parser.add_argument(
        "--ignore-hybrid-unresolved",
        action="store_true",
        help="混合用地里有段既非表内代码也非表内名称时，不再追问，只按首段代码统计（报告标注需复核）",
    )
    parser.add_argument("--scope", default="all", choices=["all", "window", "polygon", "handles", "none"], help="统计范围方式；all=按图层自动识别规划范围，none=无规划范围（全部可见图层填充合计）")
    parser.add_argument("--scope-file", help="外部规划范围线文件（DXF；DWG 需本机有 CAD）；要求与总图同坐标系原位，脚本只做单位与包围盒粗检")
    parser.add_argument("--scope-file-units", default="auto", choices=["auto", "assume-drawing"], help="范围线文件与总图单位不一致时如何处理；assume-drawing=已对齐，按总图单位直接解释")
    parser.add_argument("--window", help="矩形范围 xmin,ymin,xmax,ymax（图纸单位）")
    parser.add_argument("--polygon", help="多边形范围，成对坐标")
    parser.add_argument("--handles", help="指定对象句柄，逗号分隔")
    parser.add_argument("--scope-source", default="auto", choices=["auto", "layer", "manual"], help="规划范围来源；auto/layer=按图层识别，manual=只用 window/polygon/handles")
    parser.add_argument("--scope-layers", help="规划范围图层关键词，逗号分隔，默认含规划范围/范围/range/红线等")
    parser.add_argument("--scope-layer", help="指定作为规划范围的图层名（多候选时必填）")
    parser.add_argument("--scope-relation", default="auto", choices=["auto", "union", "difference", "intersect"], help="多个范围边界的空间关系处理方式")
    parser.add_argument("--scope-outer-only", action="store_true", help="多个范围边界时只取面积最大的一个")
    parser.add_argument("--layers", help="只统计这些图层，逗号分隔")
    parser.add_argument("--exclude-layers", help="排除这些图层，逗号分隔")
    parser.add_argument("--layer-state", default="auto", choices=["auto", "model", "layout", "all"], help="图层可见性口径；auto=检测到模型/布局差异时中断询问")
    parser.add_argument("--layout", help="指定布局名，默认取图纸中最后一个布局")
    parser.add_argument("--include-codes", help="只统计这些用地代码")
    parser.add_argument("--exclude-codes", help="排除这些用地代码")
    parser.add_argument("--unit", default="auto", help="图纸单位，auto 或 mm/cm/m/km/in/ft")
    parser.add_argument("--closure-tolerance", type=float, help="闭合容差（图纸单位），默认按全图跨度 1e-5 推算")
    parser.add_argument("--total-area", type=float, help="规划范围总面积（公顷）；未提供且识别到范围时自动取范围面积")
    parser.add_argument("--population", type=float, help="规划人口（人），用于计算人均用地")
    parser.add_argument("--clip", default="auto", choices=["auto", "intersect", "whole", "exclude"], help="范围裁剪方式；auto=有 shapely 求交，否则退化。范围外地块一律不统计，whole 仅作复核用逃生口")
    parser.add_argument("--clip-fallback", default="whole", choices=["whole", "exclude"], help="无 shapely 时跨界地块的处理：整块计入或排除")
    parser.add_argument(
        "--clip-strict",
        default="ask",
        choices=["ask", "on", "off"],
        help=(
            "CAD 直连读不到填充边界时的裁剪方式；ask=询问用户（推荐），"
            "on=导出 DXF 严格裁剪（准确但可能慢），off=保持直连（快，范围外填充可能整块计入）"
        ),
    )
    parser.add_argument("--write-back", action="store_true", help="把面积标注写回图纸（DXF 只写副本）")
    parser.add_argument("--colorize", action="store_true", help="按一级类给已归类图元着色（需 --write-back）")
    parser.add_argument("--label-layer", default="AREA_LABEL", help="面积标注图层名")
    parser.add_argument("--clipped-layer", default="AREA_CLIPPED", help="裁剪后边界副本的图层名")
    parser.add_argument("--out", default="land_area_report.md", help="Markdown 输出路径或目录")
    parser.add_argument("--json", help="JSON 摘要输出路径，默认与 Markdown 同名")
    parser.add_argument("--confirm-out", default="land_area_confirm.json", help="需要确认时的清单输出路径")
    parser.add_argument(
        "--confirm-mode",
        default="batch",
        choices=["batch", "early"],
        help=(
            "确认项输出方式：batch=一次汇总全部待确认项（默认，最多两轮：计算前 / 计算后）；"
            "early=扫描前发现问题就中断（大图可省一次扫描，但问题会分多轮问）"
        ),
    )
    parser.add_argument("--list-layers", action="store_true", help="只列出图层清单后退出")
    parser.add_argument(
        "--list-scope-layers",
        action="store_true",
        help="规划范围第一步：只读图层表，列出图层名含范围关键词的候选图层（不读图元，秒出），供用户选定",
    )
    parser.add_argument("--list-layer-state", action="store_true", help="只输出图层可见性与差异后退出")
    parser.add_argument("--list-scope", action="store_true", help="只输出规划范围候选后退出（自动只扫范围相关图层，秒级）")
    parser.add_argument("--check", action="store_true", help="只做环境自检后退出")
    parser.add_argument(
        "--scan-timeout",
        type=float,
        default=180.0,
        help="COM 单个阶段无进展的超时秒数，超时打印提示并以退出码 4 结束；0=关闭，默认 180",
    )
    parser.add_argument("--quiet", action="store_true", help="不在终端打印摘要")
    return parser


# ---------------------------------------------------------------- 图元收集
def _infer_span(polylines: Sequence[PolylineRecord], hatches: Sequence[HatchRecord]) -> Optional[float]:
    points = [pt for record in polylines for pt in record.vertices]
    for hatch in hatches:
        if hatch.rings:
            points.extend(pt for ring in hatch.rings for pt in ring)
    if not points:
        return None
    box = bbox_of(points)
    return max(box[2] - box[0], box[3] - box[1])


def _recover_broken_hatches(
    backend, hatches: Sequence[HatchRecord], enabled: bool, notes: List[str]
) -> int:
    """把读不到 Area 的填充导出 DXF，用 ezdxf 重建边界并算净面积。"""
    broken = [h for h in hatches if not h.rings and h.area_drawing is None]
    if not broken:
        return 0
    if not enabled:
        notes.append(f"{len(broken)} 个填充无法读取面积，已按 --no-hatch-fallback 跳过，未计入合计。")
        return 0
    if backend.channel != "com":
        return 0

    tmp_dir = Path(tempfile.mkdtemp(prefix="cad_hatch_"))
    dxf_path = tmp_dir / "hatch_fallback.dxf"
    recovered = 0
    try:
        if not backend.export_handles_to_dxf([h.handle for h in broken], str(dxf_path)):
            notes.append(
                f"{len(broken)} 个填充的 Area 读取失败，且导出 DXF 兜底未成功；"
                "请在 CAD 中手动另存 DXF 后用 --file 重跑。"
            )
            return 0

        from backends.dxf_backend import DxfBackend

        fallback = DxfBackend(str(dxf_path))
        try:
            exported = [h for h in fallback.iter_hatches(None) if h.rings]
        finally:
            fallback.close()

        by_handle = {h.handle.upper(): h for h in exported}
        unmatched: List[HatchRecord] = []
        for hatch in broken:
            match = by_handle.get(hatch.handle.upper())
            if match is not None:
                hatch.rings = match.rings
                hatch.area_error = ""
                recovered += 1
            else:
                unmatched.append(hatch)

        if unmatched and len(exported) == len(broken) and exported:
            for source, target in zip(exported, unmatched):
                target.rings = source.rings
                target.area_error = ""
                recovered += 1
            notes.append("填充兜底导出后部分句柄发生变化，已按导出顺序回填边界。")

        notes.append(
            f"其中 {recovered} 个填充的 Area 无法通过 ActiveX 读取（如报“输入无效”），"
            "已导出 DXF 后按边界几何重建，并按奇偶嵌套规则计算净面积。"
        )
        return recovered
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _merge_cad_areas(hatches: Sequence[HatchRecord], cad_areas: dict, notes: List[str]) -> int:
    """切到 DXF 副本后，把 CAD 自算面积按句柄写回，副本只提供边界。

    原因：DXF 里的弧边要重新离散，长弧方向标错时面积会成倍放大（本图 10EC957
    从 3.69 ha 变成 30.44 ha、101C722 从 2.70 ha 变成 22.30 ha），而 CAD 的
    .Area 与 AREA 命令同源，是可用于对账的基准值。
    """
    if not cad_areas:
        return 0
    filled = 0
    mismatched = 0
    for hatch in hatches:
        area = cad_areas.get(hatch.handle.upper())
        if not area or area <= 0:
            continue
        hatch.area_drawing = float(area)
        hatch.area_error = ""
        filled += 1
        if hatch.rings:
            geom_area, _infos = net_area(hatch.rings)
            if geom_area > 0 and abs(float(area) - geom_area) / max(float(area), 1.0) > 0.05:
                mismatched += 1
    if filled:
        notes.append(
            f"{filled} 个填充的面积改取 CAD 自算值，DXF 副本只用来提供边界；"
            f"其中 {mismatched} 个的弧段离散结果与 CAD 相差超过 5%，已按 CAD 值计。"
        )
    return filled


def _load_cad_areas(path: str) -> dict:
    """读取 CAD 自算面积清单：{句柄: 面积} 或 [{handle|句柄, area|面积}]。

    DXF 里没有 Area 属性，几何重建与 CAD 允许有微小差异（弧段离散）；把 CAD 的
    .Area 按句柄导进来，面积才能与用户在本图里 AREA 量的结果完全一致。
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception as exc:
        print(f"[警告] 读取 --cad-areas 失败：{exc}", file=sys.stderr)
        return {}
    mapping: dict = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            try:
                mapping[str(key).upper()] = float(value)
            except (TypeError, ValueError):
                continue
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            handle = item.get("handle") or item.get("句柄") or item.get("Handle")
            area = item.get("area") or item.get("面积") or item.get("Area")
            try:
                if handle and area:
                    mapping[str(handle).upper()] = float(area)
            except (TypeError, ValueError):
                continue
    return mapping


# v3：多段线凸度（bulge）必须参与范围面积与裁剪，v2 及更早的缓存里 bulges 为空，必须失效
_SCAN_CACHE_VERSION = 3


def _scan_cache_path(
    signature: Optional[str],
    layer_patterns: Optional[Sequence[str]],
    scope: str,
) -> Optional[Path]:
    """扫描缓存的存放路径：源图指纹 + 通道 + 扫描过滤条件 + 缓存版本。

    大图全量扫描一次十几分钟，而「确认参数后重跑」只改了分类口径，图元一模一样。
    按指纹命中后就不再重扫，把「先问再算」的成本从分钟级压到秒级。

    scope 用来区分「直连 CAD 扫原始图」与「扫导出的 DXF 副本」：后者的文件名是
    临时目录下的 blocks.dxf，直接按它自己的 mtime 做键会在临时目录删掉后永远命中不了，
    所以统一用源 DWG 的指纹。
    """
    if not signature:
        return None
    parts = signature.split("|")
    key = signature + "|" + scope + "|" + ",".join(sorted(layer_patterns or [])) + f"|v{_SCAN_CACHE_VERSION}"
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:12]
    stem = Path(parts[1]).stem if len(parts) > 1 else "scan"
    cache_dir = Path(tempfile.gettempdir()) / "cad_urban_cache"
    return cache_dir / f"scan_{stem}_{scope}_{digest}.pkl.gz"


def scan_or_load(
    backend,
    layer_patterns: Optional[Sequence[str]],
    args,
    notes: List[str],
    signature: Optional[str] = None,
    scope: Optional[str] = None,
):
    """扫一遍图元；命中缓存就直接读，扫完顺手写缓存（gzip，几百 MB 降到几十 MB）。"""
    scope = scope or backend.channel
    if signature is None:
        try:
            signature = backend.source_signature()
        except Exception:
            signature = None
    if getattr(args, "no_scan_cache", False):
        cache = None
    else:
        cache = _scan_cache_path(signature, layer_patterns, scope)
        if cache is None:
            notes.append(
                "图纸存在未保存改动或路径不可读，本次不写扫描缓存；保存后重跑即可复用上一次的扫描结果。"
            )

    if cache is not None and cache.exists():
        try:
            with gzip.open(cache, "rb") as handle:
                payload = pickle.load(handle)
            polylines = list(payload["polylines"])
            hatches = list(payload["hatches"])
            notes.append(
                f"命中扫描缓存（{cache.name}，图纸自上次扫描后未修改），本次未重新扫图元；"
                "如需强制重扫请加 --no-scan-cache。"
            )
            return polylines, hatches
        except Exception:
            try:
                cache.unlink()
            except Exception:
                pass

    polylines = list(backend.iter_polylines(None, layer_patterns=layer_patterns))
    hatches = list(backend.iter_hatches(None, layer_patterns=layer_patterns))

    if cache is not None:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(cache, "wb", compresslevel=1) as handle:
                pickle.dump(
                    {"polylines": polylines, "hatches": hatches},
                    handle,
                    protocol=pickle.HIGHEST_PROTOCOL,
                )
        except Exception:
            pass
    return polylines, hatches


def _maybe_switch_to_dxf(backend, args, force: bool = False, reason: str = "") -> Tuple[object, Optional[Path], List[str]]:
    """COM 通道导出整图 DXF 后改用 DXF 通道，原图不动。

    触发条件：默认是「检测到块参照」；force=True 时跳过块判断，用于
    「必须按规划范围严格裁剪、但 COM 读不到填充边界」的场景。

    导出结果会落在固定缓存路径（图纸未保存改动时按「路径 + 修改时间」命中），
    同一张图纸重跑时直接复用，不必再让 CAD 导一遍。
    """
    notes: List[str] = []
    label = reason or "块参照"
    if backend.channel != "com":
        return backend, None, notes
    if not force:
        # 选了严格裁剪时不在扫描前切 DXF：那样会丢掉 CAD 自算面积（弧段离散会把面积算大）。
        # 交给严格裁剪分支导副本 + 按句柄回填 CAD 面积，块参照同样会展开。
        if args.clip_strict == "on":
            notes.append(
                "已选严格裁剪：扫描阶段保持直连 CAD 取面积，块参照与填充边界在裁剪阶段由 DXF 副本提供。"
            )
            return backend, None, notes
        if args.blocks != "expand":
            return backend, None, notes
        try:
            has_blocks = backend.has_block_references()
        except Exception:
            has_blocks = False
        if not has_blocks:
            return backend, None, notes

    cached = None
    try:
        cached = backend.cached_dxf()  # type: ignore[attr-defined]
    except Exception:
        cached = None

    reusable = bool(cached) and Path(str(cached)).exists()
    if reusable:
        dxf_path = Path(str(cached))
        tmp_dir: Optional[Path] = None  # 缓存目录不随本次运行删除
        notes.append(f"检测到{label}，复用了上次整图导出的 DXF 缓存（图纸自那以后未修改）。")
    else:
        tmp_dir = Path(tempfile.mkdtemp(prefix="cad_blocks_"))
        dxf_path = Path(str(cached)) if cached else tmp_dir / "blocks.dxf"
        print(
            f"[DXF 副本] 检测到{label}，正在把整图导出为 DXF（大图可能较慢）……",
            file=sys.stderr,
            flush=True,
        )
        try:
            backend.prepare_cache_dir(str(dxf_path))  # type: ignore[attr-defined]
        except Exception:
            pass
        if not backend.export_all_to_dxf(str(dxf_path)):
            notes.append(
                f"检测到{label}，但整图导出 DXF 失败；本图直连无法保证精确裁剪，"
                "建议在 CAD 中另存 DXF 后用 --file 重跑。"
            )
            return backend, tmp_dir, notes
        if cached:
            notes.append(f"检测到{label}，整图已导出为 DXF 并缓存，同一图纸重跑时直接复用；用户图纸未被修改。")
        else:
            notes.append(f"检测到{label}，已在临时导出的 DXF 副本中统计，用户图纸未被修改。")

    from backends.dxf_backend import DxfBackend

    try:
        new_backend = DxfBackend(str(dxf_path), expand_blocks=True)
    except Exception as exc:
        notes.append(f"块展开失败：{exc}；块内图元未统计。")
        return backend, tmp_dir, notes

    try:
        backend.close()
    except Exception:
        pass
    return new_backend, tmp_dir, notes


# ---------------------------------------------------------------- 地块构建
def _build_polyline_parcels(
    records: Sequence[PolylineRecord],
    accept: Callable[[str, str], bool],
    tolerance: float,
    factor: float,
    issues: List[Issue],
) -> List[ParcelRecord]:
    parcels: List[ParcelRecord] = []
    for record in records:
        if not accept(record.layer, record.handle):
            continue
        if not record.vertices:
            issues.append(Issue("invalid", "未能读取顶点（可能是三维多段线或读取异常）", record.handle, record.layer))
            continue
        if len(record.vertices) < 3:
            issues.append(Issue("zero_area", "顶点少于 3 个，无法围合", record.handle, record.layer))
            continue

        geometry_closed, gap = is_closed(record.vertices, tolerance)
        if not (record.closed_flag or geometry_closed):
            issues.append(
                Issue(
                    "unclosed",
                    f"首尾间距 {gap:.4f} 图纸单位，超过容差 {tolerance:.4f}，未计入合计",
                    record.handle,
                    record.layer,
                )
            )
            continue

        points = discretize(record.vertices, record.bulges, close=True, max_step_deg=_ARC_STEP_DEG)
        raw_area = record.cad_area if record.cad_area is not None else polygon_area(points)
        area_m2 = raw_area * factor * factor
        if area_m2 <= 1e-6:
            issues.append(Issue("zero_area", "围合面积为 0", record.handle, record.layer))
            continue
        if self_intersects(points):
            issues.append(Issue("self_intersect", "边界自相交，面积可能失真，请复核", record.handle, record.layer))

        parcels.append(
            ParcelRecord(
                handle=record.handle,
                layer=record.layer,
                entity_type=record.entity_type,
                area_m2=area_m2,
                closed_flag=record.closed_flag,
                geometry_closed=geometry_closed,
                source_kind="polyline",
                outer=points,
            )
        )
    return parcels


def _build_hatch_parcels(
    hatches: Sequence[HatchRecord],
    accept: Callable[[str, str], bool],
    factor: float,
    issues: List[Issue],
    arc_stats: Optional[Dict[str, int]] = None,
    use_arc_variant: bool = False,
) -> List[ParcelRecord]:
    parcels: List[ParcelRecord] = []
    for hatch in hatches:
        if not accept(hatch.layer, hatch.handle):
            continue

        cad_area = float(hatch.area_drawing) if hatch.area_drawing else 0.0
        geom_area = 0.0
        infos = []
        outer = None
        rings = None
        if hatch.rings:
            geom_area, infos = net_area(hatch.rings)
            outer = outer_ring(infos)
            rings = list(hatch.rings)

        if hatch.rings_alt:
            # 边界含弧：统计条数，报告里给出「弧段还原」校验行（面积本身取 CAD 值，不受影响）
            if arc_stats is not None:
                arc_stats["curved"] = arc_stats.get("curved", 0) + 1

        # 备选离散默认不参与裁剪：信任 ccw 的还原偶尔会把弧拉成绕全图的巨弧
        # （本图 101C722 包围盒从 24 ha 涨到 4850 ha），只在显式 --arc-variant on 时尝试。
        if use_arc_variant and cad_area > 0 and hatch.rings_alt:
            # 只在「与 CAD 自算面积吻合到 5% 以内」且「包围盒没有异常膨胀」时才改用备选
            alt_area, alt_infos = net_area(hatch.rings_alt)
            alt_outer = outer_ring(alt_infos) if alt_infos else None
            if alt_area > 0 and alt_outer is not None:
                current_err = abs(geom_area - cad_area) / max(cad_area, 1.0) if geom_area > 0 else 1e9
                alt_err = abs(alt_area - cad_area) / max(cad_area, 1.0)
                alt_box = bbox_of(alt_outer)
                alt_box_area = (alt_box[2] - alt_box[0]) * (alt_box[3] - alt_box[1])
                if alt_err <= 0.05 and alt_err < current_err and alt_box_area <= max(cad_area * 20.0, 1.0):
                    hatch.rings = hatch.rings_alt
                    hatch.rings_alt = None
                    geom_area, infos = alt_area, alt_infos
                    outer = outer_ring(infos)
                    rings = list(hatch.rings)
                    if arc_stats is not None:
                        arc_stats["rescued"] = arc_stats.get("rescued", 0) + 1

        if cad_area > 0:
            # CAD 自算面积（.Area / AREA 命令同源）优先；几何重建只用于裁剪与校验
            raw_area = cad_area
            if geom_area > 0 and abs(cad_area - geom_area) / max(cad_area, 1.0) > 0.05:
                issues.append(
                    Issue(
                        "area_mismatch",
                        f"CAD 面积 {cad_area / 1e4:.4f} ha 与边界几何重建 {geom_area / 1e4:.4f} ha 相差 "
                        f"{abs(cad_area - geom_area) / max(cad_area, 1.0) * 100:.1f}%，已取 CAD 值",
                        hatch.handle,
                        hatch.layer,
                    )
                )
        elif geom_area > 0:
            raw_area = geom_area
        elif hatch.area_drawing:
            raw_area = hatch.area_drawing
            outer = None
            rings = None
        else:
            issues.append(
                Issue(
                    "hatch_area_unavailable",
                    f"填充面积无法获取：{hatch.area_error or '未解析出边界'}",
                    hatch.handle,
                    hatch.layer,
                )
            )
            continue

        if raw_area <= 0:
            issues.append(Issue("zero_area", "填充净面积为 0（可能仅有孤岛或边界退化）", hatch.handle, hatch.layer))
            continue

        parcels.append(
            ParcelRecord(
                handle=hatch.handle,
                layer=hatch.layer,
                entity_type="HATCH",
                area_m2=raw_area * factor * factor,
                source_kind="hatch",
                outer=outer,
                rings=rings,
                closed_flag=True,
                geometry_closed=True,
            )
        )
    return parcels


def _filter_scope(
    parcels: Sequence[ParcelRecord],
    scope: str,
    window: Optional[Tuple[float, float, float, float]],
    polygon: Optional[Sequence[Tuple[float, float]]],
    issues: List[Issue],
) -> Tuple[List[ParcelRecord], int]:
    if scope in ("all", "none"):
        return list(parcels), 0
    kept: List[ParcelRecord] = []
    excluded = 0
    for parcel in parcels:
        if parcel.outer is None:
            kept.append(parcel)
            continue
        box = bbox_of(parcel.outer)
        if scope == "window" and window is not None:
            if not in_window(box, window):
                excluded += 1
                if crosses_window(box, window):
                    issues.append(Issue("out_of_scope", "部分越出统计窗口，未计入合计", parcel.handle, parcel.layer))
                continue
        elif scope == "polygon" and polygon is not None:
            if not point_in_polygon(bbox_center(box), polygon):
                excluded += 1
                continue
        kept.append(parcel)
    return kept, excluded


def _clip_to_region(
    parcels: Sequence[ParcelRecord],
    region: ScopeRegion,
    args,
    factor: float,
    issues: List[Issue],
) -> Tuple[List[ParcelRecord], dict]:
    """把地块裁到规划范围内。shapely 可用时精确求交，否则按 fallback 处理。"""
    stats = {"full": 0, "partial": 0, "none": 0, "fallback": 0, "invalid": 0, "background": 0, "bypass": 0}
    if args.clip == "whole":
        # 显式逃生口：整块计入、不做裁剪，全部计为「未裁剪」，报告必须标注
        stats["bypass"] = len(parcels)
        return list(parcels), stats

    # 背景填充护栏：底图/总平面类填充的包围盒远大于规划范围，求交后会被算成整个范围，
    # 必须拦掉，否则一级类面积整体虚高（实测可放大到规划范围的 1000 倍以上）。
    region_points = [pt for ring in region.polygons for pt in ring]
    region_box = bbox_of(region_points) if region_points else None
    region_box_area = (
        (region_box[2] - region_box[0]) * (region_box[3] - region_box[1]) if region_box else 0.0
    )
    background_guard = region_box_area * 10 if region_box_area > 0 else 0.0

    kept: List[ParcelRecord] = []
    for parcel in parcels:
        if parcel.outer is None:
            # 只有面积、没有边界：无法判断在不在范围内。计数留痕，由上层按
            # --clip-strict 处理（严格模式会先导 DXF 补齐几何，这里正常不应出现）。
            stats["bypass"] = stats.get("bypass", 0) + 1
            issues.append(
                Issue(
                    "clip_bypass",
                    "填充缺少边界几何，无法判断是否在规划范围内，已按整块计入（面积可能偏大）",
                    parcel.handle,
                    parcel.layer,
                )
            )
            kept.append(parcel)
            continue
        if background_guard:
            box = bbox_of(parcel.outer)
            parcel_box_area = (box[2] - box[0]) * (box[3] - box[1])
            huge_box = parcel_box_area > background_guard
            # 与规划范围近等大的单块填充：真实地块不会占满整个规划范围，多为底图/用地图片
            huge_area = region.area_drawing > 0 and parcel.area_m2 >= region.area_drawing * factor * factor * 0.9
            if huge_box or huge_area:
                stats["background"] += 1
                if huge_box:
                    reason = f"包围盒是规划范围的 {parcel_box_area / region_box_area:.0f} 倍"
                else:
                    reason = f"单块面积达规划范围的 {parcel.area_m2 / (region.area_drawing * factor * factor) * 100:.0f}%"
                issues.append(
                    Issue(
                        "background_fill",
                        f"{reason}，疑似底图/背景填充，未计入（若确为整片林地/水域，请单独说明）",
                        parcel.handle,
                        parcel.layer,
                    )
                )
                continue
        area_drawing, clipped_points, status = region.clip(parcel.outer)
        stats[status] = stats.get(status, 0) + 1

        if status == "none":
            # 硬性规则：规划范围外的一概不统计
            issues.append(Issue("out_of_scope", "完全落在规划范围外，未计入合计", parcel.handle, parcel.layer))
            continue
        if status == "invalid":
            issues.append(Issue("clip_invalid", "边界无效，无法与规划范围求交，按原面积计入", parcel.handle, parcel.layer))
            kept.append(parcel)
            continue
        if status == "partial":
            clipped_area = area_drawing * factor * factor
            # 跨界块的上界就是整块面积：DXF 弧段离散可能把边界放大，裁剪后反而超过整块，
            # 这时按整块计（等价于「整块都在范围内」），避免算出比地块本身还大的面积。
            if parcel.area_m2 > 0 and clipped_area > parcel.area_m2:
                clipped_area = parcel.area_m2
            parcel.area_m2 = clipped_area
            parcel.clipped = True
            parcel.clipped_points = clipped_points
            kept.append(parcel)
            continue
        if status == "fallback":
            if args.clip_fallback == "exclude":
                issues.append(
                    Issue("clip_approx", "未安装 shapely，跨界地块按排除处理（面积偏小）", parcel.handle, parcel.layer)
                )
                continue
            issues.append(
                Issue("clip_approx", "未安装 shapely，跨界地块按整块计入（面积偏大）", parcel.handle, parcel.layer)
            )
            kept.append(parcel)
            continue
        kept.append(parcel)
    return kept, stats


def _covered_by_hatch(parcel: ParcelRecord, hatch_parcels: Sequence[ParcelRecord], ratio: float = 0.5) -> bool:
    if parcel.outer is None:
        return False
    box = bbox_of(parcel.outer)
    anchor = interior_point(parcel.outer) or bbox_center(box)
    for hatch in hatch_parcels:
        if hatch.outer is None:
            continue
        if bbox_overlap_ratio(box, bbox_of(hatch.outer)) < ratio:
            continue
        if not point_in_polygon(anchor, hatch.outer):
            continue
        bigger = max(parcel.area_m2, hatch.area_m2)
        if bigger > 0 and abs(parcel.area_m2 - hatch.area_m2) / bigger <= 0.5:
            return True
    return False


def _merge_by_primitive(
    primitive: str,
    polyline_parcels: Sequence[ParcelRecord],
    hatch_parcels: Sequence[ParcelRecord],
    allow_polyline_fill: bool = False,
) -> Tuple[List[ParcelRecord], int, str]:
    """合并填充与多段线地块，返回 (地块, 被排除的多段线数, 口径说明)。

    默认只认填充：多段线不参与补缺，避免与道路反算重复计面积。
    auto 仅在整图一个填充都没有时整体回退多段线。
    """
    poly = list(polyline_parcels)
    hatch = list(hatch_parcels)

    if primitive == "polyline":
        return poly, 0, "按 --primitive polyline 只用多段线，未使用填充。"
    if primitive == "hatch":
        if not poly:
            return hatch, 0, "只统计填充面积，图中没有多段线地块。"
        return hatch, len(poly), f"只统计填充面积，{len(poly)} 个多段线地块未计入（道路等不填充类目由差额反算）。"
    if primitive == "both":
        if not hatch:
            return poly, 0, "未发现填充，退回按多段线统计（--primitive both）。"
        kept = [parcel for parcel in poly if not _covered_by_hatch(parcel, hatch)]
        return hatch + kept, len(poly) - len(kept), "填充与多段线去重后合并统计（--primitive both）。"

    # auto
    if not hatch:
        return poly, 0, f"整图未发现填充，整体回退为多段线统计（{len(poly)} 个），结果需复核。"
    if allow_polyline_fill:
        if not any(item.outer for item in hatch):
            return hatch, len(poly), "填充缺少边界几何，已按 --allow-polyline-fill 整体改用多段线。"
        kept = [parcel for parcel in poly if not _covered_by_hatch(parcel, hatch)]
        return hatch + kept, len(poly) - len(kept), (
            f"填充优先并用多段线补 {len(poly) - len(kept)} 处缺口（--allow-polyline-fill），"
            "可能与道路反算重复计面积，请复核。"
        )
    return hatch, len(poly), (
        f"只统计填充面积，{len(poly)} 个多段线地块未计入；如需用多段线补缺请显式加 --allow-polyline-fill。"
    )


# ---------------------------------------------------------------- 确认输出
# 全局只有一个的确认项：detail 结构在各阶段不一致，按类型去重即可
_SINGLE_CONFIRM_TYPES = {"standard_conversion", "unit_suspect", "balance_params", "layer_state"}


def _confirm_key(item: dict) -> tuple:
    """确认项去重键：同一类型 + 同一对象（代码/图层）只保留一条。"""
    kind = str(item.get("type", ""))
    if kind in _SINGLE_CONFIRM_TYPES:
        return (kind, "")
    detail = item.get("detail") or {}
    subject = (
        detail.get("code")
        or detail.get("layer")
        or detail.get("layers")
        or detail.get("segments")
        or detail.get("scope_layer")
        or ""
    )
    return (kind, json.dumps(subject, ensure_ascii=False, sort_keys=True, default=str))


def merge_confirmations(*groups: Optional[Sequence[dict]]) -> List[dict]:
    """合并多阶段确认项。

    预扫描（只看图层名）与扫描后（按范围/图斑）会提出同一个问题，合并去重后
    一次交给用户，避免同一件事问两遍；也让「计算前」与「计算后」各只停一轮。
    """
    merged: Dict[tuple, dict] = {}
    for group in groups:
        for item in group or ():
            merged.setdefault(_confirm_key(item), item)
    return list(merged.values())


def _emit_confirmations(items: Sequence[dict], args, source: str) -> int:
    items = list(items)
    payload = {
        "status": "needs_confirmation",
        "source": source,
        "answer_mode": "batch",
        "count": len(items),
        "items": items,
        "hint": (
            "本次已把能判定的待确认项一次性汇总（answer_mode=batch）。"
            "请在同一条消息里把每一项连同选项按 1..N 编号一次问完，不要逐项来回追问；"
            "用户答完后，把所有被选中选项里的 args 合并成一条命令重跑一次。"
            "带 reminder 的确认项必须先把提醒内容完整转述给用户，再索取图层名或范围线文件；"
            "未确认前不生成平衡表。"
        ),
    }
    path = Path(args.confirm_out)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"\n[待确认] 清单已写入：{path}", file=sys.stderr)
    return EXIT_NEED_CONFIRM


def unit_suspect_item(
    span_units: Optional[float],
    factor: float,
    entity_count: int,
    unit: str,
    unit_source: str,
) -> Optional[dict]:
    """单位口径与几何尺度明显矛盾时的确认项，宁可停下问，也不要整体差 10⁶ 倍出表。

    判定：按当前单位解释后整图跨度不足 100 m，却有 20 个以上填充/多段线图元——
    用地图不可能把所有地块挤在几十米见方里，基本可断定 $INSUNITS 与实际绘图单位不一致。
    典型事故：米制总图被标成 $INSUNITS=4（毫米），规划范围 1512.22 ha 被算成 0.0015 ha。
    """
    if not span_units or span_units <= 0 or entity_count < 20:
        return None
    span_m = float(span_units) * factor
    # 双向守卫：过小＝毫米图标成米；过大＝毫米图被当成米（跨度放大 1000 倍，面积放大 10⁶ 倍）
    if 100.0 <= span_m <= 50000.0:
        return None
    if span_m < 100.0:
        evidence = (
            f"按当前单位「{unit}」（{unit_source}）解释，整图跨度只有 {span_m:.2f} m，"
            f"却有 {entity_count} 个填充/多段线图元——用地图不可能把所有地块挤在几十米见方里。"
        )
        direction = "too_small"
    else:
        evidence = (
            f"按当前单位「{unit}」（{unit_source}）解释，整图跨度达 {span_m / 1000:.1f} km，"
            f"却有 {entity_count} 个填充/多段线图元——总图不可能铺这么大，多半是毫米图被当成了米。"
        )
        direction = "too_large"
    return {
        "type": "unit_suspect",
        "question": (
            evidence
            + "多半是 $INSUNITS 与实际绘图单位不一致，直接出表会让面积整体差 10⁶ 倍。"
            "请确认图纸实际单位："
        ),
        "options": [
            {
                "id": "meter",
                "label": "图纸按米绘制（推荐，--unit m）",
                "args": ["--unit", "m"],
                "description": "坐标直接按米解释，面积极少出现 10⁶ 倍偏差",
            },
            {
                "id": "centimeter",
                "label": "图纸按厘米绘制（--unit cm）",
                "args": ["--unit", "cm"],
                "description": "按厘米换算为米后再统计",
            },
            {
                "id": "keep",
                "label": f"确认当前单位「{unit}」正确，按现状继续",
                "args": ["--unit", unit],
                "description": "显式指定后不再触发本校验；若判断有误，面积与占比会整体失真",
            },
            {
                "id": "diagnose",
                "label": "先不出表，只输出排查清单",
                "args": ["--diagnose-only"],
                "description": "先看单位、图层、未识别与裁剪口径，再决定怎么改",
            },
        ],
        "detail": {
            "unit": unit,
            "unit_source": unit_source,
            "direction": direction,
            "span_units": round(float(span_units), 3),
            "span_m": round(span_m, 3),
            "entity_count": entity_count,
        },
    }


def unit_params_item(
    span_units: Optional[float],
    factor: float,
    unit: str,
    unit_source: str,
    insunits,
    entity_count: int,
) -> dict:
    """单位靠 $INSUNITS 自动判定时的确认项。

    尺度守卫只在「明显不合理」时才拦人；米制图被标成毫米但尺度看起来合理时守卫不响，
    面积却会整体差 10⁶ 倍，所以只要是自动判定就必须让用户点头。
    """
    span_value = float(span_units or 0.0)
    span_m = span_value * factor
    preview = []
    for name, name_factor in (("m", 1.0), ("mm", 0.001), ("cm", 0.01)):
        preview.append({"unit": name, "span_m": round(span_value * name_factor, 2)})
    return {
        "type": "balance_params",
        "question": (
            f"图纸单位是按「$INSUNITS={insunits}」自动判定的「{unit}」（{unit_source}），未显式指定。"
            f"按该单位解译整图跨度 {span_m:.2f} m。$INSUNITS 被改错时不会报错，面积只会整体差 10⁶ 倍，"
            "请确认单位口径："
        ),
        "options": [
            {
                "id": "confirm",
                "label": f"确认按「{unit}」统计（--unit {unit}）",
                "args": ["--unit", unit],
                "description": f"跨度按 {unit} 解释为 {span_m:.2f} m",
            },
            {
                "id": "meter",
                "label": "图纸按米绘制（--unit m）",
                "args": ["--unit", "m"],
                "description": "米制总图常用，坐标直接按米解释",
            },
            {
                "id": "millimeter",
                "label": "图纸按毫米绘制（--unit mm）",
                "args": ["--unit", "mm"],
                "description": "施工图常见，坐标需除以 1000",
            },
        ],
        "detail": {
            "insunits": insunits,
            "resolved_unit": unit,
            "unit_source": unit_source,
            "span_units": round(span_value, 3),
            "span_by_unit": preview,
            "entity_count": entity_count,
            "accept_flag": "--accept-default-params 可跳过本确认，报告会记录「单位自动判定、未人工复核」",
        },
    }


def clip_strategy_item(missing: int, layers: Sequence[str]) -> dict:
    """CAD 直连读不到填充边界时，让用户在「严格裁剪」与「快速直连」之间选。

    A：导出 DXF 后严格裁剪，正确性高、可能慢，适合精确计算；
    B：保持直连，范围外/跨界填充可能被整块计入，速度快但准确性差、还会多次回问。
    """
    layer_list = list(dict.fromkeys([str(name) for name in layers if str(name)]))
    layer_text = "、".join(layer_list[:5]) or "—"
    return {
        "type": "clip_strict_strategy",
        "question": (
            f"规划范围已确认，但有 {missing} 个填充在 CAD 里只能读到面积、读不到边界"
            f"（涉及图层：{layer_text}），无法判断它们在不在规划范围内。请选择裁剪方式："
            "A. 导出 DXF 后严格裁剪——正确性高，只统计规划范围内的内容，大图可能慢几分钟，适合精确计算；"
            "B. 保持直连 CAD——快，但范围外和跨界的填充可能被整块计入，准确性较差，"
            "过程中还会多次回问，适合粗略估算。"
        ),
        "options": [
            {
                "id": "strict",
                "label": "A. 严格裁剪（正确性优先，可能慢）",
                "args": ["--clip-strict", "on"],
                "description": "整图导出 DXF 副本（同图重跑走缓存），每条填充都有边界，范围外一律不计",
            },
            {
                "id": "fast",
                "label": "B. 快速直连（速度优先，准确性差）",
                "args": ["--clip-strict", "off"],
                "description": "不导 DXF，无法裁剪的填充按整块计入；报告会标「需复核」，不可当已校核结果",
            },
        ],
        "detail": {
            "missing_hatches": missing,
            "layers": layer_list[:20],
            "note": "默认口径要求只统计规划范围内的内容，B 方案会放宽该口径",
        },
    }


def clip_export_failed_item(missing: int, layers: Sequence[str]) -> dict:
    """严格裁剪所需的 DXF 导出失败时的确认项：不得静默降级。"""
    layer_list = list(dict.fromkeys([str(name) for name in layers if str(name)]))
    return {
        "type": "clip_export_failed",
        "question": (
            f"按 A 方案严格裁剪需要把整图导出为 DXF，但导出失败；"
            f"仍有 {missing} 个填充读不到边界，无法判断是否在规划范围内。请选择："
            "① 在 CAD 中手动另存 DXF 后用 --file 重跑（推荐，最稳）；"
            "② 改走 B 方案快速直连（面积可能偏大，仅供粗略参考）。"
        ),
        "options": [
            {
                "id": "manual_dxf",
                "label": "我去 CAD 里另存 DXF，之后再跑（推荐）",
                "args": [],
                "requires_input": "none",
                "description": "另存后加 --file 指定该 DXF，可保证严格裁剪",
            },
            {
                "id": "fast",
                "label": "改走 B 方案：快速直连，允许面积偏大",
                "args": ["--clip-strict", "off"],
                "description": "报告会标「需复核」",
            },
        ],
        "detail": {"missing_hatches": missing, "layers": layer_list[:20]},
    }


def _scope_step_decision(target: str, by_layer: Dict[str, List]) -> dict:
    """--list-scope 的下一步结论：1 条闭合直接继续，多条必须先问草稿。"""
    if not target:
        return {
            "next": "ask_scope_layer",
            "message": "命中多个候选图层或没有候选，先让用户指定哪个是规划范围线。",
            "layers": sorted(by_layer),
        }
    lines = closed_polyline_candidates(by_layer.get(target, []))
    if len(lines) == 1:
        return {
            "layer": target,
            "closed_polylines": 1,
            "next": "continue",
            "message": "该图层只有 1 条闭合多段线，直接进入测算，不要再追问草稿或空间关系。",
        }
    if len(lines) > 1:
        return {
            "layer": target,
            "closed_polylines": len(lines),
            "next": "ask_draft",
            "message": "该图层有多条闭合边界，必须先问用户是否有未删除的草稿，再决定并集 / 挖空 / 交集。",
            "item": scope_draft_check_item(target, by_layer.get(target, [])),
        }
    return {
        "layer": target,
        "closed_polylines": 0,
        "next": "ask_scope_layer",
        "message": "该图层没有闭合多段线，需请用户换图层、先闭合边界，或提供范围线文件。",
    }


def _emit_scope_layer_choice(backend, keywords: Sequence[str], args) -> int:
    """规划范围第一步：只读图层表 + 按图层数一下多段线数量，不读任何几何。

    只做两件轻量的事：① 读图层名并按关键词命中；② 对命中的图层各做一次
    「选择集计数」（AutoCAD 侧只返回 Count，不逐图元读属性）。
    拿到「哪些图层里真的有线」后就交给用户指定，绝不自动进第二步。
    """
    started = time.monotonic()
    try:
        layers = backend.list_layers()
    except Exception as exc:
        raise RuntimeError(f"读取图层表失败：{exc}") from exc

    matched = match_layers_by_keyword(layers, keywords)
    counts: dict = {}
    count_errors: List[str] = []
    if matched:
        try:
            counts = backend.count_entities_by_layer([row["layer"] for row in matched]) or {}
        except Exception as exc:
            print(f"[提示] 按图层统计多段线失败（{exc}），数量按未知处理。", file=sys.stderr, flush=True)
            counts = {}
        for row in matched:
            record = counts.get(row["layer"]) or {}
            value = record.get("polylines")
            row["polylines"] = int(value) if isinstance(value, int) else None
            if isinstance(record.get("entities"), int):
                row["entities"] = int(record["entities"])
            if record.get("error"):
                row["count_error"] = str(record["error"])[:300]
                count_errors.append(f"{row['layer']}: {row['count_error']}")

        # 有线的图层排前面，其次按关键词优先级（规划范围 > 红线 > 范围），再按线数量降序
        order = {str(kw).strip().lower(): index for index, kw in enumerate(keywords)}
        matched.sort(
            key=lambda row: (
                0 if (row.get("polylines") or 0) > 0 else 1,
                order.get(str(row.get("keyword", "")).lower(), 99),
                -(row.get("polylines") or 0),
                row["layer"].lower(),
            )
        )

    line_layers = [row["layer"] for row in matched if (row.get("polylines") or 0) > 0]

    if matched:
        item = scope_layer_choice_item(matched, keywords, layers)
    else:
        item = scope_missing_item([], keywords)
        item["question"] = (
            f"没有任何图层名命中规划范围关键词（已检索：{'、'.join(keywords)}）。"
            "请让用户从图层清单里指定规划范围线所在的图层名；若图纸确实没有规划范围，"
            "可选「无规划范围」按全部可见图层填充合计，或提供同坐标系原位的范围线文件。"
        )
        item["detail"]["all_layers"] = [str(name) for name in layers][:200]
        item["detail"]["layer_count"] = len(layers)

    payload = {
        "status": "needs_confirmation",
        "source": backend.describe(),
        "layer_count": len(layers),
        "matched_layers": matched,
        "line_layers": line_layers,
        "count_errors": count_errors,
        "polyline_count_note": (
            "polylines 为该图层内 LWPOLYLINE/POLYLINE 的数量（只计数，未读几何）；"
            "entities 为该图层对象总数；null 表示对应统计失败，原因见 count_error"
        ),
        "items": [item],
        "hint": (
            "第一步只读了图层表并按图层数了多段线数量，没有读任何图元几何。"
            "若 line_layers 非空，请把图层名与线数量原样告诉用户，让用户指定哪一个是规划范围线；"
            "用户确认后再执行「--scope-layer <图层名> --list-scope」精准读取该图层边界。"
        ),
        "elapsed_s": round(time.monotonic() - started, 2),
    }

    wanted = (args.scope_layer or "").strip()
    wanted_exists = False
    if wanted:
        wanted_exists = any(str(name).strip().lower() == wanted.lower() for name in layers)
        payload["scope_layer_check"] = {"layer": wanted, "exists": wanted_exists}
        if not wanted_exists:
            payload["hint"] = f"指定的图层「{wanted}」不在图层表中，请先让用户核对图层名。"

    if wanted and wanted_exists:
        payload["status"] = "scope_layer_fixed"
        payload["suggest_args"] = ["--scope-layer", wanted, "--list-scope"]
    elif not matched:
        payload["status"] = "scope_not_found"
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if wanted and wanted_exists:
        print(
            f"\n[范围图层] 用户已指定图层「{wanted}」，下一步执行："
            f'--scope-layer "{wanted}" --list-scope（只读该图层，读取边界与面积）。',
            file=sys.stderr,
            flush=True,
        )
        return 0

    path = Path(args.confirm_out)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    print(f"\n[待确认] 图层候选已写入：{path}", file=sys.stderr, flush=True)
    return EXIT_NEED_CONFIRM


# ---------------------------------------------------------------- 主流程
def _missing_code_item(code: str) -> dict:
    """映射配置引用了分类表里没有的代码：同样是「先问再做」。"""
    node = suggest_node(code)
    add_args = f"{code}:{node['name']}:{node['level']}:{node['parent'] or ''}"
    return {
        "type": "unregistered_code",
        "question": (
            f"图层映射引用了代码 {code}，但分类表中没有该节点。"
            "请确认是配置笔误，还是需要登记为新增用地代码？"
        ),
        "options": [
            {"id": "add", "label": f"新增 {code}", "args": ["--add-code", add_args],
             "description": "登记进分类配置与指引表"},
            {"id": "fix", "label": "配置笔误，改映射文件", "args": [],
             "description": "修正映射后重跑"},
        ],
        "detail": {"code": code, "source": "mapping", "suggested_node": node},
    }


def _unregistered_item(code: str, row: dict, mapper) -> dict:
    """单个未登记代码的确认项：先问名称是否写错，再问是否新增。"""
    layers = sorted(row["layers"], key=lambda name: -row["layers"][name])
    node = suggest_node(code, layers[0])
    add_args = f"{code}:{node['name']}:{node['level']}:{node['parent'] or ''}"
    area = row["area_m2"] / 1e4
    return {
        "type": "unregistered_code",
        "question": (
            f"图层代码 {code} 不在《国土空间调查、规划、用途管制用地用海分类指南》（2023）中"
            f"（图层：{'、'.join(layers[:5])}，{row['count']} 个图斑、{area:.2f} ha）。"
            f"请先确认名称是否写错；名称无误时，是否将 {code} 登记为新增用地代码？"
        ),
        "options": [
            {"id": "rename", "label": "名称写错，先改图层名", "args": [],
             "description": "在 CAD 改正图层名后重跑，不写入配置"},
            {"id": "add", "label": f"新增 {code} {node['name']}", "args": ["--add-code", add_args],
             "description": f"登记为 {node['level']} 级类目（父级 {node['parent'] or '—'}），写入配置与指引表"},
            {"id": "map", "label": "归入已有代码", "args": ["--unregistered-code", f"{code}=map:目标代码"],
             "description": "不新增类目，显式指定归属并在说明中记录"},
            {"id": "skip", "label": "本次不处理", "args": ["--unregistered-code", f"{code}=skip"],
             "description": "沿用关键词兜底，报告标注需复核，不可直接交付"},
        ],
        "detail": {
            "code": code,
            "layers": layers[:20],
            "entities": row["count"],
            "area_ha": round(area, 4),
            "current_fallback": mapper.match(layers[0]) or "",
            "current_fallback_registered": mapper.tree is not None
            and mapper.tree.get(mapper.match(layers[0])) is not None,
            "warning": "关键词兜底会跨大类错分，未经确认不得出表",
            "suggested_node": node,
        },
    }


def unregistered_code_items(
    parcels: Sequence,
    mapper,
    tree,
    skip_codes: Optional[set] = None,
) -> List[dict]:
    """未登记代码 → 待确认项（先问再算，未确认不出表）。"""
    skip_codes = skip_codes or set()
    groups: Dict[str, dict] = {}
    for parcel in parcels:
        for code in mapper.unregistered_codes(parcel.layer):
            if code in skip_codes:
                continue
            row = groups.setdefault(code, {"layers": {}, "count": 0, "area_m2": 0.0})
            row["count"] += 1
            row["area_m2"] += parcel.area_m2
            row["layers"][parcel.layer] = row["layers"].get(parcel.layer, 0) + 1

    items: List[dict] = []
    for code, row in sorted(groups.items()):
        items.append(_unregistered_item(code, row, mapper))
    seen = {item["detail"].get("code") for item in items}
    for code in missing_rule_codes(mapper, tree):
        if code in seen:
            continue
        items.append(_missing_code_item(code))
    return items


def _prescan_region(backend, scope_layer: str, args):
    """只读范围图层，先搭一个粗裁剪区域，供扫描前判断图层在不在范围内。"""
    try:
        records = list(backend.iter_polylines(None, layer_patterns=[scope_layer]))
    except Exception:
        return None
    candidates = [
        ScopeCandidate(record.handle, record.layer, record.entity_type, record.vertices, record.cad_area or 0.0)
        for record in records
        if record.vertices and (record.closed_flag or record.vertices[0] == record.vertices[-1])
    ]
    if not candidates:
        return None
    relation = args.scope_relation if args.scope_relation in ("union", "difference", "intersect") else "union"
    try:
        return build_region(candidates, relation)
    except Exception:
        return None


def prescan_confirmations(
    backend,
    args,
    mapper,
    tree,
    skip_codes: Optional[set] = None,
) -> Tuple[List[dict], List[str], List[str]]:
    """扫描前就能判定的确认项：只读图层表，最多再读几个候选图层。

    原先「未登记代码 / 旧标准」要把整图图元扫完（大图十几分钟）才问，用户答完还得
    再扫一遍。这里把问题前移；对未登记代码图层顺带判定它在不在规划范围内——范围外
    的对合计没有影响，直接自动排除，只问真正会进表的那些。

    返回 (确认项, 说明, 自动排除的图层名)。
    """
    items: List[dict] = []
    notes: List[str] = []
    auto_excluded: List[str] = []
    skip_codes = skip_codes or set()

    try:
        layer_names = backend.list_layers()
    except Exception:
        return items, notes, auto_excluded
    if not layer_names:
        return items, notes, auto_excluded

    try:
        states = backend.get_layer_states()
    except Exception:
        states = {}

    layer_filter = {name.strip().lower() for name in _split_list(args.layers)} if args.layers else None
    excluded = {name.strip().lower() for name in _split_list(args.exclude_layers)} if args.exclude_layers else set()
    visible: List[str] = []
    for name in layer_names:
        state = states.get(name) or {}
        if args.layer_state != MODE_ALL and (state.get("on") is False or state.get("frozen") is True):
            continue
        if layer_filter is not None and name.lower() not in layer_filter:
            continue
        if name.lower() in excluded:
            continue
        visible.append(name)
    if not visible:
        return items, notes, auto_excluded

    # ---- 旧标准（只看图层名，结论与扫描后一致）
    if args.standard == "2023" and args.convert_to == "none":
        guess = guess_standard(visible, mapper)
        if guess["standard"] == "gb50137" and guess["confidence"] == "high":
            items.append(
                {
                    "type": "standard_conversion",
                    "question": (
                        "图纸图层疑似使用 GB 50137（旧标准）代码："
                        + "、".join(guess["old"][:6])
                        + "。是否按旧标准统计并换算为《国土空间调查、规划用地用海分类指南》（2023）？"
                    ),
                    "options": [
                        {
                            "id": "convert",
                            "label": "按 GB 50137 统计并换算为 2023",
                            "args": ["--standard", "gb50137", "--convert-to", "2023"],
                            "description": "按旧标准归类后换算，换不出来的一律列入待确认",
                        },
                        {
                            "id": "keep2023",
                            "label": "仍按 2023 标准统计",
                            "args": ["--standard", "2023"],
                            "description": "图层名按新标准映射，旧代码可能命中不了",
                        },
                    ],
                    "detail": {"layers": list(guess["old"])[:20]},
                }
            )

    # ---- 未登记代码（能判定范围就先判定，范围外直接排除）
    candidates: Dict[str, List[str]] = {}
    for name in visible:
        codes = [code for code in mapper.unregistered_codes(name) if code not in skip_codes]
        if codes:
            candidates[name] = codes
    if not candidates:
        return items, notes, auto_excluded

    scope_layer = (args.scope_layer or "").strip()
    can_clip = bool(scope_layer) and args.scope == "all" and args.clip != "whole"
    measured: Dict[str, dict] = {}
    seen_layers: set = set()
    probed = False
    probe_backend = None
    if can_clip:
        # 直连 CAD 时填充只有面积、没有边界，判不了范围；借用已导出的 DXF 副本（同一张图
        # 重跑时通常已经有），没有副本就退回「扫描后确认」，不为了问一句先去导一遍整图。
        if backend.channel == "com":
            cached = None
            try:
                cached = backend.cached_dxf()  # type: ignore[attr-defined]
            except Exception:
                cached = None
            if cached and Path(str(cached)).exists():
                from backends.dxf_backend import DxfBackend

                try:
                    probe_backend = DxfBackend(str(cached), expand_blocks=False)
                except Exception:
                    probe_backend = None
        else:
            probe_backend = backend

    try:
        if probe_backend is not None:
            region = _prescan_region(probe_backend, scope_layer, args)
            if region is not None:
                probed = True
                try:
                    for hatch in probe_backend.iter_hatches(None, layer_patterns=list(candidates)):
                        layer = str(hatch.layer).strip()
                        if layer not in candidates or not hatch.rings:
                            continue
                        seen_layers.add(layer)
                        area, infos = net_area(hatch.rings)
                        outer = outer_ring(infos)
                        if area <= 0 or outer is None:
                            continue
                        got, _points, status = region.clip(outer)
                        if status == "none":
                            continue
                        row = measured.setdefault(layer, {"count": 0, "area_m2": 0.0})
                        row["count"] += 1
                        row["area_m2"] += area if status == "full" else got
                except Exception:
                    probed = False
                    measured = {}
    finally:
        if probe_backend is not None and probe_backend is not backend:
            try:
                probe_backend.close()
            except Exception:
                pass

    if probed:
        # 读到过边界、但一块都没落在范围内的图层：排除掉，不再追问
        outside = sorted(layer for layer in seen_layers if layer not in measured)
        if outside:
            auto_excluded.extend(outside)
            notes.append(
                "以下未登记代码图层在规划范围内面积为 0，已自动排除（不影响合计，也不再追问）："
                + "、".join(outside)
            )
        groups: Dict[str, dict] = {}
        for layer, row in measured.items():
            for code in candidates.get(layer, []):
                group = groups.setdefault(code, {"layers": {}, "count": 0, "area_m2": 0.0})
                group["count"] += row["count"]
                group["area_m2"] += row["area_m2"]
                group["layers"][layer] = row["count"]
        for code, row in sorted(groups.items()):
            item = _unregistered_item(code, row, mapper)
            item["options"].append(
                {
                    "id": "exclude",
                    "label": "该图层不在统计范围内，排除",
                    "args": ["--exclude-layers", ",".join(sorted(row["layers"]))],
                    "description": "不参与统计，也不再追问该代码",
                }
            )
            items.append(item)
        return items, notes, auto_excluded

    # 范围还判定不了（没指定范围图层 / 无 shapely）：不在这里打扰，沿用扫描后的确认流程
    notes.append(
        "检测到未登记代码图层："
        + "、".join(sorted(candidates)[:5])
        + "；需扫描后判定是否在规划范围内再确认。"
    )
    return items, notes, auto_excluded


def hybrid_unresolved_items(rows: Sequence[dict]) -> List[dict]:
    """混合用地里有段既不是分类表代码、也不是表内中文名：先问用户，不猜。"""
    merged: Dict[str, List[str]] = {}
    for row in rows:
        bucket = merged.setdefault(row.get("layer", ""), [])
        for segment in row.get("segments", []):
            if segment and segment not in bucket:
                bucket.append(segment)

    items: List[dict] = []
    for layer, segments in sorted(merged.items()):
        text = "、".join(segments[:5])
        items.append(
            {
                "type": "hybrid_unresolved",
                "question": (
                    f"混合用地图层「{layer}」里的「{text}」既不是分类表里的用地代码，也不是表内的用地名称。"
                    "请确认：是名称写错了，还是要把它登记为新增用地代码？"
                ),
                "options": [
                    {
                        "id": "fix_name",
                        "label": "名称写错，我在 CAD 里改图层名后重跑",
                        "args": [],
                        "description": "不写入配置，改完重跑即可",
                    },
                    {
                        "id": "add_code",
                        "label": "新增代码（分类表里确实没有这类）",
                        "args": [],
                        "requires_input": "add_code",
                        "description": "格式 9001:某某用地:2:父级（层级/父级留空由脚本推断）；登记后该段按中文名自动命中",
                    },
                    {
                        "id": "first_only",
                        "label": "忽略该段，只按首段代码统计",
                        "args": ["--ignore-hybrid-unresolved"],
                        "description": "其余段不计面积，报告会标注需复核、不可直接交付",
                    },
                ],
                "detail": {"layer": layer, "segments": segments[:20]},
            }
        )
    return items


def _run(args: argparse.Namespace) -> int:
    if args.check:
        info = probe_environment(args.progid)
        info["shapely"] = HAS_SHAPELY
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    tree = load_standard(args.standard, args.classification)
    # 把分类树交给映射器：图层名里出现真实存在的完整代码时优先按该代码归类
    mapper = load_mapping(args.mapping, args.standard, tree=tree)

    # ---- 未登记代码：用户确认后的回填（--add-code / --unregistered-code）
    explicit_map: Dict[str, str] = {}
    skip_codes: set = set()
    for spec in args.unregistered_code or []:
        token, _, action = str(spec).partition("=")
        token, action = token.strip(), action.strip()
        if not token or not action:
            raise ValueError(f"--unregistered-code 格式应为 CODE=skip 或 CODE=map:目标代码，收到：{spec}")
        if action.startswith("map:"):
            target = action.split(":", 1)[1].strip()
            if tree.get(target) is None:
                raise ValueError(
                    f"--unregistered-code {token}=map:{target}：目标代码 {target} 不在分类表中，请先确认归属"
                )
            explicit_map[token] = target
        elif action == "skip":
            skip_codes.add(token)
        else:
            raise ValueError(f"--unregistered-code 只支持 skip 或 map:目标代码，收到：{action}")

    added_codes: List[dict] = []
    for spec in args.add_code or []:
        parts = [item.strip() for item in str(spec).split(":")]
        code = parts[0].replace("-", "")
        if len(parts) < 2 or not code or not parts[1]:
            raise ValueError(f"--add-code 格式应为 CODE:名称[:层级:父级]，收到：{spec}")
        name = parts[1]
        level = (
            int(parts[2])
            if len(parts) > 2 and parts[2]
            else (1 if len(code) <= 2 else (2 if len(code) <= 4 else 3))
        )
        parent = (
            parts[3]
            if len(parts) > 3 and parts[3]
            else (None if level == 1 else code[: 2 if level == 2 else 4])
        )
        from add_classification_node import register  # 与本脚本同目录的维护工具，复用其写入逻辑

        added_codes.append(
            register(
                code=code,
                name=name,
                level=level,
                parent=parent,
                standard=args.standard,
                config_path=Path(args.classification) if args.classification else None,
                source="经用户确认登记的新增类目（已同步分类配置与指引表）",
                update_reference=not args.classification,  # 用自定义分类树时不擅改内置指引表
            )
        )
        print(
            f"[新增代码] {code} {name}（{level} 级，父级 {parent or '—'}）已登记，"
            f"配置：{added_codes[-1]['config_action']}，指引表：{added_codes[-1]['reference_action']}",
            file=sys.stderr,
        )

    if added_codes:
        # 分类配置已变化，重新加载分类树与映射
        tree = load_standard(args.standard, args.classification)
        mapper = load_mapping(args.mapping, args.standard, tree=tree)
    for item in added_codes:
        mapper.rules.insert(0, MatchRule(code=item["code"], exact=[], regex=[f"^{item['code']}"], keywords=[]))
    if explicit_map:
        mapper.code_overrides.update(explicit_map)

    convert_tree = None
    conversion = None
    if args.convert_to != "none":
        if args.convert_to == args.standard:
            print(f"[跳过] 当前标准已是 {args.standard}，无需换算")
        else:
            convert_tree = load_standard(args.convert_to)
            conversion = load_conversion(args.standard, args.convert_to, args.conversion)
            print(f"[换算] {tree.title or tree.standard} → {convert_tree.title or convert_tree.standard}")

    backend = None
    tmp_dir: Optional[Path] = None
    scope_keywords = _split_list(args.scope_layers) or list(SCOPE_KEYWORDS)
    quick_notes: List[str] = []
    scan_patterns: Optional[List[str]] = None
    if args.list_scope:
        # --list-scope 只要范围候选：候选本身按图层关键词筛选，因此可以先在选择集阶段
        # 就只取范围相关图层，避免「整图 DXF 导出 + 4 万图元逐属性读取」。
        if args.scope_layer:
            scan_patterns = [name.strip() for name in _split_list(args.scope_layer) if name.strip()]
        else:
            scan_patterns = [f"*{kw}*" for kw in scope_keywords if kw]
        scan_patterns = sorted(dict.fromkeys(scan_patterns)) or None

        explicit_blocks = any(
            token == "--blocks" or token.startswith("--blocks=") for token in sys.argv[1:]
        )
        if not explicit_blocks and args.blocks == "expand":
            args.blocks = "skip"
            quick_notes.append(
                "--list-scope 只看范围候选，已自动跳过块展开（避免整图导出 DXF）；"
                "确实要块内图元请显式加 --blocks expand，或改用 --file 走 DXF 通道。"
            )
        if scan_patterns:
            quick_notes.append(
                f"--list-scope 只在范围相关图层上扫描：{', '.join(scan_patterns)}；"
                "范围候选按同一批关键词筛选，结果与全图扫描一致。"
            )

    try:
        backend = create_backend(
            args.source,
            args.file,
            args.progid,
            start_cad=args.start_cad,
            expand_blocks=(args.blocks == "expand"),
            scan_timeout=args.scan_timeout,
        )
        # 默认输出跟着图纸走：否则报告与确认清单会落在用户当前工作目录，
        # 把工程目录弄脏（历史上 land_area_confirm.json 就是这样散落到项目根的）。
        if str(args.out) == "land_area_report.md":
            args.out = str(_default_report_path(backend))
        if str(args.confirm_out) == "land_area_confirm.json":
            args.confirm_out = str(Path(args.out).with_name("land_area_confirm.json"))
        if args.list_layers:
            for name in backend.list_layers():
                print(name)
            return 0

        if args.list_scope_layers:
            # 规划范围第一步：只读图层表，不碰图元、不导 DXF，图层再多也是秒出
            return _emit_scope_layer_choice(backend, scope_keywords, args)

        if backend.channel == "com":
            print("[通道] 已连接 CAD，正在扫描填充、多段线与块参照……", file=sys.stderr)
        else:
            print(f"[通道] 解析 {args.file}……", file=sys.stderr)

        original_source = backend.describe()
        try:
            original_signature: Optional[str] = backend.source_signature()
        except Exception:
            original_signature = None
        notes: List[str] = list(quick_notes)

        # ------------------------------------------------ 扫描前确认（先问再算）
        # 未登记代码 / 旧标准只靠图层表就能判定，没必要先扫几万图元再问；
        # 用户答完带参数重跑时还能命中扫描缓存，省掉整轮重扫。
        # --list-scope / --list-layer-state 只做诊断，不能被确认项打断，也不该为此多读图元
        list_only = args.list_scope or args.list_layer_state
        if list_only:
            prescan_items, prescan_notes, prescan_excluded = [], [], []
        else:
            prescan_items, prescan_notes, prescan_excluded = prescan_confirmations(
                backend, args, mapper, tree, skip_codes
            )
        notes.extend(prescan_notes)
        # 默认 batch：不在这里中断，与扫描后的范围/单位/图层可见性确认合并成一次输出。
        # --confirm-mode early 保留旧行为（大图先问再扫，省一次扫描，但问题会分多轮）。
        if prescan_items and args.confirm_mode == "early":
            return _emit_confirmations(prescan_items, args, original_source)
        pending_items: List[dict] = list(prescan_items)

        backend, tmp_dir, switch_notes = _maybe_switch_to_dxf(backend, args)
        notes.extend(switch_notes)

        scan_started = time.monotonic()
        polylines, hatches = scan_or_load(backend, scan_patterns, args, notes)
        print(
            f"[扫描] 完成：多段线 {len(polylines)} 个，填充 {len(hatches)} 个，"
            f"用时 {time.monotonic() - scan_started:.1f}s",
            file=sys.stderr,
            flush=True,
        )
        if getattr(args, "cad_areas", None):
            _cad_map = _load_cad_areas(args.cad_areas)
            if _cad_map:
                _merge_cad_areas(hatches, _cad_map, notes)
        if not polylines and not hatches:
            if scan_patterns:
                # 过滤后为空是有效结果（说明没有范围关键词图层），继续输出空候选
                notes.append("范围关键词图层上没有找到多段线或填充，候选为空。")
            else:
                raise RuntimeError("未找到填充或多段线图元，请确认图纸内容或图层是否被过滤")

        block_stats = getattr(backend, "block_stats", None)
        if block_stats is not None and block_stats.inserts:
            notes.append(
                f"块参照 {block_stats.inserts} 个，展开后新增图元 {block_stats.expanded_entities} 个（最多 {block_stats.max_depth} 层）。"
            )

        span = _infer_span(polylines, hatches)
        drawing_insunits = backend.get_insunits()
        unit, factor, unit_source = resolve_unit(
            drawing_insunits, span, None if args.unit == "auto" else args.unit
        )
        tolerance = args.closure_tolerance if args.closure_tolerance is not None else max((span or 1.0) * 1e-5, 1e-6)

        # ------------------------------------------------ 图层可见性
        model_states = backend.get_layer_states()
        layout_frozen = backend.get_layout_frozen_layers(args.layout)
        layout_label = ""
        try:
            layout_label = backend.layout_name(args.layout)  # type: ignore[attr-defined]
        except Exception:
            layout_label = args.layout or ""
        layer_report = build_report(model_states, layout_frozen, args.layer_state, layout_label)

        if args.list_layer_state:
            print(json.dumps(layer_report.to_dict(), ensure_ascii=False, indent=2))
            return 0

        confirmations: List[dict] = []
        if args.layer_state == MODE_AUTO and layer_report.has_difference():
            confirmations.append(confirmation_item(layer_report, layout_label))

        # ------------------------------------------------ 单位合理性守卫
        # $INSUNITS 常被画图人改错（米制图标成毫米），按错单位出表会整体差 10⁶ 倍却没有任何报错
        if args.unit == "auto" and not list_only:
            suspect = unit_suspect_item(
                span, factor, len(hatches) + len(polylines), unit, unit_source
            )
            if suspect:
                confirmations.append(suspect)
            elif (
                not args.accept_default_params
                # $INSUNITS=6 是明确的米制，尺度守卫又没响，再问一次纯属打扰；
                # 只有「没写单位 / 写了非米制 / 单位码不支持」才必须让人点头。
                and int(drawing_insunits or 0) != 6
            ):
                confirmations.append(
                    unit_params_item(
                        span,
                        factor,
                        unit,
                        unit_source,
                        drawing_insunits,
                        len(hatches) + len(polylines),
                    )
                )
            else:
                notes.append(
                    f"单位依据：按 $INSUNITS={drawing_insunits} 自动判定为 {unit}"
                    "（--accept-default-params，未人工复核；如实际绘图单位不同，面积会整体偏差）。"
                )

        # ------------------------------------------------ 新旧标准判定
        if args.standard == "2023" and args.convert_to == "none":
            layer_names = sorted({p.layer for p in polylines} | {h.layer for h in hatches})
            guess = guess_standard(layer_names, mapper)
            if guess["standard"] == "gb50137" and guess["confidence"] == "high":
                confirmations.append(
                    {
                        "type": "standard_conversion",
                        "question": (
                            "图纸图层疑似使用 GB 50137（旧标准）代码："
                            + "、".join(guess["old"][:6])
                            + "。是否按旧标准统计并换算为《国土空间调查、规划、用途管制用地用海分类指南》（2023）？"
                        ),
                        "options": [
                            {
                                "id": "convert",
                                "label": "按 GB 50137 统计并换算为 2023",
                                "args": ["--standard", "gb50137", "--convert-to", "2023"],
                                "answer": "--standard gb50137 --convert-to 2023",
                            },
                            {
                                "id": "keep2023",
                                "label": "仍按 2023 标准统计（图层名按新标准映射）",
                                "args": ["--standard", "2023"],
                                "answer": "--standard 2023",
                            },
                        ],
                        "detail": {
                            "old_code_layers": guess["old"],
                            "new_code_layers": guess["new"],
                        },
                    }
                )

        # ------------------------------------------------ 规划范围
        keywords = scope_keywords
        scope_region: Optional[ScopeRegion] = None
        scope_layer = ""
        scope_group: List = []
        scope_keyword_layers: List[str] = []
        relation = "single"
        scope_confirmed = False
        scope_none = args.scope == "none"
        scope_source_kind = "drawing"
        scope_file_info: Optional[dict] = None
        scope_alignment: Optional[dict] = None

        if scope_none and args.scope_file:
            raise ValueError("--scope none 与 --scope-file 互斥：要么用范围线文件裁剪，要么按全部填充合计。")

        def _match_layer_name(chosen: str, groups) -> str:
            """图层名按忽略大小写匹配，CAD 里图层名不区分大小写。"""
            if chosen in groups:
                return chosen
            for name in groups:
                if name.strip().lower() == chosen.strip().lower():
                    return name
            return ""

        manual_scope = args.scope in ("window", "polygon", "handles")
        if scope_none:
            notes.append(
                "按 --scope none（无规划范围）执行：统计全部可见图层的填充对象，总面积取填充合计；"
                "未做规划范围裁剪、范围相交判定与差额校验。"
            )
        elif args.scope_file:
            # ---- 外部范围线文件
            source = load_scope_file(args.scope_file, args.progid, args.start_cad)
            scope_file_info = source.to_dict()
            scope_source_kind = "file"
            if source.note:
                notes.append(source.note)
            scope_file_args = ["--scope-file", str(source.path)]
            file_inventory_cache: Optional[List[dict]] = None

            def file_inventory() -> List[dict]:
                nonlocal file_inventory_cache
                if file_inventory_cache is None:
                    file_inventory_cache = layer_inventory(source.polylines, [], tolerance, keywords)
                return file_inventory_cache

            file_candidates = collect_candidates(
                source.polylines, [], tolerance, keywords, layer_filter=_split_list(args.scope_layer)
            )
            if not file_candidates and not args.scope_layer:
                # 专用范围线文件的图层名常常不含关键词，退回文件内全部多段线
                all_layers = source.layers()
                file_candidates = collect_candidates(
                    source.polylines, [], tolerance, keywords, layer_filter=all_layers
                )
                if file_candidates:
                    notes.append(
                        "范围线文件未命中范围关键词，已按文件内全部多段线作为候选（图层："
                        + "、".join(all_layers[:10])
                        + "）。"
                    )

            if not file_candidates:
                confirmations.append(
                    scope_missing_item(
                        file_inventory(),
                        keywords,
                        scope_file_args,
                        reason=f"范围线文件「{source.path.name}」里没有可用的多段线",
                    )
                )
            else:
                groups = group_by_layer(file_candidates)
                chosen = ""
                if args.scope_layer:
                    chosen = _match_layer_name(args.scope_layer, groups)
                    if not chosen:
                        confirmations.append(
                            scope_layer_invalid_item(
                                args.scope_layer,
                                file_inventory(),
                                "在范围线文件中不存在或没有多段线",
                                scope_file_args,
                            )
                        )
                elif len(groups) > 1:
                    confirmations.extend(
                        confirmation_items(
                            file_candidates,
                            groups,
                            "single",
                            extra_args=scope_file_args,
                            inventory=file_inventory(),
                        )[:1]
                    )
                else:
                    chosen = sorted(groups)[0]

                if chosen and not confirmations:
                    scope_group = groups.get(chosen, [])
                    usable = [item for item in scope_group if item.points and item.closed]
                    if not usable:
                        confirmations.append(
                            scope_layer_invalid_item(
                                chosen,
                                file_inventory(),
                                "内没有闭合多段线（未闭合前不能作为范围边界）",
                                scope_file_args,
                            )
                        )
                    else:
                        relation, relation_needs, relation_notes, relation_kind = analyze_relation(scope_group)
                        notes.extend(relation_notes)
                        scope_lines = closed_polyline_candidates(usable)
                        if args.scope_outer_only:
                            scope_group = [max(usable, key=lambda item: item.area_drawing)]
                            usable = list(scope_group)
                            relation = "single"
                        elif args.scope_relation != MODE_AUTO:
                            # 显式给关系即视为「已确认非草稿」
                            relation = args.scope_relation
                        elif len(scope_lines) > 1:
                            # 多条闭合范围线：先问草稿，再谈空间关系
                            confirmations.append(
                                scope_draft_check_item(
                                    chosen, scope_group, relation, scope_file_args, relation_kind
                                )
                            )
                        elif relation_needs:
                            confirmations.extend(
                                confirmation_items(
                                    scope_group,
                                    {chosen: scope_group},
                                    relation,
                                    extra_args=scope_file_args,
                                    kind=relation_kind,
                                )
                            )

                        if not confirmations:
                            drawing_points: List = []
                            for record in polylines:
                                drawing_points.extend(record.vertices)
                            for hatch in hatches:
                                for ring in hatch.rings or []:
                                    drawing_points.extend(ring)
                            alignment = check_alignment(
                                [pt for item in usable for pt in item.points], drawing_points
                            )
                            scope_alignment = alignment
                            if alignment["level"] == "disjoint":
                                confirmations.append(scope_file_mismatch_item(str(source.path), alignment))
                            elif alignment["level"] == "low_overlap":
                                notes.append(
                                    "范围线与总图包围盒重叠仅 "
                                    f"{alignment['bbox_overlap_ratio'] * 100:.2f}%，"
                                    "请复核是否同坐标系原位（脚本不做坐标纠偏）。"
                                )
                            if source.insunits and drawing_insunits and source.insunits != drawing_insunits:
                                file_unit = resolve_unit(source.insunits)[0]
                                drawing_unit = resolve_unit(drawing_insunits)[0]
                                if args.scope_file_units == "assume-drawing":
                                    notes.append(
                                        f"范围线文件单位为 {file_unit}，总图为 {drawing_unit}；"
                                        "已按 --scope-file-units assume-drawing 直接按总图单位解释。"
                                    )
                                else:
                                    confirmations.append(
                                        scope_file_units_item(file_unit, drawing_unit, str(source.path))
                                    )
                            if not confirmations:
                                scope_region = build_region(scope_group, relation)
                                scope_layer = f"{source.path.name}:{chosen}"
                                scope_confirmed = True
        elif not manual_scope and args.scope_source != "manual":
            # ---- 本图按图层关键词识别
            scope_candidates = collect_candidates(
                polylines, hatches, tolerance, keywords, layer_filter=_split_list(args.scope_layer)
            )
            if args.list_scope:
                by_layer = group_by_layer(scope_candidates)
                target = ""
                if args.scope_layer:
                    for name in by_layer:
                        if name.strip().lower() == str(args.scope_layer).strip().lower():
                            target = name
                            break
                elif len(by_layer) == 1:
                    target = sorted(by_layer)[0]
                decision = _scope_step_decision(target, by_layer)
                print(
                    json.dumps(
                        {
                            "candidates": [item.to_dict() for item in scope_candidates],
                            "layers": sorted(by_layer.keys()),
                            "decision": decision,
                            "scan": {
                                "layer_patterns": scan_patterns or [],
                                "polylines": len(polylines),
                                "hatches": len(hatches),
                                "blocks_expanded": args.blocks == "expand",
                                "source": original_source,
                            },
                            "notes": notes,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0

            inventory_cache: Optional[List[dict]] = None

            def inventory_now() -> List[dict]:
                """图层清单只在需要提问时构建，避免无谓遍历整图多段线。"""
                nonlocal inventory_cache
                if inventory_cache is None:
                    inventory_cache = layer_inventory(polylines, hatches, tolerance, keywords)
                return inventory_cache

            groups = group_by_layer(scope_candidates)
            scope_keyword_layers = list(groups.keys())
            if not groups:
                if args.scope_layer:
                    confirmations.append(
                        scope_layer_invalid_item(
                            args.scope_layer, inventory_now(), "不存在，或其中没有多段线/填充对象", None
                        )
                    )
                else:
                    confirmations.append(scope_missing_item(inventory_now(), keywords))
            elif len(groups) > 1 and not args.scope_layer:
                confirmations.extend(
                    confirmation_items(scope_candidates, groups, "single", inventory=inventory_now())[:1]
                )
            else:
                chosen = args.scope_layer or sorted(groups)[0]
                chosen = _match_layer_name(chosen, groups) or sorted(groups)[0]
                scope_group = groups.get(chosen, [])
                usable = [item for item in scope_group if item.points and item.closed]
                if not usable:
                    confirmations.append(
                        scope_layer_invalid_item(
                            chosen, inventory_now(), "内没有闭合多段线（未闭合前不能作为范围边界）", None
                        )
                    )
                else:
                    relation, relation_needs, relation_notes, relation_kind = analyze_relation(scope_group)
                    notes.extend(relation_notes)
                    scope_lines = closed_polyline_candidates(usable)
                    if args.scope_outer_only:
                        scope_group = [max(usable, key=lambda item: item.area_drawing)]
                        relation = "single"
                    elif args.scope_relation != MODE_AUTO:
                        # 显式给关系即视为「已确认非草稿」
                        relation = args.scope_relation
                    elif len(scope_lines) > 1:
                        # 多条闭合范围线：先问草稿，再谈空间关系
                        confirmations.append(
                            scope_draft_check_item(chosen, scope_group, relation, None, relation_kind)
                        )
                    elif relation_needs:
                        confirmations.extend(
                            confirmation_items(
                                scope_group, {chosen: scope_group}, relation, kind=relation_kind
                            )
                        )

                    if not confirmations:
                        scope_region = build_region(scope_group, relation)
                        scope_layer = chosen
                        scope_confirmed = True
                        if not args.scope_layer:
                            notes.append(
                                f"规划范围按关键词自动识别为图层「{chosen}」；"
                                "如与实际不符，请用 --scope-layer 指定真实图层或 --scope-file 提供范围线文件后重跑。"
                            )

        # ------------------------------------------------ 裁剪通道：严格 vs 快速
        need_clip = (
            scope_region is not None
            and scope_confirmed
            and not scope_none
            and args.clip != "whole"
        )
        if need_clip and backend.channel == "com":
            no_geometry = [h for h in hatches if h.rings is None]
            if no_geometry:
                if args.clip_strict == "ask":
                    confirmations.append(
                        clip_strategy_item(len(no_geometry), [h.layer for h in no_geometry])
                    )
                elif args.clip_strict == "on":
                    # 先记住 CAD 自算面积：切到 DXF 副本后只借它的边界，面积仍以 CAD 为准
                    cad_areas = {
                        h.handle.upper(): float(h.area_drawing)
                        for h in hatches
                        if h.area_drawing
                    }
                    backend, tmp_dir, switch_notes = _maybe_switch_to_dxf(
                        backend, args, force=True, reason="按规划范围严格裁剪"
                    )
                    notes.extend(switch_notes)
                    if backend.channel == "dxf":
                        rescan_started = time.monotonic()
                        # 副本扫描同样按源 DWG 指纹缓存：临时目录一删，按副本自身路径做键就再也命中不了
                        polylines, hatches = scan_or_load(
                            backend, None, args, notes, signature=original_signature, scope="dxfcopy"
                        )
                        block_stats = getattr(backend, "block_stats", None)
                        _merge_cad_areas(hatches, cad_areas, notes)
                        # 范围边界也换成副本里的（含凸度）：COM 通道读不到 bulge 时，
                        # 弧段会被当弦，范围面积与裁剪边界一起偏小。
                        if scope_region is not None and scope_source_kind == "drawing":
                            rebuilt = collect_candidates(
                                polylines,
                                hatches,
                                tolerance,
                                keywords,
                                layer_filter=[scope_layer],
                            )
                            fresh_group = [item for item in rebuilt if item.points and item.closed]
                            if len(fresh_group) == 1 and scope_region.area_source == "cad":
                                # DXF 副本没有 .Area 属性：沿用切换前读到的 CAD 自算值，
                                # 副本只负责把圆弧边界补回来
                                fresh_group[0].cad_area = scope_region.area_drawing
                            fresh_region = build_region(fresh_group, relation)
                            if fresh_region.area_drawing > 0:
                                before_ha = scope_region.area_drawing * factor * factor / 1e4
                                scope_region = fresh_region
                                scope_group = fresh_group
                                notes.append(
                                    "切到 DXF 副本后按弧段重建范围边界："
                                    f"{fresh_region.area_drawing * factor * factor / 1e4:.4f} ha"
                                    f"（切换前 {before_ha:.4f} ha，面积来源 {fresh_region.area_source}）。"
                                )
                        print(
                            f"[DXF 副本] 重新扫描完成：多段线 {len(polylines)} 个，填充 {len(hatches)} 个，"
                            f"用时 {time.monotonic() - rescan_started:.1f}s",
                            file=sys.stderr,
                            flush=True,
                        )
                    else:
                        confirmations.append(
                            clip_export_failed_item(len(no_geometry), [h.layer for h in no_geometry])
                        )
                else:
                    notes.append(
                        f"按 --clip-strict off 执行：{len(no_geometry)} 个填充读不到边界，"
                        "无法判断是否在规划范围内，已按整块计入，面积可能偏大，结论仅供粗略参考。"
                    )

        # 第一轮（计算前）：预扫描的代码/标准问题 + 范围/单位/图层可见性/裁剪口径，一次问完
        confirmations = merge_confirmations(pending_items, confirmations)
        if confirmations:
            return _emit_confirmations(confirmations, args, original_source)

        # ------------------------------------------------ 统计
        window = _parse_window(args.window) if args.window else None
        polygon = _parse_polygon(args.polygon) if args.polygon else None
        if args.scope == "window" and window is None:
            raise ValueError("--scope window 需要同时提供 --window")
        if args.scope == "polygon" and polygon is None:
            raise ValueError("--scope polygon 需要同时提供 --polygon")
        if args.scope == "handles" and not args.handles:
            raise ValueError("--scope handles 需要同时提供 --handles")

        layer_filter = {name.lower() for name in _split_list(args.layers)} if args.layers else None
        exclude_layers = {name.lower() for name in _split_list(args.exclude_layers)} if args.exclude_layers else set()
        # 扫描前判定的「范围内面积为 0 的未登记代码图层」在这里统一排除
        exclude_layers.update(name.lower() for name in prescan_excluded)
        handle_filter = {h.upper() for h in _split_list(args.handles)} if args.handles else None
        if scope_layer and scope_source_kind == "drawing":
            # 本图内的规划范围边线只用于裁剪，本身不是用地地块，避免被当成未识别面积
            exclude_layers.add(scope_layer.lower())
        if layer_filter is None and scope_keyword_layers:
            # 未选中的同关键词图层（通常是被忽略的范围草稿）同样不是用地地块，
            # 但不覆盖用户显式 --layers 指定的范围
            auto_excluded = sorted(
                name for name in scope_keyword_layers if name.lower() not in exclude_layers
            )
            if auto_excluded:
                exclude_layers.update(name.lower() for name in auto_excluded)
                notes.append(
                    "规划范围边界图层不计入地块面积，已排除："
                    + "、".join(auto_excluded)
                    + "；如需统计请用 --layers 显式指定或调整 --scope-layers 关键词。"
                )

        def accept(layer: str, handle: str) -> bool:
            if not layer_report.is_visible(layer):
                layer_report.skip(layer)
                return False
            if layer_filter is not None and layer.lower() not in layer_filter:
                return False
            if layer.lower() in exclude_layers:
                return False
            if mapper.is_ignored(layer):
                return False
            if handle_filter is not None and handle.upper() not in handle_filter:
                return False
            return True

        issues: List[Issue] = []
        recovered = _recover_broken_hatches(backend, hatches, args.hatch_fallback, notes)

        polyline_parcels = _build_polyline_parcels(polylines, accept, tolerance, factor, issues)
        arc_stats: Dict[str, int] = {}
        hatch_parcels = _build_hatch_parcels(
            hatches, accept, factor, issues, arc_stats, use_arc_variant=(args.arc_variant == "on")
        )

        polyline_parcels, out_poly = _filter_scope(polyline_parcels, args.scope, window, polygon, issues)
        hatch_parcels, out_hatch = _filter_scope(hatch_parcels, args.scope, window, polygon, issues)
        out_of_scope = out_poly + out_hatch

        parcels, dropped, primitive_note = _merge_by_primitive(
            args.primitive, polyline_parcels, hatch_parcels, args.allow_polyline_fill
        )

        clip_stats = {"full": 0, "partial": 0, "none": 0, "fallback": 0, "invalid": 0}
        if scope_region is not None and scope_confirmed:
            parcels, clip_stats = _clip_to_region(parcels, scope_region, args, factor, issues)

        dedup_records, cross_records = resolve_overlaps(
            parcels,
            factor,
            min_area_m2=args.overlap_min_area,
            min_ratio=args.overlap_min_ratio,
        )
        overlap_info = summarize_overlaps(dedup_records, cross_records)
        # 重叠发生在分类之前，这里补一次图层→代码的映射，方便报告里认出是哪类用地
        for item in overlap_info["same_layer"]["items"]:
            if not item.get("code"):
                item["code"] = mapper.match(item.get("layer") or "") or ""
        for item in overlap_info["cross_layer"]["items"]:
            if not item.get("code_a"):
                item["code_a"] = mapper.match(item.get("layer_a") or "") or ""
            if not item.get("code_b"):
                item["code_b"] = mapper.match(item.get("layer_b") or "") or ""

        # 未登记代码：先问再算，未确认不出表。
        # 这里不立即中断——把「未登记代码 / 混合未解析 / 重叠闸门」合并成第二轮一次问完。
        late_items: List[dict] = []
        unregistered_items = unregistered_code_items(parcels, mapper, tree, skip_codes)
        if unregistered_items:
            late_items.extend(unregistered_items)
        if skip_codes:
            notes.append(
                "未登记代码 " + "、".join(sorted(skip_codes)) + " 经确认本次不处理，沿用兜底归类，需复核。"
            )

        if scope_region is not None and scope_confirmed:
            if scope_source_kind == "file" and scope_file_info:
                scope_text = (
                    f"外部范围线文件「{scope_file_info['path']}」"
                    f"（图层 {scope_layer.rsplit(':', 1)[-1]}，{len(scope_group)} 个边界，关系 {relation}）"
                )
            else:
                scope_text = f"规划范围图层「{scope_layer}」（{len(scope_group)} 个边界，关系 {relation}）"
        elif scope_none:
            scope_text = "全部可见图层填充合计（无规划范围，未做空间裁剪）"
        else:
            scope_text = {
                "all": "全图",
                "window": f"矩形窗口 {window}",
                "polygon": f"多边形范围（{len(polygon) if polygon else 0} 个顶点）",
                "handles": f"指定对象 {len(handle_filter or [])} 个",
            }[args.scope]

        primitive_text = {
            "auto": "填充为准（整图无填充时整体回退多段线）",
            "hatch": "仅填充（默认口径）",
            "polyline": "仅多段线",
            "both": "填充 + 多段线去重",
        }[args.primitive]

        plan_area_m2 = args.total_area * 1e4 if args.total_area else None
        if plan_area_m2 is None and scope_region is not None and scope_confirmed:
            plan_area_m2 = scope_region.area_drawing * factor * factor
        if args.total_area and scope_region is not None and scope_confirmed:
            scope_m2 = scope_region.area_drawing * factor * factor
            if scope_m2 > 0 and abs(scope_m2 - plan_area_m2) / scope_m2 > 0.005:
                notes.append(
                    f"--total-area 给定 {args.total_area:.4f} ha，与识别到的范围面积 "
                    f"{scope_m2 / 1e4:.4f} ha 相差 {abs(scope_m2 - plan_area_m2) / scope_m2 * 100:.2f}%（>0.5%）；"
                    "占比基数按 --total-area 计算，请确认两者口径是否一致。"
                )

        meta = RunMeta(
            channel=backend.channel,
            source=original_source,
            standard=(
                f"{convert_tree.title or convert_tree.standard}（由 {tree.standard} 换算）"
                if convert_tree is not None
                else (tree.title or tree.standard)
            ),
            unit=unit,
            unit_source=unit_source,
            unit_factor=factor,
            scope=scope_text,
            plan_area_m2=plan_area_m2,
            population=args.population,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            mapping_file=str(mapper.path),
            classification_file=str(tree.path),
            layer_mode=layer_report.mode,
            layout=layout_label,
            scope_layer=scope_layer,
            scope_relation=relation if scope_confirmed else "",
            scope_area_m2=scope_region.area_drawing * factor * factor if scope_region is not None else None,
            scope_method=scope_region.method if scope_region is not None else "",
            clip_mode=args.clip,
        )
        if block_stats is not None:
            meta.blocks_expanded = block_stats.inserts
            meta.blocks_entities = block_stats.expanded_entities

        meta.notes.append(f"识别对象：{primitive_text}；填充图元 {len(hatches)} 个，多段线图元 {len(polylines)} 个。")
        meta.notes.append(f"图层口径：{layer_report.note}")
        if layer_report.skipped_layers:
            meta.notes.append("已跳过的不可见图层：" + "、".join(layer_report.skipped_layers[:10]))
        if backend.channel == "com":
            meta.notes.append("COM 通道多段线面积取自 AutoCAD 自算的 .Area；填充优先用 Area，读不到时导出 DXF 重建边界。")
        if primitive_note:
            meta.notes.append(primitive_note)
        if conversion is not None:
            meta.notes.append(
                f"新旧代码换算依据：{conversion.source}（换算表 {conversion.path}）；"
                "查不到精确对应的代码按上位类回退，歧义代码（如 E2/Ej 同时对应多个新类）不换算并列入待确认。"
            )
        if recovered:
            meta.notes.append(f"已对 {recovered} 个异常填充完成 DXF 兜底重建。")
        if scope_region is not None and scope_confirmed:
            method_text = "shapely 精确求交" if scope_region.exact else "无 shapely，按整块近似"
            meta.notes.append(
                f"规划范围面积 {scope_region.area_drawing * factor * factor / 1e4:.4f} ha，"
                f"裁剪方式：{method_text}；完全在内 {clip_stats.get('full', 0)} 个、"
                f"部分相交 {clip_stats.get('partial', 0)} 个、完全在外（已排除）{clip_stats.get('none', 0)} 个、"
                f"未能裁剪 {clip_stats.get('bypass', 0)} 个。"
            )
            if clip_stats.get("bypass"):
                meta.notes.append(
                    f"有 {clip_stats['bypass']} 个填充因缺少边界无法判断是否在规划范围内，已按整块计入，"
                    "面积可能偏大；多为填充边界退化或丢失，可在 CAD 里 HATCHEDIT 重画后再跑。"
                )
            if not HAS_SHAPELY:
                meta.notes.append("未安装 shapely，跨界地块按近似口径处理，建议 pip install shapely 后重跑。")
            geom_drawing = scope_region.area_geom_drawing
            if scope_region.area_source == "cad":
                if geom_drawing:
                    deviation = abs(geom_drawing - scope_region.area_drawing) / max(scope_region.area_drawing, 1.0)
                    meta.notes.append(
                        f"规划范围面积取 CAD 自算 .Area（{scope_region.area_drawing * factor * factor / 1e4:.4f} ha）；"
                        f"几何离散 {geom_drawing * factor * factor / 1e4:.4f} ha，偏差 {deviation * 100:.3f}%。"
                    )
                else:
                    meta.notes.append("规划范围面积取 CAD 自算 .Area（多段线圆弧无法用弦多边形准确表达）。")
            elif geom_drawing:
                meta.notes.append(
                    "规划范围面积取几何离散值（未取到 CAD 自算 .Area）；若范围线含圆弧，面积可能偏小。"
                )
            if scope_source_kind == "file":
                meta.notes.append(
                    "范围线取自外部文件，按「同坐标系原位」假设直接对齐，未做任何坐标变换；"
                    "若坐标系或单位不一致，面积与占比会整体失真。"
                )
        elif scope_none:
            meta.notes.append(
                "无规划范围：总面积取全部可见图层填充合计，未做空间裁剪、范围相交判定与差额校验。"
            )
        no_geometry = sum(1 for parcel in parcels if parcel.outer is None)
        if no_geometry:
            meta.notes.append(f"其中 {no_geometry} 个填充缺少边界几何（仅取到 Area），未参与重叠与范围判定。")
        if out_of_scope:
            meta.notes.append(f"统计范围外或跨界的图元 {out_of_scope} 个，已排除。")
        meta.notes.append(f"闭合容差 {tolerance:.4f} 图纸单位（{tolerance * factor:.4f} m）。")
        meta.notes.extend(notes)

        summary = build_summary(
            parcels,
            tree,
            mapper,
            meta,
            plan_area_m2=meta.plan_area_m2,
            population=meta.population,
            include_codes=_split_list(args.include_codes),
            exclude_codes=_split_list(args.exclude_codes),
            issues=issues,
            entity_count=len(polylines) + len(hatches),
            derive_road=(
                "off" if scope_none else (args.derive_road if args.derive_road != "on" else "auto")
            ),
            road_tolerance=args.derive_road_tolerance,
            road_max_share=args.derive_road_max_share,
            convert_tree=convert_tree,
            conversion=conversion,
        )
        if summary.hybrid_unresolved and not args.ignore_hybrid_unresolved:
            # 同样并入第二轮：与未登记代码、重叠闸门一起问，不单独打断
            late_items.extend(hybrid_unresolved_items(summary.hybrid_unresolved))
        summary.layer_report = layer_report.to_dict()
        # 换算过标准时行数据已按目标标准归类，平衡口径必须跟着目标分类表走
        balance_tree = convert_tree if convert_tree is not None else tree
        balance_config = dict(balance_tree.balance_groups)
        if args.construction_codes:
            balance_config["construction"] = _expand_codes(args.construction_codes)
        summary.balance_spec = BalanceSpec.from_config(
            balance_config,
            standard=balance_tree.standard,
            population=meta.population or 0,
        ).to_dict()
        summary.overlap = overlap_info
        same_layer = overlap_info["same_layer"]
        cross_layer = overlap_info["cross_layer"]
        if same_layer["count"]:
            meta.notes.append(
                f"同图层重叠 {same_layer['count']} 处，已从较小地块扣减 {same_layer['deducted_m2'] / 1e4:.4f} ha，"
                "该图层面积只算一次（多为绘图不规范所致）。"
            )
        if cross_layer["count"]:
            meta.notes.append(
                f"跨图层重叠 {cross_layer['count']} 处，重叠面积 {cross_layer['area_m2'] / 1e4:.4f} ha，"
                "各图层面积均保留未扣减；如需要明细请加 --overlap-report。"
            )
        if clip_stats.get("bypass"):
            summary.checks.insert(
                0,
                CheckItem(
                    label="范围裁剪",
                    ok=False,
                    message=(
                        f"{clip_stats['bypass']} 个填充缺少边界、未参与范围裁剪，已按整块计入，面积可能偏大；"
                        "多为填充边界丢失或退化，可在 CAD 里 HATCHEDIT 重画后重跑"
                    ),
                ),
            )
        arc_mismatch = [item for item in issues if item.kind == "area_mismatch"]
        if arc_stats.get("curved"):
            rescued = arc_stats.get("rescued", 0)
            detail = f"{len(arc_mismatch)} 个与 CAD 相差超过 5%，面积已按 CAD 自算值计，几何仅用于裁剪" if arc_mismatch else "全部与 CAD 自算面积一致（<5%）"
            if rescued:
                detail = f"其中 {rescued} 个改用更接近 CAD 的一套离散；" + detail
            summary.checks.insert(
                0,
                CheckItem(
                    label="弧段还原",
                    ok=not arc_mismatch,
                    message=f"{arc_stats['curved']} 个含弧填充（DXF 的 ccw 标志不可全信）；{detail}",
                ),
            )
        if scope_region is not None and scope_confirmed:
            geom_drawing = scope_region.area_geom_drawing
            if scope_region.area_source == "cad" and (geom_drawing or 0) > 0:
                deviation = abs(geom_drawing - scope_region.area_drawing) / max(scope_region.area_drawing, 1.0)
                summary.checks.insert(
                    0,
                    CheckItem(
                        label="范围面积来源",
                        ok=deviation <= 0.001,
                        message=(
                            f"面积取 CAD 自算 .Area {scope_region.area_drawing * factor * factor / 1e4:.4f} ha；"
                            f"几何离散 {geom_drawing * factor * factor / 1e4:.4f} ha，偏差 {deviation * 100:.3f}%"
                            + (
                                "（≤0.1%，口径一致）"
                                if deviation <= 0.001
                                else "（>0.1%，弧段离散偏差偏大，建议复核范围线圆弧或改用范围线文件）"
                            )
                        ),
                    ),
                )
            summary.scope_info = {
                "source": scope_source_kind,
                "layer": scope_layer,
                "relation": relation,
                "method": scope_region.method,
                "exact": scope_region.exact,
                "area_m2": round(scope_region.area_drawing * factor * factor, 2),
                "area_source": scope_region.area_source,
                "area_geom_m2": (
                    round(geom_drawing * factor * factor, 2) if (geom_drawing or 0) > 0 else None
                ),
                "rings": len(scope_group),
                "clip_stats": clip_stats,
                "file": scope_file_info,
                "alignment": scope_alignment,
            }
        elif scope_none:
            summary.scope_info = {
                "source": "none",
                "layer": "",
                "relation": "",
                "method": "",
                "exact": False,
                "area_m2": None,
                "rings": 0,
                "clip_stats": clip_stats,
                "note": (
                    "无规划范围：总面积取全部可见图层填充合计，"
                    "未做空间裁剪、范围相交判定与差额校验。"
                ),
            }
        summary.confirmations = []
        # ---- 重叠告警闸门：同图层只提示不追问；跨图层超阈值必须停表四选一 ----
        overlap_base_m2 = float((summary.scope_info or {}).get("area_m2") or summary.ratio_base_m2 or 0.0)
        base_note = "规划范围总面积" if (summary.scope_info or {}).get("area_m2") else "分类合计（无规划范围）"
        summary.overlap_gate = compute_gate(
            overlap_info,
            base_m2=overlap_base_m2,
            threshold_ratio=float(args.overlap_threshold or 0.0),
            base_note=base_note,
        )
        summary.overlap_gate["detail_report"] = bool(args.overlap_report)
        threshold_pct = summary.overlap_gate["threshold_pct"]
        same_gate = summary.overlap_gate["same_layer"]
        cross_gate = summary.overlap_gate["cross_layer"]
        same_share = same_gate["share_pct"]
        cross_share = cross_gate["share_pct"]
        same_over = same_gate["over"]
        cross_over = cross_gate["over"]
        same_causes = "重复描图、块展开叠加、嵌套包含、分次绘制/版本合并、填充边界自相交"
        if same_layer["count"]:
            same_msg = (
                f"{same_layer['count']} 处重叠已从较小地块扣减 {same_layer['deducted_m2'] / 1e4:.4f} ha，"
                "该图层面积只算一次"
            )
            if same_over:
                same_msg += (
                    f"；占规划范围 {same_share:.2f}%，已超 {threshold_pct:.0f}% 阈值，"
                    f"可能原因为{same_causes}，建议按句柄回 CAD 核对（面积仍按扣减计一次，不影响合计口径）"
                )
                meta.notes.append(
                    f"同图层重叠扣减 {same_layer['deducted_m2'] / 1e4:.4f} ha，占规划范围 {same_share:.2f}%，"
                    f"超过 {threshold_pct:.0f}% 阈值；可能原因为{same_causes}，建议按句柄回 CAD 核对。"
                )
            same_msg += "；明细报告已生成" if args.overlap_report else "；如需明细报告加 --overlap-report"
            summary.checks.insert(0, CheckItem(label="同图层重叠", ok=not same_over, message=same_msg))
        if cross_layer["count"]:
            cross_msg = f"{cross_layer['count']} 处、共 {cross_layer['area_m2'] / 1e4:.4f} ha"
            if overlap_base_m2:
                cross_msg += f"，占规划范围 {cross_share:.2f}%"
            cross_msg += "，各图层面积均保留"
            if cross_over:
                cross_msg += (
                    f"（已超 {threshold_pct:.0f}% 阈值，需确认交付口径）"
                )
                meta.notes.append(
                    f"跨图层重叠 {cross_layer['area_m2'] / 1e4:.4f} ha，占规划范围 {cross_share:.2f}%，"
                    f"超过 {threshold_pct:.0f}% 阈值，两侧面积均保留，道路反算值可能偏小。"
                )
            cross_msg += "；明细报告已生成" if args.overlap_report else "；如需明细报告加 --overlap-report"
            summary.checks.insert(0, CheckItem(label="跨图层重叠", ok=not cross_over, message=cross_msg))

        if cross_over and not (args.overlap_report or args.overlap_ack):
            summary.confirmations.append(
                {
                    "type": "overlap_gate",
                    "question": (
                        f"复检发现跨图层重叠 {cross_layer['count']} 处、共 "
                        f"{cross_layer['area_m2'] / 1e4:.4f} ha，占{summary.overlap_gate['base_note']} "
                        f"{overlap_base_m2 / 1e4:.4f} ha 的 {cross_share:.2f}%，已超过 "
                        f"{threshold_pct:.0f}% 阈值。成因多为同一块地被两层重复描图"
                        "（例：留白用地压在公园绿地上）。两侧面积目前都保留在表内，"
                        "因此道路用地（1207）的反算值会偏小。请选择处理方式："
                    ),
                    "options": [
                        {
                            "id": "report_only",
                            "label": "先出土地利用平衡表，暂时不需要明细报告",
                            "args": ["--overlap-ack"],
                            "description": "出表，报告内保留一句跨图层重叠提示",
                        },
                        {
                            "id": "report_and_detail",
                            "label": "先出土地利用平衡表，并生成明细的跨图层重叠报告",
                            "args": ["--overlap-report"],
                            "description": "出表 + *_overlap.md（逐对句柄 + 按图层汇总），便于回改 CAD",
                        },
                        {
                            "id": "diagnose_first",
                            "label": "先不出土地利用平衡表，先仔细检查是否出现其它问题",
                            "args": ["--diagnose-only"],
                            "description": "只输出排查清单：未识别/未闭合/自相交/填充面积读不到/裁剪与单位口径/道路反算状态",
                        },
                        {
                            "id": "custom",
                            "label": "其它处理方式（按你的说明执行）",
                            "args": [],
                            "requires_input": "overlap_decision",
                            "description": "你说怎么处理，我按你的口径执行并在交付说明里记明",
                        },
                    ],
                    "detail": {
                        "count": cross_layer["count"],
                        "area_m2": cross_layer["area_m2"],
                        "base_m2": round(overlap_base_m2, 2),
                        "share_pct": round(cross_share, 4),
                        "threshold_pct": round(threshold_pct, 4),
                        "layers": cross_layer["layers"],
                        "same_layer": summary.overlap_gate["same_layer"],
                        "approximate": cross_layer["approximate"],
                    },
                }
            )
        # 第二轮（计算后）：未登记代码 + 混合未解析 + 重叠闸门，一次问完再重跑
        late_pending = merge_confirmations(late_items, summary.confirmations)
        if args.diagnose_only:
            _print_diagnose(summary)
            if not late_pending:
                return 0
        if late_pending:
            return _emit_confirmations(late_pending, args, original_source)

        if args.write_back:
            _write_back(backend, parcels, factor, args)

        md_path, json_path = _resolve_outputs(args)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(render_markdown(summary), encoding="utf-8")
        json_path.write_text(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        if args.overlap_report:
            overlap_path = md_path.with_name(f"{md_path.stem}_overlap.md")
            overlap_path.write_text(render_overlap_report(summary), encoding="utf-8")
            print(f"重叠明细：{overlap_path}")

        if not args.quiet:
            _print_console_summary(summary, md_path, json_path)
        return 0
    finally:
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _write_back(backend, parcels: Sequence[ParcelRecord], factor: float, args) -> None:
    labeled = [parcel for parcel in parcels if parcel.outer]
    if not labeled:
        print("[写回] 没有可用于定位的几何边界，跳过标注。", file=sys.stderr)
        return
    height = 2.5 / factor if factor else 2.5
    palette = [1, 3, 5, 7, 2, 4, 6]
    root_order: Dict[str, int] = {}
    for parcel in labeled:
        point = bbox_center(bbox_of(parcel.outer))
        label = f"{parcel.code or '?'} {parcel.area_m2 / 1e4:.4f}ha"
        backend.add_text(args.label_layer, point, label, height=height)
        if parcel.clipped and parcel.clipped_points:
            try:
                backend.add_polyline(args.clipped_layer, parcel.clipped_points, closed=True)
            except Exception:
                pass
        if args.colorize and parcel.code:
            root = parcel.code[:2] if parcel.code[:1].isdigit() else parcel.code[:1]
            index = root_order.setdefault(root, len(root_order))
            try:
                backend.set_color(parcel.handle, palette[index % len(palette)])
            except Exception:
                pass

    if backend.channel == "com":
        print("[写回] 已在当前图纸添加面积标注与裁剪边界副本（未保存，请确认后手动保存）。", file=sys.stderr)
    elif args.file:
        target = Path(args.file).with_name(f"{Path(args.file).stem}_annotated.dxf")
        backend.save_copy(str(target))


def _default_report_path(backend) -> Path:
    """默认报告路径：<图纸目录>/<图名>_成果/<图名>_用地平衡表.md。"""
    source = getattr(backend, "path", None)
    if not source:
        return Path.cwd() / "CAD成果" / "land_area_report.md"
    base = Path(str(source))
    return base.parent / f"{base.stem}_成果" / f"{base.stem}_用地平衡表.md"


def _resolve_outputs(args) -> Tuple[Path, Path]:
    out = Path(args.out)
    if out.is_dir() or args.out.endswith(("/", "\\")):
        out = out / "land_area_report.md"
    if out.suffix.lower() not in {".md", ".markdown"}:
        out = out.with_suffix(".md")
    json_path = Path(args.json) if args.json else out.with_suffix(".json")
    return out, json_path


def _print_diagnose(summary: Summary) -> None:
    """跨图层重叠超阈值时的排查模式：只出排查清单，不生成平衡表。"""
    gate = summary.overlap_gate or {}
    same = (gate.get("same_layer") or {})
    cross = (gate.get("cross_layer") or {})
    scope = summary.scope_info or {}
    print("")
    print("=" * 72)
    print("排查模式（--diagnose-only）：不出平衡表，逐项核对下列问题")
    print("=" * 72)
    print(f"数据来源：{summary.meta.source}    单位：{summary.meta.unit}（{summary.meta.unit_source}）")
    print(
        f"规划范围：{scope.get('layer') or '无'}    面积："
        + (f"{scope['area_m2'] / 1e4:.4f} ha" if scope.get("area_m2") else "未确定（--scope none）")
        + f"    裁剪：{scope.get('method') or '—'}"
    )
    clip = scope.get("clip_stats") or {}
    if clip:
        print(
            f"裁剪统计：完全在内 {clip.get('full', 0)}、部分相交 {clip.get('partial', 0)}、"
            f"在外排除 {clip.get('none', 0)}、未裁剪 {clip.get('bypass', 0)}"
        )
    print("-" * 72)
    print(
        f"[1] 重叠：同图层 {same.get('count', 0)} 处 / 扣减 {same.get('deducted_m2', 0) / 1e4:.4f} ha"
        f"（占 {same.get('share_pct', 0):.2f}%，阈值 {gate.get('threshold_pct', 0):.0f}%）"
        + (" ← 超阈值" if same.get("over") else "")
    )
    print(
        f"           跨图层 {cross.get('count', 0)} 处 / {cross.get('area_m2', 0) / 1e4:.4f} ha"
        f"（占 {cross.get('share_pct', 0):.2f}%）"
        + (" ← 超阈值" if cross.get("over") else "")
    )
    if cross.get("layers"):
        print(f"           涉及图层：{'、'.join(cross['layers'][:8])}")
    print("-" * 72)
    if summary.unclassified:
        print(f"[2] 待确认图层 {len(summary.unclassified)} 个（面积未计入表内）：")
        for row in summary.unclassified[:10]:
            print(f"    - {row.layer}：{row.count} 个，{row.area_m2 / 1e4:.4f} ha")
    else:
        print("[2] 待确认图层：无")
    print("-" * 72)
    if summary.issues:
        counts: Dict[str, int] = {}
        for issue in summary.issues:
            counts[issue.kind] = counts.get(issue.kind, 0) + 1
        summary_text = "、".join(f"{k} {v} 个" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
        print(f"[3] 异常项 {len(summary.issues)} 条：{summary_text}")
        # 先把影响地块面积的异常排前面：填充面积读不到 > 自相交 > 未闭合 > 零面积 > 背景填充
        priority = {"hatch_area_unavailable": 0, "self_intersect": 1, "unclosed": 2, "zero_area": 3, "background_fill": 4}
        parcel_issues = [i for i in summary.issues if i.kind in priority]
        parcel_issues.sort(key=lambda i: (priority.get(i.kind, 9), i.layer))
        for issue in parcel_issues[:10]:
            print(f"    - [{issue.kind}] {issue.layer} {issue.handle}：{issue.message}")
        if len(parcel_issues) > 10:
            print(f"    …其余 {len(parcel_issues) - 10} 条（完整清单见出表后的 summary.json 或重叠明细报告）")
    else:
        print("[3] 异常项：无")
    print("-" * 72)
    road = summary.road_derivation or {}
    if road:
        print(
            f"[4] 道路反算：{road.get('root', '12')} 类 {road.get('root_area_m2', 0) / 1e4:.4f} ha，"
            f"其中 {road.get('code', '1207')} {road.get('code_name', '城镇村道路用地')} "
            f"{road.get('code_area_m2', 0) / 1e4:.4f} ha（derived，非实测）"
        )
        if road.get("formula"):
            print(f"    {road['formula']}")
        if road.get("clamped"):
            print("    ← 反算结果为负已按 0 计，需人工复核其他一级类是否超出规划范围")
    else:
        print("[4] 道路反算：未执行（无规划范围，或已因未识别面积超标而阻断）")
    print("-" * 72)
    bad_checks = [c for c in summary.checks if not c.ok]
    if bad_checks:
        print("[5] 需复核项：")
        for check in bad_checks:
            print(f"    - {check.label}：{check.message}")
    else:
        print("[5] 需复核项：无")
    print("=" * 72)
    print("排查建议：先按句柄在 CAD 内核对重叠图元是否重复描图；补齐待确认图层；")
    print("           确认单位与范围口径后，再决定是否出表（加 --overlap-ack 或 --overlap-report）。")


def _print_console_summary(summary: Summary, md_path: Path, json_path: Path) -> None:
    print("")
    print(f"通道：{summary.meta.channel}    单位：{summary.meta.unit}（{summary.meta.unit_source}）")
    print(f"分类标准：{summary.meta.standard}")
    if summary.conversion:
        conv = summary.conversion
        print(
            f"新旧换算：成功 {conv.get('converted_count', 0)} 个 / {conv.get('converted_area_m2', 0) / 1e4:.4f} ha，"
            f"失败 {conv.get('failed_count', 0)} 个 / {conv.get('failed_area_m2', 0) / 1e4:.4f} ha"
        )
    print(f"图层口径：{summary.meta.layer_mode}    统计范围：{summary.meta.scope}")
    print(f"参与合计图元：{summary.parcel_count} / 扫描图元：{summary.entity_count}")
    print("-" * 68)
    print(f"{'代码':<8}{'用地名称':<22}{'面积(ha)':>12}{'占比(%)':>10}")
    for row in summary.level1_rows:
        print(f"{row.code:<8}{row.name:<22}{row.area_m2 / 1e4:>12.4f}{row.ratio:>10.2f}")
    print("-" * 68)
    print(f"{'合计':<30}{summary.total_area_m2 / 1e4:>12.4f}")
    if summary.unclassified:
        print(f"待确认图层：{len(summary.unclassified)} 个，面积 {sum(r.area_m2 for r in summary.unclassified) / 1e4:.4f} ha")
    if summary.issues:
        print(f"异常项：{len(summary.issues)} 条（详见报告第五节）")
    for check in summary.checks:
        if not check.ok:
            print(f"[需复核] {check.label}：{check.message}")
    gate = summary.overlap_gate or {}
    if summary.overlap:
        same = summary.overlap.get("same_layer") or {}
        cross = summary.overlap.get("cross_layer") or {}
        same_gate = gate.get("same_layer") or {}
        cross_gate = gate.get("cross_layer") or {}
        if same.get("count"):
            share_txt = f"，占规划范围 {same_gate.get('share_pct', 0):.2f}%" if gate else ""
            flag = "  ← 超阈值，疑似重复描图/嵌套包含" if same_gate.get("over") else ""
            print(
                f"[重叠] 同图层 {same['count']} 处，已扣减 {same.get('deducted_m2', 0) / 1e4:.4f} ha（只算一次）"
                f"{share_txt}{flag}"
            )
            if not gate.get("detail_report"):
                print("       如需明细报告加 --overlap-report")
        if cross.get("count"):
            share_txt = f"，占规划范围 {cross_gate.get('share_pct', 0):.2f}%" if gate else ""
            flag = "  ← 超阈值" if cross_gate.get("over") else ""
            print(
                f"[重叠] 跨图层 {cross['count']} 处、共 {cross.get('area_m2', 0) / 1e4:.4f} ha，"
                f"两图层面积均保留（未扣减）{share_txt}{flag}"
            )
    for item in summary.confirmations:
        print("")
        print(f"[待确认] {item.get('question')}")
        for option in item.get("options", []):
            hint = option.get("answer") or "、".join(option.get("args") or []) or "（按用户说明执行）"
            print(f"    - {option.get('label')}：{hint}")
    print("")
    print(f"报告：{md_path}")
    print(f"摘要：{json_path}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    _setup_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("[中断] 用户取消", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
