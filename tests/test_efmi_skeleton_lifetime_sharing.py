"""EFMI 骨骼合并安全性修复回归测试（t1 契约 §1 的 T1/T2 规格）。

覆盖三条不变量：
- I1 去重生命周期域：跨 LOD 双现（same-IB）部件与 L0-only 组件**不得**共享
  运行时槽位（R1：LOD1 距离下 L0-only 组件不绘制 → 槽无人写入 → identity 冻结）。
- I2 导出可达性守卫：每个存活部件引用的槽要么落在自己声明段，要么其维护
  组件在该部件出现的所有 LOD 距离下必绘制；违反 → 明确拒绝导出。
- I3 折叠蒙皮兼容性：同 IB 折叠不得静默接受 L1 骨骼语义与基准不一致的部件
  （跨 LOD 对应账本 matrix_diff 超阈值 → 拒绝折叠，R2）。

修改前断言失败（旧代码无约束/无守卫/无 matrix_diff 检查），修改后通过；
同域组件合并、单 LOD 去重、出口可达引用等“不应回退”用例保持绿色。
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path

import numpy

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "efmi_lifetime_sharing_test_pkg"


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


def _install_package(name):
    module = types.ModuleType(name)
    module.__path__ = []
    sys.modules[name] = module
    return module


# ---- common/efmi_skeleton.py 侧的 fake 环境（无 bpy 依赖） ----
for _name in (PKG, f"{PKG}.common", f"{PKG}.utils"):
    _install_package(_name)
_load_real(f"{PKG}.utils.json_utils", "utils/json_utils.py")
# efmi_skeleton 需要 os/系统函数真实执行，但解析器/写入器不依赖 bpy。
_efmi = _load_real(f"{PKG}.common.efmi_skeleton", "common/efmi_skeleton.py")
EFMIBoneMapBuilder = _efmi.EFMIBoneMapBuilder

# ---- ui/universal/efmi.py 侧的 fake 环境（需要假 bpy 与依赖模块） ----
for _pkg_name in (
    f"{PKG}.ui", f"{PKG}.ui.universal", f"{PKG}.blueprint",
    f"{PKG}.common", f"{PKG}.utils",
):
    _install_package(_pkg_name)
_install_module("bpy", data=types.SimpleNamespace())
_install_module(
    f"{PKG}.utils.json_utils",
    JsonUtils=_load_real(f"{PKG}.utils.json_utils_", "utils/json_utils.py").JsonUtils,
)

# 注意：efmi_skeleton 已用同一个 PKG 前缀加载过；efmi.py 也通过同一前缀
# 解析 common.efmi_skeleton（如导入），因此这里重新注册同名模块指向真实文件。
if f"{PKG}.common.efmi_skeleton" not in sys.modules:
    _load_real(f"{PKG}.common.efmi_skeleton", "common/efmi_skeleton.py")

_install_module(
    f"{PKG}.utils.timer_utils",
    TimerUtils=types.SimpleNamespace(
        start_stage=lambda *_a, **_k: None, end_stage=lambda *_a, **_k: None
    ),
)
_install_module(f"{PKG}.common.global_config", GlobalConfig=types.SimpleNamespace())
_install_module(
    f"{PKG}.common.global_properties",
    GlobalProterties=types.SimpleNamespace(
        import_merged_vgmap=lambda: True, forbid_auto_texture_ini=lambda: False
    ),
)
_install_module(
    f"{PKG}.common.global_key_count_helper",
    GlobalKeyCountHelper=types.SimpleNamespace(generated_mod_number=0),
)
_real_ini_builder = _load_real(
    f"{PKG}.common.m_ini_builder", "common/m_ini_builder.py"
)
M_IniBuilder = _real_ini_builder.M_IniBuilder
M_IniSection = _real_ini_builder.M_IniSection
M_SectionType = _real_ini_builder.M_SectionType
_install_module(f"{PKG}.common.m_ini_helper", M_IniHelper=types.SimpleNamespace())
_install_module(
    f"{PKG}.common.m_ini_helper_gui", M_IniHelperGUI=types.SimpleNamespace()
)
_install_module(f"{PKG}.blueprint.model", BluePrintModel=object)
_install_module(f"{PKG}.common.submesh_model", SubMeshModel=object)
_install_module(f"{PKG}.common.drawib_model", DrawIBModel=object)
_install_module(
    f"{PKG}.blueprint.export_helper",
    BlueprintExportHelper=types.SimpleNamespace(),
)
_install_module(
    f"{PKG}.common.buffer_export_helper",
    BufferExportHelper=types.SimpleNamespace(),
)
_install_module(f"{PKG}.common.draw_call_model", DrawCallModel=object)
_install_module(
    f"{PKG}.ui.universal.export_helper", ExportHelper=types.SimpleNamespace()
)
_efmi_export = _load_real(f"{PKG}.ui.universal.efmi", "ui/universal/efmi.py")
ExportEFMI = _efmi_export.ExportEFMI


# ---- 共享的骨骼候选构造工具（与 test_efmi_skeleton_dedup 同构） ----
def _bone(tx=0.0, ty=0.0, tz=0.0):
    """12 floats 的 4x3 骨骼矩阵（单位旋转 + 平移）。"""
    return [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, tx, ty, tz]


def _sig(centroid):
    c = numpy.array(centroid, dtype=numpy.float32)
    h = numpy.array([0.05, 0.05, 0.05], dtype=numpy.float32)
    return {
        "centroid": c,
        "bbox_min": c - h,
        "bbox_max": c + h,
        "vertex_count": 10,
        "spread": 0.05,
        "weight_total": 10.0,
    }


def _entry(bones, sigs=None):
    arr = numpy.array(bones, dtype=numpy.float32).reshape(-1, 12)
    n = len(arr)
    return (arr, n, numpy.ones(n, dtype=numpy.int64), sigs or {})


# ---- 导出侧 fake 子网格（与 test_efmi_merge_active_collision 同构） ----
class _FakePreSkindSubmesh:
    def __init__(
        self, unique_str, vg_offset, vg_count, match_draw_ib="", ref_component="",
        corr=None, vg_map=None, layout_version=13,
    ):
        self.unique_str = unique_str
        self.vg_offset = vg_offset
        self.vg_count = vg_count
        self.merged_skeleton_metadata_valid = True
        self.d3d11_game_type = types.SimpleNamespace(
            GPU_PreSkinning=True,
            get_blendindices_layouts=lambda: [(0, "R16G16B16A16_UINT", "vb2")],
        )
        self.match_draw_ib = match_draw_ib
        self.match_index_count = "1"
        self.match_first_index = "0"
        self.efmi_lod_reference_component = ref_component
        self.efmi_lod_correspondence = corr or {}
        self.efmi_lod_layout_version = layout_version
        self.vg_map = vg_map or {}


class LifetimeDomainDedupTests(unittest.TestCase):
    """I1：跨组件去重仅在相同运行时更新生命周期内允许。"""

    def setUp(self):
        self._old_dedup_enabled = _efmi._DEDUP_ENABLED
        _efmi._DEDUP_ENABLED = True

    def tearDown(self):
        _efmi._DEDUP_ENABLED = self._old_dedup_enabled

    def test_component_lifetime_domains_grouped_by_same_ib(self):
        """生命周期域按跨 LOD 同 IB 归组：双现脸 = {L0,L1}，L0-only = {L0}。"""
        bone = _bone(0.0)
        collected = {
            "LOD0": {
                "LOD0.faceib-1-0": _entry([bone], {0: _sig((0.0, 0.0, 0.0))}),
                "LOD0.l0only-2-0": _entry([bone], {0: _sig((1.0, 0.0, 0.0))}),
            },
            "LOD1": {
                # 同 IB：脸部件在 LOD1 仍绘制（同一 draw）
                "LOD1.faceib-3-0": _entry([bone], {0: _sig((0.01, 0.0, 0.0))}),
                # 异 IB：l0only 组件的 L1 版本是另一个 draw，L0 版本只在 LOD0 绘制
                "LOD1.otherib-4-0": _entry([bone], {0: _sig((1.01, 0.0, 0.0))}),
            },
        }
        domains = EFMIBoneMapBuilder._component_lifetime_domains(collected)
        self.assertEqual(
            domains["LOD0"]["LOD0.faceib-1-0"], frozenset({"LOD0", "LOD1"})
        )
        self.assertEqual(
            domains["LOD0"]["LOD0.l0only-2-0"], frozenset({"LOD0"})
        )
        self.assertEqual(
            domains["LOD1"]["LOD1.faceib-3-0"], frozenset({"LOD0", "LOD1"})
        )
        self.assertEqual(
            domains["LOD1"]["LOD1.otherib-4-0"], frozenset({"LOD1"})
        )

    def test_l0_only_component_never_shares_slot_with_dual_presence_face(self):
        """R1 核心用例：位相同矩阵（旧代码必合并）也不能让 L0-only 与双现脸共享槽。"""
        bone = _bone(0.0)
        collected = {
            "LOD0": {
                "LOD0.faceib-1-0": _entry([bone], {0: _sig((0.0, 0.0, 0.0))}),
                "LOD0.l0only-2-0": _entry([bone], {0: _sig((1.0, 0.0, 0.0))}),
            },
            "LOD1": {
                "LOD1.faceib-3-0": _entry([bone], {0: _sig((0.01, 0.0, 0.0))}),
                "LOD1.otherib-4-0": _entry([bone], {0: _sig((1.01, 0.0, 0.0))}),
            },
        }
        maps, _offsets, _base = EFMIBoneMapBuilder.build_independent_lod_maps(
            collected, "LOD0"
        )
        face_slot = maps["LOD0"]["LOD0.faceib-1-0"][0]
        l0only_slot = maps["LOD0"]["LOD0.l0only-2-0"][0]
        self.assertNotEqual(
            face_slot, l0only_slot,
            "双现脸部件引用了 L0-only 组件的槽位：LOD1 距离下该槽无人写入（R1）",
        )

    def test_dual_presence_components_may_still_share_slots(self):
        """同域（双方都在 LOD0/LOD1 同现同绘）的组件仍允许去重共享槽位。"""
        bone = _bone(0.0)
        collected = {
            "LOD0": {
                "LOD0.facea-1-0": _entry([bone], {0: _sig((0.0, 0.0, 0.0))}),
                "LOD0.faceb-2-0": _entry([bone], {0: _sig((1.0, 0.0, 0.0))}),
            },
            "LOD1": {
                "LOD1.facea-3-0": _entry([bone], {0: _sig((0.01, 0.0, 0.0))}),
                "LOD1.faceb-4-0": _entry([bone], {0: _sig((1.01, 0.0, 0.0))}),
            },
        }
        maps, _offsets, _base = EFMIBoneMapBuilder.build_independent_lod_maps(
            collected, "LOD0"
        )
        self.assertEqual(
            maps["LOD0"]["LOD0.facea-1-0"][0],
            maps["LOD0"]["LOD0.faceb-2-0"][0],
            "同生命周期域的双现组件应保持既有去重语义（不得过度断边）",
        )

    def test_single_lod_components_still_deduplicate_across_components(self):
        """单 LOD（无跨 LOD 域差异）时跨组件去重保持既有行为。"""
        bone = _bone(0.0)
        collected = {
            "LOD0": {
                "LOD0.a-1-0": _entry([bone], {0: _sig((0.0, 0.0, 0.0))}),
                "LOD0.b-2-0": _entry([bone], {0: _sig((1.0, 0.0, 0.0))}),
            },
        }
        maps, _offsets, _base = EFMIBoneMapBuilder.build_independent_lod_maps(
            collected, "LOD0"
        )
        self.assertEqual(
            maps["LOD0"]["LOD0.a-1-0"][0],
            maps["LOD0"]["LOD0.b-2-0"][0],
            "单 LOD 模式（v10 语义）不得因生命周期约束回退去重",
        )

    def test_dedup_disabled_yields_identity_slots_across_lods(self):
        """deduplicate=False 时每根骨骼独占槽位（恒等映射），两 LOD 均不去重。"""
        bone = _bone(0.0)
        collected = {
            "LOD0": {
                "LOD0.facea-1-0": _entry([bone], {0: _sig((0.0, 0.0, 0.0))}),
                "LOD0.faceb-2-0": _entry([bone], {0: _sig((1.0, 0.0, 0.0))}),
            },
            "LOD1": {
                "LOD1.facea-3-0": _entry([bone], {0: _sig((0.01, 0.0, 0.0))}),
                "LOD1.faceb-4-0": _entry([bone], {0: _sig((1.01, 0.0, 0.0))}),
            },
        }
        maps, offsets, base = EFMIBoneMapBuilder.build_independent_lod_maps(
            collected, "LOD0", deduplicate=False
        )
        self.assertNotEqual(
            maps["LOD0"]["LOD0.facea-1-0"][0],
            maps["LOD0"]["LOD0.faceb-2-0"][0],
            "关闭去重后位相同矩阵也不得跨组件合并槽位",
        )
        # 每个 local 恒等映射到自己的 vg_offset + local
        for lod_name in ("LOD0", "LOD1"):
            for unique_str, part_map in maps[lod_name].items():
                offset = offsets[lod_name][unique_str]
                for local_id, global_slot in part_map.items():
                    self.assertEqual(
                        global_slot, offset + int(local_id),
                        "关闭去重时应保持 local -> vg_offset + local 恒等映射",
                    )
        # 平移基址仍非零（LOD1 段在基准段之后）
        self.assertGreater(base, 0)


class ExportSlotReachabilityTests(unittest.TestCase):
    """I2：导出前对存活 Blend 引用槽做可达性校验，违反即明确拒绝。"""

    def test_export_rejects_dangling_cross_component_slot_reference(self):
        """R1 出口守卫：双现脸（D={L0,L1}）引用 L0-only 组件（D={L0}）的槽 4 → 拒绝。"""
        exporter = object.__new__(ExportEFMI)
        exporter.submesh_model_list = [
            _FakePreSkindSubmesh(
                "LOD0.face-0", 0, 2, match_draw_ib="face",
                vg_map={"0": 0, "1": 4},  # local 1 去重进 L0-only 组件的声明段槽 4
            ),
            _FakePreSkindSubmesh(
                "LOD1.face-0", 2, 2, match_draw_ib="face",
                ref_component="LOD0.face-0",
                corr={"0": {"local_vg_id": 0}, "1": {"local_vg_id": 1}},
                vg_map={"0": 2, "1": 3},
            ),
            _FakePreSkindSubmesh(
                "LOD0.l0only-0", 4, 2, match_draw_ib="l0only",
                vg_map={"0": 4, "1": 5},
            ),
        ]
        with self.assertRaisesRegex(RuntimeError, "槽 4"):
            exporter._get_merged_skeleton_component_info()

    def test_export_allows_own_segment_and_covered_writer_slots(self):
        """可达引用不误杀：自身段槽 + 维护者在所有出现距离必绘制的槽都放行。"""
        exporter = object.__new__(ExportEFMI)
        exporter.submesh_model_list = [
            _FakePreSkindSubmesh(
                "LOD0.face-0", 0, 2, match_draw_ib="face",
                vg_map={"0": 0, "1": 1},  # 全部落在自己声明段
            ),
            _FakePreSkindSubmesh(
                "LOD1.face-0", 2, 2, match_draw_ib="face",
                ref_component="LOD0.face-0",
                corr={"0": {"local_vg_id": 0}, "1": {"local_vg_id": 1}},
                vg_map={"0": 2, "1": 3},
            ),
            # l0only 引用双现脸的槽 0：D(l0only)={L0} ⊆ D(face)={L0,L1} → 安全
            _FakePreSkindSubmesh(
                "LOD0.l0only-0", 4, 2, match_draw_ib="l0only",
                vg_map={"0": 4, "1": 0},
            ),
        ]
        parts, _id_dict = exporter._get_merged_skeleton_component_info()
        self.assertEqual(len(parts), 2)

    def test_export_single_lod_cross_component_refs_not_rejected(self):
        """单 LOD（全部同距离绘制）跨组件引用维持旧行为，不触发守卫拒绝。"""
        exporter = object.__new__(ExportEFMI)
        exporter.submesh_model_list = [
            _FakePreSkindSubmesh(
                "LOD0.a-0", 0, 2, match_draw_ib="a",
                vg_map={"0": 0, "1": 4},
            ),
            _FakePreSkindSubmesh(
                "LOD0.b-0", 4, 2, match_draw_ib="b",
                vg_map={"0": 4, "1": 0},
            ),
        ]
        parts, _id_dict = exporter._get_merged_skeleton_component_info()
        self.assertEqual(len(parts), 2)


class SameIbFoldSkinningCompatibilityTests(unittest.TestCase):
    """I3：同 IB 折叠不得静默接受 L1 骨骼语义与基准不一致的部件（R2）。"""

    def test_same_ib_fold_rejects_incompatible_l1_skinning(self):
        """跨 LOD 对应账本 matrix_diff 超阈值（佩丽卡实测 449）→ 拒绝折叠。"""
        baseline = _FakePreSkindSubmesh(
            "LOD0.face-0", 100, 2, match_draw_ib="face",
            vg_map={"0": 100, "1": 101},
        )
        lod = _FakePreSkindSubmesh(
            "LOD1.face-0", 200, 2, match_draw_ib="face",
            ref_component="LOD0.face-0",
            vg_map={"0": 200, "1": 201},
            corr={
                "0": {"local_vg_id": 0, "matrix_diff": 449.64},
                "1": {"local_vg_id": 1, "matrix_diff": 0.001},
            },
        )
        with self.assertRaisesRegex(RuntimeError, "matrix_diff|骨骼矩阵差异|蒙皮语义"):
            ExportEFMI._build_same_ib_bone_aliases(baseline, lod)

    def test_same_ib_fold_accepts_moderate_matrix_diff_with_strong_geometry_evidence(self):
        """佩丽卡 f4f4158a：几何/局部组证据近乎精确时允许 10.806 的跨帧矩阵差。"""
        baseline = _FakePreSkindSubmesh(
            "LOD0.f4f4158a-480-0", 365, 6, match_draw_ib="f4f4158a",
            vg_map={str(i): 365 + i for i in range(6)},
        )
        lod = _FakePreSkindSubmesh(
            "LOD1.f4f4158a-480-0", 734, 6, match_draw_ib="f4f4158a",
            ref_component="LOD0.f4f4158a-480-0",
            vg_map={str(i): 734 + i for i in range(6)},
            corr={
                str(i): {
                    "local_vg_id": i,
                    "matrix_diff": 10.806 if i == 1 else 3.410,
                    "component_score": 9.39e-7,
                    "centroid_distance": 3.6e-7,
                }
                for i in range(6)
            },
        )
        aliases = ExportEFMI._build_same_ib_bone_aliases(baseline, lod)
        self.assertEqual(aliases, {734 + i: 365 + i for i in range(6)})

    def test_same_ib_fold_accepts_compatible_l1_skinning(self):
        """矩阵接近（捕获抖动级差异）的 L1 部件保持可折叠，不误杀正常脸部件。"""
        baseline = _FakePreSkindSubmesh(
            "LOD0.face-0", 100, 2, match_draw_ib="face",
            vg_map={"0": 100, "1": 101},
        )
        lod = _FakePreSkindSubmesh(
            "LOD1.face-0", 200, 2, match_draw_ib="face",
            ref_component="LOD0.face-0",
            vg_map={"0": 200, "1": 201},
            corr={
                "0": {"local_vg_id": 0, "matrix_diff": 0.0004},
                "1": {"local_vg_id": 1, "matrix_diff": 0.0001},
            },
        )
        aliases = ExportEFMI._build_same_ib_bone_aliases(baseline, lod)
        self.assertEqual(aliases, {200: 100, 201: 101})


if __name__ == "__main__":
    unittest.main()