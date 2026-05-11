# Upstream 2026-05-11 + 691f84 Migration Plan

## Objective

Integrate only the upstream capabilities that are missing or incomplete in the local branch, while keeping the local architecture as the primary path.

This plan is intentionally biased toward the current local pipeline:

- Keep local `NTMI` / `NTEMI` blueprint, direct-export, multifile, shapekey, and ModImp flow as the main architecture.
- Do not introduce upstream `ui/universal/ntemi.py` as a second competing export path.
- Do not convert `import_merged_vgmap` from local `BoolProperty` to upstream enum in this phase.
- Do not introduce upstream alias-driven renaming flow in this phase unless explicitly requested later.

## Operator Impact

The migration mainly affects existing operators rather than adding new ones:

- `ssmt4.import_all_from_workspace`
- `ssmt4.import_raw`
- `ssmt.generate_mod_blueprint`
- `ssmt.generate_selected_blueprint_mod`

No new Blender operator class is required for this migration phase.

## Classification

### A. Missing locally and should be integrated

1. WWMI / NTEMI import support for:
   - `VertexOffset`
   - `VertexCount`
   - `VGOffset`
   - `VGCount`
   - `VGMap`
   - `ShapeKeysInfo`
   - `ShapeKeyOffset`
   - `ShapeKeyVertexId`
   - `ShapeKeyVertexOffset`
   - structured category types `BlendWeight` and `TangentFrame`

2. WWMI import shapekey reconstruction from extracted buffers.

3. WWMI merged-VG import fallback when `VGMap` is partial or incomplete.

4. WWMI synthetic submesh metadata for texture export:
   - `submesh_model_list`
   - `part_name_submesh_dict`
   - `get_submesh_texture_markup_info_list`

5. WWMI export skeleton binding fix:
   - use `ref` for `vs-cb3` / `vs-cb4`
   - fix missing `endif`

6. Small robustness fixes:
   - safe parse of deduped texture filename
   - safer collection assertion
   - standardized fatal raising path

### B. Already present locally or superseded by better local design

1. Upstream independent `ExportNTEMI` path in `ui/universal/ntemi.py`
   - superseded by local `NTMI` / `NTEMI` blueprint and direct-export architecture
   - do not import

2. Upstream `component_count` loop workaround in `ui/wwmi/wwmi_export.py`
   - local code already has `_get_component_index_from_name()` based on `ObjectPrefixHelper`
   - local solution is better

3. Upstream alias-based export naming
   - local branch does not yet use the same alias infrastructure
   - defer to a separate feature migration

### C. Risky or deferred

1. Converting `import_merged_vgmap` from `BoolProperty` to enum
   - touches UI, import path, export path, and behavior assumptions
   - defer

2. Routing `NTEMI` standard export through upstream `ExportNTEMI`
   - conflicts with local `blueprint/direct_export.py`, `ntmi_export_modimp.py`, `ntmi_multifile.py`, `ntmi_shapekey.py`
   - reject in this phase

## File-by-File Function-Level Migration Plan

### 1. `common/submesh_json.py`

#### Add new dataclass fields to `SubmeshJson`

Add these fields:

- `VertexOffset: int = 0`
- `VertexCount: int = -1`
- `CB4Hash: str = ""`
- `BoneMatrixFileName: str = ""`
- `VGOffset: int = 0`
- `VGCount: int = 0`
- `VGMap: dict = field(default_factory=dict)`
- `ShapeKeysInfo: dict = field(default_factory=dict)`

#### Update `SubmeshJson.parse_json_dict()`

Parse and store:

- `VertexOffset`
- `VertexCount`
- `CB4Hash`
- `BoneMatrixFileName`
- `VGOffset`
- `VGCount`
- `VGMap`
- `ShapeKeysInfo`

#### Notes

- Keep existing local field parsing untouched.
- This is foundational for the rest of the import chain.

### 2. `common/ssmt_import_helper.py`

#### Update `create_mesh_from_json()`

Required changes:

- Change `parse_category_buffers()` return value from:
  - `elements, vb_data, vb_vertex_count`
  to:
  - `elements, vb_data, vb_vertex_count, shapekey_buffers`

- Import `GlobalProterties`.
- Derive:
  - `wwmi_vg_map = submesh_json.VGMap if submesh_json.VGMap and GlobalProterties.import_merged_vgmap() else None`

