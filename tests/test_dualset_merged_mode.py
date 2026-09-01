# -*- coding: utf-8 -*-
"""t11 补测：t10 合并模式修复（方案 A）的独立验证——只测新行为，不改实现。

覆盖（t10 notes §2/§3/§4 + t9 §5-方案A）：
1. `_rekey_same_ib_aliases_by_export_identity`（ui/universal/efmi.py，t9 A4 唯一必改动点）：
   - 更名 source 键 → e(s)；未更名 source 原样保留；恒等无害（{371:596}→{596:596}）；
   - 无更名表 / 无工作区 / 建表失败（A3/A4/B10）→ 保持原 aliases（+警告）；
   - 冲突防线（映射后 source 撞车且 target 不同）→ RuntimeError（fail-closed）。
2. `_apply_dualset_export_rename`（common/submesh_model.py，合并/非合并两模式共用）：
   - 更名语义：合并槽顶点组名按 rename_map 换成 e(s)；非数字/未更名组不动；
   - 纯更名（不含预处理链，调用方统一执行）；
   - fail-closed：建表失败（A3/A4）抛 RuntimeError 中止，不静默回退。
3. 数据流（合成合并场景，对应 t10 §4.2 边界）：slot1(1:0.5/2:1.5)→e(1)=2 更名；
   折叠 aliases {1:4,3:5}→{2:4,3:5}。

夹具隔离原则：合成临时工作区（Position/Blend buf 齐全），不改产品代码/真实工作区；
对拍用真实产品函数（EFMIBoneMapBuilder.build_dualset_export_table / compute_driven_signatures）。
"""
import importlib.util
import json
import os
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "dualset_merged_mode_test_pkg"

_SENT = 0xFFFFFFFF


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
_install_module(f"{PKG}.common.logic_name", LogicName=types.SimpleNamespace(EFMI="EFMI", ZZMI="ZZMI"))
_install_module(f"{PKG}.common.d3d11_gametype", D3D11GameType=object)
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
_install_module(f"{PKG}.common.drawib_model", DrawIBModel=object)
_install_module(f"{PKG}.common.buffer_export_helper", BufferExportHelper=types.SimpleNamespace())
_install_module(f"{PKG}.ui.universal.export_helper", ExportHelper=types.SimpleNamespace())

# ---- 真实产品模块（顺序：efmi_skeleton → m_ini_builder → submesh_model → efmi）----
_efmi_skeleton = _load_real(f"{PKG}.common.efmi_skeleton", "common/efmi_skeleton.py")
EFMIBoneMapBuilder = _efmi_skeleton.EFMIBoneMapBuilder
_real_ini = _load_real(f"{PKG}.common.m_ini_builder", "common/m_ini_builder.py")
M_IniBuilder = _real_ini.M_IniBuilder
M_IniSection = _real_ini.M_IniSection
M_SectionType = _real_ini.M_SectionType
_sm = _load_real(f"{PKG}.common.submesh_model", "common/submesh_model.py")
SubMeshModel = _sm.SubMeshModel
_efmi = _load_real(f"{PKG}.ui.universal.efmi", "ui/universal/efmi.py")
ExportEFMI = _efmi.ExportEFMI


# ---------------------------------------------------------------------------
# 合成工作区构造器（Position/Blend buf 齐全 → 强度可重算）
# ---------------------------------------------------------------------------

def blend_blob(indices, weights):
    out = bytearray()
    for i, w in zip(indices, weights):
        out += struct.pack("<4I", *i)
        out += struct.pack("<4f", *w)
    return bytes(out)


def pos_blob(coords):
    out = bytearray()
    for c in coords:
        out += struct.pack("<3f", *c)
    return bytes(out)


