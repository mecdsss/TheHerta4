# ZZMI 骨骼合并（Merged Skeleton）计划书

> 状态：**已实现（单缓冲 + 骨架分组 + 组内统一骨架直拷 attach），Blender headless 端到端 [PASS]** · 最后更新：2026-08-26
> 2026-08-24 去重/分组确认（已实现）；**2026-08-25 用户拍板：放弃 CB1 校准**：
> ① **单骨骼刚性部件（单权重物体）抓帧重合误并**——不同锚点骨（头顶/前额/后脑发饰/面部）在抓帧姿态下矩阵逐位相同被并成一根；修复：刚性部件命中对追加加权质心门控（<0.05 米才合并），仅拆不并、误拆零代价。
> ② **palette 与渲染 cb1 逐物体 1:1 配对**（用户拍板）——palette 把顶点蒙皮到对象空间、渲染 VS 用 cb1 对象矩阵摆到世界；跨空间引用会被 cb1 摆错位置（身体组与头部组变换差 ≈0.5m，与"头发下沉/身体上移"症状吻合）。修复：**按渲染 cb1 对象变换分组，组内统一骨架**——每组一套全宽骨架，只直拷本组骨骼；**禁止跨组别骨骼合并**（跨组引用导出时大声报警，无校准的运行时槽位永不被写入 = 原点塌陷）。
> ③ **CB1 校准整体废弃（2026-08-25 用户拍板）**：校准版（捕获各组 cb1 + 外来骨骼经 `inv(cb1_组)×cb1_源×M` 校准乘写入）经多轮实测无法稳定（捕获段匹配脆弱、未捕获残留垃圾 CB、校准偏移越调越离谱），全部移除——不再有 cb1 捕获段/捕获源字段/校准 CS，attach 退化为纯直拷（`Toolset/zzmi_merged_skeleton_attach.hlsl`）。
> ④ **未生成组件帧对齐（2026-08-26 修正）**：导出组件在每个 deform pass 即时 attach 当帧 palette，未生成组件继续走游戏原渲染的当帧 palette；两者无需双缓冲或 Present 重放。
> 这两个修正疑似此前"dump 数据层全对但游戏内持续偏移"悬案的根因——dump 总在相似姿态抓取，重合骨/跨空间引用在 dump 里恒"正确"。
> 目标：参照 EFMI 骨骼合并（`common/efmi_skeleton.py`，已端到端验证）的「工作空间反查 FrameAnalysis dump → 数据复制回工作空间缓存」模式，为 ZZMI（绝区零）工作空间提供骨骼合并数据（VGMap/VGOffset/VGCount + 骨骼 palette 缓存）。
> 与 EFMI 的关系：**只复用数据层模式**（log 解析、反查、矩阵去重、写回缓存、复选框门控）；**运行时的 CS 着色器与 INI 段落全部按 ZZZ 的数据存储位置/格式从零编写**（deform pass 挂载、vs-t0 SRV stride 48、per-pass Map + ring buffer 时序、SO 管线），不移植 EFMI 的任何着色器/段落——不同游戏数据布局完全不同。分支选项：复用复选框 `import_merged_vgmap`（「使用融合统一顶点组」），勾选则用骨骼合并，不勾选维持现状，零副作用。
> 依据：对真实 ZZZ FrameAnalysis dump（`K:\SSMT-Package-master\3Dmigoto\ZZZ\FrameAnalysis-2026-08-19-122152`）的完整逆向分析 + **配套测试工作空间 `K:\SSMT-Package-master\WorkSpace\ZZMI\希格莉德·空岛传奇` 核对**（`Config/FrameAnalysisPath.json` 已确认指向同一 dump，draw 索引逐条吻合）。

---

## 1. 背景

### 1.1 已有样板：EFMI 骨骼合并（已完成并验证）

`common/efmi_skeleton.py`（743 行）实现了终末地（EFMI）的骨骼合并：

- `EFMILogParser`：解析 FrameAnalysis `log.txt`（draw 调用 / CB 绑定 first_constant 窗口 / SRV 绑定 / dump 文件名 → deduped 路径映射）。
- `EFMIBoneMapBuilder.get_skeleton_buffer`：反查 instance config（fc=4096 窗口 `[5][0:2]` 段偏移）→ vs-t0 骨骼池 256×12 矩阵。
- `build_vg_maps`：**跨子网格按骨骼矩阵内容去重** → 每子网格 `VGMap`（local→global）。
- `EFMISkeletonMergeHelper.ensure_skeleton_data`：幂等总流程——写回子网格 json（VGMap/VGOffset/VGCount）+ 骨骼池 buf 复制到 `<子网格>/ModImpRuntime/<bare>-BoneMatrix.buf`（NTEMI 缓存模式）。
- 集成：`ImprotFromWorkSpaceFull`（`ui/ui_func_import_ssmt.py:177-198`）中 `logic_name == EFMI and GlobalProterties.import_merged_vgmap()` 双条件触发；失败不阻断导入。

### 1.2 本次目标（用户拍板）

1. ZZMI 复刻同一模式：从工作空间反查 ZZZ dump，找到骨骼数据后**复制回工作空间**，下次直接用。
2. **导入、导出都要实现骨骼合并，且就是 EFMI 那种「统一顶点组」**：所有子网格共用一套全局顶点组（全局骨架），**组件 A 可以用组件 B 的骨骼/权重**（跨部件权重合法）。
3. **直接复用复选框** `import_merged_vgmap`（`common/global_properties.py:172`，不新增 UI、不改默认值）：勾选 = 导入全局顶点组、导出走合并骨架；不勾选 = 两侧都维持现状。

### 1.3 非目标

- 不改 SSMT4 提取端（ZZZ 提取已正确捕获 deform 输入，见 §2.3）。
- 不改 ZZMI 现有导入导出**默认**行为（复选框关闭 = 现状；开启且有 VGMap 数据才走合并路径）。
- 不做「导出时反映射回局部索引」的降级 B 模式（列入后续可选，见 §7）——本期直接做与 EFMI 同构的完整版。

---

## 2. ZZZ 渲染管线实证（FrameAnalysis-2026-08-19-122152）

### 2.1 三段式管线（log.txt 逐 draw 追踪结论）

| 阶段 | draw 范围 | 内容 |
|---|---|---|
| ① CS 形变 pass | 000012-000017、000021-000028 | CS `743108cc03f39cbf` 算 morph/blendshape，写池 `f50c0d31` / `90b64ae0`（UAV） |
| ② pointlist 蒙皮变形 pass | 000001-000011、000018-000020、000029-000036（22 个） | CPU `Map` 上传骨骼 palette → 绑 **`vs-t0`**；VS（`e8425f64cfb887cd` / `9684c4091fc9e35a` / `a0b21a8e787c5a98`）读绑定姿势 vb0 + 权重 vb2 + palette，**SO 流式输出蒙皮后顶点** |
| ③ 渲染 draw（= SSMT 的 DrawIB） | 000037 起 | vb0 直接绑对应 deform pass 的 SO 输出 buffer，**渲染 draw 本身没有骨骼数据** |

