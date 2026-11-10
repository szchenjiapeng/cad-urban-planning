#!/usr/bin/env python
"""环境自检：依赖、CAD 连接、图层清单，输出 JSON 供技能在动手前判断可行性。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from backends import create_backend, probe_environment  # noqa: E402


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="CAD 用地测算环境自检")
    parser.add_argument("--progid", help="CAD COM ProgID，默认 AutoCAD.Application")
    parser.add_argument("--file", help="可选：DXF 文件，用于验证解析链路")
    parser.add_argument("--list-layers", action="store_true", help="输出图层清单")
    parser.add_argument("--no-start-cad", dest="start_cad", action="store_false", default=True, help="探测时不要启动 CAD")
    args = parser.parse_args(argv)

    report = probe_environment(args.progid)

    if args.file:
        try:
            backend = create_backend("dxf", args.file, args.progid)
            try:
                report["file_ok"] = True
                report["file"] = str(Path(args.file).resolve())
                report["insunits"] = backend.get_insunits()
                if args.list_layers:
                    report["layers"] = backend.list_layers()
            finally:
                backend.close()
        except Exception as exc:
            report["file_ok"] = False
            report["file_error"] = str(exc)
    elif args.list_layers:
        try:
            backend = create_backend("com", None, args.progid, start_cad=args.start_cad)
            try:
                report["layers"] = backend.list_layers()
            finally:
                backend.close()
        except Exception as exc:
            report["file_error"] = str(exc)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    # 缺依赖是环境问题（4），不能返回 3——3 的语义是「待用户确认」，
    # 否则会话会把「没装 ezdxf」当成「需要问用户」而去弹问题。
    return 0 if report.get("ezdxf") else 4


if __name__ == "__main__":
    raise SystemExit(main())
