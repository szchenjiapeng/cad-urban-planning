# 回 CAD 用句柄定位图元（色块 / 填充 / 多段线）

本技能输出的一切明细（重叠报告、异常清单、待确认图层示例句柄）都带 AutoCAD **句柄（Handle，DXF 组码 5）**。
句柄是图元在图纸内的永久 ID，可直接在命令行用 LISP 定位，**不需要插件、不需要改图**。

## 一、交付时必须带这段说明

只要报告里出现了句柄（`*_overlap.md` 的第一、二节，`土地利用平衡表.md` 第六节异常清单，待确认图层示例句柄），
就必须把下面的定位方法一并给用户——否则用户拿到句柄无法在 CAD 里找到对应色块。
明细报告里由 `render/markdown_report.py` 的 `_locator_section()` 自动生成这段内容，不要手工删掉。

## 二、四种定位方式（按使用频率排序）

### ① 选中并高亮单个图元

```lisp
(sssetfirst nil (ssadd (handent "169A0D")))
```

### ② 高亮 + 居中放大

```lisp
(command "_.ZOOM" "_O" (handent "169A0D") "") (sssetfirst nil (ssadd (handent "169A0D")))
```

### ③ 同时选中一对重叠地块

两侧一起高亮，最容易判断是「重复描图」还是「分层重复绘制」：

```lisp
(sssetfirst nil (ssadd (handent "1026185") (ssadd (handent "1032D27"))))
```

### ④ 循环定位小工具（批量核对时用）

存为 `GH.lsp`，`APPLOAD` 加载（或整段粘进命令行），之后输入 `GH`，逐个粘贴句柄，回车结束：

```lisp
(defun c:GH ( / h e lay)
  (while (/= "" (setq h (getstring T "\n句柄(回车结束): ")))
    (if (setq e (handent (strcase h)))
      (progn
        (sssetfirst nil (ssadd e))
        (command "_.ZOOM" "_O" e "")
        (setq lay (cdr (assoc 8 (entget e))))
        (princ (strcat "\n已定位 " (strcase h) "  图层：" lay))
      )
      (princ (strcat "\n未找到句柄 " (strcase h)))
    )
  )
  (princ)
)
```

## 三、脚本化批量处理

```lisp
(ssget "X" '((5 . "169A0D")))                        ; 按句柄取选择集
(ssget "X" '((0 . "HATCH") (8 . "0703 农村宅基地")))  ; 按图层取全部填充
(entget (handent "169A0D"))                          ; 取图元数据：8=图层、10/20=坐标、62=颜色
```

## 四、定位不到时的排查顺序

1. **图层被关闭/冻结**：定位成功了也看不见。先打开图层；若图纸有布局视口，注意「视口冻结（VP Freeze）」是独立于模型空间的开关——本技能的 `--layer-state layout` 就是按这个口径筛的。
2. **填充只显示轮廓**：`FILLMODE` 设为 `1`。
3. **句柄已失效**：以下操作会让句柄变化或消失——
   - `PURGE` / `AUDIT` / `RECOVER` 后另存；
   - `WBLOCK` 出去再 `INSERT` 回来（块内图元句柄重排）；
   - 把图元复制到**另一个** DWG（句柄按目标图重新分配）。
   此时需要重新跑一次测算，用新报告里的句柄。
4. **句柄不存在于当前图纸**：报告句柄来自本次测算的 DXF 副本，与同一次另存出来的 DWG 一致；若用户同时开着别的图纸，先确认当前文档是目标图。

## 五、与裁剪、缩放有关的提醒

- 本技能只裁剪面积、不改原图元，所以填充的原始几何仍在原位，`ZOOM Object` 一定对得上；
- 若开启了 `--write-back` / `--colorize`，DXF 通道只写 `_annotated.dxf` 副本，原图句柄不变；COM 通道写回后新增的 `AREA_LABEL` 标注是新句柄，不影响原有句柄。
