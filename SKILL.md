---
name: cad-urban-planning
display_name: 城市规划CAD助手
display_name_en: Urban Planning CAD Assistant
version: 1.0.0
description_zh: 城市规划总图阶段的算量与控规辅助。给一份总图（DWG/DXF 或 CAD 中已打开的图），输出可复核的 Markdown 成果与新的 DWG 副本，原图永不覆盖。五项能力：用地面积测算与土地利用平衡表、地块编号、建筑退线、城市控制线、批量建筑量/容积率与体块建模。按《国土空间调查、规划、用途管制用地用海分类指南（2023）》或 GB 50137-2011 识别用地代码。
description_en: Urban Planning CAD Assistant for land-use balance tables, parcel numbering, building setback lines, and urban control lines, classified per China's 2023 guide or GB 50137-2011.
description: 城市规划总图阶段的算量与控规辅助。输入 DWG/DXF 图纸或 CAD 中已打开的图，输出可直接复核的 Markdown 成果（表 + 合计校验 + 未识别图层清单 + 异常清单 + 图元句柄）以及新的 DWG 副本，原图永不覆盖。五项能力：土地利用平衡表、地块编号、建筑退线、城市控制线、批量建筑量/容积率测算与批量建模。支持从运行中的 AutoCAD、已安装 CAD 或 DXF 取数，按《国土空间调查、规划、用途管制用地用海分类指南（2023）》或 GB 50137-2011 识别用地代码（含图层名直接写代码、旧标准代码、中文原名、混合用地），未识别的图层与异常图元会逐条列出，可回 CAD 定位。触发词：用地面积测算、色块面积统计、填充面积、用地平衡表、土地利用规划平衡表、地块面积统计、用地占比、人均用地、居住/公共管理与公共服务/商业/工业/道路/绿地用地面积、总图面积核算、地块编号、地块编码、批量编号、建筑退线、退距、建筑控制线、压线检查、城市控制线、控制线、红线、绿线、蓝线、紫线、黄线、管制线、建筑量、容积率、建筑密度、层数、批量建模、体块、拉伸、CAD 图纸算量、DXF/DWG 图纸分析。
---

# 城市规划CAD助手（Urban Planning CAD Assistant）

面向城市规划总图阶段的算量与绘图辅助：给一份总图（DWG/DXF，或 CAD 里已打开的图），
产出**可复核的成果**——Markdown 表 + 新的 DWG 副本（原图永不覆盖）+ 可回 CAD 定位的图元句柄。

五个子技能，按总图工作流排序，均为稳定版：

| 编号 | 子技能 | 用户会怎么说 | 主要成果 | 入口脚本 | 详细工作流 |
| --- | --- | --- | --- | --- | --- |
| N1 | 用地面积测算与土地利用规划平衡表 | 用地面积、色块面积、用地平衡表、用地占比、人均用地 | 一级类平衡表 + 二级类明细 + 合计校验 + 未识别/异常清单 | `land_area_report.py` | `workflow-land-area-balance.md` |
| N2 | 地块编号 | 地块编号、批量编码、用地代码上图、编码对照表 | 编号标注 + 编码对照表 + 按代码分图层 | `parcel_code.py` | `workflow-parcel-code.md` |
| N3 | 建筑退线 | 退线、退距、建筑控制线、压线检查 | 低多层/高层两级控制线 + 可建面积 + 压线清单 | `building_setback.py` | `workflow-building-setback.md` |
| N4 | 城市控制线 | 控制线、五线、红/绿/蓝/紫/黄线 | 各类控制线 + 冲突校验 + DWG 副本 | `urban_five_lines.py` | `workflow-five-lines.md` |
| N5 | 建筑量 / 容积率测算与批量建模 | 建筑量、容积率、建筑密度、层数、批量建模、拉伸、体块 | 容积率测算表 + `BZ-容积率` 标注 + `JZ-体块` 三维模型 | `building_massing.py` | `workflow-building-massing.md` |

依赖：`ezdxf`（DXF 通道必需）、`pywin32`（直连 AutoCAD，仅 Windows）、`shapely`（N3/N4/N5 必需）。

