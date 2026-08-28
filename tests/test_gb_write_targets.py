# -*- coding: utf-8 -*-
"""高斯权重球反向解析与写入（toolkit/gb_resolve.py）的纯逻辑单元测试。

覆盖：
- 顶点组 `源名=目标名` 重命名格式的角色感知查找（精确名优先 + 剥前缀）；
- 反向写入目标解析（单源物体 / 源合集多物体分发 / 排除临时合并物体）；
- 单物体权重写入（REPLACE/EPS/min 钳制/球外保留与球外清零/显式建组/
  拓扑守卫/空场跳过）；
- 缺组分类（区分“匹配关系失效”与“合法缺失可补权”）。

不依赖 bpy，直接按路径加载 gb_resolve.py 即可运行。
"""

import importlib
import importlib.util
import sys
import types
import unittest
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_gb_resolve():
    """在 stub 包命名空间下加载 gb_resolve（其 `from . import gb_core`
    相对导入需要包上下文；gb_core 为纯 numpy，无需 bpy）。"""
    pkg = "_gb_resolve_pkg"
    tk_name = f"{pkg}.toolkit"
    if tk_name not in sys.modules:
        root_mod = types.ModuleType(pkg)
        root_mod.__path__ = []
        sys.modules[pkg] = root_mod
        tk = types.ModuleType(tk_name)
        tk.__path__ = [str(_REPO_ROOT / "toolkit")]
        sys.modules[tk_name] = tk
    return importlib.import_module(f"{tk_name}.gb_resolve")


gb_resolve = _load_gb_resolve()


# ---------------------------------------------------------------------------
# 最小 fake 对象（模拟 bpy 顶点组语义：get/new/add/remove/迭代）
# ---------------------------------------------------------------------------

class _FakeGroupElem:
    def __init__(self, group, weight):
        self.group = group
        self.weight = weight


class _FakeVert:
    def __init__(self, groups=None):
        self.groups = list(groups or [])


class _FakeVG:
    """模拟 bpy.types.VertexGroup（add/remove 直接改写宿主网格权重）。"""

    def __init__(self, name, index, owner):
        self.name = name
        self.index = index
        self.lock_weight = False
        self._owner = owner

    def add(self, indices, weight, mode='ADD'):
        for i in indices:
            vert = self._owner.data.vertices[i]
            vert.groups = [g for g in vert.groups if g.group != self.index]
            vert.groups.append(_FakeGroupElem(self.index, weight))

    def remove(self, indices):
        for i in indices:
            vert = self._owner.data.vertices[i]
            vert.groups = [g for g in vert.groups if g.group != self.index]


class _VGCollection:
    """模拟 obj.vertex_groups：get/new/迭代/len。"""

    def __init__(self, owner, names=()):
        self._owner = owner
        self._vgs = [_FakeVG(n, i, owner) for i, n in enumerate(names)]

    def get(self, name, default=None):
        for vg in self._vgs:
            if vg.name == name:
                return vg
        return default

    def new(self, name=""):
        candidate, n = name, 1
        while self.get(candidate) is not None:
            candidate = f"{name}.{n:03d}"
            n += 1
        vg = _FakeVG(candidate, len(self._vgs), self._owner)
        self._vgs.append(vg)
        return vg

    def __iter__(self):
        return iter(self._vgs)

    def __len__(self):
        return len(self._vgs)


class _MeshData:
    def __init__(self, n_verts):
        self.vertices = [_FakeVert() for _ in range(n_verts)]


class _FakeObj:
    """带 vertex_groups 与 data.vertices 的网格物体。"""

    def __init__(self, name="obj", n_verts=0, vg_names=()):
        self.name = name
        self.type = "MESH"
        self.data = _MeshData(n_verts)
        self.vg_col = _VGCollection(self, vg_names)

    @property
    def vertex_groups(self):
        return self.vg_col

    def set_weight(self, vg_name, indices, weight):
        vg = self.vertex_groups.get(vg_name)
        if vg is None:
            vg = self.vertex_groups.new(name=vg_name)
        vg.add(list(indices), weight, 'REPLACE')

    def members_of(self, vg_name):
        """返回该组当前成员顶点索引集合。"""
        vg = self.vertex_groups.get(vg_name)
        if vg is None:
            return set()
        return {i for i, v in enumerate(self.data.vertices)
                for g in v.groups if g.group == vg.index}

    def weights_of(self, vg_name):
        """按顶点索引返回该组权重 dict。"""
        vg = self.vertex_groups.get(vg_name)
        if vg is None:
            return {}
        return {i: g.weight for i, v in enumerate(self.data.vertices)
                for g in v.groups if g.group == vg.index}


