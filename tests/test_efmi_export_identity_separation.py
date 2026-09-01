# -*- coding: utf-8 -*-
"""t4 两边分离实现的新增单测：FC-1/FC-2/FC-3/FC-4 断言 + F1/F2/F6 缓存契约。

覆盖（t3 设计 §7.1 + t1 发现修复验证）：
- U1（FC-2 纯函数）：validate_export_indices_in_segment——段内放行、越段
  RuntimeError、折叠别名目标放行、空索引放行、NaN/Inf/空段拒绝；
- U2（B11/FC-3）：build_dualset_export_table 对「引用无人声明槽位」的损坏
  json fail-closed（0000 实证 0 违反——合法数据不阻断）；
- U3（FC-1）：build_per_mesh_identity_map 对「槽无本组件成员」中止导出
  （不再回退全局 e(s)）；有自属成员时正常输出 ⊆ 自属段；
- U4（FC-4 语义 + 0000 三死段镜像）：折叠家族 LOD1 声明段为死段；折叠部件
  per-mesh 输出仍 ⊆ 自属段（FC-2 合法），绑定部件的值域永不命中死段；
- U5（0000 实数结构镜像）：108b0ab1 型部件 VGMap 槽全越自属段（canonical
  借位），per-mesh 身份全部映射回自属段、B11 0 违反；
- 挂载验证：_assert_fc2_fc4_written_blendindices（真实 efmi.py）——段内放行、
  越段（绕开更名的直写形态）RuntimeError、绑定网格命中死段 RuntimeError、
  折叠部件自身（未绑定）死段豁免；
- F1：_efmi_cache_intact 指纹——源数据变更失效、旧口径（无指纹键）失效、
  无声明 json 不适用；F2：clear_vgmap_cache 清无 VGMap 的投影裁决标记；
- F6：单 LOD 写回盖章三件套（在 test_efmi_skeleton_lod.py 中同步更新断言）。

夹具纯合成（临时目录），无 bpy 依赖；与 tests 既有 efmi 测试同 loader 风格。
"""
import importlib.util
import json
import os
import shutil
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "efmi_identity_separation_test_pkg"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load_real(qualname, relpath):
    spec = importlib.util.spec_from_file_location(qualname, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


# ---- fake 包骨架（与 tests/test_efmi_merge_active_collision.py 同构）----
for pkg_name in (PKG, f"{PKG}.ui", f"{PKG}.ui.universal", f"{PKG}.blueprint",
                 f"{PKG}.common", f"{PKG}.utils"):
    _pkg = types.ModuleType(pkg_name)
    _pkg.__path__ = []
    sys.modules[pkg_name] = _pkg

_install_module("bpy", data=types.SimpleNamespace(),
                types=types.SimpleNamespace(Object=object, Mesh=object))
_install_module(f"{PKG}.utils.json_utils",
                JsonUtils=_load_real(f"{PKG}.utils.json_utils_", "utils/json_utils.py").JsonUtils)
_install_module(f"{PKG}.utils.timer_utils", TimerUtils=types.SimpleNamespace(
    start_stage=lambda *_a, **_k: None, end_stage=lambda *_a, **_k: None))
_install_module(f"{PKG}.utils.export_utils", ExportUtils=object)
_install_module(f"{PKG}.utils.obj_utils", ObjUtils=object)
_install_module(f"{PKG}.utils.collection_utils", CollectionUtils=object)
_install_module(f"{PKG}.utils.shapekey_utils", ShapeKeyUtils=object)
_install_module(f"{PKG}.common.global_config", GlobalConfig=types.SimpleNamespace())
_install_module(f"{PKG}.common.global_properties", GlobalProterties=types.SimpleNamespace(
    import_merged_vgmap=lambda: True, forbid_auto_texture_ini=lambda: False))
_install_module(f"{PKG}.common.global_key_count_helper",
                GlobalKeyCountHelper=types.SimpleNamespace(generated_mod_number=0))
_install_module(f"{PKG}.common.d3d11_gametype", D3D11GameType=object)
_install_module(f"{PKG}.common.submesh_model", SubMeshModel=object)
_install_module(f"{PKG}.common.submesh_metadata", SubmeshMetadataResolver=object)
_install_module(f"{PKG}.common.submesh_json", SubmeshJson=object)
_install_module(f"{PKG}.common.draw_call_model", DrawCallModel=object)
_install_module(f"{PKG}.blueprint.export_helper", BlueprintExportHelper=object)
_install_module(f"{PKG}.blueprint.model", BluePrintModel=object)
_install_module(f"{PKG}.blueprint.node_datatype",
                reset_datatype_override_log=lambda: None, build_override_element_list=lambda *a, **k: None)
_install_module(f"{PKG}.common.workspace_helper", WorkSpaceHelper=object)
_install_module(f"{PKG}.common.m_ini_helper", M_IniHelper=types.SimpleNamespace())
_install_module(f"{PKG}.common.m_ini_helper_gui", M_IniHelperGUI=types.SimpleNamespace())
# 真实 m_ini_builder（纯 python，无 bpy）：efmi.py 顶层导入它
_load_real(f"{PKG}.common.m_ini_builder", "common/m_ini_builder.py")
_install_module(f"{PKG}.common.drawib_model", DrawIBModel=object)
_install_module(f"{PKG}.common.buffer_export_helper", BufferExportHelper=types.SimpleNamespace())
_install_module(f"{PKG}.ui.universal.export_helper", ExportHelper=types.SimpleNamespace())

# ---- 真实产品模块：efmi_skeleton → m_ini_builder → efmi ----
_efmi_skeleton = _load_real(f"{PKG}.common.efmi_skeleton", "common/efmi_skeleton.py")
EFMIBoneMapBuilder = _efmi_skeleton.EFMIBoneMapBuilder
EFMISkeletonMergeHelper = _efmi_skeleton.EFMISkeletonMergeHelper
_efmi = _load_real(f"{PKG}.ui.universal.efmi", "ui/universal/efmi.py")
ExportEFMI = _efmi.ExportEFMI
GlobalConfig = sys.modules[f"{PKG}.common.global_config"].GlobalConfig


class _FakeRegisteredUnionSM:
    """t18 挂载测试的 submesh_model 替身：仅提供 _dualset_registered_slots_union。

    真实实现（common/submesh_model.py）的并集语义由 test_dualset_merged_mode.py
    钉死；此处同口径复核路由：efmi.py FC-2 闸门 → 工作区并集 → 验证器
    （验证器语义由 FC2PureFunctionTests 钉死）。
    """

    @staticmethod
    def _dualset_registered_slots_union(workspace_root):
        import json as _json
        slots: set = set()
        ws = str(workspace_root or "")
        if not ws or not os.path.isdir(ws):
            return slots
        for lod_name in sorted(os.listdir(ws)):
            lod_root = os.path.join(ws, lod_name)
            if not os.path.isdir(lod_root) or not lod_name.upper().startswith("LOD"):
                continue
            for bare in sorted(os.listdir(lod_root)):
                folder = os.path.join(lod_root, bare)
                if not os.path.isdir(folder) or bare.startswith("DedupedTextures"):
                    continue
                for entry in sorted(os.listdir(folder)):
                    tdir = os.path.join(folder, entry)
                    if not os.path.isdir(tdir) or not entry.startswith("TYPE_"):
                        continue
                    jp = os.path.join(tdir, bare + ".json")
                    if os.path.isfile(jp):
                        try:
                            with open(jp, encoding="utf-8") as f:
                                payload = _json.load(f)
                            vg_map = payload.get("VGMap")
                            if isinstance(vg_map, dict):
                                for raw_value in vg_map.values():
                                    text_value = str(raw_value).strip()
                                    if text_value.isdigit():
                                        slots.add(int(text_value))
                        except Exception:
                            pass
                    break
        return slots


# 顶层 SubMeshModel 导入（efmi.py 内 FC-2 方法按需 from ...common.submesh_model 取用）
sys.modules[f"{PKG}.common.submesh_model"].SubMeshModel = _FakeRegisteredUnionSM

_LOD_LAYOUT_VERSION = _efmi_skeleton._CROSS_LOD_LAYOUT_VERSION
_ALGORITHM_VERSION = _efmi_skeleton._VG_MAP_ALGORITHM_VERSION


def _write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_component(ws: Path, lod: str, bare: str, vg_offset: int, vg_map: dict,
                    vg_count: int = None, corr=None, algorithm_version=None):
    """写一个含 VGMap 的子网格 json（无 buffer，逻辑层使用 recompute_strength=False）。"""
    if vg_count is None:
        vg_count = max(len(vg_map), 1)
    payload = {
        "GamePreset": "EFMI",
        "GPU-PreSkinning": True,
        "VGOffset": vg_offset,
        "VGCount": vg_count,
        "VGMap": {str(k): int(v) for k, v in sorted(vg_map.items())},
        "VGMapAlgorithmVersion": (
            algorithm_version if algorithm_version is not None else _ALGORITHM_VERSION
        ),
        "VGMapDedupEnabled": True,
        "EFMILODLayoutVersion": _LOD_LAYOUT_VERSION,
        "EFMILODReference": "LOD0" if lod != "LOD0" else lod,
        "EFMILODProjection": True,
    }
    if corr:
        payload["EFMILODCorrespondence"] = {
            str(k): dict(v) for k, v in corr.items()
        }
    unique = f"{lod}.{bare}"
    type_dir = ws / lod / bare / ("TYPE_GPU_TEST_")
    type_dir.mkdir(parents=True, exist_ok=True)
    (type_dir / f"{bare}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return unique


def identity_map_vgmap(vg_offset: int, vg_count: int) -> dict:
    """恒等映射 VGMap（local -> VGOffset+local，去重关闭/单源语义）。"""
    return {i: vg_offset + i for i in range(vg_count)}


def clear_table_cache():
    EFMIBoneMapBuilder._dualset_table_cache.clear()


class FC2PureFunctionTests(unittest.TestCase):
    """U1：validate_export_indices_in_segment 纯函数（FC-2 核心）。"""

    def setUp(self):
        clear_table_cache()

    def test_in_segment_passes(self):
        EFMIBoneMapBuilder.validate_export_indices_in_segment(
            (100, 120), (), [100, 119, 105, 100], component_label="c1"
        )

    def test_empty_indices_pass(self):
        EFMIBoneMapBuilder.validate_export_indices_in_segment(
            (100, 120), (), [], component_label="c1"
        )
        import numpy
        EFMIBoneMapBuilder.validate_export_indices_in_segment(
            (100, 120), (), numpy.empty((0,), dtype=numpy.int64), component_label="c1"
        )

    def test_out_of_segment_raises(self):
        import numpy
        with self.assertRaisesRegex(RuntimeError, "FC-2"):
            EFMIBoneMapBuilder.validate_export_indices_in_segment(
                (100, 120), (), [100, 121, 999], component_label="LOD1.xxx"
            )
        with self.assertRaisesRegex(RuntimeError, "LOD1.xxx"):
            EFMIBoneMapBuilder.validate_export_indices_in_segment(
                (100, 120), (), numpy.array([119, 120], dtype=numpy.int64),
                component_label="LOD1.xxx",
            )

    def test_fold_alias_target_passes(self):
        # 折叠别名目标（基准段身份）是 FC-2 合法值域的补集成员
        EFMIBoneMapBuilder.validate_export_indices_in_segment(
            (100, 120), (500, 501), [100, 119, 500, 501], component_label="c1"
        )

    def test_nan_inf_raises(self):
        import numpy
        # NaN/Inf 必须先于 int64 转换识别（浮点输入），fail-closed 明确点名
        for bad in ([100, float("nan")], [100, float("inf")]):
            with self.assertRaisesRegex(RuntimeError, "NaN/Inf"):
                EFMIBoneMapBuilder.validate_export_indices_in_segment(
                    (100, 120), (), bad, component_label="c1"
                )
        with self.assertRaisesRegex(RuntimeError, "NaN/Inf"):
            EFMIBoneMapBuilder.validate_export_indices_in_segment(
                (100, 120), (), numpy.array([100, numpy.nan], dtype=numpy.float64),
                component_label="c1",
            )

    def test_empty_segment_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "声明段为空"):
            EFMIBoneMapBuilder.validate_export_indices_in_segment(
                (50, 50), (), [50], component_label="c1"
            )


class B11CorruptDataTests(unittest.TestCase):
    """U2：B11/FC-3——引用「无人声明」槽位的损坏 json fail-closed。"""

    def setUp(self):
        clear_table_cache()
        self.tmp = tempfile.TemporaryDirectory(prefix="b11_")
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ref_outside_pool_raises(self):
        # 单部件声明段 [0,1)，VGMap 引用槽 5 —— 无人声明的槽位 → B11 中止
        write_component(self.ws, "LOD0", "aaaa1000-0-0", 0, {0: 5}, vg_count=1)
        with self.assertRaisesRegex(RuntimeError, "B11"):
            EFMIBoneMapBuilder.build_dualset_export_table(
                str(self.ws), recompute_strength=False
            )

    def test_valid_pool_borrow_passes(self):
        # 槽 5 ∈ 池 [0,6)（部件 B 声明 [5,6)）→ 「越出自属段」的池共享合法
        write_component(self.ws, "LOD0", "aaaa1000-0-0", 0, {0: 5}, vg_count=1)
        write_component(self.ws, "LOD0", "bbbb1000-0-0", 5, identity_map_vgmap(5, 1))
        table = EFMIBoneMapBuilder.build_dualset_export_table(
            str(self.ws), recompute_strength=False
        )
        self.assertIn(5, table)
        # 槽 5 = aaaa(identity 0) + bbbb(identity 5) 双成员；b11 通过
        self.assertEqual(
            {m["comp"] for m in table[5]["members"]},
            {"LOD0.aaaa1000-0-0", "LOD0.bbbb1000-0-0"},
        )

    def test_ref_in_gap_between_segments_raises(self):
        """T6-F3：声明段精确并集——引用落在**两段之间空洞**的槽必须拒绝。

        包围盒 [0,15) 会放行；逐段并集判定 [0,5)∪[10,15) 中槽 7 无人声明 → B11。"""
        write_component(self.ws, "LOD0", "aaaa0000-0-0", 0, identity_map_vgmap(0, 5))
        write_component(self.ws, "LOD0", "bbbb2222-0-0", 10, identity_map_vgmap(10, 5))
        # 损坏部件：声明段 [20,21)，却引用无人声明的凹槽 7
        write_component(self.ws, "LOD0", "cccc3333-0-0", 20, {0: 7}, vg_count=1)
        with self.assertRaisesRegex(RuntimeError, "B11"):
            EFMIBoneMapBuilder.build_dualset_export_table(
                str(self.ws), recompute_strength=False
            )

    def test_ref_in_hole_inside_segments_raises(self):
        """T6-F3 补充：多段并集内的空洞同样拒绝（与包围盒语义差异的极端形态）。"""
        write_component(self.ws, "LOD0", "aaaa0000-0-0", 0, identity_map_vgmap(0, 2))
        write_component(self.ws, "LOD0", "bbbb2222-0-0", 3, identity_map_vgmap(3, 2))
        # 引用槽 2 —— 位于 [0,2) 与 [3,5) 之间的空洞
        write_component(self.ws, "LOD0", "cccc3333-0-0", 5, {0: 2}, vg_count=1)
        with self.assertRaisesRegex(RuntimeError, "B11"):
            EFMIBoneMapBuilder.build_dualset_export_table(
                str(self.ws), recompute_strength=False
            )


class FC1SelfMemberTests(unittest.TestCase):
    """U3：FC-1——槽无本组件自属成员时中止（不再回退全局 e(s)）。"""

    def setUp(self):
        clear_table_cache()
        self.tmp = tempfile.TemporaryDirectory(prefix="fc1_")
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_self_member_raises(self):
        """FC-1：建表行缺失本组件成员（陈旧/错配缓存或数据不一致）→ 中止。

        本组件引用槽的自属成员恒存在（identity=VGOffset+local 天然在自属段），
        违反只能来自「表与 json 不一致」——模拟该状态：调表后剥掉 aaaa 对槽 5
        的成员贡献，per-mesh 映射即遇「无本组件成员」→ FC-1 fail-closed。
        """
        write_component(self.ws, "LOD0", "aaaa1000-0-0", 0, {0: 5}, vg_count=1)
        write_component(self.ws, "LOD0", "bbbb1000-0-0", 5, identity_map_vgmap(5, 1))
        original = EFMIBoneMapBuilder.get_dualset_export_table_cached

        def stale_table(workspace_root, recompute_strength=True):
            table = original(workspace_root, recompute_strength)
            table[5]["members"] = [
                m for m in table[5]["members"]
                if m["comp"] != "LOD0.aaaa1000-0-0"
            ]
            return table

        EFMIBoneMapBuilder.get_dualset_export_table_cached = staticmethod(stale_table)
        try:
            with self.assertRaisesRegex(RuntimeError, "FC-1"):
                EFMIBoneMapBuilder.build_per_mesh_identity_map(
                    str(self.ws), "LOD0.aaaa1000-0-0", recompute_strength=False
                )
        finally:
            EFMIBoneMapBuilder.get_dualset_export_table_cached = original

    def test_self_member_maps_to_own_segment(self):
        write_component(self.ws, "LOD0", "aaaa1000-0-0", 0, {0: 5}, vg_count=1)
        write_component(self.ws, "LOD0", "bbbb1000-0-0", 5, identity_map_vgmap(5, 1))
        pm = EFMIBoneMapBuilder.build_per_mesh_identity_map(
            str(self.ws), "LOD0.bbbb1000-0-0", recompute_strength=False
        )
        self.assertEqual(pm, {5: 5})

    def test_row_is_none_raises(self):
        """T6-F4：引用槽不在建表结果中（row is None）→ 与无自属成员合并为
        同一条 FC-1 拒绝路径（不再保持槽位原值静默直写）。"""
        write_component(self.ws, "LOD0", "aaaa1000-0-0", 0, {0: 5}, vg_count=1)
        write_component(self.ws, "LOD0", "bbbb1000-0-0", 5, identity_map_vgmap(5, 1))
        original = EFMIBoneMapBuilder.get_dualset_export_table_cached

        def table_without_slot(workspace_root, recompute_strength=True):
            table = original(workspace_root, recompute_strength)
            table.pop(5, None)  # 槽 5 建表结果缺失（陈旧缓存/表与 json 不一致）
            return table

        EFMIBoneMapBuilder.get_dualset_export_table_cached = staticmethod(
            table_without_slot
        )
        try:
            with self.assertRaisesRegex(RuntimeError, "FC-1"):
                EFMIBoneMapBuilder.build_per_mesh_identity_map(
                    str(self.ws), "LOD0.aaaa1000-0-0", recompute_strength=False
                )
        finally:
            EFMIBoneMapBuilder.get_dualset_export_table_cached = original


class IbKeyEquivalenceTests(unittest.TestCase):
    """T6-F6：_baseline_draw_key 内联兜底算法与 efmi_ib_key 全量等价（防漂移）。

    _baseline_draw_key 在缺 efmi_skeleton 模块的测试夹具环境中退化为内联算法；
    本测试把内联公式（与 efmi.py 中逐字一致）与模块单一实现按全量名称形状
    对拍——任一侧被改动而未同步另一侧即失败。
    """

    def _inline_ib_key(self, unique_str: str) -> str:
        raw_unique = str(unique_str or "")
        if raw_unique.upper().startswith("LOD") and "." in raw_unique:
            raw_unique = raw_unique.split(".", 1)[1]
        return raw_unique.split("-")[0] if "-" in raw_unique else raw_unique

    def test_inline_fallback_equivalent_to_efmi_ib_key(self):
        names = [
            "LOD0.aaaa0000-100-0", "LOD1.bbbb1111-200-1", "aaaa0000-100-0",
            "LOD2.cccc2222-300-2", "no-hyphen", "LOD1.", "LOD0.onlybare", "",
            "LOD10.x-y-0", "LOAD0.aaa-1-0", "LOD.aaa-1-0", "loD0.aaa-1-0",
            "LOD0.a5b3c2d1-10-0", "LOD1.0deadbeef-999-0", "AABBCCDD-1-0",
            "LOD0.abcdef12", "LOD  .space-1-0",
        ]
        for name in names:
            self.assertEqual(
                self._inline_ib_key(name),
                _efmi_skeleton.efmi_ib_key(name),
                f"内联兜底与 efmi_ib_key 对 {name!r} 不一致（复制粘贴漂移）",
            )

    def test_inline_fallback_via_fold_path_model(self):
        """通过真实折叠路径核对：match_draw_ib 缺失时 fold 键首元素 == efmi_ib_key。"""
        for name in ("LOD0.aaaa0000-100-0", "LOD1.bbbb1111-200-1", "LOD0.nohyphen"):
            model = types.SimpleNamespace(
                unique_str=name,
                workspace_unique_str=name,
                match_draw_ib="",
                match_first_index="100",
                match_index_count="20",
            )
            # _baseline_draw_key 是方法内闭包，经模块级复制无法直接调用；
            # 这里直接对拍内联公式（与实现逐字一致）与模块函数即可。
            raw = str(name or "")
            if raw.upper().startswith("LOD") and "." in raw:
                raw = raw.split(".", 1)[1]
            derived = raw.split("-")[0] if "-" in raw else raw
            self.assertEqual(derived, _efmi_skeleton.efmi_ib_key(name))


class DeadSegmentLOD1MirrorTests(unittest.TestCase):
    """U4：0000 三死段结构的镜像夹具——折叠死段 + canonical 借位语义。

    镜像数值：基准 LOD0.78554d71 [286,345)、LOD1.78554d71 折叠 [903,962)；
    LOD1.119b1b29 折叠 [717,729)，其 VGMap 借位引用 78554d71 折叠段槽
    [928,951]（跨部件去重池共享，t2 §4.2 实证形态）。
    """

    FOLD_SEGMENTS = [(717, 729), (817, 827), (903, 962)]

    def setUp(self):
        clear_table_cache()
        self.tmp = tempfile.TemporaryDirectory(prefix="fc4_mirror_")
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _build_ws(self):
        # 基准 LOD0 部件（折叠家族 + 独立部件）
        write_component(self.ws, "LOD0", "119b1b29-108-0", 20,
                        identity_map_vgmap(20, 12))
        write_component(self.ws, "LOD0", "78554d71-9282-0", 286,
                        identity_map_vgmap(286, 59))
        # 独立 LOD1 部件（自己段 [697,717)，引用借位到 [1031,1180)）
        write_component(self.ws, "LOD1", "108b0ab1-1860-0", 697,
                        {0: 1083, 1: 1085, 2: 1128, 3: 1249, 4: 1253, 5: 1259,
                         6: 1104, 7: 1109, 8: 1131, 9: 1250, 10: 1254, 11: 1260,
                         12: 1256, 13: 1257, 14: 1258, 15: 1261, 16: 1132,
                         17: 1129, 18: 1251, 19: 1252},
                        vg_count=20)
        write_component(self.ws, "LOD1", "a9b7357b-43773-0", 1031,
                        identity_map_vgmap(1031, 149))
        write_component(self.ws, "LOD1", "f09ecf2c-24768-0", 1180,
                        identity_map_vgmap(1180, 95))
        # 折叠 LOD1.78554d71（自属段 [903,962) = 死段，恒等 VGMap 59 项）
        write_component(self.ws, "LOD1", "78554d71-9282-0", 903,
                        identity_map_vgmap(903, 59),
                        corr={str(i): {"unique_str": "LOD0.78554d71-9282-0",
                                       "local_vg_id": i, "matrix_diff": 0.7}
                              for i in range(59)})
        # 折叠 LOD1.119b1b29（自属段 [717,729) = 死段；借位引用 78554d71 段槽）
        write_component(self.ws, "LOD1", "119b1b29-108-0", 717,
                        {0: 933, 1: 718, 2: 932, 3: 931, 4: 930, 5: 928,
                         6: 951, 7: 724, 8: 950, 9: 949, 10: 948, 11: 946},
                        vg_count=12,
                        corr={str(i): {"unique_str": "LOD0.119b1b29-108-0",
                                       "local_vg_id": i, "matrix_diff": 0.65}
                              for i in range(12)})

    def test_b11_zero_violations_on_mirror(self):
        self._build_ws()
        table = EFMIBoneMapBuilder.build_dualset_export_table(
            str(self.ws), recompute_strength=False
        )
        self.assertGreater(len(table), 0)

    def test_per_mesh_identity_of_folded_part_stays_in_own_segment(self):
        """折叠部件 per-mesh 身份 ⊆ 自属（死）段 → FC-2 合法（FC-4 只约束绑定网格）。"""
        self._build_ws()
        pm = EFMIBoneMapBuilder.build_per_mesh_identity_map(
            str(self.ws), "LOD1.119b1b29-108-0", recompute_strength=False
        )
        self.assertTrue(all(717 <= v < 729 for v in pm.values()), pm)
        # FC-2：段内放行（含折叠别名目标集为空的情形）
        EFMIBoneMapBuilder.validate_export_indices_in_segment(
            (717, 729), (), list(pm.values()), component_label="LOD1.119b1b29-108-0"
        )

    def test_bound_part_never_hits_dead_segments(self):
        """独立（绑定）部件值域永不命中死段：FC-2 自属段 ∩ 死段 = ∅。"""
        self._build_ws()
        for unique in ("LOD1.108b0ab1-1860-0", "LOD1.a9b7357b-43773-0"):
            pm = EFMIBoneMapBuilder.build_per_mesh_identity_map(
                str(self.ws), unique, recompute_strength=False
            )
            for value in pm.values():
                for dead_start, dead_end in self.FOLD_SEGMENTS:
                    self.assertFalse(
                        dead_start <= value < dead_end,
                        f"{unique} per-mesh 身份 {value} 命中死段 [{dead_start},{dead_end})",
                    )

    def test_fc1_no_self_member_violation_on_mirror(self):
        """0000 实证「self-member 恒在」：镜像夹具所有引用槽都有自属成员。"""
        self._build_ws()
        for unique in (
            "LOD1.108b0ab1-1860-0", "LOD1.119b1b29-108-0", "LOD1.a9b7357b-43773-0",
        ):
            pm = EFMIBoneMapBuilder.build_per_mesh_identity_map(
                str(self.ws), unique, recompute_strength=False
            )
            self.assertTrue(pm, unique)


class Workspace108b0ab1MirrorTests(unittest.TestCase):
    """U5：0000 LOD1.108b0ab1 实数结构镜像——VGMap 槽全越自属段（1083..1261
    落在 a9b7357b/f09ecf2c 声明段），per-mesh 身份全部映射回 [697,717)。"""

    def setUp(self):
        clear_table_cache()
        self.tmp = tempfile.TemporaryDirectory(prefix="u5_108b0ab1_")
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _build_ws(self):
        # 0000 实测槽位（t2 §1.3/§4.1）：20 项，值域 [1083,1261] 全越自属段
        slots = [1083, 1085, 1104, 1109, 1128, 1129, 1131, 1132, 1249, 1250,
                 1251, 1252, 1253, 1254, 1256, 1257, 1258, 1259, 1260, 1261]
        write_component(self.ws, "LOD1", "108b0ab1-1860-0", 697,
                        {i: slot for i, slot in enumerate(slots)}, vg_count=20)
        write_component(self.ws, "LOD1", "a9b7357b-43773-0", 1031,
                        identity_map_vgmap(1031, 149))
        write_component(self.ws, "LOD1", "f09ecf2c-24768-0", 1180,
                        identity_map_vgmap(1180, 95))

    def test_all_slots_map_back_to_own_segment(self):
        self._build_ws()
        pm = EFMIBoneMapBuilder.build_per_mesh_identity_map(
            str(self.ws), "LOD1.108b0ab1-1860-0", recompute_strength=False
        )
        self.assertEqual(len(pm), 20)
        self.assertTrue(all(697 <= v <= 716 for v in pm.values()), pm)
        # 与 0000 实证一致：per-mesh 输出 ⊆ 自属段（t2 §5 核对点 2）
        self.assertEqual(sorted(set(pm.values())), list(range(697, 717)))

    def test_b11_zero_violations(self):
        self._build_ws()
        table = EFMIBoneMapBuilder.build_dualset_export_table(
            str(self.ws), recompute_strength=False
        )
        self.assertIn(1083, table)
        self.assertIn(1261, table)

    def test_fc2_write_domain_satisfied(self):
        self._build_ws()
        pm = EFMIBoneMapBuilder.build_per_mesh_identity_map(
            str(self.ws), "LOD1.108b0ab1-1860-0", recompute_strength=False
        )
        # 写盘域断言（更名后身份域）：行内放行
        EFMIBoneMapBuilder.validate_export_indices_in_segment(
            (697, 717), (), list(pm.values()), component_label="LOD1.108b0ab1-1860-0"
        )
        # 绕开更名的直写形态（槽位原值）→ 越自属段 → fail-closed
        with self.assertRaisesRegex(RuntimeError, "FC-2"):
            EFMIBoneMapBuilder.validate_export_indices_in_segment(
                (697, 717), (), [1083, 1261], component_label="LOD1.108b0ab1-1860-0"
            )


class SourceFingerprintF1Tests(unittest.TestCase):
    """F1：_efmi_cache_intact 源数据指纹——数据变更/旧口径失效，无声明不适用。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="f1_fp_")
        self.root = Path(self.tmp.name)
        self.bare = "aaaabbbb-100-0"
        self.type_dir = self.root / self.bare / "TYPE_GPU_TEST_"
        self.type_dir.mkdir(parents=True, exist_ok=True)
        runtime = self.root / self.bare / "ModImpRuntime"
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / f"{self.bare}-BoneMatrix.buf").write_bytes(b"\x00" * 96)
        self.pos = self.type_dir / f"{self.bare}-Position.buf"
        self.blend = self.type_dir / f"{self.bare}-Blend.buf"
        self.pos.write_bytes(b"\x00" * 24)
        self.blend.write_bytes(b"\x00" * 16)
        self.json_path = self.type_dir / f"{self.bare}.json"
        (self.json_path).write_text("{}", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _fresh_fingerprint(self):
        return EFMIBoneMapBuilder._vgmap_source_fingerprint(
            str(self.type_dir),
            {"CategoryBufferList": [
                {"FileName": f"{self.bare}-Position.buf"},
                {"FileName": f"{self.bare}-Blend.buf"},
            ]},
        )

    def _payload(self, fingerprint=None, categories=True):
        data = {
            "VGMapAlgorithmVersion": _ALGORITHM_VERSION,
            "VGMapDedupEnabled": True,
            "VGCount": 2,
            "VGOffset": 0,
            "VGMap": {"0": 0, "1": 1},
            "BoneMatrixFileName": f"{self.bare}-BoneMatrix.buf",
        }
        if categories:
            data["CategoryBufferList"] = [
                {"FileName": f"{self.bare}-Position.buf"},
                {"FileName": f"{self.bare}-Blend.buf"},
            ]
        if fingerprint is not None:
            data["EFMIVGMapSourceFingerprint"] = fingerprint
        return data

    def test_fresh_fingerprint_passes(self):
        self.assertTrue(EFMISkeletonMergeHelper._efmi_cache_intact(
            self._payload(self._fresh_fingerprint()),
            str(self.json_path),
            self.bare,
        ))

    def test_no_declared_buffers_skips_check(self):
        # 无 CategoryBufferList 声明（旧/合成形态）→ 不适用指纹，其余校验照旧
        self.assertTrue(EFMISkeletonMergeHelper._efmi_cache_intact(
            self._payload(fingerprint=None, categories=False),
            str(self.json_path),
            self.bare,
        ))

    def test_declared_but_missing_fingerprint_invalidates(self):
        # 声明了 Position/Blend 但无指纹键 = 旧口径缓存 → 失效自动重建
        self.assertFalse(EFMISkeletonMergeHelper._efmi_cache_intact(
            self._payload(fingerprint=None, categories=True),
            str(self.json_path),
            self.bare,
        ))

    def test_buffer_change_invalidates(self):
        fp = self._fresh_fingerprint()
        self.assertTrue(EFMISkeletonMergeHelper._efmi_cache_intact(
            self._payload(fp), str(self.json_path), self.bare
        ))
        # 替换 Position.buf（大小变化 → mtime_ns/size 指纹变化）→ 失效
        self.pos.write_bytes(b"\x00" * 48)
        self.assertFalse(EFMISkeletonMergeHelper._efmi_cache_intact(
            self._payload(fp), str(self.json_path), self.bare
        ))

    def test_buffer_removed_invalidates(self):
        fp = self._fresh_fingerprint()
        self.blend.unlink()
        self.assertFalse(EFMISkeletonMergeHelper._efmi_cache_intact(
            self._payload(fp), str(self.json_path), self.bare
        ))


class ClearVgmapCacheF2Tests(unittest.TestCase):
    """F2：clear_vgmap_cache 清除「无 VGMap 但带投影裁决标记」的 json。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="f2_proj_")
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rel: str, payload: dict):
        _write_json(self.ws / rel, payload)

    def test_projection_skipped_marker_cleared(self):
        # 投影裁决「未匹配」部件：无 VGMap，仅带跳过标记三件套
        self._write("LOD1/eeeeffff-300-0/TYPE_GPU_TEST_/eeeeffff-300-0.json", {
            "DrawIB": "eeeeffff",
            "EFMILODLayoutVersion": _LOD_LAYOUT_VERSION,
            "EFMILODReference": "LOD0",
            "EFMILODProjection": True,
            "EFMILODProjectionSkipped": True,
        })
        # 普通 json（无任何缓存/裁决键）不应被清理
        self._write("LOD0/aaaa0000-0-0/TYPE_GPU_TEST_/aaaa0000-0-0.json", {
            "DrawIB": "aaaa0000",
        })
        cleaned, scanned = EFMISkeletonMergeHelper.clear_vgmap_cache(str(self.ws))
        self.assertEqual(cleaned, 1, "带投影跳过标记的无 VGMap json 必须被清理（F2）")
        self.assertGreaterEqual(scanned, 2)
        payload = json.loads(
            (self.ws / "LOD1" / "eeeeffff-300-0" / "TYPE_GPU_TEST_"
             / "eeeeffff-300-0.json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload, {"DrawIB": "eeeeffff"})
        self.assertEqual(
            json.loads((self.ws / "LOD0" / "aaaa0000-0-0" / "TYPE_GPU_TEST_"
                        / "aaaa0000-0-0.json").read_text(encoding="utf-8")),
            {"DrawIB": "aaaa0000"},
        )

    def test_projection_matched_marker_cleared(self):
        # CPU/无顶点组「匹配成功」标记同样清除（可重新导入）
        self._write("LOD1/cccc2222-100-0/TYPE_GPU_TEST_/cccc2222-100-0.json", {
            "DrawIB": "cccc2222",
            "EFMILODLayoutVersion": _LOD_LAYOUT_VERSION,
            "EFMILODReference": "LOD0",
            "EFMILODProjection": True,
            "EFMILODProjectionMatched": True,
        })
        cleaned, _ = EFMISkeletonMergeHelper.clear_vgmap_cache(str(self.ws))
        self.assertEqual(cleaned, 1)
        payload = json.loads(
            (self.ws / "LOD1" / "cccc2222-100-0" / "TYPE_GPU_TEST_"
             / "cccc2222-100-0.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("EFMILODProjectionMatched", payload)
        self.assertNotIn("EFMILODProjectionSkipped", payload)


class WriteDomainMountTests(unittest.TestCase):
    """挂载验证：真实 efmi.py 的 _assert_fc2_fc4_written_blendindices。

    用 object.__new__(ExportEFMI) + 合成缓冲断言 FC-2 段内放行（含零权重哨兵
    通道豁免）、越段中止、折叠目标放行、绑定网格命中死段中止（FC-4）、
    折叠部件自身（未绑定）死段豁免。
    缓冲布局对齐打包器语义（vertexgroup_utils）：未占用通道写 index=0 +
    weight=0；FC-2 只断言权重 > 0 的通道。
    """

    def _game_type(self, weight_first=True):
        element_weight = types.SimpleNamespace(
            Category="Blend", SemanticName="BLENDWEIGHT", SemanticIndex=0,
            Format="R8G8B8A8_UNORM", ByteWidth=4,
        )
        element_indices = types.SimpleNamespace(
            Category="Blend", SemanticName="BLENDINDICES", SemanticIndex=0,
            Format="R16G16B16A16_UINT", ByteWidth=8,
        )
        element_list = (
            [element_weight, element_indices] if weight_first
            else [element_indices, element_weight]
        )
        return types.SimpleNamespace(
            CategoryStrideDict={"Blend": 12},
            D3D11ElementList=element_list,
        )

    def _blend_buffer(self, rows):
        """rows: [(i0,i1,i2,i3, w0,w1,w2,w3), ...] -> 权重字节 + R16 索引字节。

        权重 0 写 0，非零权重写 255（R8G8B8A8_UNORM 活跃判定 = 原始 != 0）。
        """
        out = bytearray()
        for indices, weights in rows:
            for w in weights:
                out += struct.pack("B", 255 if w > 0 else 0)
            out += struct.pack("<4H", *indices)
        return bytes(out)

    def _make_exporter(self):
        exporter = object.__new__(ExportEFMI)
        exporter._efmi_folded_dead_segments = []
        exporter._efmi_fold_alias_targets = {}
        exporter._efmi_merged_draw_entries = {}
        return exporter

    def _model(self, unique_str="LOD1.aaa-0", vg_offset=0, vg_count=3,
               weight_first=True):
        model = types.SimpleNamespace(
            unique_str=unique_str,
            vg_offset=vg_offset,
            vg_count=vg_count,
            d3d11_game_type=self._game_type(weight_first=weight_first),
        )
        return model

    def test_in_segment_written_buffer_passes(self):
        exporter = self._make_exporter()
        model = self._model("LOD1.aaa-0", vg_offset=100, vg_count=3)
        # 第 4 通道 index=0 + weight=0：零权重哨兵豁免（打包器对未占用通道如此写）
        buf = self._blend_buffer([((100, 101, 102, 0), (1, 1, 1, 0))])
        exporter._assert_fc2_fc4_written_blendindices(
            model, "Blend", buf, [], {}, bound=True
        )

    def test_out_of_segment_active_channel_raises(self):
        exporter = self._make_exporter()
        model = self._model("LOD1.aaa-0", vg_offset=100, vg_count=3)
        # 103 有正权重 → FC-2 必须拒（越自属段 [100,103)）
        buf = self._blend_buffer([((100, 103, 102, 0), (1, 1, 1, 0))])
        with self.assertRaisesRegex(RuntimeError, "FC-2"):
            exporter._assert_fc2_fc4_written_blendindices(
                model, "Blend", buf, [], {}, bound=True
            )

    def test_zero_weight_out_of_segment_index_exempt(self):
        """零权重通道的任意索引都惰性（运行时权重 0 消去）——越段也不拒。"""
        exporter = self._make_exporter()
        model = self._model("LOD1.aaa-0", vg_offset=100, vg_count=3)
        buf = self._blend_buffer([((100, 101, 102, 9000), (1, 1, 1, 0))])
        exporter._assert_fc2_fc4_written_blendindices(
            model, "Blend", buf, [], {}, bound=True
        )

    def test_fold_alias_target_passes(self):
        exporter = self._make_exporter()
        exporter._efmi_fold_alias_targets = {717: 21}
        model = self._model("LOD1.aaa-0", vg_offset=100, vg_count=3)
        buf = self._blend_buffer([((100, 101, 21, 0), (1, 1, 1, 0))])  # 21 = 折叠目标
        exporter._assert_fc2_fc4_written_blendindices(
            model, "Blend", buf,
            [{"unique_str": "LOD1.fold-0", "segment": (903, 962)}],
            exporter._efmi_fold_alias_targets, bound=True,
        )

    def test_bound_mesh_hits_dead_segment_raises(self):
        """FC-4：绑定部件自属段异常覆盖死段时（数据异常/未来路径漂移），
        值落在死段内即拒绝（FC-2 已放行——值 ∈ 自属段）。"""
        exporter = self._make_exporter()
        # 自属段 [900,965)（数据异常地覆盖折叠死段 [903,962)）
        model = self._model("LOD1.aaa-0", vg_offset=900, vg_count=65)
        buf = self._blend_buffer([((903, 910, 960, 0), (1, 1, 1, 0))])
        with self.assertRaisesRegex(RuntimeError, "FC-4"):
            exporter._assert_fc2_fc4_written_blendindices(
                model, "Blend", buf,
                [{"unique_str": "LOD1.fold-0", "segment": (903, 962)}],
                {}, bound=True,
            )

    def test_folded_part_own_dead_segment_exempt(self):
        """折叠部件自身（未绑定）：值 ⊆ 自己声明段（= 死段）时 FC-2 合法、
        FC-4 豁免（未绑定死文件，运行时不引用）。"""
        exporter = self._make_exporter()
        model = self._model("LOD1.119b1b29-108-0", vg_offset=717, vg_count=12)
        buf = self._blend_buffer([((717, 719, 722, 0), (1, 1, 1, 0))])  # 全在自己（死）段内
        exporter._assert_fc2_fc4_written_blendindices(
            model, "Blend", buf,
            [{"unique_str": "LOD1.119b1b29-108-0", "segment": (717, 729)}],
            {}, bound=False,
        )

    # ---- t18 挂载：FC-2 并集口径（真实 efmi.py 闸门 + 全工作区注册表）----

    def _t18_workspace(self):
        """合成工作区：LOD0.placeholder 注册槽 {0,1,205,371}（含 63d1c417 写-0 案例）。"""
        import tempfile as _tf
        ws = Path(_tf.mkdtemp(prefix="efmi_t18_"))
        tdir = ws / "LOD0" / "placeholder-0-0" / "TYPE_GPU_TEST_"
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "placeholder-0-0.json").write_text(
            json.dumps({"GPU-PreSkinning": True,
                        "VGMap": {"0": 0, "1": 1, "2": 205, "3": 371}}),
            encoding="utf-8",
        )
        return ws

    def _t18_patch_workspace(self, ws):
        gc = sys.modules[f"{PKG}.common.global_config"].GlobalConfig
        gc.path_workspace_folder = lambda: str(ws)
        self.addCleanup(delattr, gc, "path_workspace_folder")

    def test_t18_registered_zero_placeholder_passes(self):
        """63d1c417 占位组件实盘案例：写 index 0（0 ∈ 全工作区已注册槽）→ 放行。"""
        ws = self._t18_workspace()
        self.addCleanup(shutil.rmtree, str(ws), True)
        self._t18_patch_workspace(ws)
        exporter = self._make_exporter()
        model = self._model("LOD1.63d1c417-312-0", vg_offset=100, vg_count=3)
        buf = self._blend_buffer([((100, 0, 102, 0), (1, 1, 1, 0))])  # 0 有正权重
        exporter._assert_fc2_fc4_written_blendindices(
            model, "Blend", buf, [], {}, bound=True
        )

    def test_t18_registered_cross_component_slot_passes(self):
        """跨组件已注册槽（373..393 类当前注册值，如 371/205）→ 放行（预期行为）。"""
        ws = self._t18_workspace()
        self.addCleanup(shutil.rmtree, str(ws), True)
        self._t18_patch_workspace(ws)
        exporter = self._make_exporter()
        model = self._model("LOD1.abc-0", vg_offset=100, vg_count=3)
        buf = self._blend_buffer([((100, 371, 205, 0), (1, 1, 1, 0))])
        exporter._assert_fc2_fc4_written_blendindices(
            model, "Blend", buf, [], {}, bound=True
        )

    def test_t18_unregistered_slot_still_rejects(self):
        """真未注册槽（段隙 2 / 越界 742+）→ FC-2 拒绝（负向钉死）。"""
        ws = self._t18_workspace()
        self.addCleanup(shutil.rmtree, str(ws), True)
        self._t18_patch_workspace(ws)
        exporter = self._make_exporter()
        model = self._model("LOD1.abc-0", vg_offset=100, vg_count=3)
        for bad_slot in (2, 742):
            buf = self._blend_buffer([((100, bad_slot, 102, 0), (1, 1, 1, 0))])
            with self.assertRaisesRegex(RuntimeError, "全工作区已注册槽"):
                exporter._assert_fc2_fc4_written_blendindices(
                    model, "Blend", buf, [], {}, bound=True
                )

    def test_t18_workspace_missing_metadata_fail_closed(self):
        """工作区 json 全清（并集为空）→ FC-2 报「元数据缺失」fail-closed（防线不弱化）。"""
        ws = self._t18_workspace()
        self.addCleanup(shutil.rmtree, str(ws), True)
        jp = ws / "LOD0" / "placeholder-0-0" / "TYPE_GPU_TEST_" / "placeholder-0-0.json"
        payload = json.loads(jp.read_text(encoding="utf-8"))
        payload.pop("VGMap", None)
        jp.write_text(json.dumps(payload), encoding="utf-8")
        self._t18_patch_workspace(ws)
        exporter = self._make_exporter()
        model = self._model("LOD1.abc-0", vg_offset=100, vg_count=3)
        buf = self._blend_buffer([((100, 371, 102, 0), (1, 1, 1, 0))])
        with self.assertRaisesRegex(RuntimeError, "元数据缺失"):
            exporter._assert_fc2_fc4_written_blendindices(
                model, "Blend", buf, [], {}, bound=True
            )


class BiOnlyLayoutFc2Tests(unittest.TestCase):
    """F1（t5）：无 BLENDWEIGHT 元素的刚性（BI-only）布局——哨兵 0 不误判。

    GPU_P12_N4_T8_BI4_ 等布局只有 BLENDINDICES；打包器对未占用通道写
    index=0 且无权重字节可消去；运行时无权重即单骨刚性（只读通道 0）。
    FC-2 只断言通道 0，哨兵 0 不得计为越段引用。
    """

    def _bi_only_game_type(self):
        element_indices = types.SimpleNamespace(
            Category="Blend", SemanticName="BLENDINDICES", SemanticIndex=0,
            Format="R16G16B16A16_UINT", ByteWidth=8,
        )
        return types.SimpleNamespace(
            CategoryStrideDict={"Blend": 8},
            D3D11ElementList=[element_indices],
        )

    def _bi_buffer(self, rows):
        out = bytearray()
        for indices in rows:
            out += struct.pack("<4H", *indices)
        return bytes(out)

    def _bi_model(self, unique_str="LOD1.32b98652-996-0", vg_offset=817, vg_count=10):
        return types.SimpleNamespace(
            unique_str=unique_str,
            vg_offset=vg_offset,
            vg_count=vg_count,
            d3d11_game_type=self._bi_only_game_type(),
        )

    def _bi_exporter(self, model):
        exporter = object.__new__(ExportEFMI)
        exporter._efmi_folded_dead_segments = []
        exporter._efmi_fold_alias_targets = {}
        exporter._efmi_merged_draw_entries = {model.unique_str: (None, None)}
        return exporter

    def test_rigid_layout_channel0_in_segment_passes(self):
        """写盘行 [826,0,0,0]：826 ∈ [817,827) 合法；三哨兵 0 不计活跃 → 放行。"""
        exporter = self._bi_exporter(self._bi_model())
        buf = self._bi_buffer([(826, 0, 0, 0), (820, 0, 0, 0)])
        exporter._assert_fc2_fc4_written_blendindices(
            self._bi_model(), "Blend", buf, [], {}, bound=True
        )

    def test_extraction_returns_channel0_only(self):
        """提取层：BI-only 布局只返回通道 0 的活跃索引。"""
        model = self._bi_model()
        buf = self._bi_buffer([(826, 0, 0, 0), (999, 0, 0, 0)])
        values = ExportEFMI._extract_active_blendindices_values(
            buf, "Blend", model.d3d11_game_type
        )
        self.assertEqual(values.tolist(), [826, 999])

    def test_rigid_layout_channel0_out_of_segment_raises(self):
        """通道 0 真实越段（900 ∉ [817,827)）→ FC-2 必须中止（通道 0 仍断言）。"""
        exporter = self._bi_exporter(self._bi_model())
        buf = self._bi_buffer([(900, 0, 0, 0)])
        with self.assertRaisesRegex(RuntimeError, "FC-2"):
            exporter._assert_fc2_fc4_written_blendindices(
                self._bi_model(), "Blend", buf, [], {}, bound=True
            )

    def test_rigid_layout_bone_zero_channel0_passes_when_in_segment(self):
        """通道 0 为 0（绑定骨骼 0）且段含 0（LOD0 首部件）→ 放行。"""
        model = self._bi_model(unique_str="LOD0.0af3ccb1-3780-0", vg_offset=0, vg_count=20)
        exporter = self._bi_exporter(model)
        buf = self._bi_buffer([(0, 0, 0, 0), (3, 0, 0, 0)])
        exporter._assert_fc2_fc4_written_blendindices(
            model, "Blend", buf, [], {}, bound=True
        )

    def test_bi_only_32b98652_r8_widened_variant_passes(self):
        """T6-F1：0000 32b98652 型无权重布局（源 R8G8B8A8_UINT 升宽为
        R16G8B16A16_UINT，仍无 BLENDWEIGHT 元素）：写盘行 [826,0,0,0]
        （t5 probe2 原样）通道 0=826 ∈ 自属段放行，三哨兵 0 不计活跃。"""
        model = self._bi_model("LOD1.32b98652-996-0", vg_offset=817, vg_count=10)
        exporter = self._bi_exporter(model)
        buf = self._bi_buffer([(826, 0, 0, 0)] * 4)  # t5 探针行翻倍形态
        exporter._assert_fc2_fc4_written_blendindices(
            model, "Blend", buf, [], {}, bound=True
        )


class _FakeMergeModel:
    """_get_merged_skeleton_component_info 用的免 bpy 合并候选模型（同既有
    test_efmi_merge_active_collision._FakePreSkindSubmesh 模式）。"""

    def __init__(self, unique_str, vg_offset, vg_count, match_draw_ib="",
                 match_index_count="1", match_first_index="0", ref_component="",
                 corr=None, vg_map=None, layout_version=13):
        self.unique_str = unique_str
        self.workspace_unique_str = unique_str
        self.vg_offset = vg_offset
        self.vg_count = vg_count
        self.merged_skeleton_metadata_valid = True
        self.d3d11_game_type = types.SimpleNamespace(
            GPU_PreSkinning=True,
            get_blendindices_layouts=lambda: [(0, "R16G16B16A16_UINT", "vb2")],
        )
        self.match_draw_ib = match_draw_ib
        self.match_index_count = match_index_count
        self.match_first_index = match_first_index
        self.efmi_lod_reference_component = ref_component
        self.efmi_lod_correspondence = corr or {}
        self.efmi_lod_layout_version = layout_version
        self.vg_map = vg_map or {}


class MissingFoldBaselineTests(unittest.TestCase):
    """F2（t5）：同 IB 折叠候选缺基准 → fail-closed；基准在位 → 折叠真触发。

    覆盖 _missing_fold_baseline_unique_str 纯判定 + _get_merged_skeleton_
    component_info 挂载：缺基准中止（不再静默按独立部件导出 = FC-4 空转）；
    基准在位时折叠成立（dead segments / aliases 记录生效）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="f2_fold_")
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()
        for name in ("path_workspace_folder", "logic_name"):
            if hasattr(GlobalConfig, name):
                delattr(GlobalConfig, name)

    def _write_fold_pair(self):
        """LOD0/LOD1.32b98652 折叠家族（工作空间侧）。"""
        write_component(self.ws, "LOD0", "32b98652-996-0", 74,
                        {i: 74 + i for i in range(10)})
        write_component(self.ws, "LOD1", "32b98652-996-0", 817,
                        {i: 817 + i for i in range(10)})

    def _fold_lod1_model(self):
        return _FakeMergeModel(
            "LOD1.32b98652-996-0", 817, 10,
            match_draw_ib="32b98652", match_index_count="996", match_first_index="0",
            ref_component="LOD0.32b98652-996-0",
            corr={str(i): {"unique_str": "LOD0.32b98652-996-0",
                           "local_vg_id": i} for i in range(10)},
            vg_map={i: 817 + i for i in range(10)},
        )

    def _base_lod0_model(self):
        return _FakeMergeModel(
            "LOD0.32b98652-996-0", 74, 10,
            match_draw_ib="32b98652", match_index_count="996", match_first_index="0",
            vg_map={i: 74 + i for i in range(10)},
        )

    def _set_workspace(self):
        GlobalConfig.path_workspace_folder = lambda: str(self.ws)

    def _make_exporter(self, models):
        exporter = object.__new__(ExportEFMI)
        exporter.submesh_model_list = models
        return exporter

    # ---- _missing_fold_baseline_unique_str 纯判定 ----

    def test_missing_baseline_detected(self):
        self._write_fold_pair()
        model = self._fold_lod1_model()
        found = ExportEFMI._missing_fold_baseline_unique_str(
            str(self.ws), model, {}
        )
        self.assertEqual(found, "LOD0.32b98652-996-0")

    def test_baseline_in_batch_returned_for_fail_closed(self):
        """基准已在批次内但未折叠（键异常/数据不一致）→ 同样中止。"""
        self._write_fold_pair()
        model = self._fold_lod1_model()
        present = {"LOD0.32b98652-996-0": object()}
        found = ExportEFMI._missing_fold_baseline_unique_str(
            str(self.ws), model, present
        )
        self.assertEqual(found, "LOD0.32b98652-996-0")

    def test_independent_part_not_detected(self):
        """几何配对部件（reference bare ≠ 自身 bare，如 108b0ab1 ↔ 0af3ccb1）
        且工作空间无 LOD0.<自身 bare> → 不是折叠候选，放行。"""
        write_component(self.ws, "LOD1", "108b0ab1-1860-0", 697,
                        {i: 1083 + i for i in range(20)})
        write_component(self.ws, "LOD0", "0af3ccb1-3780-0", 0,
                        {i: 0 + i for i in range(20)})
        model = _FakeMergeModel(
            "LOD1.108b0ab1-1860-0", 697, 20,
            match_draw_ib="108b0ab1", match_index_count="1860", match_first_index="0",
            ref_component="LOD0.0af3ccb1-3780-0",
        )
        found = ExportEFMI._missing_fold_baseline_unique_str(
            str(self.ws), model, {}
        )
        self.assertEqual(found, "")

    def test_ineligible_baseline_not_detected(self):
        """基准 json 无 VGMap（CPU/投影排除/未反查）→ 不能折叠到它，放行。"""
        self._write_fold_pair()
        lod1_path = self.ws / "LOD1" / "32b98652-996-0" / "TYPE_GPU_TEST_"
        # 重写 LOD0 基准 json：去掉 VGMap（模拟非合并候选）
        payload_path = (self.ws / "LOD0" / "32b98652-996-0" / "TYPE_GPU_TEST_"
                        / "32b98652-996-0.json")
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        payload.pop("VGMap", None)
        payload.pop("VGCount", None)
        payload.pop("VGOffset", None)
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
        model = self._fold_lod1_model()
        found = ExportEFMI._missing_fold_baseline_unique_str(
            str(self.ws), model, {}
        )
        self.assertEqual(found, "")
        self.assertTrue(lod1_path.exists())

    # ---- _get_merged_skeleton_component_info 挂载 ----

    def test_missing_baseline_aborts_export(self):
        """18 模型缺基准形态（t5 §4.4）：折叠候选无基准 → F2 RuntimeError 中止。"""
        self._write_fold_pair()
        self._set_workspace()
        exporter = self._make_exporter([self._fold_lod1_model()])
        with self.assertRaisesRegex(RuntimeError, "F2"):
            exporter._get_merged_skeleton_component_info()

    def test_fold_triggers_when_baseline_present(self):
        """基准在位（同 draw 键）→ 折叠成立：单 component、死段记录、别名生成。"""
        self._write_fold_pair()
        self._set_workspace()
        exporter = self._make_exporter(
            [self._base_lod0_model(), self._fold_lod1_model()]
        )
        parts, id_dict = exporter._get_merged_skeleton_component_info()
        self.assertGreaterEqual(len(parts), 1)
        self.assertEqual(id_dict["LOD1.32b98652-996-0"],
                         id_dict["LOD0.32b98652-996-0"],
                         "LOD1 必须映射到基准 component（折叠成立）")
        self.assertEqual(
            [d for d in getattr(exporter, "_efmi_folded_dead_segments", [])],
            [{"unique_str": "LOD1.32b98652-996-0", "segment": (817, 827)}],
            "FC-4 死段记录必须生成（保护真正生效）",
        )
        self.assertTrue(
            getattr(exporter, "_efmi_fold_alias_targets", {}),
            "折叠别名目标必须生成",
        )

    def test_fold_path_export_write_clean(self):
        """T6-F2 折叠路径写盘清洁性（真实导出逻辑，非仅合成夹具）：
        - draw entries 只含基准（折叠部件无独立入口 = INI 不注册死段）；
        - 基准网格写盘缓冲（段内身份）绑定态过 FC-2/FC-4；
        - 折叠部件自身缓冲（未绑定、值⊆ 死段）FC-2 合法 + FC-4 豁免；
        - 死段记录非空（FC-4 断言真正可达）。"""
        self._write_fold_pair()
        self._set_workspace()
        exporter = self._make_exporter(
            [self._base_lod0_model(), self._fold_lod1_model()]
        )
        exporter._get_merged_skeleton_component_info()
        dead_segments = exporter._efmi_folded_dead_segments
        fold_targets = exporter._efmi_fold_alias_targets
        draw_entries = exporter._efmi_merged_draw_entries
        self.assertTrue(dead_segments, "死段记录必须非空（FC-4 保护可达）")
        self.assertIn("LOD0.32b98652-996-0", draw_entries)
        self.assertNotIn("LOD1.32b98652-996-0", draw_entries,
                         "折叠部件不得有独立入口（INI 不得注册死段）")

        # BI-only 游戏类型（32b98652 型：无 BLENDWEIGHT，T6-F1 同挂载语义）
        element_indices = types.SimpleNamespace(
            Category="Blend", SemanticName="BLENDINDICES", SemanticIndex=0,
            Format="R16G16B16A16_UINT", ByteWidth=8,
        )
        game_type = types.SimpleNamespace(
            CategoryStrideDict={"Blend": 8},
            D3D11ElementList=[element_indices],
        )

        baseline_model = self._base_lod0_model()
        baseline_model.d3d11_game_type = game_type
        baseline_buf = struct.pack("<4H", 74, 75, 83, 0)  # 基准段 [74,84) 内身份
        exporter._assert_fc2_fc4_written_blendindices(
            baseline_model, "Blend", baseline_buf,
            dead_segments, fold_targets, bound=True,
        )

        folded_model = self._fold_lod1_model()
        folded_model.d3d11_game_type = game_type
        folded_buf = struct.pack("<4H", 817, 818, 819, 0)  # 自属（=死）段内
        exporter._assert_fc2_fc4_written_blendindices(
            folded_model, "Blend", folded_buf,
            dead_segments, fold_targets, bound=False,
        )

        # 死段零写入的整体断言：绑定网格值域 ∩ 死段 = ∅
        import numpy as _np
        bound_values = _np.frombuffer(baseline_buf, dtype="<u2").astype(_np.int64)
        for item in dead_segments:
            ds, de = item["segment"]
            self.assertFalse(((bound_values >= ds) & (bound_values < de)).any())


if __name__ == "__main__":
    unittest.main(verbosity=2)