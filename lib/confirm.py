"""统一确认项协议（所有子技能共用）。

设计目标：**一次问完、一次重跑**。

1. 同一轮能判定的问题合并成一个 `items` 列表（`answer_mode: "batch"`），
   会话把 1..N 项放进同一条消息问完，禁止「发现一个问一个」；
2. 每个选项带 `id` 与 `args`，用户答完后把选中项的 `args` 合并成一条命令重跑，
   会话不需要把中文选项翻译成参数（翻译一定会错）；
3. 信封同时输出 `items` 与 `confirmations`（旧字段名），条目同时给 `code` 与 `id`，
   保证 balance 子技能与既有回归测试都能继续读。

退出码约定：
0 成功 / 2 参数错误 / 3 待确认 / 4 环境或超时 / 5 数据质量阻断
"""

from __future__ import annotations

import json
import re
from typing import Dict, Iterable, List, Optional, Sequence

EXIT_OK = 0
EXIT_ARG = 2
EXIT_CONFIRM = 3
EXIT_TIMEOUT = 4
EXIT_DATA = 5

ANSWER_MODE = "batch"

DEFAULT_HINT = (
    "本次已把能判定的待确认项一次性汇总（answer_mode=batch）。"
    "请在同一条消息里把 1..N 每一项连同选项一次问完，不要逐项来回追问；"
    "用户答完后，把选中选项的 args 合并成 answers.json，"
    "用 `--answers answers.json` 一条命令重跑一次。"
    "未确认前不生成成果。"
)

_SLUG_KEEP = re.compile(r"[^0-9a-zA-Z_.]+")


def _slug_from_args(args: Sequence[str]) -> str:
    """由 args 推导稳定 id：['--basis','parcel'] -> 'basis-parcel'。"""
    parts: List[str] = []
    for token in args:
        text = str(token).strip().lstrip("-")
        if not text:
            continue
        parts.append(_SLUG_KEEP.sub("-", text).strip("-").lower())
    joined = "-".join(part for part in parts if part)
    return joined[:40] or "option"


def normalize_option(raw, index: int = 0) -> Dict:
    """把一个选项统一成 {id,label,desc,description,args,requires_input,reminder}。

    允许三种写法：纯字符串 / {"label","desc"} / {"label","args","description",...}。
    字符串与简写选项也会补齐 `id` 与 `args`（空列表），下游不必判断字段是否存在。
    """
    if isinstance(raw, dict):
        label = str(raw.get("label", "") or "")
        desc = str(raw.get("desc") or raw.get("description") or "")
        args = [str(token) for token in (raw.get("args") or [])]
        option_id = str(raw.get("id") or "").strip()
        requires = str(raw.get("requires_input") or "")
        reminder = [str(text) for text in (raw.get("reminder") or [])]
    else:
        label, desc, args = str(raw or ""), "", []
        option_id, requires, reminder = "", "", []

    if not option_id:
        option_id = _slug_from_args(args) if args else f"opt{index + 1}"
    return {
        "id": option_id,
        "label": label,
        "desc": desc,
        # 兼容 balance 子技能的 description 字段名
        "description": desc,
        "args": args,
        "requires_input": requires,
        "reminder": reminder,
    }


def item(
    code: str,
    question: str,
    options: Optional[Iterable] = None,
    default: Optional[str] = None,
    detail: Optional[Dict] = None,
    level: str = "blocking",
    title: str = "",
    why: str = "",
    recommended: str = "",
    round_no: int = 1,
) -> Dict:
    """构造一个确认项。

    - `code`：机器识别用（同时以 `id` 输出）；
    - `title`：给用户看的短标题（缺省取 code）；
    - `why`：改了会怎样，用于「先说影响再问」，避免用户盲选；
    - `recommended`：推荐选项的 id，会话可以一句「都按推荐」收口；
    - `detail`：证据（图层、句柄、面积、候选打分等），供会话原样转述。
    """
    normalized = [normalize_option(option, index) for index, option in enumerate(options or ())]
    return {
        "code": str(code),
        "id": str(code),
        "title": str(title or code),
        "level": level,
        "round": int(round_no),
        "question": str(question),
        "why": str(why),
        "recommended": str(recommended),
        "options": normalized,
        "default": default or "",
        "detail": detail or {},
    }