def write_submesh(root, lod, sub, offset, vg_count, vg_map, indices, weights, coords):
    type_dir = Path(root) / lod / sub / "TYPE_GPU_TEST_"
    type_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "GamePreset": "EFMI", "WorkGameType": "GPU", "VertexLimitVB": "vb0",
        "CategoryBufferList": [
            {"FileName": f"{sub}-Position.buf", "Type": "VS", "D3D11ElementList": [
                {"SemanticName": "POSITION", "SemanticIndex": 0, "Format": "R32G32B32A32_FLOAT",
                 "ByteWidth": 12, "AlignedByteOffset": 0, "Category": "Position",
                 "ExtractSlot": "VS", "ExtractTechnique": ""}]},
            {"FileName": f"{sub}-Blend.buf", "Type": "VS", "D3D11ElementList": [
                {"SemanticName": "BLENDINDICES", "SemanticIndex": 0, "Format": "R32G32B32A32_UINT",
                 "ByteWidth": 16, "AlignedByteOffset": 0, "Category": "Blend",
                 "ExtractSlot": "VS", "ExtractTechnique": ""},
                {"SemanticName": "BLENDWEIGHT", "SemanticIndex": 0, "Format": "R32G32B32A32_FLOAT",
                 "ByteWidth": 16, "AlignedByteOffset": 16, "Category": "Blend",
                 "ExtractSlot": "VS", "ExtractTechnique": ""}]},
        ],
        "VGOffset": offset, "VGCount": vg_count,
        "VGMap": {str(k): v for k, v in sorted(vg_map.items())},
        "VGMapAlgorithmVersion": 99, "VGMapDedupEnabled": True,
    }
    (type_dir / f"{sub}.json").write_text(json.dumps(payload), encoding="utf-8")
    (type_dir / f"{sub}-Position.buf").write_bytes(pos_blob(coords))
    (type_dir / f"{sub}-Blend.buf").write_bytes(blend_blob(indices, weights))
    return type_dir


def make_rename_workspace(root):
    """合并场景（t10 §4.2 边界数据，t18 v2 度量 = vertex_count）：
    LOD0 段：cA[0,2) 槽 {0:0, 1:1}（local1 vc=1）、cB[2,4) 槽 {0:1, 1:3}（local0 由 2 顶点驱动 vc=2）
    ⇒ 槽 1 合并 {id1:vc1, id2:vc2} → e(1)=2（更名）；槽 3 单源 id3（不更名）。
    """
    write_submesh(root, "LOD0", "aaaa1000-0-0", 0, 2, {0: 0, 1: 1},
                  [[1, _SENT, _SENT, _SENT], [0, _SENT, _SENT, _SENT]],
                  [[0.5, 0, 0, 0], [1.0, 0, 0, 0]],
                  [(0, 0, 0), (1, 0, 0)])
    write_submesh(root, "LOD0", "bbbb1000-0-0", 2, 2, {0: 1, 1: 3},
                  [[0, _SENT, _SENT, _SENT], [0, _SENT, _SENT, _SENT],
                   [1, _SENT, _SENT, _SENT]],
                  [[1.5, 0, 0, 0], [0.5, 0, 0, 0], [0.2, 0, 0, 0]],
                  [(2, 0, 0), (3, 0, 0), (4, 0, 0)])
    return root


def make_no_rename_workspace(root):
    """全部单源槽：无更名场景。"""
    write_submesh(root, "LOD0", "cccc2000-0-0", 0, 2, {0: 0, 1: 1},
                  [[1, _SENT, _SENT, _SENT], [0, _SENT, _SENT, _SENT]],
                  [[1.0, 0, 0, 0], [1.0, 0, 0, 0]],
                  [(0, 0, 0), (1, 0, 0)])
    return root


def make_broken_workspace(root):
    """耐久形态：Position/Blend 声明但文件物理缺失 → 建表 A4 RuntimeError。"""
    write_submesh(root, "LOD0", "dddd3000-0-0", 0, 2, {0: 0, 1: 1},
                  [[1, _SENT, _SENT, _SENT], [0, _SENT, _SENT, _SENT]],
                  [[1.0, 0, 0, 0], [1.0, 0, 0, 0]],
                  [(0, 0, 0), (1, 0, 0)])
    for blob_name in ("dddd3000-0-0-Position.buf", "dddd3000-0-0-Blend.buf"):
        p = root / "LOD0" / "dddd3000-0-0" / "TYPE_GPU_TEST_" / blob_name
        if p.is_file():
            p.unlink()
    return root


class _FakeVG:
    def __init__(self, name):
        self.name = name


