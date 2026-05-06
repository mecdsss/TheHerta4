# mod_importer 前缀分组与独立 UI 导出体系详细分析

## 1. 直接结论

你的这个新思路，**总体上是对的，而且比“把 mod_importer 功能塞进现有蓝图输出链里”更合理**。

核心结论分两部分：

### 1.1 能不能直接用蓝图链路中的物体名称前缀来分组

**可以，但应该理解成“用前缀作为分组键”，而不是“直接拿前缀字符串硬代替整套合集合同”。**

更准确地说：

- **可以直接用前缀推导 `region` 分组**
- **可以直接用前缀推导大部分对象归属**
- **不建议直接从原始节点字符串分组**
- **建议从蓝图解析后的 `DrawCallModel / SubMeshModel / DrawIBModel` 分组**

### 1.2 能不能像 `ui/wwmi` 一样，在 `ui` 下新建一个完全独立的导出目录

**完全可行，而且这是我现在最推荐的实现方向。**

原因很明确：

- TheHerta4 现有架构本来就支持“按游戏/按导出后端拆独立 exporter”
- `ui/wwmi` 就是一套独立逻辑
- `ui/universal/*` 是另一套逻辑
- 所以你现在再加一个 `ui/<new_folder>`，从架构上完全顺路

但这里有一个非常重要的设计建议：

> **建议把“完全独立”理解为“相对 TheHerta4 现有 exporter 独立”，而不是“把 mod_importer 底层导出器也完全重写一份”。**

也就是说：

- TheHerta4 层：独立 exporter、独立配置生成、独立数据生成流程
- mod_importer 层：尽量复用它的 `core/exporter.py` 核心能力，不要全量重写

---

## 2. 现有架构对这条路线的支持程度

## 2.1 TheHerta4 当前本来就有“独立 UI 导出分支”架构

当前导出器选择是显式分发的，不是写死一套逻辑：

- 分发入口一： [direct_export.py](E:\代码\TheHerta4\blueprint\direct_export.py:136)
- 分发入口二： `export_parallel.py` 中也有一套同样的分发

当前已经存在两类导出体系：

### A. `ui/universal/*`

这一套是共享 `DrawIBExportBase` 的：

- 基类在 [drawib_export_base.py](E:\代码\TheHerta4\ui\universal\drawib_export_base.py:1)
- `ExportUnity` 在 [unity.py](E:\代码\TheHerta4\ui\universal\unity.py:15)
- 其它 `GIMI/HIMI/IdentityV/...` 也是类似结构

它们共同特点：

- 从 `BluePrintModel` 出发
- 解析出 `DrawIBModel`
- 生成 buffer
- 再生成 ini / 资源配置

### B. `ui/wwmi/*`

这一套是明显独立出来的：

- 主入口在 [wwmi_export.py](E:\代码\TheHerta4\ui\wwmi\wwmi_export.py:14)
- 数据模型也单独定义在 `drawib_model_wwmi.py`

它的特点就是：

- 自己的 `DrawIBModelWWMI`
- 自己的 buffer 生成
- 自己的 ini 生成
- 自己的资源段和 commandlist 生成

所以从架构经验上说：

> **你要做一个 `ui/modimp_export/` 或 `ui/ntmi_modimp/`，本质上就是再走一次 `ui/wwmi` 这条路。**

这不是逆着系统干，而是顺着已有模式扩展。

---

## 3. 前缀分组这件事，到底能不能成立

## 3.1 前缀里已经有足够强的结构化信息

TheHerta4 当前前缀系统不是随便一段文本，而是结构化前缀：

- 前缀解析在 [object_prefix_helper.py](E:\代码\TheHerta4\common\object_prefix_helper.py:119)
- 节点有效对象名构建在 [object_prefix_helper.py](E:\代码\TheHerta4\common\object_prefix_helper.py:169)

当前能直接解析出：

- `draw_ib`
- `index_count`
- `first_index`
- `component`

也就是说，对象名前缀本身就已经是：

`DrawIB-IndexCount-FirstIndex`

这和 `mod_importer` 侧用于识别对象/region 的命名规则是高度同构的：

- `mod_importer` 对对象名的 region identity 解析见 [operators.py](E:\代码\mod_importer-main\operators.py:799)
- region collection 命名规则见 [operators.py](E:\代码\mod_importer-main\operators.py:819)

