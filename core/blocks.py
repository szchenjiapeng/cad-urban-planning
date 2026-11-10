"""块参照递归展开。

设计约束：**只在内存副本里展开，绝不修改用户图纸**。
- DXF 通道：直接对 msp 中的 INSERT 调 virtual_entities()，得到变换后的虚拟实体；
- COM 通道：不执行 EXPLODE，改为整图导出临时 DXF 后交给 DXF 通道展开。

规则：
- MINSERT（阵列插入）按 multi_insert() 逐个展开；
- 嵌套块递归展开，深度上限默认 8 层，防止循环块；
- 块内位于 0 层的实体按 DXF 语义继承块参照所在图层（否则会全部落到 0 层而无法归类）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

MAX_BLOCK_DEPTH = 8


@dataclass
class BlockStats:
    inserts: int = 0
    expanded_entities: int = 0
    max_depth: int = 0
    skipped: int = 0
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "inserts": self.inserts,
            "expanded_entities": self.expanded_entities,
            "max_depth": self.max_depth,
            "skipped": self.skipped,
            "notes": self.notes[:10],
        }


def _is_minsert(insert) -> bool:
    try:
        rows = int(getattr(insert.dxf, "row_count", 1) or 1)
        cols = int(getattr(insert.dxf, "column_count", 1) or 1)
        return rows > 1 or cols > 1
    except Exception:
        return False


def _inherit_layer(entity, parent_layer: str) -> None:
    """块内 0 层实体继承块参照图层，符合 DXF 显示语义，也避免统计时全部漏归类。"""
    try:
        if str(entity.dxf.layer) == "0" and parent_layer and parent_layer != "0":
            entity.dxf.layer = parent_layer
    except Exception:
        pass


def expand_insert(
    insert,
    max_depth: int = MAX_BLOCK_DEPTH,
    depth: int = 0,
    stats: Optional[BlockStats] = None,
) -> List:
    """把一个 INSERT 展开成实体列表（含嵌套块递归）。"""
    if stats is None:
        stats = BlockStats()
    stats.inserts += 1
    stats.max_depth = max(stats.max_depth, depth)

    if depth >= max_depth:
        stats.skipped += 1
        if len(stats.notes) < 5:
            stats.notes.append(f"块嵌套超过 {max_depth} 层，已跳过：{getattr(insert.dxf, 'name', '?')}")
        return []

    parent_layer = str(getattr(insert.dxf, "layer", "0"))
    try:
        sub_inserts = list(insert.multi_insert()) if _is_minsert(insert) else [insert]
    except Exception:
        sub_inserts = [insert]

    entities: List = []
    for sub in sub_inserts:
        try:
            virtual = list(sub.virtual_entities())
        except Exception:
            stats.skipped += 1
            continue
        for entity in virtual:
            _inherit_layer(entity, str(getattr(sub.dxf, "layer", parent_layer)))
            if entity.dxftype() == "INSERT":
                entities.extend(expand_insert(entity, max_depth, depth + 1, stats))
            else:
                stats.expanded_entities += 1
                entities.append(entity)
    return entities


def expand_entities(entities: Sequence, max_depth: int = MAX_BLOCK_DEPTH) -> tuple:
    """展开实体序列中的 INSERT，返回 (展开后的实体列表, 统计)。"""
    stats = BlockStats()
    result: List = []
    for entity in entities:
        if entity.dxftype() == "INSERT":
            result.extend(expand_insert(entity, max_depth, 0, stats))
        else:
            result.append(entity)
    return result, stats
