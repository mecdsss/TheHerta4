"""ExportZZMI 合并骨架 INI 生成单测（fake 环境，不依赖 bpy/游戏）。"""

import importlib.util
import json
import os
import shutil
import struct
import sys
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "zzmi_merged_skeleton_ini_test_pkg"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


for package_name in (PKG, f"{PKG}.ui", f"{PKG}.ui.universal", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeIniSection:
    def __init__(self, section_type):
        self.SectionType = section_type
        self.SectionName = ""
        self.SectionLineList = []

    def append(self, line):
        self.SectionLineList.append(line)

    def new_line(self):
        self.SectionLineList.append("")


class _FakeIniBuilder:
    def __init__(self):
        self.sections = []

    def append_section(self, section):
        self.sections.append(section)


class _FakeIniSectionType:
    Constants = "Constants"
    Present = "Present"
    TextureOverrideIB = "TextureOverrideIB"
    TextureOverrideVB = "TextureOverrideVB"
    TextureOverrideVertexLimitRaise = "TextureOverrideVertexLimitRaise"
    ResourceBuffer = "ResourceBuffer"
    MergedSkeleton = "MergedSkeleton"


def _all_builder_lines(builder):
    lines = []
    for section in builder.sections:
        if section.SectionName:
            lines.append(f"[{section.SectionName}]")
        lines.extend(section.SectionLineList)
    return lines


class _FakeExportUnity:
    def __init__(self, blueprint_model):
        self.blueprint_model = blueprint_model
        self.drawib_model_list = []

    def add_unity_vs_resource_vb_sections(self, ini_builder, drawib_model):
        pass

    def add_unity_vs_texture_override_vlr_section(
        self, ini_builder, drawib_model, include_uav_byte_stride=True
    ):
        pass


_fake_global_properties = types.SimpleNamespace(
    import_merged_vgmap=lambda: True,
    forbid_auto_texture_ini=lambda: False,
    zzz_use_slot_fix=lambda: False,
)
# vg_map 导出写文件：path_generate_mod_folder 必须指向临时目录，防止测试残留
# 污染仓库根（2026-08-23 曾把 Meshes/zz_vgmap_*.buf 写到仓库根）
_FAKE_MOD_FOLDER = tempfile.mkdtemp(prefix="zzmi_mod_folder_")
_fake_global_config = types.SimpleNamespace(
    path_generatemod_buffer_folder=lambda: "",
    path_generate_mod_folder=lambda: _FAKE_MOD_FOLDER,
    get_workspace_name=lambda: "",
    path_workspace_folder=lambda: "",
)


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


def _load_real_module(qualname, relpath):
    path = REPO_ROOT / relpath
    spec = importlib.util.spec_from_file_location(qualname, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


_load_real_module(f"{PKG}.utils.json_utils", "utils/json_utils.py")
_load_real_module(f"{PKG}.utils.tbn_codec", "utils/tbn_codec.py")
_load_real_module(f"{PKG}.utils.format_utils", "utils/format_utils.py")
_load_real_module(f"{PKG}.utils.ssmt_error_utils", "utils/ssmt_error_utils.py")
_load_real_module(f"{PKG}.common.m_key", "common/m_key.py")
_load_real_module(f"{PKG}.common.object_prefix_helper", "common/object_prefix_helper.py")
_load_real_module(f"{PKG}.common.draw_call_model", "common/draw_call_model.py")

_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=_fake_global_config,
)
_install_module(
    f"{PKG}.common.global_properties",
    GlobalProterties=_fake_global_properties,
)
_install_module(
    f"{PKG}.common.global_key_count_helper",
    GlobalKeyCountHelper=types.SimpleNamespace(generated_mod_number=0),
)
_install_module(
    f"{PKG}.common.m_ini_helper",
    M_IniHelper=types.SimpleNamespace(
        get_drawindexed_str_list=lambda drawcall_list, obj_name_draw_offset_dict=None, base_vertex=0: [
            line
            for dc in drawcall_list
            for line in (
                f"; [mesh:{dc.obj_name}] [vertex_count:{dc.vertex_count}]",
                f"drawindexed = {dc.index_count},{dc.index_offset},{base_vertex}",
            )
        ],
        is_slot_binding_mark_type=lambda mark_type: False,
    ),
)
_install_module(
    f"{PKG}.common.m_ini_helper_gui",
    M_IniHelperGUI=types.SimpleNamespace(),
)
_install_module(
    f"{PKG}.common.m_ini_builder",
    M_IniBuilder=_FakeIniBuilder,
    M_IniSection=_FakeIniSection,
    M_SectionType=_FakeIniSectionType,
)
_install_module(f"{PKG}.ui.universal.unity", ExportUnity=_FakeExportUnity)
_install_module(
    f"{PKG}.utils.timer_utils",
    TimerUtils=types.SimpleNamespace(start_stage=lambda *_a, **_k: None, end_stage=lambda *_a, **_k: None),
)

_module_path = REPO_ROOT / "ui" / "universal" / "zzmi.py"
_spec = importlib.util.spec_from_file_location(f"{PKG}.ui.universal.zzmi", _module_path)
_zzmi_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _zzmi_module
_spec.loader.exec_module(_zzmi_module)


class _FakeGameType:
    OrderedCategoryNameList = ["Position", "Texcoord", "Blend"]
    GPU_PreSkinning = True
    CategoryDrawCategoryDict = {
        "Position": "Position",
        "Texcoord": "Texcoord",
        "Blend": "Position",  # ZZZ: Blend 画在 Position 类别（deform pass 同一 draw）
    }
    CategoryExtractSlotDict = {
        "Position": "vb0",
        "Texcoord": "vb1",
        "Blend": "vb2",
    }
    CategoryStrideDict = {
        "Position": 40,
        "Texcoord": 20,
        "Blend": 32,
    }


class _FakeSubmesh:
    def __init__(self, unique_str, vg_offset=0, vg_count=0, skeleton_group=0, vg_map=None,
                 deform_draw=0, original_vertex_count=0, vertex_count=0,
                 exported_vertex_count=0, match_first_index=0):
        self.unique_str = unique_str
        self.match_first_index = match_first_index
        self.vg_offset = vg_offset
        self.vg_count = vg_count
        self.skeleton_group = skeleton_group
        # 缺省 identity：local -> vg_offset + local（与真实反查写回一致）
        self.vg_map = vg_map if vg_map is not None else {
            local: vg_offset + local for local in range(vg_count)
        }
        # ZZMI 导出侧守卫元数据（反查写回）：deform draw 序号 / 原部件顶点数
        self.deform_draw_index = deform_draw
        self.original_vertex_count = original_vertex_count
        self.vertex_count = vertex_count
        # 导出 buffer 顶点数（去重后；_submesh_exported_vertex_count 用）
        self.index_vertex_id_dict = (
            list(range(exported_vertex_count)) if exported_vertex_count else None
        )
        self.category_buffer_dict = {}
        self.drawcall_model_list = []


class _FakeDrawIBModel:
    def __init__(self, draw_ib, submesh_model_list, part_map=None):
        self.draw_ib = draw_ib
        self.draw_ib_alias = draw_ib
        self.draw_number = 4643
        self.vertex_limit_hash = "dd9c8d5e"
        self.d3d11GameType = _FakeGameType()
        # 游戏类型桩使用类属性作为默认值；每个 DrawIB 必须复制布局字典，
        # 否则异构 BI4/BI16 回归测试会互相污染。
        self.d3d11GameType.CategoryStrideDict = dict(
            _FakeGameType.CategoryStrideDict
        )
        self.category_hash_dict = {
            "Position": "122883aa",
            "Texcoord": "5c0fefda",
            "Blend": "bf543990",
        }
        self.submesh_model_list = submesh_model_list
        self.category_buffer_dict = {}
        self.match_first_index_partname_dict = part_map or {}
        self.submesh_ib_dict = {
            submesh.unique_str: b"\x00\x00\x00\x00" for submesh in submesh_model_list
        }
        self.obj_name_draw_offset = {}

    def get_submesh_texture_override_suffix(self, submesh_model):
        return submesh_model.unique_str.replace("-", "_")

    def get_submesh_ib_resource_name(self, submesh_model):
        return "Resource_" + submesh_model.unique_str.replace("-", "_") + "_Index"

    def get_submesh_texture_markup_info_list(self, submesh_model):
        return []


def _make_exporter(drawib_models, merged_vgmap=True, ordered_drawcalls=None):
    _fake_global_properties.import_merged_vgmap = lambda: merged_vgmap
    blueprint_model = types.SimpleNamespace(
        cross_ib_info_dict={},
        cross_ib_method_dict={},
        cross_ib_mapping_method={},
        has_cross_ib=False,
        cross_ib_object_names=set(),
        keyname_mkey_dict={},
        ordered_draw_obj_data_model_list=(ordered_drawcalls if ordered_drawcalls is not None else []),
    )
    exporter = _zzmi_module.ExportZZMI(blueprint_model)
    exporter.drawib_model_list = drawib_models
    return exporter


class ZZSIMergedSkeletonCollectTests(unittest.TestCase):
    def test_collect_gated_by_checkbox(self):
        models = [_FakeDrawIBModel("b20f90ea", [_FakeSubmesh("LOD0.b20f90ea-19182-0", 154, 51)])]
        exporter = _make_exporter(models, merged_vgmap=False)
        components, id_dict = exporter._collect_merged_skeleton_components()
        self.assertEqual(components, [])
        self.assertEqual(id_dict, {})

    def test_collect_dedup_by_drawib_and_sort(self):
        models = [
            _FakeDrawIBModel("84618ee0", [
                _FakeSubmesh("LOD0.84618ee0-22296-0", 105, 49),
                _FakeSubmesh("LOD0.84618ee0-1164-22296", 105, 49),
            ]),
            _FakeDrawIBModel("a23aa8a3", [_FakeSubmesh("LOD0.a23aa8a3-42759-0", 0, 105)]),
            _FakeDrawIBModel("b20f90ea", [_FakeSubmesh("LOD0.b20f90ea-19182-0", 154, 51)]),
        ]
        exporter = _make_exporter(models, merged_vgmap=True)
        components, id_dict = exporter._collect_merged_skeleton_components()
        # 按 vg_offset 排序；84618ee0 两个子网格只收一个
        self.assertEqual([c["draw_ib"] for c in components], ["a23aa8a3", "84618ee0", "b20f90ea"])
        self.assertEqual(id_dict, {"a23aa8a3": 0, "84618ee0": 1, "b20f90ea": 2})
        self.assertEqual(sum(c["vg_count"] for c in components), 205)

    def test_collect_skips_submesh_without_data(self):
        models = [_FakeDrawIBModel("b20f90ea", [_FakeSubmesh("LOD0.b20f90ea-19182-0", 0, 0)])]
        exporter = _make_exporter(models, merged_vgmap=True)
        components, _ = exporter._collect_merged_skeleton_components()
        self.assertEqual(components, [])

    def test_collect_rejects_vgmap_slot_outside_component_range(self):
        models = [_FakeDrawIBModel(
            "b20f90ea",
            [_FakeSubmesh("LOD0.b20f90ea-19182-0", 0, 1, vg_map={0: 999})],
        )]
        exporter = _make_exporter(models, merged_vgmap=True)
        components, id_dict = exporter._collect_merged_skeleton_components()
        self.assertEqual(components, [])
        self.assertEqual(id_dict, {})

    def test_collect_rejects_stale_vgmap_algorithm_version(self):
        submesh = _FakeSubmesh("LOD0.b20f90ea-19182-0", 0, 1)
        submesh.vg_map_algorithm_version = 1
        exporter = _make_exporter(
            [_FakeDrawIBModel("b20f90ea", [submesh])], merged_vgmap=True
        )
        components, id_dict = exporter._collect_merged_skeleton_components()
        self.assertEqual(components, [])
        self.assertEqual(id_dict, {})


class ZZSIMergedSkeletonIniTests(unittest.TestCase):
    def _build_vb_section(self, with_merged=True):
        submesh = _FakeSubmesh("LOD0.b20f90ea-19182-0", 154, 51)
        model = _FakeDrawIBModel("b20f90ea", [submesh])
        exporter = _make_exporter([model], merged_vgmap=True)
        exporter.merged_skeleton_components, exporter.merged_skeleton_component_id_dict = (
            exporter._collect_merged_skeleton_components()
        )
        exporter.has_merged_skeleton = len(exporter.merged_skeleton_components) > 0
        if not with_merged:
            exporter.merged_skeleton_component_id_dict = {}
        builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder, model)
        lines = _all_builder_lines(builder)
        return lines

    def test_vb_section_injects_copy_and_swap(self):
        lines = self._build_vb_section(with_merged=True)
        text = "\n".join(lines)

        # 逐 pass attach：deform 段内 copy 当帧 palette -> attach -> 绑定本组骨架。
        idx_copy = text.index("ResourceZZPalette_b20f90ea = copy vs-t0 unless_null")
        idx_run = text.index("run = CustomShaderZZMIMergedSkeletonAttach_C0")
        idx_swap = text.index("vs-t0 = ResourceZZMergedSkeleton_G0")
        idx_draw = text.index("draw = 4643, 0")
        self.assertLess(idx_copy, idx_run)
        self.assertLess(idx_run, idx_swap)
        self.assertLess(idx_swap, idx_draw)
        # 运行时不重放持久 palette，避免脏数据。
        self.assertNotIn("$zz_ms_attach_offset", text)

    def test_vb_section_without_merged_stays_legacy(self):
        lines = self._build_vb_section(with_merged=False)
        text = "\n".join(lines)
        self.assertNotIn("ResourceZZMergedSkeleton", text)
        self.assertNotIn("CustomShaderZZMIMergedSkeletonAttach", text)
        self.assertIn("handling = skip", text)
        self.assertIn("draw = 4643, 0", text)

    def test_merged_skeleton_sections_content(self):
        submesh_a = _FakeSubmesh("LOD0.a23aa8a3-42759-0", 0, 105)
        submesh_b = _FakeSubmesh("LOD0.b20f90ea-19182-0", 105, 51)
        exporter = _make_exporter(
            [_FakeDrawIBModel("a23aa8a3", [submesh_a]), _FakeDrawIBModel("b20f90ea", [submesh_b])],
            merged_vgmap=True,
        )
        exporter.merged_skeleton_components, exporter.merged_skeleton_component_id_dict = (
            exporter._collect_merged_skeleton_components()
        )
        builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(builder)
        lines = _all_builder_lines(builder)
        text = "\n".join(lines)

        self.assertIn("global $zz_ms_seen_c0 = 0", text)
        self.assertIn("[ResourceZZMergedSkeleton_G0]", text)
        self.assertIn("type = RWStructuredBuffer", text)
        self.assertIn("stride = 48", text)
        self.assertIn("array = 156", text)  # 全宽 = max(0+105, 105+51) = 156
        # 无 CB1 校准：不出捕获资源/捕获段/校准引用
        self.assertNotIn("ResourceZZCb1", text)
        self.assertNotIn("Cb1Capture", text)
        self.assertNotIn("cs-cb1", text)
        self.assertNotIn("cs-cb2", text)
        # palette 持久副本资源 + vg_map 表（attach CS 按此写槽位；data 行 = float4 元素流）
        self.assertIn("[ResourceZZPalette_a23aa8a3]", text)
        self.assertIn("[ResourceZZPalette_b20f90ea]", text)
        # identity 映射：a23aa8a3 槽位 0..104（vg_map 用 filename 二进制加载——
        # 多行 data 在本 3DMigoto fork 上只写第 0 个元素，2026-08-23 实证）
        self.assertIn("[ResourceZZVgMap_a23aa8a3]", text)
        self.assertIn("type = Buffer", text)
        self.assertIn("format = R32G32B32A32_UINT", text)
        self.assertIn("filename = Meshes/zz_vgmap_a23aa8a3.buf", text)
        self.assertIn("[ResourceZZVgMap_b20f90ea]", text)
        self.assertIn("filename = Meshes/zz_vgmap_b20f90ea.buf", text)
        # 逐部件 attach 段（x1=0 / y1=vg_count；cs-t1 = vg_map；Dispatch 动态取整）
        self.assertIn("[CustomShaderZZMIMergedSkeletonAttach_C0]", lines)
        self.assertIn("[CustomShaderZZMIMergedSkeletonAttach_C1]", lines)
        self.assertIn("cs = ./res/zzmi_merged_skeleton_attach.hlsl", text)
        self.assertIn("x1 = 0", text)
        self.assertIn("y1 = 105", text)
        self.assertIn("cs-t0 = ref ResourceZZPalette_a23aa8a3", text)
        self.assertIn("cs-t1 = ref ResourceZZVgMap_a23aa8a3", text)
        self.assertIn("cs-t0 = ref ResourceZZPalette_b20f90ea", text)
        self.assertIn("cs-u0 = ref ResourceZZMergedSkeleton_G0", text)
        self.assertIn("Dispatch = 2, 1, 1", text)  # ceil(105 / 64)
        # [Present] 只清理标记，不重放 attach。
        self.assertIn("[Present]", text)
        present_text = text.split("[Present]")[1]
        self.assertIn("$zz_ms_seen_c0 = 0", present_text)
        self.assertIn("$zz_ms_seen_c1 = 0", present_text)
        self.assertNotIn("run = CustomShaderZZMIMergedSkeletonAttach_", present_text)
        self.assertNotIn("$zz_ms_attach_offset", present_text)
        self.assertNotIn("$zz_ms_attach_count", present_text)

    def test_merged_skeleton_sections_per_group(self):
        """组内统一骨架版：每组一套全宽骨架资源；逐部件直拷 attach 到本组；无任何捕获/校准段。"""
        # 组 0（身体）：a23aa8a3(0,105) + b20f90ea(105,51)
        # 组 1（头部）：64d7d56f(156,1) + b51bdd59(157,11)
        exporter = _make_exporter(
            [
                _FakeDrawIBModel("a23aa8a3", [_FakeSubmesh("LOD0.a23aa8a3-42759-0", 0, 105, 0)]),
                _FakeDrawIBModel("b20f90ea", [_FakeSubmesh("LOD0.b20f90ea-19182-0", 105, 51, 0)]),
                _FakeDrawIBModel("64d7d56f", [_FakeSubmesh("LOD0.64d7d56f-900-0", 156, 1, 1)]),
                _FakeDrawIBModel("b51bdd59", [_FakeSubmesh("LOD0.b51bdd59-864-0", 157, 11, 1)]),
            ],
            merged_vgmap=True,
        )
        exporter.merged_skeleton_components, exporter.merged_skeleton_component_id_dict = (
            exporter._collect_merged_skeleton_components()
        )
        builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(builder)
        lines = _all_builder_lines(builder)
        text = "\n".join(lines)

        self.assertIn("[ResourceZZMergedSkeleton_G0]", text)
        self.assertIn("[ResourceZZMergedSkeleton_G1]", text)
        # 两组全宽 array = 全局 max(157+11) = 168（只数骨架资源的 array 行；
        # palette 副本资源自带 array=vg_count 行，需排除——骨架段结构：header/type/stride/array）
        skeleton_arrays = [
            lines[i + 3]
            for i, line in enumerate(lines)
            if line.startswith("[ResourceZZMergedSkeleton_G")
        ]
        self.assertEqual(skeleton_arrays, ["array = 168", "array = 168"])
        # 无捕获段、无校准资源、无 cb 引用
        self.assertNotIn("Cb1Capture", text)
        self.assertNotIn("ResourceZZCb1", text)
        self.assertNotIn("cs-cb1", text)
        self.assertNotIn("cs-cb2", text)
        # 逐部件 attach：4 段，各自写回**本组**骨架，带 vg_map 表
        for cid in range(4):
            self.assertIn(f"[CustomShaderZZMIMergedSkeletonAttach_C{cid}]", lines)
        c0 = text.split("[CustomShaderZZMIMergedSkeletonAttach_C0]")[1].split("[")[0]
        self.assertIn("cs-u0 = ref ResourceZZMergedSkeleton_G0", c0)  # C0 属组 0
        self.assertIn("cs-t1 = ref ResourceZZVgMap_a23aa8a3", c0)
        c2 = text.split("[CustomShaderZZMIMergedSkeletonAttach_C2]")[1].split("[")[0]
        self.assertIn("cs-u0 = ref ResourceZZMergedSkeleton_G1", c2)  # C2 属组 1
        self.assertIn("cs-t1 = ref ResourceZZVgMap_64d7d56f", c2)
        # [Present] 只清理标记，不重放 attach
        present_text = text.split("[Present]")[1]
        self.assertNotIn("run = CustomShaderZZMIMergedSkeletonAttach_", present_text)

    def test_vb_section_rebinds_to_own_group_resource(self):
        """每个 deform VB 段 copy 当帧 palette 并立即 attach 到本组骨架。"""
        model_g1 = _FakeDrawIBModel("64d7d56f", [_FakeSubmesh("LOD0.64d7d56f-900-0", 156, 1, 1)])
        model_g0 = _FakeDrawIBModel("a23aa8a3", [_FakeSubmesh("LOD0.a23aa8a3-42759-0", 0, 105, 0)])
        exporter = _make_exporter([model_g1, model_g0], merged_vgmap=True)
        exporter.merged_skeleton_components, exporter.merged_skeleton_component_id_dict = (
            exporter._collect_merged_skeleton_components()
        )

        builder0 = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder0, model_g0)
        text0 = "\n".join(builder0.sections[0].SectionLineList)
        self.assertIn("ResourceZZPalette_a23aa8a3 = copy vs-t0 unless_null", text0)
        self.assertIn("run = CustomShaderZZMIMergedSkeletonAttach_C0", text0)
        self.assertIn("vs-t0 = ResourceZZMergedSkeleton_G0", text0)

        builder1 = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder1, model_g1)
        text1 = "\n".join(builder1.sections[0].SectionLineList)
        self.assertIn("ResourceZZPalette_64d7d56f = copy vs-t0 unless_null", text1)
        self.assertIn("run = CustomShaderZZMIMergedSkeletonAttach_C1", text1)
        self.assertIn("vs-t0 = ResourceZZMergedSkeleton_G1", text1)

        # [Present] 只清理标记，不重放 attach。
        builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(builder)
        present_text = "\n".join(_all_builder_lines(builder)).split("[Present]")[1]
        self.assertNotIn("run = CustomShaderZZMIMergedSkeletonAttach_", present_text)
        self.assertIn("$zz_ms_seen_c0 = 0", present_text)
        self.assertIn("$zz_ms_seen_c1 = 0", present_text)

    def test_merged_skeleton_buffer_covers_offset_gap(self):
        """回归：中间部件缺失时 buffer 必须按 max(vg_offset+vg_count) 声明，而非 sum(vg_count)。

        场景（用户实测）：3 个部件统一顶点组 0~10 / 11~30 / 31~50，
        用户 join 部件 1+3、部件 2 不生成 → 导出组件 (0,11) + (31,20)。
        sum(vg_count)=31 会让部件 3 的 attach（offset=31）与顶点全局 id 31~50 越界；
        正确口径 max(vg_offset+vg_count)=51。
        """
        submesh_1 = _FakeSubmesh("LOD0.aaaaaaaa-100-0", 0, 11)    # 部件 1：0~10
        submesh_3 = _FakeSubmesh("LOD0.cccccccc-300-0", 31, 20)   # 部件 3：31~50（部件 2 缺席）
        exporter = _make_exporter(
            [_FakeDrawIBModel("aaaaaaaa", [submesh_1]), _FakeDrawIBModel("cccccccc", [submesh_3])],
            merged_vgmap=True,
        )
        exporter.merged_skeleton_components, exporter.merged_skeleton_component_id_dict = (
            exporter._collect_merged_skeleton_components()
        )
        builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(builder)
        text = "\n".join(_all_builder_lines(builder))

        self.assertIn("array = 51", text)  # max(0+11, 31+20) = 51，而非 sum=31

    def test_g4_slots_use_runtime_merged_skeleton_bounds(self):
        """G4 的 249..265 槽必须由实际 UAV 长度放行，不能被角色专用常量截断。"""
        shader = (REPO_ROOT / "Toolset" / "zzmi_merged_skeleton_attach.hlsl").read_text(
            encoding="utf-8"
        )

        threads = _zzmi_module.ExportZZMI.MERGED_SKELETON_ATTACH_THREADS
        self.assertIn(f"[numthreads({threads}, 1, 1)]", shader)
        self.assertIn(
            "src_palette.GetDimensions(palette_count, palette_stride)", shader
        )
        self.assertIn("vg_map.GetDimensions(vg_map_count)", shader)
        self.assertIn(
            "merged_skeleton.GetDimensions(merged_count, merged_stride)", shader
        )
        self.assertIn("slot < merged_count", shader)
        self.assertNotIn("slot < 249", shader)

        submesh_add = _FakeSubmesh(
            "LOD0.add6ff13-624-0",
            249,
            1,
            4,
            vg_map={0: 249},
        )
        submesh_d892 = _FakeSubmesh(
            "LOD0.d892c658-2256-0",
            250,
            16,
            4,
            vg_map={local: 250 + local for local in range(16)},
        )
        exporter = _make_exporter(
            [
                _FakeDrawIBModel("add6ff13", [submesh_add]),
                _FakeDrawIBModel("d892c658", [submesh_d892]),
            ],
            merged_vgmap=True,
        )
        exporter.merged_skeleton_components, exporter.merged_skeleton_component_id_dict = (
            exporter._collect_merged_skeleton_components()
        )
        builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(builder)
        text = "\n".join(_all_builder_lines(builder))

        self.assertIn("[ResourceZZMergedSkeleton_G4]", text)
        self.assertEqual(text.count("[ResourceZZMergedSkeleton_G4]"), 1)
        self.assertIn("array = 266", text)
        meshes_path = Path(_FAKE_MOD_FOLDER) / "Meshes"
        add_slots = [
            value[0]
            for value in struct.iter_unpack(
                "<4I", (meshes_path / "zz_vgmap_add6ff13.buf").read_bytes()
            )
        ]
        d892_slots = [
            value[0]
            for value in struct.iter_unpack(
                "<4I", (meshes_path / "zz_vgmap_d892c658.buf").read_bytes()
            )
        ]
        self.assertEqual(add_slots + d892_slots, list(range(249, 266)))

    def test_attach_dispatch_scales_past_512_bones(self):
        """numthreads=64 时，513 根 palette 必须生成 9 个 dispatch group。"""
        count = 513
        submesh = _FakeSubmesh("LOD0.aaaaaaaa-100-0", 0, count)
        exporter = _make_exporter(
            [_FakeDrawIBModel("aaaaaaaa", [submesh])], merged_vgmap=True
        )
        exporter.merged_skeleton_components, exporter.merged_skeleton_component_id_dict = (
            exporter._collect_merged_skeleton_components()
        )
        builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(builder)
        text = "\n".join(_all_builder_lines(builder))

        self.assertIn("y1 = 513", text)
        self.assertIn("Dispatch = 9, 1, 1", text)

    def test_vgmap_publish_failure_aborts_and_preserves_previous_file(self):
        submesh = _FakeSubmesh("LOD0.aaaaaaaa-100-0", 0, 1)
        exporter = _make_exporter(
            [_FakeDrawIBModel("aaaaaaaa", [submesh])], merged_vgmap=True
        )
        exporter.merged_skeleton_components, exporter.merged_skeleton_component_id_dict = (
            exporter._collect_merged_skeleton_components()
        )
        target = Path(_FAKE_MOD_FOLDER) / "Meshes" / "zz_vgmap_aaaaaaaa.buf"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"previous")

        with mock.patch.object(_zzmi_module.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(RuntimeError):
                exporter.add_merged_skeleton_sections(_FakeIniBuilder())
        self.assertEqual(target.read_bytes(), b"previous")

    def test_missing_attach_shader_aborts_export(self):
        exporter = _make_exporter([], merged_vgmap=True)
        with mock.patch.object(_zzmi_module.os.path, "isfile", return_value=False):
            with self.assertRaises(FileNotFoundError):
                exporter._copy_merged_skeleton_shader_to_mod()


class ZZMICrossGroupGuardTests(unittest.TestCase):
    """跨组别引用守卫（无校准模式）：引用非本组骨骼 id 必须大声报警。"""

    def setUp(self):
        _fake_bpy_data.objects._items.clear()
        _fake_bpy_data.meshes._items.clear()

    def _register_obj(self, name, bone_ids, stub=False):
        """注册 fake 对象：顶点 i 权重挂顶点组 i，组名 = bone_ids[i]（全局骨骼 id）。"""
        mesh = _fake_bpy_data.meshes.new(name=name + "_mesh")
        mesh.vertices = [
            types.SimpleNamespace(
                groups=[types.SimpleNamespace(group=i, weight=1.0)]
            )
            for i in range(len(bone_ids))
        ]
        obj = _fake_bpy_data.objects.new(name=name, object_data=mesh)
        obj.vertex_groups = _FakeVertexGroups()
        for bone_id in bone_ids:
            obj.vertex_groups.append(_FakeVertexGroup(str(bone_id)))
        if stub:
            obj["ZZMI_STUB"] = 1
        return obj

    def _make_component_exporter(self, draw_ib, submesh, components):
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        submesh.drawcall_model_list = [dcm(obj_name=submesh.unique_str)]
        exporter = _make_exporter([_FakeDrawIBModel(draw_ib, [submesh])], merged_vgmap=True)
        exporter.merged_skeleton_components = components
        exporter.merged_skeleton_component_id_dict = {
            c["draw_ib"]: i for i, c in enumerate(components)
        }
        return exporter

    def _capture_warnings(self, exporter):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exporter._warn_cross_group_bone_references()
        return buf.getvalue()

    def test_cross_group_reference_warns(self):
        """a23aa8a3（组 0，槽 0~104）的顶点引用了组 1 的骨骼 id 105 -> 报警。"""
        self._register_obj("LOD0.a23aa8a3-42759-0", [0, 100, 105])
        submesh = _FakeSubmesh("LOD0.a23aa8a3-42759-0", 0, 105, 0)
        components = [
            {"draw_ib": "a23aa8a3", "vg_offset": 0, "vg_count": 105, "skeleton_group": 0},
            {"draw_ib": "b20f90ea", "vg_offset": 105, "vg_count": 51, "skeleton_group": 1},
        ]
        exporter = self._make_component_exporter("a23aa8a3", submesh, components)
        out = self._capture_warnings(exporter)
        self.assertIn("禁止跨组别骨骼合并", out)
        self.assertIn("a23aa8a3", out)
        self.assertIn("骨架组 G0", out)
        self.assertIn("105", out)
        self.assertIn("归属组: [1]", out)

    def test_same_group_reference_silent(self):
        """同组骨骼引用（含并入本组其它部件的骨骼 id）不报警。"""
        self._register_obj("LOD0.b20f90ea-19182-0", [105, 106, 0])
        submesh = _FakeSubmesh("LOD0.b20f90ea-19182-0", 105, 51, 0)
        components = [
            {"draw_ib": "a23aa8a3", "vg_offset": 0, "vg_count": 105, "skeleton_group": 0},
            {"draw_ib": "b20f90ea", "vg_offset": 105, "vg_count": 51, "skeleton_group": 0},
        ]
        exporter = self._make_component_exporter("b20f90ea", submesh, components)
        out = self._capture_warnings(exporter)
        self.assertEqual(out, "")

    def test_stub_object_skipped(self):
        """占位小三角面（ZZMI_STUB，权重挂组 "0"）不触发跨组报警。"""
        # 组 1 的范围是 [156,157)：stub 引用骨骼 0（组外）——若不跳过会误报
        self._register_obj("LOD0.64d7d56f-900-0", [0], stub=True)
        submesh = _FakeSubmesh("LOD0.64d7d56f-900-0", 156, 1, 1)
        components = [
            {"draw_ib": "a23aa8a3", "vg_offset": 0, "vg_count": 105, "skeleton_group": 0},
            {"draw_ib": "64d7d56f", "vg_offset": 156, "vg_count": 1, "skeleton_group": 1},
        ]
        exporter = self._make_component_exporter("64d7d56f", submesh, components)
        out = self._capture_warnings(exporter)
        self.assertEqual(out, "")


class ZZSIMissingPartsGuardTests(unittest.TestCase):
    def test_warns_when_part_missing(self):
        # 工作空间有两个部件（first_index 0 / 22296），导出只找到第一个的对象
        model = _FakeDrawIBModel(
            "84618ee0",
            [_FakeSubmesh("LOD0.84618ee0-22296-0", 105, 49)],
            part_map={0: "1", 22296: "2"},
        )
        # _FakeSubmesh.match_first_index 默认 0，即只有部件 "1" 有对象
        exporter = _make_exporter([model], merged_vgmap=True)
        report = exporter._warn_missing_drawib_parts()
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]["draw_ib"], "84618ee0")
        self.assertEqual(report[0]["missing"], [(22296, "2")])
        self.assertEqual(report[0]["present_count"], 1)
        self.assertEqual(report[0]["expected_count"], 2)

    def test_no_warning_when_complete(self):
        model = _FakeDrawIBModel(
            "84618ee0",
            [_FakeSubmesh("LOD0.84618ee0-22296-0", 105, 49)],
            part_map={0: "1"},
        )
        exporter = _make_exporter([model], merged_vgmap=True)
        report = exporter._warn_missing_drawib_parts()
        self.assertEqual(report, [])


class ZZMIStubObjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zzmi_stub_ws_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        lod0 = os.path.join(self.tmp, "LOD0")
        os.makedirs(lod0, exist_ok=True)
        component_map = {
            "84618ee0": {"0": "84618ee0-22296-0", "1": "84618ee0-1164-22296"},
            "b20f90ea": {"0": "b20f90ea-19182-0"},
        }
        with open(os.path.join(lod0, "DrawIB-Component.json"), "w", encoding="utf-8") as f:
            json.dump(component_map, f)
        _fake_global_config.path_workspace_folder = lambda: self.tmp
        self.addCleanup(lambda: setattr(_fake_global_config, "path_workspace_folder", lambda: ""))
        # 清 fake bpy 注册表
        _fake_bpy_data.objects._items.clear()
        _fake_bpy_data.meshes._items.clear()

    def test_stub_created_for_missing_component(self):
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.84618ee0-22296-0")]
        exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)

        names = [str(dc.get_workspace_unique_str()) for dc in ordered]
        self.assertIn("LOD0.84618ee0-1164-22296", names)
        self.assertEqual(len(exporter._zzmi_stub_object_names), 1)

        stub = _fake_bpy_data.objects.get("LOD0.84618ee0-1164-22296")
        self.assertIsNotNone(stub)
        self.assertEqual(stub.get("ZZMI_STUB"), 1)
        self.assertEqual(stub.vertex_groups[0].name, "0")
        self.assertEqual(stub.vertex_groups[0].add_calls[0][1], 1.0)
        stub_draw_call = next(
            dc for dc in ordered
            if dc.get_workspace_unique_str() == "LOD0.84618ee0-1164-22296"
        )
        # 占位段即使在 SubMeshModel 之前被消费，也必须是可绘制的 3 索引。
        self.assertEqual(stub_draw_call.vertex_count, 3)
        self.assertEqual(stub_draw_call.index_count, 3)
        self.assertEqual(stub_draw_call.index_offset, 0)
        # 极限小三角面
        verts, _edges, faces = stub.data.from_pydata_calls[0]
        self.assertEqual(len(verts), 3)
        self.assertEqual(faces, [(0, 1, 2)])
        self.assertLess(max(abs(c) for v in verts for c in v), 1e-3)

        exporter._cleanup_stub_objects()
        self.assertIsNone(_fake_bpy_data.objects.get("LOD0.84618ee0-1164-22296"))
        self.assertEqual(exporter._zzmi_stub_object_names, [])
        self.assertNotIn(
            "LOD0.84618ee0-1164-22296",
            [str(dc.get_workspace_unique_str()) for dc in ordered],
        )

    def test_constructor_failure_after_stub_injection_cleans_all_stub_state(self):
        """基类构造失败时 export() 尚未运行，也必须清理对象、mesh 和注入 DrawCall。"""
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.84618ee0-22296-0")]
        blueprint_model = types.SimpleNamespace(
            cross_ib_info_dict={},
            cross_ib_method_dict={},
            cross_ib_mapping_method={},
            has_cross_ib=False,
            cross_ib_object_names=set(),
            keyname_mkey_dict={},
            ordered_draw_obj_data_model_list=ordered,
        )

        with mock.patch.object(
            _FakeExportUnity,
            "__init__",
            side_effect=RuntimeError("forced base constructor failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "forced base constructor failure"):
                _zzmi_module.ExportZZMI(blueprint_model)

        self.assertIsNone(_fake_bpy_data.objects.get("LOD0.84618ee0-1164-22296"))
        self.assertEqual(_fake_bpy_data.meshes._items, {})
        self.assertNotIn(
            "LOD0.84618ee0-1164-22296",
            [str(dc.get_workspace_unique_str()) for dc in ordered],
        )

    def test_stub_creation_failure_mid_batch_rolls_back_earlier_stub(self):
        """批量补占位中途失败时，已创建但尚未从 helper 返回的占位也必须回滚。"""
        lod0 = os.path.join(self.tmp, "LOD0")
        with open(os.path.join(lod0, "DrawIB-Component.json"), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "84618ee0": {
                        "0": "84618ee0-22296-0",
                        "1": "84618ee0-1164-22296",
                        "2": "84618ee0-300-23460",
                    }
                },
                f,
            )

        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.84618ee0-22296-0")]
        blueprint_model = types.SimpleNamespace(
            cross_ib_info_dict={},
            cross_ib_method_dict={},
            cross_ib_mapping_method={},
            has_cross_ib=False,
            cross_ib_object_names=set(),
            keyname_mkey_dict={},
            ordered_draw_obj_data_model_list=ordered,
        )
        original_create = _zzmi_module.ExportZZMI._create_stub_object
        create_count = 0

        def fail_second_create(exporter, bare_unique_str):
            nonlocal create_count
            create_count += 1
            if create_count == 2:
                raise RuntimeError("forced second stub failure")
            return original_create(exporter, bare_unique_str)

        with mock.patch.object(
            _zzmi_module.ExportZZMI,
            "_create_stub_object",
            new=fail_second_create,
        ):
            with self.assertRaisesRegex(RuntimeError, "forced second stub failure"):
                _zzmi_module.ExportZZMI(blueprint_model)

        self.assertEqual(_fake_bpy_data.objects._items, {})
        self.assertEqual(_fake_bpy_data.meshes._items, {})
        self.assertEqual(
            [str(dc.get_workspace_unique_str()) for dc in ordered],
            ["LOD0.84618ee0-22296-0"],
        )

    def test_buffers_only_failure_still_cleans_stub_transaction(self):
        """多轮导出的 buffer-only 路径也必须在失败时清理占位事务。"""
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.84618ee0-22296-0")]
        exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)

        with mock.patch.object(
            _FakeExportUnity,
            "export_buffers_only",
            side_effect=RuntimeError("forced buffer export failure"),
            create=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "forced buffer export failure"):
                exporter.export_buffers_only()

        self.assertIsNone(_fake_bpy_data.objects.get("LOD0.84618ee0-1164-22296"))
        self.assertEqual(_fake_bpy_data.meshes._items, {})
        self.assertEqual(
            [str(dc.get_workspace_unique_str()) for dc in ordered],
            ["LOD0.84618ee0-22296-0"],
        )

    def test_same_blueprint_can_inject_and_cleanup_stub_twice(self):
        """一次导出清理后，同一 BluePrintModel 再导出不能引用已删除的旧 DrawCall。"""
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.84618ee0-22296-0")]

        first = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)
        first._cleanup_stub_objects()
        self.assertEqual(len(ordered), 1)

        second = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)
        self.assertEqual(len(second._zzmi_stub_object_names), 1)
        self.assertEqual(len(ordered), 2)
        second._cleanup_stub_objects()
        self.assertEqual(len(ordered), 1)

    def test_no_stub_when_checkbox_off(self):
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.84618ee0-22296-0")]
        exporter = _make_exporter([], merged_vgmap=False, ordered_drawcalls=ordered)
        names = [str(dc.get_workspace_unique_str()) for dc in ordered]
        self.assertNotIn("LOD0.84618ee0-1164-22296", names)
        self.assertEqual(exporter._zzmi_stub_object_names, [])

    def test_no_stub_when_whole_drawib_absent(self):
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.b20f90ea-19182-0")]
        exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)
        # 84618ee0 整个 DrawIB 不在蓝图且无 VGMap 数据 = 不生成，不插桩
        self.assertEqual(exporter._zzmi_stub_object_names, [])
        names = [str(dc.get_workspace_unique_str()) for dc in ordered]
        self.assertEqual(names, ["LOD0.b20f90ea-19182-0"])

    def _write_vgmap_json(self, bare, gid, group=None):
        type_dir = os.path.join(self.tmp, "LOD0", bare, "TYPE_GPU_TEST_")
        os.makedirs(type_dir, exist_ok=True)
        payload = {"VGMap": {"0": str(gid)}, "VGOffset": 0, "VGCount": 1}
        if group is not None:
            payload["SkeletonGroup"] = group
        with open(os.path.join(type_dir, bare + ".json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def _register_present_object_with_groups(self, name, used_gids, group_names=None):
        mesh = _fake_bpy_data.meshes.new(name=name + "_mesh")
        group_names = list(group_names or [str(gid) for gid in used_gids])
        obj = _fake_bpy_data.objects.new(name=name, object_data=mesh)
        obj.vertex_groups = _FakeVertexGroups()
        for group_name in group_names:
            obj.vertex_groups.append(_FakeVertexGroup(str(group_name)))
        group_indices = [group_names.index(str(gid)) for gid in used_gids]
        mesh.vertices = [
            types.SimpleNamespace(
                groups=[types.SimpleNamespace(group=group_index, weight=1.0)]
            )
            for group_index in group_indices
        ]
        return obj

    def test_stub_when_absent_drawib_absorbed_into_other_object(self):
        # 84618ee0 全缺，但其 VGMap 全局 id=7 被现存对象（b20f90ea）的顶点引用 = 被合并
        self._write_vgmap_json("84618ee0-22296-0", 7)
        self._write_vgmap_json("84618ee0-1164-22296", 7)
        self._register_present_object_with_groups("LOD0.b20f90ea-19182-0", [7])

        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.b20f90ea-19182-0")]
        exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)

        names = [str(dc.get_workspace_unique_str()) for dc in ordered]
        self.assertIn("LOD0.84618ee0-22296-0", names)
        self.assertIn("LOD0.84618ee0-1164-22296", names)
        self.assertEqual(len(exporter._zzmi_stub_object_names), 2)
        exporter._cleanup_stub_objects()

    def test_no_stub_when_absent_drawib_not_referenced(self):
        # 84618ee0 全缺，其 VGMap 全局 id=250 没有任何对象引用 = 用户故意不生成
        self._write_vgmap_json("84618ee0-22296-0", 250)
        self._write_vgmap_json("84618ee0-1164-22296", 250)
        self._register_present_object_with_groups("LOD0.b20f90ea-19182-0", [7])

        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.b20f90ea-19182-0")]
        exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)

        names = [str(dc.get_workspace_unique_str()) for dc in ordered]
        self.assertNotIn("LOD0.84618ee0-22296-0", names)
        self.assertNotIn("LOD0.84618ee0-1164-22296", names)
        self.assertEqual(exporter._zzmi_stub_object_names, [])

    def test_absorption_uses_numeric_vertex_group_name_not_blender_index(self):
        """替换模型组名稀疏时，吸收判定必须读取组名而不是内部索引。"""
        self._write_vgmap_json("84618ee0-22296-0", 7)
        self._write_vgmap_json("84618ee0-1164-22296", 7)
        self._register_present_object_with_groups(
            "LOD0.b20f90ea-19182-0",
            [7],
            group_names=["unused", "7"],
        )

        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.b20f90ea-19182-0")]
        exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)

        names = [str(dc.get_workspace_unique_str()) for dc in ordered]
        self.assertIn("LOD0.84618ee0-22296-0", names)
        self.assertIn("LOD0.84618ee0-1164-22296", names)
        exporter._cleanup_stub_objects()