- Pass the following new keyword args into `MeshCreateHelper.create_mesh_object()`:
  - `wwmi_shapekey_buffers`
  - `wwmi_vertex_offset`
  - `wwmi_vertex_count`
  - `wwmi_vg_map`
  - `wwmi_vg_offset`

#### Update `parse_index_buffer(submesh_json)`

Required behavior:

- Preserve current logic for normal imports.
- When `VertexOffset > 0` and `VertexCount > 0`, subtract `VertexOffset` from the loaded IB values.
- Do not subtract when `VertexCount <= 0`.

Reason:

- sliced VB imports need local indices
- full VB imports must keep global indices

#### Update `parse_category_buffers(submesh_json)`

Required behavior:

- Introduce:
  - `STRUCTURED_BUFFER_TYPES = {"Normal", "BlendWeight", "TangentFrame"}`
  - `SHAPEKEY_TYPES = ("ShapeKeyOffset", "ShapeKeyVertexId", "ShapeKeyVertexOffset", "ShapeKeyScale")`

- Parse all `STRUCTURED_BUFFER_TYPES` through `parse_normal_category_buffer(...)`.
- Pass:
  - `vertex_slice_offset = submesh_json.VertexOffset`
  - `vertex_slice_count = submesh_json.VertexCount`

- Collect raw shapekey buffers into a dict when category type is in `SHAPEKEY_TYPES`.

- Return:
  - `elements, vb_data, vb_vertex_count, shapekey_buffers`

#### Update `parse_normal_category_buffer(category_buffer, vertex_slice_offset=0, vertex_slice_count=-1)`

Required behavior:

- Keep current dtype-based read path.
- If `vertex_slice_count > 0`, slice `category_buffer_data` to:
  - `[vertex_slice_offset : vertex_slice_offset + vertex_slice_count]`

#### Notes

- Do not remove local `DynamicBlend` handling.
- This file is the central import compatibility patch.

### 3. `common/mesh_create_helper.py`

#### Update `create_mesh_object(...)`

Extend signature with:

- `wwmi_shapekey_buffers: dict | None = None`
- `wwmi_vertex_offset: int = 0`
- `wwmi_vertex_count: int = -1`
- `wwmi_vg_map: dict | None = None`
- `wwmi_vg_offset: int = 0`

#### Replace Metadata-based merged VG import wiring

Current local behavior:

- reads `Metadata.json`
- derives `component`
- passes `component` into `import_vertex_groups()`

New desired behavior:

- if `wwmi_vg_map` exists, build a lightweight component-like object with:
  - `vg_map`
  - `vg_offset`

- otherwise keep `component = None`

- pass that into `import_vertex_groups()`

Important:

- remove the hard dependency on `Metadata.json` for this import path
- keep the rest of the local material / transform / import flow unchanged

#### Update `import_vertex_groups(mesh, obj, blend_indices, blend_weights, component)`

Required behavior:

- preserve current local behavior when `component is None`
- when `component` exists:
  - compute `max_valid_group_id` from imported indices
  - compute `mapped_group_ids` from `component.vg_map`
  - use `vg_offset = getattr(component, "vg_offset", 0)`
  - total vertex group count should be:
    - `max(max(mapped_group_ids), vg_offset + max_valid_group_id) + 1`
  - when a local blend index has no explicit mapping in `vg_map`, fallback to:
    - `vg_offset + local_index`

Compatibility requirement:

- support both string keys and int keys in `vg_map`

#### Add new function `import_shapekeys_wwmi(mesh, obj, shapekey_buffers, vertex_offset, vertex_count)`

Port the upstream WWMI shapekey import logic, but keep local style.

Required behavior:

- read:
  - `ShapeKeyOffset`
  - `ShapeKeyVertexId`
  - `ShapeKeyVertexOffset`

- create `Basis` if absent
- reconstruct `Deform {id}` shapekeys
- filter global vertex ids into local sliced range using `vertex_offset` and `vertex_count`
- use the corrected final indexing rule from upstream `74f1354`:
  - `dx_idx = entries * 6`
  - `dy_idx = dx_idx + 1`
  - `dz_idx = dx_idx + 2`

#### Call `import_shapekeys_wwmi(...)` from `create_mesh_object(...)`

Invoke it after normal `import_shapekeys(...)` when `wwmi_shapekey_buffers` is not `None`.

### 4. `ui/wwmi/extracted_object.py`

#### Add `ExtractedObjectHelper.build_from_submesh_metadata_list(metadata_list)`