**骨骼数据位置结论：在 ② 的 `vs-t0`**（每部件一个 palette SRV，float32 矩阵流，dump 在 `deduped/<内容hash>.buf`）；**权重在 ② 的 vb2**（`BLENDWEIGHTS` + `BLENDINDICES`，palette 局部索引 uint32）。

### 2.2 渲染 IB ↔ deform pass 映射（SO 输出 hash = 渲染 vb0 hash 连接，全部 11 个 IB 命中）

| DrawIB | 顶点数 | deform pass | palette (vs-t0 资源 → 内容 hash) | 权重 layout |
|---|---|---|---|---|
| 84618ee0 | 5846 | 000004 | `f6a6c781` → `f2a54012` | 4×f32 权重 + 4×u32 索引 (2840ec5f) |
| a23aa8a3 | 12314 | 000020 | `c3f98669` → `097b226c` | 同上 |
| 19086112 | 3288 | 000035 | `45c35f5b` → `26be11c4` | 同上 |
| b51bdd59 | 345 | 000036 | `c6c3b31d` → `b4f8f4d8` | 同上 |
| b20f90ea | 4643 | 000002 | `c2f5419a` → `e018278f` | 同上 |
| b30db54e | 1744 | 000008 | `773b317d` → `18fa1c05` | 同上 |
| 48625d6d | 3542 | 000018 | `f43bdd3b` → `6b520746` | 820de055（vb0 来自 CS morph 池拷贝 `a1644290`） |
| d892c658 | 488 | 000010 | `36bb475e` → `531460ff` | 2×f32 + 2×u32 (5cdc3f7c) |
| 64d7d56f | 388 | 000001 | `23de2d6a` → `eaf48535` | 单索引 R32_UINT (4e92e68b/614cf6d8) |
| 454ff522 | 76 | 000029 | `23de2d6a` → `eaf48535` | 同上 |
| add6ff13 | 193 | 000030 | `23de2d6a` → `e6f6fd14` | 同上 |

### 2.3 合并可行性实证

- `64d7d56f`（deform 1）与 `454ff522`（deform 29）的 palette dump **内容完全一致**（`eaf48535`）；`84618ee0`（deform 4）与同帧另一部件（deform 32，1793 顶点）也完全一致（`f2a54012`）——同角色部件共享同一套骨骼矩阵，**跨部件矩阵去重有实际收益，方案成立**。
- ZZMI 工作空间子网格 json 已带 `"GPU-PreSkinning": true`，`CategoryBufferList` 确认提取端捕获的是 deform 输入（POSITION/NORMAL/TANGENT←vb0、TEXCOORD←vb1、**BLENDWEIGHTS/BLENDINDICES←vb2**）。**工作空间缺的只有 palette 与 VGMap**——正是本方案要反查补齐的。

### 2.4 已识别的坑（实现必须处理）

1. **palette buffer 是 ring scratch 复用**：同一资源 hash（`23de2d6a`/`178dab71`/`f6a6c781`/`141c7638`）一帧内被多个 deform pass 重写。**必须按「该 deform pass 的 dump 逻辑文件名」`0000XX-vs-t0=<资源hash>-vs=<deformVS>.buf` 定位 deduped 内容文件，绝不能只按资源 hash 认 palette**。
2. **deform VS 不硬编码 hash**：用结构特征识别 deform pass——`Draw(VertexCount:N)` + 绑了 SO target + 绑了 vs-t0 SRV（VS hash 随版本变）。
3. **morph 部件**（48625d6d）：deform vb0 是 CS 形变池的 CopyResource 拷贝，不是静态 buffer；palette 仍走 vs-t0，不受影响。
4. **同帧多角色**：22 个 deform pass 里只有 11 个属于本角色；必须用 SO-hash join 从工作空间记录的渲染 draw 反挂，防串台（NPC 的 pass 不会被误认）。
5. **渲染 pass 的共享 `vs-t0=7dfb0292`**（内容 `ce0ededa`，所有渲染 draw 相同）：与逐部件骨骼无关，勿误用。
6. ~~矩阵步长待测定~~ **已测定（任务 0 完成，见 §2.5）**：12 floats（4×3，48 字节/骨骼）。

### 2.5 实测数据结论（任务 0，探针脚本 `.dbg/zzmi_palette_probe.py`）

对 11 个 palette buf + 15 个子网格 Blend buf 的实测：

| DrawIB | palette 骨骼数 | BLENDINDICES max+1 | 跨部件去重 |
|---|---|---|---|
| 84618ee0 | 49 | 49（完全相等） | 49 全新 |
| a23aa8a3 | 105 | 105 | 105 全新 |
| b20f90ea | 51 | 51 | 38 新（13 共享） |
| d892c658 | 16 | 16 | 16 全新 |
| b30db54e | 14 | 14 | 8 新（6 共享） |
| b51bdd59 | 11 | 11 | 11 全新 |
| 48625d6d | 10 | 10 | 9 新（1 共享） |
| 19086112 | 7 | 7 | 7 全新 |
| 64d7d56f | 1 | 1 | 0 新（整个重复） |
| 454ff522 | 1 | 1 | 0 新（整个重复） |
| add6ff13 | 1 | 1 | 1 全新 |
| **合计** | **266** | | **全局唯一 244（22 个共享槽）** |

- **步长 = 12 floats（4×3 矩阵，48 字节/骨骼）**：全部 buf 被 12 整除；旋转行单位化、平移量级正常；16-float 解释被内容否决。
- **palette 骨骼数 == 实际用量**（无填充）：`len(skeleton) >= vg_count` 校验以等号成立。
- **合并骨架 266 > 255**：BI4（454ff522/64d7d56f/add6ff13）与 BW8_BI8（d892c658）**升宽是硬需求**，无条件升宽策略正确。
- **拆分子网格共享数据**：48625d6d ×3 / 84618ee0 ×2 / a23aa8a3 ×2 的 Blend.buf 均为全量顶点（同一份），共享 palette 与 VGMap。

**两两重叠与拼接对齐实测**（探针 `.dbg/zzmi_palette_overlap.py`，bitwise 判等）：

