# -*- coding: utf-8 -*-
"""高斯权重球预览方向修复回归测试（stub bpy 全流程，toolkit/gb_operators.py）。

用户报告：原始调试物体创建后，预览应当显示“权重施加到匹配目标对象/目标
合集后的结果”，而不是继续停留在调试物体自身。

本文件在 stub bpy 场景里完整执行 GB_OT_StartFromDebug（源@原点、目标@远处
不重叠），锁定验收矩阵：
- 正向（Source_ 调试物体）：球初始锚定在接收侧目标对象/合集，目标上场非零；
- 反向（Target_ + REVERSE）：球锚定在源对象/原合集（接收侧），其上场非零；
- SELF（Target_ + AUTO）、重叠场景、多球、形态键/骨骼评估路径不回归。

stub 环境与 .dbg/gb_preview_direction_repro.py 一致：stub bpy/gpu/mathutils +
各自包命名空间（仅 __path__），绕过真实 __init__，模块级加载真实 toolkit 文件。

TDD 说明：本文件的 T1/T2/T3 类用例在修复前（球锚定在权重来源侧、采样场
“球内无源点→全零”）必须失败，修复后转绿。
"""
import importlib.util
import os
import subprocess
import sys
import types
import unittest
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "_gb_dirfix_pkg"


def _recover_real_numpy():
    """discover 模式下其他测试可能把 sys.modules['numpy'] 换成假实现；
    从已加载模块找回真实 numpy 并同步 sys.modules 与本模块 np 引用。"""
    global np
    current = sys.modules.get("numpy")
    if not hasattr(current, "float64") or not hasattr(np, "eye"):
        real_numpy = None
        for module in list(sys.modules.values()):
            for attr in ("np", "numpy"):
                try:
                    candidate = getattr(module, attr, None)
                except Exception:
                    continue
                if (getattr(candidate, "__name__", None) == "numpy"
                        and hasattr(candidate, "float64")):
                    real_numpy = candidate
                    break
            if real_numpy is not None:
                break
        if real_numpy is not None:
            sys.modules["numpy"] = real_numpy
            np = real_numpy


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


# ---------------------------------------------------------------------------
# 假 mathutils / bpy / gpu
# ---------------------------------------------------------------------------

class _Vec3:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = float(x), float(y), float(z)

    def __iter__(self):
        return iter((self.x, self.y, self.z))

    def __len__(self):
        return 3

    def __getitem__(self, i):
        return (self.x, self.y, self.z)[i]

    def copy(self):
        return _Vec3(self.x, self.y, self.z)


class _FakeMatrix:
    def __init__(self, a):
        self.a = np.asarray(a, dtype=np.float64).reshape(4, 4)
        t = self.a[:3, 3]
        self.translation = _Vec3(float(t[0]), float(t[1]), float(t[2]))

    def __array__(self, dtype=None, copy=None):
        return np.asarray(self.a, dtype=dtype)

    def __matmul__(self, other):
        return _FakeMatrix(np.asarray(self.a @ np.asarray(other, float)))

    @classmethod
    def Translation(cls, loc):
        m = np.eye(4)
        m[:3, 3] = [getattr(loc, "x", loc[0]), getattr(loc, "y", loc[1]),
                    getattr(loc, "z", loc[2])]
        return cls(m)

    @classmethod
    def Scale(cls, radius, size):
        m = np.eye(4)
        for i in range(3):
            m[i, i] = radius
        return cls(m)


# 评审修复（t7）：模块 import 环境卫生——快照既有 bpy/bpy.*/mathutils/gpu
# 引用；本文件加载完自建 gb_operators 副本后必须归还（_restore_prior_modules），
# 不得把自建 bpy stub（含自有 data）留在 sys.modules 供后续模块绑定。
_PRIOR_MODULES = {}
for _name in ("bpy", "bpy.types", "bpy.props", "bpy.data", "bpy.app",
              "bpy.utils", "bpy.ops", "bpy.context", "mathutils",
              "gpu", "gpu_extras", "gpu_extras.batch"):
    _PRIOR_MODULES[_name] = sys.modules.get(_name)


mathutils = _install_module(
    "mathutils", Matrix=_FakeMatrix,
    Vector=lambda v: _Vec3(float(v[0]), float(v[1]), float(v[2])))


class _FakeVG:
    def __init__(self, name, index):
        self.name = name
        self.index = index
        self.lock_weight = False


class _FakeVGCollection:
    def __init__(self):
        self._items = {}

    def get(self, name, default=None):
        return self._items.get(name, default)

    def new(self, name=""):
        vg = _FakeVG(name, len(self._items))
        self._items[name] = vg
        return vg

    def __iter__(self):
        return iter(self._items.values())

    def __len__(self):
        return len(self._items)


class _FakeGroupElem:
    def __init__(self, group, weight):
        self.group = group
        self.weight = weight


class _FakeVert:
    def __init__(self, co, groups=None):
        self.co = np.asarray(co, dtype=np.float64)
        self.groups = list(groups or [])


class _FakeVertices:
    def __init__(self, coords, verts):
        self._coords = np.asarray(coords, dtype=np.float64).reshape(-1, 3)
        self._verts = verts

    def __len__(self):
        return len(self._coords)

    def __getitem__(self, i):
        return self._verts[i]

    def foreach_get(self, attr, arr):
        if attr == "co":
            arr[:] = self._coords.reshape(-1)
        elif attr == "normal":
            arr[:] = np.tile([0.0, 0.0, 1.0], len(self._coords))


class _FakeEdges:
    def __init__(self, arr):
        self._arr = np.asarray(arr, dtype=np.int64)

    def __len__(self):
        return len(self._arr)

    def foreach_get(self, attr, arr):
        arr[:] = self._arr.reshape(-1)


