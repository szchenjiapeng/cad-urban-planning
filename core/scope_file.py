"""外部规划范围线文件读取（DXF / DWG）与对齐粗检。

仅在以下场景使用：用户在图纸里找不到规划范围，或否认自动识别的范围图层后，
另行提供一份与总图同坐标系原位的范围线文件。

约定：
- DXF 直接用 ezdxf 解析，不依赖主通道；
- DWG 走 CAD COM（只读打开、读完立即关闭、不保存原文件），无 CAD 时提示另存 DXF；
- 坐标一律按主图坐标系解释，不做平移/旋转/缩放拟合；
- 只做单位（$INSUNITS）与包围盒重叠粗检，可疑时交回上层生成确认项。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from core.geometry import bbox_of, bbox_overlap_ratio  # noqa: E402
from core.models import PolylineRecord  # noqa: E402
from core.scope_region import SCOPE_FILE_REMINDER_TEXT  # noqa: E402

Point = Tuple[float, float]

# 包围盒重叠比低于该值时只提示，不阻断（0 直接阻断）
_SUSPICIOUS_LOW = 0.05


@dataclass
class ScopeFileSource:
    path: Path
    channel: str
    polylines: List[PolylineRecord] = field(default_factory=list)
    insunits: int = 0
    span: Optional[float] = None
    note: str = ""

    def layers(self) -> List[str]:
        return sorted({record.layer for record in self.polylines})

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "channel": self.channel,
            "insunits": self.insunits,
            "polylines": len(self.polylines),
            "layers": self.layers(),
            "note": self.note,
        }


def load_scope_file(
    path: str,
    progid: Optional[str] = None,
    start_cad: bool = True,
) -> ScopeFileSource:
    """读取范围线文件（仅取多段线，填充不作范围线）。"""
    target = Path(path).expanduser().resolve()
    if not target.exists():
        raise FileNotFoundError(f"范围线文件不存在：{target}")

    suffix = target.suffix.lower()
    if suffix == ".dxf":
        return _load_dxf(target)
    if suffix == ".dwg":
        return _load_dwg(target, progid=progid, start_cad=start_cad)
    raise ValueError(f"范围线文件只支持 DXF / DWG，收到：{target.suffix}")


def _load_dxf(target: Path) -> ScopeFileSource:
    from backends.dxf_backend import DxfBackend

    backend = DxfBackend(str(target))
    try:
        polylines = list(backend.iter_polylines(None))
        insunits = backend.get_insunits()
    finally:
        backend.close()
    return _build_source(target, "dxf", polylines, insunits)


def _load_dwg(target: Path, progid: Optional[str], start_cad: bool) -> ScopeFileSource:
    from backends.com_backend import ComBackend

    try:
        backend = ComBackend(
            progid or "AutoCAD.Application", start_if_missing=start_cad
        )
    except Exception as exc:
        raise RuntimeError(
            f"读取 DWG 范围线需要可用的 CAD（ProgID={progid or 'AutoCAD.Application'}）：{exc}\n"
            "下一步：在 CAD 中把范围线另存为 DXF（2013/2018 版本）后用 --scope-file 重新指定，"
            "或直接使用 --scope none 按全部可见填充合计。"
        ) from exc
    try:
        polylines, insunits = backend.read_scope_geometry(str(target))
    finally:
        backend.close()
    return _build_source(target, "dwg", polylines, insunits)


def _build_source(
    target: Path, channel: str, polylines: Sequence[PolylineRecord], insunits: int
) -> ScopeFileSource:
    points = [pt for record in polylines for pt in record.vertices]
    span: Optional[float] = None
    if points:
        box = bbox_of(points)
        span = max(box[2] - box[0], box[3] - box[1])
    note = ""
    if not polylines:
        note = "范围线文件里没有多段线，请确认范围线是否被炸开成直线或多段线是否在模型空间。"
    return ScopeFileSource(
        path=target,
        channel=channel,
        polylines=list(polylines),
        insunits=int(insunits or 0),
        span=span,
        note=note,
    )


def check_alignment(
    scope_points: Sequence[Point],
    drawing_points: Sequence[Point],
) -> dict:
    """包围盒层面的对齐粗检：只回答「是否可能不在同一位置」。

    注意：重叠比高不等于坐标系一定正确，最终仍以用户「同坐标系原位」的承诺为准。
    """
    result = {
        "bbox_overlap_ratio": 0.0,
        "scope_bbox": None,
        "drawing_bbox": None,
        "suspicious": True,
        "level": "unknown",
    }
    if not scope_points or not drawing_points:
        result["level"] = "unknown"
        return result

    scope_box = bbox_of(scope_points)
    drawing_box = bbox_of(drawing_points)
    ratio = bbox_overlap_ratio(scope_box, drawing_box)
    result["bbox_overlap_ratio"] = round(ratio, 4)
    result["scope_bbox"] = [round(value, 2) for value in scope_box]
    result["drawing_bbox"] = [round(value, 2) for value in drawing_box]
    if ratio <= 0:
        result["suspicious"] = True
        result["level"] = "disjoint"
    elif ratio < _SUSPICIOUS_LOW:
        result["suspicious"] = False
        result["level"] = "low_overlap"
    else:
        result["suspicious"] = False
        result["level"] = "ok"
    return result


def scope_file_units_item(
    file_units: str,
    drawing_units: str,
    file_path: str,
) -> dict:
    """范围线文件与总图单位不一致时的确认项（提醒原文必须转述）。"""
    return {
        "type": "scope_file_units",
        "question": (
            f"范围线文件的单位是 {file_units}，总图是 {drawing_units}，两者不一致。"
            "单位不同通常是另存或插入时未对齐所致，直接按总图单位解释会导致面积整体错位。"
            "请确认：范围线是否已在 CAD 中按总图原位、原单位对齐？"
        ),
        "reminder": SCOPE_FILE_REMINDER_TEXT.splitlines(),
        "options": [
            {
                "id": "proceed",
                "label": "已对齐，按总图单位直接解释并继续",
                "args": ["--scope-file", file_path, "--scope-file-units", "assume-drawing"],
            },
            {
                "id": "realign",
                "label": "我去 CAD 对齐后另存 DXF 再重跑",
                "args": [],
            },
            {
                "id": "none",
                "label": "先按无规划范围口径统计",
                "args": ["--scope", "none"],
            },
        ],
        "detail": {
            "file": file_path,
            "file_units": file_units,
            "drawing_units": drawing_units,
            "reminder_text": SCOPE_FILE_REMINDER_TEXT,
        },
    }


def scope_file_mismatch_item(
    file_path: str,
    alignment: dict,
) -> dict:
    """范围线与总图包围盒完全不相交时的确认项（疑似坐标系/原点不一致）。"""
    return {
        "type": "scope_file_mismatch",
        "question": (
            "范围线文件与本图在空间上完全不相交（包围盒无重叠），"
            "基本可以确定坐标系或原点不一致，直接裁剪会得到 0 面积。"
            "请先在 CAD 中把范围线对齐到总图原位后另存，再重新提供。"
        ),
        "reminder": SCOPE_FILE_REMINDER_TEXT.splitlines(),
        "options": [
            {"id": "realign", "label": "我去对齐后重跑", "args": []},
            {"id": "none", "label": "先按无规划范围口径统计", "args": ["--scope", "none"]},
        ],
        "detail": {
            "file": file_path,
            "alignment": alignment,
            "reminder_text": SCOPE_FILE_REMINDER_TEXT,
        },
    }