- `b20f90ea`(51) ↔ a23aa8a3(105)：**13 根**全同（如 `#0→#9`、`#1→#10`）；拼接后 b20f90ea 只新增 38 根。
- `b30db54e`(14)：→ a23aa8a3 `#0→#9, #1→#10`；→ b20f90ea `#0→#0, #1→#1, #2-#5→#45-#48`。
- `64d7d56f#0` = `454ff522#0` = `b51bdd59#0` = `48625d6d#2`（抓帧瞬间四者矩阵逐位相同）。~~同一根挂饰骨骼四部件共用~~ **2026-08-24 修正**：几何布局取证（`.dbg/zzmi_rigid_parts_probe.py`）显示四者分处头部四个位置——64d7d56f 头顶（z≈1.645）、454ff522 前额薄件（z≈1.573）、b51bdd59#0 后脑物理发饰（y=+0.135，11 根对称骨）、48625d6d#2 面部锚点——是**抓帧重合的不同锚点骨**，游戏内动画（尤其发饰物理）分叉时会错位联动（用户游戏内实测误并，疑似此前"dump 全对但持续偏移"悬案根因：dump 总在相似姿态抓取，重合骨在 dump 里恒"正确"）。**刚性部件质心门控**后：64d7d56f、b51bdd59#0 被拆开各占各槽；454ff522#0 ↔ 48625d6d#2 质心距 0.034 < 0.05 保持合并；全局唯一骨骼 244 → **246**。
- 84618ee0 / a23aa8a3 / 19086112 / d892c658 / add6ff13 的骨骼不与其它部件重叠（独立子集）。
- **关键结论：共享骨骼的索引位置不固定**（b30db54e `#2-#5` 对应 b20f90ea `#45-#48`）——必须按矩阵内容判等去重，按索引对齐会出错。
- 矩阵形态：12 floats = 3×4（3 行单位旋转 + 平移列），旋转行模长全部 = 1.0000（无缩放）。

---

## 3. 数据链路与反查方案

### 3.1 工作空间侧可用线索（以测试工作空间 `希格莉德·空岛传奇` 实测）

- 布局：`LOD0/<drawib>-<index_count>-<first_index>/TYPE_GPU_<gametype>/<子网格同名>.json`（本角色 11 个 DrawIB、15 个子网格：84618ee0 ×2、a23aa8a3 ×2、48625d6d ×3、其余 ×1）。
- `LOD0/ComponentName_DrawCallIndexList.json`：`子网格名 -> [渲染 draw 索引]`（与 log.txt 逐条吻合，如 `b20f90ea-19182-0 -> 000038/000044/000192/000213/000225`）。
- `LOD0/DrawIB-Component.json`：drawib → component 序号 → 子网格名。
- 子网格 json（如 `b20f90ea-19182-0.json`）关键字段：
  - `GamePreset: "ZZMI"`、`GPU-PreSkinning: true`、`WorkGameType`；
  - **`CategoryHash`**：`Position=122883aa / Texcoord=5c0fefda / Blend=bf543990`——**就是 deform pass 的 vb0/vb1/vb2 资源 hash**（最直接的 join 线索）；
  - **`VertexLimitVB: "dd9c8d5e"`**——**就是 deform pass 的 SO 输出资源 hash**（第二条独立 join 线索）；
  - `CategoryBufferList`：Blend buf 文件名 + BLENDINDICES 元素（`R32G32B32A32_SINT`，EFMI 的 `_blend_indices_layout` 已兼容 SINT）；
  - `VGCount: 0 / VGOffset: 0`、无 VGMap——**待本方案回填，与 EFMI 现状同构**。
- `Config/FrameAnalysisPath.json`：SSMT4 已记录 dump 路径（EFMI `resolve_frame_analysis_dir` 三候选回退逻辑可直接搬）。

### 3.2 反查流程（核心算法：三条独立 join 路径，互为校验/兜底）

对每个工作空间子网格：

- **路径 A（最直接）**：子网格 json `CategoryHash.Position` == deform pass `IASetVertexBuffers` slot 0 资源 hash → 命中该 deform pass。（`bf543990` Blend hash 可二次校验。）
- **路径 B（SO 连接）**：子网格 json `VertexLimitVB` == deform pass `SOSetTargets[0]` 资源 hash → 命中该 deform pass。
- **路径 C（draw 索引兜底）**：`ComponentName_DrawCallIndexList.json` 取该子网格的渲染 draw → 读其 `IASetVertexBuffers` slot 0 hash（= SO 输出）→ 找 SO target 同 hash 的 deform pass；可顺带校验 `IASetIndexBuffer` hash == 子网格 drawib。

命中 deform pass 后：

4. **取 palette**：该 deform pass 的 `VSSetShaderResources` slot 0 资源 hash → 组 dump 逻辑文件名 `<draw>-vs-t0=<hash>-vs=<deformVS>` → 经 parser 的 dump_map 拿到 `deduped/<内容hash>.buf` 实际路径。
5. **解析 palette**：.buf 按 float32 流读入，按测定步长（12 或 16，见任务 0）切矩阵；按工作空间 Blend buf 的 BLENDINDICES 最大局部索引 +1 得 `vg_count`，切片到实际用量（对齐 EFMI：`len(skeleton) >= vg_count` 校验）。

> 同一 DrawIB 拆成的多个子网格（84618ee0 ×2、a23aa8a3 ×2、48625d6d ×3）会命中**同一个 deform pass 与同一份 palette**——正常：它们的权重共享同一局部索引空间，VGMap 相同、VGOffset/VGCount 相同，去重天然幂等。

### 3.3 跨子网格去重与回写

