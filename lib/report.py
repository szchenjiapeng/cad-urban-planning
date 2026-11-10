"""成果报告骨架：一、成果 / 二、口径 / 三、校验 / 四、待确认 / 五、异常 / 附：句柄定位。

与平衡表的报告口径保持一致，保证用户拿到任何子技能的成果都能按同一路径复核。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

DEFAULT_STATUS_NOTE = (
    "> 注：图层规范 / 五线颜色 / 退线距离使用内置通用默认值，可按院内标准替换后重跑。\n"
)


def md_table(headers: Sequence[str], rows: Iterable[Sequence]) -> str:
    head = "| " + " | ".join(str(item) for item in headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join([head, sep] + body)


def write_markdown(
    path: str,
    title: str,
    meta: Optional[Sequence[Sequence[str]]] = None,
    sections: Optional[Sequence[tuple]] = None,
    status_note: str = "",
) -> Path:
    lines: List[str] = [f"# {title}", ""]
    lines.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if status_note:
        lines.append("")
        lines.append(status_note.rstrip())
    if meta:
        lines.append("")
        lines.append("## 一、测算口径")
        lines.append("")
        for key, value in meta:
            lines.append(f"- **{key}**：{value}")
    for heading, body in sections or []:
        lines.append("")
        lines.append(f"## {heading}")
        lines.append("")
        if isinstance(body, str):
            lines.append(body.rstrip())
        else:
            lines.append(md_table(body[0], body[1]))
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def locator_note() -> str:
    """带句柄的成果必须附定位方法，否则用户回 CAD 找不到图元。"""
    return (
        "回 CAD 定位图元：命令行输入 `(handent \"句柄\")` 选中对象，"
        "再执行 `ZOOM` → `Object` 居中查看；一对句柄可先 `(ssadd)` 合并选择。\n"
        "完整方法见 `references/handle-locating.md`。"
    )
