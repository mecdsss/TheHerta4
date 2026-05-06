# mod_importer 导出功能融合 TheHerta4 蓝图系统可行性报告

## 1. 结论摘要

结论：**可行，但不建议直接“把 mod_importer 的导出按钮塞进现有蓝图输出”**。  
最稳妥的方案是：

1. 在 TheHerta4 蓝图体系中新增一个**专用输出节点**，例如 `SSMTNode_Result_Output_ModImp`。
2. 该节点不直接复用 TheHerta4 现有 `Generate Mod` 的导出数据结构，而是新增一层**桥接适配器**：
   - 从蓝图解析结果中收集对象、DrawIB、IndexCount、FirstIndex、PartName、贴图标记、ShapeKey 信息；
   - 在 Blender 中构建一棵满足 `mod_importer` 约束的 `sourceIB -> region -> part` 导出集合树；
   - 写入 `modimp_*` 自定义属性与必要的 Blender Text JSON；
   - 最后直接调用 `mod_importer-main/core/exporter.py` 的 `export_collection_package(...)`。
3. 不建议直接复用 `mod_importer-main/operators.py` 里的 UI Operator 作为蓝图节点执行核心，因为它强依赖 scene 属性、集合命名约定和导出前整理流程。

综合评级：**中高可行性**  

- 纯“调用导出核心”层面：**高可行性**
- 纯“零改造接入”层面：**低可行性**
- “加桥接层 + 新输出节点”层面：**高可行性**

---

## 2. 当前两套系统的本质区别

### 2.1 TheHerta4 的蓝图导出模型

TheHerta4 当前的蓝图系统是以 **`SSMTNode_Result_Output` 为唯一主输出根节点** 的：

- 蓝图主输出节点定义在 [node_obj.py](E:\代码\TheHerta4\blueprint\node_obj.py:393)
- 导出按钮实际调用 `ssmt.generate_mod_blueprint`，入口在 [ui_func_export.py](E:\代码\TheHerta4\ui\ui_func_export.py:26)
- 导出逻辑会先解析蓝图，再生成 `BluePrintModel`，最后执行后处理节点，见：
  - [model.py](E:\代码\TheHerta4\blueprint\model.py:357)
  - [model.py](E:\代码\TheHerta4\blueprint\model.py:1258)

蓝图系统的关键特点：

- 输入是“对象处理链”
- 输出是 TheHerta4 自己的 Mod 生成逻辑
- 后处理节点通过 `SSMTSocketPostProcess` 挂接到输出节点
- 连通性判断、链路遍历、嵌套蓝图处理，都默认围绕 `SSMTNode_Result_Output` 展开

关键参考：

- [export_helper.py](E:\代码\TheHerta4\blueprint\export_helper.py:597)
- [export_helper.py](E:\代码\TheHerta4\blueprint\export_helper.py:680)
- [chain_traverser.py](E:\代码\TheHerta4\blueprint\chain_traverser.py:13)
- [model.py](E:\代码\TheHerta4\blueprint\model.py:406)

### 2.2 mod_importer 的导出模型

`mod_importer-main` 的导出核心并不是“面板按钮”，而是一个**严格依赖集合树合同的导出器**：

- 导出主入口是 [core/exporter.py](E:\代码\mod_importer-main\core\exporter.py:2734) 的 `export_collection_package(...)`
- UI Operator 只是前置整理后再调用它，见 [operators.py](E:\代码\mod_importer-main\operators.py:2221)

它要求输入必须是一棵符合约束的集合树：

`sourceIB(export root) -> region collection -> part collection -> mesh objects`

而且 region / part / object / collection 上都要挂载一批 `modimp_*` 自定义属性，例如：

- `modimp_kind`
- `modimp_profile_id`
- `modimp_source_ib_hash`
- `modimp_region_hash`
- `modimp_region_index_count`
- `modimp_region_first_index`
- `modimp_match_vs_texcoord_hash`
- `modimp_match_vs_position_hash`
- `modimp_texture_slots`
- `modimp_collector_*`
- `modimp_bmc_*`