- **去重规则（用户拍板 + 实测修正）**：
  1. **同一部件内部绝不去重合并**——同部件 palette 索引是提取端权威分配，内部零重复（实测 11 个部件全部 0 个内部 bitwise 重复），原样保留。
  2. **仅跨部件之间去重合并，且只用 bitwise（字节级）判等，禁用浮点容差**——同一骨骼在同一帧被 CPU 上传到各 palette 时是同一份数据的逐位拷贝（实测共享骨骼跨部件 maxdiff = 0.00e+00）；不同骨骼或同骨骼不同帧位必然不同。
  3. **不同物体的不同编号顶点组，只要被同一骨骼矩阵驱动（bitwise 相同 = 同一骨骼），就去重为同一个全局顶点组**。例：物体 A 的 36 号顶点组与物体 B 的 7 号顶点组若由同一骨骼矩阵驱动，两者都映射到同一个全局 id，导入后在 Blender 里就是**同一个顶点组名称**（实测案例：b20f90ea 有 13 个组与其它部件共享）。
  4. **单骨骼刚性部件（单权重物体）追加加权质心门控**（2026-08-24 用户拍板）：命中对任一方 palette 仅 1 根骨骼时，bitwise 相同还须加权质心距离 < `rigid_centroid_tolerance`（默认 0.05 米）才合并，否则各占各槽。刚性部件的唯一骨骼 = 整个物体的锚点，质心即物体位置指纹——抓帧瞬间重合的不同锚点骨（头部挂件密集区高发）靠此分离。刚性部件误拆零代价（各自 attach 写同一矩阵，运行时内容恒等），误并则动画分叉时错位联动；只拆不并是安全方向。双方均为多骨骼部件时不加门控（多根同时位等不可能是巧合；真共享骨骼驱动区域质心实测可相距 0.25，加门控会误拆真共享）。缺签名时刚性命中对保守拆开。
  5. **骨架分组 + 组内统一（2026-08-24 分组拍板；2026-08-25 移除校准拍板）**：palette 矩阵把顶点蒙皮到**对象空间**（列向量约定 `object = Rm·bind + tm`，12 floats 平移在 [3,7,11]），渲染 VS 用该对象的 cb1 矩阵（rows 0-3 = 对象→世界，行向量约定 `world = object·R + t`）摆到世界。**合并骨架按渲染 cb1 对象变换分组**：变换逐位相同的部件进同组（同空间），组内去重（bitwise + 刚性门控），**骨骼 id 为全局编号（组基址拼接组内槽位）**——Blender 侧组内 join 无歧义。运行时**每组一套全宽合并骨架**（array = 全局 max(vg_offset+vg_count)）：每个 deform pass 立即用 `Toolset/zzmi_merged_skeleton_attach.hlsl` **只直拷本组当帧骨骼**，Present 仅清理到达/绘制标记，不重放 palette，无任何校准乘。**禁止跨组别骨骼合并**：外来骨骼不再经校准乘写入其它组的骨架（曾经校准版 `M' = C×M`、`C = U_目标组⁻¹ × U_源组`，世界不变性虽经单测验证，但 cb1 捕获段在多轮实测中无法稳定——捕获匹配脆弱/未捕获垃圾 CB/校准偏移，2026-08-25 整体废弃）；跨组别引用由导出侧 `_warn_cross_group_bone_references` 大声报警（无校准的运行时这些槽位永不被写入 = 原点塌陷）。json 写回 `SkeletonGroup` + 全局口径 VGMap/VGOffset。无 cb1 可解析的部件独立成组（不共享，安全方向）。实测分组见本文末尾。
- **为什么禁用容差（实测踩坑记录，`.dbg/zzmi_match_accuracy.py`）**：
  - 48625d6d 的 `#1/#8/#9` 与四部件共享骨骼 maxdiff = 3.5e-07/2.5e-06（近似非相等）——是**同一骨骼的不同动画帧姿态**（48625d6d 是脸部 morph 部件，deform pass 18 晚于 pass 1/29/30，动画已推进），容差匹配会把它们误并；
  - a23aa8a3 内部存在 maxdiff = 0.000000 的**不同骨骼**（对称/镜像骨骼浮点同值但位不同），84618ee0 内部不同骨骼最小差异仅 8.7e-04——容差稍大就误并。
  - 结论：bitwise 是精确判据（零误并零漏并），容差两头都出错。
- **算法**：每部件 palette 逐骨骼 `tobytes()` 作 dict key；跨部件命中即映射到 canonical 全局槽位（canonical 按 weighted_vertex_count 选主，复用 EFMI `build_vg_maps` 三遍扫描骨架，但判等改为 bitwise、且跳过同部件对）。产出每子网格 `VGMap{local: global}`、`VGOffset`、`VGCount`。
- **合并骨架内存布局（关键语义）**：每部件 palette 在合并骨架中**按 VGOffset 连续摆放**（VGOffset = 固定排序下前序部件 vg_count 累加），合并骨架总槽位 = Σ vg_count（本角色 266）；去重只决定顶点引用的全局 id 指向哪个 canonical 槽位，**22 个重复槽位是死槽**（无顶点引用，内容恒同，无害）。此语义与 EFMI-Tools 的 vg_offset 完全一致，运行时 CS 只需按 vg_offset 连续拷贝，无需查表。
- **回写 json**：写入各子网格自己的 `<子网格>/TYPE_GPU_<gametype>/<子网格>.json`（VGMap/VGOffset/VGCount 三字段，与 EFMI 回写位置同构）；幂等——已有 VGMap 且非 force 则跳过。
- **复制缓存**：palette buf 复制到 `<子网格>/ModImpRuntime/<子网格>-BoneMatrix.buf`（NTEMI/EFMI 同款缓存模式），并在 json 记录 `BoneMatrixFileName`。下次（或换机器）无 dump 时可直接用缓存。

---

## 4. 模块设计：新增 `common/zzmi_skeleton.py`

完全独立于 `efmi_skeleton.py`（不改 EFMI 一行），复刻其分层与 API 形状，方便对照维护：

| 类/函数 | 职责 | 对应 EFMI 样板 |
|---|---|---|
| `ZZMILogParser` | 解析 log.txt：`Draw(VertexCount:N)`、`SOSetTargets` 及其资源行、`IASetVertexBuffers/IASetIndexBuffer` 及其资源行、`VSSetShaderResources` SRV 绑定、dump 逻辑名 → deduped 路径映射 | `EFMILogParser`（正则/状态机模式直接照搬，新增 Draw/SO/IA 三类记录） |
| `ZZMIDeformResolver` | deform pass 识别（pointlist + SO + vs-t0）与 SO-hash join：渲染 draw → deform pass | 新增（ZZZ 特有，EFMI 无此层） |
| `ZZMIBoneMapBuilder` | palette buf 解析（步长探测）+ `build_vg_maps` 矩阵去重 | `EFMIBoneMapBuilder`（去重算法可直接复用/抽取共用） |
| `ZZMISkeletonMergeHelper.ensure_skeleton_data(workspace_root, target_list, force=False) -> (bool, str)` | 幂等总流程：定位 dump → 解析 → 反查 → 去重 → 回写 + 复制缓存 | `EFMISkeletonMergeHelper.ensure_skeleton_data`（含 `resolve_frame_analysis_dir` 三候选回退，直接搬） |

依赖：仅 `os/re/shutil/numpy` + `utils.json_utils`，无 bpy 依赖（保证可单测、可 Blender headless 跑）。

## 5. 集成点（分支选项，复用复选框）

### 5.1 复选框（不新增）

`GlobalProterties.import_merged_vgmap()`（`common/global_properties.py:172` 「使用融合统一顶点组」，默认 True 保持不变）。语义与 WWMI/EFMI 一致：**开 = 有 VGMap 数据就走合并骨骼；关 = 完全现状**。

### 5.2 数据生成门控（唯一新增的主动作）

`ImprotFromWorkSpaceFull`（`ui/ui_func_import_ssmt.py`）在 EFMI 段（:177-198）之后追加同构分支：

```python
if (
    GlobalConfig.logic_name == LogicName.ZZMI
    and GlobalProterties.import_merged_vgmap()
):
    from ..common.zzmi_skeleton import ZZMISkeletonMergeHelper
    ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
        workspace_root=GlobalConfig.path_workspace_folder(),
        target_list=<工作空间 drawib/component 列表>,
    )
    # 打印结果；失败不阻断导入（对齐 EFMI）
```

复选框关闭 → 整段不执行，零文件读写，绝对现状。

