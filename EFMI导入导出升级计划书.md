# EFMI 导入/导出升级计划书

> 状态：**调研完成，方案待用户确认** · 最后更新：调研闭环后
> 目标：把 EFMI-Tools（SpectrumQT，明日方舟：终末地模组工具）中「骨骼合并（Merged Skeleton）」等能力，按当前项目 TheHerta4 的 SSMT 架构合并、优化、升级到本项目的 EFMI 导入/导出链路。
> 依据：两份子代理深度报告（`docs/efmi-tools-参考插件报告存档.md`、`reports/efmi_pipeline_report.md`）+ 本计划书调研阶段对 SSMT4 提取端、EFMI 数据类型配置、实际工作空间与 FrameAnalysis dump 的实证核对。
> 原则：**先彻底搞清楚两边的数据来源与流程，再动手**。本计划书即「搞清楚」的产物，也是后续实施的唯一依据。

---

## 1. 背景与目标

### 1.1 双方插件

| | 参考插件 | 当前项目 |
|---|---|---|
| 名称 | EFMI-Tools（v0.6.2 / efmi v1.4.1） | TheHerta4（SSMT 系） |
| 位置 | `J:\QQ缓存\文件\EFMI-Tools` | `E:\代码\TheHerta4` |
| 游戏 | 明日方舟：终末地（Arknights: Endfield） | 多游戏类型，含 EFMI（终末地） |
| 数据来源 | EFMI 提取端 dump 的整对象目录（Metadata.json + Component N.fmt/.vb/.ib） | SSMT 工作空间（子网格 json + 分类 buffer），可反查提取数据（NTEMI 已验证） |
| 特色 | 骨骼合并（Merged Skeleton）、LOD BlendRemap、shapekey 批次、Jinja2 模板 ini | 蓝图节点工作流、跨 IB（骨骼跨 IB）、着色器替换、分支 GUI |

### 1.2 目标

1. 把参考插件的「骨骼合并」能力合并进当前项目的 EFMI 导入/导出。
2. 修复当前 EFMI 导出的已知断路（ENCODEDDATA 隐患）。
3. 数据补齐遵循当前项目惯例：**工作空间优先，缺什么从提取数据反查，并把拿到的数据复制回工作空间缓存**（NTEMI 已验证的模式）。
4. 不破坏现有 EFMI 工作流（蓝图、跨 IB、着色器替换、HTMI 映射）与其它游戏类型。

### 1.3 用户已拍板的决策（2026-08 确认）

- **范围**：全量 —— ENCODEDDATA 导出修复 + BLENDINDICES 升宽 + EFMI 骨骼合并导入收尾（空组清理/校验）+ 导出前顶点组预处理。
- **宽度策略**：EFMI 合并骨架场景下 BLENDINDICES 无条件升宽（对齐参考插件合并模式约定）。
- **LOD BlendRemap**：暂不移植（当前 EFMI 链路无 LOD buffer 概念，加载端约定未确认）。
- **数据补齐方式**：工作空间缺数据时从提取数据反查，复制回工作空间缓存（用户明确要求）。

### 1.4 决策确认记录（2026-08 二次确认）

| 决策点 | 结论 |
|---|---|
| 升宽目标格式 | **`R16G16B16A16_UINT`**（参考插件正统约定；配套 INI `vb2->ElementFormat(BLENDINDICES,0)=R16G16B16A16_UINT` 强制读取；加载端可更新到支持 EFMIv1 运行时，但本方案不依赖其新机制，ElementFormat 是 3Dmigoto 基础语法） |
| 骨骼数据来源 | **方案 A：Blender 侧反查**（读 FrameAnalysisPath.json → 分析 Zmd dump cb4 骨骼矩阵 → 生成 VGMap 写回工作空间 json + 复制骨骼 buffer 到 ModImpRuntime 缓存） |
| 行为开关 | **复用 `import_merged_vgmap()`**（默认 True，与现有 WWMI 语义一致） |
| INI 体系 | **保留现有 TheHerta4 INI 格式**（TextureOverride 静态绑定 + Resource 段 + 跨 IB + 着色器替换），**不照搬**参考插件的 EFMIv1 运行时绘制体系（Object_ReadConfig/Component_DrawInstances/空间识别） |
| LOD / BlendRemap / shapekey 池 / 运行时 MergedSkeleton 段 | **明确不做**（用户：不需要 LOD 相关内容，尽可能保留现有 INI 格式） |
| 加载器 | 用户确认可更新（当前 Zmd Core/EFMI 是旧版，缺 EFMIv1 全量 API）；但本方案仅用 ElementFormat 基础语法，加载器更新与否不影响落地 |

> ⚠️ **调研修正（已确认）**：用户早前拍板"无条件 R16"→ 后改为 BI16 型 R32（基于"加载端不认 R16"前提）→ 现确认加载端可更新，**最终改回参考插件正统的 `R16G16B16A16_UINT`**（导出 buffer 16 位 + INI ElementFormat 强制 16 位读取）。

---

## 2. 两套管线对比总览

| 环节 | 参考插件 EFMI-Tools | 当前项目 TheHerta4(EFMI) | 差异/缺口 |
|---|---|---|---|
| 数据来源 | 整对象目录：Metadata.json + Component N.fmt/.vb/.ib（自带 vg_map，v4+） | SSMT 工作空间：`<drawib>-<component>-<n>/TYPE_<gametype>/{json,buf,ib}`；json 无 VGMap | 当前缺骨骼合并数据 |
| 提取端 | 自带提取流程（帧 dump → RawObject → MigotoObjectBuilder → build_vg_map） | SSMT4 提取端 `efmi3.rs`（**不生成 VGMap/VGOffset/BoneMatrix**；WWMI/NTEMI 提取端有完整实现可参照） | 提取端缺口 |
| 导入 | 按 component 建 mesh；MERGED 用 vg_map 重映射（vg_remap 查找表）+ 删空组 + 版本校验 | 按 drawcall 建对象；json VGMap + import_merged_vgmap 双条件才走全局映射（EFMI 现状不满足）；空组清理不含 EFMI | 缺 EFMI 骨骼合并导入 |
| 导出 | ObjectMerger join 整模（VG 全局索引化：补缺/剔 ignore/越界/改名 str(index)）→ 按 .fmt 切 buffer | 每对象独立 SubMeshModel → 分类 buffer；`blend index = 顶点组列表 index`；无跨子网格重映射 | 缺导出侧骨骼合并 |
| BLENDINDICES 宽度 | Merged 强制 min=max=2（R16 系，含 stride 重算） | BI4=R8G8B8A8_UINT >255 **两处 Fatal**；BI16=R32G32B32A32_UINT；`_allow_wide_blendindices_for_remap` 仅 WWMI/NTEMI | 待升级 |
| ENCODEDDATA | 完整编解码（TBN 三路 → 10-10-10-2 打包） | 解码已有（TBNCodec）；**导出分支被注释 = 断路隐患**（当前类型无此元素不触发） | 待修复 |
| 空组/ignore/补缺 | 导出前 fill_gaps + 剔除 + 改名 | 蓝图有手动节点（node_vertex_group_process），EFMI 流程未自动串联 | 待升级 |
| LOD | LOD buffer + BlendRemap 表（mod 端骨骼导入 CS） | 无 | 暂缓 |
| 反查机制 | 无此概念（数据自带） | NTEMI 有完整闭环（FrameAnalysisPath.json → deduped 反查 → ModImpRuntime 复制缓存 → 骨骼合并后处理）；**EFMI 不消费** | 待移植 |

---

## 3. 参考插件 EFMI-Tools 流程详解

**完整报告见 `docs/efmi-tools-参考插件报告存档.md`（含全部文件:行号）。本节为要点浓缩。**

### 3.1 数据来源