class _FakeParent:
    def __init__(self, runtime_name=""):
        self._d = {"vgtp_runtime_source_object": runtime_name}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def __getitem__(self, key):
        return self._d[key]


# ---------------------------------------------------------------------------
# 查找：`源名=目标名` 角色感知
# ---------------------------------------------------------------------------

class FindVertexGroupTests(unittest.TestCase):
    def test_exact_name_wins(self):
        obj = _FakeObj(vg_names=("Bone", "A=B"))
        vg = gb_resolve.find_vertex_group(obj, "A=B", role=gb_resolve.ROLE_SOURCE)
        self.assertIsNotNone(vg)
        self.assertEqual(vg.name, "A=B")

    def test_source_role_strips_left_part(self):
        # 源侧组被 “应用到原物体” 改名为 `源名=目标名`
        obj = _FakeObj(vg_names=("Leg_L=Bone_L",))
        vg = gb_resolve.find_vertex_group(
            obj, "Leg_L", role=gb_resolve.ROLE_SOURCE)
        self.assertIsNotNone(vg)
        self.assertEqual(vg.name, "Leg_L=Bone_L")

    def test_target_role_strips_right_part(self):
        # 反向写入：以目标组名在源侧查找 `源名=目标名` 组
        obj = _FakeObj(vg_names=("Leg_L=Bone_L",))
        vg = gb_resolve.find_vertex_group(
            obj, "Bone_L", role=gb_resolve.ROLE_TARGET)
        self.assertIsNotNone(vg)
        self.assertEqual(vg.name, "Leg_L=Bone_L")

    def test_role_any_exact_only(self):
        obj = _FakeObj(vg_names=("Leg_L=Bone_L",))
        self.assertIsNone(gb_resolve.find_vertex_group(obj, "Leg_L"))
        self.assertIsNone(gb_resolve.find_vertex_group(obj, "Bone_L"))

    def test_missing_returns_none(self):
        obj = _FakeObj(vg_names=("Bone",))
        self.assertIsNone(gb_resolve.find_vertex_group(
            obj, "Nope", role=gb_resolve.ROLE_SOURCE))

    def test_first_candidate_deterministic(self):
        # 多个 `=` 候选时取组顺序第一个（确定性）
        obj = _FakeObj(vg_names=("A=B", "A=C"))
        vg = gb_resolve.find_vertex_group(
            obj, "A", role=gb_resolve.ROLE_SOURCE)
        self.assertEqual(vg.name, "A=B")

    def test_no_vertex_groups_returns_none(self):
        obj = _FakeObj(n_verts=2)
        self.assertIsNone(gb_resolve.find_vertex_group(obj, "X"))


class ReadGroupWeightsTests(unittest.TestCase):
    def test_reads_weights(self):
        obj = _FakeObj(n_verts=3, vg_names=("Bone",))
        obj.set_weight("Bone", [0, 2], 0.7)
        vg, weights, matched = gb_resolve.read_group_weights(obj, "Bone")
        self.assertEqual(vg.name, "Bone")
        np.testing.assert_allclose(weights, [0.7, 0.0, 0.7])
        self.assertEqual(matched, "Bone")

    def test_read_with_source_role_renamed(self):
        obj = _FakeObj(n_verts=2, vg_names=("A=B",))
        obj.set_weight("A=B", [1], 0.5)
        vg, weights, matched = gb_resolve.read_group_weights(
            obj, "A", role=gb_resolve.ROLE_SOURCE)
        self.assertEqual(matched, "A=B")
        np.testing.assert_allclose(weights, [0.0, 0.5])

    def test_missing_returns_none_tuple(self):
        obj = _FakeObj(n_verts=2, vg_names=("Bone",))
        vg, weights, matched = gb_resolve.read_group_weights(obj, "Nope")
        self.assertIsNone(vg)
        self.assertIsNone(weights)
        self.assertIsNone(matched)


# ---------------------------------------------------------------------------
# 反向写入目标解析
# ---------------------------------------------------------------------------