### 5.3 导入消费（走现有双条件路径，最小适配）

现有顶点组导入已是双条件门控：`json 有 VGMap and import_merged_vgmap()` → 全局索引；否则局部索引（`common/ssmt_import_helper.py:37`、`common/mesh_create_helper.py`）。ZZMI 侧只需让 mesh 创建在读子网格 json 时识别本方案写入的 VGMap 段——**有则用、无则现状**，不改动任何默认分支。实施时先核对 ZZMI 导入实际读子网格 json 的位置再定点接入（一个小适配点，不重写导入）。

**分组合集（2026-08-24 实施）**：分组版下，一键导入把每个子网格对象移入其骨架组合集 `SkeletonGroup_<N>`（挂在 LOD 合集下，颜色轮换区分；`ui/ui_func_import_ssmt.py:_zzmi_move_to_skeleton_group_collection`）。json 无 SkeletonGroup 字段（旧缓存/未生成）时保持原合集归属，零副作用。

### 5.4 导出（本期实现：A 模式，与 EFMI 同构的运行时合并骨架）

**闭环原理**：所有子网格共用一套全局顶点组（全局骨架）。导出时 BLENDINDICES **直写全局骨骼 id**；运行时由 INI 挂载的 CS 把各部件的实时 palette 拷进一块**合并骨架 buffer**（按 vg_offset 摆放），并把每个 deform pass 的 vs-t0 换绑到合并骨架——deform VS 拿全局索引直接蒙皮。**任何全局骨骼对任何部件可见，组件 A 可以刷组件 B 的权重。**

门控（与导入同一把复选框）：`LogicName.ZZMI and GlobalProterties.import_merged_vgmap()` 且子网格 json 有 VGMap → 走合并骨架导出；否则**完全现状**。

实现要点（数据层参照 EFMI 已验证模式；**着色器与 INI 段落全部按 ZZZ 数据布局从零编写**，见第 3 条）：

1. **全局索引化导出**：顶点组名 = 全局 id 数字串；导出前预处理——补缺组（fill_gaps）、剔 ignore/全局骨架外组、按名排序/改名保证 `g.group == 全局 id`（对照 NTEMI `_sort_export_vertex_groups_by_name` 与 EFMI ObjectMerger 的 `str(index)` 改名）。挂钩点：`common/submesh_model.py` BLENDINDICES 生成处（EFMI 升宽分支 :89-101 同位置加 ZZMI 分支）。
2. **升宽——实测后取消**：ZZMI 各 gametype 的 BLENDINDICES **本来就是 32 位通道**（BI16=`R32G32B32A32_SINT`、BI8=`R32G32_UINT`、BI4=`R32_UINT`——后缀数字是**字节数**不是位数），全局骨骼 id 直接装下，**无需升宽、无需 ElementFormat 行**（EFMI 的升宽是因为它的 BI4 是 R8 四通道 255 上限，与 ZZZ 无关）。实现已按此落地，导出 buffer 实测通过（见 §8）。
3. **INI Merged Skeleton 段与 CS 着色器——按 ZZZ 数据布局从零编写（不移植 EFMI 实现）**：EFMI 的段落/着色器面向终末地的渲染 draw + instance config 体系，**只借鉴「逐组件 attach + 换绑合并骨架」的概念**，以下要素全部按 ZZZ 实测重新定义：
   - **CS（零延迟版）**：`Toolset/zzmi_merged_skeleton_attach.hlsl`——输入 = 当前 deform pass 的 vs-t0（cs-t0 保存的当帧 palette，`StructuredBuffer<ZZBone3x4>`，stride 48）+ **vg_map 表**（cs-t1，`StructuredBuffer<uint4>`，槽位在 .x；使用二进制文件加载，避免本 fork 的多行 data 只写入第 0 个元素）；输出 = 本组 `RWStructuredBuffer` 合并骨架（全宽，槽位 = 全局骨骼编号）；逻辑 = **按 vg_map 写槽位** `merged[vg_map[id].x] = palette[id]`（本部件引用的骨骼——含跨部件共享 canonical——当帧覆盖）。无 cb1 输入、无校准乘（校准版 CS 2026-08-25 废弃删除）。每个 deform VB 段只在本段执行一次 attach；[Present] 只清理到达/绘制标记，不重放 palette。
   - **INI 匹配键**：挂点段用 `checktextureoverride` 匹配 deform pass 的 **vb0/vb2 hash**——这两个键**工作空间子网格 json 里就有**（`CategoryHash.Position` / `CategoryHash.Blend`），生成器直接取用；NPC 的 deform pass hash 不在列表天然排除。
   - **换绑**：attach 后把该 pass 的 `vs-t0` 换绑到合并骨架 Resource（SRV 视图按 stride 48 声明）。
   - **与 ZZMIv1 的组合**：ZZMIv1 的 skin commandlist 在同一批 deform draw 上做 vb0-3/ib 的 mod 替换（全局索引 vb2 即经此路径生效）与 ps-tXX 清理，**不触碰 vs-t0**；我方段落只做 palette attach + vs-t0 换绑，职责不重叠。commandlist 先后次序与守卫需游戏内验证（任务 7）。
   - **渲染 draw 不动**：渲染 pass 的 vs-t0（共享 `7dfb0292`）与 vb2 维持 ZZMIv1 现状；渲染 VS 是否消费 vb2 需游戏内观察（风险 R3）。
4. **ZZZ 运行时挂载点与帧对齐设计（2026-08-25 定案：零延迟逐 pass attach）**：11 份 palette 由 CPU 逐 pass `Map`（WRITE_DISCARD）上传且 ring buffer 复用。**渲染侧存在当帧角色级绑定矩阵（dump 143256 实证：渲染 vs-cb2 = 身体正向 + 头部逆向绑定表，每帧 Map 更新；渲染 VS/PS 消费当帧绑定；渲染 vs-t0 = 7dfb0292 部件参数表 128 矩阵对）**——"慢一帧"的 SO × 当帧绑定 = 运动时逐帧错位（静止时帧差≈0，故 dump 数据层正常；这正是"只要采用骨骼合并就错位、不合并（SO 当帧）不错位"的根因，用户实测）。因此：
    - **"全部上一帧"（Present 时序 attach）已废弃**：慢一帧与渲染当帧绑定不兼容。
    - **定案 = 零延迟逐 pass attach**：每个 deform pass：`pre` = 把该 pass 当帧 palette **copy 成持久资源** `ResourceZZPalette_<DrawIB>` → **立即 run attach CS**（cs-t1 = vg_map 表，按「局部骨骼 id → 全局槽位」写入本组骨架；本部件引用的全部骨骼——含跨部件共享的 canonical 槽位——此刻即为当帧内容）→ `vs-t0` 换绑为本组骨架 → draw 蒙皮。**deform 读到的 = 当帧姿态**，与渲染当帧绑定一致。逐 pass attach 只需本部件当帧 palette（copy 时刻有效），不依赖"当帧全套并存"（旧设计否决的只是帧尾拿全套）。
    - **2026-08-26 实测修正（渲染顺序不稳定）**：好帧与坏帧的 palette copy 均成功，但同组 target/carrier 到达顺序不同；target 先到时固定 target draw 会读取尚未被 carrier 当帧覆盖的槽位。修复为「逐组件到达标记 + 合并可见 draw 依赖守卫」：carrier/target 挂点都保留 guarded draw，只有所需 palette 全部当帧 attach 后的第一个挂点绘制一次；因此不依赖固定 DrawIB 顺序，也不引入整帧延迟。
    - **帧尾 [Present]**：只清除到达/已绘制标记，**不再重放持久 palette**；后者会把缺席部件/上一实例的内容重新灌入骨架，形成脏数据。
    - 效果：所有部件当帧姿态；首帧即正确（无自愈期）；**未生成组件走游戏原渲染（当帧 palette），与合并部件天然同帧一致，无需任何延迟机制**。
    - 备注：共享骨骼 canonical 槽位被后 deform 部件 attach 覆盖（同帧 bitwise 相同，覆盖无害）；先 deform 部件的 SO 已在 deform 时固定，不受后续覆盖影响。
