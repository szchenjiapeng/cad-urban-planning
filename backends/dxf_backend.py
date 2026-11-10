"""DXF 文件通道：基于 ezdxf，纯离线解析，不依赖本机安装 AutoCAD。

除常规图元读取外，还支持：
- 图层开/关/冻结状态（全局 LAYER 表）与布局视口冻结（VP Freeze）；
- 块参照在内存中递归展开（不修改文件）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from backends.base import CadBackend, layer_matches_patterns  # noqa: E402
from core.blocks import BlockStats, expand_insert  # noqa: E402
from core.models import HatchRecord, PolylineRecord, TextRecord  # noqa: E402


def _layer_flag(layer, name: str, default: bool = False) -> bool:
    """ezdxf 的 is_on/is_frozen/is_locked 在不同版本里可能是方法也可能是属性。"""
    try:
        value = getattr(layer, name)
    except Exception:
        return default
    try:
        if callable(value):
            value = value()
        return bool(value)
    except Exception:
        return default


class DxfBackend(CadBackend):
    channel = "dxf"

    def __init__(self, file_path: str, expand_blocks: bool = False):
        try:
            import ezdxf  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("缺少依赖 ezdxf，请先执行：python -m pip install ezdxf") from exc

        path = Path(file_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"文件不存在：{path}")
        if path.suffix.lower() == ".dwg":
            raise RuntimeError(
                "ezdxf 不能直接读取 DWG。请在 CAD 中另存为 DXF（2013/2018 版本），"
                "或用 ODA File Converter 批量转换后再运行。"
            )

        import ezdxf

        self.path = path
        self.doc = ezdxf.readfile(str(path))
        self.msp = self.doc.modelspace()
        self.expand_blocks = expand_blocks
        self.block_stats = BlockStats()
        self._handle_index = None
        self._layout_index = None

    # ---------------------------------------------------------------- 元信息
    def describe(self) -> str:
        return f"DXF 文件解析：{self.path.name}"

    def source_signature(self) -> Optional[str]:
        try:
            stat = self.path.stat()
        except Exception:
            return None
        return f"dxf|{self.path}|{stat.st_mtime_ns}|{stat.st_size}"

    def get_insunits(self) -> int:
        try:
            return int(self.doc.header.get("$INSUNITS", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def list_layers(self) -> List[str]:
        return sorted(layer.dxf.name for layer in self.doc.layers)

    # ---------------------------------------------------------------- 图层状态
    def get_layer_states(self) -> Dict[str, dict]:
        states: Dict[str, dict] = {}
        for layer in self.doc.layers:
            name = str(layer.dxf.name)
            states[name] = {
                "on": _layer_flag(layer, "is_on", True),
                "frozen": _layer_flag(layer, "is_frozen", False),
                "locked": _layer_flag(layer, "is_locked", False),
            }
        return states

    def list_layouts(self) -> List[str]:
        try:
            names = list(self.doc.layouts.names_in_taborder())
        except Exception:
            names = [str(layout.name) for layout in self.doc.layouts]
        return [name for name in names if name.lower() != "model"]

    def get_layout_frozen_layers(self, layout: Optional[str] = None) -> List[str]:
        names = self.list_layouts()
        target = layout or (names[-1] if names else None)
        if not target:
            return []
        try:
            layout_obj = self.doc.layouts.get(target)
        except Exception:
            return []
        frozen = set()
        try:
            for viewport in layout_obj.query("VIEWPORT"):
                try:
                    for name in viewport.frozen_layers:
                        frozen.add(str(name))
                except Exception:
                    continue
        except Exception:
            pass
        return sorted(frozen)

    def layout_name(self, layout: Optional[str] = None) -> str:
        names = self.list_layouts()
        return layout or (names[-1] if names else "")

    # ---------------------------------------------------------------- 图元
    def _iter_entities(self, types: Sequence[str]) -> Iterator:
        wanted = set(types)
        for entity in self.msp:
            dxftype = entity.dxftype()
            if dxftype in wanted:
                yield entity
            elif dxftype == "INSERT" and self.expand_blocks:
                try:
                    expanded = expand_insert(entity, stats=self.block_stats)
                except Exception:
                    continue
                for sub in expanded:
                    if sub.dxftype() in wanted:
                        yield sub

    def count_entities_by_layer(
        self,
        layers: Sequence[str],
        entity_types: str = "LWPOLYLINE,POLYLINE",
    ) -> dict:
        """按图层只数数量：扫一遍模拟空间，不解析几何。"""
        wanted = {str(name).strip().lower(): str(name) for name in layers if str(name).strip()}
        counts = {name: {"polylines": 0, "entities": 0, "error": ""} for name in wanted.values()}
        if not wanted:
            return counts
        types = tuple(item.strip().upper() for item in str(entity_types).split(",") if item.strip())
        try:
            for entity in self.msp:
                try:
                    layer = str(entity.dxf.layer).strip().lower()
                except Exception:
                    continue
                name = wanted.get(layer)
                if name is None:
                    continue
                counts[name]["entities"] += 1
                if entity.dxftype() in types:
                    counts[name]["polylines"] += 1
        except Exception as exc:
            for record in counts.values():
                record["error"] = f"{type(exc).__name__}: {exc}"
            return counts
        return counts

    def iter_polylines(
        self,
        layers: Optional[Sequence[str]] = None,
        layer_patterns: Optional[Sequence[str]] = None,
    ) -> Iterator[PolylineRecord]:
        layer_set = set(layers) if layers else None
        for entity in self._iter_entities(("LWPOLYLINE", "POLYLINE")):
            dxftype = entity.dxftype()
            try:
                layer_name = str(entity.dxf.layer)
            except Exception:
                continue
            if layer_set is not None and layer_name not in layer_set:
                continue
            if layer_patterns and not layer_matches_patterns(layer_name, layer_patterns):
                continue
            try:
                if dxftype == "LWPOLYLINE":
                    record = self._read_lwpolyline(entity)
                else:
                    record = self._read_polyline(entity)
            except Exception:  # 单个图元异常不应中断整图
                yield PolylineRecord(
                    handle=str(getattr(entity.dxf, "handle", "") or ""),
                    layer=layer_name,
                    entity_type=dxftype,
                )
                continue
            if record is not None:
                yield record

    def iter_hatches(
        self,
        layers: Optional[Sequence[str]] = None,
        layer_patterns: Optional[Sequence[str]] = None,
    ) -> Iterator[HatchRecord]:
        """填充对象：解析全部边界环，净面积由 core.hatch 按奇偶嵌套规则计算。"""
        from core.hatch import rings_variants

        layer_set = set(layers) if layers else None
        for entity in self._iter_entities(("HATCH",)):
            try:
                layer_name = str(entity.dxf.layer)
            except Exception:
                continue
            if layer_set is not None and layer_name not in layer_set:
                continue
            if layer_patterns and not layer_matches_patterns(layer_name, layer_patterns):
                continue
            handle = str(getattr(entity.dxf, "handle", "") or "")
            try:
                rings, rings_alt = rings_variants(entity)
            except Exception as exc:
                yield HatchRecord(handle=handle, layer=layer_name, area_error=f"边界解析失败：{exc}")
                continue
            if not rings:
                yield HatchRecord(handle=handle, layer=layer_name, area_error="未解析出闭合边界路径")
                continue
            yield HatchRecord(handle=handle, layer=layer_name, rings=rings, rings_alt=rings_alt)

    def _read_lwpolyline(self, entity) -> Optional[PolylineRecord]:
        points = list(entity.get_points("xyb"))
        if len(points) < 3:
            return None
        return PolylineRecord(
            handle=str(entity.dxf.handle),
            layer=str(entity.dxf.layer),
            entity_type="LWPOLYLINE",
            vertices=[(float(p[0]), float(p[1])) for p in points],
            bulges=[float(p[2] or 0.0) for p in points],
            closed_flag=bool(entity.closed),
        )

    def _read_polyline(self, entity) -> Optional[PolylineRecord]:
        if not entity.is_2d_polyline:
            return None
        vertices: List[tuple] = []
        bulges: List[float] = []
        for vertex in entity.vertices:
            loc = vertex.dxf.location
            vertices.append((float(loc.x), float(loc.y)))
            bulges.append(float(getattr(vertex.dxf, "bulge", 0.0) or 0.0))
        if len(vertices) < 3:
            return None
        return PolylineRecord(
            handle=str(entity.dxf.handle),
            layer=str(entity.dxf.layer),
            entity_type="POLYLINE",
            vertices=vertices,
            bulges=bulges,
            closed_flag=bool(entity.is_closed),
        )

    def iter_texts(
        self,
        layers: Optional[Sequence[str]] = None,
        layer_patterns: Optional[Sequence[str]] = None,
    ) -> Iterator[TextRecord]:
        """DXF 文字：TEXT 与 MTEXT 统一成 TextRecord，MTEXT 取纯文本。"""
        layer_set = set(layers) if layers else None
        for entity in self._iter_entities(("TEXT", "MTEXT")):
            dxftype = entity.dxftype()
            try:
                layer_name = str(entity.dxf.layer)
            except Exception:
                continue
            if layer_set is not None and layer_name not in layer_set:
                continue
            if layer_patterns and not layer_matches_patterns(layer_name, layer_patterns):
                continue
            handle = str(getattr(entity.dxf, "handle", "") or "")
            try:
                if dxftype == "MTEXT":
                    text = entity.plain_text()
                    point = entity.dxf.insert
                    height = float(getattr(entity.dxf, "char_height", 0.0) or 0.0)
                else:
                    text = str(entity.dxf.text or "")
                    point = entity.dxf.insert
                    height = float(getattr(entity.dxf, "height", 0.0) or 0.0)
                rotation = float(getattr(entity.dxf, "rotation", 0.0) or 0.0)
            except Exception:
                continue
            yield TextRecord(
                handle=handle,
                layer=layer_name,
                text=str(text or "").strip(),
                point=(float(point[0]), float(point[1])),
                height=height,
                rotation=rotation,
                entity_type=dxftype,
            )

    def iter_lines(
        self,
        layers: Optional[Sequence[str]] = None,
        layer_patterns: Optional[Sequence[str]] = None,
    ) -> Iterator[PolylineRecord]:
        """DXF 直线：包装成两点 PolylineRecord（closed_flag=False）。"""
        layer_set = set(layers) if layers else None
        for entity in self._iter_entities(("LINE",)):
            try:
                layer_name = str(entity.dxf.layer)
            except Exception:
                continue
            if layer_set is not None and layer_name not in layer_set:
                continue
            if layer_patterns and not layer_matches_patterns(layer_name, layer_patterns):
                continue
            try:
                start, end = entity.dxf.start, entity.dxf.end
                vertices = [(float(start[0]), float(start[1])), (float(end[0]), float(end[1]))]
            except Exception:
                continue
            yield PolylineRecord(
                handle=str(getattr(entity.dxf, "handle", "") or ""),
                layer=layer_name,
                entity_type="LINE",
                vertices=vertices,
                closed_flag=False,
            )

    # ---------------------------------------------------------------- 写回
    def _ensure_layer(self, layer: str) -> None:
        if layer not in self.doc.layers:
            self.doc.layers.add(layer)

    def ensure_layer(
        self,
        layer: str,
        color: Optional[int] = None,
        linetype: Optional[str] = None,
    ) -> None:
        """建图层；线型不存在时退回 CONTINUOUS，不因缺线型中断整个流程。"""
        if layer in self.doc.layers:
            self.set_layer_props(layer, color=color, linetype=linetype)
            return
        attrs = {}
        if color is not None:
            attrs["color"] = int(color)
        if linetype:
            attrs["linetype"] = self._safe_linetype(str(linetype), create=True)
        try:
            self.doc.layers.add(layer, **attrs)
        except Exception:
            attrs.pop("linetype", None)
            self.doc.layers.add(layer, **attrs)

    def set_layer_props(
        self,
        layer: str,
        color: Optional[int] = None,
        linetype: Optional[str] = None,
    ) -> None:
        if layer not in self.doc.layers:
            self.ensure_layer(layer, color=color, linetype=linetype)
            return
        obj = self.doc.layers.get(layer)
        if color is not None:
            try:
                obj.dxf.color = int(color)
            except Exception:
                pass
        if linetype:
            try:
                obj.dxf.linetype = self._safe_linetype(str(linetype), create=True)
            except Exception:
                pass

    def _safe_linetype(self, name: str, create: bool = False) -> str:
        """返回可用的线型名：图纸里没有该线型时按 CONTINUOUS 处理。"""
        key = (name or "").strip()
        if not key:
            return "CONTINUOUS"
        try:
            if key in self.doc.linetypes:
                return key
            if create:
                # 常见线型由 ezdxf 标准库提供；创建失败则退回 CONTINUOUS
                from ezdxf.tools import standards

                pattern = getattr(standards, "LINETYPES", {}).get(key.upper())
                if pattern is not None:
                    self.doc.linetypes.add(key, pattern=pattern.pattern, description=pattern.description)
                    return key
        except Exception:
            pass
        return "CONTINUOUS"

    def ensure_text_style(self, name: str, font: Optional[str] = None, bigfont: Optional[str] = None) -> bool:
        """建文字样式并设置字体；字体为空时沿用图纸默认。

        DXF 里 TTF 用 `font`（如 simfang.ttf），SHX 大字体用 `bigfont`（如 gbcbig.shx）。
        """
        key = (name or "").strip()
        if not key:
            return False
        try:
            if key in self.doc.styles:
                style = self.doc.styles.get(key)
                if font:
                    style.dxf.font = str(font)
            else:
                # ezdxf 的 TextstyleTable.add() 要求 font 为必填关键字参数
                style = self.doc.styles.add(key, font=str(font or "txt"))
            if bigfont:
                style.dxf.bigfont = str(bigfont)
            return True
        except Exception:
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
        self._ensure_layer(layer)
        attrs = {"layer": layer, "insert": (point[0], point[1])}
        if style:
            self.ensure_text_style(style)
            attrs["style"] = style
        if width_factor:
            attrs["width"] = float(width_factor)
        self.msp.add_text(text, height=height, dxfattribs=attrs)

    def add_polyline(self, layer: str, points: Sequence[tuple], closed: bool = True) -> None:
        self._ensure_layer(layer)
        self.msp.add_lwpolyline(list(points), close=closed, dxfattribs={"layer": layer})

    def add_face(
        self,
        layer: str,
        points: Sequence[tuple],
        base_z: float = 0.0,
        top_z: Optional[float] = None,
    ) -> bool:
        """体块示意：侧面逐边 3DFACE + 顶面三角化 3DFACE（非 ACIS 实体）。"""
        cleaned: List[tuple] = []
        for point in points or ():
            try:
                cleaned.append((float(point[0]), float(point[1])))
            except Exception:
                continue
        if len(cleaned) < 3:
            return False
        base = float(base_z)
        top = float(base_z if top_z is None else top_z)
        self._ensure_layer(layer)
        count = len(cleaned)
        for index in range(count):
            first = cleaned[index]
            second = cleaned[(index + 1) % count]
            self.msp.add_3dface(
                [
                    (first[0], first[1], base),
                    (second[0], second[1], base),
                    (second[0], second[1], top),
                    (first[0], first[1], top),
                ],
                dxfattribs={"layer": layer},
            )
        for triangle in self._top_faces(cleaned):
            self.msp.add_3dface(
                [(x, y, top) for x, y in triangle],
                dxfattribs={"layer": layer},
            )
        return True

    @staticmethod
    def _top_faces(points: Sequence[tuple]) -> List[List[tuple]]:
        """顶面三角化：shapely Delaunay 后只保留面内三角形；失败回退质心扇形。"""
        try:
            from shapely.geometry import Polygon
            from shapely.ops import triangulate

            polygon = Polygon(points)
            if not polygon.is_valid:
                polygon = polygon.buffer(0)
            if polygon.geom_type == "MultiPolygon":
                polygon = max(polygon.geoms, key=lambda item: item.area)
            faces: List[List[tuple]] = []
            for triangle in triangulate(polygon):
                try:
                    if polygon.contains(triangle.representative_point()):
                        faces.append([(float(p[0]), float(p[1])) for p in list(triangle.exterior.coords)[:3]])
                except Exception:
                    continue
            if faces:
                return faces
        except Exception:
            pass
        cx = sum(p[0] for p in points) / len(points)
        cy = sum(p[1] for p in points) / len(points)
        return [[(cx, cy), points[i], points[(i + 1) % len(points)]] for i in range(len(points))]

    def set_color(self, handle: str, aci: int) -> None:
        if self._handle_index is None:
            self._handle_index = {str(e.dxf.handle): e for e in self.msp if e.dxf.hasattr("handle")}
        entity = self._handle_index.get(str(handle))
        if entity is not None:
            entity.dxf.color = int(aci)

    def set_entity_layer(self, handle: str, layer: str) -> bool:
        """把指定句柄的图元移到目标图层（按代码分图层用）。"""
        self._ensure_layer(layer)
        if self._handle_index is None:
            self._handle_index = {str(e.dxf.handle): e for e in self.msp if e.dxf.hasattr("handle")}
        key = str(handle).upper()
        entity = self._handle_index.get(key) or self._handle_index.get(str(handle))
        if entity is None:
            try:
                entity = self.doc.entitydb[key]
            except Exception:
                return False
        try:
            entity.dxf.layer = layer
            return True
        except Exception:
            return False

    def save_copy(self, path: str, quiet: bool = False) -> None:
        """只写副本：原图绝不覆盖。quiet=True 用于中间件，不打印用户可见提示。"""
        target = Path(path)
        if target.resolve() == self.path.resolve():
            target = self.path.with_name(f"{self.path.stem}_annotated{self.path.suffix}")
        target.parent.mkdir(parents=True, exist_ok=True)
        self.doc.saveas(str(target))
        if not quiet:
            print(f"[写回] 已另存标注副本：{target}", file=sys.stderr)

    def close(self) -> None:
        self.doc = None  # type: ignore[assignment]
