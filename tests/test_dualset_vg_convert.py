# -*- coding: utf-8 -*-
"""t6 单测：双套顶点组导出转换规则（按权重强度更名回非去重身份）。

规格依据：.dbg/agent-teams/efmi-vg-dualset/t3-dualset-vg-spec.md（§2 转换规则、§4 强度度量、
§5 边界矩阵 B1-B15、§6.4 fail-closed 断言 A1-A5）。
被测实现：**产品实现（t8 落地）common/efmi_skeleton.EFMIBoneMapBuilder 的 t6 契约入口**
  （select_export_identity / build_export_table / compute_weight_total / run，L940-1147；
  导入点见本文件 _load_conversion_api，单一位置）。

测试纪律：
- 只测规格契约与公开接口，不抄实现内部逻辑；
- 每个边界用例至少一个正向 + 一个反向断言；
- 不修改产品代码/工作区：全部数据为临时合成工作区（tempfile），并断言工作区在
  run() 前后逐字节不变（「不写坏文件」）。
- 对拍产品 compute_driven_signatures（spec §4.1 定义的权威实现，common/efmi_skeleton.py
  L635-792 的 weight_total）验证强度度量与规格来源一致。

注：B8/B9（命名冲突）与 B15 的「强度数据不可得」属规格断言（A2/A3/A4）。B10/B11
跨 LOD 段撞车与 B15 静默容忍若与原型实际行为不符，用例如实失败并在 t6 报告中记录。
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

import numpy

REPO_ROOT = Path(__file__).resolve().parents[1]

_SENT = 0xFFFFFFFF

# ---------------------------------------------------------------------------
# 转换 API 装载——单一导入点（船长协同更新：t8 产品模块落地后只改本函数）
#
# t6 测试依赖的公开接口契约（t8 提供同语义等价物或 adapter）：
#   select_export_identity(slot:int, members:list[dict]) -> int
#       members = [{'local':int, 'identity':int, 'weight_total':float}, ...]（单源恒等）
#   build_export_table(slot_members:dict[int,list[dict]]) -> dict[int, {'slot','member_count','members','export_identity','renamed'}]
#   compute_weight_total(position_buf_path, blend_buf_path, submesh_json) -> dict[int,float]
#       （spec §4.1：Σ 有效通道原始权重；任一 buf 缺失 ⇒ 返回空表 {}，产品 compute_driven_signatures 口径）
#   run(workspace_root:str, outdir:str) -> int
#       输出 outdir/t5-summary.json（含 assert_A2_export_unique / assert_A3_identity_single 等）、
#       t5-ledger.json（{slot: {...export_identity...}}）、t5-rename_map.json
#
# 当前目标：t8 产品模块 common/efmi_skeleton.EFMIBoneMapBuilder（M1 挂载点产品落地）。
# 切换点说明（t6 报告 §1）：t8 落地后仅改本函数指向产品入口；测例与断言无需修改。
# 产品入口契约：select_export_identity / build_export_table / compute_weight_total / run。
# ---------------------------------------------------------------------------
def _load_conversion_api():
    import importlib.util as _iutil
    # 父包 stub（产品 efmi_skeleton 用 ..utils.json_utils 相对导入；无 bpy）
    root_pkg = types.ModuleType("TheHerta4")
    root_pkg.__path__ = []
    sys.modules["TheHerta4"] = root_pkg
    common_pkg = types.ModuleType("TheHerta4.common")
    common_pkg.__path__ = [str(Path(__file__).resolve().parents[1] / "common")]
    sys.modules["TheHerta4.common"] = common_pkg
    utils_pkg = types.ModuleType("TheHerta4.utils")
    utils_pkg.__path__ = [str(Path(__file__).resolve().parents[1] / "utils")]
    sys.modules["TheHerta4.utils"] = utils_pkg

    def _load_real(qualname, relpath):
        spec = _iutil.spec_from_file_location(qualname, Path(__file__).resolve().parents[1] / relpath)
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualname] = module
        spec.loader.exec_module(module)
        return module

    _load_real("TheHerta4.utils.json_utils", "utils/json_utils.py")
    efmi = _load_real("TheHerta4.common.efmi_skeleton", "common/efmi_skeleton.py")
    return efmi.EFMIBoneMapBuilder


_CONV = _load_conversion_api()
select_export_identity = _CONV.select_export_identity
build_export_table = _CONV.build_export_table
compute_weight_total = _CONV.compute_weight_total
run_conversion = _CONV.run

# ---------------------------------------------------------------------------
# 产品加载（对拍权威实现；fake PKG 模式，与 tests/test_efmi_skeleton_dedup.py 同构）
# ---------------------------------------------------------------------------
PKG = "dualset_vg_test_pkg"
for _name in (PKG, f"{PKG}.common", f"{PKG}.utils"):
    _m = types.ModuleType(_name)
    _m.__path__ = []
    sys.modules[_name] = _m


def _load_real(qualname, relpath):
    spec = importlib.util.spec_from_file_location(qualname, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


_load_real(f"{PKG}.utils.json_utils", "utils/json_utils.py")
_product_efmi = _load_real(f"{PKG}.common.efmi_skeleton", "common/efmi_skeleton.py")
ProductBuilder = _product_efmi.EFMIBoneMapBuilder

# ---------------------------------------------------------------------------
# 合成工作区构造器
# ---------------------------------------------------------------------------


def blend_blob(indices, weights):
    """R32G32B32A32_UINT INDICES + R32G32B32A32_FLOAT WEIGHTS，stride 32。"""
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


def write_submesh(root, lod, sub, offset, vg_count, vg_map, indices, weights, coords,
                  with_position=True, with_blend=True, vgmap_keys_missing=False):
    """写一个子网格 json + Position.buf/Blend.buf。返回 (json 路径, TYPE 目录)。"""
    type_dir = Path(root) / lod / sub / "TYPE_GPU_TEST_"
    type_dir.mkdir(parents=True, exist_ok=True)
    blend_elements = [
        {"SemanticName": "BLENDINDICES", "SemanticIndex": 0, "Format": "R32G32B32A32_UINT",
         "ByteWidth": 16, "AlignedByteOffset": 0, "Category": "Blend",
         "ExtractSlot": "VS", "ExtractTechnique": ""},
        {"SemanticName": "BLENDWEIGHT", "SemanticIndex": 0, "Format": "R32G32B32A32_FLOAT",
         "ByteWidth": 16, "AlignedByteOffset": 16, "Category": "Blend",
         "ExtractSlot": "VS", "ExtractTechnique": ""},
    ]
    cat_buffers = [{"FileName": f"{sub}-Blend.buf", "Type": "VS", "D3D11ElementList": blend_elements}]
    if with_position:
        cat_buffers.insert(0, {"FileName": f"{sub}-Position.buf", "Type": "VS", "D3D11ElementList": [
            {"SemanticName": "POSITION", "SemanticIndex": 0, "Format": "R32G32B32A32_FLOAT",
             "ByteWidth": 12, "AlignedByteOffset": 0, "Category": "Position",
             "ExtractSlot": "VS", "ExtractTechnique": ""},
        ]})
    mapped = dict(vg_map)
    if vgmap_keys_missing:
        mapped.pop(next(iter(mapped)), None)  # 制造 A1：键集 != VGCount
    payload = {
        "GamePreset": "EFMI", "WorkGameType": "GPU", "VertexLimitVB": "vb0",
        "CategoryBufferList": cat_buffers,
        "VGOffset": offset, "VGCount": vg_count,
        "VGMap": {str(k): v for k, v in sorted(mapped.items())},
        "VGMapAlgorithmVersion": 99, "VGMapDedupEnabled": True,
    }
    json_path = type_dir / f"{sub}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    if with_position:
        (type_dir / f"{sub}-Position.buf").write_bytes(pos_blob(coords))
    if with_blend:
        (type_dir / f"{sub}-Blend.buf").write_bytes(blend_blob(indices, weights))
    return json_path, type_dir


def snapshot_workspace(root):
    snap = {}
    for dirpath, _dirs, files in os.walk(str(root)):
        for name in files:
            full = os.path.join(dirpath, name)
            with open(full, "rb") as f:
                snap[full] = f.read()
    return snap


def member(identity, weight_total=0.0, vertex_count=0, local=None):
    """构造成员 dict（t18 v2：主度量 = vertex_count；weight_total 为参考字段）。"""
    return {"comp": "x", "local": identity if local is None else local,
            "identity": identity, "weight_total": float(weight_total or 0.0),
            "vertex_count": int(vertex_count or 0)}


def make_outdir():
    """输出目录必须位于被测工作区之外（否则快照对比会看到新增产物）。"""
    return Path(tempfile.mkdtemp(prefix="t6_out_"))


# ===========================================================================
# A. 决策规则：select_export_identity（spec §4.3 / §5 B1-B7）
# ===========================================================================

class SelectExportIdentityTests(unittest.TestCase):
    """§4.3 三层 tie-break + B1-B7 决策行为。"""

    def test_b1_single_member_identity(self):
        """B1 单源槽：无条件返回身份（==槽位号，恒等）。"""
        self.assertEqual(select_export_identity(3, [member(3, 5.0)]), 3)
        # 反向：单源即使 vertex_count=0 也恒等（B7 组合）
        self.assertEqual(select_export_identity(7, [member(7, 0.0)]), 7)

    def test_b2_two_members_strongest_not_canonical(self):
        """B2 主场景（t18 v2）：顶点数多≠canonical → 更名（组9→90 同构）。"""
        self.assertEqual(
            select_export_identity(3, [member(3, 2.0, vertex_count=4),
                                       member(108, 9.0, vertex_count=9)]), 108
        )

    def test_b2b_two_members_strongest_is_canonical(self):
        """B3 对称：顶点数多==canonical → e(s)==槽位号（rename_map 无此项）。"""
        self.assertEqual(
            select_export_identity(3, [member(3, 9.0, vertex_count=9),
                                       member(108, 2.0, vertex_count=2)]), 3
        )

    def test_b4_three_members_strongest(self):
        """B4 3 源（slot106 型：{身份2, 身份106(canonical), 身份318}，v2 按顶点数）。"""
        m = [member(2, 1.0, vertex_count=1), member(106, 5.0, vertex_count=5),
             member(318, 3.0, vertex_count=3)]
        self.assertEqual(select_export_identity(106, m), 106)
        # 反向：最多非 canonical → 取最多
        m2 = [member(2, 7.0, vertex_count=7), member(106, 5.0, vertex_count=5),
              member(318, 3.0, vertex_count=3)]
        self.assertEqual(select_export_identity(106, m2), 2)

    def test_b5_tie_canonical_preferred(self):
        """B5 并列且 canonical ∈ T（v2：顶点数精确相等）：取 canonical。"""
        m = [member(2, 5.0, vertex_count=5), member(106, 5.0, vertex_count=5),
             member(318, 3.0, vertex_count=3)]
        self.assertEqual(select_export_identity(106, m), 106)

    def test_b5b_vertex_count_exact_tie(self):
        """B5b（t18 v2 重定义）：并列=vertex_count 精确相等（计数型，无浮点容差）。
        旧 weight_total 浮点容差并列语义已作废（t17 notes §3）。"""
        m_eq = [member(3, 3.0, vertex_count=4), member(108, 3.0, vertex_count=4)]
        self.assertEqual(select_export_identity(3, m_eq), 3)
        # 反向：计数差 1 → 非并列 → 108（精确相等判定）
        m_ne = [member(3, 3.0, vertex_count=4), member(108, 3.0, vertex_count=5)]
        self.assertEqual(select_export_identity(3, m_ne), 108)

    def test_b6_tie_canonical_not_in_t_identity_asc(self):
        """B6 并列且 canonical ∉ T：identity 升序裁决（v2 顶点数精确并列）。"""
        m = [member(2, 5.0, vertex_count=5), member(106, 3.0, vertex_count=3),
             member(318, 5.0, vertex_count=5)]
        self.assertEqual(select_export_identity(106, m), 2)
        # 反向：升序稳定（318 与 2 交换构造顺序仍并列）
        m2 = [member(318, 5.0, vertex_count=5), member(2, 5.0, vertex_count=5),
              member(106, 3.0, vertex_count=3)]
        self.assertEqual(select_export_identity(106, m2), 2)

    def test_b7_zero_weight_members(self):
        """B7 全零顶点数成员：0 参与比较；并列按 tie-break（v2 语义）。"""
        m = [member(3, 0.0, vertex_count=0), member(108, 0.0, vertex_count=0)]
        self.assertEqual(select_export_identity(3, m), 3)
        # 反向：只要一个顶点数>0，另一为 0 → 取正者
        m2 = [member(3, 0.0, vertex_count=0), member(108, 0.5, vertex_count=1)]
        self.assertEqual(select_export_identity(3, m2), 108)

    def test_non_numeric_identity_ordering_deterministic(self):
        """确定性：同输入多次调用结果一致（并发/顺序无关）。"""
        m = [member(318, 5.0, vertex_count=5), member(2, 5.0, vertex_count=5),
             member(106, 3.0, vertex_count=3)]
        first = select_export_identity(106, m)
        for _ in range(5):
            self.assertEqual(select_export_identity(106, m), first)


class BuildExportTableTests(unittest.TestCase):
    """build_export_table：表结构与 renamed 标记。"""

    def test_table_shape_and_renamed_flag(self):
        table = build_export_table({
            3: [member(3, 2.0, vertex_count=2), member(108, 9.0, vertex_count=9)],
            4: [member(4, 1.0)],
        })
        self.assertEqual(set(table), {3, 4})
        row = table[3]
        self.assertEqual(row["export_identity"], 108)
        self.assertTrue(row["renamed"])
        self.assertEqual(table[4]["export_identity"], 4)
        self.assertFalse(table[4]["renamed"])

    def test_per_slot_independence(self):
        """各槽决策互不串扰（嵌套/多槽并存的基础）。"""
        table = build_export_table({
            3: [member(3, 2.0, vertex_count=1), member(108, 9.0, vertex_count=4)],
            9: [member(9, 9.0, vertex_count=9), member(200, 2.0, vertex_count=2)],
        })
        self.assertEqual(table[3]["export_identity"], 108)
        self.assertEqual(table[9]["export_identity"], 9)


# ===========================================================================
# B. 强度度量 compute_weight_total（spec §4.1：Σ 有效通道原始权重）
# ===========================================================================

class StrengthComputationTests(unittest.TestCase):
    """合成 Blend.buf/Position.buf 验证 weight_total 按规格定义计算。"""

    def _single_submesh_ctx(self, indices, weights, coords,
                            with_position=True, with_blend=True, vgmap_keys_missing=False):
        tmp = tempfile.TemporaryDirectory(prefix="t6_strength_")
        root = Path(tmp.name)
        json_path, type_dir = write_submesh(
            root, "LOD0", "aaaa1111-0-0", offset=0, vg_count=4,
            vg_map={0: 0, 1: 1, 2: 2, 3: 3},
            indices=indices, weights=weights, coords=coords,
            with_position=with_position, with_blend=with_blend,
            vgmap_keys_missing=vgmap_keys_missing,
        )
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["__json_path__"] = str(json_path)
        return tmp, type_dir, data

    def test_basic_sigma_w(self):
        """基本 Σw：逐顶点逐通道累加、哨兵剔除、w==0 剔除。"""
        tmp, type_dir, data = self._single_submesh_ctx(
            indices=[[1, 2, 3, _SENT], [2, _SENT, _SENT, _SENT], [0, _SENT, _SENT, _SENT]],
            weights=[[0.5, 0.3, 0.2, 0.0], [1.0, 0.0, 0.0, 0.0], [0.4, 0.0, 0.0, 0.0]],
            coords=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (2.0, 0.0, 0.0)],
        )
        try:
            s = compute_weight_total(
                str(type_dir / "aaaa1111-0-0-Position.buf"),
                str(type_dir / "aaaa1111-0-0-Blend.buf"), data)
            for local, expected in ((0, 0.4), (1, 0.5), (2, 1.3), (3, 0.2)):
                self.assertAlmostEqual(s[local], expected, places=6,
                                       msg=f"local {local} weight_total 不符")
        finally:
            tmp.cleanup()

    def test_position_finite_filter(self):
        """位置有限性过滤：inf/NaN 顶点的全部通道不计入强度。"""
        tmp, type_dir, data = self._single_submesh_ctx(
            indices=[[1, _SENT, _SENT, _SENT], [2, _SENT, _SENT, _SENT]],
            weights=[[1.0, 0, 0, 0], [1.0, 0, 0, 0]],
            coords=[(float("inf"), 0.0, 0.0), (1.0, 0.0, 0.0)],  # v0 位置非有限
        )
        try:
            s = compute_weight_total(
                str(type_dir / "aaaa1111-0-0-Position.buf"),
                str(type_dir / "aaaa1111-0-0-Blend.buf"), data)
            self.assertNotIn(1, s, "位置非有限的顶点不得计入 local1")
            self.assertAlmostEqual(s[2], 1.0, places=6)
        finally:
            tmp.cleanup()

    def test_zero_weight_channels_excluded(self):
        """w==0 通道剔除：全零权重的 local 不出现（产品 L759 丢弃语义）。"""
        tmp, type_dir, data = self._single_submesh_ctx(
            indices=[[0, 1, _SENT, _SENT]],
            weights=[[0.0, 0.0, 0.0, 0.0]],
            coords=[(0.0, 0.0, 0.0)],
        )
        try:
            s = compute_weight_total(
                str(type_dir / "aaaa1111-0-0-Position.buf"),
                str(type_dir / "aaaa1111-0-0-Blend.buf"), data)
            self.assertEqual(s, {}, "全零权重 → 空强度表（B7 数据级）")
        finally:
            tmp.cleanup()

    def test_missing_buffer_is_not_silent(self):
        """B15 反面：Blend/Position 不可得必须拒绝转换（A4），不得静默回退槽位直写。

        产品契约（run 层）：不抛异常，以 rc=1 + summary.assert_A4==False +
        fail_reason 含 A4 表示拒绝（与规格 §3.3/§6.4 A4「拒绝转换并提示原因」一致）。
        """
        tmp = tempfile.TemporaryDirectory(prefix="t6_b15_")
        try:
            root = Path(tmp.name)
            write_submesh(root, "LOD0", "bbbb2222-0-0", offset=0, vg_count=2,
                          vg_map={0: 0, 1: 1},
                          indices=[[1, _SENT, _SENT, _SENT], [0, _SENT, _SENT, _SENT]],
                          weights=[[1.0, 0, 0, 0], [1.0, 0, 0, 0]],
                          coords=[(0, 0, 0), (1, 0, 0)],
                          with_position=False, with_blend=False)
            before = snapshot_workspace(root)
            out = make_outdir()
            rc = run_conversion(str(root), str(out))
            after = snapshot_workspace(root)
            self.assertEqual(before, after, "run() 不得改写工作区")
            self.assertNotEqual(rc, 0, "双 buf 缺失必须拒绝转换（A4），不静默产出恒等表")
            with open(out / "t5-summary.json", "r", encoding="utf-8") as f:
                summary = json.load(f)
            self.assertFalse(summary["assert_A4_strength_available"],
                             "A4 标记必须置 False")
            self.assertIn("A4", summary["fail_reason"],
                          "fail_reason 必须提示强度数据不可得")
        finally:
            tmp.cleanup()

    def test_bad_buffer_alignment_raises(self):
        """缓冲区与 stride 不对齐 → 拒绝（负向）。"""
        tmp, type_dir, data = self._single_submesh_ctx(
            indices=[[1, _SENT, _SENT, _SENT], [0, _SENT, _SENT, _SENT]],
            weights=[[1.0, 0, 0, 0], [1.0, 0, 0, 0]],
            coords=[(0, 0, 0), (1, 0, 0)],
        )
        try:
            blend_path = type_dir / "aaaa1111-0-0-Blend.buf"
            blob = bytearray(blend_path.read_bytes())
            blob.append(0)  # 破坏 stride 对齐（32 字节/顶点 +1）
            blend_path.write_bytes(bytes(blob))
            with self.assertRaises(ValueError):
                compute_weight_total(
                    str(type_dir / "aaaa1111-0-0-Position.buf"),
                    str(blend_path), data)
        finally:
            tmp.cleanup()


class ProductParityTests(unittest.TestCase):
    """强度度量与产品 compute_driven_signatures（规格权威来源）对拍。"""

    def test_weight_total_parity_with_product(self):
        tmp, type_dir, data = None, None, None
        tmpdir = tempfile.TemporaryDirectory(prefix="t6_parity_")
        try:
            root = Path(tmpdir.name)
            _, type_dir = write_submesh(
                root, "LOD0", "cccc3333-0-0", offset=0, vg_count=4,
                vg_map={0: 0, 1: 1, 2: 2, 3: 3},
                indices=[[1, 2, 3, _SENT], [2, _SENT, _SENT, _SENT], [0, _SENT, _SENT, _SENT]],
                weights=[[0.5, 0.3, 0.2, 0.0], [1.0, 0.0, 0.0, 0.0], [0.4, 0.0, 0.0, 0.0]],
                coords=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (2.0, 0.0, 0.0)],
            )
            pos = str(type_dir / "cccc3333-0-0-Position.buf")
            blend = str(type_dir / "cccc3333-0-0-Blend.buf")
            json_path = type_dir / "cccc3333-0-0.json"
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            product = ProductBuilder.compute_driven_signatures(pos, blend, data)
            proto = compute_weight_total(pos, blend, data)
            pkeys = [k for k, v in product.items() if v.get("weight_total", 0.0) > 0]
            self.assertEqual(sorted(pkeys), sorted(proto),
                             "产品/prototype 的 local 集合不一致")
            for local in pkeys:
                pv = float(product[local].get("weight_total", 0.0))
                qv = float(proto[local])
                self.assertAlmostEqual(pv, qv, places=6,
                                       msg=f"local {local} weight_total 对拍不一致")
        finally:
            tmpdir.cleanup()


# ===========================================================================
# C. 顶点跨源组重叠（spec §4.4：跨组件共享权重只在 M3 有剔除问题，M1 整槽跟随；
#    强度按各成员自身分量独立统计，重叠不串扰）
# ===========================================================================

class OverlapTests(unittest.TestCase):
    """同一顶点多通道引用多个 local：weight_total 按 local 独立累加（重叠不串扰）。"""

    def test_overlap_members_keep_independent_strength(self):
        tmp = tempfile.TemporaryDirectory(prefix="t6_overlap_")
        try:
            root = Path(tmp.name)
            _, type_dir = write_submesh(
                root, "LOD0", "dddd4444-0-0", offset=0, vg_count=2,
                vg_map={0: 0, 1: 1},
                indices=[[1, 0, _SENT, _SENT], [0, _SENT, _SENT, _SENT]],
                weights=[[0.4, 0.6, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]],
                coords=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)],
            )
            pos = str(type_dir / "dddd4444-0-0-Position.buf")
            blend = str(type_dir / "dddd4444-0-0-Blend.buf")
            with open(type_dir / "dddd4444-0-0.json", "r", encoding="utf-8") as f:
                data = json.load(f)
            s = compute_weight_total(pos, blend, data)
            # v0 同时引用 local0(0.6)/local1(0.4)；v1 再为 local0 补 1.0
            self.assertAlmostEqual(s[0], 1.6, places=6)
            self.assertAlmostEqual(s[1], 0.4, places=6)
            # 决策层（t18 v2：主度量=vertex_count）：local0 被 v0+v1 驱动（vc=2）> local1（vc=1）
            m2 = [member(7, s[0], vertex_count=2), member(108, s[1], vertex_count=1)]
            self.assertEqual(select_export_identity(7, m2), 7)
            # 反向：交换顶点数 → 取多者（身份 108）
            m3 = [member(7, s[1], vertex_count=1), member(108, s[0], vertex_count=2)]
            self.assertEqual(select_export_identity(7, m3), 108)
        finally:
            tmp.cleanup()


# ===========================================================================
# D. run 层 fail-closed：A1/A2/A3 + B8/B9 命名冲突 + B13 损坏 json + 不写坏文件
# ===========================================================================

class RunFailClosedTests(unittest.TestCase):
    """合成工作区 + run()：断言触发时停止转换、工作区文件逐字节不变。"""

    def test_a1_damaged_vgmap_skipped_loud(self):
        """B13/A1：VGMap 键集!=VGCount → run 层跳过 + 警告 + 不计入；完好 json 照常；工作区不写坏。

        夹具隔离：损坏子网格用独立 VGOffset（9）且带齐全 buf，避免与 A3（偏移段重叠）
        或 A4（强度源缺失）耦合，单独验证 A1 的 run 层契约。
        """
        tmp = tempfile.TemporaryDirectory(prefix="t6_a1_")
        try:
            root = Path(tmp.name)
            write_submesh(root, "LOD0", "eeee5555-0-0", offset=0, vg_count=2,
                          vg_map={0: 0, 1: 1},
                          indices=[[1, _SENT, _SENT, _SENT], [0, _SENT, _SENT, _SENT]],
                          weights=[[1.0, 0, 0, 0], [1.0, 0, 0, 0]],
                          coords=[(0, 0, 0), (1, 0, 0)])
            # 损坏子网格：VGCount=3 但 VGMap 仅 1 键（A1），独立 offset + 齐全 buf（隔离 A3/A4）
            write_submesh(root, "LOD0", "gggg6666-0-0", offset=9, vg_count=3,
                          vg_map={0: 0},
                          indices=[[0, _SENT, _SENT, _SENT]],
                          weights=[[1.0, 0, 0, 0]],
                          coords=[(20.0, 0.0, 0.0)])
            before = snapshot_workspace(root)
            out = make_outdir()
            rc = run_conversion(str(root), str(out))
            after = snapshot_workspace(root)
            self.assertEqual(before, after, "run() 不得改写工作区任何文件")
            with open(out / "t5-summary.json", "r", encoding="utf-8") as f:
                summary = json.load(f)
            self.assertEqual(summary["submesh_with_vgmap"], 1, "损坏 json 不得计入（B13 不纳入）")
            self.assertTrue(any("[A1]" in w for w in summary["warnings"]),
                            "损坏 json 必须产生警告（不越权自行修复）")
            self.assertEqual(rc, 0, "run 层 A1 契约为跳过+警告+继续，不阻断完好部分")
        finally:
            tmp.cleanup()

    def test_identity_conflict_fails_closed(self):
        """B8/B9：伪造 identity 冲突（两组件同偏移段重叠）→ A3 触发 → 拒绝（rc!=0）。"""
        tmp = tempfile.TemporaryDirectory(prefix="t6_a3_")
        try:
            root = Path(tmp.name)
            # 两个不同组件，VGOffset 皆 0 → identity 0 冲突
            for sub, slot in (("hhhh7777-0-0", 0), ("iiii8888-0-0", 1)):
                write_submesh(root, "LOD0", sub, offset=0, vg_count=1,
                              vg_map={0: slot},
                              indices=[[0, _SENT, _SENT, _SENT]],
                              weights=[[1.0, 0, 0, 0]],
                              coords=[(0.0, 0.0, 0.0)])
            out = root / "out"
            rc = run_conversion(str(root), str(out))
            self.assertNotEqual(rc, 0, "identity 冲突必须 fail-closed（A3）")
            with open(out / "t5-summary.json", "r", encoding="utf-8") as f:
                summary = json.load(f)
            self.assertFalse(summary["assert_A3_identity_single"])
            self.assertFalse(summary["assert_A2_export_unique"],
                             "冲突数据下导出身份重复必须被检出（A2）")
        finally:
            tmp.cleanup()

    def test_duplicate_export_identity_detected(self):
        """B8 反向：冲突数据下 A2（重复导出身份）被检出，绝不静默输出重复索引。"""
        tmp = tempfile.TemporaryDirectory(prefix="t6_a2_")
        try:
            root = Path(tmp.name)
            # 两组件同 offset、同 identity → e(0)==e(1)==0 → 重复
            for sub, slot in (("jjjj9999-0-0", 0), ("kkkk0000-0-0", 1)):
                write_submesh(root, "LOD0", sub, offset=0, vg_count=1,
                              vg_map={0: slot},
                              indices=[[0, _SENT, _SENT, _SENT]],
                              weights=[[1.0, 0, 0, 0]],
                              coords=[(0.0, 0.0, 0.0)])
            out = make_outdir()
            before = snapshot_workspace(root)
            rc = run_conversion(str(root), str(out))
            after = snapshot_workspace(root)
            self.assertEqual(before, after)
            self.assertNotEqual(rc, 0)
            with open(out / "t5-summary.json", "r", encoding="utf-8") as f:
                summary = json.load(f)
            self.assertFalse(summary["assert_A2_export_unique"])
        finally:
            tmp.cleanup()


# ===========================================================================
# E. LOD0/LOD1 段作用域（spec B10/B11：段不相交时全局统一转换；撞车 fail-closed）
# ===========================================================================

class LodScopeTests(unittest.TestCase):
    """跨 LOD 段：正向（段不相交 → 全局唯一、无特判）+ 反向（撞车 → 规格要求 fail-closed）。"""

    def _disjoint_workspace(self):
        """段布局（与佩丽卡同构，段不相交）：
        LOD0 段：身份/slot [0,4) = cA[0,2) + cB[2,4)；LOD1 段 [4,7) = cC[4,6) + cD[6,7)。
        槽：0(cA:l0)、1(合并=cA:l1 身份1 + cB:l0 身份2)、3(cB:l1 身份3)、4(cC:l0)、5(合并=cC:l1 身份5 + cD:l0 身份6)。
        t18 v2：主度量=vertex_count——cB:l0 驱动 2 顶点（vc=2）> cA:l1（vc=1）→ e(1)=2；
        cD:l0 驱动 2 顶点（vc=2）> cC:l1（vc=1）→ e(5)=6。
        """
        root = Path(tempfile.mkdtemp(prefix="t6_lod_"))
        write_submesh(root, "LOD0", "aaaa1000-0-0", offset=0, vg_count=2,
                      vg_map={0: 0, 1: 1},
                      indices=[[1, _SENT, _SENT, _SENT], [0, _SENT, _SENT, _SENT]],
                      weights=[[1.0, 0, 0, 0], [1.0, 0, 0, 0]],
                      coords=[(0, 0, 0), (1, 0, 0)])
        # cB: local0 由 2 个顶点驱动（vc=2），local1 1 个（vc=1）
        write_submesh(root, "LOD0", "bbbb1000-0-0", offset=2, vg_count=2,
                      vg_map={0: 1, 1: 3},
                      indices=[[0, _SENT, _SENT, _SENT], [0, _SENT, _SENT, _SENT],
                               [1, _SENT, _SENT, _SENT]],
                      weights=[[1.5, 0, 0, 0], [0.5, 0, 0, 0], [0.2, 0, 0, 0]],
                      coords=[(2, 0, 0), (3, 0, 0), (4, 0, 0)])
        write_submesh(root, "LOD1", "cccc2000-0-0", offset=4, vg_count=2,
                      vg_map={0: 4, 1: 5},
                      indices=[[1, _SENT, _SENT, _SENT]],
                      weights=[[0.5, 0, 0, 0]],
                      coords=[(10, 0, 0)])
        # cD: local0 由 2 个顶点驱动（vc=2）> cC:l1（vc=1）→ e(5)=6
        write_submesh(root, "LOD1", "dddd2000-0-0", offset=6, vg_count=1,
                      vg_map={0: 5},
                      indices=[[0, _SENT, _SENT, _SENT], [0, _SENT, _SENT, _SENT]],
                      weights=[[1.0, 0, 0, 0], [0.5, 0, 0, 0]],
                      coords=[(11, 0, 0), (12, 0, 0)])
        return root

    def test_disjoint_segments_global_conversion(self):
        """B10/B11 正向：段不相交 → 全局统一转换、A2 全唯一、更名各段独立成立。"""
        root = self._disjoint_workspace()
        try:
            out = make_outdir()
            rc = run_conversion(str(root), str(out))
            self.assertEqual(rc, 0)
            with open(out / "t5-summary.json", "r", encoding="utf-8") as f:
                summary = json.load(f)
            self.assertEqual(summary["slots_total"], 5)
            self.assertEqual(summary["slots_merged"], 2, "槽 1（LOD0）、5（LOD1）为合并槽")
            self.assertEqual(summary["slots_renamed"], 2)
            self.assertTrue(summary["assert_A2_export_unique"],
                            "跨 LOD 段全局唯一性（推论 1）")
            self.assertTrue(summary["assert_A3_identity_single"],
                            "身份跨段单射（推论 2 前提）")
            with open(out / "t5-ledger.json", "r", encoding="utf-8") as f:
                ledger = json.load(f)
            self.assertEqual(ledger["1"]["export_identity"], 2,
                             "LOD0 槽 1：cA:l1(vc1)<cB:l0(vc2) → 顶点数多者身份 2")
            self.assertEqual(ledger["5"]["export_identity"], 6,
                             "LOD1 槽 5：cC:l1(vc1)<cD:l0(vc2) → 顶点数多者身份 6")
        finally:
            for p in sorted(root.rglob("*"), reverse=True):
                if p.is_file():
                    p.unlink()
                else:
                    p.rmdir()

    def test_cross_segment_slot_collision_must_fail_closed(self):
        """B10 反向：跨 LOD 段槽位号撞车 → 规格要求 fail-closed（不得静默合并）。"""
        root = Path(tempfile.mkdtemp(prefix="t6_collide_"))
        try:
            write_submesh(root, "LOD0", "aaaa3000-0-0", offset=0, vg_count=2,
                          vg_map={0: 0, 1: 3},
                          indices=[[1, _SENT, _SENT, _SENT], [0, _SENT, _SENT, _SENT]],
                          weights=[[1.0, 0, 0, 0], [1.0, 0, 0, 0]],
                          coords=[(0, 0, 0), (1, 0, 0)])
            write_submesh(root, "LOD1", "bbbb3000-0-0", offset=2, vg_count=1,
                          vg_map={0: 3},  # LOD1 也用槽位号 3（违反 v10/v11 段布局）
                          indices=[[0, _SENT, _SENT, _SENT]],
                          weights=[[1.0, 0, 0, 0]],
                          coords=[(5, 0, 0)])
            out = root / "out"
            rc = run_conversion(str(root), str(out))
            # 规格（t3 §5 B10）：跨段共享槽位号 ⇒ fail-closed、绝不静默合并。
            self.assertNotEqual(rc, 0, "跨 LOD 段槽位撞车必须 fail-closed（见 t6 报告 D2）")
        finally:
            for p in sorted(root.rglob("*"), reverse=True):
                if p.is_file():
                    p.unlink()
                else:
                    p.rmdir()


# ===========================================================================
# F. 强度数据不可得（spec §3.3/A4/B15）：拒绝转换，不静默回退槽位直写
# ===========================================================================

class StrengthUnavailableTests(unittest.TestCase):
    """B15：强度数据不可得（Position/Blend buf 缺失）→ 不得静默产出恒等直写。"""

    def test_b15_position_missing_must_refuse(self):
        """B15 正面（产品 A4）：强度数据不可得必须拒绝转换，不静默回退槽位直写。

        产品契约（run 层终态）：三种不可得形态——声明但文件物理缺失（dump 已删）、
        未声明 Position 类别（只有 Blend）、完全未声明任何强度类别——均 rc=1 +
        summary.assert_A4_strength_available=false + fail_reason 含「A4」；不抛异常、
        不静默取 0、不写坏工作区。本用例覆盖「声明完好但文件缺失（dump 已删）」。
        """
        root = Path(tempfile.mkdtemp(prefix="t6_b15p_"))
        try:
            write_submesh(root, "LOD0", "aaaa5000-0-0", offset=0, vg_count=2,
                          vg_map={0: 0, 1: 1},
                          indices=[[1, _SENT, _SENT, _SENT], [0, _SENT, _SENT, _SENT]],
                          weights=[[1.0, 0, 0, 0], [1.0, 0, 0, 0]],
                          coords=[(0, 0, 0), (1, 0, 0)],
                          with_position=True, with_blend=True)
            # 模拟 dump 已删：json 声明完好，但 Position/Blend.buf 物理缺失
            for blob_name in ("aaaa5000-0-0-Position.buf", "aaaa5000-0-0-Blend.buf"):
                p = root / "LOD0" / "aaaa5000-0-0" / "TYPE_GPU_TEST_" / blob_name
                if p.is_file():
                    p.unlink()
            before = snapshot_workspace(root)
            out = make_outdir()
            rc = run_conversion(str(root), str(out))
            after = snapshot_workspace(root)
            self.assertEqual(before, after, "拒绝路径不得改写工作区")
            self.assertNotEqual(rc, 0, "强度数据不可得必须拒绝转换（规格 A4）")
            with open(out / "t5-summary.json", "r", encoding="utf-8") as f:
                summary = json.load(f)
            self.assertFalse(summary["assert_A4_strength_available"],
                             "A4 标记必须置 False")
            self.assertIn("A4", summary["fail_reason"],
                          "fail_reason 必须提示强度数据不可得（A4）")
        finally:
            for p in sorted(root.rglob("*"), reverse=True):
                if p.is_file():
                    p.unlink()
                else:
                    p.rmdir()

    def test_b15_product_parity_missing_position(self):
        """B15 变体：Position 缺失时 weight_total 必须与产品一致（产品返回空表）。
        原型在缺失 Position 时继续计算（与产品不同）→ 记录为偏差 D1-ii。"""
        tmp = tempfile.TemporaryDirectory(prefix="t6_b15c_")
        try:
            root = Path(tmp.name)
            _, type_dir = write_submesh(
                root, "LOD0", "cccc6000-0-0", offset=0, vg_count=2,
                vg_map={0: 0, 1: 1},
                indices=[[1, _SENT, _SENT, _SENT], [0, _SENT, _SENT, _SENT]],
                weights=[[1.0, 0, 0, 0], [1.0, 0, 0, 0]],
                coords=[(0, 0, 0), (1, 0, 0)],
                with_position=False, with_blend=True)
            pos = str(type_dir / "cccc6000-0-0-Position.buf")  # 不存在
            blend = str(type_dir / "cccc6000-0-0-Blend.buf")
            with open(type_dir / "cccc6000-0-0.json", "r", encoding="utf-8") as f:
                data = json.load(f)
            product = ProductBuilder.compute_driven_signatures(pos, blend, data)
            proto = compute_weight_total(pos, blend, data)
            self.assertEqual(product, {}, "产品：任一 buf 缺失 → 空强度表（规格权威来源）")
            self.assertEqual(proto, {}, "原型必须与产品一致：缺失任何 buf → 空强度表（A4 语义）")
        finally:
            tmp.cleanup()


# ===========================================================================
# G. 嵌套合并：数据自洽下嵌套不可达（t3 §1.3 推论 2）；防御 = A2/A3；决策层无状态泄漏
# ===========================================================================

class NestedMergeTests(unittest.TestCase):
    """「源组本身是合并组」在数据模型上不可达；防御与独立性测试。"""

    def test_member_identity_equal_to_other_slot_number(self):
        """构造：某成员身份 == 另一槽位号（伪造嵌套）→ run 层 A2/A3 拒绝（fail-closed）。"""
        tmp = tempfile.TemporaryDirectory(prefix="t6_nest_")
        try:
            root = Path(tmp.name)
            # 组件 A：offset0，local0→槽0；组件 B：offset0（同 identity 0）——身份撞另一槽
            write_submesh(root, "LOD0", "aaaa7000-0-0", offset=0, vg_count=1,
                          vg_map={0: 0},
                          indices=[[0, _SENT, _SENT, _SENT]],
                          weights=[[1.0, 0, 0, 0]], coords=[(0.0, 0.0, 0.0)])
            write_submesh(root, "LOD0", "bbbb7000-0-0", offset=0, vg_count=1,
                          vg_map={0: 1},
                          indices=[[0, _SENT, _SENT, _SENT]],
                          weights=[[1.0, 0, 0, 0]], coords=[(1.0, 0.0, 0.0)])
            out = root / "out"
            rc = run_conversion(str(root), str(out))
            self.assertNotEqual(rc, 0, "伪造嵌套（身份==他槽槽位号）必须被 A2/A3 拒绝")
        finally:
            tmp.cleanup()

    def test_decision_layer_no_cross_slot_state(self):
        """正测：决策层对「成员身份恰为另一槽位号」的数据无状态泄漏（逐槽独立）。"""
        table = build_export_table({
            0: [member(0, 1.0)],
            2: [member(2, 1.0, vertex_count=1), member(5, 9.0, vertex_count=9)],
        })
        self.assertEqual(table[0]["export_identity"], 0)
        self.assertEqual(table[2]["export_identity"], 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)