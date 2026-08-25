# TheHerta4

TheHerta4 is a Blender addon for SSMT4 workflows. It imports extracted game model data into Blender, prepares objects for export through a blueprint-driven pipeline, and generates game-specific mod output.

## Language

**Blueprint**:
A node-graph configuration that describes how selected Blender objects are preprocessed and exported.
_Avoid_: graph, tree, pipeline config

**DrawIB**:
A draw-call oriented data grouping parsed from extracted buffers and reused as an export unit.
_Avoid_: mesh group, generic part

**Component**:
A finer-grained subdivision inside a **DrawIB**, typically keyed by index ranges when a game workflow needs per-part processing.
_Avoid_: object, collection

**GameType**:
The game-specific buffer layout and export rules that control how imported data is interpreted and how output files are written.
_Avoid_: preset, template

**Preprocess**:
The export preparation phase that clones, merges, normalizes, and modifier-applies Blender objects before buffer calculation.
_Avoid_: warmup, cleanup

**Non-mirror workflow**:
An import/export workflow that uses object-level X-scale flipping instead of mutating lower-level mesh data structures.
_Avoid_: mirror hack

**Deform pass**:
In GPU pre-skinning games (e.g. ZZMI/ZZZ), the pointlist vertex-shader pass that reads bind-pose buffers + per-part bone palette (vs-t0) and stream-out writes skinned vertices. Bone data lives here, never on the render draw.
_Avoid_: skinning draw, compute skinning