class ZZSIMergedMeshRedirectTests(unittest.TestCase):
    """合并网格自动重定向（2026-08-25 设计兑现：合并网格可挂在任意 DrawIB）。

    逐 pass attach 只在各部件自己的 deform draw 前写入**本部件**骨骼；palette 是
    per-pass 独立上传的 ring scratch，早 pass 时刻读不到晚 pass 部件的当帧骨骼。
    因此挂在早 pass 的合并网格由导出器**自动**把 deform+render 挪到组内最后一个
    deform draw——用户无感，任意 IB 挂载均正确。
    """

    def setUp(self):
        _fake_bpy_data.objects._items.clear()
        _fake_bpy_data.meshes._items.clear()

    def _register_obj(self, name, bone_ids):
        """fake 对象：顶点 i 权重挂顶点组 i，组名 = bone_ids[i]（全局骨骼 id）。"""
        mesh = _fake_bpy_data.meshes.new(name=name + "_mesh")
        mesh.vertices = [
            types.SimpleNamespace(groups=[types.SimpleNamespace(group=i, weight=1.0)])
            for i in range(len(bone_ids))
        ]
        obj = _fake_bpy_data.objects.new(name=name, object_data=mesh)
        obj.vertex_groups = _FakeVertexGroups()
        for bone_id in bone_ids:
            obj.vertex_groups.append(_FakeVertexGroup(str(bone_id)))
        return obj

    def _attach_drawcalls(self, submesh, index_count=0):
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        draw_call = dcm(obj_name=submesh.unique_str)
        draw_call.index_count = index_count
        submesh.drawcall_model_list = [draw_call]
        return submesh

    def _make_exporter(self, models, components):
        exporter = _make_exporter(models, merged_vgmap=True)
        exporter.merged_skeleton_components = components
        exporter.merged_skeleton_component_id_dict = {
            c["draw_ib"]: i for i, c in enumerate(components)
        }
        return exporter

    def _group3_components(self):
        """组 3 实测（按 vg_offset 升序 = _collect_merged_skeleton_components 排序）：
        a23aa8a3(draw 20) b20f90ea(draw 2) b30db54e(draw 8)。"""
        return [
            {
                "draw_ib": "a23aa8a3", "unique_str": "LOD0.a23aa8a3-42759-0",
                "vg_offset": 79, "vg_count": 105, "skeleton_group": 3,
                "vg_map": {i: 79 + i for i in range(105)}, "deform_draw": 20,
            },
            {
                "draw_ib": "b20f90ea", "unique_str": "LOD0.b20f90ea-19182-0",
                "vg_offset": 184, "vg_count": 51, "skeleton_group": 3,
                "vg_map": {i: 184 + i for i in range(51)}, "deform_draw": 2,
            },
            {
                "draw_ib": "b30db54e", "unique_str": "LOD0.b30db54e-7383-0",
                "vg_offset": 235, "vg_count": 14, "skeleton_group": 3,
                "vg_map": {i: 235 + i for i in range(14)}, "deform_draw": 8,
            },
        ]

    def _build_and_apply_plan(self, exporter):
        """构建重定向计划并写回 exporter 字段（模拟 _export_impl 的接线）。"""
        carrier_map, target_map, unredirected = exporter._build_merged_mesh_redirect_plan()
        exporter._redirect_carrier_map = carrier_map
        exporter._redirect_target_map = target_map
        return carrier_map, target_map, unredirected

    def _group3_exporter(self, merged_vertex_count=18776, target_real_vertices=0,
                         target_registered=False, cross_ib=()):
        """构造用户实测场景（合并网格挂最早 draw 的 b20f90ea）的 exporter。"""
        self._register_obj("LOD0.b20f90ea-19182-0", [79, 88, 105, 229, 248])
        if target_registered:
            self._register_obj("LOD0.a23aa8a3-42759-0", [79, 80])
            target_exported_vertices = target_real_vertices
        else:
            target_stub = self._register_obj("LOD0.a23aa8a3-42759-0", [79, 79, 79])
            target_stub["ZZMI_STUB"] = 1
            target_exported_vertices = 3
        sub_b = self._attach_drawcalls(
            _FakeSubmesh(
                "LOD0.b20f90ea-19182-0", 184, 51,
                vertex_count=31015, original_vertex_count=4643,
                exported_vertex_count=merged_vertex_count,
            ),
            index_count=69612,
        )
        sub_a = self._attach_drawcalls(
            _FakeSubmesh(
                "LOD0.a23aa8a3-42759-0", 79, 105,
                exported_vertex_count=target_exported_vertices,
            )
        )
        sub_c = self._attach_drawcalls(
            _FakeSubmesh("LOD0.b30db54e-7383-0", 235, 14)
        )
        models = [
            _FakeDrawIBModel("b20f90ea", [sub_b]),
            _FakeDrawIBModel("a23aa8a3", [sub_a]),
            _FakeDrawIBModel("b30db54e", [sub_c]),
        ]
        exporter = self._make_exporter(models, self._group3_components())
        if cross_ib:
            exporter.cross_ib_info_dict = dict(cross_ib)
        return exporter, models

    def _capture_stdout(self, fn):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn()
        return buf.getvalue()

    def test_early_carrier_auto_redirects_to_last_pass(self):
        """用户实测场景：合并网格挂 b20f90ea（draw 2，最早）-> 自动重定向到
        a23aa8a3（draw 20，最后）；target 的 3 个 stub 顶点必须先写入 SO。"""
        exporter, _models = self._group3_exporter()
        carrier_map, target_map, unredirected = self._build_and_apply_plan(exporter)

        self.assertEqual(carrier_map["b20f90ea"]["target"], "a23aa8a3")
        self.assertEqual(carrier_map["b20f90ea"]["base_vertex"], 3)
        self.assertEqual(carrier_map["b20f90ea"]["vertex_count"], 18776)
        self.assertEqual(carrier_map["b20f90ea"]["target_first_index"], 0)
        self.assertEqual(target_map["a23aa8a3"]["so_vertex_count"], 3 + 18776)
        self.assertEqual(target_map["a23aa8a3"]["target_own_vertices"], 3)
        self.assertEqual(target_map["a23aa8a3"]["deform_draws"],
                         [
                             ("Resourceb20f90eaPosition", "Resourceb20f90eaBlend", 18776),
                         ])
        self.assertFalse(target_map["a23aa8a3"]["target_has_real_geometry"])
        self.assertEqual(target_map["a23aa8a3"]["so_owner_ib"], "b20f90ea")
        self.assertEqual(unredirected, {})
        # 已自动重定向 -> 不再报警
        out = self._capture_stdout(lambda: exporter._warn_merged_mesh_timing(unredirected))
        self.assertEqual(out, "")

    def test_merged_on_last_pass_no_redirect(self):
        """合并网格已挂在组内最后一个 deform draw（a23aa8a3，draw 20）：无需重定向。"""
        self._register_obj("LOD0.a23aa8a3-42759-0", [79, 88, 105, 188, 229])
        sub_a = self._attach_drawcalls(_FakeSubmesh("LOD0.a23aa8a3-42759-0", 79, 105))
        sub_b = self._attach_drawcalls(_FakeSubmesh("LOD0.b20f90ea-19182-0", 184, 51))
        sub_c = self._attach_drawcalls(_FakeSubmesh("LOD0.b30db54e-7383-0", 235, 14))
        models = [
            _FakeDrawIBModel("a23aa8a3", [sub_a]),
            _FakeDrawIBModel("b20f90ea", [sub_b]),
            _FakeDrawIBModel("b30db54e", [sub_c]),
        ]
        exporter = self._make_exporter(models, self._group3_components())
        carrier_map, target_map, unredirected = self._build_and_apply_plan(exporter)
        self.assertEqual(carrier_map, {})
        self.assertEqual(target_map, {})
        self.assertEqual(unredirected, {})

    def test_own_component_only_no_redirect(self):
        """未合并（只引用自己 vg_map 值集合内的骨骼，含共享 canonical）不重定向。"""
        self._register_obj("LOD0.b20f90ea-19182-0", [184, 185, 186])
        sub_b = self._attach_drawcalls(_FakeSubmesh("LOD0.b20f90ea-19182-0", 184, 51))
        sub_a = self._attach_drawcalls(_FakeSubmesh("LOD0.a23aa8a3-42759-0", 79, 105))
        sub_c = self._attach_drawcalls(_FakeSubmesh("LOD0.b30db54e-7383-0", 235, 14))
        models = [
            _FakeDrawIBModel("b20f90ea", [sub_b]),
            _FakeDrawIBModel("a23aa8a3", [sub_a]),
            _FakeDrawIBModel("b30db54e", [sub_c]),
        ]
        exporter = self._make_exporter(models, self._group3_components())
        carrier_map, target_map, unredirected = self._build_and_apply_plan(exporter)
        self.assertEqual(carrier_map, {})
        self.assertEqual(target_map, {})
        self.assertEqual(unredirected, {})

    def test_missing_deform_draw_not_redirected(self):
        """反查缓存缺 DeformDrawIndex：无法重定向 -> unredirected 报警。"""
        self._register_obj("LOD0.b20f90ea-19182-0", [79, 105, 229])
        sub_b = self._attach_drawcalls(_FakeSubmesh("LOD0.b20f90ea-19182-0", 184, 51))
        sub_a = self._attach_drawcalls(_FakeSubmesh("LOD0.a23aa8a3-42759-0", 79, 105))
        components = [
            {**c, "deform_draw": 0} for c in self._group3_components()
        ]
        models = [
            _FakeDrawIBModel("b20f90ea", [sub_b]),
            _FakeDrawIBModel("a23aa8a3", [sub_a]),
        ]
        exporter = self._make_exporter(models, components)
        carrier_map, target_map, unredirected = self._build_and_apply_plan(exporter)
        self.assertEqual(carrier_map, {})
        self.assertEqual(target_map, {})
        self.assertEqual(unredirected["b20f90ea"]["reason"], "missing-deform-draw")
        out = self._capture_stdout(lambda: exporter._warn_merged_mesh_timing(unredirected))
        self.assertIn("无法自动修复", out)
        self.assertIn("骨骼合并反查", out)

    def test_cross_ib_carrier_not_redirected(self):
        """跨 IB 配置与自动重定向暂不兼容 -> unredirected 报警。"""
        exporter, _models = self._group3_exporter(
            cross_ib={("b20f90ea_0",): ["a23aa8a3_0"]}
        )
        # cross_ib_info_dict 键是 ib_key（hash_firstindex），这里直接标记 DrawIB 为源
        exporter.cross_ib_info_dict = {"b20f90ea_0": ["a23aa8a3_0"]}
        carrier_map, _target_map, unredirected = self._build_and_apply_plan(exporter)
        self.assertEqual(carrier_map, {})
        self.assertEqual(unredirected["b20f90ea"]["reason"], "cross-ib")

    def test_redirect_target_with_own_geometry_offsets(self):
        """target（a23aa8a3）自身还有真实几何：合并网格 base_vertex = 其 SO 偏移。"""
        exporter, _models = self._group3_exporter(
            merged_vertex_count=18776, target_real_vertices=12314,
            target_registered=True,
        )
        carrier_map, target_map, unredirected = self._build_and_apply_plan(exporter)
        self.assertEqual(carrier_map["b20f90ea"]["base_vertex"], 12314)
        self.assertEqual(target_map["a23aa8a3"]["so_vertex_count"], 12314 + 18776)
        self.assertEqual(target_map["a23aa8a3"]["target_own_vertices"], 12314)
        self.assertEqual(unredirected, {})

    def test_redirect_vb_sections(self):
        """carrier 的 deform 保留 3 顶点 stub，并在依赖 palette 齐全时承担
        合并网格 draw；target 也保留同一 guarded draw 作为顺序兜底，运行时只
        由首个满足依赖的挂点绘制一次。"""
        exporter, models = self._group3_exporter()
        self._build_and_apply_plan(exporter)

        builder_b = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder_b, models[0])
        text_b = "\n".join(builder_b.sections[0].SectionLineList)
        # carrier（b20f90ea，组件 C1）：copy + attach + draw 3 + guarded merged draw
        self.assertIn("ResourceZZPalette_b20f90ea = copy vs-t0 unless_null", text_b)
        self.assertIn("run = CustomShaderZZMIMergedSkeletonAttach_C1", text_b)
        self.assertIn("draw = 3, 0", text_b)
        self.assertIn("draw = 18776, 0", text_b)
        self.assertIn("$zz_ms_redirect_drawn_a23aa8a3 == 0", text_b)

        builder_a = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder_a, models[1])
        text_a = "\n".join(builder_a.sections[0].SectionLineList)
        # target（a23aa8a3）：attach C0；纯占位 target 不再捕获/重放自己的 3 顶点
        self.assertIn("run = CustomShaderZZMIMergedSkeletonAttach_C0", text_a)
        self.assertIn("vb2 = Resourceb20f90eaBlend", text_a)
        self.assertIn("vb0 = Resourceb20f90eaPosition", text_a)
        self.assertIn("draw = 18776, 0", text_a)
        self.assertIn("so0 = ref ResourceZZRedirectSO_a23aa8a3", text_a)

        # 未参与重定向的 b30db54e 保持原样
        builder_c = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder_c, models[2])
        text_c = "\n".join(builder_c.sections[0].SectionLineList)
        self.assertIn("run = CustomShaderZZMIMergedSkeletonAttach_C2", text_c)
        self.assertIn("draw = 4643, 0", text_c)
        # b30 本身保持原 draw；它不是本计划的兼容挂点，不应执行重放。
        self.assertNotIn("Resourcea23aa8a3Position", text_c)
        self.assertIn("so0 = ref ResourceZZRedirectSO_a23aa8a3", text_c)

    def test_redirect_draw_waits_for_dependencies_in_both_frame_orders(self):
        """回归 2026-08-26 实测：target 可能在 carrier 前或后到达；两种
        顺序都只能在最后一个依赖 palette attach 后绘制，不能读半成品骨架。"""
        exporter, _models = self._group3_exporter()
        _carrier_map, target_map, _unredirected = self._build_and_apply_plan(exporter)
        required = set(target_map["a23aa8a3"]["required_component_ids"])

        def first_ready_draw(draw_ib_order):
            seen = set()
            for draw_ib in draw_ib_order:
                seen.add(exporter.merged_skeleton_component_id_dict[draw_ib])
                if required <= seen:
                    return draw_ib
            return None

        # target 后到：在 target 挂点绘制；target 先到：延后到最后一个 carrier。
        self.assertEqual(
            first_ready_draw(["b20f90ea", "b30db54e", "a23aa8a3"]),
            "a23aa8a3",
        )
        self.assertEqual(
            first_ready_draw(["a23aa8a3", "b30db54e", "b20f90ea"]),
            "b20f90ea",
        )

    def test_redirect_dependencies_omit_stub_target_when_not_referenced(self):
        """纯占位 target 且 carrier 未引用其骨骼时，不应阻塞兼容 carrier。"""
        exporter, _models = self._group3_exporter()
        # b20f90ea 的合并几何改为引用自身 + b30db54e，故旧实现不会把
        # a23aa8a3(target) 加入 required_component_ids。
        self._register_obj("LOD0.b20f90ea-19182-0", [184, 235])
        _carrier_map, target_map, _unredirected = self._build_and_apply_plan(exporter)
        required = set(target_map["a23aa8a3"]["required_component_ids"])
        target_component_id = exporter.merged_skeleton_component_id_dict["a23aa8a3"]
        self.assertNotIn(target_component_id, required)

    def test_redirect_does_not_use_incompatible_stub_target_as_host(self):
        """BI4 的占位 target 即使依赖齐全，也不能执行 BI16 carrier 重放。"""
        exporter, models = self._group3_exporter()
        models[1].d3d11GameType.CategoryStrideDict["Blend"] = 4
        _carrier_map, target_map, _unredirected = self._build_and_apply_plan(exporter)

        target_component_id = exporter.merged_skeleton_component_id_dict["a23aa8a3"]
        carrier_component_id = exporter.merged_skeleton_component_id_dict["b20f90ea"]
        self.assertNotIn(target_component_id, target_map["a23aa8a3"]["compatible_component_ids"])
        self.assertIn(carrier_component_id, target_map["a23aa8a3"]["compatible_component_ids"])

        builder_target = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder_target, models[1])
        text_target = "\n".join(builder_target.sections[0].SectionLineList)
        self.assertNotIn("ResourceZZRedirectSO_a23aa8a3 = ref so0", text_target)
        self.assertNotIn("$zz_ms_redirect_drawn_a23aa8a3 == 0", text_target)

        builder_carrier = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder_carrier, models[0])
        text_carrier = "\n".join(builder_carrier.sections[0].SectionLineList)
        self.assertIn("ResourceZZRedirectSO_a23aa8a3 = ref so0", text_carrier)
        self.assertIn("draw = 18776, 0", text_carrier)

    def test_real_target_with_incompatible_blend_layout_is_not_redirected(self):
        """真实 target 与 carrier 的 Blend 布局不同，不能把整段重放伪装成兼容。"""
        exporter, models = self._group3_exporter(
            target_real_vertices=12314,
            target_registered=True,
        )
        models[1].d3d11GameType.CategoryStrideDict["Blend"] = 4

        carrier_map, target_map, unredirected = self._build_and_apply_plan(exporter)

        self.assertEqual(carrier_map, {})
        self.assertEqual(target_map, {})
        self.assertEqual(
            unredirected["b20f90ea"]["reason"],
            "incompatible-blend-layout",
        )
        warning = self._capture_stdout(lambda: exporter._warn_merged_mesh_timing(unredirected))
        self.assertIn("Blend 输入布局不兼容", warning)

    def test_missing_blend_layout_is_not_assumed_compatible(self):
        """布局元数据缺失时必须显式拒绝，不能让换角色后的未知格式静默重放。"""
        exporter, models = self._group3_exporter()
        models[0].d3d11GameType.CategoryStrideDict.pop("Blend")

        carrier_map, target_map, unredirected = self._build_and_apply_plan(exporter)

        self.assertEqual(carrier_map, {})
        self.assertEqual(target_map, {})
        self.assertEqual(
            unredirected["b20f90ea"]["reason"],
            "missing-blend-layout",
        )
        warning = self._capture_stdout(lambda: exporter._warn_merged_mesh_timing(unredirected))
        self.assertIn("缺少可验证的 Blend 输入布局", warning)

    def test_redirect_ib_sections(self):
        """carrier/target 各自保留 render 身份；carrier 只换绑合并 SO，target
        的占位 IB 仍然输出，避免共享 hash 导致物体串扰或被静默跳过。"""
        exporter, models = self._group3_exporter()
        self._build_and_apply_plan(exporter)

        builder = _FakeIniBuilder()
        for model in models:
            exporter.add_unity_vs_texture_override_ib_sections(builder, model)
        text = "\n".join(
            line for section in builder.sections for line in section.SectionLineList
        )

        # carrier 的 render override：hash/first_index 仍是 b20f90ea，顶点显式
        # 读取 target 的 RedirectSO，索引和 mesh 备注仍属于 carrier。
        self.assertIn("[TextureOverride_LOD0.b20f90ea_19182_0]", text)
        self.assertIn("hash = b20f90ea", text)
        self.assertIn("vb0 = ResourceZZRedirectSO_a23aa8a3", text)
        self.assertIn("ib = Resource_LOD0.b20f90ea_19182_0_Index", text)
        self.assertIn("vb1 = ResourceZZRedirectTexcoord_a23aa8a3_b20f90ea_3", text)
        self.assertIn("drawindexed = 69612,0,3", text)
        self.assertIn("; [mesh:LOD0.b20f90ea-19182-0]", text)
        # carrier 的原 render draw 被 IB 级 skip 抑制
        self.assertIn("[TextureOverride_IB_b20f90ea]", text)
        # target 的 stub 子网格保留自己的 hash/IB；占位三角由导出阶段写入。
        self.assertIn("[TextureOverride_LOD0.a23aa8a3_42759_0]", text)
        self.assertIn("hash = a23aa8a3", text)
        self.assertIn("ib = Resource_LOD0.a23aa8a3_42759_0_Index", text)
        self.assertNotIn("ib = null", text)

    def test_redirect_texcoord_payload_matches_so_base_vertex(self):
        """carrier 的 UV 前缀必须与 RedirectSO 的 base_vertex 完全相同。"""
        submesh = self._attach_drawcalls(
            _FakeSubmesh("LOD0.b20f90ea-19182-0", 184, 51, exported_vertex_count=5)
        )
        model = _FakeDrawIBModel("b20f90ea", [submesh])
        source_bytes = bytes(range(5 * 20))
        model.category_buffer_dict["Texcoord"] = source_bytes
        exporter = self._make_exporter([model], self._group3_components())

        payload, stride = exporter._build_redirect_texcoord_payload(
            "b20f90ea",
            {"target": "a23aa8a3", "base_vertex": 3, "vertex_count": 5},
        )

        self.assertEqual(stride, 20)
        self.assertEqual(payload[: 3 * stride], b"\x00" * (3 * stride))
        self.assertEqual(payload[3 * stride :], source_bytes)

    def test_redirect_texcoord_resource_is_declared_and_written(self):
        submesh = self._attach_drawcalls(
            _FakeSubmesh("LOD0.b20f90ea-19182-0", 184, 51, exported_vertex_count=5)
        )
        model = _FakeDrawIBModel("b20f90ea", [submesh])
        source_bytes = bytes(range(5 * 20))
        model.category_buffer_dict["Texcoord"] = source_bytes
        exporter = self._make_exporter([model], self._group3_components())
        exporter._redirect_carrier_map = {
            "b20f90ea": {
                "target": "a23aa8a3",
                "base_vertex": 3,
                "vertex_count": 5,
            }
        }
        exporter._redirect_target_map = {
            "a23aa8a3": {"so_stride": 40}
        }

        builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(builder)
        text = "\n".join(
            line for section in builder.sections for line in section.SectionLineList
        )
        filename = "zz_redirect_texcoord_a23aa8a3_b20f90ea_3.buf"
        self.assertIn(
            "[ResourceZZRedirectTexcoord_a23aa8a3_b20f90ea_3]", text
        )
        self.assertIn("stride = 20", text)
        self.assertIn(f"filename = Meshes/{filename}", text)
        payload = (Path(_FAKE_MOD_FOLDER) / "Meshes" / filename).read_bytes()
        self.assertEqual(payload, (b"\x00" * (3 * 20)) + source_bytes)

    def test_redirect_keeps_each_submesh_first_index(self):
        """同一 DrawIB 的多个子网格不能共用 target 首索引，否则会再次串台。"""
        exporter, models = self._group3_exporter()
        second_target = self._attach_drawcalls(
            _FakeSubmesh(
                "LOD0.a23aa8a3-288-42759",
                79,
                105,
                match_first_index=42759,
            )
        )
        models[1].submesh_model_list.append(second_target)
        models[1].submesh_ib_dict[second_target.unique_str] = b"\x00\x00\x00\x00"
        self._build_and_apply_plan(exporter)

        builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_ib_sections(builder, models[1])
        text = "\n".join(builder.sections[0].SectionLineList)
        self.assertIn("[TextureOverride_LOD0.a23aa8a3_288_42759]", text)
        self.assertIn("hash = a23aa8a3\nmatch_first_index = 42759", text)
        self.assertIn("ib = Resource_LOD0.a23aa8a3_288_42759_Index", text)

    def test_merged_skeleton_refuses_empty_index_buffer(self):
        """合并骨架下不能退回 ib=null；缺失占位索引必须让导出显式失败。"""
        exporter, models = self._group3_exporter()
        exporter.has_merged_skeleton = True
        models[1].submesh_ib_dict["LOD0.a23aa8a3-42759-0"] = b""

        with self.assertRaisesRegex(RuntimeError, "禁止以 ib=null/IB skip"):
            exporter.add_unity_vs_texture_override_ib_sections(
                _FakeIniBuilder(), models[1]
            )

    def test_redirect_vlr_section(self):
        """VertexLimitRaise：纯占位 target 的 SO 由 carrier 拥有并声明总容量。"""
        exporter, models = self._group3_exporter()
        self._build_and_apply_plan(exporter)

        builder_b = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vlr_section(builder_b, models[0])
        text_b = "\n".join(builder_b.sections[0].SectionLineList)
        self.assertIn("override_vertex_count = 18779", text_b)

        builder_a = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vlr_section(builder_a, models[1])
        text_a = "\n".join(builder_a.sections[0].SectionLineList)
        self.assertIn("override_vertex_count = 18779", text_a)


