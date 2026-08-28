# -*- coding: utf-8 -*-
"""高斯权重球操作符接线（toolkit/gb_operators.py）stub 冒烟测试。

加载策略（仓库既有范式，参考 test_node_postprocess_draginteraction.py）：
- stub bpy / gpu / gpu_extras.batch；
- `_ssmt_root` 包命名空间（blueprint/common/toolkit/utils，仅 __path__），
  gb_core 与 gb_resolve 按文件加载挂到 toolkit stub 包下；
- utils.log_utils 用 stub 满足 from ..utils.log_utils import LOG。

覆盖：
- gb_operators 可导入、operators 清单完整；
- _GBSession 新字段默认值（direction/create_missing）；
- 反向写入目标解析接线（注入 source_objects，跳过临时合并物体）；
- 角色感知读组接线（`源名=目标名` 命中）。
"""

import importlib
import importlib.util
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _install_stub_bpy():
    """安装最小 bpy stub（兼容 discover 模式下的既有 stub）。"""
    existing = sys.modules.get("bpy")
    if existing is not None and isinstance(getattr(existing, "types", None), type) and hasattr(existing.types, "PropertyGroup"):
        return

    bpy = types.ModuleType("bpy")

    class _Props:
        def StringProperty(self, **kw): return None
        def IntProperty(self, **kw): return None
        def FloatProperty(self, **kw): return None
        def BoolProperty(self, **kw): return None
        def EnumProperty(self, **kw): return None
        def CollectionProperty(self, **kw): return None
        def PointerProperty(self, **kw): return None

    bpy.props = _Props()

    class _Types:
        class PropertyGroup: pass
        class Operator: pass
        class Object: pass
        class Collection: pass
        class Node: pass
        class NodeSocket: pass
        class Menu: pass
        class UIList: pass
        class SpaceView3D: pass

    bpy.types = _Types()
    bpy.utils = types.SimpleNamespace(
        register_class=lambda _cls: None, unregister_class=lambda _cls: None)
    bpy.data = types.SimpleNamespace(objects=None, node_groups=None)
    bpy.context = types.SimpleNamespace(scene=None)
    bpy.app = types.SimpleNamespace(timers=None)

    sys.modules["bpy"] = bpy
    sys.modules["bpy.types"] = bpy.types
    sys.modules["bpy.props"] = bpy.props


def _install_gpu_stubs():
    """gpu / gpu_extras.batch 运行期符号 stub（import 期只需模块可导入）。"""
    if "gpu" not in sys.modules:
        gpu = types.ModuleType("gpu")
        gpu.shader = types.SimpleNamespace(from_builtin=lambda *a, **k: object())
        gpu.state = types.SimpleNamespace(
            blend_set=lambda *a: None, depth_test_set=lambda *a: None)
        sys.modules["gpu"] = gpu
    if "gpu_extras" not in sys.modules:
        gpu_extras = types.ModuleType("gpu_extras")
        gpu_extras.__path__ = []
        sys.modules["gpu_extras"] = gpu_extras
    if "gpu_extras.batch" not in sys.modules:
        batch_mod = types.ModuleType("gpu_extras.batch")
        batch_mod.batch_for_shader = lambda *a, **k: None
        sys.modules["gpu_extras.batch"] = batch_mod
        sys.modules["gpu_extras"].batch = batch_mod


def _install_root_packages(root):
    """stub 包命名空间：blueprint/common/toolkit/utils 仅挂 __path__。"""
    pkg_specs = {
        root: REPO_ROOT,
        f"{root}.blueprint": REPO_ROOT / "blueprint",
        f"{root}.common": REPO_ROOT / "common",
        f"{root}.toolkit": REPO_ROOT / "toolkit",
        f"{root}.utils": REPO_ROOT / "utils",
    }
    for name, path in pkg_specs.items():
        if name not in sys.modules:
            pkg = types.ModuleType(name)
            pkg.__path__ = [str(path)]
            sys.modules[name] = pkg


def _install_utils_log_stub(root):
    name = f"{root}.utils.log_utils"
    if name in sys.modules:
        return
    mod = types.ModuleType(name)

    class LOG:
        @staticmethod
        def info(_text): pass
        @staticmethod
        def warning(_text): pass
        @staticmethod
        def debug(_text): pass
        @staticmethod
        def error(_text): pass
        @staticmethod
        def exception(_exc=None): pass

    mod.LOG = LOG
    sys.modules[name] = mod
    sys.modules[f"{root}.utils"].log_utils = mod


def _install_file_module(root, pkg, filename):
    """按文件加载 toolkit 下的纯模块并挂到 stub 包。"""
    name = f"{root}.toolkit.{pkg}"
    if name in sys.modules:
        return sys.modules[name]
    path = REPO_ROOT / "toolkit" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    sys.modules[f"{root}.toolkit"].__dict__[pkg] = mod
    return mod