class _FakeMesh:
    def __init__(self, owner, verts, edges, tris):
        self._verts = [_FakeVert(c) for c in verts]
        self.vertices = _FakeVertices(verts, self._verts)
        self.edges = _FakeEdges(edges)
        self.loop_triangles = _FakeEdges(tris)
        self.shape_keys = None
        self.materials = []
        self.library = None
        self._owner = owner

    def calc_loop_triangles(self):
        pass


class _FakeObj:
    def __init__(self, name, type="EMPTY", verts=None, edges=None, tris=None,
                 matrix=None):
        self.name = name
        self.type = type
        self._props = {}
        self.parent = None
        self.library = None
        self.matrix_world = matrix if matrix is not None else _FakeMatrix(np.eye(4))
        self.scale = _Vec3(1, 1, 1)
        self.empty_display_type = "PLAIN_AXES"
        self.empty_display_size = 1.0
        self.location = _Vec3(0, 0, 0)
        self.show_name = False
        self.vertex_groups = _FakeVGCollection()
        self.gb_ball = types.SimpleNamespace(
            strength=1.0, falloff_k=4.6, use_source_sampling=False,
            use_surface_propagation=False, enabled=True)
        self.modifiers = []
        self.hide_render = False
        self.display_type = 'WIRE'
        self.data = None
        self.bound_box = None
        if type == "MESH":
            self.data = _FakeMesh(self, verts or [], edges or [], tris or [])
            if verts:
                arr = np.asarray(verts, dtype=np.float64)
                mn, mx = arr.min(axis=0), arr.max(axis=0)
                self.bound_box = [
                    (mn[0], mn[1], mn[2]), (mx[0], mn[1], mn[2]),
                    (mx[0], mx[1], mn[2]), (mn[0], mx[1], mn[2]),
                    (mn[0], mn[1], mx[2]), (mx[0], mn[1], mx[2]),
                    (mx[0], mx[1], mx[2]), (mn[0], mx[1], mx[2]),
                ]

    def get(self, key, default=None):
        return self._props.get(key, default)

    def __setitem__(self, key, value):
        self._props[key] = value

    def __getitem__(self, key):
        return self._props[key]

    def select_set(self, val):
        pass


class _FakeObjectList:
    def __init__(self):
        self._items = []

    def new(self, name, mesh=None):
        obj = _FakeObj(name, type="MESH") if mesh is not None else _FakeObj(name, type="EMPTY")
        if mesh is not None:
            obj.data = mesh
        self._items.append(obj)
        return obj

    def append(self, obj):
        self._items.append(obj)

    def get(self, name, default=None):
        for o in self._items:
            if o.name == name:
                return o
        return default

    def remove(self, obj, do_unlink=True):
        if obj in self._items:
            self._items.remove(obj)

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)


class _FakeCollection:
    def __init__(self, name, objects):
        self.name = name
        self.all_objects = objects


class _FakeData:
    def __init__(self):
        self.objects = _FakeObjectList()
        self._collections = {}

    def get(self, name, default=None):
        return self.objects.get(name, default)

    def collection(self, name, objects):
        coll = _FakeCollection(name, objects)
        self._collections[name] = coll
        return coll

    def clear_scene(self):
        self.objects._items.clear()
        self._collections.clear()


class _FakeDepsgraph:
    def __init__(self, objects):
        self.objects = objects

    def __iter__(self):
        return iter(self.objects)


class _FakeProps:
    def __init__(self):
        self.tick_interval = 0.1
        self.preview_mode = "COMBINED"
        self.use_evaluated_preview = False
        self.heat_opacity = 0.85
        self.only_nearest_island = False
        self.xray_preview = True
        self.normalize_on_confirm = False
        self.status_text = "未开始"
        self.start_direction = "AUTO"
        self.start_create_missing = False
        self.clear_outside_on_write = False


class _FakeContext:
    def __init__(self, active=None):
        self.active_object = active
        self.scene = types.SimpleNamespace(
            gb_props=_FakeProps(),
            collection=types.SimpleNamespace(objects=types.SimpleNamespace(
                link=lambda obj: data.objects.append(obj))),
        )
        self.view_layer = types.SimpleNamespace(objects=types.SimpleNamespace(active=None))
        self.window_manager = types.SimpleNamespace(windows=[])

    def evaluated_depsgraph_get(self):
        return _FakeDepsgraph(list(data.objects))


data = _FakeData()
_handlers = types.SimpleNamespace(undo_post=[], redo_post=[], load_post=[])
_Context = None


def _install_bpy_stub(active=None):
    """(重新)安装满足 gb_operators 的 bpy stub；active 为当前调试物体。"""
    global _Context
    _Props = types.SimpleNamespace(
        StringProperty=lambda **_kw: None, IntProperty=lambda **_kw: None,
        FloatProperty=lambda **_kw: None, BoolProperty=lambda **_kw: None,
        EnumProperty=lambda **_kw: None, PointerProperty=lambda **_kw: None,
        CollectionProperty=lambda **_kw: None)
    _Types = types.SimpleNamespace(
        PropertyGroup=type("PropertyGroup", (), {}),
        Operator=type("Operator", (), {"report": lambda self, rtype, msg: None}),
        SpaceView3D=type("SpaceView3D", (), {
            "draw_handler_add": lambda *a, **k: 1,
            "draw_handler_remove": lambda *a, **k: None}),
        Scene=type("Scene", (), {}), Object=type("Object", (), {}))
    _Context = _FakeContext(active=active)
    bpy = _install_module(
        "bpy", types=_Types, props=_Props, data=data, context=_Context,
        app=types.SimpleNamespace(timers=types.SimpleNamespace(
            register=lambda *a, **k: None, unregister=lambda *a, **k: None),
            handlers=_handlers),
        utils=types.SimpleNamespace(register_class=lambda _c: None,
                                    unregister_class=lambda _c: None),
        ops=types.SimpleNamespace(object=types.SimpleNamespace(
            mode_set=lambda *a, **k: None,
            vertex_group_normalize_all=lambda *a, **k: None,
            select_all=lambda *a, **k: None)))
    _install_module("bpy.types", **_Types.__dict__)
    _install_module("bpy.props", **_Props.__dict__)


