"""AutoCAD COM 通道：直接操作 CAD，可读取当前图纸并写回标注。

连接顺序（对应“优先连运行中的 CAD → 没有就启动已安装的 CAD → 都没有再降级 DXF”）：
1. GetActiveObject 连接已运行的实例；
2. 失败则 Dispatch 启动本机已安装的 CAD（可用 --no-start-cad 关闭）；
3. 仍失败由上层降级到 DXF 文件解析。

设计要点：
- 用 SelectionSet 一次性取全图候选对象，减少跨进程往返；
- 多段线面积优先用 AutoCAD 自算的 .Area；填充面积读取失败时记录句柄，
  由 export_handles_to_dxf 复制到新文件导出 DXF 再做几何重建；
- 所有 COM 调用包异常，连接失败立即抛出由上层降级，不做无意义重试；
- 可选 layer_patterns 让 AutoCAD 在选择集阶段就按图层过滤，把「4 万图元 × 5 次属性读取」
  压到「几十个图元」，是 --list-scope 这类只看范围线的场景的主要提速手段；
- 看门狗：COM 调用是同步阻塞且不可中断的，AutoCAD 弹模态框时会无限期卡住，
  因此在旁路线程看心跳，超过 --scan-timeout 就打印可操作提示并退出码 4。
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Tuple

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from backends.base import CadBackend, escape_filter_literal, layer_matches_patterns  # noqa: E402
from core.models import HatchRecord, PolylineRecord, TextRecord  # noqa: E402

_DEFAULT_PROGID = "AutoCAD.Application"
_SELECTION_SET_NAME = "CB_AREA_SCAN"
_COUNT_SET_NAME = "CB_AREA_COUNT"
EXIT_COM_TIMEOUT = 4
_CACHE_DIR_NAME = "cad_urban_cache"
_CACHE_MAX_AGE_DAYS = 7
_PROGRESS_MIN_TOTAL = 500

# AutoCAD 在忙（弹了模态对话框、命令没结束、前台正在刷新）时会拒绝 COM 调用。
# 这类错误码是暂时性的，重试一次往往就过了；不重试会让整轮几分钟的扫描白跑。
_RETRYABLE_COM_ERRORS = {
    -2147418111,  # RPC_E_CALL_REJECTED 被呼叫方拒绝接收呼叫
    -2147417846,  # RPC_E_SERVERCALL_RETRYLATER 服务器调用重试
    -2147417847,  # RPC_E_SERVERCALL_REJECTED
}


def _com_retry(func, *args, attempts: int = 4, base_delay: float = 0.4, **kwargs):
    """调用 AutoCAD 时遇到「被呼叫方拒绝接收呼叫」自动退避重试。"""
    last_exc: Optional[BaseException] = None
    for index in range(max(1, attempts)):
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - COM 错误码不统一，按 args[0] 判断
            code = exc.args[0] if getattr(exc, "args", ()) else None
            last_exc = exc
            if code not in _RETRYABLE_COM_ERRORS or index == attempts - 1:
                raise
            time.sleep(base_delay * (2**index))
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("COM 调用重试失败")


class ComWatchdog:
    """监控 COM 阶段是否长时间无进展；超时打印提示并直接结束进程。

    被监控的 COM 调用在工作线程里无法中断（AutoCAD 侧的模态对话框会让调用永不返回），
    所以只能旁路看心跳：扫描循环里持续 touch()，超时即 os._exit，避免父进程无限等待。
    idle_seconds<=0 表示关闭看门狗。
    """

    def __init__(self, idle_seconds: float = 180.0):
        self.idle_seconds = max(0.0, float(idle_seconds or 0))
        self._limit = self.idle_seconds
        self._phase = "连接 CAD"
        self._last = time.monotonic()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        if self.idle_seconds > 0:
            self._thread = threading.Thread(target=self._loop, name="com-watchdog", daemon=True)
            self._thread.start()

    @property
    def enabled(self) -> bool:
        return self._thread is not None

    def phase(self, name: str, limit: Optional[float] = None) -> None:
        self._phase = name
        self._limit = self.idle_seconds if limit is None else max(0.0, float(limit))
        self.touch()

    def touch(self) -> None:
        self._last = time.monotonic()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            limit = self._limit
            if limit <= 0:
                continue
            idle = time.monotonic() - self._last
            if idle <= limit:
                continue
            try:
                print(
                    f"[超时] 「{self._phase}」已 {idle:.0f}s 无进展（上限 {limit:.0f}s），"
                    "判定 CAD 侧阻塞：多为弹出了模态对话框、命令未结束，或整图导出 DXF 过慢。\n"
                    "处理建议：① 切到 AutoCAD 关掉所有对话框后重试；② 加 --blocks skip 跳过整图 DXF 导出；"
                    "③ 在 CAD 里另存 DXF 后用 --file 走文件通道（最快，且后续重跑秒级）；"
                    "④ 确属大图正常耗时就用 --scan-timeout 0 关闭本保护。",
                    file=sys.stderr,
                    flush=True,
                )
            finally:
                os._exit(EXIT_COM_TIMEOUT)


def _cached_dxf_path(full_name: str) -> Optional[Path]:
    """按图纸路径 + 修改时间生成稳定的 DXF 缓存路径，供 --blocks expand 复用。"""
    try:
        source = Path(full_name)
        stat = source.stat()
    except Exception:
        return None
    stamp = f"{source}|{stat.st_mtime_ns}|{stat.st_size}"
    digest = hashlib.md5(stamp.encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / _CACHE_DIR_NAME / f"{source.stem}_{digest}.dxf"


def _prune_cache(cache_dir: Path, max_age_days: int = _CACHE_MAX_AGE_DAYS) -> None:
    try:
        deadline = time.time() - max_age_days * 86400
        for item in cache_dir.glob("*.dxf"):
            try:
                if item.stat().st_mtime < deadline:
                    item.unlink()
            except Exception:
                continue
    except Exception:
        pass


class ComBackend(CadBackend):
    channel = "com"

    def __init__(
        self,
        progid: str = _DEFAULT_PROGID,
        start_if_missing: bool = True,
        scan_timeout: float = 180.0,
    ):
        try:
            import pythoncom
            import win32com.client
        except ImportError as exc:
            raise RuntimeError("缺少依赖 pywin32，请先执行：python -m pip install pywin32") from exc

        self._pythoncom = pythoncom
        self._win32 = win32com.client
        self.progid = progid or _DEFAULT_PROGID
        self.started = False
        self.scan_timeout = max(0.0, float(scan_timeout or 0))
        self._watchdog = ComWatchdog(self.scan_timeout)
        self._watchdog.phase("连接 CAD")
        pythoncom.CoInitialize()

        try:
            app = None
            active_error = ""
            try:
                app = win32com.client.GetActiveObject(self.progid)
                self._watchdog.touch()
            except Exception as exc:
                active_error = f"{type(exc).__name__}: {exc}"

            if app is None:
                if not start_if_missing:
                    raise RuntimeError(
                        f"未检测到运行中的 CAD（ProgID={self.progid}）。{active_error} "
                        "已按 --no-start-cad 禁止自动启动，请先打开 CAD 或改用 --file 指定 DXF。"
                    )
                try:
                    app = win32com.client.Dispatch(self.progid)
                    self.started = True
                    self._watchdog.touch()
                except Exception as exc:
                    raise RuntimeError(
                        f"未检测到运行中的 CAD，且启动本机 CAD 失败（ProgID={self.progid}）：{exc}。"
                        "若本机未安装 CAD，请改用 --file 指定 DXF 文件。"
                    ) from exc

            self._app = app
            try:
                app.Visible = True
            except Exception:
                pass

            try:
                if int(app.Documents.Count) == 0:
                    app.Documents.Add()
                self._doc = app.ActiveDocument
            except Exception as exc:
                raise RuntimeError(f"已连接 CAD 但没有可用的图纸：{exc}") from exc
            self._watchdog.touch()
        except Exception:
            self._watchdog.stop()
            raise

    # ---------------------------------------------------------------- 元信息
    def describe(self) -> str:
        try:
            name = str(self._doc.Name)
        except Exception:
            name = "<未知图纸>"
        started = "（由本脚本启动）" if self.started else ""
        return f"AutoCAD 活动文档：{name}{started}"

    @property
    def path(self) -> Optional[str]:
        """当前图纸的完整路径（未落盘的新图返回 None）。"""
        try:
            full_name = str(self._doc.FullName or "")
        except Exception:
            return None
        return full_name or None

    def get_insunits(self) -> int:
        # AutoCAD 通过系统变量暴露 INSUNITS；取不到时交由几何层按包围盒启发式判定
        try:
            return int(self._doc.GetVariable("INSUNITS"))
        except Exception:
            return 0

    def list_layers(self) -> List[str]:
        self._watchdog.phase("读取图层表")
        try:
            return sorted(str(layer.Name) for layer in self._doc.Layers)
        except Exception:
            return []
        finally:
            self._watchdog.touch()

    # ---------------------------------------------------------------- 图层状态
    def get_layer_states(self) -> dict:
        states: dict = {}
        try:
            layers = self._doc.Layers
            for index in range(int(layers.Count)):
                layer = layers.Item(index)
                try:
                    name = str(layer.Name)
                except Exception:
                    continue
                try:
                    on = bool(layer.LayerOn)
                except Exception:
                    on = True
                try:
                    frozen = bool(layer.Freeze)
                except Exception:
                    frozen = False
                try:
                    locked = bool(layer.Lock)
                except Exception:
                    locked = False
                states[name] = {"on": on, "frozen": frozen, "locked": locked}
        except Exception:
            return states
        return states

    def list_layouts(self) -> List[str]:
        names: List[str] = []
        try:
            layouts = self._doc.Layouts
            for index in range(int(layouts.Count)):
                try:
                    names.append(str(layouts.Item(index).Name))
                except Exception:
                    continue
        except Exception:
            return names
        return [name for name in names if name.lower() != "model"]

    def get_layout_frozen_layers(self, layout: Optional[str] = None) -> List[str]:
        names = self.list_layouts()
        target = layout or (names[-1] if names else "")
        try:
            layout_obj = self._doc.Layouts.Item(target) if target else self._doc.ActiveLayout
        except Exception:
            return []
        frozen = set()
        try:
            block = layout_obj.Block
            for index in range(int(block.Count)):
                obj = block.Item(index)
                try:
                    if "Viewport" not in str(obj.ObjectName):
                        continue
                    for name in obj.FrozenLayers:
                        frozen.add(str(name))
                except Exception:
                    continue
        except Exception:
            pass
        return sorted(frozen)

    def layout_name(self, layout: Optional[str] = None) -> str:
        names = self.list_layouts()
        return layout or (names[-1] if names else "")

    # ---------------------------------------------------------------- 块
    def has_block_references(self) -> bool:
        """只数 INSERT 数量。过滤器不可用时返回 False——宁可少展开块，
        也不要因为把「全图对象数」误当成 INSERT 数而白白整图导出 DXF。"""
        self._watchdog.phase("检查块参照")
        sets = self._doc.SelectionSets
        try:
            existing = sets.Item(_COUNT_SET_NAME)
            existing.Delete()
        except Exception:
            pass
        selection = sets.Add(_COUNT_SET_NAME)
        try:
            applied, error = self._select(selection, "INSERT")
            if not applied:
                print(
                    f"[提示] 无法按类型过滤（{error or '未知原因'}），跳过块参照检测，不导出整图 DXF。",
                    file=sys.stderr,
                    flush=True,
                )
                return False
            return int(selection.Count) > 0
        finally:
            self._watchdog.touch()
            try:
                selection.Delete()
            except Exception:
                pass

    def export_all_to_dxf(self, path: str) -> bool:
        """整图导出 DXF（不改变当前文档的保存路径），用于块展开与填充边界重建。

        AutoCAD COM 的两个坑（都表现为静默失败 / 无效的参数数目）：
        - Export 必须带 SelectionSet，缺第三个参数会抛「无效的参数数目」；
        - FileName 不能带扩展名，带 .dxf 时 AutoCAD 不生成文件，需由 Extension 决定。
        """
        target = Path(path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        self._watchdog.phase("整图导出 DXF（--blocks expand）", max(self.scan_timeout * 4, 600))
        try:
            name = str(target.with_suffix(""))
            sets = self._doc.SelectionSets
            ss = None
            for suffix in ("", "_2", "_3"):
                try:
                    ss = sets.Add(f"AREA_EXPORT{suffix}")
                    break
                except Exception:
                    continue
            if ss is None:
                return False
            try:
                ss.Select(5)  # acSelectionSetAll：整图（含布局）导出
                self._doc.Export(name, "dxf", ss)
            finally:
                try:
                    ss.Delete()
                except Exception:
                    pass
            return target.exists()
        except Exception:
            return False
        finally:
            self._watchdog.touch()

    def source_signature(self) -> Optional[str]:
        try:
            if not bool(self._doc.Saved):
                return None
            full_name = str(self._doc.FullName)
            stat = Path(full_name).stat()
        except Exception:
            return None
        return f"com|{full_name}|{stat.st_mtime_ns}|{stat.st_size}"

    def cached_dxf(self) -> Optional[str]:
        """整图 DXF 缓存路径（图纸未修改时可复用）；有未保存改动或未落盘时返回 None。"""
        try:
            if not bool(self._doc.Saved):
                return None
            full_name = str(self._doc.FullName)
        except Exception:
            return None
        path = _cached_dxf_path(full_name)
        return str(path) if path else None

    def prepare_cache_dir(self, path: str) -> None:
        """导出前清理过期缓存，避免临时目录无限膨胀。"""
        try:
            cache_dir = Path(path).resolve().parent
            if cache_dir.name == _CACHE_DIR_NAME:
                cache_dir.mkdir(parents=True, exist_ok=True)
                _prune_cache(cache_dir)
        except Exception:
            pass

    # ---------------------------------------------------------------- 图元
    def count_entities_by_layer(
        self,
        layers: Sequence[str],
        entity_types: str = "LWPOLYLINE,POLYLINE",
    ) -> dict:
        """按图层只数数量：每个图层一次 Select + 读 Count，不碰任何图元属性。

        返回 {图层名: {"polylines": int|None, "entities": int|None, "error": str}}；
        过滤器不被支持时如实报错，不拿「全图数量」冒充该图层数量。
        """
        counts: dict = {}
        names = [str(name) for name in layers if str(name).strip()]
        if not names:
            return counts

        self._watchdog.phase("按图层统计多段线数量", max(self.scan_timeout, 120))
        sets = self._doc.SelectionSets
        set_name = _COUNT_SET_NAME
        try:
            existing = sets.Item(set_name)
            existing.Delete()
        except Exception:
            pass
        ss = sets.Add(set_name)
        try:
            total = len(names)
            for index, name in enumerate(names, 1):
                record = {"polylines": None, "entities": None, "error": ""}
                try:
                    ss.Clear()
                except Exception:
                    pass
                applied, error = self._select(ss, entity_types, [name])
                if applied:
                    record["polylines"] = int(ss.Count)
                    record["entities"] = record["polylines"]
                else:
                    record["error"] = error or "CAD 不接受图层过滤参数，无法在不读图元的前提下计数"
                counts[name] = record
                self._watchdog.touch()
                if record["error"]:
                    print(f"[提示] 图层「{name}」计数异常：{record['error']}", file=sys.stderr, flush=True)
                elif total >= 4:
                    print(
                        f"[图层] 已统计 {index}/{total}：{name} → {record['polylines']} 个多段线",
                        file=sys.stderr,
                        flush=True,
                    )
            return counts
        finally:
            try:
                ss.Delete()
            except Exception:
                pass

    def _select(
        self,
        ss,
        entity_types: str,
        layer_patterns: Optional[Sequence[str]] = None,
    ) -> Tuple[bool, str]:
        """对选择集做「全图 + 可选图层过滤」，返回 (是否真的带上了过滤, 错误说明)。

        关键兼容性：AutoCAD 的 Select 只接受 VARIANT 数组形式的 FilterType/FilterData，
        直接传 Python list 会报「参数 FilterType 无效」，且旧代码会静默退回全选——
        那会把「按图层 2 个对象」变成「全图 4 万个对象」，既慢又把 has_block_references
        之类的判断全部弄错。这里按 VARIANT → list → 无过滤 的顺序降级，并如实报告。
        """
        patterns = [str(p).strip() for p in (layer_patterns or ()) if str(p).strip()]

        def variant_arrays(codes, values):
            return (
                self._win32.VARIANT(self._pythoncom.VT_ARRAY | self._pythoncom.VT_I2, list(codes)),
                self._win32.VARIANT(self._pythoncom.VT_ARRAY | self._pythoncom.VT_VARIANT, list(values)),
            )

        def plain_arrays(codes, values):
            return list(codes), list(values)

        last_error = ""
        for make in (variant_arrays, plain_arrays):
            try:
                if patterns:
                    # 多个图层模式必须一次 Select 传进去：SelectionSet.Select(5) 是「全选后覆盖」，
                    # 循环调用只会留下最后一个图层，前面几个会静默丢失。
                    merged = ",".join(
                        # 不含 * ? 的按精确图层名处理，避免图层名里的 . # 等被当成通配符
                        pattern if any(ch in pattern for ch in "*?") else escape_filter_literal(pattern)
                        for pattern in dict.fromkeys(patterns)
                    )
                    codes, values = make([0, 8], [entity_types, merged])
                    ss.Select(5, None, None, codes, values)
                else:
                    codes, values = make([0], [entity_types])
                    ss.Select(5, None, None, codes, values)
                # 只做类型过滤时这也是「过滤器真的生效了」，不能返回 bool(patterns)：
                # 旧写法让 has_block_references 永远以为过滤不可用，块检测与 --blocks expand 全部失效。
                return True, ""
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                try:
                    ss.Clear()
                except Exception:
                    pass

        # 两种写法都不被接受：退回无过滤全选，保证不漏数据，但明确告知调用方
        try:
            ss.Select(5)
        except Exception as exc:
            raise RuntimeError(f"选择集全选失败：{exc}（此前的过滤器错误：{last_error}）") from exc
        return False, last_error

    def _selection_set(self, entity_types: str, layer_patterns: Optional[Sequence[str]] = None):
        sets = self._doc.SelectionSets
        try:
            existing = sets.Item(_SELECTION_SET_NAME)
            existing.Delete()
        except Exception:
            pass
        ss = sets.Add(_SELECTION_SET_NAME)

        patterns = [str(p).strip() for p in (layer_patterns or ()) if str(p).strip()]
        applied, error = self._select(ss, entity_types, patterns or None)
        if patterns and not applied:
            print(
                f"[提示] 图层过滤不可用（{error or '未知原因'}），已退回全量扫描，速度会明显变慢。",
                file=sys.stderr,
                flush=True,
            )
        elif patterns and int(ss.Count) == 0:
            print(
                f"[提示] 图层过滤未命中（{', '.join(patterns)}），可能是图层名不符或该图层没有对象。",
                file=sys.stderr,
                flush=True,
            )
        return ss

    def _progress(self, label: str, done: int, total: int, started: float, force: bool = False) -> None:
        """枚举进度：只在图元量大时输出，保证重定向到日志文件也能看到进展。"""
        self._watchdog.touch()
        if total < _PROGRESS_MIN_TOTAL:
            return
        step = max(1, total // 20)
        if not force and done % step:
            return
        elapsed = time.monotonic() - started
        print(f"[扫描] {label} {done}/{total}，已用 {elapsed:.1f}s", file=sys.stderr, flush=True)

    def _warn_misplaced_layers(
        self,
        layers: Optional[Sequence[str]],
        layer_patterns: Optional[Sequence[str]],
    ) -> None:
        """提醒 layers / layer_patterns 传错位置：layers 是枚举完的本地过滤。

        两个参数形似，误把图层名传给 layers 时 CAD 仍会枚举全图图元
        （4 万图元约十几分钟），且因为一直有进展不会触发看门狗。
        这里只提示不改行为，避免静默慢成"假死"。
        """
        if not layers or layer_patterns:
            return
        names = [str(name).strip() for name in layers if str(name).strip()]
        if not names:
            return
        preview = "、".join(names[:5]) + (f" 等 {len(names)} 个" if len(names) > 5 else "")
        print(
            f"[提示] 图层名 {preview} 传给了 layers：该参数只在 CAD 枚举完成后本地过滤，"
            "全图图元仍会被逐条读取（大图需十几分钟）。"
            "要缩小扫描范围请改传 layer_patterns，例如 iter_polylines(None, [图层名])。",
            file=sys.stderr,
            flush=True,
        )

    def iter_polylines(
        self,
        layers: Optional[Sequence[str]] = None,
        layer_patterns: Optional[Sequence[str]] = None,
    ) -> Iterator[PolylineRecord]:
        self._warn_misplaced_layers(layers, layer_patterns)
        self._watchdog.phase("枚举多段线")
        started = time.monotonic()
        ss = self._selection_set("LWPOLYLINE,POLYLINE", layer_patterns)
        try:
            layer_set = set(layers) if layers else None
            total = int(ss.Count)
            count = 0
            for index in range(total):
                obj = _com_retry(ss.Item, index)
                try:
                    record = _com_retry(self._read_entity, obj)
                except Exception:
                    record = None
                count += 1
                self._progress("多段线", count, total, started)
                if record is None:
                    continue
                if layer_set is not None and record.layer not in layer_set:
                    continue
                yield record
            self._progress("多段线", total, total, started, force=True)
        finally:
            try:
                ss.Delete()
            except Exception:
                pass

    def iter_hatches(
        self,
        layers: Optional[Sequence[str]] = None,
        layer_patterns: Optional[Sequence[str]] = None,
    ) -> Iterator[HatchRecord]:
        """填充对象只取句柄、图层与 Area；Area 读不到时把句柄留给 DXF 兜底。"""
        self._warn_misplaced_layers(layers, layer_patterns)
        self._watchdog.phase("枚举填充")
        started = time.monotonic()
        ss = self._selection_set("HATCH", layer_patterns)
        try:
            layer_set = set(layers) if layers else None
            total = int(ss.Count)
            count = 0
            for index in range(total):
                obj = _com_retry(ss.Item, index)
                count += 1
                self._progress("填充", count, total, started)
                try:
                    handle, layer_name = _com_retry(lambda: (str(obj.Handle), str(obj.Layer)))
                except Exception:
                    continue
                if layer_set is not None and layer_name not in layer_set:
                    continue
                if layer_patterns and not layer_matches_patterns(layer_name, layer_patterns):
                    continue

                area = None
                error = ""
                try:
                    area = _com_retry(lambda: float(obj.Area))
                    if area <= 0:
                        error = f"Area 返回 {area}"
                        area = None
                except Exception as exc:
                    error = f"读取 Area 失败：{exc}"

                yield HatchRecord(
                    handle=handle,
                    layer=layer_name,
                    rings=None,
                    area_drawing=area,
                    area_error=error,
                )
            self._progress("填充", total, total, started, force=True)
        finally:
            try:
                ss.Delete()
            except Exception:
                pass

    def iter_texts(
        self,
        layers: Optional[Sequence[str]] = None,
        layer_patterns: Optional[Sequence[str]] = None,
    ) -> Iterator[TextRecord]:
        """枚举文字：只读 TextString / InsertionPoint / Height / Rotation。"""
        self._warn_misplaced_layers(layers, layer_patterns)
        self._watchdog.phase("枚举文字")
        started = time.monotonic()
        ss = self._selection_set("TEXT,MTEXT", layer_patterns)
        try:
            layer_set = set(layers) if layers else None
            total = int(ss.Count)
            count = 0
            for index in range(total):
                obj = _com_retry(ss.Item, index)
                count += 1
                self._progress("文字", count, total, started)
                try:
                    handle = str(obj.Handle)
                    layer_name = str(obj.Layer)
                except Exception:
                    continue
                if layer_set is not None and layer_name not in layer_set:
                    continue
                if layer_patterns and not layer_matches_patterns(layer_name, layer_patterns):
                    continue
                try:
                    text = str(obj.TextString or "").strip()
                except Exception:
                    continue
                if not text:
                    continue
                try:
                    raw_point = tuple(float(v) for v in obj.InsertionPoint)
                    point = (raw_point[0], raw_point[1])
                except Exception:
                    point = (0.0, 0.0)
                try:
                    height = float(obj.Height)
                except Exception:
                    height = 0.0
                try:
                    rotation = float(obj.Rotation)
                except Exception:
                    rotation = 0.0
                try:
                    entity_type = str(obj.EntityName).upper()
                except Exception:
                    entity_type = "TEXT"
                yield TextRecord(
                    handle=handle,
                    layer=layer_name,
                    text=text,
                    point=point,
                    height=height,
                    rotation=rotation,
                    entity_type=entity_type,
                )
            self._progress("文字", total, total, started, force=True)
        finally:
            try:
                ss.Delete()
            except Exception:
                pass

    def iter_lines(
        self,
        layers: Optional[Sequence[str]] = None,
        layer_patterns: Optional[Sequence[str]] = None,
    ) -> Iterator[PolylineRecord]:
        """枚举直线：包装成两点 PolylineRecord，供五线/红线读取。"""
        self._warn_misplaced_layers(layers, layer_patterns)
        self._watchdog.phase("枚举直线")
        started = time.monotonic()
        ss = self._selection_set("LINE", layer_patterns)
        try:
            layer_set = set(layers) if layers else None
            total = int(ss.Count)
            count = 0
            for index in range(total):
                obj = _com_retry(ss.Item, index)
                count += 1
                self._progress("直线", count, total, started)
                try:
                    layer_name = str(obj.Layer)
                except Exception:
                    continue
                if layer_set is not None and layer_name not in layer_set:
                    continue
                if layer_patterns and not layer_matches_patterns(layer_name, layer_patterns):
                    continue
                try:
                    start = tuple(float(v) for v in obj.StartPoint)
                    end = tuple(float(v) for v in obj.EndPoint)
                    vertices = [(start[0], start[1]), (end[0], end[1])]
                    handle = str(obj.Handle)
                except Exception:
                    continue
                yield PolylineRecord(
                    handle=handle,
                    layer=layer_name,
                    entity_type="LINE",
                    vertices=vertices,
                    closed_flag=False,
                )
            self._progress("直线", total, total, started, force=True)
        finally:
            try:
                ss.Delete()
            except Exception:
                pass

    def read_scope_geometry(self, path: str) -> Tuple[List[PolylineRecord], int]:
        """只读打开另一份图纸（范围线文件），取其中多段线与 INSUNITS，读完立即关闭不保存。

        注意：Documents.Open 会切换活动文档，这里临时把 self._doc 指向该文档再复用
        iter_polylines；finally 中还原引用并 Close(False)，不动用户已打开的主图。
        """
        target = Path(path).resolve()
        saved = self._doc
        doc = None
        try:
            doc = self._app.Documents.Open(str(target), True)
        except Exception as exc:
            raise RuntimeError(
                f"CAD 打开范围线文件失败：{target}（{exc}）。"
                "若该文件已在 CAD 中打开，请先关闭；或另存 DXF 后用 --scope-file 指定。"
            ) from exc
        try:
            self._doc = doc
            polylines = list(self.iter_polylines(None))
            try:
                insunits = int(doc.GetVariable("INSUNITS"))
            except Exception:
                insunits = 0
            return polylines, insunits
        finally:
            self._doc = saved
            try:
                doc.Close(False)
            except Exception:
                pass

    def _read_entity(self, obj) -> Optional[PolylineRecord]:
        try:
            object_name = str(obj.ObjectName)
        except Exception:
            return None
        if "Polyline" not in object_name:
            return None

        try:
            handle = str(obj.Handle)
            layer = str(obj.Layer)
        except Exception:
            return None

        try:
            closed = bool(obj.Closed)
        except Exception:
            closed = False

        try:
            cad_area = float(obj.Area)
        except Exception:
            cad_area = None

        try:
            flat = tuple(float(v) for v in obj.Coordinates)
        except Exception:
            flat = ()

        vertices = [(flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2)]
        # 凸度必须读出来：只拿顶点会把圆弧当弦，规划范围的面积与裁剪边界都会偏小
        # （本图范围线 41 段圆弧，漏读导致 1512.2180 ha 被算成 1510.9635 ha）。
        bulges: List[float] = []
        for index in range(len(vertices)):
            try:
                bulges.append(float(obj.GetBulge(index)))
            except Exception:
                bulges = []
                break
        if bulges and not any(abs(value) > 1e-12 for value in bulges):
            bulges = []  # 全为 0 等价于没有圆弧，省内存
        return PolylineRecord(
            handle=handle,
            layer=layer,
            entity_type=object_name,
            vertices=vertices,
            bulges=bulges,
            closed_flag=closed,
            cad_area=cad_area,
        )

    def get_polyline(self, handle: str) -> Optional[PolylineRecord]:
        try:
            obj = self._doc.HandleToObject(handle)
        except Exception:
            return None
        try:
            return self._read_entity(obj)
        except Exception:
            return None

    # ---------------------------------------------------------------- DXF 兜底
    def export_handles_to_dxf(self, handles: Sequence[str], path: str) -> bool:
        """把指定句柄的对象复制到新建文件并另存为 DXF，不改动用户当前图纸。

        用于 HATCH 的 Area 读不到（“输入无效”）时，把对象单独导出后交给 ezdxf
        重建边界、按奇偶嵌套规则算净面积。
        """
        objects = []
        for handle in handles:
            try:
                objects.append(self._doc.HandleToObject(str(handle)))
            except Exception:
                continue
        if not objects:
            return False

        target = Path(path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)

        try:
            new_doc = self._app.Documents.Add()
        except Exception:
            return False

        try:
            try:
                array = self._win32.VARIANT(
                    self._pythoncom.VT_ARRAY | self._pythoncom.VT_DISPATCH, objects
                )
                self._doc.CopyObjects(array, new_doc.ModelSpace)
            except Exception:
                return False

            try:
                new_doc.SaveAs(str(target))
            except Exception:
                # 部分版本需显式指定 DXF 类型（61 = 2013 DXF）
                try:
                    new_doc.SaveAs(str(target), 61)
                except Exception:
                    return False
            return target.exists()
        finally:
            try:
                new_doc.Close(False)
            except Exception:
                pass

    # ---------------------------------------------------------------- 写回
    def _ensure_layer(self, layer: str) -> None:
        try:
            self._doc.Layers.Item(layer)
        except Exception:
            self._doc.Layers.Add(layer)

    def ensure_layer(
        self,
        layer: str,
        color: Optional[int] = None,
        linetype: Optional[str] = None,
    ) -> None:
        """建图层并设置颜色/线型；线型不存在时退回 CONTINUOUS。"""
        try:
            layer_obj = self._doc.Layers.Item(layer)
        except Exception:
            layer_obj = self._doc.Layers.Add(layer)
        self._apply_layer_props(layer_obj, color, linetype)

    def set_layer_props(
        self,
        layer: str,
        color: Optional[int] = None,
        linetype: Optional[str] = None,
    ) -> None:
        if color is None and not linetype:
            return
        try:
            layer_obj = self._doc.Layers.Item(layer)
        except Exception:
            layer_obj = self._doc.Layers.Add(layer)
        self._apply_layer_props(layer_obj, color, linetype)

    def _apply_layer_props(self, layer_obj, color: Optional[int], linetype: Optional[str]) -> None:
        if color is not None:
            try:
                layer_obj.color = int(color)
            except Exception:
                pass
        if linetype:
            try:
                layer_obj.Linetype = str(linetype)
            except Exception:
                try:
                    layer_obj.Linetype = "CONTINUOUS"
                except Exception:
                    pass

    def ensure_text_style(self, name: str, font: Optional[str] = None, bigfont: Optional[str] = None) -> bool:
        key = str(name or "").strip()
        if not key:
            return False
        try:
            try:
                style = self._doc.TextStyles.Item(key)
            except Exception:
                style = self._doc.TextStyles.Add(key)
            if font:
                style.fontFile = str(font)
                if bigfont:
                    style.BigFontFile = str(bigfont)
            return True
        except Exception:
            return False

    def add_text(
        self,
        layer: str,
        point: tuple,
        text: str,
        height: float = 2.5,
        style: Optional[str] = None,
        width_factor: Optional[float] = None,
    ) -> None:
        self._ensure_layer(layer)
        x, y = float(point[0]), float(point[1])
        # 部分 AutoCAD 版本（如 2019）不接受 tuple/array 形式的点参数，只认 VARIANT 数组点，
        # 直接传 tuple 会报 E_INVALIDARG「发生意外」；与 add_polyline 同样做一次降级。
        try:
            insertion = self._win32.VARIANT(
                self._pythoncom.VT_ARRAY | self._pythoncom.VT_R8, [x, y, 0.0]
            )
            text_obj = self._doc.ModelSpace.AddText(text, insertion, float(height))
        except Exception:
            text_obj = self._doc.ModelSpace.AddText(text, (x, y, 0.0), float(height))
        text_obj.Layer = layer
        if style:
            self.ensure_text_style(style)
            try:
                text_obj.StyleName = str(style)
            except Exception:
                pass
        if width_factor:
            try:
                text_obj.WidthFactor = float(width_factor)
            except Exception:
                pass

    def set_entity_layer(self, handle: str, layer: str) -> bool:
        self._ensure_layer(layer)
        try:
            obj = self._doc.HandleToObject(str(handle))
        except Exception:
            return False
        try:
            obj.Layer = layer
            return True
        except Exception:
            return False

    def add_polyline(self, layer: str, points: Sequence[tuple], closed: bool = True) -> None:
        self._ensure_layer(layer)
        coords: List[float] = []
        for point in points:
            coords.append(float(point[0]))
            coords.append(float(point[1]))
        if len(coords) < 6:
            return
        try:
            array = self._win32.VARIANT(self._pythoncom.VT_ARRAY | self._pythoncom.VT_R8, coords)
            polyline = self._doc.ModelSpace.AddLightWeightPolyline(array)
        except Exception:
            polyline = self._doc.ModelSpace.AddLightWeightPolyline(coords)
        polyline.Closed = bool(closed)
        polyline.Layer = layer

    def add_extruded_solid(
        self, layer: str, points: Sequence[tuple], height: float, base_z: float = 0.0
    ) -> bool:
        """闭合轮廓 → Region → 3DSOLID。

        先建**临时**轻量多段线（不动用户原有建筑轮廓），AddRegion 后立即删除临时线，
        再 AddExtrudedSolid。任何一步失败都返回 False，由上层降级为体块网格并记录原因。
        """
        cleaned: List[tuple] = []
        for point in points or ():
            try:
                cleaned.append((float(point[0]), float(point[1])))
            except Exception:
                continue
        if len(cleaned) < 3 or float(height) <= 0:
            return False
        self._ensure_layer(layer)
        msp = self._doc.ModelSpace
        coords: List[float] = []
        for point in cleaned:
            coords.append(point[0])
            coords.append(point[1])
        polyline = None
        try:
            try:
                array = self._win32.VARIANT(self._pythoncom.VT_ARRAY | self._pythoncom.VT_R8, coords)
                polyline = msp.AddLightWeightPolyline(array)
            except Exception:
                polyline = msp.AddLightWeightPolyline(coords)
            polyline.Closed = True
            if base_z:
                try:
                    polyline.Elevation = float(base_z)
                except Exception:
                    pass
            try:
                objects = self._win32.VARIANT(
                    self._pythoncom.VT_ARRAY | self._pythoncom.VT_DISPATCH, [polyline]
                )
                regions = msp.AddRegion(objects)
            except Exception:
                regions = msp.AddRegion([polyline])
            try:
                region = regions[0]
            except Exception:
                region = regions
            if region is None:
                return False
            solid = msp.AddExtrudedSolid(region, float(height), 0.0)
            try:
                solid.Layer = layer
            except Exception:
                pass
            return True
        except Exception:
            return False
        finally:
            if polyline is not None:
                try:
                    polyline.Delete()
                except Exception:
                    pass

    def set_color(self, handle: str, aci: int) -> None:
        obj = self._doc.HandleToObject(handle)
        obj.color = int(aci)

    def save_copy(self, path: str, quiet: bool = False) -> None:
        self._doc.SaveAs(str(Path(path).resolve()))

    def close(self) -> None:
        try:
            self._watchdog.stop()
        except Exception:
            pass
        try:
            self._pythoncom.CoUninitialize()
        except Exception:
            pass