这批属性与合同写入逻辑集中在：

- [operators.py](E:\代码\mod_importer-main\operators.py:607)
- [operators.py](E:\代码\mod_importer-main\operators.py:675)
- [operators.py](E:\代码\mod_importer-main\operators.py:827)
- [operators.py](E:\代码\mod_importer-main\operators.py:1033)

导出器还会在正式导出时校验 region 合同是否合法：

- [core/exporter.py](E:\代码\mod_importer-main\core\exporter.py:1121)
- [core/exporter.py](E:\代码\mod_importer-main\core\exporter.py:2637)

---

## 3. 为什么“直接融合”不可取

### 3.1 不能直接把 mod_importer 的 Operator 当成蓝图输出逻辑

`MODIMP_OT_export_collection_buffers` 会依赖 scene 上注册好的这些属性：

- `modimp_export_mode`
- `modimp_export_runtime_shapekeys`
- `modimp_runtime_shapekey_names`

定义位置：

- [properties.py](E:\代码\mod_importer-main\properties.py:244)
- [properties.py](E:\代码\mod_importer-main\properties.py:443)
- [properties.py](E:\代码\mod_importer-main\properties.py:452)
- [properties.py](E:\代码\mod_importer-main\properties.py:460)

同时它还默认前面已经有一套“导入/分析/同步 region metadata”的工作流：

- `_ensure_supported_profile(...)` 只支持 `yihuan`
- `_export_root_for_scene(...)` 依赖已有导出根集合
- `_sync_export_collection_metadata(...)` 会修补 region/part 元数据

关键位置：

- [operators.py](E:\代码\mod_importer-main\operators.py:66)
- [operators.py](E:\代码\mod_importer-main\operators.py:1033)
- [operators.py](E:\代码\mod_importer-main\operators.py:2160)

这意味着：

- 它不是一个“给一批对象就能导出”的纯函数式模块；
- 它是一个“假设前面的导入分析链都跑过”的半工作流式模块。

### 3.2 TheHerta4 蓝图当前没有现成的 `mod_importer` 集合合同

TheHerta4 蓝图导出链擅长的是：

- 物体处理链解析
- 多文件导出轮次
- ShapeKey 多轮导出
- 后处理节点串联

但它目前**并不产出** `mod_importer` 要求的集合树和 `modimp_*` 元数据合同。

所以直接融合会卡在这里：

- `mod_importer` 需要“区域集合 + part 集合 + 运行时合同”
- TheHerta4 当前产出的是“蓝图对象链 + 自身导出器上下文”

这两者不是同一种中间表示。

---

## 4. 真正可行的融合点

### 4.1 最佳复用点：`export_collection_package(...)`

最值得复用的是：

- [core/exporter.py](E:\代码\mod_importer-main\core\exporter.py:2734)

原因：

1. 这是 `mod_importer` 最稳定、最核心的导出实现。
2. 它比 UI Operator 依赖更少。
3. 只要你能构造出符合要求的 collection tree 和 runtime contract，它就能独立工作。
4. 这样可以避免把 `modimp` 面板属性系统整套复制进 TheHerta4。

### 4.2 需要自建的桥接层

真正要新增的是一个桥接器，例如可以设计成：

- `blueprint/modimp_bridge.py`
- `blueprint/node_result_modimp.py`

桥接层职责：

1. 从蓝图输出链收集目标对象
2. 解析每个对象的 region identity：
   - `draw_ib`
   - `index_count`
   - `first_index`
3. 按 `sourceIB -> region -> part` 组织导出集合
4. 回填 `modimp_*` 自定义属性
5. 生成或映射必要的 Blender Text JSON
6. 调用 `export_collection_package(...)`

这是整个融合任务里最关键、也是最有价值的一层。

---

## 5. TheHerta4 里哪些数据已经够用

### 5.1 对象的 DrawIB / IndexCount / FirstIndex

TheHerta4 现有对象命名和前缀系统，本身就能拿到大部分 region identity：

