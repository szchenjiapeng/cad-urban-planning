# 平衡表参数速查（`scripts/land_area_report.py`）

跑平衡表前必读本文件；通用口径见 `references/contract.md`，硬性规则见 `references/rules-land-area.md`。

## 参数表

| 参数 | 说明 |
| --- | --- |
| `--source auto\|com\|dxf` | 通道；`auto` 有 `--file` 走 DXF，否则连 CAD（活动 → 启动） |
| `--file` | DXF 路径，DWG 需先另存 DXF |
| `--progid` | 非 AutoCAD 产品的 COM ProgID，也可用环境变量 `CAD_PROGID` |
| `--no-start-cad` | CAD 未运行时禁止自动启动已安装的 CAD |
| `--primitive hatch\|auto\|polyline\|both` | 识别对象；默认 `hatch` = 只算填充。`auto` = 整图一个填充都没有时才整体回退多段线；`both` = 两者去重合并 |
| `--allow-polyline-fill` | 允许用多段线补填充缺口（默认关，会与道路反算重复计面积） |
| `--derive-road auto\|on\|off` | 是否按差额反算道路用地；`off` 关闭，默认 `auto` |
| `--derive-road-tolerance` | 允许反算的未识别面积上限（占规划范围 %，默认 1），超过则阻断反算 |
| `--derive-road-max-share` | 反算道路占比的经验上限（%，默认 40），超过只标需复核 |
| `--overlap-report` | 输出重叠明细报告（默认只提示数量与面积，先问用户再生成） |
| `--cad-areas` | 导入 CAD 自算面积清单（JSON：`{句柄: 面积m²}` 或 `[{handle, area}]`），DXF 通道用它把面积基准换成 CAD `.Area` |
| `--overlap-threshold` | 重叠告警阈值（占规划范围总面积的占比，默认 `0.01`=1%，`0` 关闭）。跨图层超阈值时脚本中断并输出四选一确认项，未确认不出表；同图层超阈值只在报告与交付说明里着重提示，不追问 |
| `--overlap-ack` | 已确认跨图层重叠，按当前阈值放行：出平衡表但不出明细（对应四选一的选项 1） |
| `--diagnose-only` | 只输出排查清单（未识别/异常/重叠/道路反算状态），不生成平衡表；对应跨图层超阈值的选项 3 |
| `--overlap-min-area` / `--overlap-min-ratio` | 重叠面积下限（m²，默认 0.5）/ 占较小地块比例下限（默认 1%） |
| `--no-hatch-fallback` | 关闭「填充 Area 读不到 → 导出 DXF 重建边界」的兜底 |
| `--no-scan-cache` | 禁用图元扫描缓存。默认按「图纸路径 + mtime + size」复用上次扫描结果，确认参数后重跑从十几分钟降到秒级 |
| `--arc-variant off\|on` | 填充弧段离散策略，默认 `off`（沿用原策略）；`on` 会在与 CAD 自算面积吻合（<5%）且包围盒正常时改用「信任 ccw」的还原，仅用于排查 |
| `--standard 2023\|gb50137` | 分类标准，默认 `2023` |
| `--mapping` / `--classification` | 自定义映射 / 分类树 JSON 路径 |
| `--add-code "CODE:名称[:层级:父级]"` | 确认新增用地代码：先写入分类配置与指引表，再按该代码归类（可重复） |
| `--unregistered-code "CODE=skip\|map:目标代码"` | 未登记代码的处理：`skip`=本次沿用兜底（报告标需复核），`map:0902`=归入已有代码（可重复） |
| `--ignore-hybrid-unresolved` | 混合用地里有段对不上分类表时不再追问，只按首段代码统计（报告标需复核） |
| `--scope all\|window\|polygon\|handles\|none` | 统计范围；`all`=按图层自动识别规划范围，`none`=无规划范围（全部可见图层填充合计）；`window/polygon/handles` 配同名参数 |
| `--scope-file` | 外部规划范围线文件（DXF；DWG 需本机有 CAD，只读打开且不保存）。须与总图同坐标系原位 |
| `--scope-file-units auto\|assume-drawing` | 范围线文件与总图 `$INSUNITS` 不一致时：`assume-drawing`=已对齐、按总图单位直接解释 |
| `--scope-layer` / `--scope-layers` | 指定范围图层名（忽略大小写，可指定不含关键词的图层）/ 范围图层关键词（默认含规划范围线、范围线、GHFW/ghfw、fw、fwx、红线等）；配 `--scope-file` 时指范围线文件内的图层 |
| `--scope-relation auto\|union\|difference\|intersect` | 多环范围的空间关系；`auto` 相离取并集、**A 在 B 内自动挖空（统计两环之间）**、相邻/交叉中断待确认。显式指定即视为「已确认非草稿」，不再追问 |
| `--scope-outer-only` | 多环时只取面积最大的外环（草稿直通开关：不追问草稿，直接取最大） |
| `--clip auto\|intersect\|whole\|exclude` | 范围裁剪方式；**范围外地块一律不计入**，`whole` 只作复核用的逃生口（会整块计入范围外面积） |
| `--clip-fallback whole\|exclude` | 未安装 shapely 时跨界地块的处理：整块计入（默认，面积偏大）或排除 |
| `--clip-strict ask\|on\|off` | CAD 直连读不到填充边界时的裁剪策略：`ask`=问用户（默认），`on`=导 DXF 严格裁剪（**面积仍取 CAD 自算值，副本只提供边界**；准确但可能慢），`off`=保持直连（快，范围外填充可能整块计入） |
| `--layer-state auto\|model\|layout\|all` | 图层可见性口径；**只统计已打开图层**，`auto` 检测到模型/布局差异时中断询问，`all` 仅复核用 |
| `--layers` / `--exclude-layers` | 图层白名单 / 排除名单 |
| `--include-codes` / `--exclude-codes` | 只统计 / 排除指定用地代码 |
| `--construction-codes` | 覆盖「建设用地」的用地代码口径，支持区间写法 `06-16` 或逐项 `07,08,09`；缺省读分类表 `balance_groups` |
| `--unit auto\|mm\|cm\|m\|km` | 图纸单位；`auto` 读 `$INSUNITS`，缺失时按全图跨度推断 |
| `--closure-tolerance` | 闭合容差（图纸单位），默认全图跨度 1e-5 |
| `--total-area` | 规划范围总面积（公顷），用于覆盖率校验 |
| `--population` | 规划人口（人），用于人均用地 |
| `--write-back` / `--colorize` | 写回面积标注 / 按一级类着色；DXF 通道只写 `_annotated.dxf` 副本 |
| `--out` / `--json` | 报告与摘要输出路径 |
| `--confirm-out` | 待确认清单输出路径，默认 `land_area_confirm.json` |
| `--confirm-mode batch\|early` | 确认项输出方式：`batch`（默认）=一轮汇总、最多两轮问完；`early`=扫描前发现问题立刻中断（超大图省一次扫描，问题分多轮） |
| `--list-scope-layers` | 范围识别第一步：只读图层表，列出图层名含范围关键词的候选（不读图元，秒出），多个候选时退出码 3 待用户选定 |
| `--list-layers` / `--list-scope` / `--check` | 只列图层 / 范围识别第二步：只进 `--scope-layer` 指定图层读边界（未指定时自动只扫范围关键词图层）/ 只做环境自检 |
| `--scan-timeout` | COM 单个阶段无进展的超时秒数，默认 180；超时打印排查提示并以退出码 4 结束，`0`=关闭 |
