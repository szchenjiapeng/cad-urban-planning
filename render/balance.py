"""规划土地利用平衡表（范式表）。

新标准表式：
    一级类 | 二级类 | 三级类 | 用地性质名称 | 用地规模(公顷) | 占建设用地比例(%) | 占总用地比例(%)

规则：
- 一级类行给两个占比；二级、三级类只给「占建设用地比例」，「占总用地比例」留空；
- 「占建设用地比例」只对建设用地大类（2023 指南为 06–16，可用 --construction-codes 覆盖）
  填写；非建设用地（01–05、17–24）留空——它们不是建设用地，填了会读错分母；
- 先排 06–16 各一级类（带下级），随后「建设用地面积」小计；
- 非建设用地（01–05、17–24）按代码顺序平铺为一级类，不设「农林用地」分组，
  除非配置里显式给了 groups；
- 只列图纸实际出现的类目，零面积类目不占行。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Set

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from core.models import Summary, SummaryRow  # noqa: E402

BLANK = ""


@dataclass
class BalanceSpec:
    construction: Set[str] = field(default_factory=set)
    construction_label: str = "建设用地面积"
    total_label: str = "总用地面积"
    groups: List[dict] = field(default_factory=list)
    per_capita: bool = False
    ratio_label: str = "占建设用地比例(%)"
    three_code_columns: bool = True

    @classmethod
    def from_config(
        cls,
        config: Optional[dict],
        standard: str = "",
        population: float = 0.0,
    ) -> "BalanceSpec":
        cfg = dict(config or {})
        gb = standard == "gb50137"
        construction = {str(c) for c in cfg.get("construction", [])}
        if not construction:
            construction = (
                {"R", "A", "B", "M", "W", "S", "U", "G"} if gb else {f"{i:02d}" for i in range(6, 17)}
            )
        return cls(
            construction=construction,
            construction_label=str(
                cfg.get("construction_label") or ("城市建设用地" if gb else "建设用地面积")
            ),
            total_label=str(cfg.get("total_label") or "总用地面积"),
            groups=list(cfg.get("groups") or []),
            per_capita=bool(population) and gb,
            ratio_label="占城市建设用地比例(%)" if gb else "占建设用地比例(%)",
            three_code_columns=not gb,
        )

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "BalanceSpec":
        """从 summary.balance_spec 还原口径，供渲染层复用。"""
        cfg = dict(data or {})
        return cls(
            construction={str(c) for c in (cfg.get("construction") or [])},
            construction_label=str(cfg.get("construction_label") or "建设用地面积"),
            total_label=str(cfg.get("total_label") or "总用地面积"),
            groups=list(cfg.get("groups") or []),
            per_capita=bool(cfg.get("per_capita")),
            ratio_label=str(cfg.get("ratio_label") or "占建设用地比例(%)"),
            three_code_columns=bool(cfg.get("three_code_columns", True)),
        )

    def to_dict(self) -> dict:
        return {
            "construction": sorted(self.construction),
            "construction_label": self.construction_label,
            "total_label": self.total_label,
            "groups": list(self.groups),
            "per_capita": self.per_capita,
            "ratio_label": self.ratio_label,
            "three_code_columns": self.three_code_columns,
        }


def _ha(value: float) -> str:
    return f"{value / 1e4:,.2f}"


def _pct(value: float) -> str:
    return f"{value:.2f}"


def _ratio(area: float, base: float) -> str:
    return _pct(area / base * 100.0) if base else BLANK


def _join(cells: Sequence[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def render_balance_section(summary: Summary, spec: BalanceSpec) -> List[str]:
    """生成规划土地利用平衡表的 Markdown 表格行。"""
    unresolved = sum(row.area_m2 for row in summary.unclassified)
    total_area = summary.meta.plan_area_m2 or (summary.total_area_m2 + unresolved)
    level1 = {row.code: row for row in summary.level1_rows}
    c_total = sum(level1[c].area_m2 for c in spec.construction if c in level1)

    lines: List[str] = []
    if spec.three_code_columns:
        lines.append(
            _join(["一级类", "二级类", "三级类", "用地性质名称", "用地规模(公顷)",
                   spec.ratio_label, "占总用地比例(%)"])
        )
        lines.append(_join(["---", "---", "---", "---", "---:", "---:", "---:"]))
    else:
        head = ["用地代码", "用地名称", "用地面积(公顷)", spec.ratio_label]
        algn = ["---", "---", "---:", "---:"]
        if spec.per_capita:
            head.append("人均建设用地(m²/人)")
            algn.append("---:")
        lines.append(_join(head))
        lines.append(_join(algn))

    def emit(root: SummaryRow) -> None:
        """输出一个一级类及其二级、三级下级。

        「占建设用地比例」只对建设用地大类（2023 指南为 06–16）填写；
        非建设用地（01–05、17–24）本身不是建设用地，该列留空，
        避免把「占建设用地」读成「占全部用地」。
        """
        in_construction = root.code in spec.construction

        def ratio_cell(area: float, bold: bool) -> str:
            if not in_construction:
                return BLANK
            text = _ratio(area, c_total)
            return f"**{text}**" if bold else text

        if spec.three_code_columns:
            lines.append(_join([f"**{root.code}**", BLANK, BLANK, f"**{root.name}**",
                                f"**{_ha(root.area_m2)}**",
                                ratio_cell(root.area_m2, True),
                                f"**{_ratio(root.area_m2, total_area)}**"]))
        else:
            cells = [f"**{root.code}**", f"**{root.name}**", f"**{_ha(root.area_m2)}**",
                     ratio_cell(root.area_m2, True)]
            if spec.per_capita:
                cells.append(f"**{_pct(root.per_capita)}**" if root.per_capita else BLANK)
            lines.append(_join(cells))

        for child in [r for r in summary.level2_rows if r.parent == root.code]:
            if spec.three_code_columns:
                lines.append(_join([BLANK, child.code, BLANK, child.name, _ha(child.area_m2),
                                    ratio_cell(child.area_m2, False), BLANK]))
            else:
                cells = [child.code, child.name, _ha(child.area_m2),
                         ratio_cell(child.area_m2, False)]
                if spec.per_capita:
                    cells.append(_pct(child.per_capita) if child.per_capita else BLANK)
                lines.append(_join(cells))

            for grand in [r for r in summary.level3_rows if r.parent == child.code]:
                if spec.three_code_columns:
                    lines.append(_join([BLANK, BLANK, grand.code, grand.name, _ha(grand.area_m2),
                                        ratio_cell(grand.area_m2, False), BLANK]))
                else:
                    cells = [grand.code, grand.name, _ha(grand.area_m2),
                             ratio_cell(grand.area_m2, False)]
                    if spec.per_capita:
                        cells.append(_pct(grand.per_capita) if grand.per_capita else BLANK)
                    lines.append(_join(cells))

    def subtotal(label: str, area: float, full_ratio: bool) -> None:
        if spec.three_code_columns:
            lines.append(_join([BLANK, BLANK, BLANK, f"**{label}**", f"**{_ha(area)}**",
                                "**100.00**" if full_ratio else BLANK,
                                f"**{_ratio(area, total_area)}**"]))
        else:
            cells = [BLANK, f"**{label}**", f"**{_ha(area)}**", "**100.00**" if full_ratio else BLANK]
            if spec.per_capita:
                cells.append(BLANK)
            lines.append(_join(cells))

    construction_rows = [r for r in summary.level1_rows if r.code in spec.construction]
    for row in construction_rows:
        emit(row)
    subtotal(spec.construction_label, c_total, full_ratio=True)

    rest = [r for r in summary.level1_rows if r.code not in spec.construction]
    covered: Set[str] = set()
    if spec.groups:
        for group in spec.groups:
            codes = [str(c) for c in group.get("codes", [])]
            covered.update(codes)
            members = [r for r in rest if r.code in codes]
            if not members:
                continue
            subtotal(str(group.get("label") or "非建设用地"), sum(r.area_m2 for r in members), False)
            for row in members:
                emit(row)
    for row in rest:
        if row.code not in covered:
            emit(row)

    if spec.three_code_columns:
        lines.append(_join([BLANK, BLANK, BLANK, f"**{spec.total_label}**",
                            f"**{_ha(total_area)}**", BLANK, "**100.00**"]))
    else:
        cells = [BLANK, f"**{spec.total_label}**", f"**{_ha(total_area)}**", BLANK]
        if spec.per_capita:
            cells.append(BLANK)
        lines.append(_join(cells))
    return lines
