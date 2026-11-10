"""成果导出：默认出 DWG 副本，只有本机无法执行 CAD 时才回退 DXF。

链路：
1. ezdxf 先写 DXF 中间件（中文路径没问题）；
2. 有 accoreconsole.exe（AutoCAD 自带的无界面控制台）时，把它当转换器：
   复制到 ASCII 临时目录 → `_.SAVEAS 2018 <ascii>.dwg` → 移回目标中文路径；
3. 两者都没有 → 回退 DXF，并在成果里明确写出原因，不静默降级。

注意：accoreconsole 对中文路径不稳定，所以进出都用 ASCII 临时名；
原图永远不动，只产出 `<原名>_<后缀>.dwg` 副本。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional, Tuple

_DWG_VERSION = "2018"


def _registry_candidates():
    try:
        import winreg
    except Exception:
        return
    roots = (
        r"SOFTWARE\Autodesk\AutoCAD",
        r"SOFTWARE\WOW6432Node\Autodesk\AutoCAD",
    )
    for root in roots:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root) as key:
                for index in range(winreg.QueryInfoKey(key)[0]):
                    version = winreg.EnumKey(key, index)
                    try:
                        with winreg.OpenKey(key, version) as version_key:
                            for sub in range(winreg.QueryInfoKey(version_key)[0]):
                                product = winreg.EnumKey(version_key, sub)
                                try:
                                    with winreg.OpenKey(version_key, product) as product_key:
                                        location, _ = winreg.QueryValueEx(product_key, "AcadLocation")
                                except OSError:
                                    continue
                                if location:
                                    yield str(Path(location) / "accoreconsole.exe")
                    except OSError:
                        continue
        except OSError:
            continue


def find_accoreconsole() -> Optional[str]:
    """定位 accoreconsole.exe：环境变量 → 注册表 → PATH。"""
    env_value = os.environ.get("ACCORECONSOLE")
    if env_value and Path(env_value).exists():
        return str(Path(env_value))
    for candidate in _registry_candidates():
        if candidate and Path(candidate).exists():
            return candidate
    found = shutil.which("accoreconsole.exe") or shutil.which("accoreconsole")
    return found


def dwg_ready() -> Tuple[bool, str]:
    exe = find_accoreconsole()
    if exe:
        return True, f"accoreconsole：{exe}"
    return False, "未检测到 accoreconsole.exe（AutoCAD 无界面控制台），无法生成 DWG"


def resolve_format(requested: str) -> Tuple[str, str]:
    """返回 (实际格式, 说明)。requested: auto|dwg|dxf|none。"""
    value = (requested or "auto").lower()
    if value == "none":
        return "none", "按参数跳过导出"
    if value == "dxf":
        return "dxf", "按参数指定 DXF"
    ready, reason = dwg_ready()
    if value == "dwg":
        if ready:
            return "dwg", reason
        return "dxf", f"指定 DWG 但{reason}，已回退 DXF"
    return ("dwg", reason) if ready else ("dxf", reason)


def _run_script(exe: str, dxf_path: Path, out_dwg: Path, version: str, timeout: float) -> bool:
    """跑一次 accoreconsole 转换；失败返回 False。"""
    if out_dwg.exists():
        try:
            out_dwg.unlink()
        except OSError:
            pass
    sequences = [
        f"FILEDIA\n0\n_.SAVEAS\n{version}\n{out_dwg}\n",
        f'FILEDIA\n0\n(command "_.saveas" "{version}" "{out_dwg}")\n',
    ]
    workdir = dxf_path.parent
    for text in sequences:
        script = workdir / "dwg_export.scr"
        # 不能自己把 \n 换成 \r\n：Windows 文本模式会再转一次，变成空行，
        # accoreconsole 会把下一行当命令执行（曾导致 FILEDIA 0 变成无效命令）。
        script.write_text(text, encoding="ascii", errors="ignore")
        try:
            subprocess.run(
                [exe, "/i", str(dxf_path), "/s", str(script)],
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except Exception:
            continue
        if out_dwg.exists() and out_dwg.stat().st_size > 0:
            return True
    return False


def export_dwg(dxf_path: str, dwg_path: str, timeout: float = 240.0) -> Tuple[bool, str]:
    """把 DXF 转成 DWG 目标文件；成功返回 (True, 说明)。"""
    exe = find_accoreconsole()
    if not exe:
        return False, "未检测到 accoreconsole.exe"
    source = Path(dxf_path)
    target = Path(dwg_path)
    if not source.exists():
        return False, f"DXF 中间件不存在：{source}"

    workdir = Path(tempfile.mkdtemp(prefix="up_dwg_"))
    try:
        ascii_dxf = workdir / "input.dxf"
        ascii_dwg = workdir / "output.dwg"
        shutil.copy2(source, ascii_dxf)
        if not _run_script(exe, ascii_dxf, ascii_dwg, _DWG_VERSION, timeout):
            return False, "accoreconsole 转换失败（SAVEAS 未生成文件）"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(ascii_dwg), str(target))
        return True, f"DWG 已生成：{target.name}（AutoCAD {_DWG_VERSION} 格式）"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def report_note(fmt: str, reason: str) -> str:
    if fmt == "dwg":
        return f"成果格式 DWG（{reason}）"
    if fmt == "dxf":
        return f"成果格式 DXF（{reason}；拿到能执行 CAD 的机器后可重跑出 DWG）"
    return "未导出图纸（仅出清单）"


def main(argv=None) -> int:  # pragma: no cover - 手工排查用
    args = list(argv if argv is not None else sys.argv[1:])
    if len(args) < 2:
        print("用法：python scripts/lib/export_dwg.py <in.dxf> <out.dwg>")
        return 2
    ok, message = export_dwg(args[0], args[1])
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