Port the upstream builder with local naming preserved.

Required behavior:

- build `ExtractedObject` from the list of `SubmeshMetadata`
- collect per-component:
  - `vertex_offset`
  - `vertex_count`
  - `index_offset`
  - `index_count`
  - `vg_offset`
  - `vg_count`
  - `vg_map`

- build `ExtractedObjectShapeKeys` from `ShapeKeysInfo`
- fill:
  - `vb0_hash`
  - `cb4_hash`
  - total `vertex_count`
  - total `index_count`

Implementation note:

- store `vg_map` in the format most compatible with the patched `import_vertex_groups()`
- no file I/O should happen in this builder

### 5. `ui/wwmi/drawib_model_wwmi.py`

#### Update imports

Add:

- `from types import SimpleNamespace`
- `from ...common.texture_metadata_helper import TextureMetadataResolver`

#### Replace metadata loading in `__post_init__`

Current local behavior:

- load one `Metadata.json` from `primary_submesh_metadata.extract_gametype_folder_path`

Target behavior:

- keep `primary_submesh_metadata` and `d3d11GameType`
- replace `read_metadata(...)` with:
  - collect ordered metadata list from `unique_str_metadata_dict`
  - `self.extracted_object = ExtractedObjectHelper.build_from_submesh_metadata_list(ordered_metadata)`

#### Add synthetic submesh metadata structures in `__post_init__`

Add fields:

- `self.submesh_model_list`
- `self.match_first_index_partname_dict`
- `self.submesh_texturemarkinfolist_dict`
- `self.partname_texturemarkinfolist_dict`

Populate them from `self.ordered_drawcall_model_list`:

- one synthetic `SimpleNamespace` per unique submesh
- include:
  - `unique_str`
  - `match_first_index` as `int`
  - `d3d11_game_type = self.d3d11GameType`

Then load texture markup via `TextureMetadataResolver`:

- `load_submesh_texture_markup_info_from_all_submeshes(draw_ib_model=self)`
- `load_texture_markup_info_from_all_submeshes(draw_ib_model=self)`

#### Add methods / properties

Add:

- `get_part_name_by_match_first_index(self, match_first_index)`
- `part_name_submesh_dict` property
- `get_submesh_part_name(self, submesh_model)`
- `get_submesh_texture_markup_info_list(self, submesh_model)`

#### Patch merged VG pruning in `build_merged_object(...)`

Current local code:

- uses `sum(extracted_component.vg_count for extracted_component in extracted_object.components)`

Patch to:

- `max((extracted_component.vg_count for extracted_component in extracted_object.components), default=0)`

Reason:

- upstream explicitly corrected this
- likely avoids pruning valid groups incorrectly in merged mode

### 6. `ui/wwmi/wwmi_export.py`

#### Update `add_commandlist_trigger_shared_cleanup_section(...)`

This is the most important direct export bugfix.

Replace old skeleton binding lines with the upstream `ref` binding logic.

Required exact behavior:

- when using merged VG mode and remap is enabled:
  - use `vs-cb4 = ref ResourceMergedSkeleton`
  - use `vs-cb3 = ref ResourceExtraMergedSkeleton`
  - in override branch use:
    - `vs-cb4 = ref ResourceMergedSkeletonOverride`
    - `vs-cb3 = ref ResourceExtraMergedSkeletonOverride`
  - preserve the conditional structure introduced upstream so `vs-cb3` can fall back when `vs-cb4` is not the marker slot

- when remap is disabled:
  - also use `ref` for skeleton resources

#### Fix missing `endif`

Local file currently still has the malformed branch structure from before `a36c04`.

Patch the missing `endif` in the override branch.

#### Do not import upstream `component_count = 0` loop logic

Keep local `_get_component_index_from_name()` strategy.

Local strategy is better because it is prefix-aware and survives current naming conventions.

### 7. `common/m_ini_helper.py`

#### Patch `_get_part_extract_gametype_folder_path(...)`

Add fallback:

- if `submesh_model.d3d11_game_type` is `None`
- fallback to `draw_ib_model.d3d11_game_type` or `draw_ib_model.d3d11GameType`

#### Patch `_get_slot_texture_source_path(...)`

Same fallback rule while iterating synthetic `submesh_model_list`.

#### Do not import upstream trace logging

Keep local logging style.
Only port behavior, not the debug spam.

#### Leave alias-based output naming out of scope

