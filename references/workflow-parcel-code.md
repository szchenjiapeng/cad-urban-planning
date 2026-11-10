# 子技能 N2：地块编号 / 用地代码上图

触发词：地块编号、地块编码、编号、批量编号、地块号、编码对照表、按顺序编号、用地代码上图、按代码分图层。

## 1. 三种上图模式（先问清要哪一种）

| `--mode` | 用途 | 标注内容 | 是否编序号 |
| --- | --- | --- | --- |
| `seq`（默认） | 地块编号 | 序号 + 用地代码，如 `001-1402` | 是 |
| `code` | 只写用地代码 | 如 `1402` | **否** |
| `none` | 只出清单 / 只分图层 | 不写标注 | 否 |

配合 `--layer-mode`：

| `--layer-mode` | 效果 |
| --- | --- |
| `keep`（默认） | 地块图元留在原图层 |
| `code` | 把地块图元移到 `--code-layer-template`（默认 `YD-{code}`）命名的新图层，即「按代码分图层」 |

用户说「只把用地代码写到新图层」「按图层写」「不编号」时，用 `--mode code`，不要硬加序号。

## 2. 必问场景（退出码 3）

确认项分两轮汇总，**同一轮里必须一次问完**（`answer_mode: "batch"`），答完用 `--answers` 一条命令重跑：

| 轮次 | 时机 | 可能出现的确认项 |
| --- | --- | --- |
| 第一轮 | 扫描前，零成本 | `standard_choice`（图层成片旧代码）、`number_rule`（`--mode seq` 且未给 `--template`） |
| 第二轮 | 扫描后，一次性 | `unregistered_code` / `unknown_code`（图层代码没登记）、`small_parcels`（有地块 < `--min-area`） |

1. **编号规范**：`--mode seq` 且未显式给 `--template` 时必问。默认规则是 `{seq:03d}-{code}`（如 `001-1402`）；用户可选「用默认 / 自己给规则 / 只写代码不编号」。
2. **小地块**：存在面积 < `--min-area`（默认 1 ㎡）的地块时必问（`--small-parcel-policy ask`）。选项：照常编号 / 不编号仅列清单 / 调整阈值重跑。**不得默认丢弃。**
3. 图层名含未登记代码（如 `0905`）→ 写错 / 新增代码 / 归入已有类；
4. 用地图层能识别但代码缺失 → 继续编号留空 / 跳过 / 先修图；
5. 图纸成片出现 GB 50137 旧代码 → 按旧标准 / 换算 2023 / 坚持新标准。

## 3. 命令

```bash
# A. 编号（默认规则 001-1402），出 DWG 副本
python scripts/up.py code --file 总图.dxf --template "{seq:03d}-{code}" \
  --small-parcel-policy number --export dwg --out 地块编码成果.md

# B. 只写用地代码，并把地块按代码分图层
python scripts/up.py code --file 总图.dxf --mode code --layer-mode code \
  --export dwg --out 用地代码上图成果.md

# C. 街坊号 + 序号
python scripts/up.py code --file 总图.dxf --template "{prefix}-{block:02d}-{seq:03d}" --sort block --block-grid 200

# D. 只出表，不动图
python scripts/up.py code --file 总图.dxf --template "{seq:03d}-{code}" --dry-run --out 编码清单.md
```

直连 CAD 时把 `--file` 换成 `--source com`。

## 4. 参数

