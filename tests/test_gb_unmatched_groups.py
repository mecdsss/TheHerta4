# -*- coding: utf-8 -*-
"""高斯权重球：未匹配/缺失顶点组的显式补权（toolkit/gb_resolve.py）单元测试。

覆盖：
- 会话方向判定（Source_*/Target_* 调试物体 × AUTO/REVERSE 请求）；
- 写入策略门控（正向自动建组 vs 反向显式开启才建组——R5 防误写保护）；
- 合法缺失（缺组）与匹配失败（无源物体）在解析层的区分；
- 缺组时显式创建补权的完整链路（策略 + 写入）。

不依赖 bpy，直接按路径加载 gb_resolve.py。
"""

import importlib
import sys
import types
import unittest
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_gb_resolve():
    """在 stub 包命名空间下加载 gb_resolve（其 `from . import gb_core`
    相对导入需要包上下文；gb_core 为纯 numpy，无需 bpy）。"""
    pkg = "_gb_unmatched_pkg"
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
# 最小 fake（同 test_gb_write_targets.py 的语义）
# ---------------------------------------------------------------------------

class _FakeGroupElem:
    def __init__(self, group, weight):
        self.group = group
        self.weight = weight


class _FakeVert:
    def __init__(self, groups=None):
        self.groups = list(groups or [])


class _FakeVG:
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
    def __init__(self, name="obj", n_verts=0, vg_names=()):
        self.name = name
        self.type = "MESH"
        self.data = _MeshData(n_verts)
        self.vg_col = _VGCollection(self, vg_names)

    @property
    def vertex_groups(self):
        return self.vg_col

    def weights_of(self, vg_name):
        vg = self.vertex_groups.get(vg_name)
        if vg is None:
            return {}
        return {i: g.weight for i, v in enumerate(self.data.vertices)
                for g in v.groups if g.group == vg.index}


class DecideDirectionTests(unittest.TestCase):
    def test_source_marker_always_forward(self):
        self.assertEqual(
            gb_resolve.decide_direction("Source_Bone", "AUTO"),
            gb_resolve.DIRECTION_FORWARD)
        # 反向请求对 Source_ 无意义，钳制为正向
        self.assertEqual(
            gb_resolve.decide_direction("Source_Bone", "REVERSE"),
            gb_resolve.DIRECTION_FORWARD)

    def test_target_marker_auto_is_self(self):
        self.assertEqual(
            gb_resolve.decide_direction("Target_Bone", "AUTO"),
            gb_resolve.DIRECTION_SELF)

    def test_target_marker_reverse_is_reverse(self):
        self.assertEqual(
            gb_resolve.decide_direction("Target_Bone", "REVERSE"),
            gb_resolve.DIRECTION_REVERSE)

    def test_is_reverse_request(self):
        self.assertTrue(
            gb_resolve.is_reverse_request("Target_Bone", "REVERSE"))
        self.assertFalse(
            gb_resolve.is_reverse_request("Target_Bone", "AUTO"))
        self.assertFalse(
            gb_resolve.is_reverse_request("Source_Bone", "REVERSE"))

    def test_unknown_prefix_falls_to_self(self):
        self.assertEqual(
            gb_resolve.decide_direction("Whatever", "AUTO"),
            gb_resolve.DIRECTION_SELF)


class WritePolicyTests(unittest.TestCase):
    """R5 防误写门控 + NW2 显式补权门控（纯逻辑，不依赖会话）。"""

    def test_forward_exact_lookup_auto_create(self):
        # 正向写目标侧：精确名查找（目标侧不猜 `=` 名），缺组自动创建
        p = gb_resolve.write_policy(gb_resolve.DIRECTION_FORWARD, False)
        self.assertEqual(p["role"], gb_resolve.ROLE_ANY)
        self.assertTrue(p["allow_create"])

    def test_self_mode_treats_as_forward(self):
        p = gb_resolve.write_policy(gb_resolve.DIRECTION_SELF, False)
        self.assertEqual(p["role"], gb_resolve.ROLE_ANY)
        self.assertTrue(p["allow_create"])

    def test_reverse_uses_target_name_perspective(self):
        # 反向写源侧：以目标组名（`源名=目标名` 右部）命中被改名的源侧组
        p = gb_resolve.write_policy(gb_resolve.DIRECTION_REVERSE, False)
        self.assertEqual(p["role"], gb_resolve.ROLE_TARGET)
        self.assertFalse(p["allow_create"])

    def test_reverse_with_explicit_create(self):
        p = gb_resolve.write_policy(gb_resolve.DIRECTION_REVERSE, True)
        self.assertEqual(p["role"], gb_resolve.ROLE_TARGET)
        self.assertTrue(p["allow_create"])