- `object_source_folder` = 一个对象的源目录：`Metadata.json` + 每组件 `Component N.fmt/.vb/.ib` + 贴图 + `TextureUsage.json`。
- 由插件自带提取流程从 3DMigoto 风格帧 dump 生成（`extract_frame_data/` → `object_extractor/`：RawObjectExtractor 按 DrawIndexedInstanced 调用收集 → MigotoObjectBuilder 组装组件/贴图/vg_map）。
- **Metadata.json 关键字段**：`format_version`（v4 才有 vg_map）、`weigthing_type`（EXPLICITLY_WEIGHTED/IMPLICITLY_WEIGHTED/NOT_WEIGHTED）、`rotation`、`components[]`（每个含 `vg_offset/vg_count/vg_map`（local→global）、`cpu_posed`、`lods[]`（含 `vg_map`（full→lod，方向相反）、`vb_formats`））、`shapekeys`、`export_format`。
- **vg_map 生成**（提取端 `migoto_object_builder.py:386-522`）：跳过 CPU-posed → 从 instance config CB 读骨骼偏移 → vs-t0 纹理读每骨骼 4×3 矩阵 → **按矩阵内容去重**（相同骨骼矩阵合并为同一全局 id）→ 三遍扫描选 canonical → 写 `vg_map[local]=global`。

### 3.2 导入