- `DrawCallModel` 会从对象名解析 `DrawIB-IndexCount-FirstIndex`，见 [draw_call_model.py](E:\代码\TheHerta4\common\draw_call_model.py:17)
- `ObjectPrefixHelper` 也能从前缀中拆出 `draw_ib/index_count/first_index`，见 [object_prefix_helper.py](E:\代码\TheHerta4\common\object_prefix_helper.py:104)

这意味着构造 region collection 所需的基础身份信息，**TheHerta4 是有的**。

### 5.2 PartName 与贴图标记

TheHerta4 的 `DrawIBModel` / `SubmeshMetadata` 已经能从 SubmeshJson 读取：

- `PartName`
- `TextureMarkUpInfoList`
- `CategoryHash`
- `VSHashList`

关键位置：

- [drawib_model.py](E:\代码\TheHerta4\common\drawib_model.py:92)
- [drawib_model.py](E:\代码\TheHerta4\common\drawib_model.py:102)
- [drawib_model.py](E:\代码\TheHerta4\common\drawib_model.py:328)
- [submesh_metadata.py](E:\代码\TheHerta4\common\submesh_metadata.py:96)
- [texture_metadata_helper.py](E:\代码\TheHerta4\common\texture_metadata_helper.py:98)

这部分对 `mod_importer` 很有帮助，因为它正好也关心：

- region / part 粒度
- 贴图语义映射
- VS 匹配合同

### 5.3 ShapeKey 相关信息

TheHerta4 已经有成熟的 ShapeKey 导出上下文：

- 蓝图层的 ShapeKey 收集与轮次控制： [export_helper.py](E:\代码\TheHerta4\blueprint\export_helper.py:917)
- 直出与多轮导出支持： [ui_func_export.py](E:\代码\TheHerta4\ui\ui_func_export.py:116)
- 运行时 ShapeKey 名称上下文： [export_helper.py](E:\代码\TheHerta4\blueprint\export_helper.py:12)

而 `mod_importer` 的导出器也支持 runtime shapekey：

- 排序逻辑： [core/exporter.py](E:\代码\mod_importer-main\core\exporter.py:511)
- 主导出入口参数： [core/exporter.py](E:\代码\mod_importer-main\core\exporter.py:2734)

所以 ShapeKey 不是阻塞项，反而是容易打通的一块。

---

## 6. 真正的难点

## 6.1 运行时合同不是 TheHerta4 现成产物

`mod_importer` region 合同里最硬的字段包括：

- `modimp_match_vs_texcoord_hash`
- `modimp_match_vs_position_hash`
- `modimp_collector_group_slot`
- `modimp_collector_collect_key`
- `modimp_collector_finish_condition`

校验位置：

- [core/exporter.py](E:\代码\mod_importer-main\core\exporter.py:1121)
- [core/exporter.py](E:\代码\mod_importer-main\core\exporter.py:2637)
- [core/exporter.py](E:\代码\mod_importer-main\core\exporter.py:2173)

这些字段原本来自 `mod_importer` 的 FrameAnalysis / Analyze 流程，不是普通对象处理链就能自然推出的。

这意味着如果你希望 **`generate_ini=True`**，就必须解决以下之一：

1. 从 TheHerta4 现有 workspace / import json / submesh json 中补齐这些合同；
2. 从 `mod_importer` 的分析产物里读取并转写；
3. 第一阶段先只支持 `generate_ini=False`，只导出 Buffer。

这是当前融合的**最大技术风险**。

## 6.2 region/part 集合树必须严格构建

`mod_importer` 导出器对集合树很严格：

- 根集合下不能直接挂 mesh
- 必须先有 region
- region 下再解析 part
- part index / BMC identity 也要正确

参考：

- [core/exporter.py](E:\代码\mod_importer-main\core\exporter.py:1209)
- [operators.py](E:\代码\mod_importer-main\operators.py:675)
- [operators.py](E:\代码\mod_importer-main\operators.py:695)