因此从“分组键”角度看：

**前缀绝对是可用的。**

## 3.2 但不建议直接从“原始节点前缀字符串”分组

这里是最关键的细化结论。

你现在问的是“能不能直接使用蓝图链路中的物体名称前缀来进行分组”，答案是：

**可以使用，但最好不要直接读节点上的原始前缀字段，而要读蓝图解析后的中间模型。**

建议使用顺序：

1. `BluePrintModel.ordered_draw_obj_data_model_list`
2. `SubMeshModel`
3. `DrawIBModel`
4. 最后才是节点原始前缀

原因：

- 节点前缀是“输入侧意图”
- `DrawCallModel` / `SubMeshModel` 是“导出侧最终事实”

蓝图处理中间会发生这些事情：

- rename
- multifile 切轮
- object duplicate / copy
- shapekey 轮次处理
- cross-IB
- object mapping

这些都会让“原始节点名”和“最终导出对象名”产生偏移。  
而 `DrawCallModel` 里保存的是已经进入导出解析后的结构化匹配信息：

- `match_draw_ib`
- `match_index_count`
- `match_first_index`

定义见 [draw_call_model.py](E:\代码\TheHerta4\common\draw_call_model.py:17)

所以真正推荐的做法是：

> **先让蓝图正常解析成 `BluePrintModel`，再从 `ordered_draw_obj_data_model_list` / `SubMeshModel` 里提取分组键，而不是绕过蓝图结果直接从节点 UI 文本分组。**

---

## 4. 前缀能直接映射到 mod_importer 的哪一层

`mod_importer` 的导出集合树要求是：

`sourceIB root -> region -> part`

核心约束在：

- [core/exporter.py](E:\代码\mod_importer-main\core\exporter.py:1210)
- [core/exporter.py](E:\代码\mod_importer-main\core\exporter.py:2744)

### 4.1 可以直接映射的层

#### A. `region`

这是最适合直接由前缀驱动的一层。

因为 `region collection` 的识别本身就是靠：

- hash
- index_count
- first_index

而你当前前缀里正好就有这三项。

#### B. 对象归属

同一个前缀的对象，理论上就应该归到同一个 region。

如果你用 `DrawCallModel.get_unique_str()` 来做聚合，逻辑更稳：

- 定义见 [draw_call_model.py](E:\代码\TheHerta4\common\draw_call_model.py:89)

### 4.2 不能只靠前缀完全解决的层

#### A. `sourceIB root`

这个要看你当前工程里：

- 是否所有对象都属于同一个 `draw_ib`
- 是否一个蓝图里混了多套 DrawIB

如果当前导出对象都来自同一个 `draw_ib`，那 root 可以直接用这个值。

如果蓝图里同时存在多个 `draw_ib`：

- 就必须先按 `draw_ib` 分根
- 一个 root 对应一个 exporter 执行上下文

#### B. `part`

`part` 不是总能单靠前缀准确推出。

原因是 `mod_importer` 的 `part` 语义更像：

- export sub-collection
- bone palette split chunk
- 明确 partNN

而前缀只告诉你：

- DrawIB
- IndexCount
- FirstIndex

它不直接告诉你：

- `part_index`
- BMC chunk index
- 同 region 下是否还要细分多个 part

不过这里有个非常重要的好消息：

> **`mod_importer` 导出器允许 region 下直接放 mesh，把它当成一个隐式 `part00`。**

证据在 [core/exporter.py](E:\代码\mod_importer-main\core\exporter.py:1260) 之后的 `_resolve_export_parts(...)`：

- 如果 region 下有 direct meshes 且没有显式 part collection
- 它会自动生成一个隐式 `part00`

这意味着：

### 第一版完全可以不显式造 `partNN`

你只需要：

1. 先按 prefix/unique_str 分出 region
2. 把同组 mesh 放进对应 region collection
3. 让导出器把该 region 当成 implicit `part00`

这会大幅降低第一版实现复杂度。

---

## 5. 为什么这条路线比“蓝图直接挂输出节点”更合理

如果你直接在蓝图层做一个“新输出节点”，你还是会遇到一个问题：

TheHerta4 当前主导出逻辑是围绕 `BluePrintModel -> Exporter` 这套结构跑的。