**Merged skeleton**:
The opt-in unified global vertex-group mode (checkbox `import_merged_vgmap`). All **Components** of a character share one global bone-id namespace; cross-part weights are legal. Import reverse-looks-up palettes from the FrameAnalysis dump and writes `VGMap`/`VGOffset`/`VGCount` back to the workspace json; export writes global bone ids and generates the runtime attach CS + INI sections. Because exported components render one frame behind (deform reads the previous frame's fully-attached skeleton, `[Present]`-timed direct-copy attach), **non-generated components (in the workspace but absent from the export) get the same one-frame delay via a per-component double buffer**: their deform pass copies vs-t0 into `ResourceZZPalette_<DrawIB>` and rebinds vs-t0 to `ResourceZZDelayedPalette_<DrawIB>` (previous frame), advanced at `[Present]` by the same direct-copy CS — so the whole character is frame-aligned. EFMI export follows the official EFMI 1.4.1 runtime architecture: per-component `TextureOverride_EntryPoint_ComponentN` (no DRAW_TYPE gating by explicit user decision) → mod-side `CommandList_Component_DrawInstances` glue (namespaced `$\EFMIv1\bones_count/component_count/instance_count` assignment, `Pool_ObjectSpatialIdentity`, `SpatialIdentity_IdentifyComponentInstances`, ConnectComponent callback mount) → runtime per-instance `MergedSkeleton_Apply` → `CommandList_Draw_ComponentN` custom draw callback. `bones_count` = `max(VGOffset + VGCount)` (subset exports keep workspace-global offsets). `$\EFMIv1\identification_min_components` is lowered to `min(component_count, 4)` so subset exports still pass spatial identification.
_Avoid_: global palette, unified rig

**VGMap**:
Per-**Component** mapping `{local bone index -> global bone id}`. Every map completely covers `0..VGCount-1`; an all-zero EFMI matrix does not participate in deduplication but still owns an independent stable slot, so import and export never disagree over a sparse map. ZZMI: parts are grouped into **SkeletonGroup**s by their render-pass vs-cb1 object transform (palette skins into object space; the render VS places it via cb1 — palette and cb1 are paired 1:1 per object). Dedup runs within a group (cross-part bitwise + weighted-centroid gate for single-bone rigid parts: a bitwise hit involving a one-bone palette merges only if driven-centroid distance < 0.05 — distinct attachment bones can coincide in the captured frame; splitting a rigid part is content-harmless, merging wrongly is not). Bone ids are global (group bases concatenated), so joined objects within a group are unambiguous. **Each group's full-width merged skeleton is filled by direct copies of its own bones only — CB1 calibration is removed (2026-08-25 user decision): no cb1 captures, no foreign-bone writes, cross-group bone merging is forbidden** (the export guard `_warn_cross_group_bone_references` loudly flags any vertex referencing a non-own-group bone id, which would collapse to origin at runtime). Json carries `SkeletonGroup`; imported objects land in per-group `SkeletonGroup_<N>` collections. EFMI: 矩阵 maxdiff ≥ `match_tolerance` (默认 1e-3) 是硬拒绝；矩阵逐位相同在有扩散采样时也要通过接触权重一致性、无采样时才兼容直接合并；近似矩阵优先在绑定姿态空间比较两组正权重采样的接触扩散场（覆盖率≥30%、原始权重平均误差≤0.20，弱权重点至少保留最大权重 25% 的评估影响），缺少扩散采样才回退到加权质心阈值。接触投影允许不同网格密度的多对一最近点，但至少需要多个不同目标支持点；最近点使用分块精确搜索，且多层表面会先应用法向/切向兼容约束再选最近点，避免较近的错误层遮挡正确上/下层。每对 Component 的通过边按证据等级、矩阵差、权重误差、覆盖缺口和空间误差动态收紧成一对一匹配；同一 local 在目标 Component 中只能保留一个候选，完全同分才按稳定 id 决胜。弱权重下限只改变误差评估影响、不会改写参与比较的原始权重值。同部件拒绝、合并后扩散连通图复核，`_DEDUP_ENABLED` 默认开启。写回的 `VGMapAlgorithmVersion` 会自动使旧策略缓存失效；策略或 Position/Blend 数据变化后也可先用面板“清除骨骼合并VGMap缓存”（`EFMISkeletonMergeHelper.clear_vgmap_cache`，EFMI/ZZMI）再导入。

**Workspace skeleton source cache**: clearing VGMap must not make FrameAnalysis a permanent dependency. EFMI copies both the full vs-t0 bone pool (`BoneMatrixFileName`) and the exact instance-config CB (`InstanceConfigFileName` + `InstanceConfigFirstConstant`) used to select the component's 256-bone segment; single-LOD and multi-LOD rebuilds run the same slicer against these workspace files when the dump is absent. ZZMI copies the deform palette plus the exact valid render CB1 that produced `SkeletonGroup`; `ObjectCB1CacheValid=False` records an observed-but-invalid CB1 so a stale file cannot change cache-only grouping. A source marked `ObjectCB1CacheValid=True` is a strong contract: if every sibling copy is missing or invalid and the dump is unavailable, rebuild fails explicitly without rewriting `SkeletonGroup`; it never silently degrades to independent grouping. Dump-backed and workspace-only rebuilds must produce identical `VGMap`, `VGOffset`, LOD correspondence, and `SkeletonGroup`. While a dump is still available, an otherwise-idempotent import must migrate legacy workspaces that lack this source-cache contract; an unmarked legacy ZZMI CB1 is never trusted for cache-only regrouping. Each component's source files and JSON are staged and committed as one rollback-capable file transaction, so a second-source or JSON failure cannot leave mixed generations. If any target in an EFMI/ZZMI pre-generation batch fails, that import operation disables merged VGMap for the whole batch, turns off the merged-VGMap option for subsequent export, and imports every Component through the ordinary local-group path; stale JSON can never create a mixed global/local namespace or an import/export mode split. Partitioned workspaces are resolved through child directories carrying `Config.json`; duplicate LOD/component keys across partitions are ambiguous and fail explicitly rather than writing the wrong JSON.

**EFMI multi-LOD VG correspondence**: import keeps the original per-LOD groups until both LODs have been collected. It first matches LOD0/LOD1 submesh components one-to-one by their weighted diffusion point clouds (symmetric nearest-point distance, bounds and extent as stabilizers), then matches raw local groups only inside each paired component; weighted center is primary for local matching and matrix is a secondary discriminator. This cross-LOD tolerance is intentionally broader than the intra-LOD 1e-3 hard gate because the two dumps can contain different capture transforms; it never relaxes the intra-LOD `build_vg_maps` gate. LOD0 is the baseline and runs the weight-diffusion dedup once. By default, LOD1 projects the LOD0 partition onto its own original slots; unmatched LOD1 extras remain identity slots, so it may retain more groups but does not lose a baseline group merely because its own point cloud differs. The `EFMI LOD 分组投影` checkbox selects this mode; when disabled, both LODs keep the component correspondence ledger but run dedup independently. JSON records `EFMILODActualGroupCount`, `EFMILODMissingBaselineCount`, `EFMILODProjection`, and per-local `EFMILODCorrespondence`; runtime `VGOffset` spaces and palette attachment remain independent per LOD.
_Avoid_: bone remap, index patch

EFMI weight diffusion is layer-aware: when local PCA can identify two approximately parallel
surface point clouds, matching may cross a bounded normal-direction gap (e.g. thigh and tights)
while still requiring low tangential projection error and normal dot >= 0.70. Geometry constraints
are applied before nearest-point selection, so a closer incompatible skirt layer cannot hide a
slightly farther compatible layer. Merged groups are then rechecked as a connected weight-diffusion
graph: every member needs a bridge and any disconnected component is split back out, so a plane
plus multiple groove bottoms can merge through local bridges without admitting an isolated group.
It does not require shared vertices or topology connectivity; volume-like point clouds without
reliable normals use the bounded diffusion corridor rather than a topology-only test.
当同一 Component 的一个 local 命中多个候选时，矩阵差先按 `1e-3 → 1e-4 → 1e-5 → 1e-6`
有限收紧；到 `1e-6` 仍无法区分才比较扩散证据、权重、覆盖率和空间误差。全局并查集并入
也沿用矩阵优先顺序，避免扩散更近但矩阵略差的覆盖层链先占用目标 local。

## Relationships

- A **Blueprint** drives one **Preprocess** flow and one export flow
- A **GameType** determines how a **DrawIB** and its **Components** are interpreted during import and export
- A **DrawIB** may contain multiple **Components**
- The **Non-mirror workflow** can wrap both import and export behavior
- A **Merged skeleton** spans several **DrawIB**s: each **Component**'s palette attaches into it at its `VGOffset`, and **Deform pass** draws skin against it one frame behind (uniform lag, no intra-frame inconsistency)

## Example dialogue

> **Dev:** "Should this logic live on the **Blueprint**, or is it really part of **Preprocess**?"
> **Domain expert:** "If it changes object state before buffer generation, treat it as **Preprocess**. The **Blueprint** only declares which flow should happen."

## Flagged ambiguities

- "export" can mean buffer-only export or full mod package output. Prefer "buffer export" or "mod export" when the distinction matters.
- "**Component**" and Blender object are not interchangeable terms. A single object may contribute data to one or more **Components** depending on the game workflow.
