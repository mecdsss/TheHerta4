# NTMI ModImp 导出实施设计

## 1. 目标和结论

本次实现的目标是在 TheHerta4 蓝图系统里新增一条独立的 NTMI/mod_importer 风格导出链路：

- 在蓝图里新增 `SSMTNode_Result_Output_NTMIModImp` 输出节点。
- 在 `ui/ntmi_modimp/` 下实现独立导出逻辑，不复用标准 SSMT `Generate Mod` 的工作空间数据路径。
- 以蓝图解析后的物体名前缀作为分组依据，构造 `mod_importer-main` 需要的 `sourceIB -> region -> part` 导出集合树。
- 复用 `E:\代码\mod_importer-main\core\exporter.py` 的 `export_collection_package(...)` 生成 Buffer 和 INI。
- 在参考插件生成的 INI 基础上补丁式接入蓝图物体切换节点，生成 `KeySwap`、`$active0`、`if ... endif` draw 开关。

总体结论：该方案可行，且比把 mod_importer 的 UI Operator 直接塞进现有 SSMT 输出链路更稳。核心原因是 `export_collection_package(...)` 是相对纯粹的导出核心，只要求集合树和 `modimp_*` 合同字段；而 `operators.py` 依赖参考插件自己的 scene 属性、分析流程、工作合集和面板状态，不适合作为蓝图输出节点直接调用。

## 2. 新增结构

新增 UI 目录：

- `ui/ntmi_modimp/modimp_core.py`
- `ui/ntmi_modimp/export_tree_builder.py`
- `ui/ntmi_modimp/ini_swap_patcher.py`
- `ui/ntmi_modimp/export_ntmi_modimp.py`
- `ui/ntmi_modimp/__init__.py`

新增蓝图节点：

- `blueprint/node_result_ntmi_modimp.py`
- 节点类型：`SSMTNode_Result_Output_NTMIModImp`
- Operator：`ssmt.generate_ntmi_modimp_blueprint`

为了让新输出节点能被现有蓝图解析器当作输出终点识别，扩展了：

- `BlueprintExportHelper.runtime_result_output_node_type`
- `BlueprintExportHelper.iter_result_output_node_types()`
- `BlueprintExportHelper.is_result_output_node()`
- `BluePrintModel` 和 `ChainTraverser` 的输出节点识别。

节点菜单也加入了 `NTMI ModImp输出`，并让嵌套、解组、链路高亮等蓝图工具识别新输出节点。

## 3. 数据来源设计

新游戏类型不依赖 SSMT 工作空间。当前实现不读取 `GlobalConfig.path_workspace_folder()`，也不从 SSMT workspace 的 `Config.json`、`TYPE_*`、SubMesh json 里取参数。

数据来源分为三层：

1. 蓝图解析结果：使用 `BluePrintModel.ordered_draw_obj_data_model_list` 中的 `match_draw_ib`、`match_index_count`、`match_first_index` 作为分组身份。
2. Blender 对象和参考插件自定义属性：从物体、原物体、物体所在合集、同名前缀合集、`<draw_ib>_Export` 导出根以及其 region 子集合继承 `modimp_*` 字段。
3. 参考插件导出核心：只调用 `mod_importer-main/core/exporter.py::export_collection_package(...)`，不执行参考插件 `__init__.py`，避免注册它的 Blender UI 和属性。

默认输出目录也独立于 SSMT workspace：

- 如果节点填写 `export_dir`，使用该目录。
- 如果未填写且当前 `.blend` 已保存，输出到 `.blend` 同目录下的 `NTMI_ModImp_Output`。
- 如果 `.blend` 未保存，输出到用户目录下的 `TheHerta4_NTMI_ModImp_Output`。

## 4. 前缀分组规则

用户提出的“集合名称和物体名称前缀一致，可以直接使用蓝图链路中的物体名称前缀分组”在第一版中采用如下实现：

- 不直接读取 UI 节点上原始前缀字符串。
- 先让蓝图按现有链路完成解析、重命名、物体切换、前处理副本处理。
- 再读取 `DrawCallModel` 的最终导出身份，即 `DrawIB-IndexCount-FirstIndex`。
- 按 `draw_ib` 分 source root。
- 按 `(draw_ib, index_count, first_index)` 分 region。
- 第一版不显式生成 `partNN` 子集合，而是将 mesh 直接链接到 region 下，让参考插件 exporter 自动按 implicit `part00` 处理。