def _ensure_keys(entry: Dict) -> Dict:
    """兼容 balance 子技能：它用 `type` 而不是 `code`，选项用 `description`。"""
    if not isinstance(entry, dict):  # pragma: no cover - 防御性
        return entry
    kind = entry.get("code") or entry.get("id") or entry.get("type") or ""
    entry.setdefault("code", kind)
    entry.setdefault("id", kind)
    entry.setdefault("title", entry.get("title") or kind)
    entry.setdefault("why", entry.get("why") or "")
    entry.setdefault("recommended", entry.get("recommended") or "")
    entry.setdefault("level", entry.get("level") or "blocking")
    entry.setdefault("round", entry.get("round") or 1)
    entry.setdefault("detail", entry.get("detail") or {})
    entry["options"] = [
        normalize_option(option, index)
        for index, option in enumerate(entry.get("options") or ())
    ]
    return entry


def merge(*groups: Optional[Sequence[Dict]]) -> List[Dict]:
    """合并多阶段确认项，并按「类型 + 对象」去重（同一问题只问一次）。"""
    merged: Dict[str, Dict] = {}
    for group in groups:
        for entry in group or ():
            if not isinstance(entry, dict):
                continue
            key = json.dumps(
                [entry.get("code") or entry.get("type"), entry.get("detail") or {}],
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            merged.setdefault(key, _ensure_keys(dict(entry)))
    return list(merged.values())


def build_payload(
    items: Sequence[Dict],
    round_no: int = 1,
    hint: Optional[str] = None,
    source: str = "",
    extra: Optional[Dict] = None,
) -> Dict:
    """组装统一信封。

    `items` 与 `confirmations` 指向同一份列表（旧字段名兼容）；
    `count` 是条目数，会话按 1..count 编号一次问完。
    """
    normalized = [_ensure_keys(dict(entry)) for entry in (items or ())]
    payload: Dict = {
        "status": "need_confirm",
        "answer_mode": ANSWER_MODE,
        "round": int(round_no),
        "count": len(normalized),
        "items": normalized,
        "confirmations": normalized,
        "hint": hint or DEFAULT_HINT,
    }
    if source:
        payload["source"] = source
    if extra:
        payload.update(extra)
    return payload


def emit(
    items: Sequence[Dict],
    stream=None,
    round_no: int = 1,
    hint: Optional[str] = None,
    source: str = "",
    extra: Optional[Dict] = None,
) -> int:
    """打印统一确认信封，返回退出码 3。"""
    payload = build_payload(items, round_no=round_no, hint=hint, source=source, extra=extra)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if stream is None:
        print(text)
    else:
        print(text, file=stream)
    return EXIT_CONFIRM


def fail(code: str, message: str, exit_code: int = EXIT_DATA, detail: Optional[Dict] = None) -> int:
    """数据质量阻断：输出错误 JSON 并返回 5，不生成成果。"""
    payload = {
        "status": "blocked",
        "exit_code": exit_code,
        "code": code,
        "message": message,
        "detail": detail or {},
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


def choices_to_args(payload: Dict, choices: Dict) -> List[str]:
    """把 {确认项 id: 选项 id} 翻译成命令行参数。

    用于 `--answers`：会话只需要交出用户选了哪一项，不必自己拼参数。
    找不到的确认项或选项会抛 KeyError，避免静默按默认值跑。
    """
    index: Dict[str, Dict] = {}
    for entry in payload.get("items") or payload.get("confirmations") or ():
        key = str(entry.get("id") or entry.get("code") or entry.get("type") or "")
        if not key:
            continue
        options = {str(opt.get("id")): opt for opt in entry.get("options") or ()}
        index[key] = options

    args: List[str] = []
    for item_id, option_id in (choices or {}).items():
        if item_id not in index:
            raise KeyError(f"确认项不存在：{item_id}（可选：{', '.join(sorted(index))}）")
        if option_id not in index[item_id]:
            raise KeyError(
                f"确认项 {item_id} 没有选项 {option_id}"
                f"（可选：{', '.join(sorted(index[item_id]))}）"
            )
        args.extend(str(token) for token in index[item_id][option_id].get("args") or ())
    return args