class ResolveReverseTargetsTests(unittest.TestCase):
    def _mesh(self, name):
        return _FakeObj(name=name, n_verts=4)

    def test_single_source_object(self):
        a = self._mesh("src_a")
        res = gb_resolve.resolve_reverse_targets([a])
        self.assertEqual(res["kind"], "single")
        self.assertEqual([o.name for o in res["objects"]], ["src_a"])

    def test_collection_multi_object_distribution(self):
        a, b, c = self._mesh("src_a"), self._mesh("src_b"), self._mesh("src_c")
        res = gb_resolve.resolve_reverse_targets([a, b, c])
        self.assertEqual(res["kind"], "collection")
        self.assertEqual([o.name for o in res["objects"]],
                         ["src_a", "src_b", "src_c"])

    def test_order_preserved_and_deduped(self):
        a, b = self._mesh("src_a"), self._mesh("src_b")
        res = gb_resolve.resolve_reverse_targets([b, a, b])
        self.assertEqual([o.name for o in res["objects"]], ["src_b", "src_a"])

    def test_non_mesh_excluded(self):
        a = self._mesh("src_a")
        arm = _FakeObj(name="armature", n_verts=0)
        arm.type = "ARMATURE"
        res = gb_resolve.resolve_reverse_targets([a, arm])
        self.assertEqual([o.name for o in res["objects"]], ["src_a"])

    def test_runtime_temp_object_excluded(self):
        # 临时合并物体（匹配计算用拷贝）不能成为反向写入目标
        a = self._mesh("src_a")
        runtime = self._mesh("SSMT_VGMatchRuntime_Tree_Node")
        res = gb_resolve.resolve_reverse_targets(
            [a, runtime], exclude_names=[runtime.name])
        self.assertEqual([o.name for o in res["objects"]], ["src_a"])

    def test_empty_source_objects(self):
        res = gb_resolve.resolve_reverse_targets([])
        self.assertEqual(res["kind"], "none")
        self.assertEqual(res["objects"], [])

    def test_none_entries_skipped(self):
        a = self._mesh("src_a")
        res = gb_resolve.resolve_reverse_targets([a, None])
        self.assertEqual([o.name for o in res["objects"]], ["src_a"])


# ---------------------------------------------------------------------------
# 单物体权重写入
# ---------------------------------------------------------------------------