统一入口（等价于直接调脚本，参数完全一致）：

```bash
python scripts/up.py balance   # N1 用地平衡表
python scripts/up.py code      # N2 地块编号
python scripts/up.py setback   # N3 建筑退线
python scripts/up.py fivelines # N4 城市控制线（别名 control）
python scripts/up.py massing   # N5 建筑量/容积率 + 批量建模（别名 far / extrude）
```

## 子技能路由

| 编号 | 用户说什么 | 读这份文档 | 跑什么 |
| --- | --- | --- | --- |
| N1 | 用地面积、平衡表、用地占比、人均用地 | `workflow-land-area-balance.md` + `cli-land-area.md` + `rules-land-area.md` | `up.py balance` |
| N2 | 地块编号、批量编码、编码对照表 | `workflow-parcel-code.md` | `up.py code` |
| N3 | 退线、退距、建筑控制线、压线 | `workflow-building-setback.md` | `up.py setback` |
| N4 | 控制线、五线、红线、绿线、蓝线、紫线、黄线 | `workflow-five-lines.md` | `up.py fivelines` / `control` |
| N5 | 建筑量、容积率、建筑密度、层数、批量建模、拉伸、体块 | `workflow-building-massing.md` | `up.py massing` / `far` / `extrude` |

通用文档（五个子技能共用）：

| 场景 | 读这份文档 | 说明 |
| --- | --- | --- |
| 图层命名不规范、图层直接写代码、新老标准混用 | `layer-spec.md` | 先读规范再跑 |
| 单位、数据通道、确认项、成果格式等通用口径 | `contract.md` | — |
| 图纸打不开、算不出、面积异常 | `troubleshooting.md` | 按条目排查 |
| 成果里的句柄怎么回 CAD 定位 | `handle-locating.md` | 按图层+句柄选中 |

五个子技能共用 `lib/`（`cli` / `confirm` / `answers` / `layer_spec` / `landuse` / `shapes` / `offset` / `labels` / `report`；
退线另用 `line_layers`（线条图层候选与指派） / `road_level`（等级判定，宽度不采信） / `building_class`（低多层/高层分级） / `parcel_sides`（临绿地水系边段））。
**用地代码解析一律委托 `core.classify.LayerMapper`**，不得另写一套。

> 配置：`assets/` 下的 `layer_spec.json`、`five_lines.json`、`setback_rules.json`、`massing_rules.json`
> 已内置通用默认值（`_status` 字段标明来源）。各地标准不同时替换对应文件即可，无需改代码；
> 脚本会在报告与 JSON 里写明本次实际使用的配置来源（`rules_source` / `config_source`）。

## 环境自检（条件触发，不要每次都跑）

**先直接跑主命令**：只有主命令返回退出码 `4`/`5`、或没给 `--file` 又不知道 CAD 是否在运行时，才回头做环境自检。小图直接跑比「先体检再跑」少一次往返。

```bash
python scripts/cad_env_check.py --file <图纸.dxf> --list-layers
python scripts/cad_env_check.py            # 未给 --file 时探测运行中的 AutoCAD
```

- `recommended_channel` 有三种取值：`com` = 已有 CAD 在运行，直连活动图纸；`com-start` = 本机装了 CAD 但未运行，运行时自动启动；`dxf` = 本机无可用 CAD，必须用 `--file` 指定 DXF。
- `cad_installed` 与 `com` 是两个独立信号：前者查注册表判断是否安装（不启动进程），后者才是真正连上了活动文档。
- `ezdxf` 为空说明缺依赖，先执行 `python -m pip install ezdxf`，不要假设已安装。
- **退线与城市控制线还需要 `shapely`**：`python -m pip install shapely`。缺 shapely 时平衡表只能粗算裁剪，而退线/控制线直接以退出码 5 中断，不做近似降级。
- 顺便用 `--list-layers` 拿到图层清单，这是判断映射可行性的第一手依据。

## 平衡表：先读三份文档

平衡表细节已拆到 references，**执行前必读**：

