"""CAD 数据接入抽象层：上层几何/分类/渲染只依赖本接口，不直接接触 COM 或 DXF。"""

from __future__ import annotations

import abc
import re
import sys
from pathlib import Path
from typing import Iterator, List, Optional, Sequence

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from core.models import HatchRecord, PolylineRecord, TextRecord  # noqa: E402


def layer_matches_patterns(layer: str, patterns: Optional[Sequence[str]]) -> bool:
    """按 CAD 通配符（* / ?）判断图层名是否命中；patterns 为空视为不过滤。

    只用于“扫描阶段预过滤”以提速，命中范围必须覆盖后续 Python 侧的精确匹配，
    否则会漏掉图元（调用方宁可少传 pattern，也不要传过窄的 pattern）。
    """
    if not patterns:
        return True
    name = (layer or "").strip().lower()
    if not name:  # 图层名缺失时不参与任何通配符命中，避免把脏数据当成范围线
        return False
    for pattern in patterns:
        text = str(pattern or "").strip().lower()
        if not text:
            continue
        regex = re.escape(text).replace(r"\*", ".*").replace(r"\?", ".")
        if re.fullmatch(regex, name):
            return True
    return False


_FILTER_SPECIAL = set("*?[]#@.~,`")


def escape_filter_literal(name: str) -> str:
    """把图层名转成 AutoCAD 选择集过滤器里的字面量（用反引号转义通配符）。

    图层名里带 `.`、`#`、`-`、`[` 等字符时，直接塞进 code 8 过滤器会被当通配符，
    导致「精确指定图层」反而选不到对象。
    """
    text = str(name or "")
    if not any(ch in _FILTER_SPECIAL for ch in text):
        return text
    return "".join(("`" + ch) if ch in _FILTER_SPECIAL else ch for ch in text)


