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
The opt-in unified global vertex-group mode (checkbox `import_merged_vgmap`). All **Components** of a character share one global bone-id namespace; cross-part weights are legal. Import reverse-looks-up palettes from the FrameAnalysis dump and writes `VGMap`/`VGOffset`/`VGCount` back to the workspace json; export writes global bone ids and generates the runtime attach CS + INI sections. EFMI export follows the official EFMI 1.4.1 runtime architecture: per-component `TextureOverride_EntryPoint_ComponentN` (no DRAW_TYPE gating by explicit user decision) → mod-side `CommandList_Component_DrawInstances` glue (namespaced `$\EFMIv1\bones_count/component_count/instance_count` assignment, `Pool_ObjectSpatialIdentity`, `SpatialIdentity_IdentifyComponentInstances`, ConnectComponent callback mount) → runtime per-instance `MergedSkeleton_Apply` → `CommandList_Draw_ComponentN` custom draw callback. `bones_count` = `max(VGOffset + VGCount)` (subset exports keep workspace-global offsets). `$\EFMIv1\identification_min_components` is lowered to `min(component_count, 4)` so subset exports still pass spatial identification.
_Avoid_: global palette, unified rig

**VGMap**:
Per-**Component** mapping `{local bone index -> global bone id}`. ZZMI: cross-part bitwise matrix dedup (same-part never merged; canonical = first occurrence in sorted part order), plus a weighted-centroid gate for single-bone rigid parts — a bitwise hit involving a one-bone palette (rigid "single-weight" object) merges only if the driven-centroid distance < 0.05, since distinct attachment bones can coincide bitwise in the captured frame (head-ornament anchors); splitting a rigid part is content-harmless at runtime, merging wrongly is not. EFMI: layered-gate dedup — matrix diff ≥ `match_tolerance` (1e-3) never merges (hard gate), bitwise-equal merges outright, near-equal requires weighted-centroid confirmation (< 0.02; missing signatures stay split); same-part rejected, complete-graph anti-chaining; toggle `_DEDUP_ENABLED` in `common/efmi_skeleton.py`. The earlier EFMI multi-dimensional voting dedup (matrix as one of four votes; centroid/bbox/spread proximity could outvote matrix mismatch) was abolished after 42/195 measured false-merge groups on real data — geometry dimensions may only split, never merge. After any strategy change, wipe stale json caches via the panel button "清除骨骼合并VGMap缓存" (`EFMISkeletonMergeHelper.clear_vgmap_cache`, EFMI/ZZMI) before re-import.
_Avoid_: bone remap, index patch

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
