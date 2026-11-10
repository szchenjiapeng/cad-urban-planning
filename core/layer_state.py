"""图层可见性状态：区分模型空间开关、视口冻结（VP Freeze）与“全部统计”。

关键事实：DXF 图层表中的 On/Off、Freeze 是**全局**的；布局差异主要来自视口冻结。
因此常见“模型里看得到、布局视口里被冻结”的图层。这类差异必须让用户确认口径，
脚本不替用户决定。

使用约定：
- mode=model：只看模型空间开/关与冻结；
- mode=layout：在模型可见基础上，再排除当前布局视口冻结的图层；
- mode=all：全部统计（用于复核“关掉的图层里到底有多少量”）；
- mode=auto：只做差异检测，检测到差异时由上层中断并询问用户。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Sequence

MODE_MODEL = "model"
MODE_LAYOUT = "layout"
MODE_ALL = "all"
MODE_AUTO = "auto"
VALID_MODES = (MODE_AUTO, MODE_MODEL, MODE_LAYOUT, MODE_ALL)


@dataclass
class LayerState:
    name: str
    on: bool = True
    frozen: bool = False
    locked: bool = False
    layout_frozen: bool = False

    def model_visible(self) -> bool:
        return self.on and not self.frozen

    def layout_visible(self) -> bool:
        return self.model_visible() and not self.layout_frozen


@dataclass
class LayerDiff:
    layer: str
    kind: str  # model_on_layout_frozen / model_off_layout_on
    model_state: str
    layout_state: str
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "layer": self.layer,
            "kind": self.kind,
            "model_state": self.model_state,
            "layout_state": self.layout_state,
            "description": self.description,
        }


@dataclass
class LayerStateReport:
    mode: str = MODE_MODEL
    layout: str = ""
    states: Dict[str, LayerState] = field(default_factory=dict)
    diffs: List[LayerDiff] = field(default_factory=list)
    skipped_layers: List[str] = field(default_factory=list)
    note: str = ""

    def is_visible(self, layer: str) -> bool:
        if self.mode == MODE_ALL:
            return True
        state = self.states.get(layer)
        if state is None:  # 未登记的图层按可见处理，避免静默丢弃
            return True
        if self.mode == MODE_LAYOUT:
            return state.layout_visible()
        return state.model_visible()

    def skip(self, layer: str) -> None:
        if layer not in self.skipped_layers:
            self.skipped_layers.append(layer)

    def has_difference(self) -> bool:
        return bool(self.diffs)

    def hidden_layers(self) -> List[str]:
        return sorted(name for name, state in self.states.items() if not self.is_visible(name))

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "layout": self.layout,
            "diffs": [item.to_dict() for item in self.diffs],
            "skipped_layers": self.skipped_layers,
            "hidden_layers": self.hidden_layers(),
            "note": self.note,
        }


def build_report(
    model_states: Dict[str, dict],
    layout_frozen: Sequence[str],
    mode: str,
    layout: str = "",
) -> LayerStateReport:
    """把后端读到的原始状态整理成报告；mode=auto 时只标记差异，不决定口径。"""
    if mode not in VALID_MODES:
        mode = MODE_AUTO
    frozen_set = {str(name) for name in layout_frozen}
    report = LayerStateReport(mode=mode, layout=layout)

    for name, raw in model_states.items():
        state = LayerState(
            name=name,
            on=bool(raw.get("on", True)),
            frozen=bool(raw.get("frozen", False)),
            locked=bool(raw.get("locked", False)),
            layout_frozen=name in frozen_set,
        )
        report.states[name] = state
        if state.model_visible() and state.layout_frozen:
            report.diffs.append(
                LayerDiff(
                    layer=name,
                    kind="model_on_layout_frozen",
                    model_state="显示",
                    layout_state="冻结",
                    description="模型空间可见，但当前布局视口中被冻结（VP Freeze）",
                )
            )
        elif not state.model_visible() and state.layout_visible():
            report.diffs.append(
                LayerDiff(
                    layer=name,
                    kind="model_off_layout_on",
                    model_state="关闭/冻结",
                    layout_state="显示",
                    description="模型空间不可见，但布局视口可见，多为图层状态管理器残留",
                )
            )

    if mode == MODE_AUTO:
        report.note = (
            f"检测到 {len(report.diffs)} 个图层的模型/布局可见性不一致，需先确认统计口径"
            if report.diffs
            else "模型与布局图层状态一致，按模型空间口径统计"
        )
    elif mode == MODE_LAYOUT:
        report.note = f"按布局“{layout or '默认布局'}”的视口可见性统计"
    elif mode == MODE_MODEL:
        report.note = "按模型空间图层开关统计"
    else:
        report.note = "统计全部图层（含关闭/冻结图层）"
    return report


def confirmation_item(report: LayerStateReport, layout_label: str = "") -> dict:
    """生成交给会话向用户提问的结构化确认项。"""
    options = [
        {
            "label": "按模型空间口径统计（模型可见即统计）",
            "args": ["--layer-state", "model"],
            "description": "关闭或冻结的图层一律不计入",
        },
        {
            "label": "按布局视口口径统计（布局冻结的不统计）",
            "args": ["--layer-state", "layout"],
            "description": "以当前布局里实际看到的为准",
        },
        {
            "label": "全部统计（含关闭图层，仅用于复核）",
            "args": ["--layer-state", "all"],
            "description": "把草稿图层也算进来，面积会偏大",
        },
    ]
    layers = "、".join(item.layer for item in report.diffs[:10])
    more = f" 等 {len(report.diffs)} 个图层" if len(report.diffs) > 10 else ""
    return {
        "type": "layer_state",
        "question": (
            f"检测到模型空间与布局{('“' + layout_label + '”') if layout_label else ''}的图层可见性不一致："
            f"{layers}{more}。面积按哪个口径统计？"
        ),
        "options": options,
    }