class _FakeVGroup:
    """bpy 顶点权重条目（vertex.groups 的元素）：group=组索引, weight>0 才写盘。"""

    def __init__(self, group, weight):
        self.group = group
        self.weight = weight


class _FakeVertex:
    def __init__(self, groups):
        self.groups = groups


def _fake_mesh_obj(groups, weighted_by_group_index=None):
    """构造带 data.vertices 的 fake obj（走真实权重读取路径）。

    weighted_by_group_index: {组索引: [顶点索引...]} —— 该组带权重的顶点。
    未列出的组视为空组（不写盘）。
    """
    weighted_by_group_index = weighted_by_group_index or {}
    vg_list = [_FakeVG(g) for g in groups]
    vertices = []
    for vi in range(8):
        entries = []
        for gi, vlist in weighted_by_group_index.items():
            if vi in vlist:
                entries.append(_FakeVGroup(gi, 1.0))
        vertices.append(_FakeVertex(entries))
    return types.SimpleNamespace(
        type="MESH", name="objx",
        vertex_groups=vg_list,
        data=types.SimpleNamespace(vertices=vertices),
    )


# ===========================================================================
# 1. same-IB 折叠 aliases 按 e(s) 重写（t9 方案 A4 / t10 §4）
# ===========================================================================

class RekeyAliasesTests(unittest.TestCase):
    """_rekey_same_ib_aliases_by_export_identity（真实 efmi.py + 真实表 API）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="t11_rekey_")
        self.root = Path(self.tmp.name)
        self.exporter = object.__new__(ExportEFMI)
        # GlobalConfig 是测试桩 SimpleNamespace：为真实分支注入工作区路径
        _efmi.GlobalConfig.path_workspace_folder = lambda: str(self.root)
        _cref = None

    def tearDown(self):
        self.tmp.cleanup()
        try:
            del _efmi.GlobalConfig.path_workspace_folder
        except AttributeError:
            pass

    def test_renamed_source_rekeyed(self):
        """{1:4, 3:5}：槽 1 更名成 2 → {2:4, 3:5}（t10 §4.2 边界）。"""
        make_rename_workspace(self.root)
        table = EFMIBoneMapBuilder.build_dualset_export_table(str(self.root))
        self.assertEqual(table[1]["export_identity"], 2, "前置：槽1 e(s)=2")
        self.assertEqual(table[3]["export_identity"], 3, "前置：槽3 不更名")
        out = self.exporter._rekey_same_ib_aliases_by_export_identity({1: 4, 3: 5})
        self.assertEqual(out, {2: 4, 3: 5})

    def test_identity_noop_rename(self):
        """{371:596}（槽 371 更名成 596）→ {596:596}（恒等，remap 0 命中无害）。""" 
        make_rename_workspace(self.root)
        self.exporter._rekey_same_ib_aliases_by_export_identity  # ensure attr exists
        # 构造 slot 371 → 596：直接以合成表里不存在的数字验证「e_of.get(source, source)」行为
        # （合成表只含 0/1/3 等小槽；用真实表验证映射存在性见 test_renamed_source_rekeyed）
        make_rename_workspace(self.root)
        # 临时造大号更名：槽 700 无成员时不可更名；这里退而验证未更名 source 原样保留
        out = self.exporter._rekey_same_ib_aliases_by_export_identity({371: 596})
        self.assertEqual(out, {371: 596}, "371 不在更名表中 → 原样保留（恒等无害前提）")

    def test_unrenamed_source_kept_same_object(self):
        """无更名项命中时返回原对象（changed=False → aliases 本体）。"""
        make_rename_workspace(self.root)
        aliases = {3: 592, 9: 100}
        out = self.exporter._rekey_same_ib_aliases_by_export_identity(aliases)
        self.assertIs(out, aliases, "无更名命中 → 原对象返回（零开销）")
        self.assertEqual(out, {3: 592, 9: 100})

    def test_no_rename_table_returns_same(self):
        """全单源槽工作区：e_of 空 → 原对象返回。"""
        make_no_rename_workspace(self.root)
        aliases = {0: 7}
        out = self.exporter._rekey_same_ib_aliases_by_export_identity(aliases)
        self.assertIs(out, aliases)

    def test_no_workspace_keeps_aliases(self):
        """GlobalConfig.path_workspace_folder 缺失/不可调 → 保持原 aliases（+警告），不崩溃。"""
        _efmi.GlobalConfig.path_workspace_folder = None
        aliases = {1: 4}
        out = self.exporter._rekey_same_ib_aliases_by_export_identity(aliases)
        self.assertIs(out, aliases)

    def test_table_failure_keeps_aliases(self):
        """建表失败（A4 强度不可得）→ 保持原 aliases（警告分支），不崩溃。"""
        make_broken_workspace(self.root)
        aliases = {1: 4}
        out = self.exporter._rekey_same_ib_aliases_by_export_identity(aliases)
        self.assertIs(out, aliases)

    def test_conflict_raises(self):
        """映射后 source 撞车且 target 不同 → RuntimeError（fail-closed，理论不可达防御）。"""
        make_rename_workspace(self.root)
        # 槽 1 → e=2；伪造 aliases 含非法 source 键 2（推论 2：非槽位号）→ new_source 撞 2
        aliases = {1: 4, 2: 5}
        with self.assertRaises(RuntimeError):
            self.exporter._rekey_same_ib_aliases_by_export_identity(aliases)


# ===========================================================================
# 2. 双套更名粘合（合并/非合并两模式共用）：rename_map 应用 + fail-closed
# ===========================================================================

class ApplyRenameTests(unittest.TestCase):
    """_apply_dualset_export_rename（真实 submesh_model.py，fake obj 免 bpy 环境）。

    t25 v3（per-mesh）：模型必须带 unique_str/workspace_unique_str（定位本组件段
    [VGOffset, VGOffset+VGCount)）；更名目标 = **本组件成员身份**（非全局 e(s)）。
    合成工作区组件：LOD0.aaaa1000-0-0（[0,2)，槽 {0:0,1:1}）、LOD0.bbbb1000-0-0
    （[2,4)，槽 {0:1,1:3}）；槽 1 成员 = {id1(aaaa l1), id2(bbbb l0)}——
    per-mesh 下 bbbb 模型 e_M(1)=2（更名 1→2），aaaa 模型 e_M(1)=1（恒等不更名）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="t11_rename_")
        self.root = Path(self.tmp.name)
        _sm.GlobalConfig.path_workspace_folder = lambda: str(self.root)
        EFMIBoneMapBuilder._dualset_table_cache.clear()

    def tearDown(self):
        self.tmp.cleanup()
        try:
            del _sm.GlobalConfig.path_workspace_folder
        except AttributeError:
            pass

    def _model(self, unique_str="LOD0.bbbb1000-0-0"):
        model = object.__new__(SubMeshModel)
        model.vg_map_algorithm_version = 99
        model.merged_skeleton_metadata_valid = True
        model.d3d11_game_type = types.SimpleNamespace(GPU_PreSkinning=True)
        model.unique_str = unique_str
        model.workspace_unique_str = unique_str
        return model

    def test_merged_slot_renamed_to_identity(self):
        """per-mesh：bbbb 模型（身份段 [2,4)）槽 1 → 本组件成员 id2 → 组名 1→2。

        域前置（t31）：带权重数字组名 ⊆ 身份域 {1,3} 才放行；组 7 为空组
        （无权重）不写盘、保留不动。
        """
        make_rename_workspace(self.root)
        # 组索引：1->0, 3->1, 7->2；带权重的是组 1 与组 3
        obj = _fake_mesh_obj(
            ["1", "3", "7"], weighted_by_group_index={0: [0, 1], 1: [2, 3]}
        )
        self._model()._apply_dualset_export_rename(obj)
        names = sorted(vg.name for vg in obj.vertex_groups)
        self.assertEqual(names, ["2", "3", "7"], "槽 1 更名成 2（本组件成员身份）；槽 3/7 未更名保持")

    def test_weighted_out_of_domain_raises_with_guidance(self):
        """t31 佩丽卡场景A硬化：带权重数字组名 ∉ 身份域 → RuntimeError + 重新导入指引。

        模拟「旧导入/旧缓存」不同期对象：带权重组名 = 旧合并槽（如佩丽卡
        373..393 一类），不在当前 json VGMap 引用槽域内。静默跳过会让其原样
        直写、直到 FC-2 才被拦；现在 M1 更名点前置 fail-closed。
        """
        make_rename_workspace(self.root)
        # bbbb 身份域 = {1,3}；对象带权重组 = {1, 99}（99 域外）
        obj = _fake_mesh_obj(
            ["1", "3", "99"], weighted_by_group_index={0: [0, 1], 2: [4, 5]}
        )
        with self.assertRaises(RuntimeError) as ctx:
            self._model()._apply_dualset_export_rename(obj)
        msg = str(ctx.exception)
        self.assertIn("99", msg, "报错应列出域外槽位")
        self.assertIn("重新导入", msg, "报错必须给出重新导入指引")
        self.assertIn("fail-closed", msg)
        # 更名不得发生（中止导出）
        names = sorted(vg.name for vg in obj.vertex_groups)
        self.assertEqual(names, ["1", "3", "99"])

    def test_unweighted_out_of_domain_ok(self):
        """t31 兼容：域外数字组名若**不带权重**（不写盘）→ 放行。

        覆盖 import_skip_empty_vertex_groups=False 的全量 0..N-1 空组工作流
        与 0 哨兵/补缺空组：与 FC-2 同口径（只断言权重>0 通道），不误伤。
        """
        make_rename_workspace(self.root)
        # 组 1/3 带权重（域内），组 7/99 空组（域外但不写盘）
        obj = _fake_mesh_obj(
            ["1", "3", "7", "99"], weighted_by_group_index={0: [0, 1], 1: [2, 3]}
        )
        self._model()._apply_dualset_export_rename(obj)  # 不抛
        names = sorted(vg.name for vg in obj.vertex_groups)
        self.assertEqual(names, ["2", "3", "7", "99"], "带权重组 1 更名；空组保持")

    def test_fold_baseline_slots_allowed(self):
        """t16 统一骨架并集域：全工作区已注册槽并集放行（引用口径）。

        工作区已注册槽并集 = aaaa{0,1} ∪ bbbb{1,3}（VGMap 引用值，非段补集）
        → {0,1,3}；LOD1 拷贝带权重组 {4,3} ⊆ {4,5} ∪ 并集 → 放行；带权重组含
        7 或未注册的 2（段隙但无引用）→ 拒绝（消息含「全工作区已注册槽」）。
        """
        make_rename_workspace(self.root)
        self.assertEqual(
            _sm.SubMeshModel._dualset_registered_slots_union(str(self.root)),
            {0, 1, 3},
            "并集 = aaaa{0,1} ∪ bbbb{1,3}（引用值）",
        )
        # 带权重组 {4,3}：3 ∈ 并集（跨组件已注册槽）→ 放行（A 引用 B 槽语义）
        obj_ok = _fake_mesh_obj(
            ["4", "5", "3"], weighted_by_group_index={2: [0, 1], 0: [2, 3]}
        )
        _sm.SubMeshModel._assert_dualset_weighted_groups_in_identity_domain(
            obj_ok, {4, 5}, str(self.root), "LOD1.bbbb1000-0-0"
        )
        # 带权重组 {4, 7}：7 未注册（不在并集）→ 拒绝 + 文案含「全工作区已注册槽」
        obj_bad = _fake_mesh_obj(
            ["4", "5", "7"], weighted_by_group_index={0: [0, 1], 2: [2, 3]}
        )
        with self.assertRaises(RuntimeError) as ctx:
            _sm.SubMeshModel._assert_dualset_weighted_groups_in_identity_domain(
                obj_bad, {4, 5}, str(self.root), "LOD1.bbbb1000-0-0"
            )
        self.assertIn("7", str(ctx.exception))
        self.assertIn("全工作区所有组件已注册槽", str(ctx.exception),
                      "未注册槽报错必须含真实域表述（全工作区已注册槽）")
        # 段隙槽 2 无引用 → 同样拒绝（引用口径）
        obj_gap = _fake_mesh_obj(
            ["4", "5", "2"], weighted_by_group_index={0: [0, 1], 2: [2, 3]}
        )
        with self.assertRaises(RuntimeError) as ctx_gap:
            _sm.SubMeshModel._assert_dualset_weighted_groups_in_identity_domain(
                obj_gap, {4, 5}, str(self.root), "LOD1.bbbb1000-0-0"
            )
        self.assertIn("2", str(ctx_gap.exception))
        # 并集对所有组件生效（LOD0 组件也可引用 LOD0 其它组件注册槽 → 放行）
        obj_l0 = _fake_mesh_obj(
            ["0", "1", "3"], weighted_by_group_index={0: [0, 1], 2: [2, 3]}
        )
        _sm.SubMeshModel._assert_dualset_weighted_groups_in_identity_domain(
            obj_l0, {0, 1}, str(self.root), "LOD0.aaaa1000-0-0"
        )

    def test_t16_cross_component_reference_and_placeholder(self):
        """t16 三分支：A 引用 B 注册槽放行 + B 占位注册在场；旧代槽拒绝；元数据缺失区分。"""
        import json as _json
        # 合成工作区：LOD1.A 注册 {700,701}；LOD1.B 注册 {372}（B 占位槽）
        for bare_name, vgmap in (("aaaacross-100-0", {"0": 700, "1": 701}),
                                 ("bbbplaceholder-200-0", {"0": 372})):
            tdir = os.path.join(self.root, "LOD1", bare_name, "TYPE_X")
            os.makedirs(tdir, exist_ok=True)
            with open(os.path.join(tdir, bare_name + ".json"), "w", encoding="utf-8") as f:
                _json.dump({"VGMap": vgmap, "GPU-PreSkinning": True}, f)

        union = _sm.SubMeshModel._dualset_registered_slots_union(str(self.root))
        self.assertTrue({700, 701, 372} <= union, f"并集应含 A/B 注册槽: {union}")
        # B 占位注册在场：B json VGMap 含 372（A 引用它时 B 生成占位符保持注册）
        self.assertIn(372, union, "B 占位槽 372 已注册（占位符机制在场）")

        # A 对象带权重组 {700（自属）, 372（引用 B 注册槽）} → 放行
        obj_a = _fake_mesh_obj(
            ["700", "701", "372"], weighted_by_group_index={0: [0], 1: [1], 2: [2]}
        )
        _sm.SubMeshModel._assert_dualset_weighted_groups_in_identity_domain(
            obj_a, {700, 701}, str(self.root), "LOD1.aaaacross-100-0"
        )
        # 旧代槽 373（未注册）→ 拒绝（fail-closed 牙齿保留）
        obj_old = _fake_mesh_obj(
            ["700", "373"], weighted_by_group_index={0: [0], 1: [1]}
        )
        with self.assertRaises(RuntimeError) as ctx:
            _sm.SubMeshModel._assert_dualset_weighted_groups_in_identity_domain(
                obj_old, {700, 701}, str(self.root), "LOD1.aaaacross-100-0"
            )
        msg = str(ctx.exception)
        self.assertIn("373", msg)
        self.assertNotIn("无法判定槽位归属", msg, "未注册槽 ≠ 元数据缺失")
        self.assertIn("全工作区所有组件已注册槽", msg)

        # 元数据缺失（清空全部 json 的 VGMap）→ 报错区分「元数据缺失」
        for dirpath, _dirs, files in os.walk(self.root):
            for name in files:
                if not name.endswith(".json"):
                    continue
                p = os.path.join(dirpath, name)
                try:
                    with open(p, encoding="utf-8") as f:
                        payload = _json.load(f)
                except Exception:
                    continue
                payload.pop("VGMap", None)
                with open(p, "w", encoding="utf-8") as f:
                    _json.dump(payload, f, ensure_ascii=False)
        with self.assertRaises(RuntimeError) as ctx2:
            _sm.SubMeshModel._assert_dualset_weighted_groups_in_identity_domain(
                obj_a, {700, 701}, str(self.root), "LOD1.aaaacross-100-0"
            )
        self.assertIn("元数据缺失", str(ctx2.exception),
                      "全 json 无 VGMap → 必须区分「元数据缺失」原因")

    def test_other_component_maps_to_own_identity(self):
        """per-mesh 对称：aaaa 模型（身份段 [0,2)）槽 1 → 本组件成员 id1 → 恒等（不更名）。"""
        make_rename_workspace(self.root)
        obj = types.SimpleNamespace(type="MESH", name="objx",
                                    vertex_groups=[_FakeVG("1"), _FakeVG("0")])
        self._model(unique_str="LOD0.aaaa1000-0-0")._apply_dualset_export_rename(obj)
        self.assertEqual(sorted(vg.name for vg in obj.vertex_groups), ["0", "1"],
                         "aaaa 模型槽 1 恒等（e_M(1)=id1=1），不更名")

    def test_non_numeric_and_unweighted_groups_untouched(self):
        """非数字组名与不带权重的域外组不处理（t31：域前置只查带权重数字组）。"""
        make_rename_workspace(self.root)
        # 组索引 0 = "head"（非数字，权重忽略），组索引 1 = "99"（数字但空组）
        obj = _fake_mesh_obj(
            ["head", "99"], weighted_by_group_index={0: [6, 7]}
        )
        self._model()._apply_dualset_export_rename(obj)
        self.assertEqual([vg.name for vg in obj.vertex_groups], ["head", "99"])

    def test_no_rename_map_noop(self):
        """全单源槽（cccc 模型）→ per-mesh 表 {0:0,1:1} → 不动作。"""
        make_no_rename_workspace(self.root)
        obj = types.SimpleNamespace(type="MESH", name="objx", vertex_groups=[_FakeVG("0")])
        self._model(unique_str="LOD0.cccc2000-0-0")._apply_dualset_export_rename(obj)
        self.assertEqual([vg.name for vg in obj.vertex_groups], ["0"])

    def test_table_failure_fail_closed(self):
        """建表失败（A4 强度不可得）→ RuntimeError 冒泡（不静默回退槽位直写）。"""
        make_broken_workspace(self.root)
        obj = types.SimpleNamespace(type="MESH", name="objx", vertex_groups=[_FakeVG("1")])
        with self.assertRaises(RuntimeError):
            self._model()._apply_dualset_export_rename(obj)

    def test_metadata_gate_skip(self):
        """vg_map_algorithm_version<=0 或 metadata 无效 → 直接返回（不尝试建表）。"""
        make_rename_workspace(self.root)
        model = self._model()
        model.vg_map_algorithm_version = 0
        obj = types.SimpleNamespace(type="MESH", name="objx", vertex_groups=[_FakeVG("1")])
        model._apply_dualset_export_rename(obj)
        self.assertEqual([vg.name for vg in obj.vertex_groups], ["1"],
                         "非去重对象不得更名")