_install_bpy_stub()

gpu = _install_module(
    "gpu",
    shader=types.SimpleNamespace(from_builtin=lambda *a: object()),
    state=types.SimpleNamespace(blend_set=lambda *a, **k: None,
                                depth_test_set=lambda *a, **k: None))
batch = _install_module("gpu_extras.batch", batch_for_shader=lambda *a, **k: None)
sys.modules["gpu_extras"] = types.ModuleType("gpu_extras")

# 评审修复（t7）：记录本文件在 import 阶段自建的共享 stub 模块实例，
# 供环境卫生自检断言"不得残留"（归还后 sys.modules 不再指向这些实例）。
_MY_SHARED_STUBS = {
    "bpy": sys.modules.get("bpy"),
    "bpy.types": sys.modules.get("bpy.types"),
    "bpy.props": sys.modules.get("bpy.props"),
    "mathutils": mathutils,
    "gpu": gpu,
    "gpu_extras": sys.modules.get("gpu_extras"),
    "gpu_extras.batch": batch,
}

for name in (PKG, f"{PKG}.toolkit", f"{PKG}.utils", f"{PKG}.blueprint"):
    pkg = types.ModuleType(name)
    pkg.__path__ = []
    sys.modules[name] = pkg

_install_module(f"{PKG}.utils.log_utils", LOG=types.SimpleNamespace(
    info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None,
    debug=lambda *_a, **_k: None))


def _fake_get_debug_source_objects(debug_parent):
    coll_name = debug_parent.get("vgtp_source_collection", "")
    if coll_name:
        coll = data._collections.get(coll_name)
        if coll is not None:
            objs = [o for o in coll.all_objects if o.type == "MESH" and o.data]
            if objs:
                return objs
    src_name = debug_parent.get("vgtp_source_name", "")
    obj = data.objects.get(src_name)
    if obj is not None and obj.type == "MESH" and obj.data:
        return [obj]
    return []


_install_module(
    f"{PKG}.blueprint.node_vertex_group_match",
    get_debug_runtime_source_object=lambda parent: None,
    get_debug_source_objects=_fake_get_debug_source_objects,
    get_or_create_debug_material=lambda *a, **k: None,
    SSMTNode_VertexGroupMatch=types.SimpleNamespace(
        get_or_create_debug_material=lambda *a, **k: None))


def _load_module(pkg_name, file_name):
    full = f"{PKG}.toolkit.{pkg_name}"
    spec = importlib.util.spec_from_file_location(full, REPO_ROOT / "toolkit" / file_name)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    sys.modules[f"{PKG}.toolkit"].__dict__[pkg_name] = mod
    return mod


_recover_real_numpy()

gb_core = _load_module("gb_core", "gb_core.py")
gb_resolve = _load_module("gb_resolve", "gb_resolve.py")
gb_preview = _load_module("gb_preview", "gb_preview.py")
gb_operators = _load_module("gb_operators", "gb_operators.py")


def _restore_prior_modules():
    """归还模块 import 前快照的 bpy/bpy.*/mathutils/gpu 引用。

    本文件需要自建 bpy stub（含自有 data）加载 gb_operators 副本，但不得把
    它留在 sys.modules：后续文件（lifecycle 范式的"兼容即复用"检查）会把
    bpy.data 绑到本文件自建 data，导致其场景数据与 bpy.data 失配。模块
    import 完成后立即归还；tearDownModule 再归还一次，覆盖场景测试 setUp
    运行时重装 stub 的残留。
    """
    for name, mod in _PRIOR_MODULES.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod


# 关键：import 阶段结束立即归还（后续文件的导入在收集期绑定 sys.modules）
_restore_prior_modules()


def tearDownModule():
    _restore_prior_modules()


# ---------------------------------------------------------------------------
# 评审修复（t7）：模块 import 环境卫生（自检用例跑在场景用例之前）
# ---------------------------------------------------------------------------

def _bpy_reusable(existing):
    """后续文件"兼容检查通过即复用 sys.modules['bpy']"的两类既有检查形状：
    - lifecycle 形：app.timers / data.objects / types.Operator / props.EnumProperty；
    - wiring 形（test_gb_operators_wiring / draginteraction）：types 是类且带
      PropertyGroup。
    本文件归还后，后续文件按任一形状复用到的都必须是原环境（绝不能是本文件
    自建 stub/data）。
    """
    if existing is None:
        return False
    lifecycle_ok = (
        getattr(getattr(existing, "app", None), "timers", None) is not None
        and getattr(getattr(existing, "data", None), "objects", None) is not None
        and getattr(getattr(existing, "types", None), "Operator", None) is not None
        and getattr(getattr(existing, "props", None), "EnumProperty", None) is not None)
    existing_types = getattr(existing, "types", None)
    wiring_ok = (
        isinstance(existing_types, type)
        and hasattr(existing_types, "PropertyGroup"))
    return lifecycle_ok or wiring_ok


