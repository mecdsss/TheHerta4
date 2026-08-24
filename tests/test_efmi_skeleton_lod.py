"""EFMI 骨骼合并多 LOD 适配（原始候选联合对应 + LOD0 基准同步）单测。

LOD0 / LOD1 仍然使用各自的 FrameAnalysis dump 和运行时槽位，但在去重之前先用
原始矩阵/权重中心建立对应；LOD0 执行一次去重，LOD1 按对应关系同步分区。

覆盖：
- _parse_lod_name / resolve_frame_analysis_dirs_by_lod（WorkPageTabs 映射 + 兜底）；
- ensure_skeleton_data 按 LOD 分组：每个 LOD 从自己的 dump 读取骨骼数据、
  各自 VGOffset 从 0 起、BoneMatrix 缓存来自各自 dump，并写回跨 LOD 对应账本；
- WorkPageTabs 缺失时退化为共用默认目录（此时查不到自己 drawcall 的 LOD 被跳过）。
"""

import importlib.util
import json
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path

import numpy

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "efmi_lod_test_pkg"

CB_HASH = "aaaa1111"
T0_HASH_A = "bbbb2222"
T0_HASH_B = "cccc3333"
CB_HASH_B = "dddd5555"

GAMETYPE = "GPU_P12_N4_T8_C4_BW8_BI4_"


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

EFMISkeletonMergeHelper = _efmi.EFMISkeletonMergeHelper
EFMIBoneMapBuilder = _efmi.EFMIBoneMapBuilder
VG_MAP_ALGORITHM_VERSION = _efmi._VG_MAP_ALGORITHM_VERSION


def _write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _make_dump(dump_dir, draw_index, cb_hash, t0_hash, bone_tx):
    """构造一个最小 FrameAnalysis dump（log.txt + deduped/ 下 instance cb 与骨骼池）。

    log 契约对齐 get_skeleton_buffer：instance config cb（num_constants==4096）
    在 first_constant 窗口第 6 个 float4 的 xy（uint32 位型）记录骨骼段偏移
    （float4 单位）；vs-t0 骨骼池在 偏移+3 处放 256 骨骼的 4x3 矩阵
    （GLOBAL_RESERVED_ROWS=3）。bone_tx 用于区分两个 dump 的骨骼内容。
    """
    dump_dir = Path(dump_dir)
    (dump_dir / "deduped").mkdir(parents=True, exist_ok=True)

    cb = numpy.zeros((16, 4), dtype=numpy.float32)
    cb_uint = cb.view(numpy.uint32)
    cb_uint[5, 0] = 0
    cb_uint[5, 1] = 4  # 骨骼段偏移（float4 单位）-> data_offset = 4 + 3 = 7
    (dump_dir / "deduped" / f"{draw_index}-vs-cb2={cb_hash}.buf").write_bytes(cb.tobytes())

    pool = numpy.zeros((800, 4), dtype=numpy.float32)
    mat = numpy.array([1, 0, 0, 0, 1, 0, 0, 0, 1, bone_tx, 0, 0], dtype=numpy.float32)
    pool[7:10] = mat.reshape(3, 4)
    (dump_dir / "deduped" / f"{draw_index}-vs-t0={t0_hash}.buf").write_bytes(pool.tobytes())

    deduped_abs = (dump_dir / "deduped").resolve()
    cb_name = f"{draw_index}-vs-cb2={cb_hash}.buf"
    t0_name = f"{draw_index}-vs-t0={t0_hash}.buf"
    log_lines = [
        f"{draw_index} VSSetConstantBuffers1(StartSlot:2,",
        f"2: resource=0x00000000 hash={cb_hash} first_constant=0 num_constants=4096",
        f"{draw_index} VSSetShaderResources(StartSlot:0,",
        f"0: view=0x00000000 resource=0x00000000 hash={t0_hash}",
        f"{draw_index} DrawIndexedInstanced(IndexCountPerInstance:100, InstanceCount:1, "
        f"StartIndexLocation:0, BaseVertexLocation:0, StartInstanceLocation:0)",
        # 真实 log 的 Dumping 行带 draw 前缀（如 "000100 3DMigoto Dumping Buffer ..."）
        f"{draw_index} 3DMigoto Dumping Buffer {draw_index}-vs-cb2={cb_hash} -> {deduped_abs / cb_name}",
        f"{draw_index} 3DMigoto Dumping Buffer {draw_index}-vs-t0={t0_hash} -> {deduped_abs / t0_name}",
    ]
    (dump_dir / "log.txt").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    return dump_dir


