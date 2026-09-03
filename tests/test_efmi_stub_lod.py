"""ExportEFMI 占位插桩多 LOD 适配单测（fake bpy 环境，不依赖 Blender/游戏）。

实测定案（2026-08）：LOD0 / LOD1 相互独立——每个 LOD 目录有自己的
DrawIB-Component.json，缺席部件按 LOD 各自插桩；「被引用」判定只查**同 LOD**
现存对象的顶点组（跨 LOD 组 id 各自从 0 起，混查会因编号碰撞误判吸收）；
stub 对象名必须带 LOD 前缀（"LOD1.xxx"），否则会错误解析到 LOD0 的 json。
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

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "efmi_stub_lod_test_pkg"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


for package_name in (
    PKG,
    f"{PKG}.ui",
    f"{PKG}.ui.universal",
    f"{PKG}.common",
    f"{PKG}.utils",
    f"{PKG}.blueprint",
):
    package = _install_module(package_name)
    package.__path__ = []


# ---------------- fake bpy ----------------

class _FakeMesh:
    def __init__(self, name):
        self.name = name
        self.users = 0
        self.from_pydata_calls = []
        self.vertices = []

    def from_pydata(self, verts, edges, faces):
        self.from_pydata_calls.append((verts, edges, faces))

    def update(self):
        pass


class _FakeVertices(list):
    def foreach_get(self, attribute, target):
        if attribute != "co":
            raise ValueError(attribute)
        for index, vertex in enumerate(self):
            target[index * 3:index * 3 + 3] = vertex.co


class _FakeVertexGroup:
    def __init__(self, name):
        self.name = name
        self.add_calls = []

    def add(self, indices, weight, mode):
        self.add_calls.append((list(indices), weight, mode))


class _FakeVertexGroups(list):
    def new(self, name):
        vg = _FakeVertexGroup(name)
        self.append(vg)
        return vg


class _FakeObject:
    def __init__(self, name, object_data=None):
        self.name = name
        self.data = object_data
        self.vertex_groups = _FakeVertexGroups()
        self.props = {}

    def __setitem__(self, key, value):
        self.props[key] = value

    def get(self, key, default=None):
        return self.props.get(key, default)


class _FakeObjectRegistry:
    def __init__(self):
        self._items = {}

    def new(self, name, object_data=None):
        obj = _FakeObject(name, object_data)
        self._items[name] = obj
        return obj

    def get(self, name):
        return self._items.get(name)

    def remove(self, obj, do_unlink=False):
        self._items.pop(obj.name, None)

    def __iter__(self):
        return iter(list(self._items.values()))


class _FakeMeshRegistry:
    def __init__(self):
        self._items = {}

    def new(self, name):
        mesh = _FakeMesh(name)
        self._items[name] = mesh
        return mesh

    def remove(self, mesh):
        self._items.pop(mesh.name, None)


_fake_bpy_data = types.SimpleNamespace(objects=_FakeObjectRegistry(), meshes=_FakeMeshRegistry())
_install_module(
    "bpy",
    data=_fake_bpy_data,
    context=types.SimpleNamespace(
        collection=types.SimpleNamespace(objects=types.SimpleNamespace(link=lambda _obj: None)),
        scene=types.SimpleNamespace(collection=types.SimpleNamespace(objects=types.SimpleNamespace(link=lambda _obj: None))),
    ),
)


# ---------------- fake 配置 / 业务 stub ----------------

_fake_global_properties = types.SimpleNamespace(import_merged_vgmap=lambda: True)
_fake_global_config = types.SimpleNamespace(
    path_workspace_folder=lambda: "",
    path_generatemod_buffer_folder=lambda: "",
    path_generate_mod_folder=lambda: "",
    get_workspace_name=lambda: "",
)

_install_module(f"{PKG}.utils.ssmt_error_utils", SSMTErrorUtils=types.SimpleNamespace())
_install_module(
    f"{PKG}.utils.timer_utils",
    TimerUtils=types.SimpleNamespace(start_stage=lambda *_a, **_k: None, end_stage=lambda *_a, **_k: None),
)
_install_module(f"{PKG}.common.global_config", GlobalConfig=_fake_global_config)
_install_module(f"{PKG}.common.global_properties", GlobalProterties=_fake_global_properties)
_install_module(
    f"{PKG}.common.global_key_count_helper",
    GlobalKeyCountHelper=types.SimpleNamespace(generated_mod_number=0),
)
_install_module(f"{PKG}.common.m_ini_helper", M_IniHelper=types.SimpleNamespace())
_install_module(f"{PKG}.common.m_ini_helper_gui", M_IniHelperGUI=types.SimpleNamespace())
_install_module(
    f"{PKG}.common.m_ini_builder",
    M_IniBuilder=types.SimpleNamespace(),
    M_IniSection=types.SimpleNamespace(),
    M_SectionType=types.SimpleNamespace(MergedSkeleton="MergedSkeleton"),
)
_install_module(f"{PKG}.common.buffer_export_helper", BufferExportHelper=types.SimpleNamespace())
_install_module(f"{PKG}.common.submesh_model", SubMeshModel=types.SimpleNamespace)
_install_module(f"{PKG}.common.drawib_model", DrawIBModel=types.SimpleNamespace)
_install_module(f"{PKG}.blueprint.model", BluePrintModel=types.SimpleNamespace)
_install_module(f"{PKG}.blueprint.export_helper", BlueprintExportHelper=types.SimpleNamespace())


class _FakeExportHelper:
    """ordered 为空时导出流程不实例化 SubMeshModel，这里只兜住两个入口。"""

    @staticmethod
    def parse_submesh_model_list_from_blueprint_model(blueprint_model):
        return []

    @staticmethod
    def parse_drawib_model_list_from_submesh_model_list(submesh_model_list, combine_ib):
        return []


_install_module(f"{PKG}.ui.universal.export_helper", ExportHelper=_FakeExportHelper)


def _load_real_module(qualname, relpath):
    path = REPO_ROOT / relpath
    spec = importlib.util.spec_from_file_location(qualname, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


_load_real_module(f"{PKG}.utils.json_utils", "utils/json_utils.py")
_load_real_module(f"{PKG}.common.m_key", "common/m_key.py")
_load_real_module(f"{PKG}.common.object_prefix_helper", "common/object_prefix_helper.py")
_load_real_module(f"{PKG}.common.draw_call_model", "common/draw_call_model.py")
_load_real_module(f"{PKG}.ui.universal.efmi", "ui/universal/efmi.py")

ExportEFMI = sys.modules[f"{PKG}.ui.universal.efmi"].ExportEFMI
DrawCallModel = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel


def _make_exporter(ordered_drawcalls):
    blueprint_model = types.SimpleNamespace(
        ordered_draw_obj_data_model_list=ordered_drawcalls,
        cross_ib_info_dict={},
        cross_ib_method_dict={},
        has_cross_ib=False,
        cross_ib_mapping_objects={},
        cross_ib_vb_condition_mapping={},
        cross_ib_source_to_target_dict={},
        cross_ib_object_vb_condition={},
        cross_ib_target_info={},
        cross_ib_object_names=set(),
        cross_ib_match_mode="",
        keyname_mkey_dict={},
        multi_file_export_nodes=[],
        shader_replace_info_list=[],
        shader_replace_object_names=set(),
        shader_replace_object_info_map={},
        has_shader_replace=False,
    )
    return ExportEFMI(blueprint_model)


class EFMIStubLodTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="efmi_stub_ws_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        _fake_global_config.path_workspace_folder = lambda: self.tmp
        self.addCleanup(lambda: setattr(_fake_global_config, "path_workspace_folder", lambda: ""))
        _fake_bpy_data.objects._items.clear()
        _fake_bpy_data.meshes._items.clear()

    def _write_component_map(self, lod, component_map):
        lod_dir = os.path.join(self.tmp, lod)
        os.makedirs(lod_dir, exist_ok=True)
        with open(os.path.join(lod_dir, "DrawIB-Component.json"), "w", encoding="utf-8") as f:
            json.dump(component_map, f)

    def _write_vgmap_json(self, lod, bare, gid, positions=None, excluded=False):
        type_dir = os.path.join(self.tmp, lod, bare, "TYPE_GPU_TEST_")
        os.makedirs(type_dir, exist_ok=True)
        payload = {
            "VGMap": {"0": str(gid)} if gid is not None else {},
            "VGOffset": 0,
            "VGCount": 1 if gid is not None else 0,
        }
        if excluded:
            payload["VGMapDedupExcluded"] = True
        if positions is not None:
            payload["CategoryBufferList"] = [{
                "D3D11ElementList": [{
                    "Category": "Position",
                    "ByteWidth": 12,
                }],
            }]
        with open(os.path.join(type_dir, bare + ".json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)
        if positions is not None:
            with open(os.path.join(type_dir, bare + "-Position.buf"), "wb") as f:
                for position in positions:
                    f.write(struct.pack("<3f", *position))

    def _register_object_with_groups(self, name, used_gids, positions=None):
        mesh = _fake_bpy_data.meshes.new(name=name + "_mesh")
        if positions is None:
            positions = [(0.0, 0.0, 0.0)] * len(used_gids)
        obj = _fake_bpy_data.objects.new(name=name, object_data=mesh)
        group_indices = []
        for gid in used_gids:
            group_indices.append(len(obj.vertex_groups))
            obj.vertex_groups.new(name=str(gid))
        mesh.vertices = _FakeVertices(
            types.SimpleNamespace(
                co=position,
                groups=[types.SimpleNamespace(group=group_index, weight=1.0)],
            )
            for group_index, position in zip(group_indices, positions)
        )
        return obj

    def _workspace_unique_strs(self, ordered):
        return [str(dc.get_workspace_unique_str()) for dc in ordered]

    # ---------------- 用例 ----------------

    def test_stub_created_for_missing_lod1_component(self):
        """LOD1 部分缺失：缺失组件补占位，stub 对象名必须带 LOD1 前缀。"""
        self._write_component_map("LOD1", {
            "ed6d1655": {"0": "ed6d1655-816-0", "1": "ed6d1655-999-0"},
        })
        ordered = [DrawCallModel(obj_name="LOD1.ed6d1655-816-0")]
        exporter = _make_exporter(ordered)

        names = self._workspace_unique_strs(ordered)
        self.assertIn("LOD1.ed6d1655-999-0", names)
        self.assertEqual(len(exporter._efmi_stub_object_names), 1)

        stub = _fake_bpy_data.objects.get("LOD1.ed6d1655-999-0")
        self.assertIsNotNone(stub)
        self.assertEqual(stub.get("EFMI_STUB"), 1)
        self.assertEqual(stub.get("3DMigoto:WorkspaceUniqueStr"), "LOD1.ed6d1655-999-0")
        self.assertEqual(stub.vertex_groups[0].name, "0")
        # 极限小三角面
        verts, _edges, faces = stub.data.from_pydata_calls[0]
        self.assertEqual(len(verts), 3)
        self.assertEqual(faces, [(0, 1, 2)])
        self.assertLess(max(abs(c) for v in verts for c in v), 1e-3)

        exporter._cleanup_stub_objects()
        self.assertIsNone(_fake_bpy_data.objects.get("LOD1.ed6d1655-999-0"))
        self.assertEqual(exporter._efmi_stub_object_names, [])

    def test_stub_weight_group_uses_registered_slot_when_vgmap_present(self):
        """合并骨架模式：缺部件有 VGMap 时，占位权重组必须是已注册槽（首个 VGMap 值），
        否则 EFMI 双套域前置会以「未注册/旧代槽」拦截导出。"""
        self._write_component_map("LOD0", {
            "31f9ac3d": {"0": "31f9ac3d-500-0", "1": "31f9ac3d-501-0"},
        })
        self._write_vgmap_json("LOD0", "31f9ac3d-501-0", 371)
        ordered = [DrawCallModel(obj_name="LOD0.31f9ac3d-500-0")]
        exporter = _make_exporter(ordered)

        self._workspace_unique_strs(ordered)
        stub = _fake_bpy_data.objects.get("LOD0.31f9ac3d-501-0")
        self.assertIsNotNone(stub)
        self.assertEqual(stub.get("EFMI_STUB"), 1)
        # 权重组挂已注册槽 371，而不是局部命名空间的 "0"
        self.assertEqual(stub.vertex_groups[0].name, "371")
        exporter._cleanup_stub_objects()

    def test_absent_drawib_registered_slot_referenced_is_not_absorbed(self):
        """LOD1 整个 DrawIB 缺席，其独有槽被现存对象实际使用——但该槽已在
        全工作区 json 注册（缺部件自己的 json 声明了 7）：统一顶点组模式下
        跨组件引用已注册槽是设计内合法状态（用户裁决 2026-11，基线改为
        全工作区注册域），不判吸收 -> 不插桩（游戏保留原版）。"""
        self._write_component_map("LOD1", {"26ab840d": {"0": "26ab840d-24570-0"}})
        self._write_vgmap_json("LOD1", "26ab840d-24570-0", 7)
        self._write_vgmap_json("LOD1", "ed6d1655-816-0", 3)  # 现存对象声明槽 3
        self._register_object_with_groups("LOD1.ed6d1655-816-0", [7])  # 实际却用了 7（已注册）
        ordered = [DrawCallModel(obj_name="LOD1.ed6d1655-816-0")]
        exporter = _make_exporter(ordered)

        names = self._workspace_unique_strs(ordered)
        self.assertNotIn("LOD1.26ab840d-24570-0", names)
        self.assertEqual(exporter._efmi_stub_object_names, [])
        exporter._cleanup_stub_objects()

    def test_absent_drawib_every_reference_is_registered_never_absorbed(self):
        """整 DrawIB 缺席：declared = 全工作区注册域，缺部件自身的 json VGMap
        值必然 ⊆ 注册域 -> vg_values ∩ (used − declared) 恒为空 -> absorbed
        恒 False（用户裁决：几何未并入时槽位引用不算吸收证据；占位只由
        「部分缺失」分支承担）。未注册槽（如 999）不属于任何 json 值域，
        同样无法命中 vg_values，故正常数据下整缺席分支不再插桩。"""
        self._write_component_map("LOD0", {"b20f90ea": {"0": "b20f90ea-19182-0"}})
        self._write_vgmap_json("LOD0", "b20f90ea-19182-0", 7)
        self._register_object_with_groups(
            "LOD0.84618ee0-22296-0",
            [7, 999],  # 7 已注册（缺部件声明）；999 未注册（也不属任何值域）
        )
        ordered = [DrawCallModel(obj_name="LOD0.84618ee0-22296-0")]
        exporter = _make_exporter(ordered)

        names = self._workspace_unique_strs(ordered)
        self.assertNotIn("LOD0.b20f90ea-19182-0", names)
        self.assertEqual(exporter._efmi_stub_object_names, [])
        exporter._cleanup_stub_objects()

    def test_shared_slot_from_matrix_dedup_does_not_absorb_missing_drawib(self):
        """用户裁决：矩阵去重的共享槽（A 自己经 VGMap 声明了同槽）不算吸收证据——
        A 用 X 是用自己的骨骼，B 缺席时不应补占位（游戏保留原版）。"""
        self._write_component_map("LOD1", {"26ab840d": {"0": "26ab840d-24570-0"}})
        self._write_vgmap_json("LOD1", "26ab840d-24570-0", 7)  # B: local0 -> 槽 7
        self._write_vgmap_json("LOD1", "ed6d1655-816-0", 7)    # A: local0 -> 亦声明槽 7（共享）
        self._register_object_with_groups("LOD1.ed6d1655-816-0", [7])
        ordered = [DrawCallModel(obj_name="LOD1.ed6d1655-816-0")]
        exporter = _make_exporter(ordered)

        names = self._workspace_unique_strs(ordered)
        self.assertNotIn("LOD1.26ab840d-24570-0", names)
        self.assertEqual(exporter._efmi_stub_object_names, [])
        exporter._cleanup_stub_objects()

    def test_dedup_excluded_missing_component_skips_stub(self):
        """VGMapDedupExcluded=True 的缺失部件：即使槽被引用，也按用户意图不生成占位。"""
        self._write_component_map("LOD1", {"26ab840d": {"0": "26ab840d-24570-0"}})
        self._write_vgmap_json("LOD1", "26ab840d-24570-0", 7, excluded=True)
        self._register_object_with_groups("LOD1.ed6d1655-816-0", [7])
        ordered = [DrawCallModel(obj_name="LOD1.ed6d1655-816-0")]
        exporter = _make_exporter(ordered)

        names = self._workspace_unique_strs(ordered)
        self.assertNotIn("LOD1.26ab840d-24570-0", names)
        self.assertEqual(exporter._efmi_stub_object_names, [])
        exporter._cleanup_stub_objects()

    def test_replacement_model_registered_slot_reference_is_not_absorbed(self):
        """替换模型即使几何不同且组下标被压缩，也按数字组名识别统一骨骼；
        但被引用的槽 7 已由缺席部件自己的 json 注册（全工作区注册域）——
        跨组件引用已注册槽是合法状态（用户裁决 2026-11）-> 不插桩，
        游戏保留原版绘制。"""
        self._write_component_map("LOD0", {
            "b20f90ea": {"0": "b20f90ea-19182-0"},
        })
        self._write_vgmap_json(
            "LOD0",
            "b20f90ea-19182-0",
            7,
            positions=[(0.0, 0.0, 0.0)],
        )
        self._register_object_with_groups(
            "LOD0.84618ee0-22296-0",
            [7],
            positions=[(10.0, 10.0, 10.0)],
        )
        ordered = [DrawCallModel(obj_name="LOD0.84618ee0-22296-0")]
        exporter = _make_exporter(ordered)

        names = self._workspace_unique_strs(ordered)
        self.assertNotIn("LOD0.b20f90ea-19182-0", names)
        self.assertEqual(exporter._efmi_stub_object_names, [])
        exporter._cleanup_stub_objects()

    def test_geometry_overlap_without_vgmap_relation_does_not_create_stub(self):
        """只有几何重合不能证明 Component 被合并；占位判定必须只认统一顶点组。"""
        self._write_component_map("LOD0", {
            "b20f90ea": {"0": "b20f90ea-19182-0"},
        })
        self._write_vgmap_json(
            "LOD0",
            "b20f90ea-19182-0",
            None,
            positions=[(0.0, 0.0, 0.0)],
        )
        self._register_object_with_groups(
            "LOD0.84618ee0-22296-0",
            [7],
            positions=[(0.0, 0.0, 0.0)],
        )
        ordered = [DrawCallModel(obj_name="LOD0.84618ee0-22296-0")]
        exporter = _make_exporter(ordered)

        names = self._workspace_unique_strs(ordered)
        self.assertNotIn("LOD0.b20f90ea-19182-0", names)
        self.assertEqual(exporter._efmi_stub_object_names, [])

    def test_new_stub_group_zero_is_not_absorption_evidence(self):
        """本轮刚创建的占位组 0 不能误触发后续缺席 Component。"""
        self._write_component_map("LOD0", {
            "aaaa1111": {
                "0": "aaaa1111-300-0",
                "1": "aaaa1111-300-300",
            },
            "bbbb2222": {"0": "bbbb2222-600-0"},
        })
        self._write_vgmap_json("LOD0", "bbbb2222-600-0", 0)
        self._register_object_with_groups("LOD0.aaaa1111-300-0", [7])
        ordered = [DrawCallModel(obj_name="LOD0.aaaa1111-300-0")]
        exporter = _make_exporter(ordered)

        names = self._workspace_unique_strs(ordered)
        self.assertIn("LOD0.aaaa1111-300-300", names)
        self.assertNotIn("LOD0.bbbb2222-600-0", names)
        self.assertEqual(len(exporter._efmi_stub_object_names), 1)
        exporter._cleanup_stub_objects()

    def test_no_stub_when_absent_lod1_drawib_not_referenced(self):
        """LOD1 整个 DrawIB 缺席且零引用 = 用户故意不生成，不插桩。"""
        self._write_component_map("LOD1", {"26ab840d": {"0": "26ab840d-24570-0"}})
        self._write_vgmap_json("LOD1", "26ab840d-24570-0", 250)
        self._register_object_with_groups("LOD1.ed6d1655-816-0", [7])
        ordered = [DrawCallModel(obj_name="LOD1.ed6d1655-816-0")]
        exporter = _make_exporter(ordered)

        names = self._workspace_unique_strs(ordered)
        self.assertNotIn("LOD1.26ab840d-24570-0", names)
        self.assertEqual(exporter._efmi_stub_object_names, [])
        self.assertEqual(len(names), 1)

    def test_no_cross_lod_group_id_false_absorb(self):
        """关键回归：LOD0 对象引用组 7，LOD1 缺席 DrawIB 的 VGMap 也含 7 ——
        跨 LOD 组 id 各自从 0 起（命名空间独立），绝不能因 LOD0 的引用误判
        LOD1 部件被吸收（旧实现 used_group_ids 全局混查会误插桩）。"""
        self._write_component_map("LOD1", {"26ab840d": {"0": "26ab840d-24570-0"}})
        self._write_vgmap_json("LOD1", "26ab840d-24570-0", 7)
        self._register_object_with_groups("LOD0.84618ee0-22296-0", [7])
        ordered = [DrawCallModel(obj_name="LOD0.84618ee0-22296-0")]
        exporter = _make_exporter(ordered)

        names = self._workspace_unique_strs(ordered)
        self.assertNotIn("LOD1.26ab840d-24570-0", names)
        self.assertEqual(exporter._efmi_stub_object_names, [])

    def test_lod0_registered_slot_referenced_is_not_absorbed(self):
        """LOD0 新语义不回退：LOD0 缺席 DrawIB 的已注册槽被 LOD0 对象引用
        （槽 3 由缺部件 json 注册在全工作区）-> 跨组件引用已注册槽合法，
        不判吸收 -> 不插桩（用户裁决 2026-11，基线=全工作区注册域）。"""
        self._write_component_map("LOD0", {"b20f90ea": {"0": "b20f90ea-19182-0"}})
        self._write_vgmap_json("LOD0", "b20f90ea-19182-0", 3)
        self._register_object_with_groups("LOD0.84618ee0-22296-0", [3])
        ordered = [DrawCallModel(obj_name="LOD0.84618ee0-22296-0")]
        exporter = _make_exporter(ordered)

        names = self._workspace_unique_strs(ordered)
        self.assertNotIn("LOD0.b20f90ea-19182-0", names)
        self.assertEqual(exporter._efmi_stub_object_names, [])
        exporter._cleanup_stub_objects()


if __name__ == "__main__":
    unittest.main()