因此桥接器不能只“临时塞几个对象进去”，而要认真构造一棵合法的 export tree。

## 6.3 蓝图主干默认只有一个 Result Output

TheHerta4 当前多个核心逻辑都把 `SSMTNode_Result_Output` 写死为主输出：

- [model.py](E:\代码\TheHerta4\blueprint\model.py:406)
- [chain_traverser.py](E:\代码\TheHerta4\blueprint\chain_traverser.py:13)
- [export_helper.py](E:\代码\TheHerta4\blueprint\export_helper.py:680)

所以如果你想做“全新输出节点”，需要考虑两种方式：

### 方案 A：独立第二输出节点类型

优点：

- 语义清晰
- 可以把 TheHerta4 标准导出和 mod_importer 导出彻底分开

缺点：

- 需要扩展：
  - 输出节点查找逻辑
  - 连通性判断
  - 蓝图模型初始化
  - 嵌套蓝图输出识别

### 方案 B：继续沿用 `SSMTNode_Result_Output`，在其后处理输出链上挂一个“ModImp Export 节点”

优点：

- 对现有蓝图主干侵入最小
- 不需要重写主输出节点识别逻辑
- 可以把它做成一种特殊的 postprocess/export node

缺点：

- 从语义上看它不是纯 postprocess，而是第二套主导出器
- 设计上稍微别扭一些

**综合建议：如果优先考虑稳定落地，先做方案 B；如果优先考虑长期架构清晰，再做方案 A。**

---

## 7. 推荐融合路线

## 7.1 第一阶段推荐目标

建议第一阶段目标定义为：

**在 TheHerta4 蓝图中新增一个“ModImp Buffer Export”节点，只支持 Buffer 导出，不强制生成 INI。**

原因：

1. `export_collection_package(..., generate_ini=False)` 对 runtime contract 的依赖明显更小；
2. 可以先证明对象收集、集合桥接、Buffer 写出链路可用；
3. 能把最大风险从“整体不通”缩小为“INI/runtime contract 尚未接通”。

## 7.2 第二阶段目标

在第一阶段稳定后，再扩展：

- 支持 `generate_ini=True`
- 支持 texture slot / texture semantic 转换
- 支持 collector contract
- 支持 bone merge / BMC identity 精确映射

---

## 8. 推荐技术方案

## 8.1 节点侧设计

建议新增：

- `blueprint/node_result_modimp.py`

节点类型建议：

- `SSMTNode_Result_Output_ModImp`

节点属性建议：

- `export_dir`
- `generate_ini`
- `export_runtime_shapekeys`
- `runtime_shapekey_names`
- `profile_id`，默认 `yihuan`
- `build_temp_collection_tree`
- `keep_temp_collection_tree`

如果想走低侵入路线，也可以新增：

- `SSMTNode_PostProcess_ModImpExport`

这样它直接挂在现有 `Result Output` 的 postprocess socket 后面。

## 8.2 桥接层设计

建议新增：

- `blueprint/modimp_bridge.py`

建议职责拆分：

1. `collect_blueprint_export_objects(tree, context)`
2. `group_objects_by_source_ib_and_region(...)`
3. `build_modimp_export_tree(...)`
4. `fill_region_runtime_contract(...)`
5. `fill_part_contract(...)`
6. `invoke_modimp_export(...)`

## 8.3 复用方式

推荐方式不是 import 整个 `mod_importer-main` 插件包注册流程，而是**按模块导入核心函数**：

- `core.exporter.export_collection_package`

必要时也可参考但尽量少复用：

- `operators.py` 中的 `_mark_*`、`_auto_split_export_root_by_limits()` 一类辅助逻辑

更稳的做法是把这些小工具在桥接层中本地重写一份最小实现，避免：

- 强耦合 UI Scene 属性
- 强耦合未来 `mod_importer-main` 的内部私有函数

---

## 9. 可直接复用、建议复写、暂不建议复用的部分

### 9.1 可直接复用