class ModuleHygieneTests(unittest.TestCase):
    """评审修复（t7）：本文件导入不得把自建 bpy stub（含自有 data）留在
    sys.modules 供后续模块绑定——模块 import 完必须归还既有引用。

    本类不继承 _DirectionFixTestCase（无 setUp 重装 stub），且定义在场景测试
    之前；注意测试运行期 sys.modules 可能已被更晚导入的文件再次替换，因此
    断言的是“本文件自建 stub 实例不残留”与“归还机制正确”，而非与某个后期
    文件的最终态比较。
    """

    def test_no_leftover_own_stub_in_sys_modules(self):
        # 本文件自建 stub 模块实例（bpy/bpy.*/mathutils/gpu）不得残留在
        # sys.modules——修复前（无归还）单文件运行时 sys.modules['bpy'] 就是
        # 本文件实例 → 断言失败；归还后不再残留。
        for name, own in _MY_SHARED_STUBS.items():
            self.assertIsNot(
                sys.modules.get(name), own,
                f"{name} 残留本文件自建 stub（应为归还后的环境）")

    def test_restore_returns_prior_references(self):
        # 归还机制：_restore_prior_modules() 后共享名回到 import 前快照
        #（或不存在）——后续 lifecycle 范式文件绑定的将是原环境而非本文件。
        _restore_prior_modules()
        for name, prior in _PRIOR_MODULES.items():
            current = sys.modules.get(name)
            if prior is None:
                self.assertIsNone(current,
                                  f"{name} 归还后应不存在（不得残留本文件 stub）")
            else:
                self.assertIs(current, prior,
                              f"{name} 归还后应为既有引用")

    def test_later_lifecycle_style_importer_never_binds_own_data(self):
        # 模拟"后续文件"（兼容检查通过即复用 sys.modules['bpy'] 并持有自己的
        # data）：在本文件归还后的状态下做兼容检查，其绑定的 bpy.data 是原
        # 环境 data，绝不是本文件自建 data（修复前残留 stub 时——本文件 stub
        # 自带 lifecycle 兼容形状——断言失败，见修复前红）。
        _restore_prior_modules()
        existing = sys.modules.get("bpy")
        if not _bpy_reusable(existing):
            self.skipTest(
                "原环境 bpy 无兼容可复用形状（单文件/无关 stub 场景）")
        bound_data = existing.data
        self.assertIsNot(bound_data, data,
                         "后续兼容复用文件不得绑定本文件自建 data（stub 应已归还）")


# ---------------------------------------------------------------------------
# 场景构建
# ---------------------------------------------------------------------------

SRC_VERTS = [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]]
TGT_VERTS_FAR = [[10, 0, 0], [10.1, 0, 0], [10.1, 0.1, 0], [10, 0.1, 0]]
TGT_VERTS_FAR2 = [[20, 0, 0], [20.1, 0, 0], [20.1, 0.1, 0], [20, 0.1, 0]]
TGT_VERTS_REV = [[10, 0, 0], [11, 0, 0], [11, 1, 0], [10, 1, 0]]


def _make_mesh(name, verts, edges=None, tris=None, matrix=None,
               vg_weights=None, vg_name="Bone"):
    edges = edges or [[0, 1], [1, 2], [2, 3], [3, 0], [0, 2]]
    tris = tris or [[0, 1, 2], [0, 2, 3]]
    obj = _FakeObj(name, type="MESH", verts=verts, edges=edges, tris=tris,
                   matrix=matrix)
    if vg_weights:
        vg = obj.vertex_groups.new(vg_name)
        for i, w in vg_weights.items():
            obj.data._verts[i].groups.append(_FakeGroupElem(vg.index, w))
    data.objects.append(obj)
    return obj


def _make_debug_parent(target_name, source_name="", source_collection="",
                       suffix="1"):
    parent = _FakeObj(f"Debug_Match_{target_name}_{suffix}", type="EMPTY")
    parent["vgtp_target_name"] = target_name
    parent["vgtp_source_name"] = source_name
    parent["vgtp_source_collection"] = source_collection
    parent["vgtp_runtime_source_object"] = ""
    parent["vgtp_matched_count"] = 1
    data.objects.append(parent)
    return parent


def _make_marker(name, parent, vg_name="Bone"):
    marker = _FakeObj(name, type="MESH",
                      verts=[[0, 0, 0]] * 8, edges=[[0, 1]], tris=[[0, 1, 2]],
                      matrix=_FakeMatrix.Translation(_Vec3(0.5, 0.3, 0.0)))
    marker.parent = parent
    marker["original_vg_name"] = vg_name
    marker["is_connected"] = True
    data.objects.append(marker)
    return marker


class _FakeEvaluatedObj:
    def __init__(self, mesh, matrix=None):
        self.mesh = mesh
        self.matrix_world = matrix

    def to_mesh(self, **_kwargs):
        return self.mesh

    def to_mesh_clear(self):
        pass


def _attach_evaluated(obj, coords, matrix=None):
    """给假物体挂 evaluated_get：返回指定坐标（模拟形态键/骨骼姿态评估位置）。"""
    mesh = _FakeMesh(obj, coords,
                     [[0, 1], [1, 2], [2, 3], [3, 0], [0, 2]],
                     [[0, 1, 2], [0, 2, 3]])
    obj.evaluated_get = lambda dg: _FakeEvaluatedObj(
        mesh, matrix=matrix or obj.matrix_world)


def _reset_state(active_marker):
    """重装 stub context + 清空会话/场景，返回 ready 的 execute 上下文。"""
    _install_bpy_stub(active=active_marker)
    gb_operators._sessions.clear()
    gb_operators._next_session_id = 1
    gb_operators._active_session_id = None
    gb_operators._dirty = True
    gb_operators._sync_state()
    return _Context


def _run_start_from_debug(marker, direction="AUTO", use_evaluated=False):
    ctx = _reset_state(marker)
    props = ctx.scene.gb_props
    props.start_direction = direction
    props.use_evaluated_preview = use_evaluated
    op = gb_operators.GB_OT_StartFromDebug()
    result = op.execute(ctx)
    assert result == {"FINISHED"}, f"execute 失败: {result}"
    return ctx, next(iter(gb_operators._sessions.values()))


def _ball_translation(session):
    ball = data.objects.get(session.ball_names[0])
    return np.asarray(ball.matrix_world)[:3, 3]