`EFMI_Import` → `import_object`：校验（MERGED 需 v4 + Explicit 权重）→ 逐组件：`vg_remap = np.array(list(vg_map.values()))`（仅 MERGED + dedupe_bones + 非 cpu_posed）→ `DataModelEFMI.set_data`：ENCODEDDATA0 解码（10-10-10-2 → 八面体法线）→ 各语义 converter（mirror/rotate/flip_texcoord_v/**converter_apply_lookup(vg_remap)**）→ BlenderDataImporter 建网格/UV/顶点组（组名 = 全局 id 序号）。最后 `skip_empty_vertex_groups and MERGED` → 删空组（按 index 倒序，保持剩余组 index 不变）。

### 3.3 导出

`EFMI_Export` → `ModExporter.export_mod`：读 Metadata → 校验 → 逐组件：
1. **ObjectMerger**（骨骼合并核心）：按 `component[_ -]*(\d+)` 正则挑对象 → 复制 TEMP → transform_apply → 应用修改器（保留 shapekey）→ 三角化 → **fill_gaps 补缺组 → 删 ignore/越界组（index >= Σvg_count）→ 全部改名 str(vg.index)** → join 成 TEMP_EXPORT_OBJECT。
2. **build_data_buffers**：buffers_format 来自 `export_format + Component N.fmt + LOD vb_formats`；`get_data` 注入 TBN 临时语义（Tangent1/BitangentSign1/Normal1）→ `export_data`（**Merged 时 `force_compatible_buffers_format(Blendindices, min=2, max=2)` 强制 16 位**；IB min 2B）→ ENCODEDDATA0 打包（flip_texcoord_v 时 tangents *= -1）→ build_buffers（BLENDWEIGHTS 归一化/量化）。
3. **LOD**：PerComponent 模式重映射 LOD VB2；Merged 模式生成 `VB2_LODx_BlendRemap`（R16_UINT 表，长度=全模 vg_count，供 mod 端骨骼导入 CS）。
4. **shapekey**：每 127 个一批，BatchConfigs/VertexIds/VertexOffsets 三 buffer。
5. **ini**：Jinja2 模板，Merged Skeleton 段注册 VertexGroupOffsets/Counts/LodRemaps，并强制 `vb2->ElementFormat(BLENDINDICES,0)=R16G16B16A16_UINT`。

### 3.4 骨骼合并最小闭环

① 提取端生成 vg_map/vg_offset/vg_count + format_version=4 → ② 导入端 converter_apply_lookup 重映射 → ③ 导出端 VG 全局索引化（改名 str(index)+join）+ 强制升宽 → ④（可选）LOD BlendRemap 表 + ini 注册。

### 3.5 可移植清单（按价值排序）

| 能力 | 参考插件实现 | 当前项目对应 | 移植价值 |
|---|---|---|---|
| vg_map 骨骼去重生成 | migoto_object_builder.py:386-522 | 无（SSMT4 提取端 wwmi.rs 有等价实现） | **高**（骨骼合并数据源头） |
| 导入 vg_remap 重映射 | data_model.py:105-108 + data_importer | mesh_create_helper.import_vertex_groups 已有等价 | 已具备 |
| 导出 VG 全局索引化 | object_merger.py:224-252 | 蓝图节点手动版 | **高** |
| blend 升宽 | force_compatible_buffers_format | `_allow_wide_blendindices_for_remap`（仅 WWMI/NTEMI） | **高** |
| 删空组 | vertex_groups.py:18-32 | VertexGroupUtils.remove_unused_vertex_groups（EFMI 未启用） | 中 |
| LOD BlendRemap | blender_export.py:267-292 | 无 | 暂缓（用户已定） |

---

## 4. 当前项目 TheHerta4 EFMI 流程详解

**完整报告见 `reports/efmi_pipeline_report.md`（含全部文件:行号与 16 个 EFMI 数据类型表）。本节为要点浓缩。**

### 4.1 数据来源：SSMT 工作空间

- 路径：`<SSMTWorkFolder>\WorkSpace\<Game>\<ws>\`（本机 `K:\SSMT-Package-master\WorkSpace\EFMI\<角色>\`，如 佩丽卡 含 LOD0/LOD1）。
- 结构：`Import.json`（`LODn.<drawib>-<component>-<n>` → gametype 名）→ `LODn/<name>/TYPE_<WorkGameType>/{json,buf,ib}`；`Config/FrameAnalysisPath.json`（**SSMT4 已为 EFMI 记录提取数据路径**）+ `Config/Tabs/ws-tab-*.json`（含 frameAnalysisFolderPath、modelRows）。
- 子网格 json（SubmeshJson）：CategoryBufferList（每类 buffer 的 D3D11ElementList）、IndexBufferList、`VGCount/VGOffset/VGMap`（**EFMI 实况：0/0/缺省**）、`BoneMatrixFileName`（EFMI 无）、VertexOffset/Count、ShapeKeysInfo（EFMI 全空）。
- 数据类型（`%LOCALAPPDATA%\SSMT4GlobalConfigs\GameType\EFMI\*.json`，16 个）：
  - N4 系：`NORMAL = R32_UINT`（10-10-10-2 打包，走 TBNCodec 编解码）；
  - N12 系：`NORMAL = R32G32B32_FLOAT`（非压缩）；
  - Blend：**BI4 系 `BLENDINDICES = R8G8B8A8_UINT`（4 通道，>255 Fatal）**；**BI16 系 `R32G32B32A32_UINT`**；BW8 系 `BLENDWEIGHTS = R16G16B16A16_UNORM`、BI16 系 `R32G32B32A32_FLOAT`；
  - **所有类型均无 ENCODEDDATA 元素**。

### 4.2 NTEMI 反查机制（升级要复刻的样板）

1. **FrameAnalysis 定位**：`_resolve_frame_analysis_dir`（ntemi_importer.py:99）读 `Config/FrameAnalysisPath.json` 的 `frameAnalysisFolderPath`，兜底扫 `Config/Tabs/ws-tab-*.json`；`_load_frame_analysis_dir_map`（:124）按 drawIB → FrameAnalysis 映射（一个 drawIB 可对应不同 dump）。
2. **反查内容**：FrameAnalysis 的 `deduped/` 按 CategoryHash 模糊匹配原始 vb/ib buffer（`:841-853`），IB 按 `ib-format=..-first=..-count=..` 匹配 txt，vb1-layout、pre-CS/bind-pose 位置流（`_resolve_root_vb0_path`, :766）。
3. **复制缓存回工作空间**：`localize_runtime_path_props`（runtime_cache.py:103）——源在 FrameAnalysis（工作空间外）→ `shutil.copy2` 到 `<submesh>/ModImpRuntime/`，文件名冲突重命名，对象属性改写为本地路径；配套 `prefix_property_cache.py` 按对象名前缀快照 modimp_* 属性（改名/复制后恢复）。
4. **骨骼合并后处理**：`_perform_bone_merge_postprocess`（ntemi_importer.py:969）委托外部包 `E:\代码\mod_importer-main`：`discover_yihuan_model`（解析 log.txt + deduped 文件名正则 → DetectedModelBundle，含每 draw 切片与 producer dispatch）→ `analyze_yihuan_frame_stages` → `_build_bone_merge_map`（operators.py:1237：**每 dispatch 骨骼数 = cs-t0 buf 字节数/48**，按 dispatch 顺序累加全局偏移 → entries{region_hash, first_index, index_count, local_bone, global_bone}）→ `_apply_bone_merge_map_to_objects`（:1692：把纯数字名顶点组改名/权重合并为全局 id，冲突则 ADD 合并；查不到 raise）。
5. **导出侧配套**：`_sort_export_vertex_groups_by_name`（mod_importer operators.py:767）——改名式合并后必须按名字排序，保证 index == 全局 id。

### 4.3 EFMI 导入流程（现状）

`ImprotFromWorkSpaceFull`（非 NTEMI 分支）→ 逐子网格 `SSMTImportHelper.create_mesh_from_json` → `MeshCreateHelper.create_mesh_object`：
- 坐标：axis_conversion(-Z, Y)；EFMI rotation=(0,0,0)（不翻转三角形、不缩放）。
- 元素处理：POSITION/COLOR/BLENDINDICES（65535→-1）/BLENDWEIGHT（缺时补 1,0,0,0）/TEXCOORD（v 翻转）/NORMAL（**EFMI R32_UINT → TBNCodec.decode_octahedral_r32_uint**）/ENCODEDDATA（EFMI 分支解码，当前类型无此元素）。
- **顶点组导入**（import_vertex_groups, :356）：component 非空（json VGMap + import_merged_vgmap 双条件）→ 全局 id 映射（vg_map 命中 → 全局；未命中 → vg_offset+local）；否则按局部索引 0..N 建组。**EFMI 现状无 VGMap → 走局部索引路径**。
- 空组删除仅 WWMI/NTEMI（:267-269）。

### 4.4 EFMI 导出流程（现状）

- 路由：`export_parallel.py:131` / `direct_export.py:146`（**HTMI 也映射到 ExportEFMI**）→ `ExportEFMI`。
- `SubMeshModel.calc_buffer`：读子网格 json → d3d11_game_type（可被蓝图 DataType 节点覆盖）→ 同 unique_str 对象 join → 权重归一化（v3 两次归一化）→ EFMI 空间变换 → `parse_elementname_data_dict`：
  - NORMAL R32_UINT EFMI 分支：`TBNCodec.encode_efmi_tools_r32_uint_from_tbn(flip_texcoord_v=True, flip_bitangent_sign=True)`；
  - **ENCODEDDATA 导出断路**（元素存在则 NORMAL/TANGENT/BINORMAL 跳过 + ENCODEDDATA 分支被注释 → Fatal；当前类型不触发）；
  - BLENDINDICES：**`g.group`（顶点组列表 index）**；R8 >255 两处 Fatal（`_allow_wide_blendindices_for_remap` 仅 WWMI/NTEMI）。
- `calc_index_vertex_buffer_wwmi_v2`：loop 字节去重 → 按 CategoryStrideDict 切分类 buffer；IB 固定 R32_UINT；EFMI 不翻转三角形。
- INI：每子网格 `[TextureOverride_<unique>]`（hash/match_first_index/match_index_count/handling=skip/run=CommandList\EFMIv1\OverrideTextures/ib/vb0-3/贴图/drawindexedinstanced）+ `[Resource_*]`（stride 来自 CategoryStrideDict）+ 跨 IB/着色器替换/分支 GUI。
- 预处理：`blueprint/preprocess.py` 逐对象副本（应用约束/修改器/三角化/变换），**无跨对象骨骼合并**。

### 4.5 实证结论（本计划书调研新增）

1. **SSMT4 EFMI 提取端（efmi3.rs）不生成任何骨骼合并数据**：无 VGMap/VGOffset/BoneMatrix 逻辑（对比 wwmi.rs:798-847、ntemi.rs:708-720 有完整实现：BoneMatrix.buf 按矩阵去重构建 vg_map、merged_vg_offset 跨子网格累加）。
2. **真实 EFMI 工作空间 json 无 VGMap**（佩丽卡/洛茜全部样本 `VGCount:0, VGOffset:0`，无 VGMap 字段，VertexOffset/IndexOffset 全 0）。
3. **EFMI 的 FrameAnalysis 数据存在且含骨骼矩阵**：`K:\SSMT-Package-master\3Dmigoto\Zmd\FrameAnalysis-2026-05-19-224322`（6353 个文件，deduped/ 4.8GB），其中 `deduped/256f909d-cb4.txt`（22MB）等 **cb4 常量缓冲 = 骨骼矩阵数据**（与 WWMI 提取端用的 `{index}-vs-cb4=` 同源）；根目录 `.buf` 是 0 字节占位，真实数据在 deduped/ 的 `.txt`。→ **反查方案数据源充足**。
4. EFMI 工作空间 `Config/FrameAnalysisPath.json` 指向的旧 dump（2026-03-31）已删，现存 2026-05-19 —— 读取必须 isdir 校验 + 多候选回退。
5. 可复用现成组件：`runtime_cache.py`（ModImpRuntime 复制缓存）、`prefix_property_cache.py`（属性快照）、`_resolve_frame_analysis_dir`（定位）、`mod_importer-main`（骨骼合并实现参考）、SSMT4 `wwmi.rs`（BoneMatrix 去重算法参考）。

---

## 5. 差异分析与缺口清单

### 5.1 缺口清单（P0-P3）

| # | 缺口 | 现状位置 | 等级 |
|---|---|---|---|
| G1 | **ENCODEDDATA 导出断路（隐患）** | obj_buffer_helper.py:541-575（分支注释） | P0 |
| G2 | **EFMI BLENDINDICES 无升宽**：BI4(R8) >255 两处 Fatal | obj_buffer_helper.py:80-84, 413-438, 610-622 | P0 |
| G3 | **骨骼合并数据缺失**：提取端不产、工作空间无 VGMap | SSMT4 efmi3.rs / 工作空间 json | P0（数据源头） |
| G4 | **EFMI 不消费 FrameAnalysisPath.json**（反查定位缺失） | ui_func_import_ssmt.py（非 NTEMI 分支） | P1 |
| G5 | **EFMI 无 ModImpRuntime 复制缓存**（数据不回写工作空间） | 可复用 runtime_cache.py | P1 |
| G6 | **EFMI 导入无骨骼合并后处理**（BoneMergeMap 等价物） | 参考 mod_importer operators.py:1237,1692 | P1 |
| G7 | **EFMI 空组清理未启用** | mesh_create_helper.py:267-269 | P1 |
| G8 | **导出前顶点组预处理未串联**（fill gaps/ignore 剔除/按名排序） | 蓝图节点手动版（node_vertex_group_process.py） | P1 |
| G9 | 顶点组 index vs 名字漂移风险（改名式合并后必须排序） | vertexgroup_utils.py:692（用 g.group） | P1（配套 G6） |
| G10 | LOD BlendRemap 缺失 | 无 | **不做**（用户：不需要 LOD 相关内容） |
| G11 | shapekey 批次导出缺失 | 无（EFMI json ShapeKeysInfo 全空） | **不做**（用户：保留现有 INI 格式） |

### 5.2 已具备/不需做的

- 导入 vg_remap 映射逻辑已存在（import_vertex_groups 的 component 分支）——只需喂数据。
- TBN 编解码已存在且与参考插件等价（TBNCodec）。
- 跨 IB、着色器替换、分支 GUI 等 TheHerta4 独有特性保持不动。
- 不需要照搬 ObjectMerger 的 join（当前每对象独立导出，骨骼合并在当前架构 = 全局索引直通 + 升宽 + 名字/排序纪律）。

---

## 6. 升级方案设计

### 6.1 架构原则

1. **不照搬 ObjectMerger 的 join**：当前每 drawcall 独立导出；骨骼合并在当前架构等价于「全局骨骼索引直通」。需要做的：导入时用全局 id 建组（喂 VGMap 数据）→ 导出前保证组名=全局 id 且按名排序 → blend 升宽。
2. **数据补齐走反查 + 复制回工作空间**（用户明确要求）：EFMI 导入时读 `Config/FrameAnalysisPath.json` → 定位 Zmd dump → 分析骨骼（cb4 矩阵，参照 wwmi.rs 去重算法）→ 生成 `vg_map/vg_offset/vg_count` → **写回工作空间子网格 json 缓存**（下次直接可用）；原始骨骼 buffer 按 NTEMI 模式复制到 `ModImpRuntime/`。
3. **开关与默认值**：骨骼合并相关行为挂在现有 `import_merged_vgmap()` 下，默认不改变现有非骨骼合并工作流；新增开关仅在需要时开启。
4. **游戏类型隔离**：所有改动限定 EFMI 分支（或通用 Helper 供 NTEMI/WWMI 复用）；兼容 HTMI → EFMI 映射。
5. **不引入外部包依赖**：骨骼合并逻辑参照 `mod_importer-main` 但**自研实现进 TheHerta4**（避免运行时依赖 `E:\代码\mod_importer-main`）。

### 6.2 数据流设计（升级后 EFMI 链路）

```
[提取端 SSMT4 efmi3.rs]（可选后续：补齐 VGMap 生成，参照 wwmi.rs）
        │ 现状：不产骨骼数据
        ▼
[SSMT 工作空间] 子网格 json（无 VGMap）
        │
        ▼ ① 导入（ImprotFromWorkSpaceFull EFMI 分支）
[Blender 插件] 逐子网格：
   a. 读子网格 json（CategoryBufferList 等）
   b. ★反查：读 Config/FrameAnalysisPath.json → Zmd FrameAnalysis
      → 按 drawib/region 定位 cb4 骨骼矩阵（deduped/*-cb4.txt）
      → 按矩阵内容去重生成 vg_map/vg_offset/vg_count（参照 wwmi.rs 算法）
      → ★写回工作空间 json 缓存 + 复制骨骼 buffer 到 ModImpRuntime/（参照 runtime_cache.py）
   c. 若 json 有 VGMap 且 import_merged_vgmap() → import_vertex_groups 全局 id 建组
   d. EFMI 加入空组清理列表
        │
        ▼ ② 导出（ExportEFMI）
[Blender 插件] 每 SubMeshModel：
   a. 导出前顶点组预处理（可选开关）：fill gaps / 剔 ignore / 按数字名排序
   b. ★BLENDINDICES 归一化：合并骨架场景将 R8/R16/R32 整数格式按原通道数、
      原 signedness 统一到 R16 系（R32 仅在值域可承载时降宽），同步
      CategoryStrideDict 与 INI Resource stride
   c. ★INI 变化（仅两处）：Resource_Blend stride 更新 + TextureOverride 段加
      `vb2->ElementFormat(BLENDINDICES, 0) = R16G16B16A16_UINT`（见 §6.6）
   d. ★ENCODEDDATA 导出分支接通（若未来类型含 ENCODEDDATA）
   e. 其余（跨 IB/着色器替换/贴图）不变
        │
        ▼
[Mod 输出] buffers + INI
```

### 6.3 BLENDINDICES 宽度归一化策略（最终口径：R16 整数系）

- **目标为同通道数、同 signedness 的 R16 整数 DXGI 格式**；常见四通道 UINT 即
  `R16G16B16A16_UINT`。R8 升宽、R16 保持、R32 在实际索引可承载时降宽，
  写盘前做精确范围检查，绝不截断。三通道因 DXGI 无对应 R16 整数格式而明确拒绝。
- 归一化在**导出时动态修改 gametype 元素**（运行时副本，不落盘改配置），并同步
  Format/ByteWidth/CategoryStrideDict/AlignedByteOffset；不是“发现一个 R32 就全体升 R32”。
- 触发条件（已确认）：`logic_name == EFMI 且 import_merged_vgmap() 开启`。合并模式下无条件升宽（全局索引可能超 255），不做 VGCount 阈值判断。
- 联动修改：
  - `parse_elementname_data_dict` 与 `convert_to_element_vertex_ndarray` 使用归一化后的
    R16 signed/unsigned dtype，并按 `UINT <= 65535` / `SINT <= 32767` 精确检查；
  - INI `[Resource_*_Blend]` 的 stride 用新的 CategoryStrideDict（自动联动，见 §6.6）；
  - INI 每个含 Blend 的 TextureOverride 段输出对应 SemanticIndex 的实际归一化
    R16 格式（常见四通道见 §6.6）。

### 6.6 INI 变化规格（骨骼合并后，现有 INI 格式下）★用户关注点

**结论：现有 INI 体系下，骨骼合并只带来两处必要变化，无新增 section、无新增 CustomShader/HLSL、无新增 $变量/标签。**

1. **`[Resource_<unique>_Blend]` 的 stride 变化**：BLENDINDICES 从 `R8G8B8A8_UINT`（4B）→ `R16G16B16A16_UINT`（8B）后，Blend 类别 stride 由 CategoryStrideDict 自动更新（如 BW8_BI4：4+4=8B → 4+8=12B；BI4 无权重：4B → 8B）。导出 Blend.buf 字节数同步变大。**与游戏 shader 声明的 input layout（4×8bit）不一致，必须配合第 2 点。**
2. **每个含 Blend 的 TextureOverride 段加一行**：
   ```
   vb2->ElementFormat(BLENDINDICES, 0) = R16G16B16A16_UINT
   ```
   作用：3Dmigoto 在 draw 时按 16 位重新解释 vb2 的 BLENDINDICES 元素，使 16 位数据与游戏 shader 读取方式对齐（HLSL 侧 BLENDINDICES 为 uint4 语义，8→16 位只是数据宽度变化）。参考插件同样用此招（`mod.ini.j2:491`）。**不加这行，升宽后的整模会错位撕裂。**（ElementFormat 是 3Dmigoto 基础语法，旧版加载器同样支持。）

**明确不需要的（对比参考插件）**：
- `[Constants] global $bones_count`、`$\EFMIv1\bones_count/instance_count/custom_mesh_scale`、`Callback_MergedSkeleton_ConnectComponent` —— 运行时绘制体系专属；
- `[Pool_MergedSkeleton_*]` 5 个池、`[ResourceMergedSkeletonDataRW]`、`[CommandListInitializeMergedSkeleton]`、`[CommandList_MergedSkeleton_ConnectComponent]` —— 运行时 MergedSkeleton 机制（用户确认不做）；
- `Pool_ShapeKeyedPositionsRW` 等 shapekey 池 —— 不做；
- `$object_detected/$mod_enabled/RegisterMod` 对象检测流程 —— 现有 INI 由 TextureOverride 挂钩，无需引入。

**顶点组处理与 INI 的关系**：全部在数据侧（导入建组/导出预处理），INI 无对应标签；参考插件的 `ignore` 顶点组命名约定可作为 EFMI 自动预处理的可选规则（剔 ignore 组），非 INI 内容。

### 6.4 反查方案（G3/G4/G5/G6）—— 需要用户确认的大决策

**方案 A（推荐）：Blender 插件侧反查**（用户明示的路线）
- 导入 EFMI 工作空间时，对每个子网格：读 FrameAnalysisPath → 定位 dump → 分析该 drawib 的骨骼（cb4 矩阵；按 wwmi.rs 的"矩阵字节去重"算法）→ 生成 vg_map 写回工作空间 json + 复制骨骼 buffer 到 ModImpRuntime。
- 优点：不动 SSMT4（Rust/注入器），纯 Blender 插件改动；数据进工作空间缓存，一劳永逸；完全符合用户"拿完复制到工作空间"的要求。
- 难点：EFMI dump 的骨骼 buffer 布局需勘察确认（cb4 是 vs-cb4 还是 cs-cb4；矩阵是 4x3 float 还是 3x16 字节行；与 NTEMI 的 cs-t0/48B 不同）；需要自研 BoneMergeMap 生成（可参照 mod_importer 与 wwmi.rs）。
- 风险：EFMI 是 GPU-PreSkinning 混合渲染（"全局只有一个 Buffer"），骨骼矩阵可能不在 vs-cb4 而在别的槽位 —— **实施第一步必须先勘察 dump**。

**方案 B：改 SSMT4 提取端 efmi3.rs**
- 参照 wwmi.rs/ntemi.rs 在提取时生成 VGMap/VGOffset/VGCount/BoneMatrix.buf 并写入工作空间 json。
- 优点：数据在源头补齐，Blender 侧只用现有 import_vertex_groups 路径；与 WWMI/NTEMI 语义完全一致。
- 缺点：SSMT4 是独立 Rust/Tauri 仓库（`E:\代码\SSMT4-Alpha-main`），需要重新编译注入器/后端；本次任务仓库是 Blender 插件 —— 跨仓库改动需要用户确认。
- 可两者兼做：A 先落地（Blender 侧兜底 + 缓存），B 作为后续（提取端根治）。

### 6.5 导出侧顶点组预处理（G8/G9）

新增 EFMI 专用导出前置步骤（开关控制，默认随 import_merged_vgmap 联动）：
1. `fill_gaps`：按数字名补缺（VertexGroupUtils.fill_vertex_group_gaps 已有，vertexgroup_utils.py:395）；
2. 剔除名字含 `ignore` 或超出 `json.VGCount` 的组（参照参考插件 object_merger.py:234-237）；
3. **按数字名排序**（参照 mod_importer `_sort_export_vertex_groups_by_name`），保证 `g.group == 全局 id`（v3 提取用 g.group）。
4. 与蓝图手动节点（node_vertex_group_process）并存：自动处理仅 EFMI 骨骼合并模式生效，手动节点流程不动。

---

## 7. 实施步骤（分阶段）

### 阶段 0：勘察与闭环（本计划书）
- [x] 参考插件全流程报告（`docs/efmi-tools-参考插件报告存档.md`）
- [x] 当前项目 EFMI 流程 + NTEMI 反查机制报告（`reports/efmi_pipeline_report.md`）
- [x] SSMT4 提取端/数据类型/工作空间/FrameAnalysis 实证
- [ ] 用户确认本计划书（§6.3 升宽目标、§6.4 反查方案 A/B、开关语义）

### 阶段 1：P0 修复（不依赖新数据，可立即做）
- [x] G2 BLENDINDICES 归一化：`d3d11_gametype.widen_blendindices()` 将
  R8/R16/R32 整数格式按原通道数与 signedness 统一到 R16 系，ByteWidth、dtype、
  CategoryStrideDict、AlignedByteOffset 联动且幂等；R32 降宽写盘前做范围检查，
  不可承载则失败 + `submesh_model.calc_buffer` 接入（EFMI + import_merged_vgmap）+
  `blendindices_widened` 标记
- [x] INI 变化（§6.6）：合并模式 → `$\EFMIv1\component_id` + `run = CommandList_MergedSkeleton_ConnectComponent`；非合并模式 → ElementFormat 单行（stride 经 CategoryStrideDict 自动联动）
- [x] G1 ENCODEDDATA 导出接通：`obj_buffer_helper._parse_encodeddata`（TBNCodec.encode_efmi_tools_r32_uint_from_tbn），替换被注释代码

### 阶段 2：P1 导入侧骨骼合并（已完成核心，实测通过）
- [x] G4 反查定位：`EFMISkeletonMergeHelper.resolve_frame_analysis_dir`（FrameAnalysisPath.json → tabs → migoto 目录最新 FrameAnalysis-* 多候选回退）
- [x] G3 骨骼数据生成：`common/efmi_skeleton.py`（EFMILogParser 解析 log.txt draw/cb/srv/dump 绑定 + EFMIBoneMapBuilder 骨骼段读取[instance config fc=10960 `[5][0:2]` 偏移 → vs-t0 池 256×12 矩阵] + 跨子网格矩阵去重 vg_map + vg_offset 累加）→ 写回 json（VGMap/VGOffset/VGCount）+ 复制骨骼池到 ModImpRuntime/
- [x] G6 导入接入：`ui_func_import_ssmt.ImprotFromWorkSpaceFull` EFMI 分支导入前预生成（不阻断导入）
- [x] G7 空组清理：EFMI 加入列表 + 删后按名排序（`mesh_create_helper.py`）
- [x] 实测（"测试"工作空间 + 05-19 dump）：14/14 子网格成功，596 条映射（96.8% 去重），矩阵一致性 3/3 通过，VGOffset 累加 Σ=596 正确

### 阶段 3：P1 导出侧骨骼合并收尾（已完成，INI 文本结构实测通过）
- [x] G8 导出前顶点组预处理：`submesh_model._prepare_efmi_merged_skeleton_vertex_groups`（补缺[export_add_missing_vertex_groups 开关] → 按名排序 → 删 ignore 组 → 改名 str(index) 紧凑化）
- [x] G9 运行时 Merged Skeleton INI 段：`efmi._add_merged_skeleton_section`（Constants[$component_count/$bones_count/$max_instance_count/$merged_skeleton_initialized] + 5 Pool + ResourceMergedSkeletonDataRW + CommandList_MergedSkeleton_ConnectComponent + CommandListInitializeMergedSkeleton[vg_offset/vg_count 注册，LodRemaps 全 null]）+ 节序加入 MergedSkeleton
- [x] 升宽后 dtype/stride/INI 一致性：CategoryStrideDict 自动联动验证通过

### 阶段 4：验证与回归（Blender headless 端到端 [PASS]）
- [x] 反查（log 解析/骨骼读取/vg_map/去重）：佩丽卡 + "测试"工作空间 dry-run，矩阵一致性 3/3
- [x] 写回（json VGMap/VGOffset/VGCount + ModImpRuntime）：14/14 子网格，VGOffset 累加 Σ=596 正确
- [x] 升宽（dtype/stride/幂等/R32 跳过）：独立验证通过
- [x] INI Merged Skeleton 段结构：mock 文本 57 行全结构通过
- [x] **Blender 5.0.1 headless 端到端**（"测试"工作空间 + 05-19 dump，settings 切换脚本驱动）：
  - 导入：14 网格对象，596 组，全局最大骨骼 id 595，json 14 个含 VGMap
  - 导出：INI 全校验 OK（ConnectComponent/AttachComponent/ElementFormat R16/$bones_count/$component_count/vg_offset 注册/InitializeMergedSkeleton）；ElementFormat 行 1 + ConnectComponent run 14 + component_id 赋值 14
  - buffer：14 个 Blend.buf 全部 16B/顶点（BW8 8B + BI16 8B，升宽生效）
  - 脚本：`.dbg/bl_efmi_headless_validate.py`（Blender 内）+ `.dbg/run_efmi_headless_validation.ps1`（驱动，含 settings 备份/切换/恢复）
  - 结果：[PASS] 全部验证通过

### 阶段 5：与参考插件 EFMI-Tools 对照验证（已通过）
- [x] 参考插件装入 Blender 5.0 addons（`EFMI-Tools`）并跑通 extract_frame_data → import_object → export_mod
- [x] **原始 blend 数据 100% 一致**（th 工作空间 Blend.buf vs 参考插件 Component11.vb：828 顶点索引/权重/分布/逐顶点全同）→ 数据源头一致
- [x] **去重后骨骼数 14/14 完全一致**（40/94/43/149/59/17/5/45/10/12/68/20/15/19）→ 骨骼段读取 + 矩阵去重等价
- [x] **th 导出索引结构 = 原始**（th 导入导出保真，分布 {2:84,3:459,4:285} 与原始一致）
- [x] 顶点数 14/14 一致、权重量化值大部分一致（量化实现差异）
- [x] INI 结构对照：两边 MergedSkeleton 段 + ElementFormat R16 + vg_offset 注册
- 差异说明（均合理非错误）：① 全局骨骼 id 编号不同（分配顺序：th 子网格序 vs ref 组件 Y 序，各自 vg_offset 自洽，CS 按全局 id 直接索引）；② 参考插件导出分布 ≠ 原始（其导入导出链自身行为，th 更贴近原始）；③ 参考插件多提取的 Component 13/Weighted 4043 为场景物，不属该角色，已排除
- **结论：升级没有问题，骨骼合并数据与参考插件语义等价**

### 阶段 6：加载器核对与骨骼合并调用方式修正（路线B，已完成）
- [x] 加载器已更新（Core\EFMI，2026-08-21）：MergedSkeleton.ini + Shaders/（BoneDataInitializer/BoneDataImporter_RollingBuffer+ConstantBuffer/InstanceConfigOverrider）+ SpatialIdentification.ini，配置 cfg_ms_* 与生成公式一致
- [x] **发现并修正调用方式问题**：TheHerta4 静态绑定体系下 `run = ConnectComponent` 不会被运行时调用（正确方式是 Callback + Component_DrawInstances 运行时流程）；按用户决策选**路线B（静态绑定手动串联）**，跨 IB 暂不考虑、保留着色器替换/形态键等现有体系
- [x] TextureOverride 段改为手动串联（每含 Blend 子网格，14 处）：
  `$\EFMIv1\component_id` + `$\EFMIv1\instance_count = 1`（保证 Apply 的 instance_data_index=component_id，避免共用 UpdateFrame[0]）+ `$\EFMIv1\custom_mesh_scale = 1.0` + `run Component_ReadConfig`（检测 instance config cb 窗口）+ `run ConnectComponent` + `run MergedSkeleton_DetectBoneDataSource` + `run MergedSkeleton_Apply`，位于 ib/vb 绑定与 drawindexedinstanced **之前**（先覆盖 vs-t0/cb 再绘制）
- [x] Blender headless 端到端复测 [PASS]，INI 结构验证正确（14 段串联齐全）
- [ ] **游戏内实测（待用户）**：启动游戏加载 mod，验证骨骼合并生效（模型正常显示/骨骼不错乱）；若异常，排查 Apply 在静态单实例下的语义（$draw_call_instance_id=0、空间识别池默认值）

### 阶段 7：去重算法修正（容差聚类，已完成并复测 [PASS]）
- [x] **用户发现去重问题**：精确 tuple 匹配对浮点敏感，大量"矩阵近似相同（差 1e-7~1e-4）"的骨骼未合并（例：539 与 493 矩阵差 1.8e-07 本应同一骨骼却被分成两个 id；容差 1e-4 下 171 组矩阵相同骨骼中 157 组被分配不同 id）
- [x] `build_vg_maps` 改为**容差聚类（并查集连通分量）**：矩阵 allclose(atol=1e-4) 的骨骼合并为同一 canonical（取组内权重顶点数最多者），`match_tolerance=1e-4` 可调
- [x] **drawcall 反查兜底**：ComponentName_DrawCallIndexList.json 被 SSMT4 重置（仅剩 1 条）时，`EFMILogParser.find_drawcalls_by_ib` 从 dump 按 ib hash + index_count + first_index 反查 drawcall（验证：同 ib 的 000020 vs 000071 骨骼段完全相同，反查可靠）
- [x] 效果：唯一全局骨骼 id 596→**255**（多合并 341 根），跨子网格共享骨骼 47→**162**，539/493 合并为 44；162/162 共享骨骼在 Blender 里同名（合并 100% 生效）
- [x] 误合并检查：169 组同 id 骨骼中 168 组矩阵差全在容差内，唯一 1.47e-4 的 gid 419（30 部件共享的核心骨骼，属正常）
- [x] Blender headless 端到端复测 [PASS]（导入 509 组/导出 INI 全校验 OK/Blend buffer 14 个）

### 阶段 8：去重回退到精确匹配（修正容差误合并，已完成并复测 [PASS]）
- [x] **用户发现容差误合并**：容差 1e-4 把"矩阵几乎相同但语义不同"的骨骼也合并了——同一网格内手指的两节骨骼（d6128f13[57]/[58]，矩阵差仅 1.79e-07）被合并成一段，丢失独立动画能力。参考插件精确匹配则保留两段（57→531、58→532 不同 id）
- [x] **根因**：矩阵差无法区分"同一骨骼的浮点误差"与"不同骨骼的恰好重合"——539/493（差1.8e-07）与手指两段（差1.79e-07）差值几乎一样，矩阵去重无法两全。**参考插件用精确匹配正是为此：漏合并无害（蒙皮仍正确），误合并有害（功能错误）**
- [x] `build_vg_maps` 回退到**精确匹配**（match_tolerance 默认 0.0，矩阵逐元素完全相同才合并，与参考插件一致）；容差聚类代码保留但默认禁用（match_tolerance>0 时才启用，文档标注勿随意放宽）
- [x] 验证：d6128f13[57]→585、[58]→586（手指两段分开 ✔）；唯一全局 id 回 540；539/493 不合并（与参考插件一致，无害）；Blender headless 端到端复测 [PASS]（导入 596 组 d6128f13 回 68 组/导出 INI 全校验 OK）

### 阶段 9：双维度去重（矩阵+驱动质心，已实现并复测 [PASS]）——用户提出的"模拟驱动"思路
- [x] **用户提出更优去重思路**：骨骼的唯一标识不只是蒙皮矩阵，还有"它驱动哪些顶点"——矩阵相同但驱动顶点区域不同（手指两节）=不同骨骼；矩阵相同/近似且驱动顶点区域重合=同一骨骼。解决矩阵去重"无法区分浮点误差 vs 恰好重合"的根本两难。
- [x] 实现 `EFMIBoneMapBuilder.compute_driven_centroids`：读 Position.buf+Blend.buf 计算每局部骨骼驱动的顶点加权质心（绑定姿态坐标）
- [x] `build_vg_maps` 改为**双维度聚类**：
  - 矩阵完全相同（diff=0）→ 必合并（不经质心检查）
  - 矩阵近似（diff<match_tolerance=1e-3 粗筛）**且** 驱动质心重合（距离<centroid_tolerance=0.02）→ 合并
  - 矩阵近似但质心不同 → 不合并
  - 缺质心数据时矩阵近似不合并（保守）
- [x] 实测：手指两段 57→585/58→586（分开 ✔，质心差 0.0265）；539/493 全→44（合并 ✔，质心差~0.01）；唯一 id 395（精确540<395<容差255）；同 id 组平移差>1e-3 的误合并=0
- [x] Blender headless 端到端复测 [PASS]（共享骨骼 id 102，532 组，INI 全校验 OK）

### 阶段 10：投票制弯路废止，回到分层判据（矩阵硬门控 + 质心确认，2026-08-24 实测定案）

- [x] **弯路记录**：390/393 误判（后查明系陈旧缓存污染）后曾改"权重扩散评估 + 多维度投票"（矩阵/质心/包围盒/扩散球 vote≥2，矩阵降为可输一票），并叠加防链式/完全图/阈值升降共 7 次迭代。
- [x] **用户实测反馈误并**："明明不在一起的骨骼被判定成一个并合并"。真实数据取证（"测试"工作空间 + 08-10 dump，只读探针 `.dbg/efmi_dedup_evidence_probe.py`）：**195 个合并组中 42 组矩阵差异 > 1e-3（最高 0.27）**——质心/包围盒/扩散球是"驱动点云接近度"的相关度量（同进同退），区域相邻的不同骨骼几何票全过即可推翻矩阵反对票；vote_threshold 2→3 无效（三票几何仍凑满）；完全图防链式对两两全过的密集簇无效。
- [x] **回退到阶段 9 分层判据**（`build_vg_maps`，矩阵恢复为必要条件）：diff≥1e-3 永不合并；bitwise 相同直接合并；0<diff<1e-3 需质心 <0.02 确认（缺签名保守不合并）。几何维度只用于"拆"，无权"并"。
- [x] 修复后真实数据复测：**164 组（148 精确 + 16 容差带质心确认），误并 0**；新增 `tests/test_efmi_skeleton_dedup.py` 9 例（误并复现/手指/539·493/完全图拦截/开关恒等等）全绿。
- [x] 配套：「清除骨骼合并VGMap缓存」按钮放开到 ZZMI（原 EFMI 限定；ZZMI 侧此前无清缓存路径，陈旧 VGMap 只能手工删）。

### 阶段 11：权重扩散接触检测（2026-08-23）

- [x] 修正旧分层判据的漏并：近似矩阵不再只比较整体加权质心；从 Position.buf + Blend.buf
  为每个 local 顶点组保留正权重采样（最多 256 点），在绑定姿态空间建立局部权重扩散场。
- [x] 跨部件候选在接触位置做双向最近邻采样：一侧覆盖率达到 30% 且原始权重平均误差不超过
  0.20 才确认同一扩散场；评估时弱权重点至少保留最大权重 25% 的影响，避免左右两侧的弱点
  被强中心点淹没；适配“大平面 + 贴合平面的散落物体”而不要求整体质心重合。
- [x] 矩阵 maxdiff ≥ 1e-3 仍是硬拒绝，避免相邻但不同骨骼被几何扩散证据推翻；无扩散字段时回退
  到旧质心判据，保证旧缓存/测试调用的兼容性；同部件拒绝和权重扩散连通性校验保留。
- [x] `_DEDUP_ENABLED` 恢复默认开启；策略或 Position/Blend 数据变化后必须先清理 VGMap 缓存。
- [x] 写回 `VGMapAlgorithmVersion` + `VGMapDedupEnabled`，旧策略或开关模式缓存会在导入时自动失效；面板清理按钮仍可强制重算。
- [x] 回归覆盖：平面/散落物体接触权重一致应合并、接触权重反向应拆分、Position/Blend buffer
  能生成扩散采样，EFMI LOD/缓存单测保持通过。
- [x] 增强跨层匹配：对点云做局部 PCA 法向估计；两张近似平行表面允许沿法向存在层间距，
  但切向投影误差和法向夹角必须通过，覆盖大腿与丝袜这类不共享顶点/拓扑的表面；法向
  夹角默认点积 0.70，允许真实凹槽底与平面之间的错位桥接。
- [x] 明确当前不是体素化的“体积投影”：它是 3D 正权重点云最近邻扩散，只有在局部 PCA
  能证明面状时才启用法向层间投影；线状/体积状点云仍使用更严格的真实接触距离门控。
- [x] 最近邻匹配增加目标点唯一占用、稀疏侧双向覆盖检查和至少 2 个（按实际采样数
  动态下调）唯一配对点，避免凹槽边缘的多对一最近邻制造虚假覆盖率；该判据变更使旧
  VGMap 缓存自动失效。
- [x] 合并后对每个 global VG 做最终权重扩散连通性复核：成员必须形成连通图，发现
  孤立/断开的组件立即拆回独立槽位；不再要求平面与每个凹槽底直接构成完全图。

### 阶段 12：分组投影未匹配导入过滤（2026-08-26 用户决策）

- [x] 语义：`EFMI LOD 分组投影` 模式下 LOD0 的物体 LOD1 的导入约束——LOD1 部件
  若几何匹配不成功（未进入部件一对一配对，或配对得分超过 `_CROSS_LOD_PART_IMPORT_SCORE_LIMIT`=0.30
  [0.5·对称最近邻点云中位距 + 0.25·bbox 间隙 + 0.10·中心距 + 0.05·尺寸误差 + 有界数量项]）
  则不导入：不写 VGMap/VGCount/VGOffset，json 写 `EFMILODProjectionSkipped=True`
  （连带 EFMILODLayoutVersion/EFMILODReference/EFMILODProjection），导入循环据此排除
  该物体；导出侧因无 VGMap 也不纳入合并骨架。
- [x] 未匹配部件仍发布 BoneMatrix/InstanceConfig 工作空间来源缓存（保留清缓存/取消
  过滤后凭原文件重建的能力）；正常写回时撤销历史 `EFMILODProjectionSkipped` 标记。
- [x] 联合缓存的幂等门控把有效跳过裁决视为“已处理”，不重算、不报缺口；
  `clear_vgmap_cache` 一并清除该标记；`EFMILODLayoutVersion` 升 v6 使旧缓存
  自动重建以应用新裁决。
- [x] 含义修正（2026-08-27 用户实测反馈"没有效果"后查明）：LOD1 提取中还有
  **收集失败**（dump/工作空间无骨骼来源、Blend 无 BLENDINDICES）的未知部件，
  它们原本让 `ensure_skeleton_data` 整批失败 → 导入回退普通导入 → 全部物体
  （含未知物体）导入——过滤完全没机会生效。修复：非基准 LOD 的未收集目标
  同样纳入"投影未匹配"裁决（写 EFMILODProjectionSkipped 标记、json-only 事务），
  且不计入整批失败（基准侧失败仍按原语义回退）；跳过分支同时清空历史
  VGMap/VGCount/VGOffset 等残留键。真实工作空间验证：LOD0 14（全部生成）、
  LOD1 110 = 12 匹配生成 + 98 投影未匹配跳过，无残留无标记目标，二次运行
  走联合缓存快路径。
- [x] 基准侧（LOD0）不做导入过滤；基准侧读取失败仍按原有失败语义整批回退
  普通导入，不静默丢弃。回归：`tests/test_efmi_skeleton_lod.py` 新增未匹配/
  弱匹配/未收集跳过用例，`test_without_tabs_both_lod_use_default_dump` 与
  `test_joint_cache_invalidates_when_json_missing` 同步新语义。

### 阶段 13：导入后自动创建跨 LOD 顶点组匹配链

- [x] 语义：合并路线导入成功后，基于分组投影匹配账本（LOD1 json 的
  `EFMILODCorrespondence`）自动给相关组插入「物体 >>> 物体组 >>> 顶点组处理
  节点(组内全部部件匹配节点) >>> 输出」：每组一个 `SSMTNode_VertexGroupProcess`
  插在 Object_Group 与输出/合并节点之间；每组每对匹配一个
  `SSMTNode_VertexGroupMatch`（source_object = LOD0 物体、target_object = LOD1
  物体、target_hash 留空、非精确路由），并立即调用 `execute_match`。映射来源是
  当前 Blender 中两边实际导入物体的顶点组中心（阈值 0.06；关闭 Chamfer、形态键
  与调试物体），不是导入前 JSON 的 VGMap/骨骼槽位账本。匹配结果由节点自己写入
  映射文本；失败或零命中会明确计数并保留节点供人工调整。
- [x] 数据来源：`EFMISkeletonMergeHelper.load_lod_match_pairs`——只收集
  目标侧（row.unique_str 的 LOD == json EFMILODReference）避免与基准侧账本
  重复；它只返回 `target_key/reference_key/target_lod/reference_lod` 四个配对字段，
  刻意不读取两侧 `VGMap`，也不返回 `vg_mapping`。无对应/跳过标记/无 LOD 前缀
  的目标不产生配对；无匹配对的组不加处理节点（避免 fill/merge 副作用）；回退
  普通导入时不构建任何东西。
- [x] 同轮追加：每组链中插入**重命名物体节点**
  （LOD0 端组，规则 = LOD0 物体名 → 对应 LOD1 物体名，与匹配节点方向一致），
  位于物体组与顶点组处理节点之间：物体 >>> 物体组 >>> [重命名 >>> 顶点组
  处理(匹配节点)] >>> 输出；规则全部加在一个重命名节点（每组一个）。
- [x] **统一顶点组编号修复**（用户反馈 LOD1 物体顶点组 1000+，并要求收窄范围）：
  最初 LOD1 匹配部件保留自身恒等槽位空间（0..sum 各部件本地组），与 LOD0 编号
  完全不共享，导入物体组名各自累加；随后 v9 改为把已对应的目标侧 local 投影到
  **参考侧全局组 id**（跨 LOD 同名同号）——实测 LOD1 爆炸（LOD1 顶点组与 LOD0
  共用同一批槽位，运行时 MergedSkeleton_Apply 对同一 component 每帧只导入一次
  骨骼、仅允许更优 $lod_level 覆盖；同帧先 LOD0 后 LOD1 时 LOD1 网格读到 LOD0
  已导入的矩阵，而 L0/L1 两侧矩阵数据不同）。
  **v10（撤销 v9 共享槽位投影）**：每 LOD 用自己的 dump 独立
  执行权重扩散去重（槽位从 0 起），非基准 LOD（LOD1）的整个编号空间**平移**到
  基准 LOD 段之后（LOD0: 0..max0，LOD1: base 起，base = LOD0 段总槽位）——
  两域不相交、全局唯一，跨 LOD 零共享零串扰。LOD1 每个部件挂**自己的**
  component 槽位段与自己的绘制入口（无 full→lod BlendRemap），运行时把当前
  LOD draw 的**自己的**矩阵写入**自己的**槽位。真实工作空间验证：
  当前选择中的 LOD0/LOD1 各 10 个对象分别位于 [0, 370] 与 [371, 739]，
  两域不相交；工作空间中被投影过滤的未匹配部件无 VGMap。
- [x] **v13 分组投影开关语义**：投影不是让 LOD1 直接使用 LOD0 槽位，而是把
  LOD0 的去重**分区关系**镜像为 LOD1 的约束：LOD0 同一去重组对应的 LOD1
  组必须合并，不同 LOD0 组对应的 LOD1 组禁止互并；LOD1 结果仍整体平移到
  自己的独立槽位段。开关开启时同时执行几何未匹配 LOD1 过滤和自动匹配链；
  关闭时不传镜像约束、不过滤、不建链，双侧完全独立去重。
- [x] **撤销把账本当顶点组映射的回归修复**：分组投影账本描述骨骼候选与分区关系，
  不能代替导入后自主生成物体的匹配节点。曾把两侧 VGMap 直接换算为映射文本，
  导致实际物体仍有正权重组（如组 0）未被改名，最终被 LOD 域校验拒绝；更换角色
  时还会把角色特定槽位关系误当成通用规则。现恢复为“账本只配物体，节点重新匹配
  实际物体”。`target_hash` 留空，避免重命名后的 `.001` 后缀绕过精确路由。
- [x] **导出 LOD 域校验**：普通 LOD BlendIndices 只能引用本 LOD 本帧实际写入的
  component 槽位；仅允许声明过的 same-IB 跨 LOD 折叠别名作为例外。过去只检查
  全局大池，另一个 LOD 的合法 id 也会误过关；现在此类残留引用在写文件前失败。
  same-IB 折叠是运行时复用 draw/component 的既有设计，不作为错误移除。
- [x] **相同网络是合法输入**：取消“LOD1 与 LOD0 顶点数相同就疑似导错几何”的
  推断和警告。不同 LOD 可以故意复用完全相同的拓扑、Position 与 Index；几何相同
  本身不参与判错，实际正确性只由对应账本、槽位域和 BlendIndices 写盘校验决定。

### 阶段 5（明确不做，备忘）
- ~~LOD BlendRemap~~（用户：不需要 LOD 相关内容）
- ~~shapekey 批次导出~~（用户：保留现有 INI 格式）
- ~~运行时 MergedSkeleton 段 / 空间识别 / RegisterMod 流程~~（用户：保留现有 INI 格式）
- ~~SSMT4 提取端 efmi3.rs 补齐 VGMap（方案 B）~~（方案 A 已够用，视后续需要再议）

---

## 8. 风险与验证

| 风险 | 等级 | 缓解 |
|---|---|---|
| R16 升宽后与游戏 shader 读取方式不匹配 | 高 | 必须输出 ElementFormat 行（§6.6）；与参考插件已验证路径一致；保留开关可回退 |
| EFMI dump 骨骼 buffer 布局与预期不符（GPU 混合渲染） | 高 | 阶段 2 第一步勘察（读 deduped/*-cb4.txt 结构）后再写生成逻辑 |
| 反查依赖的 dump 被删（实测旧 dump 已删） | 中 | isdir 校验 + 多候选回退 + 数据缓存进工作空间后不再依赖 dump |
| 改动影响其它游戏类型 | 中 | 全部限 EFMI 分支；全量 tests/ 回归 |
| 顶点组改名/排序破坏用户手动分组 | 中 | 开关默认随 import_merged_vgmap；仅 EFMI 合并模式自动处理；手动节点流程不动 |
| TBN 重编码不可逆（切线角信息丢失） | 低 | 现状已如此，非本次引入；如出现闪烁再评估 |
| 写回工作空间 json 污染提取端数据 | 低 | 只增不删字段（VGMap/VGOffset/VGCount/BoneMatrixFileName），提取端重导会覆盖 |
| 参考插件运行时机制未照搬导致能力差距（多实例/换装/LOD 骨骼切换） | 低（用户已接受） | 用户明确保留现有 INI 格式；骨骼合并以数据侧实现 |

---

## 9. 决策记录（实施前已拍板）

| # | 问题 | 决策 | 影响 |
|---|---|---|---|
| D1 | BLENDINDICES 归一化目标 | **按原通道数归一化到 R16 整数 DXGI 格式**（常见四通道为 `R16G16B16A16_UINT`） | R8 升到 R16；R32 仅在实际索引可承载时降到 R16，写出前做范围检查，禁止静默截断；无需全量升到 R32 |
| D2 | 骨骼数据生成方案 | **方案 A：Blender 侧反查 + 写回工作空间** | §6.4 按 A 实施；SSMT4 不动 |
| D3 | 行为开关 | **复用 `import_merged_vgmap()`** | 导入/导出骨骼合并 + 升宽均随此开关（默认 True） |
| D4 | 骨骼 buffer 缓存位置 | 照 NTEMI 模式 `ModImpRuntime/`（实施时确认） | §6.2 数据流 |
| D5 | dump 勘察 | 阶段 2 第一步读 `K:\SSMT-Package-master\3Dmigoto\Zmd\FrameAnalysis-2026-05-19-224322\deduped\*-cb4.txt`（只读） | 决定 BoneMergeMap 生成细节 |

---

## 10. 参考资料

- 参考插件报告：`docs/efmi-tools-参考插件报告存档.md`（子代理A）
- 当前项目报告：`reports/efmi_pipeline_report.md`（子代理B）
- 提取端：`E:\代码\SSMT4-Alpha-main\src-tauri\src\extract_new\{efmi3,wwmi,ntemi}.rs`、`src\workspace\submesh_json.rs`、`src\config\path_manager.rs`
- 外部参考实现：`E:\代码\mod_importer-main\operators.py`（_build_bone_merge_map:1237、_apply_bone_merge_map_to_objects:1692、_bone_count_from_t0_path:1077、_sort_export_vertex_groups_by_name:767）、`core\discovery.py`
- 实证数据：`K:\SSMT-Package-master\WorkSpace\EFMI\佩丽卡`、`K:\SSMT-Package-master\3Dmigoto\Zmd\FrameAnalysis-2026-05-19-224322`、`%LOCALAPPDATA%\SSMT4GlobalConfigs\GameType\EFMI\*.json`
