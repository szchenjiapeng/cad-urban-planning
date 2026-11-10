#!/usr/bin/env python
"""规划 CAD 子技能统一入口。

    python scripts/up.py code        # 地块编号（N1）
    python scripts/up.py setback     # 建筑退线（N2）
    python scripts/up.py fivelines   # 城市控制线（N3，原「城市五线」）
    python scripts/up.py balance     # 土地利用平衡表（既有子技能）

各子命令的参数与对应脚本完全一致，加 `--help` 查看。
统一入口只是转发，不改变任何既有脚本的行为。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import answers  # noqa: E402

COMMANDS = {
    "code": ("parcel_code", "地块编号批量生成"),
    "parcel": ("parcel_code", "地块编号批量生成"),
    "setback": ("building_setback", "建筑退线"),
    "fivelines": ("urban_five_lines", "城市控制线"),
    "five": ("urban_five_lines", "城市控制线"),
    "control": ("urban_five_lines", "城市控制线"),
    "balance": ("land_area_report", "土地利用平衡表"),
    "massing": ("building_massing", "批量建筑量/容积率测算与批量建筑建模"),
    "far": ("building_massing", "批量建筑量/容积率测算与批量建筑建模"),
    "extrude": ("building_massing", "批量建筑量/容积率测算与批量建筑建模"),
}


def usage() -> str:
    lines = ["用法：python scripts/up.py <子命令> [参数...]", "", "可用子命令："]
    seen = set()
    for name, (module, desc) in COMMANDS.items():
        if module in seen:
            continue
        seen.add(module)
        lines.append(f"  {name:<10} {desc}")
    lines.append("")
    lines.append("示例：python scripts/up.py code --file 总图.dxf --write-back")
    lines.append("答完确认项后一次重跑：python scripts/up.py setback --file 总图.dxf --answers answers.json")
    return "\n".join(lines)


def main(argv=None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] in {"-h", "--help", "help"}:
        print(usage())
        return 0
    name = args[0].lower()
    if name not in COMMANDS:
        print(f"未知子命令：{args[0]}\n\n{usage()}", file=sys.stderr)
        return 2
    module_name, _desc = COMMANDS[name]
    # --answers 必须在子命令解析之前展开：把用户选中的选项翻译成参数，
    # 避免会话手工拼 flag（拼错就得多扫一次图）。
    try:
        forwarded = answers.expand_argv(args[1:])
    except KeyError as exc:
        print(f"[参数错误] 应答文件里的选择对不上确认清单：{exc}", file=sys.stderr)
        return 2
    except (ValueError, FileNotFoundError) as exc:
        print(f"[参数错误] {exc}", file=sys.stderr)
        return 2
    module = importlib.import_module(module_name)
    return int(getattr(module, "main")(forwarded) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