def _field_on(session, target_name):
    target = data.objects.get(target_name)
    tc = session.targets[target_name]
    return gb_operators._compute_merged_field(
        session, tc, target, _Context.scene.gb_props)


def _covered(field):
    field = np.asarray(field)
    return (int(np.count_nonzero(field > gb_core.EPS_WEIGHT)),
            float(field.max()) if field.size else 0.0)


# ---------------------------------------------------------------------------
# 验收测试
# ---------------------------------------------------------------------------

def _compose_ball_matrix_stub(location, radius):
    """绕过 gb_operators 内部的动态 import mathutils（discover 模式下其他测试
    可能替换了 mathutils stub，动态 import 会拿到残缺版本）。"""
    return (_FakeMatrix.Translation(location)
            @ _FakeMatrix.Scale(max(float(radius), 1e-4), 4))


class _DirectionFixTestCase(unittest.TestCase):
    """基类：每测试前恢复本文件 stub 面 + 清空场景/会话；每测试后归还
    sys.modules 环境（评审修复 R1：测试体/运行时重装的 bpy stub 不得跨测试、
    跨文件残留，避免污染 lifecycle 等后续文件的 data 身份）。"""

    def setUp(self):
        gb_operators._compose_ball_matrix = _compose_ball_matrix_stub
        data.clear_scene()

    def tearDown(self):
        _restore_prior_modules()


class PreviewDirectionForwardTests(_DirectionFixTestCase):
    """正向：源@原点 → 目标@(10,0,0) 不重叠。修复前球锚在源侧、目标场恒 0。"""

    def setUp(self):
        super().setUp()

    def test_t1_ball_anchors_on_receive_side(self):
        _make_mesh("SrcBone", SRC_VERTS, vg_weights={0: 0.9, 1: 0.5})
        _make_mesh("TgtMesh", TGT_VERTS_FAR)  # 接收侧目标：无组 → bbox 中心锚点
        parent = _make_debug_parent("TgtMesh", source_name="SrcBone")
        marker = _make_marker("Source_Bone", parent)

        _, session = _run_start_from_debug(marker)

        t = _ball_translation(session)
        # 球初始位置落在接收侧目标 bbox 中心（≈10.05），而非源侧质心（≈0.36）
        self.assertGreater(t[0], 9.0, f"球锚定错误（源侧）: {t}")
        np.testing.assert_allclose(t, [10.05, 0.05, 0.0], atol=0.15)

    def test_t2_receive_side_field_nonzero(self):
        _make_mesh("SrcBone", SRC_VERTS, vg_weights={0: 0.9, 1: 0.5})
        _make_mesh("TgtMesh", TGT_VERTS_FAR)
        parent = _make_debug_parent("TgtMesh", source_name="SrcBone")
        marker = _make_marker("Source_Bone", parent)

        _, session = _run_start_from_debug(marker)
        self.assertEqual(list(session.targets.keys()), ["TgtMesh"])

        covered, mx = _covered(_field_on(session, "TgtMesh"))
        # 接收侧目标必须显示非零热力图（修复前 covered=0 / max=0）
        self.assertGreater(covered, 0, "接收侧目标场覆盖为 0——预览停在源调试物体")
        self.assertGreater(mx, gb_core.EPS_WEIGHT)
        # 帧一致最近源归属：目标≈(10,0,0) 距 (1,0,0)（w0.5）9.0 < 距 (0,0,0)
        # （w0.9）10.0 → 最近源权重 0.5 × 球强度（=源最大权重 0.9）= 0.45
        np.testing.assert_allclose(
            np.asarray(_field_on(session, "TgtMesh")), [0.45] * 4, atol=1e-6)

    def test_forward_multi_target_primary_receives_field(self):
        _make_mesh("SrcBone", SRC_VERTS, vg_weights={0: 0.9, 1: 0.5})
        _make_mesh("TgtA", TGT_VERTS_FAR)
        _make_mesh("TgtB", TGT_VERTS_FAR2)
        parent_a = _make_debug_parent("TgtA", source_name="SrcBone", suffix="a")
        _make_debug_parent("TgtB", source_name="SrcBone", suffix="b")
        marker = _make_marker("Source_Bone", parent_a)

        _, session = _run_start_from_debug(marker)
        self.assertEqual(list(session.targets.keys()), ["TgtA", "TgtB"])
        covered, mx = _covered(_field_on(session, "TgtA"))
        self.assertGreater(covered, 0)
        self.assertGreater(mx, gb_core.EPS_WEIGHT)

    def test_forward_from_source_collection_receive_target_nonzero(self):
        src_a = _make_mesh("SrcA", SRC_VERTS, vg_weights={0: 0.8, 1: 0.5})
        src_b = _make_mesh("SrcB", SRC_VERTS, vg_weights={0: 0.7, 2: 0.4})
        data.collection("VGMatchSources_Tgt", [src_a, src_b])
        _make_mesh("TgtMesh", TGT_VERTS_FAR)
        parent = _make_debug_parent("TgtMesh", source_collection="VGMatchSources_Tgt")
        marker = _make_marker("Source_Bone", parent)

        _, session = _run_start_from_debug(marker)
        self.assertEqual(list(session.targets.keys()), ["TgtMesh"])
        covered, mx = _covered(_field_on(session, "TgtMesh"))
        self.assertGreater(covered, 0)
        self.assertGreater(mx, gb_core.EPS_WEIGHT)

    def test_forward_evaluated_positions_still_receive(self):
        # 形态键/骨骼姿态评估坐标：锚点与场都在评估空间（不回归）
        src = _make_mesh("SrcBone", SRC_VERTS, vg_weights={0: 0.9, 1: 0.5})
        tgt = _make_mesh("TgtMesh", TGT_VERTS_FAR)
        ev = np.asarray(TGT_VERTS_FAR, dtype=np.float64) + [0.0, 0.0, 0.2]
        ev_src = np.asarray(SRC_VERTS, dtype=np.float64) + [0.0, 0.0, 0.2]
        _attach_evaluated(src, ev_src.tolist())
        _attach_evaluated(tgt, ev.tolist())
        parent = _make_debug_parent("TgtMesh", source_name="SrcBone")
        marker = _make_marker("Source_Bone", parent)

        _, session = _run_start_from_debug(marker, use_evaluated=True)
        t = _ball_translation(session)
        self.assertGreater(t[0], 9.0, f"评估模式下球未锚定接收侧: {t}")
        # 回退锚点用（评估）顶点坐标，锚点与热力图几何同空间（z=0.2 评估偏移）
        np.testing.assert_allclose(t, [10.05, 0.05, 0.2], atol=0.15)
        covered, mx = _covered(_field_on(session, "TgtMesh"))
        self.assertGreater(covered, 0)
        self.assertGreater(mx, gb_core.EPS_WEIGHT)

    def test_overlap_scene_still_previews(self):
        # 源/目标同位（重叠）：修复前后都应有非零预览（无回归）
        _make_mesh("SrcBone",
                   [[0, 0, 0], [0.5, 0, 0], [0.5, 0.5, 0], [0, 0.5, 0]],
                   vg_weights={0: 0.9, 1: 0.5})
        _make_mesh("TgtMesh",
                   [[0.15, 0.15, 0], [0.35, 0.15, 0],
                    [0.35, 0.35, 0], [0.15, 0.35, 0]])
        parent = _make_debug_parent("TgtMesh", source_name="SrcBone")
        marker = _make_marker("Source_Bone", parent)

        _, session = _run_start_from_debug(marker)
        covered, mx = _covered(_field_on(session, "TgtMesh"))
        self.assertGreater(covered, 0)
        self.assertGreater(mx, gb_core.EPS_WEIGHT)

    def test_multiball_reaches_second_target(self):
        # 多球：第二个球覆盖第二个目标时，其场非零（多球/多目标不回归）
        _make_mesh("SrcBone", SRC_VERTS, vg_weights={0: 0.9, 1: 0.5})
        _make_mesh("TgtA", TGT_VERTS_FAR)
        _make_mesh("TgtB", TGT_VERTS_FAR2)
        parent_a = _make_debug_parent("TgtA", source_name="SrcBone", suffix="a")
        _make_debug_parent("TgtB", source_name="SrcBone", suffix="b")
        marker = _make_marker("Source_Bone", parent_a)

        _, session = _run_start_from_debug(marker)
        ball2 = _FakeObj("GB_Bone_002", type="EMPTY")
        ball2["gb_vg_name"] = "Bone"
        ball2["gb_session_id"] = session.id
        ball2.gb_ball.use_source_sampling = True
        ball2.gb_ball.strength = 1.0
        ball2.gb_ball.falloff_k = 4.6
        ball2.matrix_world = gb_operators._compose_ball_matrix(
            [20.05, 0.05, 0.0], 0.63)
        data.objects.append(ball2)
        session.ball_names.append(ball2.name)

        covered_b, mx_b = _covered(_field_on(session, "TgtB"))
        self.assertGreater(covered_b, 0, "多球下第二目标仍应为非零场")
        self.assertGreater(mx_b, gb_core.EPS_WEIGHT)
        # 第一目标场不受第二球影响（逐点 max 合并语义）
        covered_a, _ = _covered(_field_on(session, "TgtA"))
        self.assertGreater(covered_a, 0)