5. **跨部件/跨组权重**：同组（相同对象空间）直接引用合法——组内统一骨架；**跨组别骨骼合并已禁止**（2026-08-25 用户拍板，无校准）——导出时 `_warn_cross_group_bone_references` 对引用非本组骨骼 id 的部件大声报警（这些槽位永不被写入 = 原点塌陷）。用户只应把同组部件 join 到同一对象。
6. **未生成组件（2026-08-25 定案：零延迟后无需任何机制）**：合并部件 deform 输出当帧姿态，未生成部件走游戏原渲染（当帧 palette）——两者天然同帧一致，**延迟双缓冲机制（`ResourceZZDelayedPalette_<>` / `TextureOverride_VB_ZZDelayed_<>`）已整体废弃删除**。导出子集时未生成部件保持原版渲染即可。

## 6. 任务拆解（TDD，每步可独立验收）

| # | 任务 | 状态 |
|---|---|---|
| 0 | **实证测定**：矩阵步长 = 12 floats（48 字节/骨骼）；palette 数 == 用量；合并骨架 Σ 266 槽 / 全局唯一 244；升宽需求后经实测取消（全 32 位通道） | ✅ 结论在 §2.5 |
| 1 | `ZZMILogParser` + 单测 | ✅ `tests/test_zzmi_skeleton.py`（22 deform pass、SO/vb0/SRV/IB 绑定、dump_map） |
| 2 | `ZZMIDeformResolver`（A/B/C 三 join）+ `ZZMIBoneMapBuilder`（同部件不去重 + 跨部件 bitwise）+ 单测 | ✅ 15 子网格全部命中；48625d6d `#1/#8/#9` 异帧不误并回归测试在列 |
| 3 | `ZZMISkeletonMergeHelper.ensure_skeleton_data`：回写 json + ModImpRuntime 缓存 + 幂等 | ✅ 15 子网格写回；二次运行全跳过；force 重建 |
| 4 | 导入门控（`ui/ui_func_import_ssmt.py`，ZZMI + 复选框）；VGMap 消费走 `create_mesh_from_json` 既有双条件路径（零导入改动） | ✅ |
| 5 | 导出侧：`common/submesh_model.py` ZZMI 预处理分支（补缺/剔 ignore/排序/紧凑改名，`g.group` == 全局 id）；无需升宽 | ✅ buffer 实测：全局 id 正确写入（64d7d56f/454ff522 全为 7；b20f90ea 51 个使用 id 含 13 个跨部件引用） |
| 6 | Merged Skeleton CS + INI（按 ZZZ 从零编写）：`Toolset/zzmi_merged_skeleton_attach.hlsl` + `ExportZZMI` 生成 Constants/Resource(RWStructuredBuffer stride 48)/CustomShader 段 + 逐 deform VB 段注入换绑/attach | ✅ 生成 INI 校验通过；单测 `tests/test_zzmi_merged_skeleton_ini.py`（含跨组引用守卫用例） |
| 6.5 | **移除 CB1 校准（2026-08-25 用户拍板）**：删校准 CS/捕获段/`SkeletonGroupCb1SourceIb` 字段/校准数学测试；attach 退化为逐部件纯直拷；导出侧新增跨组别引用大声报警 | ✅ 单测 + 真实数据 e2e 全绿 |
| 7 | **端到端**：Blender headless（`.dbg/run_zzmi_headless_validation.ps1` + `bl_zzmi_headless_validate.py`）导入+导出 [PASS]；**游戏内实测（ZZMIv1 加载、跨部件权重、帧内一致性）待用户侧执行** | ⏳ headless ✅ / 游戏内待测 |
| 8 | 文档：CONTEXT.md 增补词条（Deform pass / Merged skeleton / VGMap） | ✅ |

**测试数据（用户指定，已确认配对）**：工作空间 `K:\SSMT-Package-master\WorkSpace\ZZMI\希格莉德·空岛传奇` ↔ dump `K:\SSMT-Package-master\3Dmigoto\ZZZ\FrameAnalysis-2026-08-19-122152`（`Config/FrameAnalysisPath.json` 指向一致；其他工作空间可能没有配套提取文件，不作为测试对象）。

## 7. 风险与后续可选

**风险清单**：