这和 `mod_importer-main` 的规则兼容，因为它允许 region 下直接有 mesh 且没有显式 part collection，此时会自动形成一个隐式 `part00`。

## 5. 临时集合树

导出前会在 Blender 场景里临时构造集合树：

```text
TheHerta4_NTMI_ModImp_<draw_ib>
└── <draw_ib>-<index_count>-<first_index>
    ├── mesh_a_copy
    └── mesh_b_copy
```

source root 写入：

- `modimp_kind = source_ib`
- `modimp_profile_id = yihuan`
- `modimp_source_ib_hash = <draw_ib>`
- `modimp_collector_*` 字段，尽量从参考插件已有对象/合集继承。

region 写入：

- `modimp_kind = region`
- `modimp_profile_id = yihuan`
- `modimp_source_ib_hash = <draw_ib>`
- `modimp_region_hash = <draw_ib>`
- `modimp_region_index_count = <index_count>`
- `modimp_region_first_index = <first_index>`
- `modimp_match_vs_texcoord_hash`
- `modimp_match_vs_position_hash`
- `modimp_match_vs_outline_hash`
- `modimp_texture_slots`

导出完成后默认清理临时集合树。节点高级选项 `Keep Temp Collections` 可保留它用于调试。

## 6. INI 生成逻辑

INI 生成分两段：

1. `export_collection_package(..., generate_ini=True)` 先生成和参考插件一致的 NTMI runtime INI。
2. `ini_swap_patcher.py` 再按蓝图物体切换信息补丁式修改 INI。

完整 INI 需要以下合同字段：

- source/root 级：`modimp_collector_group_slot`、`modimp_collector_u0_hash`、`modimp_collector_u1_hash`、`modimp_collector_collect_key`、`modimp_collector_finish_condition`
- region 级：`modimp_match_vs_texcoord_hash`、`modimp_match_vs_position_hash`

如果节点勾选 `Missing Contract -> Buffer Only` 且字段缺失，则不会伪造 hash 或 collector 配置，而是自动降级为 buffer-only，并在 `theherta4_ntmi_modimp_export_report.json` 中记录缺失字段。这个策略是有意的：这些字段来自 FrameAnalysis/Profile，错误伪造会生成看似完整但运行期错误的 INI。

## 7. 物体切换兼容

物体切换节点由 TheHerta4 的 `node_swap_processor.py` 集成进 `DrawCallModel.work_key_list`。本实现只读取 `is_swapkey=True` 的 `M_Key`，不读取形态键变量，避免生成未声明的 `$shapekey*` 条件。

INI patcher 会做以下修改：

- 确保 `[Constants]` 中有 `global $active0 = 0`。
- 为每个物体切换节点写入 `global persist $swapkeyN = 0` 或自定义变量名。
- 确保 `[Present]` 中有 `post $active0 = 0`。
- 为每个切换节点写入 `[KeySwap_NTMIModImp_N]`，避免和参考插件或其它后处理生成的 KeySwap 段名冲突。
- 在每个 `[TextureOverride...]` 下面加入 `$active0 = 1`。
- 根据参考插件 INI 里的 `; [mesh:<object>]` 注释定位对应 draw，包裹：

```ini
if $swapkey0 == 1
  drawindexed = ...
endif
```

多级/嵌套物体切换会保留蓝图里的 `&&` / `||` 条件关系。

## 8. 暂时跳过和限制

第一版重点是完整导出功能和 INI 物体切换兼容，以下内容暂时不强行处理：

- 非物体切换的形态键条件 INI 生成。
- 标准 SSMT 后处理节点默认自动执行。节点保留 `Run Compatible Postprocess` 选项，但默认关闭；即使手动打开，也只执行 `BufferCleanup`、`ResourceMerge`、`WebPanel`、`SliderPanel` 这类白名单节点，其余节点会跳过。
- 显式 `partNN` 拆分、BMC 多 part 精细控制。当前使用参考插件支持的 implicit `part00`。
- 缺失 runtime contract 时不尝试猜测 `match_vs_*` 或 collector 字段。
- 贴图 fallback 只能从 `modimp_texture_slots` 或 Blender 材质推导基础槽位，缺 hash 的贴图绑定会被参考插件忽略。