class PreviewDirectionReverseTests(_DirectionFixTestCase):
    """反向：目标@(10,0,0) → 源物体@原点。球必须锚定在源侧接收物体。"""

    def setUp(self):
        super().setUp()

    def test_t3_reverse_ball_anchors_on_source_side_and_field_nonzero(self):
        _make_mesh("SrcBone", SRC_VERTS, vg_weights={0: 0.8, 1: 0.5})
        _make_mesh("TgtMesh", TGT_VERTS_REV, vg_weights={0: 0.9, 1: 0.5})
        parent = _make_debug_parent("TgtMesh", source_name="SrcBone")
        marker = _make_marker("Target_Bone", parent)

        _, session = _run_start_from_debug(marker, direction="REVERSE")
        self.assertEqual(list(session.targets.keys()), ["SrcBone"])

        t = _ball_translation(session)
        # 反向接收侧 = 源物体：球锚在源侧（≈0.38），而非目标侧（≈10.36）
        self.assertLess(t[0], 5.0, f"反向球未锚定源侧接收物体: {t}")
        covered, mx = _covered(_field_on(session, "SrcBone"))
        self.assertGreater(covered, 0, "反向接收侧（源物体）场覆盖为 0")
        self.assertGreater(mx, gb_core.EPS_WEIGHT)

    def test_reverse_collection_all_sources_receive(self):
        _make_mesh("SrcA", SRC_VERTS, vg_weights={0: 0.8, 1: 0.5})
        _make_mesh("SrcB", SRC_VERTS, vg_weights={0: 0.8, 1: 0.5})
        data.collection("VGMatchSources_Tgt", [i for i in data.objects
                                               if i.name in ("SrcA", "SrcB")])
        _make_mesh("TgtMesh", TGT_VERTS_REV, vg_weights={0: 0.9, 1: 0.5})
        parent = _make_debug_parent("TgtMesh", source_collection="VGMatchSources_Tgt")
        marker = _make_marker("Target_Bone", parent)

        _, session = _run_start_from_debug(marker, direction="REVERSE")
        self.assertEqual(list(session.targets.keys()), ["SrcA", "SrcB"])
        for name in ("SrcA", "SrcB"):
            covered, mx = _covered(_field_on(session, name))
            self.assertGreater(covered, 0,
                               f"反向合集成员 {name} 场覆盖为 0")
            self.assertGreater(mx, gb_core.EPS_WEIGHT)