| 文档 | 内容 |
| --- | --- |
| `references/workflow-land-area-balance.md` | 四阶段流程、范围识别两步法、对话示例、复核要点 |
| `references/cli-land-area.md` | 全部参数速查 |
| `references/rules-land-area.md` | 硬性规则完整版（填充、范围、反算、重叠、分类折算） |

最小可用命令：

```bash
python scripts/up.py balance --file 总图.dxf --total-area 128.60 --population 32000 --out 平衡表.md
python scripts/up.py balance --source com --write-back      # 直连活动 CAD 并回写
python scripts/up.py balance --file 总图.dxf --scope none   # 确认无规划范围
```

三条最容易出事的口径：默认**只算填充**（`--primitive hatch`）；规划范围必须**先认图层名再取边界**，认不到就出 `scope_not_found` 确认项；道路用地靠**差额反算**并带守卫，不要手工填数。

## 通用硬性规则（所有子技能）

- **只统计已打开图层**：参与统计的图层限定为**模型空间开启且未冻结**（`--layer-state layout` 时再叠加视口冻结），关闭/冻结图层一律不计入，并在报告「图层可见性」里列出跳过清单。`--layer-state all` 只用于复核“关掉的图层里有多少量”，其结果不得作为交付口径，用了必须在报告里标注。
- **单位必须交代**：报告头部写明单位判定依据；`$INSUNITS=0` 时是启发式推断，必须提示用户复核。面积内部统一按平方米，输出公顷与平方米双单位。`$INSUNITS` 与实际绘图单位不一致是常见事故（本图米制图标成 `$INSUNITS=4` 毫米，全表差 10⁶ 倍且不报错），因此按当前单位解释后若「整图跨度 <100 m 却有 20 个以上图元」必须输出 `unit_suspect` 确认项（退出码 3）先问用户，不得直接出表。
- **未闭合不计入**：首尾间距超过容差的图元进异常清单，不参与合计；不要为了凑总数强行闭合。
- **未识别不静默归类**：未命中映射的图层进「待确认图层」，面积不计入表内。禁止猜测归类，也禁止把未识别面积摊到其他类目；唯一例外是平衡表的「道路反算」，且反算公式里必须先把未识别面积减掉。
- **代码解析只有一套**：用地代码一律走 `core.classify.LayerMapper`（整名 exact → 混合拆段 → 点名代码 → 分类表中文原名 → regex → keywords），新子技能不得自建规则，否则同一张图会出现两套代码。
- **图层名里的代码点名优先**：图层名出现分类表中真实存在的完整代码时，直接归到该代码，不得降级到上位类——`090101` 必须归 090101（不能是 0901 或 09），`0804-中小学` 归 0804；`0807+090101` 拆段后新建 `0807+090101` 行（08 下的二级），不得识别成 0807、090101 或 08。图层**只有中文名、没有代码**时按分类表原名映射（如 `二类城镇住宅用地` → 070102）；名称与表内不一致时同样先问用户。实现上「点名最长代码优先」，两位代码（01/07）只有整个图层名就是它时才算，避免「标注-01」被当成耕地。
- **图层名里的代码没登记必须先问，不许关键词兜底出表**：图层名出现「形如分类代码、但分类表里没有登记」的片段（如 `090201`、`0909`、`0809`）时，脚本输出 `unregistered_code` 确认项、退出码 3 中断，**未确认前不生成成果**。必须原样问用户两件事：① 图层名是否写错（写错就改图层名后重跑）；② 名称无误时，是否把 `CODE 名称` 登记为新增用地代码。回填方式：同意新增 → `--add-code "0909:商业服务业混合用地:2:09"`（写入分类配置与指引表）；归入已有类 → `--unregistered-code "090201=map:0902"`；本次不处理 → `--unregistered-code "090201=skip"`（报告标需复核，不得交付）。事故案例：`090201 研发设计用地` 未登记时被 0802 的「研发」关键词抢走 88.78 ha，导致 08 类虚高、09 类虚低。
- **混合用地有段对不上也要先问**：混合图层里某段既不是表内代码、也不是表内名称时，脚本输出 `hybrid_unresolved` 确认项（退出码 3），问「名称写错 / 新增代码 / 只按首段统计」。选「只按首段统计」需加 `--ignore-hybrid-unresolved`，报告会标需复核，不得直接交付。混合段解析**只认「代码点名」与「分类表中文原名」**：纯中文描述性段落（如 `0902+100104 商务金融+研发设计混合用地` 里的「研发设计混合用地」）不作为独立一段，否则会被 0802 的「研发」关键词抢成第三段。
- **分类代码不杜撰**：代码与名称以 `references/landuse-2023.md`、`assets/classification.*.json` 为准，未收录的类目先查官方原文再补配置，宁可留空标「未收录」。分类表可用 `scripts/build_classification_from_docx.py` 从指南 Word 原文表 2.1 重建，不要手抄代码。
- **非官方代码要标注**：如 0709 商住混合用地（官方表 2.1 未列，按用户要求扩展），配置里带 `source` 字段，报告说明节必须注明其非官方来源。
- **确认项一次问完（平衡表最多两轮）**：脚本把同一轮能判定的问题汇总成一份清单（`answer_mode: "batch"`，`count` 为条目数，默认 `--confirm-mode batch`）。第一轮=旧标准换算 / 未登记代码 / 规划范围 / 单位 / 图层可见性 / 填充裁剪通道；第二轮=图斑级未登记代码 / 混合用地未解析 / 跨图层重叠闸门。会话必须把 1..N 项在**同一条回复里一次问完**，再把用户选中选项的 `args` 合并成**一条命令重跑一次**；禁止发现一个问一个、反复打断用户。超大图想省掉「先扫再问」那次扫描可加 `--confirm-mode early`（回到多轮询问）。
- **答完用 `--answers` 一次重跑，不要手工拼参数**：每个确认选项都带 `id` 与 `args`。用户答完后，把选中选项的 `args` 收集成 `answers.json`（对象或数组皆可，例如 `{"basis": "parcel", "accept-default-rules": true, "unregistered-code": ["1709=map:1701"]}`），一条命令重跑：`python scripts/up.py setback --file 总图.dxf --answers answers.json`。`code` / `setback` / `fivelines` / `massing` 四个子技能与统一入口都支持；应答文件写错或选项 id 对不上会以退出码 `2` 明确报错，不会被误当成「待确认」而多扫一次图。
- **旧标准必须先问**：图层名出现 `R2`、`B1`、`A33`、`S1` 等旧代码（或成片 `R/A/B/M/W/S/U/G` 开头）时，先向用户确认「是否按 GB 50137 统计并换算为 2023」，不要自己默认一种口径闷头算。确认后：要换算用 `--standard gb50137 --convert-to 2023`，坚持新表则 `--standard 2023`。脚本检测到旧代码图层会输出 `standard_conversion` 确认项并以退出码 3 中断。
- **给句柄必须给定位方法**：凡报告中出现句柄（明细报告、异常清单、待确认图层示例句柄），交付时都要附「回 CAD 用句柄定位图元」的操作说明（`handent` 高亮、`ZOOM Object` 居中、一对句柄同选、`GH.lsp` 循环定位、失效情形排查），完整内容见 `references/handle-locating.md`；明细报告由 `render/markdown_report.py` 的 `_locator_section()` 自动生成该章节，不得手工删除。否则用户拿到句柄无法在 CAD 里定位色块。
- **桥位口径与桥宽上限必须问用户**：蓝线执行前先量出本图「水域面最远配对距离」，输出 `blue_bridge_gap` 确认项（退出码 3，默认 60 m），并写明**超过该值的桥位上下游不会连接**；「与道路共线的河口线识别不出断口」「岸线配不上对」时再输出 `blue_bridge_mode` 确认项，默认 A（贯通）。禁止静默按分段边界出图。详见 `references/workflow-five-lines.md`。
- **成果默认出 DWG 新副本，不覆盖原图**：地块编号、建筑退线、城市控制线**默认就导出**（`--export auto`），生成 `<原名>_<后缀>.dwg`；只有本机无法执行 CAD（无 `accoreconsole.exe`）才回退 `<原名>_<后缀>.dxf`，并在报告与 JSON 写明回退原因，不静默降级。`--copy-out` 可指定路径（扩展名决定格式），`--no-write-back` 只出报告不导图；平衡表的 `--write-back` 仍按需显式开启。
- **报告默认跟着图纸走**：`--out` 不填时写到 `<图纸目录>/<图名>_成果/<图名>_<子技能>.md`（地块编号、退线、控制线、容积率、平衡表同规则），不再落到当前工作目录——否则报告与 `land_area_confirm.json` 会散落在用户的工程目录里。只要图纸与 JSON 时加 `--no-report`。
- **退线基准必须先解释再问**：`parcel`/`road`/`both` 三者差别（退哪条界线、反映什么、适用场景、退距来源）必须完整讲给用户，再让用户选；`--basis` 不填时脚本输出 `setback_basis` 确认项（退出码 3，带 `basis_options` 与本图道路等级实测），**禁止只丢三个词就让用户决定**。没有道路红线图层时要明说 `road`/`both` 不可用。详见 `references/workflow-building-setback.md` 第 1 节。
- **退距规则来源必须先问**：`assets/setback_rules.json` 是内置通用默认值（`_status` 标明来源），退距直接决定控制线位置，各城市差异大。用默认文件且未加 `--accept-default-rules`、未给 `--distance-override` 时，脚本输出 `setback_rules_source` 确认项（退出码 3，`detail.values` 列出当前低多层/高层/临绿地/道路取值）；用户确认后加 `--accept-default-rules`，报告与 JSON 必须写明来源（内置默认值已确认 / 用户提供数值 / 院内标准文件）与限制说明。**不得直接按默认值闷头出成果**。另需说明：规则里按用地性质的条目（如商业 3m）在当前按高度取距口径下不生效。
- **退线六项口径同样必须先问**：建筑图层（空图层陷阱）、绿地水系是否出线、高度分级、杂线阈值、环清理策略、规划范围裁剪，未逐项指定时脚本输出 `setback_options` 确认项（退出码 3，`detail.items` 每项带 evidence 与对应 flag）；一次性接受用 `--accept-default-options`，报告「口径确认」行会写「按推荐值执行」并列出每项取值。
- **其他子技能的默认数值同样要确认，不许默认照搬**：控制线的 `assets/five_lines.json` 是内置通用默认值，用默认配置且未加 `--accept-default-rules` 时抛 `five_lines_rules_source`（`detail.line_values` 列出各线来源/缓冲/是否成图）；平衡表在 `--unit auto` 下单位由 `$INSUNITS` 自动判定，尺度守卫拦不住「标成毫米但看起来合理」的情况，未显式 `--unit` 时抛 `balance_params`（`detail.span_by_unit` 给出 m/mm/cm 解译对照），批量跑用 `--accept-default-params` 并在报告记「未人工复核」。所有确认结果都要进报告与 JSON（`rules_source` / `config_source` / `notes`），禁止静默按默认值交付。
- **退线成果必须做图形核查与清理**：写出的每条环都必须是闭合多段线；无效环（自交/顶点不足）、内环碎线（可建范围内部的洞）、面积 <10㎡ 或最小短边宽 <5m 的杂线一律删除；**重线只在同一输出图层内判定**（完全重合/被覆盖），低多层线与高层线之间的重合（如临绿地水系同为 5m）属规则结果、不算重线、不去重；同层部分共线重叠默认只进清单不删线。删除一律逐条留痕进报告「八、图形核查与清理」，不得静默丢弃。详见 `references/workflow-building-setback.md` 第 9 节。

