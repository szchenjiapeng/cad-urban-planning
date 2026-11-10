# 通用契约（所有子技能共用）

新增子技能必须遵守本文件；与既有平衡表子技能的规则保持一致，不得另立口径。

## 1. 取数通道（三级顺序）

1. 运行中的 CAD（COM 直连活动图纸）；
2. 本机已安装但未运行 → 自动启动（`--no-start-cad` 可关闭）；
3. 本机无 CAD → `--file` 指定 DXF（DWG 必须先另存 DXF）。

沿用 `scripts/backends` 的 `create_backend`，不新写连接逻辑。统一入口：

```bash
python scripts/up.py <code|setback|fivelines|balance> --file 总图.dxf
```

## 2. 单位

- 图纸单位由 `$INSUNITS` 判定，`--unit` 可强制指定（mm/cm/m/km/in/ft）。
- 面积统一换算成平方米；成果同时给平方米与公顷。
- `$INSUNITS=0` 时按图纸跨度启发式推断，报告必须写明「置信度低」。
- 米制图标成毫米是常见事故（全表差 10⁶ 倍且不报错），发现「跨度很小但图元很多」要先问用户。

## 3. 图层可见性

只统计**模型空间开启且未冻结**的图层；关闭/冻结图层一律不计入，并在报告里列出跳过清单。
新增脚本统一调用 `lib.cli.visible_layers()`，不要自己遍历图层表。

## 4. 确认协议（不得静默取默认值）

所有「不能猜」的分支输出结构化确认项并以退出码 3 结束，由会话把问题原样转给用户：

```json
{"status": "need_confirm", "exit_code": 3,
 "confirmations": [{"code": "...", "level": "blocking", "question": "...",
                    "options": [{"label": "...", "desc": "..."}], "detail": {}}]}
```

退出码统一为：`0` 成功 / `2` 参数错误 / `3` 待确认 / `4` 环境或超时 / `5` 数据质量阻断。

**确认项必须批量汇总，最多两轮。** 同一轮内能判定的问题要合并成一个 `items` 列表一次输出（`answer_mode: "batch"`，`count` 为条目数），不得「发现一个问一个」：

| 轮次 | 时机 | 汇总内容 |
| --- | --- | --- |
| 第一轮（计算前） | 扫描完成后、统计之前 | 旧标准换算、未登记代码、规划范围、单位口径、图层可见性、裁剪通道 |
| 第二轮（计算后） | 统计完成、写成果之前 | 未登记代码（图斑级）、混合用地未解析、跨图层重叠闸门 |

会话的处理方式固定为：把 `items` 按 1..N **在同一条消息里一次问完** → 用户答完后把选中选项的 `args` 合并成一条命令重跑一次。禁止一次只转述一个问题、反复打断用户。平衡表可通过 `--confirm-mode early` 恢复「扫到就问」的旧行为（超大图省一次扫描，代价是问题分成多轮）。

### 统一信封（五个子技能一致）

```json
{
  "status": "need_confirm", "answer_mode": "batch", "round": 1, "count": 2,
  "items": [{
    "id": "setback_basis", "code": "setback_basis", "title": "退线基准",
    "why": "决定退哪条界线，三种口径结果差异很大", "recommended": "parcel",
    "question": "……", "level": "blocking",
    "options": [{"id": "parcel", "label": "只退用地红线", "desc": "……",
                 "args": ["--basis", "parcel"]}],
    "detail": {}
  }],
  "confirmations": ["与 items 指向同一份列表，旧字段名兼容"],
  "hint": "请一次问完，答完用 --answers 重跑"
}
```

`items` 与 `confirmations` 同时输出；条目同时给 `code` 与 `id`；**每个选项必须带 `args`**，否则会话只能靠读中文描述手工拼参数（必然出错）。

### 重跑用 `--answers`，不要手工拼参数

把选中选项的 `args` 写进应答文件，一条命令重跑：

```bash
python scripts/up.py setback --file 总图.dxf --answers answers.json
```

```json
{"basis": "parcel", "accept-default-rules": true, "unregistered-code": ["1709=map:1701", "0909=skip"]}
```

也支持「只交选项 id」的形式，由脚本查确认清单翻译：

```json
{"_confirm": "land_area_confirm.json", "_choices": {"setback_basis": "parcel"}}
```

应答文件写错、确认项或选项 id 对不上时返回退出码 `2`（参数错误），不会被误当成「待确认」而白扫一次图。

必须确认的场景（至少）：
- 旧标准成片出现（`R2`/`A33`/`B1`/`S1` 等）→ 按 GB50137 还是换算 2023；
- 图层名含未登记代码（`0905` 等）→ 写错 / 新增代码 / 归入已有类；
- 无规划范围、无法识别退线基准、红线不是闭合面；
- 控制线（五线）来源没命中任何图层。

## 5. 成果骨架（报告固定六节）

1. 一、口径（标准、单位、通道、图层口径）
2. 二、成果（清单表）
3. 三、校验
4. 四、异常清单
5. 五、待确认 / 未识别
6. 附：句柄定位（`references/handle-locating.md`）

**只要出现句柄，就必须附定位方法**，否则用户回 CAD 找不到图元。

## 6. 成果格式与不覆盖原图

- **默认输出 DWG 新副本**（`--export auto`）：`<原名>_<后缀>.dwg`，AutoCAD 2018 格式，地块编号 / 建筑退线 / 城市控制线无需再加 `--write-back`；
- 只有本机无法执行 CAD（检测不到 `accoreconsole.exe`）时才回退 DXF，并在报告首屏写明原因，不静默降级；
- **永不覆盖原图**：`--copy-out` 可指定副本路径，扩展名决定格式；`--no-write-back` 只出报告与清单；
- COM 通道同样只出副本：优先 `另存为` 目标 DWG（原文件不动），另存失败才走 DXF 导出链路；脚本不会静默保存用户正在编辑的原图；
- 任何「合并、删除、炸块」类操作一律只作用于副本或内存数据。
- 导出验收：DWG 生成后用 `accoreconsole` 读回校验图元数、图层数与原图一致（见 `lib/export_dwg.py`）。

## 7. 工程数据包（跨子技能传递）

同一方案多次运行时，用 JSON 承载中间结果，避免反复重扫 CAD：

```
<方案目录>/
  project.json      # 标准、单位、源图指纹、生成时间
  parcels.json      # 地块（handle 主键）
  buildings.json    # 建筑（N5 建筑量测算产出：handle/基底面积/层数/层数依据/高度/所属地块）
  parcels_codes.csv # 编码对照表
  five_lines.json   # 控制线成果
```

handle 是唯一主键；跨脚本引用地块一律用 handle，不用序号。

`buildings.json` 由子技能 N5（`up.py massing`）产出，是「建筑」的唯一权威清单：
层数、层数依据、高度、基底面积、所属地块都只在这里算一次。退线与后续子技能应复用它，
不得各自再解析一套层数——否则同一栋楼会出现两个层数。

## 8. 依赖

- 必需：`ezdxf`（DXF 通道）、`pywin32`（COM 通道，仅 Windows）；
- 退线与五线必需：`shapely`。缺 shapely 时**直接报错退出（退出码 5）**，不得降级成近似算法。

```bash
python -m pip install ezdxf shapely pywin32
```

## 9. 配置替换提醒

`layer_spec.json` / `five_lines.json` / `setback_rules.json` / `massing_rules.json` 均内置通用默认值，顶部带
`"_status": "内置通用默认值（未按地方标准校准，可按院内标准替换）"`。运行时会自动在报告首屏提示，
院内标准到位后只替换配置，不改代码。
