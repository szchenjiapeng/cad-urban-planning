"""子技能共用 CLI：取数参数、输出参数、单位判定、写回与 JSON 落盘。

目的：避免每个子技能重复声明 20 多个相同参数，也避免单位/通道语义各写一套。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from backends import create_backend  # noqa: E402
from core.geometry import resolve_unit  # noqa: E402
from lib import export_dwg  # noqa: E402


def add_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", default="auto", choices=["auto", "com", "dxf"], help="取数通道")
    parser.add_argument("--file", help="DXF 路径（DWG 需先另存 DXF）")
    parser.add_argument("--progid", help="CAD COM ProgID，默认 AutoCAD.Application")
    parser.add_argument("--no-start-cad", dest="start_cad", action="store_false", default=True, help="CAD 未运行时不要自动启动")
    parser.add_argument("--scan-timeout", type=float, default=180.0, help="COM 单阶段无进展超时秒数，0 关闭")
    parser.add_argument("--unit", choices=["auto", "mm", "cm", "m", "km", "in", "ft"], default="auto", help="图纸单位")


def add_output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--out", help="Markdown 报告输出路径")
    parser.add_argument("--json", dest="json_path", help="JSON 摘要输出路径")
    parser.add_argument(
        "--write-back",
        dest="write_back",
        action="store_true",
        default=True,
        help="生成成果图纸副本（默认开启：DWG 新副本，本机无 CAD 时回退 DXF）",
    )
    parser.add_argument(
        "--no-write-back",
        dest="write_back",
        action="store_false",
        help="只出报告与清单，不生成成果图纸副本",
    )
    parser.add_argument("--copy-out", help="成果副本路径；缺省为 原图名_后缀.dwg，扩展名（.dwg/.dxf）决定格式")
    parser.add_argument(
        "--export",
        default="auto",
        choices=["auto", "dwg", "dxf", "none"],
        help="成果格式：auto 默认 DWG、本机无 CAD 才回退 DXF；dxf / none 为强制指定",
    )
    parser.add_argument(
        "--no-report",
        dest="want_report",
        action="store_false",
        default=True,
        help="只出成果图纸与 JSON，不写 Markdown 报告",
    )


def add_layer_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--layers", help="图层白名单，逗号分隔")
    parser.add_argument("--exclude-layers", help="图层排除名单，逗号分隔")
    parser.add_argument("--layer-pattern", action="append", default=None, help="图层通配符（可重复），如 'JZ-*'")


def parse_list(text: Optional[str]) -> List[str]:
    if not text:
        return []
    return [item.strip() for item in str(text).replace("，", ",").split(",") if item.strip()]


def open_backend(args) -> Tuple[object, str, float, str]:
    """按参数打开后端，返回 (backend, 单位名, 图纸单位→米 系数, 判定依据)。"""
    backend = create_backend(
        source=getattr(args, "source", "auto"),
        file_path=getattr(args, "file", None),
        progid=getattr(args, "progid", None),
        start_cad=getattr(args, "start_cad", True),
        scan_timeout=getattr(args, "scan_timeout", 180.0),
    )
    try:
        insunits = backend.get_insunits()
    except Exception:
        insunits = 0
    override = getattr(args, "unit", "auto")
    unit, factor, source = resolve_unit(insunits, None, None if override == "auto" else override)
    return backend, unit, factor, source


def visible_layers(backend) -> List[str]:
    """只取「开且未冻结」的图层；取不到状态时退回全部。"""
    try:
        states = backend.get_layer_states()
    except Exception:
        states = {}
    if not states:
        try:
            return backend.list_layers()
        except Exception:
            return []
    return sorted(
        name
        for name, state in states.items()
        if state.get("on", True) and not state.get("frozen", False)
    )


def report_folder(backend) -> Path:
    """成果目录：跟着图纸走（<图纸目录>/<图名>_成果），不污染用户当前工作目录。"""
    source = getattr(backend, "path", None)
    if source:
        base = Path(str(source))
        return base.parent / f"{base.stem}_成果"
    return Path.cwd() / "CAD成果"


def resolve_out(args, backend, suffix: str) -> Optional[Path]:
    """决定 Markdown 报告路径。

    - `--out` 显式给了就按用户说的（目录结尾则补 `<图名>_<后缀>.md`）；
    - 没给则默认 `<图纸目录>/<图名>_成果/<图名>_<后缀>.md`，用户不必记着传 --out；
    - `--no-report` 返回 None，只出图纸与 JSON。
    """
    if not getattr(args, "want_report", True):
        return None
    source = getattr(backend, "path", None)
    stem = Path(str(source)).stem if source else ""
    raw = getattr(args, "out", None)
    if raw:
        target = Path(str(raw))
        if target.is_dir() or str(raw).endswith(("/", "\\")):
            target = target / (f"{stem}_{suffix}.md" if stem else f"{suffix}.md")
        if target.suffix.lower() not in {".md", ".markdown"}:
            target = target.with_suffix(".md")
        return target
    folder = report_folder(backend)
    return folder / (f"{stem}_{suffix}.md" if stem else f"{suffix}.md")


def copy_target(backend, args, suffix: str, fmt: str) -> Tuple[Path, str]:
    """决定成果副本路径；`--copy-out` 的扩展名（.dwg/.dxf）优先于 `--export`。"""
    raw = getattr(args, "copy_out", None)
    if raw:
        target = Path(raw)
        ext = target.suffix.lower()
        if ext in {".dwg", ".dxf"}:
            return target, ext.lstrip(".")
        return target.with_suffix(f".{fmt}"), fmt
    source = getattr(backend, "path", None)
    if source:
        base = Path(source)
        return base.with_name(f"{base.stem}_{suffix}.{fmt}"), fmt
    return Path.cwd() / f"{suffix}.{fmt}", fmt


def _save_dxf(backend, path, quiet: bool = False) -> None:
    """把当前成果写成 DXF。COM 通道用 Export（不动用户文档的保存路径）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if getattr(backend, "channel", "") == "com":
        if backend.export_all_to_dxf(str(target.resolve())):
            return
        raise RuntimeError(f"CAD 整图导出 DXF 失败：{target}")
    backend.save_copy(str(target), quiet=quiet)