def _make_submesh(workspace, lod, bare):
    """构造一个最小子网格目录（json + Blend.buf），返回 unique_str。

    2 顶点 × BLENDINDICES R8G8B8A8_UINT（全 0）-> vg_count = 1。
    """
    type_dir = workspace / lod / bare / ("TYPE_" + GAMETYPE)
    type_dir.mkdir(parents=True, exist_ok=True)
    _write_json(type_dir / f"{bare}.json", {
        "CategoryBufferList": [
            {"D3D11ElementList": [
                {"Category": "Blend", "SemanticName": "BLENDINDICES",
                 "Format": "R8G8B8A8_UINT", "ByteWidth": 4},
            ]},
        ],
    })
    blend = numpy.zeros((2, 4), dtype=numpy.uint8)
    (type_dir / f"{bare}-Blend.buf").write_bytes(blend.tobytes())
    return f"{lod}.{bare}"


def _make_workspace(tmp, dump_a, dump_b):
    ws = Path(tmp) / "ws"
    (ws / "Config" / "Tabs").mkdir(parents=True, exist_ok=True)
    _write_json(ws / "Config" / "FrameAnalysisPath.json",
                {"frameAnalysisFolderPath": str(dump_b)})
    _write_json(ws / "Config" / "WorkPageTabs.json", {
        "activeTabId": "ws-tab-2",
        "tabs": [
            {"id": "ws-tab-1", "name": "LOD0"},
            {"id": "ws-tab-2", "name": "LOD1"},
        ],
    })
    _write_json(ws / "Config" / "Tabs" / "ws-tab-1.json",
                {"frameAnalysisFolderPath": str(dump_a)})
    _write_json(ws / "Config" / "Tabs" / "ws-tab-2.json",
                {"frameAnalysisFolderPath": str(dump_b)})
    return ws


class ParseLodNameTests(unittest.TestCase):
    def test_lod_prefix(self):
        self.assertEqual(EFMISkeletonMergeHelper._parse_lod_name("LOD0.aaaabbbb-100-0"), "LOD0")
        self.assertEqual(EFMISkeletonMergeHelper._parse_lod_name("LOD1.ccccdddd-200-0"), "LOD1")

    def test_no_prefix(self):
        self.assertEqual(EFMISkeletonMergeHelper._parse_lod_name("aaaabbbb-100-0"), "")
        self.assertEqual(EFMISkeletonMergeHelper._parse_lod_name(""), "")

    def test_lod_like_but_not_lod(self):
        self.assertEqual(EFMISkeletonMergeHelper._parse_lod_name("LOAD0.aaaabbbb-100-0"), "")
        self.assertEqual(EFMISkeletonMergeHelper._parse_lod_name("LOD.aaaabbbb-100-0"), "")


class ResolveFrameAnalysisDirsByLodTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="efmi_lod_cfg_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dump_a = Path(self.tmp) / "dumpA"
        self.dump_b = Path(self.tmp) / "dumpB"
        self.dump_a.mkdir()
        self.dump_b.mkdir()
        self.ws = _make_workspace(self.tmp, self.dump_a, self.dump_b)

    def test_lod_map_from_work_page_tabs(self):
        lod_map, default_dir = EFMISkeletonMergeHelper.resolve_frame_analysis_dirs_by_lod(
            str(self.ws)
        )
        self.assertEqual(lod_map, {"LOD0": str(self.dump_a), "LOD1": str(self.dump_b)})
        # 默认目录 = 工作空间级 FrameAnalysisPath.json（活动 tab 的路径）
        self.assertEqual(default_dir, str(self.dump_b))

    def test_invalid_tab_path_skipped(self):
        _write_json(self.ws / "Config" / "Tabs" / "ws-tab-1.json",
                    {"frameAnalysisFolderPath": str(Path(self.tmp) / "不存在的目录")})
        lod_map, default_dir = EFMISkeletonMergeHelper.resolve_frame_analysis_dirs_by_lod(
            str(self.ws)
        )
        self.assertEqual(lod_map, {"LOD1": str(self.dump_b)})
        self.assertEqual(default_dir, str(self.dump_b))

    def test_missing_work_page_tabs_falls_back_to_default(self):
        (self.ws / "Config" / "WorkPageTabs.json").unlink()
        lod_map, default_dir = EFMISkeletonMergeHelper.resolve_frame_analysis_dirs_by_lod(
            str(self.ws)
        )
        self.assertEqual(lod_map, {})
        self.assertEqual(default_dir, str(self.dump_b))


