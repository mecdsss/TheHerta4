# -*- coding: utf-8 -*-
"""t15 独立复核：get_dualset_export_table_cached（t14 导出缓存修复）——缓存正确性/失效边界。

覆盖（t15 任务核验点 1/2）：
1. 指纹失效：工作区任一 json 内容变化（mtime_ns+size 摘要变化）→ 下次调用重建且反映新数据；
2. 同指纹一致性：同工作区重复调用返回缓存表（同一对象），且逐槽与无缓存 direct build 一致；
3. 失效边界：导出会话中途外部增/改 json → 必须重建（安全，不用旧表）；
4. 缓存隔离：不同工作区互不串扰；缓存键按 workspace_root。
5. 卫生：每个用例前清空类级缓存（_dualset_table_cache），避免跨用例污染。

注：recompute_strength 参数未纳入缓存键的问题不在本文件（见 t15 报告 F1，探针
.dbg/t15_verify.py 单独取证）。
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
PKG = "dualset_cache_test_pkg"

_SENT = 0xFFFFFFFF

for _n in (PKG, f"{PKG}.common", f"{PKG}.utils"):
    m = types.ModuleType(_n)
    m.__path__ = []
    sys.modules[_n] = m


def _load_real(qualname, relpath):
    spec = importlib.util.spec_from_file_location(qualname, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


_load_real(f"{PKG}.utils.json_utils", "utils/json_utils.py")
_efmi = _load_real(f"{PKG}.common.efmi_skeleton", "common/efmi_skeleton.py")
Builder = _efmi.EFMIBoneMapBuilder


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


def write_submesh(root, sub, offset, vg_count, vg_map, indices, weights, coords):
    td = Path(root) / "LOD0" / sub / "TYPE_GPU_TEST_"
    td.mkdir(parents=True, exist_ok=True)
    payload = {
        "GamePreset": "EFMI", "CategoryBufferList": [
            {"FileName": f"{sub}-Position.buf", "Type": "VS", "D3D11ElementList": [
                {"SemanticName": "POSITION", "SemanticIndex": 0, "Format": "R32G32B32A32_FLOAT",
                 "ByteWidth": 12, "AlignedByteOffset": 0, "Category": "Position"}]},
            {"FileName": f"{sub}-Blend.buf", "Type": "VS", "D3D11ElementList": [
                {"SemanticName": "BLENDINDICES", "SemanticIndex": 0, "Format": "R32G32B32A32_UINT",
                 "ByteWidth": 16, "AlignedByteOffset": 0, "Category": "Blend"},
                {"SemanticName": "BLENDWEIGHT", "SemanticIndex": 0, "Format": "R32G32B32A32_FLOAT",
                 "ByteWidth": 16, "AlignedByteOffset": 16, "Category": "Blend"}]}],
        "VGOffset": offset, "VGCount": vg_count,
        "VGMap": {str(k): v for k, v in sorted(vg_map.items())},
        "VGMapAlgorithmVersion": 99, "VGMapDedupEnabled": True,
    }
    (td / f"{sub}.json").write_text(json.dumps(payload), encoding="utf-8")
    (td / f"{sub}-Position.buf").write_bytes(pos_blob(coords))
    (td / f"{sub}-Blend.buf").write_bytes(blend_blob(indices, weights))
    return td


def clear_cache():
    Builder._dualset_table_cache.clear()


class CacheFreshnessTests(unittest.TestCase):
    """核验点 1：指纹变化 → 重建且反映新数据。"""

    def setUp(self):
        clear_cache()
        self.tmp = tempfile.TemporaryDirectory(prefix="t15_fresh_")
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _base(self):
        write_submesh(self.root, "aaaa1000-0-0", 0, 2, {0: 0, 1: 1},
                      [[1, _SENT, _SENT, _SENT], [0, _SENT, _SENT, _SENT]],
                      [[0.5, 0, 0, 0], [1.0, 0, 0, 0]], [(0, 0, 0), (1, 0, 0)])
        # t18 v2：cB local0 由 2 顶点驱动（vc=2）> cA local1（vc=1）→ 槽1 e=2（更名）
        write_submesh(self.root, "bbbb1000-0-0", 2, 2, {0: 1, 1: 3},
                      [[0, _SENT, _SENT, _SENT], [0, _SENT, _SENT, _SENT],
                       [1, _SENT, _SENT, _SENT]],
                      [[1.5, 0, 0, 0], [0.5, 0, 0, 0], [0.2, 0, 0, 0]],
                      [(2, 0, 0), (3, 0, 0), (4, 0, 0)])

    def test_rebuild_on_json_change(self):
        """改一个 json（VGMap 变更导致新合并/更名语义）→ 缓存失效重建且反映新数据。"""
        self._base()
        t1 = Builder.get_dualset_export_table_cached(str(self.root))
        self.assertEqual(t1[1]["export_identity"], 2, "前置：槽1 e=2（更名）")
        # 外部改动：把 bbbb 的 VGMap 改掉（local0→槽 9 新槽），并附一新子网格
        # （VGOffset=9 使槽 9 落在全池声明段并集内，B11/FC-3 合法——引用
        # 「无人声明」槽位属损坏数据，会 fail-closed，不是本缓存用例范围）。
        td = self.root / "LOD0" / "bbbb1000-0-0" / "TYPE_GPU_TEST_"
        p = td / "bbbb1000-0-0.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        d["VGMap"] = {"0": 9, "1": 3}
        p.write_text(json.dumps(d), encoding="utf-8")
        write_submesh(self.root, "cccc2000-0-0", 9, 1, {0: 9},
                      [[0, _SENT, _SENT, _SENT]], [[2.0, 0, 0, 0]], [(10, 0, 0)])
        t2 = Builder.get_dualset_export_table_cached(str(self.root))
        self.assertIsNot(t2, t1, "指纹变化必须重建（不返回旧表对象）")
        self.assertNotIn(9, t1, "旧表不应包含新槽 9")
        self.assertIn(9, t2, "新表必须包含外部新增的槽 9")
        self.assertEqual(t2[1]["export_identity"], 1,
                         "槽 1 只剩 aaaa local1 → 单源恒等（新数据反射）")

    def test_add_json_file_rebuilds(self):
        """外部新增 json → 指纹变化 → 重建并纳入。"""
        self._base()
        t1 = Builder.get_dualset_export_table_cached(str(self.root))
        # dddd 引用槽 10/11：新增 eeee[10,11) 与 ffff[11,12) 把声明段精确并集
        # 覆盖到两个槽（B11/FC-3 逐段判定——槽必须落在某声明段内；越段/凹槽
        # 引用属损坏数据 fail-closed，T6-F3）。
        write_submesh(self.root, "dddd3000-0-0", 4, 2, {0: 10, 1: 11},
                      [[0, _SENT, _SENT, _SENT], [0, _SENT, _SENT, _SENT],
                       [1, _SENT, _SENT, _SENT]],
                      [[1.0, 0, 0, 0], [1.0, 0, 0, 0], [1.0, 0, 0, 0]],
                      [(12, 0, 0), (13, 0, 0), (14, 0, 0)])
        write_submesh(self.root, "eeee4000-0-0", 10, 1, {0: 10},
                      [[0, _SENT, _SENT, _SENT]], [[1.0, 0, 0, 0]], [(20, 0, 0)])
        write_submesh(self.root, "ffff5000-0-0", 11, 1, {0: 11},
                      [[0, _SENT, _SENT, _SENT]], [[1.0, 0, 0, 0]], [(30, 0, 0)])
        t2 = Builder.get_dualset_export_table_cached(str(self.root))
        self.assertIsNot(t2, t1)
        self.assertIn(10, t2)
        self.assertIn(11, t2)
        self.assertEqual(t2[10]["export_identity"], 4,
                         "槽 10 合并槽：e(s)=成员身份（dddd VGOffset4+local0，"
                         "顶点数 2 > eeee 的 1），非槽位号 10")


class CacheConsistencyTests(unittest.TestCase):
    """核验点 1（同指纹一致）+ 3（缓存命中语义）：与无缓存 build 逐槽一致。"""

    def setUp(self):
        clear_cache()
        self.tmp = tempfile.TemporaryDirectory(prefix="t15_consist_")
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _ws(self):
        write_submesh(self.root, "aaaa1000-0-0", 0, 2, {0: 0, 1: 1},
                      [[1, _SENT, _SENT, _SENT], [0, _SENT, _SENT, _SENT]],
                      [[0.5, 0, 0, 0], [1.0, 0, 0, 0]], [(0, 0, 0), (1, 0, 0)])
        write_submesh(self.root, "bbbb1000-0-0", 2, 2, {0: 1, 1: 3},
                      [[0, _SENT, _SENT, _SENT], [1, _SENT, _SENT, _SENT]],
                      [[1.5, 0, 0, 0], [0.2, 0, 0, 0]], [(2, 0, 0), (3, 0, 0)])

    def test_cached_equals_direct_and_hits(self):
        """同指纹重复调用 = 同一对象（命中缓存）；与 direct build 逐槽一致。"""
        self._ws()
        direct = Builder.build_dualset_export_table(str(self.root))
        c1 = Builder.get_dualset_export_table_cached(str(self.root))
        c2 = Builder.get_dualset_export_table_cached(str(self.root))
        self.assertIs(c1, c2, "无变化重复调用必须命中缓存（同一表对象）")
        self.assertEqual(set(c1), set(direct))
        for slot, row in direct.items():
            self.assertEqual(c1[slot]["export_identity"], row["export_identity"], f"槽 {slot} e(s)")
            self.assertEqual(c1[slot]["renamed"], row["renamed"], f"槽 {slot} renamed")
            self.assertEqual(
                [(m["comp"], m["local"], m["identity"], round(float(m["weight_total"]), 6))
                 for m in c1[slot]["members"]],
                [(m["comp"], m["local"], m["identity"], round(float(m["weight_total"]), 6))
                 for m in row["members"]],
                f"槽 {slot} 成员")

    def test_workspace_isolation(self):
        """不同工作区缓存互不串扰（键=workspace_root）。"""
        self._ws()
        other = Path(self.tmp.name + "_other")
        write_submesh(other, "zzzz9000-0-0", 0, 1, {0: 0},
                      [[0, _SENT, _SENT, _SENT]], [[1.0, 0, 0, 0]], [(0, 0, 0)])
        t_a = Builder.get_dualset_export_table_cached(str(self.root))
        t_b = Builder.get_dualset_export_table_cached(str(other))
        self.assertNotIn(1, t_b, "其它工作区不得包含本工作区槽位")
        self.assertIn(1, t_a)
        # 改 A 后 B 缓存不受影响
        td = self.root / "LOD0" / "bbbb1000-0-0" / "TYPE_GPU_TEST_"
        p = td / "bbbb1000-0-0.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        # 槽位改到全池声明段 [0,4) 内（B11/FC-3 合法域；越池槽属损坏数据 fail-closed）
        d["VGMap"]["0"] = 0
        p.write_text(json.dumps(d), encoding="utf-8")
        t_a2 = Builder.get_dualset_export_table_cached(str(self.root))
        t_b2 = Builder.get_dualset_export_table_cached(str(other))
        self.assertIsNot(t_a2, t_a, "A 变化 → A 重建")
        self.assertIs(t_b2, t_b, "B 无变化 → B 缓存命中不受 A 影响")

    def test_recompute_strength_cache_isolation(self):
        """F1 修复验证（t16）：缓存键含 recompute_strength——True/False 独立缓存，
        False 调用不得复用 True 的真实强度表（t15 原 F1 用例，修复后转绿）。"""
        self._ws()
        t_true_1 = Builder.get_dualset_export_table_cached(str(self.root), recompute_strength=True)
        t_false = Builder.get_dualset_export_table_cached(str(self.root), recompute_strength=False)
        t_true_2 = Builder.get_dualset_export_table_cached(str(self.root), recompute_strength=True)

        # True 调用：真实强度（>0）；False 调用：全 0 强度（与 direct(False) 一致）
        self.assertIs(t_true_1, t_true_2, "再调 True 必须命中 True 缓存（参数隔离下不串扰）")
        self.assertIsNot(t_true_1, t_false, "True/False 必须各自独立建表（F1 修复）")
        wt_true = [float(m["weight_total"]) for r in t_true_1.values() for m in r["members"]]
        wt_false = [float(m["weight_total"]) for r in t_false.values() for m in r["members"]]
        self.assertTrue(any(w > 0 for w in wt_true), "True 语义应含真实强度")
        self.assertTrue(all(w == 0.0 for w in wt_false), "False 语义必须全 0（不复用 True 表）")
        direct_false = Builder.build_dualset_export_table(str(self.root), recompute_strength=False)
        for slot, row in direct_false.items():
            self.assertEqual(
                [round(float(m["weight_total"]), 6) for m in t_false[slot]["members"]],
                [round(float(m["weight_total"]), 6) for m in row["members"]],
                f"槽 {slot}: False 缓存必须与 direct(False) 一致")


class CacheBoundaryTests(unittest.TestCase):
    """核验点 2：导出会话中途外部改动 → 安全（重建，不用旧表）。"""

    def setUp(self):
        clear_cache()
        self.tmp = tempfile.TemporaryDirectory(prefix="t15_bound_")
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_mid_session_touch_and_change(self):
        """会话内多次调用间外部改写 json → 指纹变 → 重建（宁可重建不用旧表）。"""
        write_submesh(self.root, "aaaa1000-0-0", 0, 2, {0: 0, 1: 1},
                      [[1, _SENT, _SENT, _SENT], [0, _SENT, _SENT, _SENT]],
                      [[0.5, 0, 0, 0], [1.0, 0, 0, 0]], [(0, 0, 0), (1, 0, 0)])
        t1 = Builder.get_dualset_export_table_cached(str(self.root))
        # 外部修改 json（VGMap 值变更，槽位收敛到全池声明段 [0,2) 内——
        # B11/FC-3 合法；越池槽位属损坏数据 fail-closed）→ 重建且反映
        td = self.root / "LOD0" / "aaaa1000-0-0" / "TYPE_GPU_TEST_"
        p = td / "aaaa1000-0-0.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        d["VGMap"] = {"0": 1, "1": 1}
        p.write_text(json.dumps(d), encoding="utf-8")
        t2 = Builder.get_dualset_export_table_cached(str(self.root))
        self.assertIsNot(t2, t1, "会话中途外部改动 json 必须重建（宁可重建不用旧表）")
        self.assertNotIn(0, t2, "重建表必须反映新的槽位映射（旧槽 0 已无成员）")
        self.assertIn(1, t2, "新槽位映射应包含槽 1")
        self.assertEqual(len(t2[1]["members"]), 2,
                         "槽 1 现由 aaaa 两个 local 成员共享")

    def test_buffer_only_change_does_not_invalidate(self):
        """指纹边界（设计行为，供报告 F2）：仅改 .buf（非 .json）不触发重建。

        指纹只覆盖 .json（mtime_ns+size）——强度值依赖的 buffer 内容不在指纹内；
        缓冲属于提取产物（导入后静态），json 才是结构元数据。属设计取舍，参见
        t15 报告 F2（低危观察：权重场外部变更在 json 未变时不失效）。
        """
        write_submesh(self.root, "aaaa1000-0-0", 0, 2, {0: 0, 1: 1},
                      [[1, _SENT, _SENT, _SENT], [0, _SENT, _SENT, _SENT]],
                      [[0.5, 0, 0, 0], [1.0, 0, 0, 0]], [(0, 0, 0), (1, 0, 0)])
        t1 = Builder.get_dualset_export_table_cached(str(self.root))
        td = self.root / "LOD0" / "aaaa1000-0-0" / "TYPE_GPU_TEST_"
        (td / "aaaa1000-0-0-Blend.buf").write_bytes(blend_blob(
            [[1, _SENT, _SENT, _SENT], [0, _SENT, _SENT, _SENT]],
            [[9.0, 0, 0, 0], [9.0, 0, 0, 0]]))
        t2 = Builder.get_dualset_export_table_cached(str(self.root))
        self.assertIs(t2, t1, "仅改 buffer（非 json）→ 指纹不变 → 缓存命中（设计范围）")


if __name__ == "__main__":
    unittest.main(verbosity=2)