- R1（已消解）：矩阵步长 → 实测 12 floats/48 字节，无歧义（§2.5）。
- R2（已设计消除）：跨部件骨骼帧内不一致 → 按 §5.4-4「逐组件到达标记 + 合并可见 draw 依赖守卫」设计消除；不使用上一帧骨架或 Present 重放。
- R3（待游戏内验证）：渲染 draw 绑定的 vb2 也是全局索引版本（ZZMIv1 对渲染 draw 同样替换 vb2）——渲染 VS 不蒙皮（顶点已蒙皮），但若它把 blend 索引用于其他用途（描边/遮罩），全局 id 可能引起异常；任务 7 观察，若中招则只在 deform pass 替换全局索引 vb2、渲染 draw 保留原布局。
- R4（待游戏内验证）：我方 Merged Skeleton 段与 ZZMIv1 skin commandlist 在同一 deform draw 上的执行次序（vb2 替换必须先于/不干扰 vs-t0 换绑）；ZZMIv1 不触碰 vs-t0，静态分析无冲突。
- R5：ring buffer 复用 → 一律按 deform pass 的 dump 逻辑文件名定位 palette，禁止按资源 hash 全局搜索。
- R6：复选框默认 True：ensure_skeleton_data 失败必须静默降级（打印 + 不阻断导入），不得让「无 dump 的老工作空间」导入失败。
- R7（已修复，用户实测踩坑）：**合并物体 + 删面/删空组导致的两类导出事故**：
- R8（已回退）：**双缓冲合并骨架尝试失败（游戏内模型爆炸）**——[Present] 帧翻转 + 前后缓冲条件换绑的写法在 ZZMIv1 运行时下不成立，已回退到单缓冲（`vs-t0 = ResourceZZMergedSkeleton` + draw 后 attach）版本，该版本经两份游戏内 dump 实证数据正确。教训：ZZZ 管线 INI 改动必须游戏内验证，dump 数据正确不等于运行时行为正确。
- R9（已修复，2026-08-26 双帧对照）：**固定 target draw 顺序假设不成立**。好帧 `a23/b20 → b30 target`，坏帧 `b30 target → a23/b20`；两帧 copy 均成功，坏帧读到的是单缓冲内未完成的当帧组合。修复为单缓冲依赖就绪 draw，不使用 R8 已失败的 A/B 翻页。
  - **组号移位塌陷**：合并模式组名=全局骨骼 id，若数字组被删出缺口且预处理做「紧凑化重命名」，缺口之后的全局 id 全部 -1 移位 → 后续部件权重集体错位、模型塌陷消失（用户实测「删了手臂、下半身消失」即此）。修复：`_prepare_zzmi_merged_skeleton_vertex_groups` 无条件补缺 + 不删任何组 + 排序后恒等重命名 + 非数字组名告警（`common/submesh_model.py`）。
  - **对象映射丢失**：join 多个物体后只有幸存名字的部件有对象，其余部件无 DrawCall → 输出空 IB（`ib=null`）→ 游戏内整件消失。修复（三层）：① `ExportZZMI._warn_missing_drawib_parts` 大声报警列出缺失部件；② **占位小三角面自动补齐**（用户拍板）：复选框开启时，缺失部件自动创建 1e-6 三角面占位对象（权重给组 "0"，统一顶点组下恒在范围内），游戏内不可见、不再 ib=null，顺带成为「故意删除部件」的合法手段；导出后自动清理 + 残留自愈；③ **占位不是无条件的**（用户修正）：部分缺失的 DrawIB 直接补；**整个 DrawIB 缺席时**读其 VGMap 全局骨骼 id、检测是否被现存对象顶点实际引用（权重>0）——被引用 = 几何已被合并进别的对象 → 全组件补占位（抑制原版防重影）；零引用 = 用户故意不生成 → 不插桩（该 DrawIB 不进 mod，游戏内显示原版）。无反查数据的缺席 DrawIB 一律不插桩。**更进一步的「合并物体按面来源拆分导出」仍列后续可选。**

**后续可选（本期不做）**：

- **B 模式降级**：导出时按 VGMap 反映射回局部索引（零运行时依赖，但跨部件权重会被越界剔除）——作为无法使用运行时合并骨架环境（如加载端不兼容）时的降级选项，按需另起。
- ~~合并物体按面来源拆分导出~~ **已被更优解取代（用户拍板）**：目标组件继续由**物体前缀**控制（现有体系）；跨组件改派 = 分离面到新物体 + 改前缀；缺失部件由条件占位小三角兜底。已实测：全 join 成一个对象的导出结构自洽（join 目标组件画完整合并网格 drawindexed=122979，其余组件全部不可见占位，无 ib=null/重影），见 `.dbg/bl_zzmi_joined_validate.py`。
- palette 缓存的跨工作空间复用/共享池。
- ZZZ shapekey（CS morph 池）与 mod 的联动（48625d6d 类脸部部件）。

**游戏内 dump 实证记录**（FrameAnalysis-2026-08-22-094614 / 212619 / 224434 / 08-23-001555）：换绑生效（hooked pass 的 vs-t0 无 dump、未 hook 的正常）、attach 偏移与 json 逐一吻合、合并网格内头发顶点用 live palette 重建 ≈ 实际 SO 输出（残差 0~0.0004、零系统性偏移）、各部件渲染 draw 的 CB 窗口完全一致（无逐部件原点差）、跨部件内容位置在带内。**用户游戏内持续观察到的偏移在三份出错 dump 的数据层均不存在**——2026-08-24 起按两个新根因修复（刚性锚点抓帧重合误并 + cb1 对象空间分组）；2026-08-25 用户拍板放弃 CB1 校准（组内统一骨架 + 禁止跨组别合并），等待重导 + 重导出后游戏内复验证；若仍在，回到"偏移正显示的那一帧抓 dump + 截图"的诊断路线。

**2026-08-23 修复链（三帧游戏内 dump 实证：164525 / 170515 / 171955，用户复验通过）**——此前"偏移离谱/炸模/消失"的真根因不在时序，而在 attach CS 的两个运行时细节：

1. **ini 参数布局（本 fork 与标准版不同）**：标准 3DMigoto 的 CustomShader 参数是 4 个一组从 `IniParams[0]` 起（`IniParams[0]=(x1,y1,z1,w1)`，mouse.hlsl/3dvision2sbs.hlsl 实证）；**本 3DMigoto-Armor fork 的 `y1` 在 `IniParams[1].y`**。读错位置的行为差异（同一 CS 三帧实测）：
   - 读 `IniParams[1].y`（=y1）：count=vg_count 正确 → attach 执行；
   - 读 `IniParams[0].y`（=x1=0）：count=0 → attach 一根不写 → **G3 全零 → 蒙皮全部塌向原点 → 模型消失**。
   - 结论：`#define ZZ_ATTACH_COUNT IniParams[1].y`（`Toolset/zzmi_merged_skeleton_attach.hlsl`）。