而你现在提的这条路线，本质上更像是：

> **在现有 BluePrintModel 解析结果之上，单独再走一套“modimp 风格导出后端”。**

这和 `ui/wwmi` 的思路完全一致：

- 蓝图负责给你“已经整理好的导出对象语义”
- `ui/<backend>` 负责决定“怎么写 buffer / 怎么写配置”

所以从职责划分上看：

- 蓝图层：不要过度承载导出后端差异
- `ui/<new_exporter>`：专门承载 modimp 风格导出

这个边界是干净的。

---

## 6. 新建独立 UI 目录这件事，架构上怎么做最顺

## 6.1 推荐目录形态

推荐新建一个和 `ui/wwmi` 同级的目录，例如：

- `ui/modimp_export/`

或更明确一点：

- `ui/ntmi_modimp/`

我更推荐第二种命名，因为它更能表达“这是异环/modimp 风格导出后端”。

建议结构：

```text
ui/ntmi_modimp/
  __init__.py
  export_ntmi_modimp.py
  export_tree_builder.py
  runtime_contract_builder.py
  config_generator.py
  buffer_generator.py
  metadata_mapper.py
```

### 推荐职责

#### `export_ntmi_modimp.py`

主入口，提供和现有 exporter 一致的接口：

- `export()`
- `export_buffers_only()`

#### `export_tree_builder.py`

负责从 `BluePrintModel` 构建临时 `sourceIB -> region -> part` collection tree。

#### `runtime_contract_builder.py`

负责补齐：

- `modimp_match_vs_texcoord_hash`
- `modimp_match_vs_position_hash`
- `modimp_collector_*`
- `modimp_texture_slots`

#### `config_generator.py`

负责配置表/ini 风格文本生成。  
这里可以：

- 直接复用 `mod_importer` 导出器产出的 ini
- 或者只做 TheHerta4 自定义补充层

#### `buffer_generator.py`

负责底层 buffer 写出调度。  
这里不建议自己重写底层字节格式，尽量复用 `mod_importer` 核心导出器。

#### `metadata_mapper.py`

负责把：

- `DrawIBModel`
- `SubMeshMetadata`
- `TextureMarkUpInfoList`
- `part_name`

映射成 `modimp` 需要的 region runtime contract。

---

## 7. 独立体系应该“独立到什么程度”

这是这次分析里最重要的架构判断之一。

## 7.1 推荐的独立程度

建议你做的是：

### 对 TheHerta4 现有 exporter 独立

- 独立 buffer 生成调度
- 独立 config 生成
- 独立数据组织
- 独立目录结构
- 独立类与入口

### 但不要对 mod_importer 底层格式实现完全重写

也就是说：

- **不要复用 `ExportUnity/ExportWWMI`**
- **可以复用 `mod_importer-main/core/exporter.py`**

这是最划算的平衡点。

## 7.2 为什么不建议全量重写 mod_importer 的底层导出器

`mod_importer-main/core/exporter.py` 已经包含：

- strict collection tree 解析
- part/palette 约束
- runtime shapekey
- texture preflight
- ini 写出
- NTMI 资源段组织

主入口在 [core/exporter.py](E:\代码\mod_importer-main\core\exporter.py:2734)

如果你在 TheHerta4 的 `ui/<newfolder>` 里把这套逻辑再抄一份：

- 维护成本会非常高
- 两边 bugfix 会分叉
- 后续任何格式调整都要改两份

因此我建议：

> **独立的是“编排层”和“中间表示构建层”，不是“最终导出字节和配置格式层”。**

---

## 8. 用前缀分组后，第一版最优实现路径

## 8.1 第一版目标建议

建议第一版只做：

- 独立 exporter 目录
- 从 `BluePrintModel` 提取分组
- 自动建临时 export tree
- 按 prefix 推导 region
- region 下直接放 mesh，当 implicit `part00`
- 直接调用 `export_collection_package(...)`

暂时不要第一版就上：

- 手写复杂 part 细分
- 复杂 collector contract
- 全量贴图语义修正
- 手写完整 config 风格复制

## 8.2 第一版分组策略建议

建议分组键使用：

`(draw_ib, index_count, first_index)`

数据来源不要直接用节点前缀，而用：

- `DrawCallModel.match_draw_ib`
- `DrawCallModel.match_index_count`
- `DrawCallModel.match_first_index`