class PreviewDirectionSelfTests(_DirectionFixTestCase):
    """SELF（Target_ + AUTO）：来源侧==接收侧，锚点与本征场不回归。"""

    def setUp(self):
        super().setUp()

    def test_self_direction_field_nonzero_unaltered(self):
        _make_mesh("SrcBone", SRC_VERTS)  # 源关系存在即可（解析用）
        _make_mesh("TgtMesh", TGT_VERTS_REV, vg_weights={0: 0.9, 1: 0.5})
        parent = _make_debug_parent("TgtMesh", source_name="SrcBone")
        marker = _make_marker("Target_Bone", parent)

        _, session = _run_start_from_debug(marker, direction="AUTO")
        self.assertEqual(list(session.targets.keys()), ["TgtMesh"])
        t = _ball_translation(session)
        self.assertGreater(t[0], 9.0, "SELF 锚点应在目标物体上")
        covered, mx = _covered(_field_on(session, "TgtMesh"))
        self.assertGreater(covered, 0)
        self.assertGreater(mx, gb_core.EPS_WEIGHT)


def _make_gate_session(direction, src_positions, src_weights, verts,
                       ball_matrix):
    """构造采样场方向门控测试会话（球内目标顶点、源点全在球外）。

    verts = [(0,0,0), (0.95,0,0), (1.05,0,0)]，组权重只在顶点 0/2；
    球心 (0.98,0,0) 半径 0.06：only 顶点 1 在球内，两个源点都在球外。
    sampled_field（SELF）→ 球内无源点 → 0；projected（FORWARD/REVERSE）
    → 取最近源点（顶点 2，权重 0.5）。
    """
    session = gb_operators._GBSession(50)
    session.mode = "target" if direction == gb_resolve.DIRECTION_SELF else "source"
    session.direction = direction
    session.vg_name = "Bone"
    session.use_evaluated = False
    session.source_positions = np.asarray(src_positions, dtype=np.float64)
    session.source_weights = np.asarray(src_weights, dtype=np.float64)
    tcache = gb_operators._GBTargetCache("GateMesh")
    tcache.verts_world = np.asarray(verts, dtype=np.float64)
    session.targets["GateMesh"] = tcache
    ball = _FakeObj("GB_Bone_001", type="EMPTY")
    ball.matrix_world = ball_matrix
    ball.gb_ball.use_source_sampling = True
    ball.gb_ball.strength = 1.0
    ball.gb_ball.falloff_k = 4.6
    data.objects.append(ball)
    session.ball_names = [ball.name]
    return session


class DirectionGatingTests(_DirectionFixTestCase):
    """采样场按方向门控：来源侧≠接收侧（FORWARD/REVERSE）走投影语义，
    SELF 保持 sampled_field（球内源点最近邻原子语义不动）。"""

    GATE_VERTS = [[0.0, 0.0, 0.0], [0.95, 0.0, 0.0], [1.05, 0.0, 0.0]]
    GATE_SRC = [[0.0, 0.0, 0.0], [1.05, 0.0, 0.0]]
    GATE_SRC_W = [0.9, 0.5]

    def setUp(self):
        super().setUp()

    def _ball_matrix(self):
        return _compose_ball_matrix_stub([0.98, 0.0, 0.0], 0.06)

    def _merged(self, session):
        target = _FakeObj("GateMesh", type="MESH",
                          verts=self.GATE_VERTS + [[0.0, 0.0, 0.0]],
                          edges=[[0, 1]], tris=[[0, 1, 2]])
        return gb_operators._compute_merged_field(
            session, session.targets["GateMesh"], target,
            _Context.scene.gb_props)

    def test_self_keeps_sampled_field_atomic_semantics(self):
        # 源点全在球外：SELF 必须保持 sampled_field 的“球内无源点→全零”
        # （防穿透原子语义），不得被投影语义改成非零
        session = _make_gate_session(
            gb_resolve.DIRECTION_SELF, self.GATE_SRC, self.GATE_SRC_W,
            self.GATE_VERTS, self._ball_matrix())
        field = self._merged(session)
        covered, mx = _covered(field)
        self.assertEqual(covered, 0, "SELF 不得走投影语义（球内无源点应为 0）")
        self.assertEqual(mx, 0.0)

    def test_forward_uses_projected_semantics(self):
        session = _make_gate_session(
            gb_resolve.DIRECTION_FORWARD, self.GATE_SRC, self.GATE_SRC_W,
            self.GATE_VERTS, self._ball_matrix())
        field = self._merged(session)
        covered, mx = _covered(field)
        self.assertEqual(covered, 1, "FORWARD 应走投影语义（球内目标取最近源点）")
        self.assertAlmostEqual(mx, 0.5, places=6)

    def test_reverse_uses_projected_semantics(self):
        session = _make_gate_session(
            gb_resolve.DIRECTION_REVERSE, self.GATE_SRC, self.GATE_SRC_W,
            self.GATE_VERTS, self._ball_matrix())
        field = self._merged(session)
        covered, mx = _covered(field)
        self.assertEqual(covered, 1)
        self.assertAlmostEqual(mx, 0.5, places=6)


