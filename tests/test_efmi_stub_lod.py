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

    def _write_vgmap_json(self, lod, bare, gid):
        type_dir = os.path.join(self.tmp, lod, bare, "TYPE_GPU_TEST_")
        os.makedirs(type_dir, exist_ok=True)
        payload = {"VGMap": {"0": str(gid)}, "VGOffset": 0, "VGCount": 1}
        with open(os.path.join(type_dir, bare + ".json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def _register_object_with_groups(self, name, used_gids):
        mesh = _fake_bpy_data.meshes.new(name=name + "_mesh")
        mesh.vertices = [
            types.SimpleNamespace(groups=[types.SimpleNamespace(group=gid, weight=1.0)])
            for gid in used_gids
        ]
        return _fake_bpy_data.objects.new(name=name, object_data=mesh)

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

    def test_stub_when_absent_lod1_drawib_absorbed_by_lod1_object(self):
        """LOD1 整个 DrawIB 缺席，但其 VGMap 全局 id 被 LOD1 现存对象引用 = 被合并 -> 插桩。"""
        self._write_component_map("LOD1", {"26ab840d": {"0": "26ab840d-24570-0"}})
        self._write_vgmap_json("LOD1", "26ab840d-24570-0", 7)
        self._register_object_with_groups("LOD1.ed6d1655-816-0", [7])
        ordered = [DrawCallModel(obj_name="LOD1.ed6d1655-816-0")]
        exporter = _make_exporter(ordered)

        names = self._workspace_unique_strs(ordered)
        self.assertIn("LOD1.26ab840d-24570-0", names)
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

    def test_lod0_behavior_unchanged(self):
        """LOD0 原语义不回退：LOD0 缺席 DrawIB 被 LOD0 对象引用 -> 仍插桩。"""
        self._write_component_map("LOD0", {"b20f90ea": {"0": "b20f90ea-19182-0"}})
        self._write_vgmap_json("LOD0", "b20f90ea-19182-0", 3)
        self._register_object_with_groups("LOD0.84618ee0-22296-0", [3])
        ordered = [DrawCallModel(obj_name="LOD0.84618ee0-22296-0")]
        exporter = _make_exporter(ordered)

        names = self._workspace_unique_strs(ordered)
        self.assertIn("LOD0.b20f90ea-19182-0", names)
        self.assertEqual(len(exporter._efmi_stub_object_names), 1)
        stub = _fake_bpy_data.objects.get("LOD0.b20f90ea-19182-0")
        self.assertIsNotNone(stub)
        exporter._cleanup_stub_objects()


if __name__ == "__main__":
    unittest.main()