class CadBackend(abc.ABC):
    """数据接入协议。

    新增 CAD 产品支持（中望、浩辰等）时，只需实现本类的抽象方法。
    """

    channel: str = "base"

    @abc.abstractmethod
    def describe(self) -> str:
        """返回人类可读的数据源描述，用于报告头部。"""

    @abc.abstractmethod
    def get_insunits(self) -> int:
        """返回图纸单位代码（DXF $INSUNITS 语义）。"""

    @abc.abstractmethod
    def iter_polylines(
        self,
        layers: Optional[Sequence[str]] = None,
        layer_patterns: Optional[Sequence[str]] = None,
    ) -> Iterator[PolylineRecord]:
        """迭代多段线形式的用地界线。

        layers：精确图层名（Python 侧过滤，为空不过滤）；
        layer_patterns：CAD 通配符（如 ``*规划范围*``），供通道在选择集阶段预过滤以提速，
        为空不过滤。图元集合必须与 ``layers`` 语义一致，缺失时退化为不过滤。
        """

    def iter_hatches(
        self,
        layers: Optional[Sequence[str]] = None,
        layer_patterns: Optional[Sequence[str]] = None,
    ) -> Iterator[HatchRecord]:
        """迭代填充对象。默认无填充的通道返回空。layer_patterns 语义同上。"""
        return iter(())

    def get_polyline(self, handle: str) -> Optional[PolylineRecord]:
        return None

    def iter_texts(
        self,
        layers: Optional[Sequence[str]] = None,
        layer_patterns: Optional[Sequence[str]] = None,
    ) -> Iterator[TextRecord]:
        """迭代文字（TEXT/MTEXT）。默认无文字的通道返回空，语义同 iter_polylines。"""
        return iter(())

    def iter_lines(
        self,
        layers: Optional[Sequence[str]] = None,
        layer_patterns: Optional[Sequence[str]] = None,
    ) -> Iterator[PolylineRecord]:
        """迭代直线（LINE），统一包装成两点 PolylineRecord，供五线/红线读取。

        默认返回空，不影响只关心多段线的既有子技能。
        """
        return iter(())

    def count_entities_by_layer(
        self,
        layers: Sequence[str],
        entity_types: str = "LWPOLYLINE,POLYLINE",
    ) -> dict:
        """按图层统计指定类型图元的**数量**（只计数，不读属性）。

        用于「列图层候选」时判断哪些图层里真的有可当范围线的多段线：
        AutoCAD 侧只做选择集计数（一次 Select 拿 Count），不需要逐图元往返。
        返回 {图层名: {"polylines": 多段线数, "entities": 该图层对象总数, "error": 失败原因}}；
        不支持的通道返回空字典（上层按“数量未知”处理）。
        """
        return {}

    def export_handles_to_dxf(self, handles: Sequence[str], path: str) -> bool:
        """把指定句柄的对象复制到新文件并导出 DXF，供几何兜底解析。"""
        return False

    def export_all_to_dxf(self, path: str) -> bool:
        """整图导出 DXF（不改变用户文档），用于块展开与几何兜底。"""
        return False

    def list_layers(self) -> List[str]:
        return []

    # ---------------------------------------------------------------- 图层状态
    def get_layer_states(self) -> dict:
        """返回 {图层名: {on, frozen, locked}}；不支持的通道返回空字典。"""
        return {}

    def list_layouts(self) -> List[str]:
        """返回图纸空间布局名（不含 Model）。"""
        return []

    def get_layout_frozen_layers(self, layout: Optional[str] = None) -> List[str]:
        """返回指定布局视口中被冻结的图层（VP Freeze）。"""
        return []

    # ---------------------------------------------------------------- 块
    def has_block_references(self) -> bool:
        """是否包含块参照；用于决定是否需要导出 DXF 后展开。"""
        return False

    def ensure_layer(
        self,
        layer: str,
        color: Optional[int] = None,
        linetype: Optional[str] = None,
    ) -> None:
        """确保图层存在；color 为 ACI 索引，linetype 为线型名（不存在时按 CONTINUOUS）。"""
        raise NotImplementedError(f"{self.channel} 通道不支持创建图层")

    def set_layer_props(
        self,
        layer: str,
        color: Optional[int] = None,
        linetype: Optional[str] = None,
    ) -> None:
        """修改既有图层属性；图层不存在时先创建。"""
        raise NotImplementedError(f"{self.channel} 通道不支持修改图层属性")

    def ensure_text_style(
        self,
        name: str,
        font: Optional[str] = None,
        bigfont: Optional[str] = None,
    ) -> bool:
        """建文字样式并设置字体；不支持时返回 False（调用方退回默认样式）。"""
        return False

    def add_text(
        self,
        layer: str,
        point: tuple,
        text: str,
        height: float = 2.5,
        style: Optional[str] = None,
        width_factor: Optional[float] = None,
    ) -> None:
        raise NotImplementedError(f"{self.channel} 通道不支持写回文字")

    def add_polyline(self, layer: str, points: Sequence[tuple], closed: bool = True) -> None:
        raise NotImplementedError(f"{self.channel} 通道不支持写回多段线")

    def add_extruded_solid(
        self, layer: str, points: Sequence[tuple], height: float, base_z: float = 0.0
    ) -> bool:
        """按闭合轮廓批量拉伸成 3DSOLID；通道不支持时返回 False，由上层降级为体块网格。"""
        return False

    def add_face(
        self,
        layer: str,
        points: Sequence[tuple],
        base_z: float = 0.0,
        top_z: Optional[float] = None,
    ) -> bool:
        """按闭合轮廓生成体块面（侧面 + 顶面 3DFACE），用于无 CAD 时的体块示意。"""
        return False

    def touch_watchdog(self) -> None:
        """批量写回/拉伸时续命看门狗；没有看门狗的通道为空操作。"""
        watchdog = getattr(self, "_watchdog", None)
        if watchdog is None:
            return
        try:
            watchdog.touch()
        except Exception:
            pass

    def set_color(self, handle: str, aci: int) -> None:
        raise NotImplementedError(f"{self.channel} 通道不支持写回颜色")

    def set_entity_layer(self, handle: str, layer: str) -> bool:
        """把指定句柄的图元移到目标图层；不支持时返回 False，由调用方报异常。"""
        raise NotImplementedError(f"{self.channel} 通道不支持移动图元到新图层")

    def save_copy(self, path: str, quiet: bool = False) -> None:
        """另存副本（原图不动）；quiet=True 时不打印中间件提示。"""
        raise NotImplementedError(f"{self.channel} 通道不支持另存")

    def source_signature(self) -> Optional[str]:
        """源图指纹（通道 + 路径 + 修改时间 + 大小），用于复用扫描缓存。

        拿不到（图纸未落盘、有未保存改动）时返回 None，调用方就不写缓存。
        """
        return None

    def close(self) -> None:
        return None