2. **vg_map 多行 data 只写第 0 个元素**：`[ResourceZZVgMap_<DrawIB>] type=Buffer format=R32G32B32A32_UINT data = <slot> 0 0 0`（每行一个元素）在本 fork 上**只写入第 0 个元素**——CS 其余线程 `vg_map[i]` 越界读到 0 → **全部骨骼塌进 slot 0**，G3 仅 3 槽非零（`[0, 79, 88]`，slot 79/88 = 各部件 palette[0] 写入正确、slot 0 = 塌陷）→ 蒙皮 246/249 骨骼用零矩阵 → **模型炸裂**。修复：导出器把 vg_map 写成**二进制文件**（`Meshes/zz_vgmap_<DrawIB>.buf`，每元素 4×uint32 = 槽位值,0,0,0），INI 改 `filename` 加载（与 VB 资源同一路径，buffer 大小由文件决定，视图必然覆盖全部元素）；HLSL 侧 `Buffer<uint4> vg_map` 与 `format=R32G32B32A32_UINT` 精确匹配（`ui/universal/zzmi.py` `add_merged_skeleton_sections`）。
3. **合并网格自动重定向（任意 IB 挂载兑现）**：palette 是 per-pass 独立 Map 上传的 ring scratch（同资源 hash 帧内两次 dump 内容不同，141c7638→8a40ccd0/0b9416aa 实证），早 deform pass 时刻读不到晚 pass 部件的当帧骨骼——因此**合并网格（同组跨部件 join 成一个对象）物理上只能在组内最后一个 deform draw 蒙皮**。为兑现「用户可自由 join 到任意 IB」的设计承诺，导出器自动重定向（`_build_merged_mesh_redirect_plan`）：carrier（合并网格挂载的 DrawIB）deform 退化为 3 顶点 stub（保留 copy palette + attach 写当帧骨骼）；组内最后 deform draw 的 DrawIB（target）deform 追加画合并网格（绑定 carrier 的 vb0/vb2，SO 按 [target 完整导出顶点（含 stub）][merged...] 拼接，保证 target 的 remapped IB 与 SO 顶点偏移一致）；**render 阶段每个子网格仍使用自己的 hash/first_index 和 IB，carrier 仅显式换绑 `vb0 = ResourceZZRedirectSO_<target>` 并用 `base_vertex` 指向合并 SO，target/缺失部件保留极限小三角占位，不以 `ib=null` 静默跳过**；VertexLimitRaise 按 SO 实际大小重排（carrier=3 / target=Σ）；合并网格的渲染换绑 `vb1 = Resource<carrier>Texcoord`（导出顶点超原部件顶点数时，防 OOB UV）。支撑数据：json 新增 `DeformDrawIndex`/`OriginalVertexCount`（`common/zzmi_skeleton.py` 反查写回；幂等门控纳入，旧缓存自动整批刷新），透传到导出侧（`common/submesh_model.py`/`common/submesh_metadata.py`）。无法自动重定向的情形（缺 DeformDrawIndex / 配置跨 IB）由 `_warn_merged_mesh_timing` 大声报警并给出改名指引。
4. **游戏内复验（用户确认 2026-08-23）**：同组内合并（组 3 全部部件 join 成一个对象）蒙皮正确、不再炸裂/消失。G3 骨架由「3/249 槽非零」修复为全量写入（170 槽左右，分布 79..248）。
5. **边界/调度泛化修复（2026-08-24 审计）**：attach CS 曾残留测试角色专用的 `slot < 249`，会把 G4 的 249..265 全部拒写；INI 又固定 `Dispatch = 8`，在单部件 palette 超过 512 根时会漏写尾骨骼。现改为用 `src_palette`/`vg_map`/`merged_skeleton.GetDimensions` 校验真实资源边界，输出槽位上限随每组全宽骨架的 `array` 自动变化；Dispatch 按 `ceil(vg_count / 64)` 生成。回归测试覆盖 G4 全 17 槽与 513 根 palette。

**分组实测（dump 122152，按渲染 cb1 对象变换，2026-08-24 分组定案口径）**：5 组——
| 组 | 部件 | 对象变换平移（row3） | 全局槽位范围 |
|---|---|---|---|
| G0 | 19086112 | (-15.537, 2.381, -5.661) | 0..6 |
| G1 头部 | 454ff522 / 48625d6d / 64d7d56f / b51bdd59 | (-15.212, 2.115, -5.556) | 7..29 |
| G2 头发 | 84618ee0 | (-15.209, 2.047, -5.561) | 30..78 |
| G3 身体 | a23aa8a3 / b20f90ea / b30db54e（共享 13/2/6 根） | (-15.223, 1.585, -5.513) | 79..248 |
| G4 | add6ff13 + d892c658（同空间） | (-15.459, 1.815, -5.629) | 249..265 |
全局合计 266 槽（组基址拼接组内槽位）；组内去重后唯一骨骼总计 244；每组运行时骨架全宽 array=266，**只直拷本组骨骼（无校准，2026-08-25 起）**。cb1 提取口径（仅用于分组键）：dump 行逐 draw 反查 vs-cb1（绑定调用是持久状态、多数 draw 不重发，不可靠），只解析 ≤512B 的逐部件块（>512B 是多对象共享变换数组+窗口索引，首条未必是本 draw 的对象），rows 0-3 即对象→世界矩阵（w 列 0/0/0/1 校验）。

## 8. 验收标准

1. 对测试工作空间 `希格莉德·空岛传奇` 执行导入：15 个子网格全部生成并写回 VGMap/VGOffset/VGCount；palette buf 落入各 `<子网格>/ModImpRuntime/`。
2. 去重结果与实测一致：5 个骨架组（身体/头部/头发/19086112/add6ff13+d892c658），全局骨骼编号按组基址拼接（0..6/7..29/30..78/79..248/249..265，Σ 266）；头部组内 `454ff522#0` ↔ `48625d6d#2`（质心距 0.034）合并为全局槽 7、`64d7d56f` 头顶件与 `b51bdd59#0` 后脑发饰骨被刚性门控拆开（槽 18/19）；身体组 b20f90ea 38 新 + 13 共享等（对照 §2.5）；**同部件内部零合并；48625d6d 的 `#1/#8/#9`（同骨骼异帧）保持独立不被误并；跨组部件的骨骼分占各组槽位，运行时各自组内直拷（无校准）**。
3. 幂等：二次导入不重复反查（跳过并提示）；`force=True` 可重建。
4. **统一顶点组**：复选框开导入后，所有子网格对象共用同一套全局顶点组（组名 = 全局骨骼 id）；**同组部件可互刷权重（组内统一骨架）；跨组别引用被禁止——导出时 `_warn_cross_group_bone_references` 大声报警**；导入对象按 `SkeletonGroup` 归入对应 `SkeletonGroup_<N>` 合集。
5. **导出合并骨架**：复选框开 + 有 VGMap 时，导出 vb2 的 BLENDINDICES = 全局骨骼 id；INI 含 Merged Skeleton 全套（逐组件到达标记 + 每组全宽 `ResourceZZMergedSkeleton_G<N>`（array=266）+ 逐部件直拷 attach CustomShader（`cs = ./res/zzmi_merged_skeleton_attach.hlsl`，无 cb1 引用）+ 合并可见 draw 依赖守卫 + [Present] 仅清理标记）。**INI 不得含任何 cb1 捕获段/捕获资源/校准着色器引用，也不得在 [Present] 重放持久 palette attach**。
6. **游戏内实测**：导出的 mod 在 ZZMIv1 加载端下姿态正确，同组跨部件权重生效（每个 deform pass 的 vs-t0 使用当帧合并骨架；依赖就绪后只绘制一次合并可见几何，帧内无部件间不一致）；渲染 draw 无 R3 异常。
7. 复选框关闭：导入与导出行为均与现状完全一致（无 VGMap 读写、无 ModImpRuntime 写入、BLENDINDICES 走 `g.group` 原路径、无 Merged Skeleton 段）。
8. 单测全绿 + Blender headless e2e [PASS]。