## 建筑量测算与建模（N5）专项硬规则

- **口径必须说成「简单计容」**：建筑量 = 基底面积 × 层数，只含主体建筑；未扣阳台、架空层、设备层、地下车库，也未做规范折算。不得表述为报建口径，交付话术必须原样带这句。
- **层数缺失默认 1 层，但要单列清单**：`砼` 单独出现按 1 层；无任何层数/高度信息也按 1 层，并在报告「校验」里写明「N 栋按 1 层计入（容积率可能被低估）」。层数与高度文本冲突时**以层数为准**，高度只做交叉校验。
- **层高必须先问**：未给 `--floor-height` 也未给 `--floor-height-policy` 时输出 `floor_height` 确认项（退出码 3）。默认住宅 3.0m / 已识别非住宅 4.5m / 用地代码未识别 3.0m。层高同时改容积率与体块高度。
- **图层必须由用户拍板，不能只按名字认**：未显式指定图层且存在歧义（建筑组 ≠1、有「塔楼/裙房/楼/栋」等近义层、≥2 个图层含闭合轮廓、有空图层、范围组 ≠1、有近义范围层、范围候选含影像、用地代码未登记）时，必须先输出 `layer_assignment` 确认项（退出码 3），把每个候选的图元数/闭合面数/面积/是否影像/是否空图层摆给用户；`--accept-layers` 才允许跳过。典型陷阱：`选取现状建筑` 是空图层、`建筑肌理0429`/`总平面0422` 是 IMAGE，而真建筑在 `1-202512塔楼/裙房` 这种不含「建筑」二字的图层里。
- **单位先验，别信 `$INSUNITS`**：米制方案图标成毫米（`$INSUNITS=4`）时，建筑面积被乘 1e-6 全部低于阈值，最后报「没有建筑」。先看规划范围包围盒跨度（2 km 量级是米，2e6 量级才是毫米），必要时显式 `--unit m`。
- **未登记代码的图层不得当地块，但也不许静默丢弃**：`1-202512塔楼` 的 `202512`、`总平面0422` 的 `0422` 会被误读成用地代码，把建筑算成用地面积。只收「识别出真实用地代码」的图层，其余输出 `unregistered_code` 确认项（退出码 3）让用户判断：改图层名重跑 / 登记新增代码 / `--unregistered-code "C=map:目标"` 归入已有类 / `=skip`（面积仍进表、代码留空标需复核）。静默剔除会让容积率分母凭空变小。
- **地块筛选按建设用地口径**：识别到用地性质时**只对建设用地（2023 为 06–16）编号并出容积率/建筑密度**，其中**绿地与开敞空间用地（14，GB 50137 为 G）默认也不编号、不出指标**；非建设用地（01–05、17–24）同样不编号。识别不到性质时**只对有建筑落位的地块编号出指标**，没建筑的空地块不编号。两种情况都必须在报告开头与「校验」节说明，并给出剔除清单（面积 + 句柄，`parcel_filtered`）；剔除地块上的建筑单列 `building_on_filtered_parcel`，不得静默计入。用户另有要求时用 `--parcel-filter all|construction|built`、`--parcel-classes`（白名单）、`--parcel-exclude-classes`（追加排除）覆盖。**该口径只作用于本子技能的编号与指标，不影响平衡表把绿地计入建设用地面积**；建设用地范围取分类表 `balance_groups.construction`，换标准自动跟随。
- **混合用地按合成代码计，面积只算一次**：`100104+0909新型工业+商业服务业混合用地` 在地块表、超限清单、`BZ-容积率` 标注里一律显示 `100104+0909`（与平衡表同一口径），但**用地面积按整块几何只计量一次**，绝不按段拆分重复计容；层高按首段代码（`100104` → 非住宅 4.5m）。报告固定有「混合用地与用地性质确认」一节，列出「图层 → 合成代码 → 各段代码 → 未解析段」，交付时逐条对照。图面想用首段代码可把模板改成 `{seq}-{code_first}-FAR{far:.2f}`。
- **图上完全没有层数标注必须先问**：全图扫不到 `砼/F/层` 时，按 1 层算出的「容积率」等于建筑密度，不能交付。先问层数，再用 `--floors` / `--floors-by-layer` 指定；报告「层数来源」必须写明各图层层数与命令行补了几栋。
- **容积率只算规划范围内**：默认按「规划范围/设计范围」图层裁剪地块与建筑（`--scope-layers` 可指定），`--scope-mode none` 只用于复核，不得作为交付口径。
- **超限必须单列**：`--far-limit`（默认 6.0）以上的地块单列清单，便于复核层数假设与建筑归属。
- **单图层必须先告知偏差**：建筑组为空即判为「疑似单图层」，必须先输出偏差风险声明（结果可能有较大偏差、必须人工核对）并让用户确认，才可加 `--auto-detect-buildings` 启用几何+文字候选识别；不得静默把闭合多段线都当建筑。单图层通常也没有独立用地边界，容积率算不了，改用 `--no-parcel` 只出全图建筑量合计。
- **跨地块建筑必须告知**：默认按代表点整栋归属（与退线一致），但必须在异常清单列明「跨地块、可能影响相邻地块容积率」；可选 `--cross-parcel clip|exclude`。
- **同一图层「填充 + 多段线」不得重复计算**：建筑采集统一 `source="both"` + `dedupe=True`（几何级去重：小面被大面覆盖 ≥90% 且面积比 ≥60% 判重复，保留填充优先，重复项进 `building_duplicate` 清单）。**不要用 `source="auto"` 的图层级跳过**——它会把「同层只用多段线表达」的建筑整层漏掉。
- **只有闭合轮廓才拉伸**：闭合三态 `closed_flag`（可直接拉伸）/ `snapped`（容差吸附，进「需人工闭合」清单）/ 未闭合与自交（不拉伸，进异常清单给句柄）。不得为了凑数量强行闭合后拉伸。
- **体块是否按塔楼/裙房分层必须先问**：图上建筑图层已区分塔楼/裙房时，问「体块分两个图层 / 合并单层 / 改按高度重分」；图上未区分时，问「是否按高度分层（>24 m 塔楼、≤24 m 裙房）」。确认项 `extrude_layering`（退出码 3），`--extrude-layering ask|keep|by-height|none` 覆盖，阈值 `--tower-height`（默认 24 m）。归层优先级：**源图层点名 > 高度阈值 > 裙房兜底**；`keep` 但图上没分时自动回退按高度并在报告注明。阈值判的是「层数 × 层高」的简单计容高度，不是报建高度，交付话术必须说明。纯测算、`--dry-run`、`--extrude-format none` 不问。
- **无 CAD 时的建模形态必须问用户**：有 CAD 走 `AddRegion` → `AddExtrudedSolid` 出真 `3DSOLID`；无 CAD 只能出顶面+侧面 `3DFACE` 体块网格（非 ACIS 实体），必须由用户选「体块网格 / 只出清单 / 换机器重跑」，不得静默降级。