- `export_collection_package(...)`
- `runtime shapekey` 相关导出行为
- `NTMI/YIHUAN` 的 buffer 写出格式逻辑

### 9.2 建议“参考实现后复写”

- `_mark_export_root_collection(...)`
- `_mark_region_collection(...)`
- `_mark_part_collection(...)`
- `_auto_split_export_root_by_limits(...)`

原因：

- 这些函数本身并不复杂；
- 但它们位于 `operators.py`，和 scene/UI 流程耦合较重；
- 在 TheHerta4 里维护一份裁剪版更安全。

### 9.3 暂不建议复用

- `MODIMP_OT_export_collection_buffers`
- `properties.py` 的整套 scene 属性注册
- `panel.py` 的 UI 逻辑

原因：

- 这些都是面板工作流层，而不是导出核心层。

---

## 10. 关键兼容性判断

## 10.1 Profile 兼容性

`mod_importer-main` 当前导出实现只支持：

- `YIHUAN_PROFILE`

见：

- [profiles.py](E:\代码\mod_importer-main\core\profiles.py:18)
- [operators.py](E:\代码\mod_importer-main\operators.py:66)

所以如果 TheHerta4 当前工程不是围绕这套 profile 目标在做，直接融合的收益会下降。

## 10.2 ShapeKey 兼容性

ShapeKey 层面是兼容的，甚至比较友好：

- TheHerta4 已有 ShapeKey 蓝图收集与导出状态管理
- `mod_importer` 也接受 runtime shapekey 名称列表

这块不构成主要阻碍。

## 10.3 贴图兼容性

贴图部分可以分成两层看：

1. **仅导出 Buffer**：贴图不是阻塞项
2. **导出带 INI 的完整包**：贴图语义、槽位、材质回填会明显增加复杂度

---

## 11. 实施复杂度评估

### 11.1 最小可用版

范围：

- 新增节点
- 新增桥接层
- 构建临时 export tree
- 调 `export_collection_package(..., generate_ini=False)`

复杂度：**中等**

### 11.2 完整版

范围：

- 支持 generate_ini
- 支持 region runtime contract
- 支持 texture marks / collector contract
- 支持 bone merge / BMC

复杂度：**中高到高**

---

## 12. 风险清单

### 高风险

1. `mod_importer` 的 runtime contract 来源不足，导致 INI 导出无法稳定工作。
2. TheHerta4 当前对象命名虽然能提供 region identity，但不一定能提供完整 collector contract。
3. 若要做“真正第二输出节点”，需要修改蓝图主干里多个把 `SSMTNode_Result_Output` 写死的地方。

### 中风险

1. 集合树临时构建与清理不当，可能污染用户场景。
2. part / BMC 映射不精确时，某些目标游戏的导出结果会错。
3. 贴图槽位语义映射如果只靠现有 TextureMarkUpInfo，可能不够完整。

### 低风险

1. Buffer-only 模式
2. Runtime shapekey 导出
3. 基于对象前缀解析 region identity

---

## 13. 最终建议

### 建议结论

**建议融合，但要按“桥接导出器”的思路做，不建议硬拼两套工作流。**

### 最优落地顺序

1. 先做一个 `ModImp Buffer Export` 节点
2. 节点内部通过桥接层构建临时 collection tree
3. 直接调用 `export_collection_package(..., generate_ini=False)`
4. 跑通后再补 `generate_ini=True`
5. 最后再决定要不要升级成真正的“第二输出节点类型”

### 架构建议

短期最稳方案：

- **保留现有 `SSMTNode_Result_Output` 主干**
- **新增一个挂在 postprocess 链后的专用导出节点**

长期更清晰方案：

- **新增独立 `SSMTNode_Result_Output_ModImp`**
- **扩展蓝图系统支持多种输出根节点**

---

## 14. 一句话判断

**这件事能做，最关键的不是“能不能调用 mod_importer 的导出函数”，而是“要不要为它补一层符合 modimp 合同的中间表示”。只要接受新增桥接层和新节点，这个融合就是可落地的。**