def export_drawing(backend, args, suffix: str) -> Tuple[Optional[str], str, str]:
    """把当前成果另存为图纸副本：默认 DWG，DXF 兜底，原图永不覆盖。

    返回 (副本路径, 实际格式, 说明)；不导出时路径为 None、格式为 none。
    """
    if not getattr(args, "write_back", True):
        return None, "none", "未生成副本（--no-write-back）"
    fmt, reason = export_dwg.resolve_format(getattr(args, "export", "auto"))
    if fmt == "none":
        return None, "none", "未导出（--export none）"
    target, fmt = copy_target(backend, args, suffix, fmt)

    if getattr(backend, "channel", "") == "com":
        try:
            backend.save_copy(str(target))
            return str(target), fmt, f"{fmt.upper()} 副本（当前 CAD 另存，原图未改动）：{target}"
        except Exception as exc:  # 另存失败再走 DXF 导出链路
            reason = f"CAD 另存失败：{exc}"

    if fmt == "dwg":
        workdir = Path(tempfile.mkdtemp(prefix="up_export_"))
        try:
            tmp_dxf = workdir / "result.dxf"
            _save_dxf(backend, tmp_dxf, quiet=True)
            ok, message = export_dwg.export_dwg(str(tmp_dxf), str(target))
            if ok:
                return str(target), "dwg", f"DWG 副本：{target}（{message}）"
            reason = message
        except Exception as exc:
            reason = f"DXF 中间件写出失败：{exc}"
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        fallback = target.with_suffix(".dxf")
        _save_dxf(backend, fallback)
        return str(fallback), "dxf", f"DWG 未生成（{reason}），已回退 DXF：{fallback}"

    _save_dxf(backend, target)
    return str(target), "dxf", f"DXF 副本：{target}（{reason}）"


def finalize(backend, args, suffix: str, hint: str = "") -> Optional[str]:
    """兼容旧调用：等同 `export_drawing`，返回副本路径；hint 仅在未导出时提示。"""
    path, _fmt, note = export_drawing(backend, args, suffix)
    if path is None and hint:
        print(hint, file=sys.stderr)
    return path


def write_json(path: Optional[str], payload: dict) -> Optional[str]:
    if not path:
        return None
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(target)


def setup_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


def dedupe(items: Sequence[str]) -> List[str]:
    return list(dict.fromkeys(str(item) for item in items if str(item)))


def pattern_literal(name: str) -> str:
    """把精确图层名转成通配符安全的字面量（供 COM 选择集预过滤）。"""
    special = set("*?[]#@.~,`")
    return "".join(("`" + ch) if ch in special else ch for ch in str(name))


def preprocess_argv(argv: Optional[Sequence[str]] = None) -> List[str]:
    """展开 `--answers <应答文件>`：把用户选中的选项变成参数。

    放在 parse_args 之前调用；参数错误直接以退出码 2 结束，
    避免「应答文件写错」被当成「待确认」而多跑一轮扫描。
    """
    from lib import answers  # 延迟导入：只有真用 --answers 时才需要

    tokens = list(sys.argv[1:] if argv is None else argv)
    try:
        return answers.expand_argv(tokens)
    except KeyError as exc:
        print(f"[参数错误] 应答文件里的选择对不上确认清单：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except (ValueError, FileNotFoundError) as exc:
        print(f"[参数错误] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