class ProjectedFieldNonunitMatrixTests(_DirectionFixTestCase):
    """gb_core.projected_sampled_field 非单位球矩阵帧一致回归（repair round 2）。

    最近邻必须在球局部帧内比较（diff = v_local - s_local，与 sampled_field
    一致）；若拿球局部目标顶点对世界源点求差，平移/旋转/非均匀缩放会误指
    更远端源点。每例在修复前（混合帧）断言失败。
    """

    def _identity(self):
        return np.eye(4, dtype=np.float64)

    def _matrix(self, loc=(0.0, 0.0, 0.0), scale=(1.0, 1.0, 1.0)):
        m = self._identity()
        m[0, 0], m[1, 1], m[2, 2] = scale
        m[0, 3], m[1, 3], m[2, 3] = loc
        return m

    def _field(self, target_verts, src_pos, src_w, matrix):
        return gb_core.projected_sampled_field(
            np.asarray(target_verts, dtype=np.float64),
            np.asarray(src_pos, dtype=np.float64),
            np.asarray(src_w, dtype=np.float64),
            matrix)

    def test_translation_frame_consistent_nearest(self):
        # 球平移 (5,0,0)：世界帧最近源 = (6.2,0,0) 权重 0.6
        # （距目标 (5.5,0,0) 0.7 < (4.5,0,0) 的 1.0）
        m = self._matrix(loc=(5.0, 0.0, 0.0))
        src_pos = np.array([[4.5, 0.0, 0.0], [6.2, 0.0, 0.0]])
        src_w = np.array([0.9, 0.6])
        verts = np.array([[5.5, 0.0, 0.0]])  # 球内（d=0.5）
        field = self._field(verts, src_pos, src_w, m)
        self.assertAlmostEqual(float(field[0]), 0.6, places=6)

    def test_nonuniform_scale_frame_consistent_nearest(self):
        # 非均匀缩放椭球 (3,1,1)：帧一致最近源 = (2,0,0) 权重 0.6
        # （局部 |0.5-0.667|=0.167 < |0.5-0|=0.5）；混合帧会被 (0,0,0) 0.9 误导
        m = self._matrix(scale=(3.0, 1.0, 1.0))
        src_pos = np.array([[0.0, 0.0, 0.0], [2.0, 0.0, 0.0]])
        src_w = np.array([0.9, 0.6])
        verts = np.array([[1.5, 0.0, 0.0]])  # 球内（x 方向 d=0.5）
        field = self._field(verts, src_pos, src_w, m)
        self.assertAlmostEqual(float(field[0]), 0.6, places=6)

    def test_rotation_frame_consistent_nearest(self):
        # 绕 Z 旋转 90°：一致帧最近源 = (0,0.5,0) 权重 0.6（局部与目标同点 d=0）；
        # 混合帧（局部目标 vs 世界源）会被 x 轴近源 (0.6,0,0) 0.9 误导
        theta = np.pi / 2.0
        rot = np.array([
            [np.cos(theta), -np.sin(theta), 0.0],
            [np.sin(theta), np.cos(theta), 0.0],
            [0.0, 0.0, 1.0],
        ])
        m = self._identity()
        m[:3, :3] = rot
        src_pos = np.array([[0.6, 0.0, 0.0], [0.0, 0.5, 0.0]])
        src_w = np.array([0.9, 0.6])
        verts = np.array([[0.0, 0.5, 0.0]])  # 局部 (0.5,0,0)，球内
        field = self._field(verts, src_pos, src_w, m)
        self.assertAlmostEqual(float(field[0]), 0.6, places=6)


class ReceiveSideAnchorTests(_DirectionFixTestCase):
    """_receive_side_anchor 回退健壮性（t7 评审可选加固，非阻断）。"""

    def setUp(self):
        super().setUp()

    def test_fallback_anchor_none_when_no_verts(self):
        # 接收物体顶点为空：回退分支必须返回 None（调用方保持既有锚点），
        # 不得返回 NaN（修复前 mean(空) = NaN 并触发 RuntimeWarning）
        empt = _make_mesh("EmptyTgt", [])
        anchor = gb_operators._receive_side_anchor(
            [empt], "Bone", False, _Context)
        self.assertIsNone(anchor)

    def test_group_centroid_anchor_still_works(self):
        src = _make_mesh("SrcAnchor", SRC_VERTS, vg_weights={0: 0.9, 1: 0.5})
        anchor = gb_operators._receive_side_anchor(
            [src], "Bone", False, _Context)
        self.assertIsNotNone(anchor)
        self.assertGreater(float(anchor[0]), 0.0)


class CrossFileOrderRegressionTests(unittest.TestCase):
    """评审修复（t7 R1）：本文件不得永久污染 sys.modules['bpy']/mathutils 或
    lifecycle 测试的 data 身份——与 test_gb_operators_lifecycle 的**导入/执行
    顺序无关**：direction→lifecycle 与 lifecycle→direction 两个真实 pytest
    子进程都必须全绿（子进程内本类用例经环境变量跳过，避免递归）。"""

    _SUBPROCESS_GUARD = "GB_DIRFIX_NO_SUBPROCESS"

    FILES = [
        str(REPO_ROOT / "tests" / "test_gb_preview_direction.py"),
        str(REPO_ROOT / "tests" / "test_gb_operators_lifecycle.py"),
    ]

    def setUp(self):
        if os.environ.get(self._SUBPROCESS_GUARD):
            self.skipTest("子进程内不再递归运行跨文件顺序回归")

    def _run_pair(self, order):
        env = dict(os.environ)
        env[self._SUBPROCESS_GUARD] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", *order, "-q"],
            cwd=str(REPO_ROOT), env=env,
            capture_output=True, timeout=240)
        tail = (proc.stdout.decode("utf-8", errors="replace")[-2500:]
                + proc.stderr.decode("utf-8", errors="replace")[-1500:])
        return proc, tail

    def test_direction_then_lifecycle_order(self):
        proc, tail = self._run_pair(self.FILES)
        self.assertEqual(proc.returncode, 0,
                         f"direction→lifecycle 顺序必须全绿:\n{tail}")

    def test_lifecycle_then_direction_order(self):
        proc, tail = self._run_pair(list(reversed(self.FILES)))
        self.assertEqual(proc.returncode, 0,
                         f"lifecycle→direction 顺序必须全绿:\n{tail}")


if __name__ == "__main__":
    unittest.main()