class MultiLodBuildTests(unittest.TestCase):
    """构建端 e2e：两个 LOD 各自用自己的 dump，LOD1 复用 LOD0 分区。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="efmi_lod_ws_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # dump A 只有 draw 000100（LOD0 的子网格）；dump B 只有 draw 000200（LOD1 的）。
        # 若实现错误地让某个 LOD 去查对方的 dump，该 LOD 会因找不到骨骼数据被跳过。
        self.dump_a = _make_dump(Path(self.tmp) / "dumpA", "000100", CB_HASH, T0_HASH_A, 1.5)
        self.dump_b = _make_dump(Path(self.tmp) / "dumpB", "000200", CB_HASH_B, T0_HASH_B, 2.5)
        self.ws = _make_workspace(self.tmp, self.dump_a, self.dump_b)
        self.u_lod0 = _make_submesh(self.ws, "LOD0", "aaaabbbb-100-0")
        self.u_lod1 = _make_submesh(self.ws, "LOD1", "ccccdddd-200-0")
        _write_json(self.ws / "Import.json",
                    {self.u_lod0: GAMETYPE, self.u_lod1: GAMETYPE})
        _write_json(self.ws / "LOD0" / "ComponentName_DrawCallIndexList.json",
                    {"aaaabbbb-100-0": ["000100"]})
        _write_json(self.ws / "LOD1" / "ComponentName_DrawCallIndexList.json",
                    {"ccccdddd-200-0": ["000200"]})

    def _read_submesh_json(self, lod, bare):
        for type_dir in (self.ws / lod / bare).iterdir():
            if type_dir.is_dir() and type_dir.name.startswith("TYPE_"):
                return json.loads((type_dir / f"{bare}.json").read_text(encoding="utf-8"))
        return None

    def test_each_lod_uses_own_dump_and_own_slot_space(self):
        ok, message = EFMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws),
            unique_str_list=[self.u_lod0, self.u_lod1],
        )
        self.assertTrue(ok, message)

        json0 = self._read_submesh_json("LOD0", "aaaabbbb-100-0")
        json1 = self._read_submesh_json("LOD1", "ccccdddd-200-0")
        # 两个 LOD 都成功生成（各自在自己的 dump 里找到了骨骼数据）
        self.assertEqual(json0.get("VGMap"), {"0": 0}, "LOD0 应生成 VGMap")
        self.assertEqual(json1.get("VGMap"), {"0": 0}, "LOD1 应生成 VGMap")
        # 各自槽位从 0 起：LOD1 的 VGOffset 绝不能接在 LOD0 后面
        self.assertEqual(json0.get("VGOffset"), 0)
        self.assertEqual(json1.get("VGOffset"), 0)
        self.assertEqual(json0.get("VGCount"), 1)
        self.assertEqual(json1.get("VGCount"), 1)
        self.assertEqual(json0.get("VGMapAlgorithmVersion"), VG_MAP_ALGORITHM_VERSION)
        self.assertEqual(json1.get("VGMapAlgorithmVersion"), VG_MAP_ALGORITHM_VERSION)
        self.assertEqual(json0.get("EFMILODReference"), "LOD0")
        self.assertEqual(json1.get("EFMILODReference"), "LOD0")
        self.assertTrue(json0.get("EFMILODProjection"))
        self.assertTrue(json1.get("EFMILODProjection"))
        self.assertEqual(json0.get("EFMILODBaselineGroupCount"), 1)
        self.assertEqual(json1.get("EFMILODBaselineGroupCount"), 1)
        self.assertEqual(json0.get("EFMILODGroupCount"), 1)
        self.assertEqual(json1.get("EFMILODGroupCount"), 1)
        self.assertEqual(json0.get("EFMILODMissingBaselineCount"), 0)
        self.assertEqual(json1.get("EFMILODMissingBaselineCount"), 0)
        self.assertEqual(json0.get("EFMILODActualGroupCount"), 1)
        self.assertEqual(json1.get("EFMILODActualGroupCount"), 1)
        # 两个最小 dump 故意使用不同平移矩阵，超过硬门控时应保留“无对应”事实，
        # 不能为了凑数量伪造一条跨 LOD 匹配。
        self.assertEqual(json0.get("EFMILODCorrespondence"), {})
        self.assertEqual(json1.get("EFMILODCorrespondence"), {})

        # BoneMatrix 缓存来自各自 dump（整池拷贝 800x4，骨骼 0 位于 偏移 7 起；tx 不同：A=1.5，B=2.5）
        pool0 = numpy.fromfile(
            self.ws / "LOD0" / "aaaabbbb-100-0" / "ModImpRuntime" / "aaaabbbb-100-0-BoneMatrix.buf",
            dtype=numpy.float32,
        ).reshape(-1, 4)
        pool1 = numpy.fromfile(
            self.ws / "LOD1" / "ccccdddd-200-0" / "ModImpRuntime" / "ccccdddd-200-0-BoneMatrix.buf",
            dtype=numpy.float32,
        ).reshape(-1, 4)
        mat0 = pool0[7:10].reshape(12)
        mat1 = pool1[7:10].reshape(12)
        self.assertAlmostEqual(float(mat0[9]), 1.5, places=4, msg="LOD0 骨骼应来自 dump A")
        self.assertAlmostEqual(float(mat1[9]), 2.5, places=4, msg="LOD1 骨骼应来自 dump B")

    def test_without_tabs_both_lod_use_default_dump(self):
        """WorkPageTabs 缺失时共用默认目录：查不到自己 drawcall 的 LOD 被跳过。

        完整性语义：请求了 2 个目标、只有 1 个生成 -> 必须报告未完整生成。
        """
        (self.ws / "Config" / "WorkPageTabs.json").unlink()
        ok, message = EFMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws),
            unique_str_list=[self.u_lod0, self.u_lod1],
        )
        # LOD1 的 drawcall 在默认 dump B 里 -> 生成；LOD0 查不到 -> 跳过
        self.assertFalse(ok, "存在未生成目标时不得报告完整成功")
        self.assertIn("未生成骨骼数据", message)
        json0 = self._read_submesh_json("LOD0", "aaaabbbb-100-0")
        json1 = self._read_submesh_json("LOD1", "ccccdddd-200-0")
        self.assertIsNone(json0.get("VGMap"), "默认目录无 LOD0 drawcall，应被跳过")
        self.assertEqual(json1.get("VGMap"), {"0": 0})
        self.assertEqual(json1.get("VGOffset"), 0)

    def test_independent_lod_mode_runs_dedup_on_both_sides(self):
        """关闭分组投影时，两侧分别调用原有去重，不复用 LOD0 映射。"""
        original = EFMIBoneMapBuilder.build_vg_maps
        calls = []

        def wrapped(submesh_skeletons, *args, **kwargs):
            calls.append(kwargs.get("deduplicate"))
            return original(submesh_skeletons, *args, **kwargs)

        EFMIBoneMapBuilder.build_vg_maps = staticmethod(wrapped)
        try:
            ok, message = EFMISkeletonMergeHelper.ensure_skeleton_data(
                workspace_root=str(self.ws),
                unique_str_list=[self.u_lod0, self.u_lod1],
                force=True,
                lod_group_projection=False,
            )
        finally:
            EFMIBoneMapBuilder.build_vg_maps = staticmethod(original)
        self.assertTrue(ok, message)
        self.assertEqual(calls, [None, None])
        json0 = self._read_submesh_json("LOD0", "aaaabbbb-100-0")
        json1 = self._read_submesh_json("LOD1", "ccccdddd-200-0")
        self.assertFalse(json0.get("EFMILODProjection"))
        self.assertFalse(json1.get("EFMILODProjection"))

    def test_idempotent_second_run_skips(self):
        ok1, message1 = EFMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws),
            unique_str_list=[self.u_lod0, self.u_lod1],
        )
        self.assertTrue(ok1, message1)
        ok2, message2 = EFMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws),
            unique_str_list=[self.u_lod0, self.u_lod1],
        )
        self.assertTrue(ok2, message2)
        self.assertIn("无需重新生成", message2)

    def test_stale_vgmap_version_is_rebuilt(self):
        ok1, message1 = EFMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws),
            unique_str_list=[self.u_lod0],
        )
        self.assertTrue(ok1, message1)
        json_path = next((self.ws / "LOD0" / "aaaabbbb-100-0").glob("TYPE_*/*.json"))
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        payload["VGMapAlgorithmVersion"] = VG_MAP_ALGORITHM_VERSION - 1
        json_path.write_text(json.dumps(payload), encoding="utf-8")

        ok2, message2 = EFMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws),
            unique_str_list=[self.u_lod0],
        )
        self.assertTrue(ok2, message2)
        rebuilt = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(rebuilt.get("VGMapAlgorithmVersion"), VG_MAP_ALGORITHM_VERSION)

    def test_missing_vgcount_rebuilds_single_lod_cache(self):
        """缓存完整性：VGCount 缺失必须使快路径失效并重建。"""
        ok1, message1 = EFMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws),
            unique_str_list=[self.u_lod0],
        )
        self.assertTrue(ok1, message1)
        json_path = next((self.ws / "LOD0" / "aaaabbbb-100-0").glob("TYPE_*/*.json"))
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        payload.pop("VGCount", None)
        json_path.write_text(json.dumps(payload), encoding="utf-8")

        ok2, message2 = EFMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws),
            unique_str_list=[self.u_lod0],
        )
        self.assertTrue(ok2, message2)
        self.assertNotIn("无需重新生成", message2)
        rebuilt = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(rebuilt.get("VGCount"), 1)

    def test_missing_bone_matrix_file_rebuilds_and_restores(self):
        """缓存完整性：BoneMatrix 缓存文件缺失必须重建并恢复（自愈）。"""
        ok1, message1 = EFMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws),
            unique_str_list=[self.u_lod0],
        )
        self.assertTrue(ok1, message1)
        cache_path = (
            self.ws / "LOD0" / "aaaabbbb-100-0" / "ModImpRuntime"
            / "aaaabbbb-100-0-BoneMatrix.buf"
        )
        self.assertTrue(cache_path.is_file())
        cache_path.unlink()

        ok2, message2 = EFMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws),
            unique_str_list=[self.u_lod0],
        )
        self.assertTrue(ok2, message2)
        self.assertNotIn("无需重新生成", message2)
        self.assertTrue(cache_path.is_file())

    def test_truncated_bone_matrix_file_rebuilt(self):
        """缓存完整性：缓存文件被截断（存在但损坏）也必须重建并恢复。"""
        ok1, message1 = EFMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws),
            unique_str_list=[self.u_lod0],
        )
        self.assertTrue(ok1, message1)
        cache_path = (
            self.ws / "LOD0" / "aaaabbbb-100-0" / "ModImpRuntime"
            / "aaaabbbb-100-0-BoneMatrix.buf"
        )
        cache_path.write_bytes(b"\x00" * 16)  # 存在但远小于骨骼池

        ok2, message2 = EFMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws),
            unique_str_list=[self.u_lod0],
        )
        self.assertTrue(ok2, message2)
        self.assertNotIn("无需重新生成", message2)
        self.assertGreater(cache_path.stat().st_size, 16)

    def test_joint_cache_invalidates_when_bone_matrix_missing(self):
        ok1, message1 = EFMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws),
            unique_str_list=[self.u_lod0, self.u_lod1],
        )
        self.assertTrue(ok1, message1)
        cache_path = (
            self.ws / "LOD0" / "aaaabbbb-100-0" / "ModImpRuntime"
            / "aaaabbbb-100-0-BoneMatrix.buf"
        )
        cache_path.unlink()

        ok2, message2 = EFMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws),
            unique_str_list=[self.u_lod0, self.u_lod1],
        )
        self.assertTrue(ok2, message2)
        self.assertNotIn("无需重新生成", message2)
        self.assertTrue(cache_path.is_file())

    def test_joint_cache_invalidates_when_json_missing(self):
        ok1, message1 = EFMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws),
            unique_str_list=[self.u_lod0, self.u_lod1],
        )
        self.assertTrue(ok1, message1)
        json_path = next((self.ws / "LOD0" / "aaaabbbb-100-0").glob("TYPE_*/*.json"))
        json_path.unlink()

        ok2, message2 = EFMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws),
            unique_str_list=[self.u_lod0, self.u_lod1],
        )
        # 输入 json 缺失必须使联合快路径失效，绝不能继续“无需重新生成”
        self.assertNotIn("无需重新生成", message2)
        # 存活的 LOD1 仍应保有有效 VGMap
        json1 = self._read_submesh_json("LOD1", "ccccdddd-200-0")
        self.assertEqual(json1.get("VGMap"), {"0": 0})


class FallbackDrawcallTests(unittest.TestCase):
    """P1 回归：后备 drawcall 成功读取骨骼后，元数据必须记录实际成功的 draw_index。

    旧实现把 drawcall_index_list[0] 固定写进元数据；骨骼池复制按 draw_index
    反查 vs-t0，指向第一个失败的候选会复制到错误骨骼池或根本没有文件。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="efmi_fallback_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dump = Path(self.tmp) / "dump"
        (self.dump / "deduped").mkdir(parents=True, exist_ok=True)
        deduped_abs = (self.dump / "deduped").resolve()

        # draw 000100：坏候选（无 instance cb / vs-t0，读不到骨骼）；
        # draw 000200：好候选（完整 cb2 + 骨骼池，bone_tx=2.5）
        cb = numpy.zeros((16, 4), dtype=numpy.float32)
        cb.view(numpy.uint32)[5, 1] = 4  # 骨骼段偏移（float4 单位）
        pool = numpy.zeros((800, 4), dtype=numpy.float32)
        mat = numpy.array([1, 0, 0, 0, 1, 0, 0, 0, 1, 2.5, 0, 0], dtype=numpy.float32)
        pool[7:10] = mat.reshape(3, 4)
        cb_name = f"000200-vs-cb2={CB_HASH}.buf"
        t0_name = f"000200-vs-t0={T0_HASH_A}.buf"
        (self.dump / "deduped" / cb_name).write_bytes(cb.tobytes())
        (self.dump / "deduped" / t0_name).write_bytes(pool.tobytes())
        log_lines = [
            "000100 DrawIndexedInstanced(IndexCountPerInstance:50, InstanceCount:1, "
            "StartIndexLocation:0, BaseVertexLocation:0, StartInstanceLocation:0)",
            "000200 VSSetConstantBuffers1(StartSlot:2,",
            f"2: resource=0x00000000 hash={CB_HASH} first_constant=0 num_constants=4096",
            "000200 VSSetShaderResources(StartSlot:0,",
            f"0: view=0x00000000 resource=0x00000000 hash={T0_HASH_A}",
            "000200 DrawIndexedInstanced(IndexCountPerInstance:100, InstanceCount:1, "
            "StartIndexLocation:0, BaseVertexLocation:0, StartInstanceLocation:0)",
            f"000200 3DMigoto Dumping Buffer {cb_name} -> {deduped_abs / cb_name}",
            f"000200 3DMigoto Dumping Buffer {t0_name} -> {deduped_abs / t0_name}",
        ]
        (self.dump / "log.txt").write_text("\n".join(log_lines) + "\n", encoding="utf-8")

        self.ws = _make_workspace(
            self.tmp, self.dump, Path(self.tmp) / "dump_empty"
        )
        self.u = _make_submesh(self.ws, "LOD0", "aaaabbbb-100-0")
        _write_json(self.ws / "Import.json", {self.u: GAMETYPE})
        _write_json(self.ws / "LOD0" / "ComponentName_DrawCallIndexList.json",
                    {"aaaabbbb-100-0": ["000100", "000200"]})

    def test_meta_draw_index_points_at_successful_fallback(self):
        parser = _efmi.EFMILogParser(str(self.dump / "log.txt"))
        skeletons, meta, skipped = EFMISkeletonMergeHelper._ensure_skeleton_data_for_group(
            workspace_root=str(self.ws),
            unique_str_list=[self.u],
            parser=parser,
            collect_only=True,
        )
        self.assertEqual(skipped, 0)
        self.assertIn(self.u, skeletons)
        self.assertEqual(meta[self.u]["draw_index"], "000200")

    def test_bone_matrix_cache_comes_from_successful_fallback(self):
        ok, message = EFMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws),
            unique_str_list=[self.u],
        )
        self.assertTrue(ok, message)
        pool = numpy.fromfile(
            self.ws / "LOD0" / "aaaabbbb-100-0" / "ModImpRuntime" / "aaaabbbb-100-0-BoneMatrix.buf",
            dtype=numpy.float32,
        ).reshape(-1, 4)
        # 缓存必须来自后备 draw 000200 的骨骼池（tx=2.5），而不是坏候选
        self.assertAlmostEqual(float(pool[7:10].reshape(12)[9]), 2.5, places=4)