Do not port `_get_aliased_texture_output_filename()` in this migration phase.

### 8. `common/workspace_helper.py`

#### Patch `get_hash_deduped_texture_info_dict(...)`

Current local logic still assumes:

- `original_hash = deduped_filename.split("_")[0]`
- `render_hash = deduped_filename.split("_")[1].split("-")[0]`

Replace with the safer upstream variant:

- split once into `filename_parts`
- guard missing parts

This is a direct low-risk robustness fix.

### 9. `utils/obj_utils.py`

#### Patch `assert_collection(col)`

Change:

- `elif col not in bpy.data.collections.values():`

To:

- `elif col.name not in bpy.data.collections:`

This is a direct low-risk robustness fix from upstream `74f1354`.

### 10. `common/obj_buffer_helper.py`

#### Patch fatal raising style

Replace the direct `raise SSMTErrorUtils.Fatal(...)` site in attribute validation with:

- `SSMTErrorUtils.raise_fatal(...)`

This is a small consistency fix only.

## Integration Order

### Phase 1: Low-risk direct fixes

1. `utils/obj_utils.py`
2. `common/workspace_helper.py`
3. `common/obj_buffer_helper.py`
4. `ui/wwmi/wwmi_export.py` skeleton `ref` fix and `endif`

### Phase 2: Import-chain foundation

1. `common/submesh_json.py`
2. `common/ssmt_import_helper.py`
3. `common/mesh_create_helper.py`

### Phase 3: WWMI extracted object and metadata bridge

1. `ui/wwmi/extracted_object.py`
2. `ui/wwmi/drawib_model_wwmi.py`

### Phase 4: Texture export bridge

1. `common/m_ini_helper.py`
2. re-check `ui/wwmi/drawib_model_wwmi.py`

### Phase 5: Regression audit

Re-check all operators:

- `ssmt4.import_all_from_workspace`
- `ssmt4.import_raw`
- `ssmt.generate_mod_blueprint`
- `ssmt.generate_selected_blueprint_mod`

## Explicit Non-Goals For This Migration

- Do not add upstream `ui/universal/ntemi.py`
- Do not route local standard export through upstream `ExportNTEMI`
- Do not convert `import_merged_vgmap` to enum in this phase
- Do not import upstream alias-driven export naming changes
- Do not replace local direct-export / ModImp chain

## Validation Checklist

### Import validation

- Importing classic non-sliced SSMT JSON still works.
- Importing sliced WWMI / NTEMI JSON with `VertexOffset` / `VertexCount` works.
- `BLENDINDICES` / `BLENDWEIGHTS` import correctly from `BlendWeight`.
- `TANGENT` / `NORMAL` import correctly from `TangentFrame`.
- WWMI shapekeys appear after import when extracted buffers exist.
- Merged VG import does not fail when `VGMap` is partial.

### Export validation

- WWMI export still writes buffers and ini successfully.
- Generated WWMI INI uses `ref` for merged skeleton bindings.
- Generated WWMI INI has balanced `if/endif` structure in the shared override command list.
- Slot textures can still be copied from extracted folders.
- Hash texture generation still works when deduped filename structure is shorter than expected.

### Regression validation

- Local NTMI direct export path remains unchanged.
- Local NTMI ModImp path remains unchanged.
- Existing direct export, multifile, and shapekey postprocess chains still resolve runtime blueprint tree and buffer folder correctly.

## Delegation Notes

### Implementation worker scope

The implementation worker should only edit:

- `common/submesh_json.py`
- `common/ssmt_import_helper.py`
- `common/mesh_create_helper.py`
- `ui/wwmi/extracted_object.py`
- `ui/wwmi/drawib_model_wwmi.py`
- `ui/wwmi/wwmi_export.py`
- `common/m_ini_helper.py`
- `common/workspace_helper.py`
- `utils/obj_utils.py`
- `common/obj_buffer_helper.py`

The implementation worker must not:

- add `ui/universal/ntemi.py`
- modify `blueprint/direct_export.py`
- modify `blueprint/ntmi_export_modimp.py`
- modify `blueprint/ntmi_multifile.py`
- modify `blueprint/ntmi_shapekey.py`
- change `import_merged_vgmap` property type

### Review worker scope

The review worker should verify:

- no regression in local NTMI / ModImp architecture
- no accidental routing changes for `NTEMI`
- all operator impact is limited to existing operators
- no file drift outside the approved edit set
- no malformed generated WWMI command list logic