class LegalMissingNotMatchFailureTests(unittest.TestCase):
    """合法缺失（缺组）≠ 匹配失败（无源/无目标）——解析层区分。"""

    def test_no_source_objects_is_match_failure(self):
        # 反向解析无源物体：kind=none，调用方报“匹配/合集失效”，不按缺组处理
        res = gb_resolve.resolve_reverse_targets([])
        self.assertEqual(res["kind"], "none")

    def test_missing_group_is_legal_missing(self):
        a = _FakeObj("src_a", vg_names=("Bone",))
        b = _FakeObj("src_b")
        info = gb_resolve.classify_group_presence(
            [a, b], "Bone", role=gb_resolve.ROLE_SOURCE)
        self.assertEqual(len(info["present"]), 1)
        self.assertEqual(len(info["missing"]), 1)
        self.assertEqual(info["total"], 2)

    def test_explicit_create_backfills_missing_group(self):
        # 反向 + 显式创建：缺组按高斯球范围补权成功
        obj = _FakeObj("src_b", n_verts=2)
        policy = gb_resolve.write_policy(
            gb_resolve.DIRECTION_REVERSE, create_missing=True)
        res = gb_resolve.write_field_to_object(
            obj, "Bone", np.array([0.6, 0.3]),
            role=policy["role"], create_missing=policy["allow_create"])
        self.assertEqual(res["reason"], "")
        self.assertTrue(res["created"])
        got = obj.weights_of("Bone")
        self.assertAlmostEqual(got[0], 0.6, places=6)
        self.assertAlmostEqual(got[1], 0.3, places=6)

    def test_reverse_without_explicit_skips_missing(self):
        # 反向未显式开启：缺组跳过（不静默创建用户数据），reason=no_group
        obj = _FakeObj("src_b", n_verts=2)
        policy = gb_resolve.write_policy(
            gb_resolve.DIRECTION_REVERSE, create_missing=False)
        res = gb_resolve.write_field_to_object(
            obj, "Bone", np.array([0.6, 0.3]),
            role=policy["role"], create_missing=policy["allow_create"])
        self.assertEqual(res["reason"], "no_group")
        self.assertIsNone(obj.vertex_groups.get("Bone"))

    def test_forward_auto_creates_on_target_side(self):
        # 正向写目标侧：保持既有“目标组缺失自动创建”行为
        obj = _FakeObj("tgt", n_verts=2)
        policy = gb_resolve.write_policy(gb_resolve.DIRECTION_FORWARD, False)
        res = gb_resolve.write_field_to_object(
            obj, "Bone", np.array([0.6, 0.0]),
            role=policy["role"], create_missing=policy["allow_create"])
        self.assertEqual(res["reason"], "")
        self.assertTrue(res["created"])

    def test_reverse_multi_object_partial_distribution(self):
        # 源合集多物体分发：有组物体命中 `=` 组、缺组物体显式新建
        a = _FakeObj("src_a", n_verts=2, vg_names=("Leg_L=Bone",))
        b = _FakeObj("src_b", n_verts=2)
        policy = gb_resolve.write_policy(
            gb_resolve.DIRECTION_REVERSE, create_missing=True)
        field = np.array([0.8, 0.4])
        ra = gb_resolve.write_field_to_object(
            a, "Bone", field, role=policy["role"],
            create_missing=policy["allow_create"])
        rb = gb_resolve.write_field_to_object(
            b, "Bone", field, role=policy["role"],
            create_missing=policy["allow_create"])
        # a 命中既有 `源名=目标名` 组（右部匹配），不新建；b 显式新建
        self.assertEqual(ra["reason"], "")
        self.assertFalse(ra["created"])
        self.assertEqual(rb["reason"], "")
        self.assertTrue(rb["created"])
        self.assertAlmostEqual(a.weights_of("Leg_L=Bone")[0], 0.8, places=6)
        self.assertAlmostEqual(b.weights_of("Bone")[0], 0.8, places=6)


if __name__ == "__main__":
    unittest.main()