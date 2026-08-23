"""ExportZZMI 合并骨架 INI 生成单测（fake 环境，不依赖 bpy/游戏）。"""

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
    def __init__(self, _section_type):
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
    TextureOverrideIB = "TextureOverrideIB"
    TextureOverrideVB = "TextureOverrideVB"
    ResourceBuffer = "ResourceBuffer"
    MergedSkeleton = "MergedSkeleton"


class _FakeExportUnity:
    def __init__(self, blueprint_model):
        self.blueprint_model = blueprint_model
        self.drawib_model_list = []

    def add_unity_vs_resource_vb_sections(self, ini_builder, drawib_model):
        pass


_fake_global_properties = types.SimpleNamespace(import_merged_vgmap=lambda: True)
_fake_global_config = types.SimpleNamespace(
    path_generatemod_buffer_folder=lambda: "",
    path_generate_mod_folder=lambda: "",
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
    M_IniHelper=types.SimpleNamespace(),
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
    def __init__(self, unique_str, vg_offset=0, vg_count=0, skeleton_group=0, cb1_source_ib=""):
        self.unique_str = unique_str
        self.match_first_index = 0
        self.vg_offset = vg_offset
        self.vg_count = vg_count
        self.skeleton_group = skeleton_group
        self.skeleton_group_cb1_source_ib = cb1_source_ib
        self.drawcall_model_list = []


class _FakeDrawIBModel:
    def __init__(self, draw_ib, submesh_model_list, part_map=None):
        self.draw_ib = draw_ib
        self.draw_ib_alias = draw_ib
        self.draw_number = 4643
        self.d3d11GameType = _FakeGameType()
        self.category_hash_dict = {
            "Position": "122883aa",
            "Texcoord": "5c0fefda",
            "Blend": "bf543990",
        }
        self.submesh_model_list = submesh_model_list
        self.match_first_index_partname_dict = part_map or {}


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
        lines = builder.sections[0].SectionLineList
        return lines

    def test_vb_section_injects_copy_and_swap(self):
        lines = self._build_vb_section(with_merged=True)
        text = "\n".join(lines)

        # palette 持久副本在 draw 之前；换绑本组骨架；attach 不在 VB 段（挪到 [Present]）
        idx_copy = text.index("ResourceZZPalette_b20f90ea = copy vs-t0 unless_null")
        idx_swap = text.index("vs-t0 = ResourceZZMergedSkeleton_G0")
        idx_draw = text.index("draw = 4643, 0")
        self.assertLess(idx_copy, idx_swap)
        self.assertLess(idx_swap, idx_draw)
        # VB 段不再做 attach（帧对齐：attach 统一在 [Present] 帧尾执行）
        self.assertNotIn("run = CustomShaderZZMIMergedSkeletonAttach", text)
        self.assertNotIn("cs-t0", text)
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
        text = "\n".join(builder.sections[0].SectionLineList)

        self.assertIn("global $zz_ms_initialized = 0", text)
        self.assertIn("[ResourceZZMergedSkeleton_G0]", text)
        self.assertIn("type = RWStructuredBuffer", text)
        self.assertIn("stride = 48", text)
        self.assertIn("array = 156", text)  # 全宽 = max(0+105, 105+51) = 156
        self.assertIn("[ResourceZZCb1_G0]", text)
        # 无捕获源（fake 未设 cb1_source_ib）：不出捕获段，attach 段不带 cb1 引用（CS 直拷兜底）
        self.assertNotIn("Cb1Capture", text)
        # palette 持久副本资源（Present 时序 attach 的输入）
        self.assertIn("[ResourceZZPalette_a23aa8a3]", text)
        self.assertIn("[ResourceZZPalette_b20f90ea]", text)
        self.assertIn("[CustomShaderZZMIMergedSkeletonAttach_C0_G0]", text)
        self.assertIn("[CustomShaderZZMIMergedSkeletonAttach_C1_G0]", text)
        self.assertIn("cs-t0 = ref ResourceZZPalette_a23aa8a3", text)
        self.assertIn("cs-t0 = ref ResourceZZPalette_b20f90ea", text)
        self.assertIn("cs = ./res/zzmi_merged_skeleton_attach_calibrated.hlsl", text)
        self.assertIn("x1 = $zz_ms_attach_offset", text)
        self.assertIn("y1 = $zz_ms_attach_count", text)
        self.assertIn("cs-u0 = ref ResourceZZMergedSkeleton_G0", text)
        self.assertIn("Dispatch = 8, 1, 1", text)
        self.assertNotIn("cs-t1 = ref", text)
        self.assertNotIn("cs-t2 = ref", text)
        # attach 运行在 [Present]（帧尾统一对齐），且逐 run 前带本部件 offset/count
        self.assertIn("[Present]", text)
        present_text = text.split("[Present]")[1]
        idx_offset = present_text.index("$zz_ms_attach_offset = 0")
        idx_count = present_text.index("$zz_ms_attach_count = 105")
        idx_run = present_text.index("run = CustomShaderZZMIMergedSkeletonAttach_C0_G0")
        self.assertLess(idx_offset, idx_count)
        self.assertLess(idx_count, idx_run)
        self.assertIn("run = CustomShaderZZMIMergedSkeletonAttach_C1_G0", present_text)
    def test_merged_skeleton_sections_per_group(self):
        """校准版：每组一套全宽骨架资源；逐（部件 × 组）attach；有捕获源的组出捕获段。"""
        # 组 0（身体）：a23aa8a3(0,105) + b20f90ea(105,51)，捕获源 = a23aa8a3
        # 组 1（头部）：64d7d56f(156,1) + b51bdd59(157,11)，捕获源 = 48625d6d（本例缺席）
        exporter = _make_exporter(
            [
                _FakeDrawIBModel("a23aa8a3", [_FakeSubmesh("LOD0.a23aa8a3-42759-0", 0, 105, 0, "a23aa8a3")]),
                _FakeDrawIBModel("b20f90ea", [_FakeSubmesh("LOD0.b20f90ea-19182-0", 105, 51, 0, "a23aa8a3")]),
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
        text = "\n".join(builder.sections[0].SectionLineList)

        self.assertIn("[ResourceZZMergedSkeleton_G0]", text)
        self.assertIn("[ResourceZZMergedSkeleton_G1]", text)
        # 两组全宽 array = 全局 max(157+11) = 168（只数骨架资源的 array 行；
        # palette 副本资源自带 array=vg_count 行，需排除——骨架段结构：header/type/stride/array）
        section_lines = builder.sections[0].SectionLineList
        skeleton_arrays = [
            section_lines[i + 3]
            for i, line in enumerate(section_lines)
            if line.startswith("[ResourceZZMergedSkeleton_G")
        ]
        self.assertEqual(skeleton_arrays, ["array = 168", "array = 168"])
        # 组 0 有捕获源（a23aa8a3）-> 捕获段 + attach 带 cs-t1/cs-t2；组 1 无 -> 无捕获段
        self.assertIn("[TextureOverrideIB_a23aa8a3_Cb1Capture_G0]", text)
        self.assertIn("hash = a23aa8a3", text)
        self.assertIn("match_instance_count = 0", text)
        self.assertIn("ResourceZZCb1_G0 = copy vs-cb1 unless_null", text)
        self.assertNotIn("Cb1Capture_G1", text)
        # 逐（部件 × 组）attach：4 部件 × 2 组 = 8 段
        for cid in range(4):
            for g in (0, 1):
                self.assertIn(f"[CustomShaderZZMIMergedSkeletonAttach_C{cid}_G{g}]", text)
        # 组 0 的 attach：cs-cb1（本组 cb1）在 C0/C1（组 0 部件）上，cs-cb2（目标组 cb1）在 G0 目标上
        c0_g0 = text.split("[CustomShaderZZMIMergedSkeletonAttach_C0_G0]")[1].split("[")[0]
        self.assertIn("cs-cb1 = ref ResourceZZCb1_G0", c0_g0)   # C0 属组 0（资源名 ZZCb1，槽 cb1）
        self.assertIn("cs-cb2 = ref ResourceZZCb1_G0", c0_g0)   # 目标组 0（资源名 ZZCb1，槽 cb2）
        c2_g0 = text.split("[CustomShaderZZMIMergedSkeletonAttach_C2_G0]")[1].split("[")[0]
        self.assertNotIn("cs-cb1 = ref", c2_g0)                # C2 属组 1（无捕获源）
        self.assertNotIn("cs-t1 = ref", c2_g0)                 # 不得错绑到 SRV 槽

    def test_vb_section_rebinds_to_own_group_resource(self):
        """每个 deform VB 段 copy 当帧 palette 成持久副本并换绑本组骨架（attach 在 [Present]）。"""
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
        self.assertIn("vs-t0 = ResourceZZMergedSkeleton_G0", text0)
        self.assertNotIn("run = CustomShaderZZMIMergedSkeletonAttach", text0)

        builder1 = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder1, model_g1)
        text1 = "\n".join(builder1.sections[0].SectionLineList)
        self.assertIn("ResourceZZPalette_64d7d56f = copy vs-t0 unless_null", text1)
        self.assertIn("vs-t0 = ResourceZZMergedSkeleton_G1", text1)

        # 逐（部件 × 组）attach 运行在 [Present]：2 部件 × 2 组 = 4 次
        builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(builder)
        present_text = "\n".join(builder.sections[0].SectionLineList).split("[Present]")[1]
        for cid, draw_ib in ((0, "a23aa8a3"), (1, "64d7d56f")):
            for g in (0, 1):
                self.assertIn(f"run = CustomShaderZZMIMergedSkeletonAttach_C{cid}_G{g}", present_text)

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
        text = "\n".join(builder.sections[0].SectionLineList)

        self.assertIn("array = 51", text)  # max(0+11, 31+20) = 51，而非 sum=31


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
        # 极限小三角面
        verts, _edges, faces = stub.data.from_pydata_calls[0]
        self.assertEqual(len(verts), 3)
        self.assertEqual(faces, [(0, 1, 2)])
        self.assertLess(max(abs(c) for v in verts for c in v), 1e-3)

        exporter._cleanup_stub_objects()
        self.assertIsNone(_fake_bpy_data.objects.get("LOD0.84618ee0-1164-22296"))
        self.assertEqual(exporter._zzmi_stub_object_names, [])

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

    def _register_present_object_with_groups(self, name, used_gids):
        mesh = _fake_bpy_data.meshes.new(name=name + "_mesh")
        mesh.vertices = [
            types.SimpleNamespace(groups=[types.SimpleNamespace(group=gid, weight=1.0)])
            for gid in used_gids
        ]
        return _fake_bpy_data.objects.new(name=name, object_data=mesh)

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


if __name__ == "__main__":
    unittest.main()
