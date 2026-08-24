"""ZZMI 骨骼合并（common/zzmi_skeleton.py）单测。

使用真实提取数据做 fixture：
- dump:      K:/SSMT-Package-master/3Dmigoto/ZZZ/FrameAnalysis-2026-08-19-122152
- 工作空间:  K:/SSMT-Package-master/WorkSpace/ZZMI/希格莉德·空岛传奇
路径不存在时整套跳过。
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

import numpy

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "zzmi_skeleton_test_pkg"


def _install_package(name):
    module = types.ModuleType(name)
    module.__path__ = []
    sys.modules[name] = module
    return module


def _load_module(qualname, path):
    spec = importlib.util.spec_from_file_location(qualname, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


for _name in (PKG, f"{PKG}.common", f"{PKG}.utils"):
    _install_package(_name)
_load_module(f"{PKG}.utils.json_utils", REPO_ROOT / "utils" / "json_utils.py")
_efmi = _load_module(f"{PKG}.common.efmi_skeleton", REPO_ROOT / "common" / "efmi_skeleton.py")
_zzmi = _load_module(f"{PKG}.common.zzmi_skeleton", REPO_ROOT / "common" / "zzmi_skeleton.py")

EFMIBoneMapBuilder = _efmi.EFMIBoneMapBuilder

ZZMIBoneMapBuilder = _zzmi.ZZMIBoneMapBuilder
ZZMIDeformResolver = _zzmi.ZZMIDeformResolver
ZZMILogParser = _zzmi.ZZMILogParser
ZZMISkeletonMergeHelper = _zzmi.ZZMISkeletonMergeHelper

DUMP_DIR = r"K:\SSMT-Package-master\3Dmigoto\ZZZ\FrameAnalysis-2026-08-19-122152"
WORKSPACE = r"K:\SSMT-Package-master\WorkSpace\ZZMI\希格莉德·空岛传奇"
LOG_PATH = os.path.join(DUMP_DIR, "log.txt")

# 实测期望值（见 ZZMI骨骼合并计划书.md §2.5；去重 2026-08-24 起含刚性部件质心门控）
EXPECTED_VG_COUNT = {
    "84618ee0": 49, "a23aa8a3": 105, "19086112": 7, "b51bdd59": 11,
    "b20f90ea": 51, "b30db54e": 14, "48625d6d": 10, "d892c658": 16,
    "64d7d56f": 1, "454ff522": 1, "add6ff13": 1,
}
EXPECTED_DEFORM_DRAW = {  # drawib -> deform pass draw index
    "84618ee0": "000004", "a23aa8a3": "000020", "19086112": "000035",
    "b51bdd59": "000036", "b20f90ea": "000002", "b30db54e": "000008",
    "48625d6d": "000018", "d892c658": "000010", "64d7d56f": "000001",
    "454ff522": "000029", "add6ff13": "000030",
}
EXPECTED_TOTAL_SLOTS = 266
# 刚性门控后唯一骨骼数：244 + 2（64d7d56f 头顶件、b51bdd59#0 后脑发饰骨
# 从抓帧重合的头部锚点组中被质心门控拆开）
EXPECTED_UNIQUE_BONES = 246


def _list_submesh_jsons(workspace_root):
    """遍历 LOD0 全部子网格 json，返回 (unique_str, json_path, json_dict)。"""
    lod0 = os.path.join(workspace_root, "LOD0")
    import_json = {}
    import_json_path = os.path.join(workspace_root, "Import.json")
    if os.path.isfile(import_json_path):
        with open(import_json_path, encoding="utf-8") as f:
            import_json = json.load(f)
    results = []
    for name in sorted(os.listdir(lod0)):
        type_parent = os.path.join(lod0, name)
        if not os.path.isdir(type_parent):
            continue
        unique_str = "LOD0." + name
        gametype = import_json.get(unique_str, "")
        if not gametype:
            continue
        json_path = os.path.join(type_parent, "TYPE_" + gametype, name + ".json")
        if not os.path.isfile(json_path):
            continue
        with open(json_path, encoding="utf-8") as f:
            results.append((unique_str, json_path, json.load(f)))
    return results


@unittest.skipUnless(os.path.isfile(LOG_PATH) and os.path.isdir(WORKSPACE), "提取数据不在本机")
class TestZZMILogParser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parser = ZZMILogParser(LOG_PATH)

    def test_deform_pass_count_and_spot_checks(self):
        passes = self.parser.get_deform_passes()
        self.assertEqual(len(passes), 22)

        p2 = passes["000002"]
        self.assertEqual(p2["vertex_count"], 4643)
        self.assertEqual(p2["vb0_hash"], "122883aa")
        self.assertEqual(p2["so_hash"], "dd9c8d5e")
        self.assertEqual(p2["palette_hash"], "c2f5419a")

        p36 = passes["000036"]
        self.assertEqual(p36["vertex_count"], 345)
        self.assertEqual(p36["so_hash"], "6a8ea608")
        self.assertEqual(p36["palette_hash"], "c6c3b31d")

    def test_render_draw_vb0_and_ib(self):
        # 渲染 draw 000039: ib=84618ee0, vb0=840c1713（= deform 000004 的 SO 输出）
        self.assertEqual(self.parser.draws["000039"]["ib"], "84618ee0")
        self.assertEqual(self.parser.get_vb_hash("000039", 0), "840c1713")
        self.assertEqual(self.parser.get_vb_hash("000040", 0), "01b35c45")

    def test_dump_map_palette_path(self):
        path = self.parser.get_deduped_path("000002-vs-t0=c2f5419a")
        self.assertIsNotNone(path)
        self.assertTrue(path.endswith(os.path.join("deduped", "e018278f.buf")))

    def test_palette_load_shape(self):
        path = self.parser.get_deduped_path("000004-vs-t0=f6a6c781")
        palette = ZZMIBoneMapBuilder.load_palette(path)
        self.assertEqual(palette.shape, (49, 12))


@unittest.skipUnless(os.path.isfile(LOG_PATH) and os.path.isdir(WORKSPACE), "提取数据不在本机")
class TestZZMIDeformResolver(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parser = ZZMILogParser(LOG_PATH)
        cls.resolver = ZZMIDeformResolver(cls.parser)

    def test_all_submeshes_resolve_path_a_and_b(self):
        for unique_str, _json_path, submesh_json in _list_submesh_jsons(WORKSPACE):
            draw_ib = unique_str.split(".", 1)[-1].split("-")[0]
            expected_draw = EXPECTED_DEFORM_DRAW[draw_ib]

            position_hash = (submesh_json.get("CategoryHash") or {}).get("Position", "")
            draw_a, pass_a, via_a = self.resolver.resolve(position_hash=position_hash)
            self.assertEqual(via_a, "A", unique_str)
            self.assertEqual(draw_a, expected_draw, unique_str)
            self.assertEqual(pass_a["vertex_count"], pass_a["vertex_count"])

            vertex_limit_hash = submesh_json.get("VertexLimitVB", "")
            draw_b, _pass_b, via_b = self.resolver.resolve(vertex_limit_hash=vertex_limit_hash)
            self.assertEqual(via_b, "B", unique_str)
            self.assertEqual(draw_b, expected_draw, unique_str)

    def test_resolve_path_c_fallback(self):
        # 只用渲染 draw 列表（路径 C）：b20f90ea-19182-0 -> 渲染 draw 38/44/192/213/225
        draw, deform_pass, via = self.resolver.resolve(
            render_draw_indices=["000038", "000044", "000192", "000213", "000225"]
        )
        self.assertEqual(via, "C")
        self.assertEqual(draw, "000002")
        self.assertEqual(deform_pass["so_hash"], "dd9c8d5e")


@unittest.skipUnless(os.path.isfile(LOG_PATH) and os.path.isdir(WORKSPACE), "提取数据不在本机")
class TestZZMIBoneMapBuilder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parser = ZZMILogParser(LOG_PATH)
        cls.palettes = {}
        for draw_ib, draw_index in EXPECTED_DEFORM_DRAW.items():
            deform_pass = parser.get_deform_passes()[draw_index]
            path = parser.get_deduped_path(f"{draw_index}-vs-t0={deform_pass['palette_hash']}")
            cls.palettes[draw_ib] = ZZMIBoneMapBuilder.load_palette(path)
        # 驱动签名（生产路径同款：ensure_skeleton_data 对每个 DrawIB 代表子网格计算）
        cls.signatures = {}
        for unique_str, json_path, submesh_json in _list_submesh_jsons(WORKSPACE):
            draw_ib = unique_str.split(".", 1)[-1].split("-")[0]
            if draw_ib in cls.signatures:
                continue  # 同 DrawIB 拆分子网格共享 palette，取代表即可
            base = os.path.splitext(os.path.basename(json_path))[0]
            dirpath = os.path.dirname(json_path)
            cls.signatures[draw_ib] = EFMIBoneMapBuilder.compute_driven_signatures(
                os.path.join(dirpath, base + "-Position.buf"),
                os.path.join(dirpath, base + "-Blend.buf"),
                submesh_json,
            )
        cls.vg_maps, cls.vg_offsets, cls.total_slots = ZZMIBoneMapBuilder.build_vg_maps(
            cls.palettes, cls.signatures
        )

    def test_palette_counts_match_measured(self):
        for draw_ib, palette in self.palettes.items():
            self.assertEqual(len(palette), EXPECTED_VG_COUNT[draw_ib], draw_ib)

    def test_total_slots(self):
        self.assertEqual(self.total_slots, EXPECTED_TOTAL_SLOTS)

    def test_unique_bone_count(self):
        unique_globals = set()
        for vg_map in self.vg_maps.values():
            unique_globals.update(vg_map.values())
        self.assertEqual(len(unique_globals), EXPECTED_UNIQUE_BONES)

    def test_same_part_never_merged(self):
        # 每个部件内部：所有 local 都被映射，且不会有两个 local 映射到
        # 本部件 vg_offset 段内的同一槽位（同部件不去重）
        for draw_ib, vg_map in self.vg_maps.items():
            vg_count = EXPECTED_VG_COUNT[draw_ib]
            self.assertEqual(sorted(vg_map.keys()), list(range(vg_count)), draw_ib)
            offset = self.vg_offsets[draw_ib]
            own_slots = [g for g in vg_map.values() if offset <= g < offset + vg_count]
            self.assertEqual(len(own_slots), len(set(own_slots)), draw_ib)

    def test_known_shared_bone_merged(self):
        # 454ff522#0（前额薄件）与 48625d6d#2（面部锚点）：bitwise 全同且质心距 0.034
        # 通过刚性门控 -> 仍合并
        g1 = self.vg_maps["454ff522"][0]
        g2 = self.vg_maps["48625d6d"][2]
        self.assertEqual(g1, g2)

    def test_rigid_gate_splits_coincident_head_attachments(self):
        # 刚性门控回归（2026-08-24）：64d7d56f#0（头顶件，质心距 0.072）与
        # b51bdd59#0（后脑物理发饰骨，质心距 0.18~0.20）虽与共享矩阵 bitwise 全同，
        # 但质心过远 -> 各占各槽（抓帧重合的不同锚点骨，动画分叉时绝不可共用槽位）
        shared_global = self.vg_maps["454ff522"][0]
        self.assertNotEqual(self.vg_maps["64d7d56f"][0], shared_global)
        self.assertNotEqual(self.vg_maps["b51bdd59"][0], shared_global)
        self.assertNotEqual(self.vg_maps["64d7d56f"][0], self.vg_maps["b51bdd59"][0])

    def test_same_bone_different_frame_not_merged(self):
        # 回归：48625d6d 的 #1/#8/#9 与共享骨骼 maxdiff 3.5e-07~2.5e-06（同骨骼异帧），
        # 绝不可与共享骨骼的全局 id 相同
        shared_global = self.vg_maps["64d7d56f"][0]
        for local in (1, 8, 9):
            self.assertNotEqual(self.vg_maps["48625d6d"][local], shared_global)

    def test_b20f90ea_thirteen_shared_with_a23aa8a3(self):
        # b20f90ea 的 13 根共享骨骼应映射到 a23aa8a3 段内
        a_offset = self.vg_offsets["a23aa8a3"]
        a_count = EXPECTED_VG_COUNT["a23aa8a3"]
        into_a = [
            local for local, global_id in self.vg_maps["b20f90ea"].items()
            if a_offset <= global_id < a_offset + a_count
        ]
        self.assertEqual(len(into_a), 13)


@unittest.skipUnless(os.path.isfile(LOG_PATH) and os.path.isdir(WORKSPACE), "提取数据不在本机")
class TestZZMISkeletonMergeHelper(unittest.TestCase):
    def setUp(self):
        # 把工作空间反查所需文件复制到临时目录（json + Blend.buf + 配置，跳过贴图）
        self.tmp = tempfile.mkdtemp(prefix="zzmi_ws_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for entry in ("Import.json",):
            shutil.copy2(os.path.join(WORKSPACE, entry), os.path.join(self.tmp, entry))
        shutil.copytree(
            os.path.join(WORKSPACE, "Config"), os.path.join(self.tmp, "Config")
        )
        lod0_src = os.path.join(WORKSPACE, "LOD0")
        lod0_dst = os.path.join(self.tmp, "LOD0")
        os.makedirs(lod0_dst, exist_ok=True)
        for name in os.listdir(lod0_src):
            src_path = os.path.join(lod0_src, name)
            if os.path.isfile(src_path) and name.endswith(".json"):
                shutil.copy2(src_path, os.path.join(lod0_dst, name))
                continue
            if not os.path.isdir(src_path):
                continue
            for type_dir in os.listdir(src_path):
                if not type_dir.startswith("TYPE_"):
                    continue
                type_src = os.path.join(src_path, type_dir)
                type_dst = os.path.join(lod0_dst, name, type_dir)
                os.makedirs(type_dst, exist_ok=True)
                for file_name in os.listdir(type_src):
                    if file_name.endswith(".json") or file_name.endswith("-Blend.buf") or file_name.endswith("-Position.buf"):
                        shutil.copy2(
                            os.path.join(type_src, file_name),
                            os.path.join(type_dst, file_name),
                        )
        # 剥离已写回的骨骼合并字段，模拟纯净工作空间（真实工作空间可能已被 e2e 写回过）
        for name in os.listdir(os.path.join(self.tmp, "LOD0")):
            type_parent = os.path.join(self.tmp, "LOD0", name)
            if not os.path.isdir(type_parent):
                continue
            for type_dir in os.listdir(type_parent):
                if not type_dir.startswith("TYPE_"):
                    continue
                json_path = os.path.join(type_parent, type_dir, name + ".json")
                if not os.path.isfile(json_path):
                    continue
                with open(json_path, encoding="utf-8") as f:
                    payload = json.load(f)
                changed = False
                for field in (
                    "VGMap", "VGOffset", "VGCount", "BoneMatrixFileName",
                    "SkeletonGroupCb1SourceIb", "DeformDrawIndex", "OriginalVertexCount",
                ):
                    if field in payload:
                        payload.pop(field)
                        changed = True
                if changed:
                    with open(json_path, "w", encoding="utf-8") as f:
                        json.dump(payload, f, ensure_ascii=False, indent=4)

    def _unique_str_list(self):
        with open(os.path.join(self.tmp, "Import.json"), encoding="utf-8") as f:
            import_json = json.load(f)
        return sorted(import_json.keys())

    def test_ensure_skeleton_data_end_to_end(self):
        unique_str_list = self._unique_str_list()
        self.assertEqual(len(unique_str_list), 15)

        ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=self.tmp, unique_str_list=unique_str_list
        )
        self.assertTrue(ok, message)

        # 每个子网格 json 写回了 VGMap/VGOffset/VGCount，且 ModImpRuntime 缓存存在
        for unique_str, json_path, submesh_json in _list_submesh_jsons(self.tmp):
            draw_ib = unique_str.split(".", 1)[-1].split("-")[0]
            vg_map = submesh_json.get("VGMap")
            self.assertTrue(vg_map, unique_str)
            self.assertEqual(submesh_json.get("VGCount"), EXPECTED_VG_COUNT[draw_ib], unique_str)
            self.assertEqual(len(vg_map), EXPECTED_VG_COUNT[draw_ib], unique_str)

            bare = unique_str.split(".", 1)[-1]
            cache_path = os.path.join(
                self.tmp, "LOD0", bare, "ModImpRuntime", bare + "-BoneMatrix.buf"
            )
            self.assertTrue(os.path.isfile(cache_path), unique_str)
            palette = ZZMIBoneMapBuilder.load_palette(cache_path)
            self.assertEqual(len(palette), EXPECTED_VG_COUNT[draw_ib], unique_str)

        # 同 DrawIB 拆分子网格：VGMap/VGOffset 必须一致
        jsons = {u: j for u, _p, j in _list_submesh_jsons(self.tmp)}
        a = jsons["LOD0.84618ee0-22296-0"]
        b = jsons["LOD0.84618ee0-1164-22296"]
        self.assertEqual(a["VGMap"], b["VGMap"])
        self.assertEqual(a["VGOffset"], b["VGOffset"])

        # 刚性门控：454ff522#0（前额件）与 48625d6d#2（面部锚点，质心距 0.034）仍合并；
        # 64d7d56f#0（头顶件，质心距 0.072）被拆开（抓帧重合的不同锚点骨）
        g_face_band = jsons["LOD0.454ff522-216-0"]["VGMap"]["0"]
        sub_486 = sorted(u for u in jsons if "48625d6d" in u)[0]
        self.assertEqual(jsons[sub_486]["VGMap"]["2"], g_face_band)
        g_top = jsons["LOD0.64d7d56f-900-0"]["VGMap"]["0"]
        self.assertNotEqual(g_top, g_face_band)

        # 骨架分组（渲染 cb1 对象变换 1:1 配对）：实测 5 组
        # 身体组 {a23aa8a3, b20f90ea, b30db54e}、头部组 {454ff522, 48625d6d, 64d7d56f, b51bdd59}、
        # 头发 84618ee0 独立、19086112 独立、{add6ff13, d892c658} 共享同一对象空间
        group_of = {}
        for unique_str, _json_path, submesh_json in _list_submesh_jsons(self.tmp):
            draw_ib = unique_str.split(".", 1)[-1].split("-")[0]
            self.assertIn("SkeletonGroup", submesh_json, unique_str)
            group_of[draw_ib] = submesh_json["SkeletonGroup"]

        body = {"a23aa8a3", "b20f90ea", "b30db54e"}
        head = {"454ff522", "48625d6d", "64d7d56f", "b51bdd59"}
        self.assertEqual(len({group_of[d] for d in body}), 1)
        self.assertEqual(len({group_of[d] for d in head}), 1)
        self.assertEqual(len(set(group_of.values())), 5)
        self.assertNotEqual(group_of["a23aa8a3"], group_of["454ff522"])
        self.assertNotEqual(group_of["84618ee0"], group_of["a23aa8a3"])
        self.assertNotEqual(group_of["19086112"], group_of["a23aa8a3"])
        # add6ff13 与 d892c658 共享同一对象空间（实测同一 cb1 变换）-> 同组
        self.assertEqual(group_of["add6ff13"], group_of["d892c658"])
        self.assertNotEqual(group_of["add6ff13"], group_of["a23aa8a3"])
        # 同组判定的刚性门控拆分不改变分组：头顶件与头部组同组（同空间，仅锚点不同）
        self.assertEqual(group_of["64d7d56f"], group_of["454ff522"])

        # 全局骨骼编号（组基址拼接；组序按组内最小 draw_ib）：
        # G0={19086112}(7) base 0 / G1=头部(23) base 7 / G2={84618ee0}(49) base 30 /
        # G3=身体(170) base 79 / G4={add6ff13,d892c658}(17) base 249
        jsons_by_ib = {u.split(".", 1)[-1].split("-")[0]: j for u, _p, j in _list_submesh_jsons(self.tmp)}
        expected_offsets = {
            "19086112": 0,
            "454ff522": 7, "48625d6d": 8, "64d7d56f": 18, "b51bdd59": 19,
            "84618ee0": 30,
            "a23aa8a3": 79, "b20f90ea": 184, "b30db54e": 235,
            "add6ff13": 249, "d892c658": 250,
        }
        for draw_ib, expected_offset in expected_offsets.items():
            self.assertEqual(jsons_by_ib[draw_ib]["VGOffset"], expected_offset, draw_ib)

        # 无 CB1 校准（2026-08-25 用户拍板）：不再写 cb1 捕获源字段
        for draw_ib, submesh_json in jsons_by_ib.items():
            self.assertNotIn("SkeletonGroupCb1SourceIb", submesh_json, draw_ib)

        # 导出侧守卫元数据：deform draw 序号 + 原部件顶点数（合并网格时序校验/vb1 换绑）
        for unique_str, _json_path, submesh_json in _list_submesh_jsons(self.tmp):
            draw_ib = unique_str.split(".", 1)[-1].split("-")[0]
            expected_draw = int(EXPECTED_DEFORM_DRAW[draw_ib])
            self.assertEqual(submesh_json.get("DeformDrawIndex"), expected_draw, unique_str)
            self.assertGreater(submesh_json.get("OriginalVertexCount", 0), 0, unique_str)
        # 实测原部件顶点数（deform draw vertex count）：组 3 三个部件的 Blend.buf 行数
        original_counts = {
            "b20f90ea": 4643, "a23aa8a3": 12314, "b30db54e": 1744,
            "84618ee0": 5846, "19086112": 3288, "b51bdd59": 345,
        }
        for draw_ib, expected_count in original_counts.items():
            self.assertEqual(
                jsons_by_ib[draw_ib]["OriginalVertexCount"], expected_count, draw_ib
            )

    def test_idempotent_second_run_skips(self):
        unique_str_list = self._unique_str_list()
        ok1, _ = ZZMISkeletonMergeHelper.ensure_skeleton_data(self.tmp, unique_str_list)
        self.assertTrue(ok1)
        ok2, message2 = ZZMISkeletonMergeHelper.ensure_skeleton_data(self.tmp, unique_str_list)
        self.assertTrue(ok2, message2)
        self.assertIn("跳过", message2)

    def test_force_rebuild(self):
        unique_str_list = self._unique_str_list()
        ZZMISkeletonMergeHelper.ensure_skeleton_data(self.tmp, unique_str_list)
        ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            self.tmp, unique_str_list, force=True
        )
        self.assertTrue(ok, message)
        self.assertIn("15 个子网格", message)


if __name__ == "__main__":
    unittest.main()