## 失败处理

- 连不上 AutoCAD、命令卡住、填充面积读不到、裁剪 bypass、面积异常、DWG 转换、乱码、缺依赖等，一律查 `references/troubleshooting.md`（第 1–12 节）。
- CAD 未运行：脚本会按需自动启动本机已安装的 CAD，首次启动可能需要 10–30 秒；不想启动就加 `--no-start-cad`。
- 退出码含义：`0` 成功 / `2` 参数错误 / `3` 待确认（必须问用户）/ `4` 环境或超时 / `5` 数据质量阻断。
- 缺 `shapely`：退线与控制线直接中断（退出码 5），先 `python -m pip install shapely`，不要试图绕过。
- 新技能专属问题（编号 0 个、编号重复、退线无结果、控制线某条未生成）见 `troubleshooting.md` 第 12 节。

## 扩展新子技能

1. 在 `lib/` 新增或复用业务模块（取数、几何、判定），入口脚本保持薄；
2. **用地代码解析必须走 `core.classify`**，不要在新模块里另写一套识别规则；
3. 入口脚本放 `scripts/`，并在 `scripts/up.py` 的 `COMMANDS` 里注册子命令；
4. 在 `references/` 新增 `workflow-<子技能>.md`，写清触发条件、输入、执行步骤、成果形态与复核要点；
5. 在 `tests/fixtures/make_fixtures.py` 补一份最小 DXF，并在 `test_new_skills.py` 加用例；
6. 在本文「能力清单」加一行，然后跑两套回归：`test_regression.py` + `test_new_skills.py`。

