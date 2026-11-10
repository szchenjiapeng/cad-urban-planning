"""后端工厂：通道探测与自动降级。

连接优先级（用户要求的顺序）：
1. 已运行的 CAD（GetActiveObject）；
2. 未运行但本机已安装 → 启动 CAD（Dispatch，可用 --no-start-cad 关闭）；
3. 本机无 CAD → 降级解析 DXF 文件（需 --file）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from backends.base import CadBackend  # noqa: F401
from backends.com_backend import ComBackend
from backends.dxf_backend import DxfBackend

__all__ = ["CadBackend", "ComBackend", "DxfBackend", "create_backend", "probe_environment", "is_cad_installed"]


def is_cad_installed(progid: str) -> bool:
    """查注册表判断本机是否安装了该 ProgID 对应的 CAD，不会启动进程。"""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid):
            return True
    except Exception:
        return False


def create_backend(
    source: str = "auto",
    file_path: Optional[str] = None,
    progid: Optional[str] = None,
    start_cad: bool = True,
    expand_blocks: bool = False,
    scan_timeout: float = 180.0,
) -> CadBackend:
    """按 source 创建后端。

    - dxf：必须给 file_path；
    - com：强制连 CAD（活动 → 启动）；
    - auto：给了 file_path 就按 DXF 解析（显式文件优先），否则连 CAD（活动 → 启动）。

    scan_timeout 只对 COM 通道有效：单个阶段无进展超过该秒数即打印提示并退出码 4。
    """
    source = (source or "auto").lower()
    if source not in {"auto", "com", "dxf"}:
        raise ValueError(f"未知的 --source 取值：{source}")
    progid = progid or os.environ.get("CAD_PROGID") or "AutoCAD.Application"

    if source == "dxf" or (source == "auto" and file_path):
        if not file_path:
            raise ValueError("DXF 通道需要通过 --file 指定文件路径")
        return DxfBackend(file_path, expand_blocks=expand_blocks)

    try:
        return ComBackend(progid, start_if_missing=start_cad, scan_timeout=scan_timeout)
    except Exception as exc:
        if source == "com":
            raise
        raise RuntimeError(
            f"COM 通道不可用：{exc}\n"
            "降级方案：把图纸另存为 DXF 后用 --file 指定，脚本将改用 ezdxf 解析。"
        ) from exc


def probe_environment(progid: Optional[str] = None) -> dict:
    """探测依赖与通道可用性，不抛异常、不启动 CAD。"""
    progid = progid or os.environ.get("CAD_PROGID") or "AutoCAD.Application"
    result = {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "ezdxf": None,
        "pywin32": False,
        "cad_installed": is_cad_installed(progid),
        "com": False,
        "com_error": "",
        "progid": progid,
        "recommended_channel": "dxf",
        "hint": "",
    }
    try:
        import ezdxf

        result["ezdxf"] = getattr(ezdxf, "__version__", "unknown")
    except ImportError:
        result["ezdxf"] = None

    try:
        import win32com.client  # noqa: F401

        result["pywin32"] = True
    except ImportError:
        pass

    if result["pywin32"]:
        try:
            backend = ComBackend(progid, start_if_missing=False)
            result["com"] = True
            result["document"] = backend.describe()
            backend.close()
        except Exception as exc:
            result["com_error"] = str(exc)

    if result["com"]:
        result["recommended_channel"] = "com"
        result["hint"] = "CAD 已运行，可直接读取活动图纸"
    elif result["cad_installed"] and result["pywin32"]:
        result["recommended_channel"] = "com-start"
        result["hint"] = "检测到已安装 CAD 但未运行，运行时可自动启动（--no-start-cad 可关闭）"
    elif result["ezdxf"]:
        result["recommended_channel"] = "dxf"
        result["hint"] = "本机无可用 CAD，需用 --file 指定 DXF"
    else:
        result["hint"] = "缺少 ezdxf 且无可用 CAD，请先安装依赖"
    return result