class ZZSIMergedMeshRenderRebindTests(unittest.TestCase):
    """合并网格渲染换绑：导出顶点数超过原部件顶点数时，渲染 draw 必须把 vb1
    换绑为本 mod 的 Texcoord buffer（游戏原 vb1 只覆盖原部件顶点数，合并网格
    索引会越界读 -> UV 糊到 (0,0) 角落）。"""

    def _render_override_text(self, submesh, draw_ib="b20f90ea"):
        model = _FakeDrawIBModel(draw_ib, [submesh])
        exporter = _make_exporter([model], merged_vgmap=True)
        builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_ib_sections(builder, model)
        return "\n".join(builder.sections[0].SectionLineList)

    def test_oversized_mesh_binds_vb1(self):
        submesh = _FakeSubmesh(
            "LOD0.b20f90ea-19182-0", 184, 51,
            vertex_count=31015, original_vertex_count=4643,
        )
        text = self._render_override_text(submesh)
        self.assertIn("ib = Resource_LOD0.b20f90ea_19182_0_Index", text)
        self.assertIn("vb1 = Resourceb20f90eaTexcoord", text)
        self.assertLess(
            text.index("ib = "), text.index("vb1 = Resourceb20f90eaTexcoord")
        )

    def test_same_size_mesh_keeps_game_vb1(self):
        submesh = _FakeSubmesh(
            "LOD0.b20f90ea-19182-0", 184, 51,
            vertex_count=4643, original_vertex_count=4643,
        )
        text = self._render_override_text(submesh)
        self.assertNotIn("vb1 = Resource", text)

    def test_stub_smaller_than_original_keeps_game_vb1(self):
        submesh = _FakeSubmesh(
            "LOD0.a23aa8a3-42759-0", 79, 105,
            vertex_count=3, original_vertex_count=12314,
        )
        text = self._render_override_text(submesh, draw_ib="a23aa8a3")
        self.assertNotIn("vb1 = Resource", text)


if __name__ == "__main__":
    unittest.main()