由此生成：

- `root_key = draw_ib`
- `region_key = f"{draw_ib}-{index_count}-{first_index}"`

然后：

1. 先按 `draw_ib` 分 root
2. 再按 `(draw_ib, index_count, first_index)` 分 region
3. 同 region 下对象直接挂到 region collection
4. 让 `mod_importer` 自动当 `part00`

这个路线风险最低。

---

## 9. 真正需要注意的坑

## 9.1 不要把“前缀分组”误认为“前缀等于完整合同”

前缀只能解决：

- 分组
- 归属
- 基础 identity

前缀不能天然解决：

- collector contract
- VS 匹配 hash
- texture slot metadata
- BMC chunk identity

这些仍然需要从：

- `SubmeshMetadata`
- `SubmeshJson`
- `TextureMarkUpInfoList`
- 或额外分析结果

里补出来。

## 9.2 如果一上来就要求“完全独立配置表生成”

如果你这里的“完全独立配置表生成”是指：

> 不调用 `mod_importer` 的 ini 生成，而是在 TheHerta4 里自己重新写一套

那复杂度会明显上升。

原因是这相当于要在 `ui/<newfolder>` 里自己重写：

- region runtime contract 消费
- ntmi resource sections
- texture binding
- collector commandlist
- runtime shapekey sections

所以我的建议是：

### 第一阶段

让 `ui/<newfolder>` 独立决定流程，但仍调 `mod_importer` 的核心导出器来产最终 config/data。

### 第二阶段

如果后面你发现 `mod_importer` 的 config 风格必须二次定制，再在 `ui/<newfolder>` 里逐步替换 config generator。

---

## 10. 这条路线和现有 LogicName 体系的关系

这里还有一个非常重要的设计点。

TheHerta4 当前 exporter 选择是通过 `logic_name` 分发的：

- [direct_export.py](E:\代码\TheHerta4\blueprint\direct_export.py:136)

而 `logic_name` 在整个工程里不只决定 exporter，还影响：

- mesh create
- obj buffer export
- tangent / color / rotation 逻辑
- 某些 shapekey 分支

这意味着：

## 10.1 不建议只是为了新 exporter 就随便新增一个 LogicName

因为一旦新增 `LogicName`，你就得审视很多公共模块分支是否也要跟着变。

## 10.2 更好的做法

如果你的目标只是：

- 同一个游戏
- 同样的蓝图输入
- 但换一套导出后端

那么更合理的设计不是“新增游戏逻辑名”，而是：

### 方案 A

新增一个独立输出节点，节点内部直接选 `ExportNTMIModImp`

### 方案 B

在现有输出节点上增加“导出后端”选择项：

- `default`
- `wwmi_style`
- `modimp_style`

然后 exporter 分发不再只看 `logic_name`，而是看：

- `logic_name`
- `export_backend`

这会比乱加一个新 `LogicName` 更干净。

---

## 11. 我对这条路线的最终评价

## 11.1 从工程实现角度

这条路线比“改蓝图主干”更稳。

因为你做的其实不是：

- 改蓝图语义

而是：

- 在蓝图解析结果之上加一个新的导出后端

这正是 `ui/wwmi` 已经证明过的模式。

## 11.2 从维护角度

这条路线长期也更健康。

因为以后你会得到一套清晰边界：

- 蓝图层：负责整理对象语义
- `ui/ntmi_modimp`：负责 modimp 风格导出
- `mod_importer core`：负责底层格式输出

三层职责不混。

## 11.3 从落地顺序角度

建议顺序是：

1. 先做 `ui/<newfolder>` 独立 exporter
2. 先用前缀/DrawCallModel 做 region 分组
3. 先让 region 直接承载 mesh，当 implicit `part00`
4. 先跑通 buffer + 基础 config
5. 再补 runtime contract
6. 再补高级 texture / collector / BMC

---

## 12. 一句话总结

**可以直接把“物体名称前缀”当成分组键来驱动 mod_importer 风格导出，而且最推荐的实现方式，就是在 `ui` 下新开一套和 `wwmi` 同级的独立 exporter 目录；但分组最好基于蓝图解析后的结构化模型来做，底层导出格式最好复用 mod_importer 的核心导出器，而不是全量重写。**