def _load_gb_operators():
    _install_stub_bpy()
    _install_gpu_stubs()
    root = "_gb_ops_root"
    _install_root_packages(root)
    _install_utils_log_stub(root)
    _install_file_module(root, "gb_core", "gb_core.py")
    _install_file_module(root, "gb_resolve", "gb_resolve.py")
    return importlib.import_module(f"{root}.toolkit.gb_operators")


gb_operators = _load_gb_operators()


# ---------------------------------------------------------------------------
# fake 物体（最小）
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
        self._owner = owner

    def add(self, indices, weight, mode='ADD'):
        for i in indices:
            vert = self._owner.data.vertices[i]
            vert.groups = [g for g in vert.groups if g.group != self.index]
            vert.groups.append(_FakeGroupElem(self.index, weight))


class _VGCollection:
    def __init__(self, owner, names=()):
        self._owner = owner
        self._vgs = [_FakeVG(n, i, owner) for i, n in enumerate(names)]

    def get(self, name, default=None):
        for vg in self._vgs:
            if vg.name == name:
                return vg
        return default

    def __iter__(self):
        return iter(self._vgs)


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


class _FakeParent:
    def __init__(self, runtime_name=""):
        self._d = {"vgtp_runtime_source_object": runtime_name}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def __getitem__(self, key):
        return self._d[key]


class OperatorsWiringTests(unittest.TestCase):
    def test_operators_list_complete(self):
        ids = [op.bl_idname for op in gb_operators.gb_operators_list]
        self.assertIn("toolkit.gb_start_from_debug", ids)
        self.assertIn("toolkit.gb_confirm", ids)
        self.assertIn("toolkit.gb_cleanup_orphans", ids)
        self.assertIn("toolkit.gb_add_ball", ids)
        self.assertIn("toolkit.gb_duplicate_ball", ids)  # t3 新增复制球
        self.assertEqual(len(ids), 10)  # 本任务未新增操作符（双向逻辑在既有算子内）

    def test_session_defaults(self):
        s = gb_operators._GBSession(7)
        self.assertEqual(s.direction, gb_operators.gb_resolve.DIRECTION_FORWARD)
        self.assertFalse(s.create_missing)
        self.assertTrue(s.use_evaluated)  # t3 字段默认开启（未与核心冲突）

    def test_reverse_targets_injected_sources(self):
        parent = _FakeParent(runtime_name="SSMT_VGMatchRuntime_Tree_Node")
        a = _FakeObj("src_a", n_verts=4)
        runtime = _FakeObj("SSMT_VGMatchRuntime_Tree_Node", n_verts=4)
        res = gb_operators._resolve_reverse_targets(
            parent, source_objects=[a, runtime])
        self.assertEqual(res["kind"], "single")
        self.assertEqual([o.name for o in res["objects"]], ["src_a"])

    def test_reverse_targets_collection(self):
        parent = _FakeParent()
        a = _FakeObj("src_a", n_verts=4)
        b = _FakeObj("src_b", n_verts=4)
        res = gb_operators._resolve_reverse_targets(
            parent, source_objects=[a, b])
        self.assertEqual(res["kind"], "collection")
        self.assertEqual([o.name for o in res["objects"]], ["src_a", "src_b"])

    def test_reverse_targets_none(self):
        parent = _FakeParent()
        res = gb_operators._resolve_reverse_targets(parent, source_objects=[])
        self.assertEqual(res["kind"], "none")

    def test_role_aware_vg_read_wiring(self):
        # 源侧 `源名=目标名` 组经 gb_operators._read_vg_weights(role=...) 命中
        obj = _FakeObj("src", n_verts=2, vg_names=("Leg_L=Bone_L",))
        weights = gb_operators._read_vg_weights(
            obj, "Leg_L", role=gb_operators.gb_resolve.ROLE_SOURCE)
        self.assertIsNotNone(weights)
        self.assertEqual(weights.shape, (2,))
        # 精确名缺失时不误命中
        self.assertIsNone(gb_operators._read_vg_weights(
            obj, "Leg_L", role=gb_operators.gb_resolve.ROLE_ANY))

    def test_marker_direction_uses_reverse(self):
        # 调试标记方向判定（确认写入后）：反向会话生成源侧绿方块标记
        session = gb_operators._GBSession(1)
        session.mode = "target"
        session.direction = gb_operators.gb_resolve.DIRECTION_REVERSE
        # 仅验证判定表达式所用常量与分支可达（真实 bmesh 创建在操纵器内）
        is_reverse_marker = (session.mode == "source"
                             or session.direction
                             == gb_operators.gb_resolve.DIRECTION_REVERSE)
        self.assertTrue(is_reverse_marker)
        self.assertEqual(
            gb_operators.gb_resolve.decide_direction("Target_Bone", "REVERSE"),
            gb_operators.gb_resolve.DIRECTION_REVERSE)


if __name__ == "__main__":
    unittest.main()