| 参数 | 说明 |
| --- | --- |
| `--mode seq\|code\|none` | 上图模式，默认 seq |
| `--layer-mode keep\|code` | 是否按用地代码分图层，默认 keep |
| `--code-layer-template` | 分图层模板，默认 `YD-{code}`，支持 `{code}{name}` |
| `--template` | 编号模板，支持 `{code}{name}{seq}{block}{prefix}`；**不给则先询问**，默认 `{seq:03d}-{code}` |
| `--small-parcel-policy ask\|number\|skip` | 小于阈值地块的处理，默认 ask（必问） |
| `--min-area` | 小地块判定阈值，默认 1 ㎡ |
| `--sort row\|block\|area\|handle` | 排序；`row` = 从上到下、从左到右，默认 |
| `--block-grid` | 街坊网格边长（米），`sort=block` 时分区用 |
| `--start` | 起始序号，默认 1 |
| `--label-layer` / `--label-height` / `--label-gap` | 标注图层（seq 默认 `BZ-地块编号`，code 默认 `BZ-用地代码`）、字高（米）、最小间距 |
| `--text-style` | 文字样式名；不给则沿用图纸默认样式（Standard） |
| `--font` / `--bigfont` | 字体文件（`simfang.ttf`、`gbcbig.shx` 等）；给了会自动建/改样式 |
| `--width-factor` | 文字宽度因子，窄条地块可设 `0.7` 压缩字宽 |
| `--short-code on\|off` | 是否把纯两位数字图层当地块，默认 off |
| `--unknown-code ask\|seq_only\|skip` | 代码缺失时的处理，默认 ask |
| `--unregistered-code CODE=skip\|map:目标` | 未登记代码的处理方式 |
| `--scope-layer` | 只对指定图层围合范围内编号 |
| `--codes-file` | 已有编码表（CSV，首列编号） |
| `--export auto\|dwg\|dxf\|none` | 成果格式，默认 auto：有 CAD 出 DWG，没有才出 DXF |
| `--copy-out` | 指定成果副本路径（扩展名决定格式） |
| `--no-write-back` | 只出表，不写标注也不导出（等价旧行为的“不写回”） |
| `--dry-run` | 只出表，不写图也不导出 |

导出**默认开启**：不需要再写 `--write-back`（该参数保留为兼容别名）。

## 4.1 字体（CAD 的字体挂在「文字样式」上）

TEXT 的字体不是图元属性，而是它引用的**文字样式（Text Style）**的属性：

- 不指定样式 → 全部落到 `Standard`，改字体等于改全图 Standard 文字，风险高；
- 要单独控制编号字体，必须建一个专用样式（如 `地块编号`）并赋给这些文字；
- 命令：`--text-style "地块编号" --font simfang.ttf`（仿宋）；
  SHX 走 `--font txt.shx --bigfont gbcbig.shx`；
- 窄条地块可用 `--width-factor 0.7` 压字宽，比单纯降字高更有效；
- 报告「文字样式」一行会写明实际生效的样式、字体与宽度因子；样式创建失败会显式标注并回退默认。

## 5. 排序与可复现

- `row`：按街坊网格分块 → 块内 y 降序、x 升序 → **handle 兜底**；
- 同一输入两次运行必须得到完全相同的编号（回归测试已锁死）；
- 不要用「图形顺序」或「选择集顺序」排序，那不可复现。

## 6. 校验

| 校验 | 处理 |
| --- | --- |
| 重复编号（仅 seq 模式） | 进异常清单，阻断交付；code 模式同代码重复属正常 |
| 代码缺失（code 模式） | 无法只写代码，进异常清单 |
| 代码未识别 | 编号保留，代码列留空，进清单 |
| 几何无效（自交） | 进异常清单，给句柄 |
| 未参与（退化/小于阈值） | 报告第六节逐条列出句柄、面积与原因 |
| 未识别图层 | 单列清单，说明原因（范围线/图框/无代码） |

## 7. 成果（默认 DWG）

导出链路：`--export auto` 时优先 DWG，只有本机无法执行 CAD（无 accoreconsole）才回退 DXF：

| 格式 | 条件 | 终点 |
| --- | --- | --- |
| DWG | 检测到 `accoreconsole.exe` | `<原名>_地块编号.dwg`（AutoCAD 2018 格式） |
| DXF | 无 CAD 或显式 `--export dxf` | `<原名>_地块编号.dxf`，报告注明回退原因 |

- **永不覆盖原图**，只生成副本；`--copy-out` 可指定路径；
- 附带 Markdown 报告与 JSON，含编号↔句柄↔面积↔代码对照表。

## 8. 复核话术

交付时说清五件事：上图模式（编号还是只写代码）、编号总数、代码未识别几个、有没有重号、成果是 DWG 还是 DXF（以及为什么）。带句柄的异常必须附定位方法。