def _make_blend_submesh(workspace, lod, bare, indices_uint32):
    """构造 R32_UINT BLENDINDICES 子网格（无 BLENDWEIGHTS、无 Position.buf）。"""
    type_dir = workspace / lod / bare / ("TYPE_" + GAMETYPE)
    type_dir.mkdir(parents=True, exist_ok=True)
    _write_json(type_dir / f"{bare}.json", {
        "CategoryBufferList": [
            {"D3D11ElementList": [
                {"Category": "Blend", "SemanticName": "BLENDINDICES",
                 "Format": "R32_UINT", "ByteWidth": 4},
            ]},
        ],
    })
    (type_dir / f"{bare}-Blend.buf").write_bytes(
        numpy.asarray(indices_uint32, dtype=numpy.uint32).tobytes()
    )
    return f"{lod}.{bare}" if lod else bare


class BlendSentinelTests(unittest.TestCase):
    """P1 回归：EFMI vg_count 按数据格式排除哨兵，bincount 前先过骨骼段上限。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="efmi_sentinel_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dump = _make_dump(Path(self.tmp) / "dumpA", "000100", CB_HASH, T0_HASH_A, 1.5)
        self.ws = _make_workspace(self.tmp, self.dump, Path(self.tmp) / "dumpB")
        self.u = _make_blend_submesh(self.ws, "LOD0", "eeeeffff-100-0",
                                     numpy.array([0], dtype=numpy.uint32))
        _write_json(self.ws / "Import.json", {self.u: GAMETYPE})
        _write_json(self.ws / "LOD0" / "ComponentName_DrawCallIndexList.json",
                    {"eeeeffff-100-0": ["000100"]})

    def _set_blend(self, indices):
        type_dir = self.ws / "LOD0" / "eeeeffff-100-0" / ("TYPE_" + GAMETYPE)
        (type_dir / "eeeeffff-100-0-Blend.buf").write_bytes(
            numpy.asarray(indices, dtype=numpy.uint32).tobytes()
        )

    def _read_json(self):
        type_dir = self.ws / "LOD0" / "eeeeffff-100-0" / ("TYPE_" + GAMETYPE)
        return json.loads((type_dir / "eeeeffff-100-0.json").read_text(encoding="utf-8"))

    def test_u4_sentinel_not_counted_as_bone(self):
        # 3 个有效索引 0 + 1 个 u4 哨兵 0xFFFFFFFF
        self._set_blend([0, 0xFFFFFFFF, 0, 0])

        ok, message = EFMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws),
            unique_str_list=[self.u],
        )
        self.assertTrue(ok, message)
        payload = self._read_json()
        self.assertEqual(payload.get("VGCount"), 1, "0xFFFFFFFF 哨兵不能算成真实骨骼")
        self.assertEqual(payload.get("VGMap"), {"0": 0})

    def test_huge_garbage_index_skipped_before_bincount(self):
        # 非哨兵巨值索引（0x0FFFFFFF）：vg_count 会超过骨骼段上限，
        # 必须在 bincount 分配之前被拦截（旧实现会先 minlength 分配数十 GB）。
        self._set_blend([0, 0x0FFFFFFF, 0, 0])

        ok, message = EFMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws),
            unique_str_list=[self.u],
        )
        self.assertFalse(ok)
        self.assertIn("没有子网格成功生成骨骼数据", message)
        payload = self._read_json()
        self.assertNotIn("VGMap", payload)


class SingleLodAtomicCacheTests(unittest.TestCase):
    """P1 回归：单 LOD 必须整组原子处理，禁止部分缓存跳过导致的槽位碰撞。

    复现用例：A VGOffset=0、B VGOffset=1；仅 B 的 BoneMatrix 缓存丢失后重新生成，
    旧实现会跳过 A、只重算 B，把 B 重新编号成 VGOffset=0 与 A 碰撞。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="efmi_atomic_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        root = Path(self.tmp)
        self.dump = root / "dump"
        (self.dump / "deduped").mkdir(parents=True, exist_ok=True)
        deduped_abs = (self.dump / "deduped").resolve()

        log_lines = []
        for draw, cb_hash, t0_hash, tx in (
            ("000100", CB_HASH, T0_HASH_A, 1.5),
            ("000200", CB_HASH_B, T0_HASH_B, 2.5),
        ):
            cb = numpy.zeros((16, 4), dtype=numpy.float32)
            cb.view(numpy.uint32)[5, 1] = 4
            pool = numpy.zeros((800, 4), dtype=numpy.float32)
            mat = numpy.array([1, 0, 0, 0, 1, 0, 0, 0, 1, tx, 0, 0], dtype=numpy.float32)
            pool[7:10] = mat.reshape(3, 4)
            cb_name = f"{draw}-vs-cb2={cb_hash}.buf"
            t0_name = f"{draw}-vs-t0={t0_hash}.buf"
            (self.dump / "deduped" / cb_name).write_bytes(cb.tobytes())
            (self.dump / "deduped" / t0_name).write_bytes(pool.tobytes())
            log_lines += [
                f"{draw} VSSetConstantBuffers1(StartSlot:2,",
                f"2: resource=0x00000000 hash={cb_hash} first_constant=0 num_constants=4096",
                f"{draw} VSSetShaderResources(StartSlot:0,",
                f"0: view=0x00000000 resource=0x00000000 hash={t0_hash}",
                f"{draw} DrawIndexedInstanced(IndexCountPerInstance:100, InstanceCount:1, "
                "StartIndexLocation:0, BaseVertexLocation:0, StartInstanceLocation:0)",
                f"{draw} 3DMigoto Dumping Buffer {cb_name} -> {deduped_abs / cb_name}",
                f"{draw} 3DMigoto Dumping Buffer {t0_name} -> {deduped_abs / t0_name}",
            ]
        (self.dump / "log.txt").write_text("\n".join(log_lines) + "\n", encoding="utf-8")

        self.ws = root / "ws"
        (self.ws / "Config").mkdir(parents=True, exist_ok=True)
        _write_json(self.ws / "Config" / "FrameAnalysisPath.json",
                    {"frameAnalysisFolderPath": str(self.dump)})
        self.u_a = _make_blend_submesh(self.ws, "", "aaaabbbb-100-0",
                                       numpy.array([0], dtype=numpy.uint32))
        self.u_b = _make_blend_submesh(self.ws, "", "ccccdddd-200-0",
                                       numpy.array([0], dtype=numpy.uint32))
        _write_json(self.ws / "Import.json",
                    {self.u_a: GAMETYPE, self.u_b: GAMETYPE})
        _write_json(self.ws / "ComponentName_DrawCallIndexList.json",
                    {"aaaabbbb-100-0": ["000100"], "ccccdddd-200-0": ["000200"]})

    def _read_json(self, bare):
        for type_dir in (self.ws / bare).iterdir():
            if type_dir.is_dir() and type_dir.name.startswith("TYPE_"):
                return json.loads((type_dir / f"{bare}.json").read_text(encoding="utf-8"))
        raise AssertionError(f"未找到 {bare} 的子网格 json")

    def test_partial_cache_invalidation_recomputes_whole_group(self):
        ok, message = EFMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws),
            unique_str_list=[self.u_a, self.u_b],
        )
        self.assertTrue(ok, message)

        json_a = self._read_json("aaaabbbb-100-0")
        json_b = self._read_json("ccccdddd-200-0")
        self.assertEqual(json_a["VGOffset"], 0)
        self.assertEqual(json_b["VGOffset"], 1)
        self.assertEqual(json_b["VGMap"], {"0": 1})

        # 仅删除 B 的 BoneMatrix 缓存（A 缓存完整）
        cache_b = self.ws / "ccccdddd-200-0" / "ModImpRuntime" / "ccccdddd-200-0-BoneMatrix.buf"
        cache_b.unlink()

        ok2, message2 = EFMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws),
            unique_str_list=[self.u_a, self.u_b],
        )
        self.assertTrue(ok2, message2)

        # 整组重算：B 绝不能单独被重算成 VGOffset=0（与 A 槽位碰撞）
        json_a2 = self._read_json("aaaabbbb-100-0")
        json_b2 = self._read_json("ccccdddd-200-0")
        self.assertEqual(json_a2["VGOffset"], 0)
        self.assertEqual(json_b2["VGOffset"], 1)
        self.assertEqual(json_a2["VGMap"], {"0": 0})
        self.assertEqual(json_b2["VGMap"], {"0": 1})
        # B 的缓存文件已恢复
        self.assertTrue(cache_b.is_file())

    def test_second_run_all_cached_skips(self):
        ok1, message1 = EFMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws),
            unique_str_list=[self.u_a, self.u_b],
        )
        self.assertTrue(ok1, message1)
        ok2, message2 = EFMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws),
            unique_str_list=[self.u_a, self.u_b],
        )
        self.assertTrue(ok2, message2)
        self.assertIn("无需重新生成", message2)

    def test_missing_target_directory_invalidates_gate_and_fails(self):
        """P2 回归：目标目录被删后不得报“全部已缓存”并静默遗漏该目标。"""
        ok1, message1 = EFMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws),
            unique_str_list=[self.u_a, self.u_b],
        )
        self.assertTrue(ok1, message1)

        # 删除 B 的整个目录（请求仍包含 B）
        shutil.rmtree(self.ws / "ccccdddd-200-0")

        ok2, message2 = EFMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws),
            unique_str_list=[self.u_a, self.u_b],
        )
        self.assertFalse(ok2, "存在无法解析的目标时不得报告完整成功")
        self.assertNotIn("无需重新生成", message2)
        self.assertIn("未生成骨骼数据", message2)
        # 存活的 A 数据仍完好
        json_a = self._read_json("aaaabbbb-100-0")
        self.assertEqual(json_a["VGOffset"], 0)

    def test_cache_copy_failure_is_not_reported_as_success(self):
        """缓存是生成事务的一部分：复制失败时不能提交 JSON/计入 written。"""
        with mock.patch.object(_efmi.shutil, "copy2", side_effect=OSError("disk full")):
            ok, message = EFMISkeletonMergeHelper.ensure_skeleton_data(
                workspace_root=str(self.ws),
                unique_str_list=[self.u_a, self.u_b],
            )

        self.assertFalse(ok)
        self.assertIn("未生成骨骼数据", message)
        for bare in ("aaaabbbb-100-0", "ccccdddd-200-0"):
            cache_path = self.ws / bare / "ModImpRuntime" / f"{bare}-BoneMatrix.buf"
            self.assertFalse(cache_path.exists())
            self.assertNotIn("BoneMatrixFileName", self._read_json(bare))

    def test_group_rebuild_refreshes_same_size_cache(self):
        """整组重建必须刷新当前 dump；同尺寸旧文件不能继续冒充最新缓存。"""
        ok, message = EFMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws),
            unique_str_list=[self.u_a, self.u_b],
        )
        self.assertTrue(ok, message)

        source = self.dump / "deduped" / f"000100-vs-t0={T0_HASH_A}.buf"
        cache_a = (
            self.ws / "aaaabbbb-100-0" / "ModImpRuntime"
            / "aaaabbbb-100-0-BoneMatrix.buf"
        )
        cache_b = (
            self.ws / "ccccdddd-200-0" / "ModImpRuntime"
            / "ccccdddd-200-0-BoneMatrix.buf"
        )
        changed = bytearray(source.read_bytes())
        changed[0:4] = numpy.float32(123.0).tobytes()
        source.write_bytes(changed)
        cache_b.unlink()  # 触发非 force 整组重建

        ok2, message2 = EFMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws),
            unique_str_list=[self.u_a, self.u_b],
        )
        self.assertTrue(ok2, message2)
        self.assertEqual(cache_a.read_bytes(), source.read_bytes())


if __name__ == "__main__":
    unittest.main()
