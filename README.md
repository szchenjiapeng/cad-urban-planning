# 城市规划 CAD 助手

[![最新版本](https://img.shields.io/github/v/release/szchenjiapeng/cad-urban-planning?label=version&color=blue)](https://github.com/szchenjiapeng/cad-urban-planning/releases)
[![许可证](https://img.shields.io/github/license/szchenjiapeng/cad-urban-planning)](https://github.com/szchenjiapeng/cad-urban-planning/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)

面向城市规划总图阶段的算量与绘图辅助。给一份总图（DWG / DXF，或 CAD 里已打开的图），
直接产出**可复核的成果**：Markdown 表 + 新的 DWG 副本 + 可回 CAD 定位的图元句柄。

原图永不覆盖。

## 能做什么

按总图工作流排序，五个子技能可单独使用，也可串起来做：

| 编号 | 子技能 | 你会怎么说 | 主要成果 |
| --- | --- | --- | --- |
| **N1** | 用地面积测算与土地利用规划平衡表 | 用地面积、色块面积、用地平衡表、用地占比、人均用地 | 一级类平衡表 + 二级类明细 + 合计校验 + 未识别/异常清单 |
| **N2** | 地块编号 | 地块编号、批量编码、用地代码上图、编码对照表 | 编号标注 + 编码对照表 + 按代码分图层 |
| **N3** | 建筑退线 | 退线、退距、建筑控制线、压线检查 | 低多层/高层两级控制线 + 可建面积 + 压线清单 |
| **N4** | 城市控制线 | 控制线、五线、红/绿/蓝/紫/黄线 | 各类控制线 + 冲突校验 + DWG 副本 |
| **N5** | 建筑量 / 容积率测算与批量建模 | 建筑量、容积率、建筑密度、层数、批量建模、拉伸、体块 | 容积率测算表 + `BZ-容积率` 标注 + `JZ-体块` 三维模型 |

用地分类支持《国土空间调查、规划、用途管制用地用海分类指南（2023）》与 GB 50137-2011，
包括图层名直接写代码、旧标准换算、中文原名、混合用地（如 `100104+0909`）。

## 环境要求

| 项 | 要求 |
| --- | --- |
| 操作系统 | Windows（可直连运行中的 AutoCAD）／ macOS、Linux（走 DXF 通道） |
| Python | ≥ 3.9 |
| 必需依赖 | `ezdxf` |
| N3 / N4 / N5 额外依赖 | `shapely` |
| 直连 CAD 额外依赖 | `pywin32`（仅 Windows） |

```bash
pip install ezdxf shapely pywin32
```

导出 DWG 需要本机安装 AutoCAD（使用其自带的 `accoreconsole`）；没有时会自动改用 DXF，
并在报告里写明原因，不会静默降级。

## 三步上手

以下命令都在**技能目录**下执行（命令里的 `python` 若指向不同版本，用你自己的解释器路径）：

**1. 看一眼环境和图层**

```bash
python scripts/cad_env_check.py --file 总图.dxf --list-layers
```

**2. 跑一个子技能**（以用地平衡表为例）

```bash
python scripts/up.py balance --file 总图.dxf
```

**3. 看成果**

```
总图_成果/
├── 总图_用地平衡表.md      ← 报告
└── 总图_用地平衡表.dwg     ← 新副本，原图不动
```

> 报告默认写到「图纸所在目录 / 图名_成果 /」，不会散落到你当前的工程目录。
> 只想要图纸和 JSON、不要 Markdown，加 `--no-report`。

## 五个入口

```bash
python scripts/up.py balance   # N1 用地平衡表
python scripts/up.py code      # N2 地块编号
python scripts/up.py setback   # N3 建筑退线
python scripts/up.py fivelines # N4 城市控制线（别名 control）
python scripts/up.py massing   # N5 建筑量/容积率 + 批量建模（别名 far / extrude）
```

统一入口与直接调脚本的参数完全一致，例如 `python scripts/land_area_report.py --file 总图.dxf`。

## 成果长什么样

每份 Markdown 报告都包含：

- 本次的口径（单位、分类标准、图层范围、规则来源）
- 主表（平衡表 / 对照表 / 容积率表），带**合计校验**
- 用地占比、人均用地等派生指标
- **未识别图层清单**：哪些图层没被识别为用地、为什么
- **异常清单**：每个异常都带图层名 + 图元句柄，可直接回 CAD 定位

## 遇到疑问时怎么答

技能不会替你猜口径。当参数没给全、或图纸存在歧义时，它会**一次把问题问完**（最多两轮），
而不是问一个、跑一遍、再问一个。

答完用一条命令重跑，不用重打一长串参数：

```bash
python scripts/up.py setback --file 总图.dxf --answers answers.json
```

```json
{
  "setback_basis": "parcel",
  "setback_rules_source": "accept_default",
  "setback_options": "accept"
}
```

## 适用范围与边界

**适用**：总图阶段的用地面积核算、用地平衡表、地块编号、建筑控制线、城市控制线、
批量容积率/建筑密度测算与体块生成。

**不在本技能范围**：方案强排、日照分析、管线综合、竖向与土方、报建面积核算
（本技能的面积口径为简单计容口径，不等同于报建面积）。

## 常见问题

**Q：面积差了 100 万倍？**
看 `$INSUNITS`。AutoCAD 的图形单位常被误设成毫米/无单位，技能会做尺度检查并在
跨度异常时停下来问你，但最稳妥的做法是先确认图纸单位。

**Q：提示「没有找到可编号的地块」？**
地块必须由闭合多段线或填充表达，且图层名里带用地代码（或通过映射表指定）。
块参照、外部参照需要先炸开。

**Q：报告里出现「未识别图层」？**
说明这些图层的名字里没有可识别的用地代码。可以在报告里看到完整清单，
再按 `references/layer-spec.md` 的命名建议调整图层名，或用映射表指定。

**Q：Mac 上能用吗？**
能，走 DXF 通道（`--source dxf`）。直连运行中的 AutoCAD 需要 Windows + `pywin32`。

## 许可与免责

- 本技能的图层规范、控制线口径、退距数值均为**内置通用默认值**，未按任何地方标准校准。
- 输出的控制线、退线、容积率成果**必须经设计人员复核**，在按当地标准校准前不得直接作为报建依据。
- 原图不会被修改；所有成果写入新文件。使用直连 CAD 方式时，你的 CAD 当前文档会切换到成果副本。
- 未经授权不得二次分发。商业使用请与作者联系。