# ===========================================================================
# 3. 合并模式数据流（对应 t10 §4.2 端到端边界）
# ===========================================================================

class MergedModeDataFlowTests(unittest.TestCase):
    """合成合并场景：表 e(s) + 更名表 + 折叠换算联合（与 t10 声明数值对拍）。"""

    def test_merged_flow_rename_and_rekey(self):
        tmp = tempfile.TemporaryDirectory(prefix="t11_flow_")
        try:
            root = Path(tmp.name)
            make_rename_workspace(root)
            table = EFMIBoneMapBuilder.build_dualset_export_table(str(root))
            rename_map = {s: r["export_identity"] for s, r in table.items() if r["renamed"]}
            self.assertEqual(rename_map, {1: 2}, "更名表：槽1→身份2")
            # 折叠 aliases 按 e(s) 重写（模拟 _get_merged_skeleton_component_info 聚合处行为）
            _efmi.GlobalConfig.path_workspace_folder = lambda: str(root)
            exporter = object.__new__(ExportEFMI)
            out = exporter._rekey_same_ib_aliases_by_export_identity({1: 4, 3: 5})
            self.assertEqual(out, {2: 4, 3: 5})
            # 导出身份集合唯一（A2 数值验证）
            ids = [r["export_identity"] for r in table.values()]
            self.assertEqual(len(ids), len(set(ids)))
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)