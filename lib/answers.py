"""应答文件：把「用户选了什么」变成一条命令的参数。

为什么要有它：确认项有 4–8 个，每个选项还带参数。若让会话把中文选项手工翻译成
flag，几乎必然拼错，一错就多跑一轮扫描。有了 `--answers`，会话只需要把用户选中的
选项 `args` 收集起来写成 JSON，一条命令重跑即可。

三种写法（可混用）：

1. 参数对象（最常用，值复制自确认项选项的 args）：
       {"basis": "parcel", "accept-default-rules": true,
        "unregistered-code": ["1709=map:1701", "0909=skip"]}
   → --basis parcel --accept-default-rules --unregistered-code 1709=map:1701 ...

2. 参数数组（原样透传，适合脚本生成）：
       ["--basis", "parcel", "--accept-default-rules"]

3. 选项 id（会话只需交用户的选择，由本模块查确认清单翻译）：
       {"_confirm": "land_area_confirm.json",
        "_choices": {"setback_basis": "parcel", "setback_options": "accept"}}

false / null 表示「不加该参数」；数字与字符串按 `--flag 值` 展开。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import confirm  # noqa: E402

FLAG = "--answers"
CHOICES_KEY = "_choices"
CONFIRM_KEY = "_confirm"


def _flag(name: str) -> str:
    text = str(name or "").strip()
    if not text:
        return ""
    return text if text.startswith("-") else f"--{text}"


def _value_args(flag: str, value) -> List[str]:
    if value is None or value is False or value == "":
        return []
    if value is True:
        return [flag]
    if isinstance(value, (list, tuple)):
        args: List[str] = []
        for entry in value:
            args.extend(_value_args(flag, entry))
        return args
    return [flag, str(value)]


def expand(payload) -> List[str]:
    """把应答内容展开成命令行参数列表。"""
    if isinstance(payload, list):
        return [str(token) for token in payload]
    if not isinstance(payload, dict):
        raise ValueError("应答文件必须是 JSON 对象或数组")

    data = dict(payload)
    args: List[str] = []

    choices = data.pop(CHOICES_KEY, None)
    confirm_path = data.pop(CONFIRM_KEY, None)
    if choices:
        if not confirm_path:
            raise ValueError(f"{CHOICES_KEY} 需要同时给出 {CONFIRM_KEY}（确认清单路径）")
        confirm_file = Path(str(confirm_path))
        if not confirm_file.exists():
            raise FileNotFoundError(f"确认清单不存在：{confirm_file}")
        manifest = json.loads(confirm_file.read_text(encoding="utf-8"))
        args.extend(confirm.choices_to_args(manifest, choices))

    for key, value in data.items():
        flag = _flag(key)
        if not flag:
            continue
        args.extend(_value_args(flag, value))
    return args


def load(path: str) -> List[str]:
    """读取应答文件并展开成参数；错误信息直接可读，便于会话自己修。"""
    file_path = Path(str(path))
    if not file_path.exists():
        raise FileNotFoundError(f"应答文件不存在：{file_path}")
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"应答文件不是合法 JSON：{file_path}（第 {exc.lineno} 行 {exc.msg}）") from exc
    return expand(payload)


def strip(argv: Sequence[str]) -> tuple[List[str], Optional[str]]:
    """从 argv 里摘出 `--answers`（支持 `--answers=x` 与 `--answers x`）。"""
    rest: List[str] = []
    path: Optional[str] = None
    tokens = list(argv)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == FLAG:
            if index + 1 >= len(tokens):
                raise ValueError(f"{FLAG} 后面缺少应答文件路径")
            path = tokens[index + 1]
            index += 2
            continue
        if token.startswith(FLAG + "="):
            path = token.split("=", 1)[1]
            index += 1
            continue
        rest.append(token)
        index += 1
    return rest, path


def expand_argv(argv: Optional[Sequence[str]]) -> List[str]:
    """统一入口：摘出 --answers，把应答参数插到最前面（命令行显式值仍可覆盖）。

    参数错误时抛 ValueError，由调用方返回退出码 2。
    """
    tokens, path = strip(list(argv or ()))
    if not path:
        return tokens
    return load(path) + tokens