## 9. 验证策略

已做静态验证：

```powershell
python -m py_compile .\blueprint\node_result_ntmi_modimp.py .\blueprint\export_helper.py .\blueprint\model.py .\blueprint\chain_traverser.py .\blueprint\node_menu.py .\ui\ntmi_modimp\modimp_core.py .\ui\ntmi_modimp\export_tree_builder.py .\ui\ntmi_modimp\ini_swap_patcher.py .\ui\ntmi_modimp\export_ntmi_modimp.py
```

仍需在 Blender 内做运行时验证：

- 新节点能注册并出现在蓝图菜单。
- 蓝图链路连接到 `NTMI ModImp输出` 后能被 `BluePrintModel` 正确解析。
- 有完整 `modimp_*` 合同的参考插件导入对象能生成完整 Buffer 和 INI。
- 缺失合同字段时能降级 buffer-only，并生成 report。
- 物体切换节点能生成正确 `KeySwap` 和 `if` 包裹。

## 10. 后续建议

后续如果要提高完整度，建议按优先级推进：

1. 在 NTMI 节点面板增加合同字段诊断按钮，提前显示哪些 `modimp_*` 缺失。
2. 增加显式 part 生成策略，从物体集合、材质或用户字段推导 `partNN`。
3. 为确认兼容的后处理节点建立白名单，例如 BufferCleanup、ResourceMerge。
4. 如果未来要完全摆脱参考插件，可逐步把 `core/exporter.py` 中需要的纯函数抽成内部适配层；当前阶段不建议重写。

## 11. 手动导出目录

`SSMTNode_Result_Output_NTMIModImp` 增加了显式的 `Use Custom Export Directory` 开关。

- 关闭时：导出器忽略 `export_dir`，继续使用独立 NTMI 默认目录，即已保存 `.blend` 同目录下的 `NTMI_ModImp_Output`，或未保存 `.blend` 时的用户目录 `TheHerta4_NTMI_ModImp_Output`。
- 开启时：导出器使用输出节点上的目录选择器 `export_dir`。
- 开启但未选择目录时：导出会直接取消并给出明确错误，避免误写入默认目录。

## 12. 镜像补偿处理

参考 `mod_importer-main` 的导入器默认 `Mirror Flip=True`，导入对象会写入 `modimp_mirror_flip=True`。参考导出器导出时会优先读取对象级 `modimp_mirror_flip`，并再次执行 X 镜像补偿。

TheHerta4 的非镜像工作流在前处理阶段已经会对 `_copy` 导出副本再次镜像一次，把几何恢复到游戏导出空间。因此 NTMI ModImp 适配层在 `PreProcessHelper.execute_preprocess(...)` 后，会把本次导出副本继承来的 `modimp_mirror_flip=True` 显式改为 `False`，避免参考导出器重复镜像导致整模左右反转。

## 13. 输出节点默认项和名称解析

NTMI ModImp 输出节点现在默认开启手动导出目录、`Flip UV V` 和兼容后处理节点。保留临时合集、运行时形态键、默认镜像翻转保留为内部兼容字段，但不在节点 UI 暴露，默认均为关闭。

名称解析不再强制 `DrawIB-IndexCount-FirstIndex.Alias` 的点号格式。`DrawCallModel` 现在只要求能从名称前缀解析出 `DrawIB-IndexCount-FirstIndex`，后缀别名可以不存在，也可以使用 `_`、空格等分隔方式。源对象解析在精确名称和旧的点号别名找不到时，会按前缀匹配 Blender 场景对象。

## 14. 前置插件检测

输出节点的 `mod_importer-main` 路径现在是可选覆盖路径，不再是必填项。NTMI ModImp 导出会按以下顺序解析前置插件：

1. 节点手动填写的路径。
2. Blender 已安装/已启用的 `Mod Importer` 插件目录。
3. TheHerta4 相邻目录下的 `mod_importer-main`。
4. 默认本地路径 `E:\代码\mod_importer-main`。

节点 UI 会显示检测结果，并提供“检测前置插件”按钮。导出时如果找不到可用的 `core/exporter.py`，会明确提示安装/启用前置插件或手动设置插件根目录。