## 资源索引

- `references/contract.md`：所有子技能共用的取数、单位、确认协议、退出码、成果骨架。
- `references/layer-spec.md`：图层规范定位、用地识别七级优先级、新老标准、院内标准替换清单。
- `references/workflow-land-area-balance.md`：平衡表完整流程、范围识别两步法、对话示例、复核要点。
- `references/cli-land-area.md`：平衡表全部参数速查。
- `references/rules-land-area.md`：平衡表硬性规则完整版。
- `references/workflow-parcel-code.md`：地块编号（模板、排序、校验、成果）。
- `references/workflow-building-setback.md`：建筑退线（基准、规则、压线校验、常见坑）。
- `references/workflow-five-lines.md`：城市控制线（原「城市五线」；来源写法、派生规则、冲突校验）。
- `references/workflow-building-massing.md`：批量建筑量/容积率测算与批量建模（简单计容口径、层数与层高、单图层候选、闭合三态、拉伸双路线）。
- `references/landuse-2023.md` / `references/landuse-gb50137.md`：两套分类标准参考表与汇总口径。
- `references/layer-naming-and-mapping.md`：图层命名习惯、映射规则语法、未识别图层调优方法。
- `references/troubleshooting.md`：COM 连接、DWG 转换、单位、闭合、编码、依赖等常见问题（含新增子技能专项）。
- `references/handle-locating.md`：回 CAD 用句柄定位图元的完整方法（`handent` 高亮、`ZOOM Object`、一对句柄同选、`GH.lsp` 循环定位脚本、批量选择集、句柄失效排查）。
- `assets/`：分类树与图层映射 JSON 样例，可复制修改后用 `--mapping` / `--classification` 指定；`layer_spec.json` / `five_lines.json` / `setback_rules.json` / `massing_rules.json` 为内置通用默认值，可按院内标准替换。
- `scripts/up.py`：子技能统一入口（`balance` / `code` / `setback` / `fivelines` / `massing` / `far` / `extrude`）。
- `lib/`：新增子技能共用业务层（图层规范、用地识别桥、轮廓收集、偏移、标注、确认项、报告骨架；`building_massing` 建筑归属与指标、`extrude` 闭合校验与拉伸编排）。
- `scripts/cad_env_check.py`：环境自检与图层清单。
- `scripts/clean_package.py`：打包前清理字节码并统计包体（`--check` 供 CI，发现字节码返回 1）。
- `scripts/build_package.py`：按 frontmatter 版本生成 `dist/` 包，并自检「SKILL.md 引用是否齐全、有无字节码、脚本能否编译」。
- `scripts/land_area_report.py`：N1 用地面积测算与土地利用规划平衡表主入口。
- `scripts/parcel_code.py`：N2 地块编号批量生成。
- `scripts/building_setback.py`：N3 建筑退线。
- `scripts/urban_five_lines.py`：N4 城市控制线（文件名保持不变，兼容旧调用）。
- `scripts/building_massing.py`：N5 建筑量/容积率测算与批量建模（`--task measure|extrude|all`）。
- `assets/massing_rules.json`：层高默认值、单图层候选判据与权重、面积阈值、成果图层与标注模板（内置通用默认值）。
- `scripts/add_classification_node.py`：登记新增用地代码（同步分类配置与指引表）。
- `scripts/build_classification_from_docx.py`：维护工具，从 Word 原文重建分类配置（`--layout 2023` 指南表 2.1 / `--layout gb50137` 表 2.0.1）。
- `scripts/build_conversion_from_pdf.py`：维护工具，从新老代码对照表 PDF 生成换算表（依赖 pdfplumber）。
- `tests/test_regression.py`：回归测试（代码点名优先、混合取首段、分级加和、报表只出一张表），不需要 CAD。
- `tests/test_new_skills.py`：新子技能回归（图层识别七级、编号可复现、退线可建面积与压线、控制线与蓝线冲突、容积率混合用地合成代码、平衡表零回归）。
- `tests/test_docs.py`：文档引用完整性检查（SKILL.md 引用的 references 必须存在）。
- `tests/fixtures/make_fixtures.py`：生成回归用最小 DXF（`codes_only` / `parcels` / `setback` / `five_lines` / `massing` / `single_layer`）。
