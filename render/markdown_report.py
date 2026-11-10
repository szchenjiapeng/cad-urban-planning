"""Markdown 渲染层：只依赖 core.models.Summary，后续 Excel/Word 渲染器可平行新增。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from core.models import Summary, SummaryRow  # noqa: E402

try:  # 作为包导入
    from render.balance import BalanceSpec, render_balance_section  # noqa: E402
except ImportError:  # 直接以脚本方式运行本文件
    from balance import BalanceSpec, render_balance_section  # noqa: E402

_MAX_ISSUE_ROWS = 100
_CN_NUM = "一二三四五六七八九十"


def _ha(area_m2: float) -> str:
    return f"{area_m2 / 1e4:,.4f}"


def _m2(area_m2: float) -> str:
    return f"{area_m2:,.2f}"


def _pct(value: float) -> str:
    return f"{value:.2f}"


def _per_capita(value: float, has_population: bool) -> str:
    return f"{value:.2f}" if has_population else "—"


def _row_line(row: SummaryRow, index, has_population: bool, level: int = 1) -> str:
    name = f"{row.name}（反算）" if row.derived else row.name
    count = "—" if row.derived else str(row.count)
    if level == 1:
        return (
            f"| {index} | {row.code} | {name} | {_ha(row.area_m2)} | {_m2(row.area_m2)} | "
            f"{_pct(row.ratio)} | {_per_capita(row.per_capita, has_population)} | {count} |"
        )
    return (
        f"| {row.code} | {name} | {_ha(row.area_m2)} | {_m2(row.area_m2)} | "
        f"{_pct(row.ratio)} | {_per_capita(row.per_capita, has_population)} | {count} |"
    )


def render_markdown(summary: Summary) -> str:
    meta = summary.meta
    has_population = bool(meta.population)
    lines: List[str] = []

    spec = BalanceSpec.from_dict(summary.balance_spec) if summary.balance_spec else None
    only_one_table = spec is not None
    section_no = 0

    def sec(title: str) -> str:
        nonlocal section_no
        section_no += 1
        label = _CN_NUM[section_no - 1] if section_no <= len(_CN_NUM) else str(section_no)
        return f"## {label}、{title}"

    lines.append("# 土地利用规划平衡表")
    lines.append("")
    lines.append(f"- **数据来源**：{meta.source or '未记录'}（通道：{meta.channel}）")
    lines.append(f"- **分类标准**：{meta.standard}")
    lines.append(f"- **图纸单位**：{meta.unit}（{meta.unit_source}）")
    lines.append(f"- **统计范围**：{meta.scope}")
    if meta.plan_area_m2:
        lines.append(f"- **规划范围总面积**：{_ha(meta.plan_area_m2)} ha（{_m2(meta.plan_area_m2)} m²）")
    if has_population:
        lines.append(f"- **人口规模**：{meta.population:,.0f} 人")
    lines.append(f"- **生成时间**：{meta.generated_at}")
    lines.append("")
    lines.append(
        f"> 占比基数："
        f"{'规划范围总面积 ' + _ha(summary.ratio_base_m2) + ' ha' if meta.plan_area_m2 else '分类合计 ' + _ha(summary.ratio_base_m2) + ' ha'}。"
        f"参与合计的图元 {summary.parcel_count} 个，识别到 {len(summary.level1_rows)} 个一级类。"
    )
    lines.append("")

    # 重叠告警：超阈值时在首屏着重提示（同图层只提示不追问，跨图层需用户四选一）
    gate = summary.overlap_gate or {}
    same_gate = gate.get("same_layer") or {}
    cross_gate = gate.get("cross_layer") or {}
    if same_gate.get("over"):
        lines.append(
            f"> **⚠ 同图层重叠异常**：{same_gate.get('count', 0)} 处已扣减 "
            f"{_ha(same_gate.get('deducted_m2', 0))} ha，占{gate.get('base_note') or '规划范围总面积'} "
            f"{same_gate.get('share_pct', 0):.2f}%，超过 {gate.get('threshold_pct', 0):.0f}% 阈值。"
            "可能原因：重复描图、块展开叠加、嵌套包含、分次绘制/版本合并、填充边界自相交；"
            "面积已按扣减计一次，不影响合计口径，建议按句柄回 CAD 核对。"
            + ("明细报告已随本表生成（`*_overlap.md`）。" if gate.get("detail_report") else "如需明细报告加 `--overlap-report`。")
        )
        lines.append("")
    if cross_gate.get("over"):
        lines.append(
            f"> **⚠ 跨图层重叠异常**：{cross_gate.get('count', 0)} 处、共 {_ha(cross_gate.get('area_m2', 0))} ha，"
            f"占{gate.get('base_note') or '规划范围总面积'} {cross_gate.get('share_pct', 0):.2f}%，"
            f"超过 {gate.get('threshold_pct', 0):.0f}% 阈值。两侧面积均保留，道路（1207）反算值可能偏小；"
            "已按用户确认的口径交付（详见「校验」与交付说明）。"
        )
        lines.append("")

    # ---------------------------------------------------------------- 一级类
    if not only_one_table:
        lines.append(sec("用地平衡表（一级类）"))
        lines.append("")
        if summary.level1_rows:
            lines.append("| 序号 | 用地代码 | 用地名称 | 面积(ha) | 面积(m²) | 占比(%) | 人均(m²/人) | 地块数 |")
            lines.append("| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: |")
            for index, row in enumerate(summary.level1_rows, start=1):
                lines.append(_row_line(row, index, has_population))
            lines.append(
                f"| — | — | **合计** | **{_ha(summary.total_area_m2)}** | **{_m2(summary.total_area_m2)}** | "
                f"**{_pct(sum(r.ratio for r in summary.level1_rows))}** | "
                f"**{_per_capita(sum(r.per_capita for r in summary.level1_rows), has_population)}** | "
                f"**{sum(r.count for r in summary.level1_rows)}** |"
            )
        else:
            lines.append("> 未统计到任何已分类地块，请检查图层映射与闭合情况。")
        lines.append("")

    # ---------------------------------------------------------------- 范式平衡表
    if spec is not None:
        lines.append(sec("规划土地利用平衡表"))
        lines.append("")
        lines.extend(render_balance_section(summary, spec))
        lines.append("")
        lines.append(
            f"> 建设用地口径：{'、'.join(sorted(spec.construction))}，"
            f"小计为「{spec.construction_label}」；"
            f"二级、三级类只给{spec.ratio_label}，占总用地比例仅一级类与小计给值。"
        )
        lines.append("")

    # ---------------------------------------------------------------- 混合用地
    if summary.hybrid_rows:
        lines.append("### 附：混合用地清单（已在平衡表中单独成行）")
        lines.append("")
        lines.append("| 原图层 | 新增代码 | 用地名称 | 面积(ha) | 地块数 |")
        lines.append("| --- | --- | --- | ---: | ---: |")
        for row in summary.hybrid_rows:
            lines.append(f"| {row.layer} | {row.code} | {row.name} | {_ha(row.area_m2)} | {row.count} |")
        lines.append("")
        lines.append(
            "> 混合用地在平衡表中单独成行：代码与名称由各段用 + 连接（如 070101+0809），"
            "层级与父级跟随首段代码，面积独立计量后逐级向上加和（不并入首段）。"
        )
        lines.append("")

    # ---------------------------------------------------------------- 标准换算
    if summary.conversion:
        conv = summary.conversion
        lines.append(sec(f"新旧标准换算（{conv.get('from_standard') or '旧标准'} → {conv.get('to_standard')}）"))
        lines.append("")
        lines.append(f"- 换算依据：{conv.get('source') or '内置换算表'}")
        lines.append(
            f"- 换算成功：{conv.get('converted_count', 0)} 个地块，"
            f"{_ha(conv.get('converted_area_m2', 0))} ha"
        )
        lines.append(
            f"- 未能换算：{conv.get('failed_count', 0)} 个地块，"
            f"{_ha(conv.get('failed_area_m2', 0))} ha（已计入待确认，未参与分类合计）"
        )
        lines.append("")
        if conv.get("failed"):
            lines.append("| 旧代码 | 原因 | 候选新代码 | 面积(ha) | 地块数 |")
            lines.append("| --- | --- | --- | ---: | ---: |")
            for item in conv["failed"][:10]:
                reason = "歧义（多个新类目）" if item.get("reason") == "ambiguous" else "无对应"
                candidates = "、".join(item.get("candidates") or []) or "—"
                lines.append(
                    f"| {item.get('old') or '—'} | {reason} | {candidates} | {_ha(item.get('area_m2', 0))} | {item.get('count', 0)} |"
                )
            lines.append("")
            lines.append("> 未能换算的代码需人工指定归属（改映射或补换算表），不要直接交付。")
            lines.append("")

    # ---------------------------------------------------------------- 二级类
    if not only_one_table:
        lines.append(sec("二级类明细"))
        lines.append("")
    if not only_one_table and summary.level2_rows:
        for root in summary.level1_rows:
            children = [row for row in summary.level2_rows if row.parent == root.code]
            if not children:
                continue
            lines.append(f"### {root.code} {root.name}（合计 {_ha(root.area_m2)} ha，占比 {_pct(root.ratio)}%）")
            lines.append("")
            lines.append("| 用地代码 | 用地名称 | 面积(ha) | 面积(m²) | 占比(%) | 人均(m²/人) | 地块数 |")
            lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: |")
            for row in children:
                lines.append(_row_line(row, None, has_population, level=2))
            lines.append("")
            # 三级类只列有数据的，避免空表刷版面
            for child in children:
                grand = [row for row in summary.level3_rows if row.parent == child.code]
                if not grand:
                    continue
                lines.append(f"**{child.code} {child.name} · 三级类**")
                lines.append("")
                lines.append("| 用地代码 | 用地名称 | 面积(ha) | 面积(m²) | 占比(%) | 人均(m²/人) | 地块数 |")
                lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: |")
                for row in grand:
                    lines.append(_row_line(row, None, has_population, level=3))
                lines.append("")
    elif not only_one_table:
        lines.append("> 本次结果全部归入一级类，无二级类明细（图层映射直接指向一级类）。")
        lines.append("")

    # ---------------------------------------------------------------- 规划范围
    lines.append(sec("规划范围与裁剪"))
    lines.append("")
    scope_info = summary.scope_info
    if scope_info and scope_info.get("source") == "none":
        lines.append(f"- **范围口径**：无规划范围（备用口径），统计范围：{meta.scope}")
        lines.append("- **总面积**：全部可见图层填充合计（未做空间裁剪、范围相交判定与差额校验）")
        lines.append("- **规划范围面积**：未提供，报告不输出「分类合计与规划范围」差额校验，也不做道路反算")
        lines.append("")
        lines.append(
            "> 本次未识别到规划范围，也未提供范围线文件。若需要覆盖率校核、道路反算或跨界裁剪，"
            "请补充规划范围图层（`--scope-layer`）或同坐标系原位的范围线文件（`--scope-file`）后重跑。"
        )
    elif scope_info:
        file_info = scope_info.get("file") or {}
        if scope_info.get("source") == "file" and file_info:
            lines.append(f"- **范围来源**：外部范围线文件 `{file_info.get('path')}`（{file_info.get('channel')}）")
            lines.append(f"- **文件图层**：{str(scope_info.get('layer') or '').rsplit(':', 1)[-1] or '—'}")
            alignment = scope_info.get("alignment") or {}
            if alignment:
                lines.append(
                    f"- **对齐粗检**：包围盒重叠 {float(alignment.get('bbox_overlap_ratio') or 0) * 100:.2f}%"
                    f"（{alignment.get('level')}；脚本不做坐标纠偏）"
                )
        else:
            lines.append(f"- **范围图层**：{scope_info.get('layer') or '—'}")
        lines.append(f"- **边界数量**：{scope_info.get('rings', 0)} 个，空间关系：{scope_info.get('relation')}")
        lines.append(f"- **范围面积**：{_ha(scope_info.get('area_m2') or 0)} ha")
        area_m2 = float(scope_info.get("area_m2") or 0)
        geom_m2 = scope_info.get("area_geom_m2")
        if scope_info.get("area_source") == "cad":
            source_text = "CAD 自算 .Area（与 AREA 命令同源，含圆弧）"
            if geom_m2:
                deviation = abs(float(geom_m2) - area_m2) / max(area_m2, 1e-9)
                source_text += f"；几何离散 {_ha(geom_m2)} ha，偏差 {deviation * 100:.3f}%"
            lines.append(f"- **面积来源**：{source_text}")
        else:
            lines.append(
                "- **面积来源**：几何离散（未取到 CAD 自算 .Area）；范围线含圆弧时面积可能偏小"
            )
        lines.append(
            f"- **裁剪方式**：{'shapely 精确求交' if scope_info.get('exact') else '几何近似（未安装 shapely）'}"
        )
        stats = scope_info.get("clip_stats") or {}
        lines.append(
            f"- **裁剪结果**：完全在内 {stats.get('full', 0)} 个、部分相交 {stats.get('partial', 0)} 个、"
            f"完全在外 {stats.get('none', 0)} 个、近似处理 {stats.get('fallback', 0)} 个"
        )
        lines.append("")
        if scope_info.get("source") == "file":
            lines.append(
                "> 范围线取自外部文件，按「同坐标系原位」假设直接对齐，未做任何坐标变换；"
                "若坐标系或单位不一致，面积与占比会整体失真。跨界地块按相交部分计算面积，原图元未被修剪。"
            )
        else:
            lines.append("> 跨界地块按相交部分计算面积，原图元未被修剪；写回时仅在副本图层生成裁剪边界。")
    else:
        lines.append(f"> 未识别到规划范围图层，统计口径为：{meta.scope}")
    lines.append("")

    # ---------------------------------------------------------------- 图层可见性
    lines.append(sec("图层可见性"))
    lines.append("")
    layer_report = summary.layer_report or {}
    lines.append(f"- **统计口径**：{meta.layer_mode or layer_report.get('mode', 'model')}")
    if layer_report.get("layout"):
        lines.append(f"- **布局**：{layer_report.get('layout')}")
    diffs = layer_report.get("diffs") or []
    skipped = layer_report.get("skipped_layers") or []
    if diffs:
        lines.append("")
        lines.append("| 图层 | 模型空间 | 布局视口 | 说明 |")
        lines.append("| --- | --- | --- | --- |")
        for item in diffs[:30]:
            lines.append(
                f"| {item.get('layer')} | {item.get('model_state')} | {item.get('layout_state')} | {item.get('description', '')} |"
            )
    if skipped:
        lines.append("")
        lines.append(f"> 按当前口径跳过、未计入的图层：{'、'.join(skipped[:20])}")
    if not diffs and not skipped:
        lines.append("> 未发现关闭/冻结图层，也没有图元因可见性被排除。")
    lines.append("")

    # ---------------------------------------------------------------- 校验
    lines.append(sec("校验"))
    lines.append("")
    lines.append("| 校验项 | 结果 | 说明 |")
    lines.append("| --- | --- | --- |")
    for check in summary.checks:
        lines.append(f"| {check.label} | {'通过' if check.ok else '**需复核**'} | {check.message} |")
    lines.append("")

    # ---------------------------------------------------------------- 待确认
    lines.append(sec("待确认图层（未识别）"))
    lines.append("")
    if summary.unclassified:
        lines.append("| 图层 | 图元数 | 面积(ha) | 示例句柄 |")
        lines.append("| --- | ---: | ---: | --- |")
        for row in summary.unclassified:
            handles = "、".join(row.handles[:5]) if row.handles else "—"
            lines.append(f"| {row.layer} | {row.count} | {_ha(row.area_m2)} | {handles} |")
        lines.append("")
        lines.append("> 这些图层未命中映射配置，面积**未计入**上表。请确认用地性质后补充 mapping 配置并重跑。")
    else:
        lines.append("> 无未识别图层。")
    lines.append("")

    # ---------------------------------------------------------------- 异常
    lines.append(sec("异常清单"))
    lines.append("")
    if summary.issues:
        lines.append("| 类型 | 图层 | 句柄 | 说明 |")
        lines.append("| --- | --- | --- | --- |")
        for issue in summary.issues[:_MAX_ISSUE_ROWS]:
            lines.append(f"| {issue.kind} | {issue.layer or '—'} | {issue.handle or '—'} | {issue.message} |")
        if len(summary.issues) > _MAX_ISSUE_ROWS:
            lines.append("")
            lines.append(f"> 仅显示前 {_MAX_ISSUE_ROWS} 条，共 {len(summary.issues)} 条，完整清单见 summary.json。")
    else:
        lines.append("> 无异常图元。")
    lines.append("")

    # ---------------------------------------------------------------- 说明
    lines.append(sec("测算说明"))
    lines.append("")
    notes: List[str] = [
        "识别对象只取填充（HATCH）色块，不用多段线补填充缺口；城镇道路等不填充类目由差额反算得出，"
        "如需用多段线补缺须显式加 --allow-polyline-fill（会与反算重复计面积）。",
        "填充面积优先取 CAD 自算值；读取失败时导出 DXF 重建边界，并按奇偶嵌套规则扣除孤岛（洞）。",
        f"面积按围合范围计算，图纸单位 {meta.unit} 换算系数 {meta.unit_factor:g}；含圆弧边界按 2° 步长离散后积分。",
        "未闭合多段线不参与合计，已列入异常清单；重叠分两种：同图层重叠从较小地块扣减、面积只算一次，"
        "跨图层重叠保留各图层面积并单独提示（明细需 --overlap-report）。",
        "占比 = 该类面积 ÷ 占比基数；基数缺省为分类合计，提供规划范围总面积时以规划范围为准。",
        "分类标准为 " + meta.standard + "，图层映射文件：" + (meta.mapping_file or "默认配置") + "。",
        "面积自下而上逐级加和：三级类同时计入其二级类与一级类，二级类合计含其下三级类；"
        "混合用地单独成行后同样逐级加和，面积不拆分。",
    ]
    if only_one_table and summary.level1_rows:
        notes.append(
            "一级类地块数与人均用地（复核用，明细表已并入平衡表）："
            + "；".join(
                f"{row.code} {row.name} {row.count} 块"
                + (f"（{row.per_capita:.2f} m²/人）" if has_population else "")
                for row in summary.level1_rows
            )
            + "。"
        )
    if summary.hybrid_rows:
        notes.append(
            "混合用地已单独成行（"
            + "、".join(f"{row.layer}→{row.code}" for row in summary.hybrid_rows[:5])
            + "），面积不拆分、只按合并代码统计。"
        )
    if meta.plan_area_m2 is None:
        notes.append("未提供规划范围总面积（--total-area），无法校核用地覆盖完整性，建议补充后重跑。")
    if has_population:
        notes.append(f"人均用地按人口 {meta.population:,.0f} 人折算，口径需与委托方确认。")
    notes.extend(meta.notes)
    for index, note in enumerate(notes, start=1):
        lines.append(f"{index}. {note}")
    lines.append("")
    return "\n".join(lines)


def render_overlap_report(summary: Summary) -> str:
    """独立的重叠明细报告：只用于定位绘图问题，不参与平衡表合计。"""
    info = summary.overlap or {}
    same = info.get("same_layer") or {}
    cross = info.get("cross_layer") or {}
    meta = summary.meta

    lines: List[str] = []
    lines.append("# 重叠明细报告")
    lines.append("")
    lines.append(f"- **数据来源**：{meta.source}")
    lines.append(f"- **生成时间**：{meta.generated_at}")
    lines.append(f"- **同图层重叠**：{same.get('count', 0)} 处，已扣减 {_ha(same.get('deducted_m2', 0))} ha（统计中只算一次）")
    lines.append(f"- **跨图层重叠**：{cross.get('count', 0)} 处，重叠 {_ha(cross.get('area_m2', 0))} ha（两侧面积均保留）")
    lines.append("")
    lines.append(
        "> 本报告仅用于定位绘图问题以回改 CAD，不参与平衡表合计。"
        "同图层重叠多为同一图层内色块叠画，已在统计中扣减；跨图层重叠可能是分类口径差异或重复描图，需人工判断归属。"
    )
    lines.append("")

    lines.append("## 一、同图层重叠（已扣减）")
    lines.append("")
    items = same.get("items") or []
    if items:
        lines.append("| 图层 | 分类 | 保留句柄 | 扣减句柄 | 扣减面积(m²) |")
        lines.append("| --- | --- | --- | --- | ---: |")
        for item in items:
            lines.append(
                f"| {item.get('layer')} | {item.get('code') or '—'} | {item.get('keep')} | "
                f"{item.get('drop')} | {_m2(item.get('overlap_m2', 0))} |"
            )
    else:
        lines.append("> 未发现同图层重叠。")
    lines.append("")

    lines.append("## 二、跨图层重叠（未扣减，需人工确认）")
    lines.append("")
    items = cross.get("items") or []
    if items:
        lines.append(
            "| 图层A | 句柄A | 图层B | 句柄B | 重叠面积(m²) | 占较小地块(%) | 说明 |"
        )
        lines.append("| --- | --- | --- | --- | ---: | ---: | --- |")
        for item in items:
            notes = []
            if item.get("oversize"):
                notes.append("重叠≥较小地块面积（残片或重复描图）")
            if item.get("approximate"):
                notes.append("包围盒估算")
            ratio_pct = min(float(item.get("ratio") or 0.0), 1.0) * 100
            lines.append(
                f"| {item.get('layer_a')} | {item.get('handle_a')} | {item.get('layer_b')} | "
                f"{item.get('handle_b')} | {_m2(item.get('overlap_m2', 0))} | "
                f"{_pct(ratio_pct)} | {'；'.join(notes)} |"
            )
    else:
        lines.append("> 未发现跨图层重叠。")
    lines.append("")

    if items:
        per_layer: dict = {}
        for item in items:
            for layer_key, area_key in (("layer_a", "area_a_m2"), ("layer_b", "area_b_m2")):
                per_layer.setdefault(item.get(layer_key), {"count": 0, "area_m2": 0.0, "total_m2": 0.0})
                per_layer[item.get(layer_key)]["count"] += 1
                per_layer[item.get(layer_key)]["area_m2"] += item.get("overlap_m2", 0.0)
        lines.append("## 三、按图层汇总")
        lines.append("")
        lines.append("| 图层 | 参与重叠的地块数 | 重叠面积合计(m²) |")
        lines.append("| --- | ---: | ---: |")
        for layer_name, stat in sorted(per_layer.items(), key=lambda kv: kv[1]["area_m2"], reverse=True):
            lines.append(f"| {layer_name} | {stat['count']} | {_m2(stat['area_m2'])} |")
        lines.append("")
        lines.append("> 建议优先处理重叠面积大的图层，并核对是否为同一地块被重复描图或分层重复绘制。")
        lines.append("")

    lines.extend(_locator_section())
    return "\n".join(lines)


def _locator_section() -> List[str]:
    """回 CAD 用句柄定位色块的操作说明，随重叠明细报告一起交付。"""
    return [
        "## 附：回 CAD 用句柄定位色块",
        "",
        "上表的「句柄A / 句柄B」就是 AutoCAD 图元的 Handle，**可直接在命令行用 LISP 定位**，无需插件。"
        "本报告句柄取自同一份图纸导出的 DXF 副本，与 DWG 中一致；"
        "若之后做过 PURGE / AUDIT / RECOVER，或 WBLOCK 后插回别的图，句柄会变，需重新出报告。",
        "",
        "**① 选中并高亮单个图元**（把 `句柄` 换成表中的值，如 `169A0D`）：",
        "",
        "```lisp",
        '(sssetfirst nil (ssadd (handent "句柄")))',
        "```",
        "",
        "**② 高亮并居中放大**：",
        "",
        "```lisp",
        '(command "_.ZOOM" "_O" (handent "句柄") "") (sssetfirst nil (ssadd (handent "句柄")))',
        "```",
        "",
        "**③ 同时选中一对重叠地块**（两侧一起高亮，最易判断是否重复描图）：",
        "",
        "```lisp",
        '(sssetfirst nil (ssadd (handent "句柄A") (ssadd (handent "句柄B"))))',
        "```",
        "",
        "**④ 循环定位小工具**：存为 `GH.lsp` 用 `APPLOAD` 加载（或整段粘进命令行），"
        "之后输入 `GH` 回车，逐个粘贴句柄即可定位，回车结束：",
        "",
        "```lisp",
        "(defun c:GH ( / h e lay)",
        '  (while (/= "" (setq h (getstring T "\\n句柄(回车结束): ")))',
        "    (if (setq e (handent (strcase h)))",
        "      (progn",
        "        (sssetfirst nil (ssadd e))",
        '        (command "_.ZOOM" "_O" e "")',
        "        (setq lay (cdr (assoc 8 (entget e))))",
        '        (princ (strcat "\\n已定位 " (strcase h) "  图层：" lay))',
        "      )",
        '      (princ (strcat "\\n未找到句柄 " (strcase h)))',
        "    )",
        "  )",
        "  (princ)",
        ")",
        "```",
        "",
        "**注意事项**",
        "",
        "- 句柄所在图层若被关闭或冻结（含布局视口冻结）会看不到，定位前先打开图层；",
        "- 填充只显示轮廓时把 `FILLMODE` 设为 1；",
        "- 需查属性可在命令行执行 `(entget (handent \"句柄\"))`，DXF 组码 8 为图层、10/20 为坐标；",
        "- 批量核对可直接用 `QSELECT` 选该图层全部填充，再按句柄逐个排除。",
        "",
    ]