class WriteFieldToObjectTests(unittest.TestCase):
    def test_replace_writes_only_inside_ball(self):
        obj = _FakeObj(n_verts=4, vg_names=("Bone",))
        obj.set_weight("Bone", [0, 1], 0.9)  # 球外顶点 1 保留原值
        field = np.array([0.8, 0.0, 0.5, 0.2])
        res = gb_resolve.write_field_to_object(obj, "Bone", field)
        self.assertEqual(res["reason"], "")
        self.assertEqual(res["written"], 3)
        got = obj.weights_of("Bone")
        self.assertAlmostEqual(got[0], 0.8, places=6)   # 球内覆写
        self.assertAlmostEqual(got[1], 0.9, places=6)   # 球外保留原值
        self.assertAlmostEqual(got[2], 0.5, places=6)
        self.assertAlmostEqual(got[3], 0.2, places=6)

    def test_min_clamp_to_one(self):
        obj = _FakeObj(n_verts=1, vg_names=("Bone",))
        res = gb_resolve.write_field_to_object(obj, "Bone", np.array([3.0]))
        self.assertEqual(res["written"], 1)
        self.assertAlmostEqual(obj.weights_of("Bone")[0], 1.0, places=6)

    def test_eps_threshold_boundary(self):
        obj = _FakeObj(n_verts=2, vg_names=("Bone",))
        res = gb_resolve.write_field_to_object(
            obj, "Bone", np.array([1e-4, 1e-4 + 1e-9]))
        # 恰等于 eps 不写，略大于 eps 写
        self.assertEqual(res["written"], 1)

    def test_create_missing_group(self):
        obj = _FakeObj(n_verts=2)
        res = gb_resolve.write_field_to_object(
            obj, "NewGroup", np.array([0.5, 0.0]), create_missing=True)
        self.assertTrue(res["created"])
        self.assertEqual(res["written"], 1)
        self.assertIsNotNone(obj.vertex_groups.get("NewGroup"))

    def test_no_group_skipped_without_create(self):
        obj = _FakeObj(n_verts=2)
        res = gb_resolve.write_field_to_object(
            obj, "NewGroup", np.array([0.5, 0.0]), create_missing=False)
        self.assertEqual(res["reason"], "no_group")
        self.assertIsNone(obj.vertex_groups.get("NewGroup"))

    def test_write_hits_renamed_source_group_by_role(self):
        # 反向写入：目标组名 → 源侧 `源名=目标名` 组（右部=目标名命中，不新建）
        obj = _FakeObj(n_verts=2, vg_names=("Leg_L=Bone_L",))
        obj.set_weight("Leg_L=Bone_L", [0], 0.1)
        res = gb_resolve.write_field_to_object(
            obj, "Bone_L", np.array([0.9, 0.0]),
            role=gb_resolve.ROLE_TARGET, create_missing=True)
        self.assertEqual(res["reason"], "")
        self.assertFalse(res["created"])  # 命中既有 `=` 组，不建新组
        self.assertEqual(len(obj.vertex_groups), 1)
        self.assertAlmostEqual(obj.weights_of("Leg_L=Bone_L")[0], 0.9, places=6)

    def test_clear_outside_removes_outside_vertices(self):
        obj = _FakeObj(n_verts=4, vg_names=("Bone",))
        obj.set_weight("Bone", [0, 1, 2, 3], 0.5)
        field = np.array([0.8, 0.0, 0.5, 0.0])
        res = gb_resolve.write_field_to_object(
            obj, "Bone", field, clear_outside=True)
        self.assertEqual(res["written"], 2)
        self.assertEqual(obj.members_of("Bone"), {0, 2})
        self.assertAlmostEqual(obj.weights_of("Bone")[0], 0.8, places=6)
        self.assertAlmostEqual(obj.weights_of("Bone")[2], 0.5, places=6)

    def test_clear_outside_default_preserves(self):
        obj = _FakeObj(n_verts=2, vg_names=("Bone",))
        obj.set_weight("Bone", [1], 0.4)
        res = gb_resolve.write_field_to_object(obj, "Bone", np.array([0.8, 0.0]))
        self.assertEqual(obj.members_of("Bone"), {0, 1})  # 球外顶点保留成员关系

    def test_empty_field_skips_everything(self):
        obj = _FakeObj(n_verts=3, vg_names=("Bone",))
        obj.set_weight("Bone", [0, 1, 2], 0.4)
        res = gb_resolve.write_field_to_object(
            obj, "Bone", np.zeros(3), clear_outside=True)
        self.assertEqual(res["reason"], "empty_field")
        self.assertEqual(res["written"], 0)
        self.assertEqual(obj.members_of("Bone"), {0, 1, 2})  # 空场不写也不清

    def test_topology_mismatch_skips(self):
        obj = _FakeObj(n_verts=4, vg_names=("Bone",))
        obj.set_weight("Bone", [0, 1, 2, 3], 0.4)
        res = gb_resolve.write_field_to_object(
            obj, "Bone", np.array([0.5, 0.5, 0.5]))  # 3 顶点场 vs 4 顶点物体
        self.assertEqual(res["reason"], "topology_mismatch")
        self.assertEqual(res["written"], 0)
        self.assertEqual(obj.members_of("Bone"), {0, 1, 2, 3})

    def test_result_dict_shape(self):
        obj = _FakeObj(n_verts=1, vg_names=("Bone",))
        res = gb_resolve.write_field_to_object(obj, "Bone", np.array([0.5]))
        self.assertEqual(set(res.keys()), {"written", "created", "reason"})
        self.assertEqual(res["reason"], "")


# ---------------------------------------------------------------------------
# 缺组分类（合法缺失 vs 匹配失败）
# ---------------------------------------------------------------------------

class ClassifyGroupPresenceTests(unittest.TestCase):
    def test_mixed_presence(self):
        a = _FakeObj("src_a", vg_names=("Bone",))
        b = _FakeObj("src_b")
        c = _FakeObj("src_c", vg_names=("Bone",))
        info = gb_resolve.classify_group_presence(
            [a, b, c], "Bone", role=gb_resolve.ROLE_SOURCE)
        self.assertEqual([o.name for o in info["present"]], ["src_a", "src_c"])
        self.assertEqual([o.name for o in info["missing"]], ["src_b"])
        self.assertEqual(info["total"], 3)

    def test_all_missing(self):
        a = _FakeObj("src_a")
        info = gb_resolve.classify_group_presence([a], "Nope")
        self.assertEqual(info["present"], [])
        self.assertEqual(info["missing"], [a])
        self.assertEqual(info["total"], 1)

    def test_role_aware_partial(self):
        # 源侧 `源名=目标名` 组对目标组名（右部）可见
        a = _FakeObj("src_a", vg_names=("Leg_L=Bone_L",))
        b = _FakeObj("src_b")
        info = gb_resolve.classify_group_presence(
            [a, b], "Bone_L", role=gb_resolve.ROLE_TARGET)
        self.assertEqual(info["present"], [a])
        self.assertEqual(info["missing"], [b])


if __name__ == "__main__":
    unittest.main()