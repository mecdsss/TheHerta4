"""高斯权重球：操作器与生命周期测试（stub bpy/gpu，绕开真 Blender）。

覆盖 t3 验收的交互与生命周期：
- 复制球操作器（GB_OT_DuplicateBall）：参数继承、命名、加入会话、UNDO 注册；
- undo/redo 后一致性：删除的根 → 会话清理；恢复的球 → 收养回会话；
- 孤儿会话重建：从根/球的持久属性恢复会话（undo 回退确认/取消、文件残留）；
- 真实热力图几何刷新：变形评估顶点数不匹配 → 回退基础网格 + eval_note；
- 单球贡献预览：preview_info 标注单球；
- 几何签名：同一几何稳定、姿态/形态键变化敏感。

加载方式与 test_node_postprocess_draginteraction 一致：sys.modules 预置包命名
空间（不执行真实 __init__），bpy/gpu/gpu_extras.batch/utils.log_utils 用 stub。
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path

import numpy as np


def _recover_real_numpy():
    """discover 模式下其他测试可能把 sys.modules['numpy'] 换成假实现；
    从已加载模块找回真实 numpy 并同步 sys.modules 与本模块 np 引用。
    """
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
                if getattr(candidate, "__name__", None) == "numpy" and hasattr(candidate, "float64"):
                    real_numpy = candidate
                    break
            if real_numpy is not None:
                break
        if real_numpy is not None:
            sys.modules["numpy"] = real_numpy
            np = real_numpy


REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "_gb_lifecycle_pkg"

# ---------------------------------------------------------------------------
# 假 bpy / gpu 环境
# ---------------------------------------------------------------------------

def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _Vec3:
    def __init__(self, x, y, z):
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)

    def __iter__(self):
        return iter((self.x, self.y, self.z))

    def __len__(self):
        return 3

    def __getitem__(self, i):
        return (self.x, self.y, self.z)[i]

    def copy(self):
        return _Vec3(self.x, self.y, self.z)


class _FakeMatrix:
    """4x4 矩阵假件：np.array() 可用、@ 可用、.translation 可读。"""

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
        m[:3, 3] = [loc.x, loc.y, loc.z]
        return cls(m)

    @classmethod
    def Scale(cls, radius, size):
        m = np.eye(4)
        for i in range(3):
            m[i, i] = radius
        return cls(m)


mathutils = _install_module(
    "mathutils", Matrix=_FakeMatrix,
    Vector=lambda v: _Vec3(float(v[0]), float(v[1]), float(v[2])))


class _FakeVG:
    def __init__(self, name, index):
        self.name = name
        self.index = index
        self.lock_weight = False


class _FakeVGCollection:
    """vertex_groups 假件：get() + new() + 迭代 + fromkeys 兼容。"""

    def __init__(self):
        self._items = {}

    def get(self, name, default=None):
        exact = self._items.get(name)
        if exact is not None:
            return exact
        # 角色感知剥 '=' 前缀由 gb_resolve.find_vertex_group 负责，这里仅精确名
        return default

    def new(self, name=""):
        vg = _FakeVG(name, len(self._items))
        self._items[name] = vg
        return vg

    def __iter__(self):
        return iter(self._items.values())

    def __len__(self):
        return len(self._items)


class _FakeVertices:
    def __init__(self, coords):
        self._coords = np.asarray(coords, dtype=np.float64).reshape(-1, 3)

    def __len__(self):
        return len(self._coords)

    def __getitem__(self, i):
        return types.SimpleNamespace(co=self._coords[i])

    def foreach_get(self, attr, arr):
        if attr == "co":
            arr[:] = self._coords.reshape(-1)
        elif attr == "normal":
            arr[:] = np.tile([0.0, 0.0, 1.0], len(self._coords))


class _FakeMesh:
    def __init__(self, verts, edges, tris):
        self.vertices = _FakeVertices(verts)
        self.edges = _FakeEdges(edges)
        self.loop_triangles = _FakeEdges(tris)
        self.shape_keys = None
        self.materials = []
        self.library = None

    def calc_loop_triangles(self):
        pass


class _FakeEdges:
    def __init__(self, arr):
        self._arr = np.asarray(arr, dtype=np.int64)

    def __len__(self):
        return len(self._arr)

    def foreach_get(self, attr, arr):
        arr[:] = self._arr.reshape(-1)


class _FakeObj:
    def __init__(self, name, type="EMPTY", verts=None, edges=None, tris=None,
                 gb_ball=None, matrix=None, scale=None):
        self.name = name
        self.type = type
        self._props = {}
        self.parent = None
        self.library = None
        self.matrix_world = matrix if matrix is not None else _FakeMatrix(np.eye(4))
        self.scale = scale if scale is not None else _Vec3(1, 1, 1)
        self.empty_display_type = "PLAIN_AXES"
        self.empty_display_size = 1.0
        self.location = _Vec3(0, 0, 0)
        self.show_name = False
        self.gb_ball = gb_ball if gb_ball is not None else types.SimpleNamespace(
            strength=1.0, falloff_k=4.6, use_source_sampling=False,
            use_surface_propagation=False, enabled=True)
        self.vertex_groups = _FakeVGCollection()
        self.modifiers = []
        self.hide_render = False
        self.display_type = 'WIRE'
        if type == "MESH":
            self.data = _FakeMesh(verts or [], edges or [], tris or [])
        else:
            self.data = None

    def get(self, key, default=None):
        return self._props.get(key, default)

    def __setitem__(self, key, value):
        self._props[key] = value

    def select_set(self, val):
        pass


class _FakeObjectList:
    """bpy.data.objects 假件：new() + get() + append + 迭代。"""

    def __init__(self):
        self._items = []

    def new(self, name, mesh=None):
        if mesh is not None:
            obj = _FakeObj(name, type="MESH")
            obj.data = mesh
        else:
            obj = _FakeObj(name, type="EMPTY")
        self._items.append(obj)
        return obj

    def append(self, obj):
        self._items.append(obj)

    def get(self, name, default=None):
        for o in self._items:
            if o.name == name:
                return o
        return default

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)


class _FakeData:
    def __init__(self):
        self.objects = _FakeObjectList()
        self.collections = types.SimpleNamespace(get=lambda *a, **k: None)

    def get(self, name, default=None):
        return self.objects.get(name, default)


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
    def __init__(self, active=None, scene_objects=None):
        self.active_object = active
        self.scene = types.SimpleNamespace(
            gb_props=_FakeProps(),
            collection=types.SimpleNamespace(objects=types.SimpleNamespace(
                link=lambda obj: data.objects.append(obj))),
        )
        self.view_layer = types.SimpleNamespace(
            objects=types.SimpleNamespace(active=None))
        self.window_manager = types.SimpleNamespace(windows=[])
        self._evaluated = {}

    def evaluated_depsgraph_get(self):
        return _FakeDepsgraph(list(data.objects))


# 全局 fake bpy.data（_FakeContext 与各函数引用同一实例）
data = _FakeData()

bpy_app_timers = types.SimpleNamespace(
    register=lambda *a, **k: None, unregister=lambda *a, **k: None)


def _install_bpy_stub():
    """安装/替换满足 gb_operators 的 bpy stub（discover 模式兼容）。"""
    existing = sys.modules.get("bpy")
    if existing is not None:
        has_app = getattr(getattr(existing, "app", None), "timers", None) is not None
        has_data = getattr(getattr(existing, "data", None), "objects", None) is not None
        has_types = getattr(getattr(existing, "types", None), "Operator", None) is not None
        has_props = getattr(getattr(existing, "props", None), "EnumProperty", None) is not None
        if has_app and has_data and has_types and has_props:
            return
        # 不兼容 → 替换（连同子模块，防止被旧引用重绑定）
        for sub in ("bpy", "bpy.types", "bpy.props", "bpy.data",
                    "bpy.app", "bpy.utils", "bpy.ops", "bpy.context"):
            sys.modules.pop(sub, None)
    _Props = types.SimpleNamespace(
        StringProperty=lambda **_kw: None,
        IntProperty=lambda **_kw: None,
        FloatProperty=lambda **_kw: None,
        BoolProperty=lambda **_kw: None,
        EnumProperty=lambda **_kw: None,
        PointerProperty=lambda **_kw: None,
        CollectionProperty=lambda **_kw: None,
    )
    _Types = types.SimpleNamespace(
        PropertyGroup=type("PropertyGroup", (), {}),
        Operator=type("Operator", (), {"report": lambda self, rtype, msg: None}),
        SpaceView3D=type("SpaceView3D", (), {"draw_handler_add": lambda *a, **k: 1,
                                             "draw_handler_remove": lambda *a, **k: None}),
        Scene=type("Scene", (), {}),
        Object=type("Object", (), {}),
    )
    handlers = types.SimpleNamespace(
        undo_post=[], redo_post=[], load_post=[])
    app = types.SimpleNamespace(
        timers=bpy_app_timers, handlers=handlers)
    utils = types.SimpleNamespace(
        register_class=lambda _c: None, unregister_class=lambda _c: None)
    ctx = _FakeContext()
    bpy = _install_module(
        "bpy", types=_Types, props=_Props, data=data, context=ctx,
        app=app, utils=utils,
        ops=types.SimpleNamespace(
            object=types.SimpleNamespace(
                mode_set=lambda *a, **k: None,
                vertex_group_normalize_all=lambda *a, **k: None,
                select_all=lambda *a, **k: None)))
    _install_module("bpy.types", **_Types.__dict__)
    _install_module("bpy.props", **_Props.__dict__)


_install_bpy_stub()


class _FakeShader:
    pass


# 模块级 gpu stub（gb_operators 导入时需要；GBStubTestCase.setUp 也要引用）
gpu = _install_module(
    "gpu",
    shader=types.SimpleNamespace(from_builtin=lambda *a: _FakeShader()),
    state=types.SimpleNamespace(
        blend_set=lambda *a, **k: None,
        depth_test_set=lambda *a, **k: None))
batch = _install_module(
    "gpu_extras.batch",
    batch_for_shader=lambda *a, **k: None)
sys.modules["gpu_extras"] = types.ModuleType("gpu_extras")


# ---------------------------------------------------------------------------
# 加载 toolkit 包与 gb 模块（绕过 __init__）
# ---------------------------------------------------------------------------

for name in (PKG, f"{PKG}.toolkit", f"{PKG}.utils", f"{PKG}.blueprint"):
    pkg = types.ModuleType(name)
    pkg.__path__ = []
    sys.modules[name] = pkg

for name in (f"{PKG}.utils.log_utils",):
    _install_module(
        name, LOG=types.SimpleNamespace(
            info=lambda *_a, **_k: None,
            warning=lambda *_a, **_k: None,
            debug=lambda *_a, **_k: None))

# 顶点组匹配节点仅提供解析函数 stub（生命周期测试不触碰真实匹配逻辑）
_install_module(
    f"{PKG}.blueprint.node_vertex_group_match",
    get_debug_runtime_source_object=lambda parent: None,
    get_debug_source_objects=lambda parent: [],
    get_or_create_debug_material=lambda *a, **k: None,
    SSMTNode_VertexGroupMatch=types.SimpleNamespace(
        get_or_create_debug_material=lambda *a, **k: None))


def _load_module(pkg_name, file_name):
    path = REPO_ROOT / "toolkit" / file_name
    full = f"{PKG}.toolkit.{pkg_name}"
    spec = importlib.util.spec_from_file_location(full, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    sys.modules[f"{PKG}.toolkit"].__dict__[pkg_name] = mod
    return mod


gb_core = _load_module("gb_core", "gb_core.py")
gb_resolve = _load_module("gb_resolve", "gb_resolve.py")
gb_preview = _load_module("gb_preview", "gb_preview.py")
gb_operators = _load_module("gb_operators", "gb_operators.py")


def _make_target(name="TargetMesh"):
    verts = [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0],
             [0.0, 1.0, 0.0]]
    edges = [[0, 1], [1, 2], [2, 3], [3, 0], [0, 2]]
    tris = [[0, 1, 2], [0, 2, 3]]
    obj = _FakeObj(name, type="MESH", verts=verts, edges=edges, tris=tris)
    obj.vertex_groups.new("Bone")
    obj.vertex_groups.new("Bone2")
    if name not in [o.name for o in data.objects]:
        data.objects.append(obj)
    return obj


def _make_ball(name, session_id, vg="Bone", matrix=None):
    ball = _FakeObj(name, type="EMPTY",
                    gb_ball=types.SimpleNamespace(
                        strength=1.0, falloff_k=4.6, use_source_sampling=False,
                        use_surface_propagation=False, enabled=True),
                    matrix=matrix)
    ball["gb_vg_name"] = vg
    ball["gb_session_id"] = session_id
    data.objects.append(ball)
    return ball


def _make_session(sid=10, mode="source", direction=None, vg="Bone"):
    session = gb_operators._GBSession(sid)
    session.mode = mode
    session.direction = direction or gb_resolve.DIRECTION_FORWARD
    session.vg_name = vg
    session.use_evaluated = False
    session.session_root_name = f"GB_Session_{vg}"
    return session


class _FakeEvaluatedObj:
    """evaluated_get 的评估物体假件（to_mesh/to_mesh_clear 生命周期）。"""

    def __init__(self, mesh, matrix=None):
        self.mesh = mesh
        self.matrix_world = matrix if matrix is not None else _FakeMatrix(np.eye(4))
        self.cleared = False

    def to_mesh(self, **_kwargs):
        return self.mesh

    def to_mesh_clear(self):
        self.cleared = True


def _reset_module_state():
    gb_operators._sessions.clear()
    gb_operators._next_session_id = 1
    gb_operators._active_session_id = None
    gb_operators._select_counter = 0
    gb_operators._dirty = False
    gb_operators._sync_state()
    # 场景残留（跨测试的假 bpy.data）也要清空，避免孤儿会话跨测试恢复
    data.objects._items.clear()


def _compose_ball_matrix_stub(location, radius):
    """绕过 gb_operators 内部的动态 import mathutils（discover 模式下其他测试
    可能替换了 mathutils stub，动态 import 会拿到残缺版本）。"""
    return _FakeMatrix.Translation(location) @ _FakeMatrix.Scale(max(float(radius), 1e-4), 4)


class GBStubTestCase(unittest.TestCase):
    """基类：每个测试前恢复本文件的 stub 完整性 + 模块状态。"""

    def setUp(self):
        # discover 模式下其他测试可能替换 sys.modules 里的 stub 模块；
        # 恢复 gb_operators 运行所需的最小面。
        sys.modules["gpu"] = gpu
        sys.modules["gpu_extras.batch"] = batch
        gb_operators._compose_ball_matrix = _compose_ball_matrix_stub
        _reset_module_state()


class DuplicateBallTests(GBStubTestCase):
    """NU1：复制球操作器（添加/选择/编辑/复制/删除闭环的一部分）。"""

    def setUp(self):
        super().setUp()
        self.props = _FakeProps()
        self.ctx = _FakeContext()

    def test_duplicate_copies_params_and_registers(self):
        session = _make_session(1)
        src_ball = _make_ball("GB_Bone_001", 1)
        src_ball.gb_ball.strength = 0.7
        src_ball.gb_ball.falloff_k = 3.2
        src_ball.gb_ball.use_source_sampling = True
        src_ball.matrix_world = _FakeMatrix(np.diag([0.05, 0.05, 0.05, 1.0]))
        session.ball_names = ["GB_Bone_001"]
        root = _FakeObj("GB_Session_Bone")
        root["gb_session_root"] = True
        root["gb_session_id"] = 1
        data.objects.append(root)
        gb_operators._sessions[1] = session
        gb_operators._active_session_id = 1
        gb_operators._sync_state()

        self.ctx.active_object = src_ball
        self.ctx.scene.collection = types.SimpleNamespace(
            objects=types.SimpleNamespace(link=lambda obj: None))
        op = gb_operators.GB_OT_DuplicateBall()
        result = op.execute(self.ctx)

        self.assertEqual(result, {'FINISHED'})
        self.assertEqual(len(session.ball_names), 2)
        dup_name = session.ball_names[-1]
        dup = data.get(dup_name)
        self.assertIsNotNone(dup)
        self.assertEqual(dup.gb_ball.strength, 0.7)
        self.assertEqual(dup.gb_ball.falloff_k, 3.2)
        self.assertTrue(dup.gb_ball.use_source_sampling)
        self.assertEqual(dup.get("gb_session_id"), 1)
        self.assertEqual(dup.get("gb_vg_name"), "Bone")
        self.assertEqual(self.ctx.view_layer.objects.active, dup)

    def test_duplicate_poll_requires_ball(self):
        session = _make_session(1)
        session.ball_names = []
        gb_operators._sessions[1] = session
        gb_operators._state = "active"
        self.ctx.active_object = _FakeObj("Cube")
        self.assertFalse(
            gb_operators.GB_OT_DuplicateBall.poll(self.ctx))


class GeometryRefreshTests(GBStubTestCase):
    """NU2：评估位置注入与顶点数守卫回退。"""

    def setUp(self):
        super().setUp()
        self.ctx = _FakeContext()
        self.target = _make_target()

    def _session(self, use_evaluated):
        s = _make_session(2, mode="target", direction=gb_resolve.DIRECTION_SELF)
        s.use_evaluated = use_evaluated
        return s

    def test_refresh_uses_evaluated_positions(self):
        session = self._session(use_evaluated=True)
        evaluated_mesh = _FakeMesh(
            [[0.0, 0.0, 0.0], [2.0, 0.0, 0.0], [2.0, 2.0, 0.0],
             [0.0, 2.0, 0.0]],
            [[0, 1], [1, 2], [2, 3], [3, 0], [0, 2]],
            [[0, 1, 2], [0, 2, 3]])
        evaluated_obj = _FakeEvaluatedObj(evaluated_mesh)
        # 同顶点数 → 守卫放行，位置来自评估网格
        self.target.evaluated_get = lambda dg: evaluated_obj

        tcache = gb_operators._GBTargetCache(self.target.name)
        gb_operators._refresh_target_geometry(session, tcache, self.target, self.ctx)
        self.assertEqual(tcache.verts_world.shape[0], 4)
        np.testing.assert_allclose(tcache.verts_world[1], [2.0, 0.0, 0.0])
        # 无骨骼/形态键 → 能力反馈提示（验收：清晰反馈）
        self.assertIn("无骨骼", tcache.eval_note)
        self.assertTrue(evaluated_obj.cleared)  # to_mesh_clear 已调用（无泄漏）

    def test_refresh_falls_back_on_vertex_mismatch(self):
        session = self._session(use_evaluated=True)
        # 评估网格顶点数 = 3 ≠ 基础 4 → 守卫回退基础网格位置
        evaluated_obj = _FakeEvaluatedObj(_FakeMesh(
            [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0]],
            [[0, 1], [1, 2]], [[0, 1, 2]]))
        self.target.evaluated_get = lambda dg: evaluated_obj

        tcache = gb_operators._GBTargetCache(self.target.name)
        gb_operators._refresh_target_geometry(session, tcache, self.target, self.ctx)
        self.assertEqual(tcache.verts_world.shape[0], 4)  # 回退基础
        self.assertIn("顶点数", tcache.eval_note)
        self.assertTrue(evaluated_obj.cleared)

    def test_base_mode_uses_base_mesh(self):
        session = self._session(use_evaluated=False)
        tcache = gb_operators._GBTargetCache(self.target.name)
        gb_operators._refresh_target_geometry(session, tcache, self.target, self.ctx)
        self.assertEqual(tcache.verts_world.shape[0], 4)
        self.assertEqual(tcache.eval_note, "")


class GeometrySignatureTests(GBStubTestCase):
    """NU4：几何签名决定脏检测（持续拖动只比签名，不重建数据）。"""

    def setUp(self):
        super().setUp()
        self.target = _make_target()
        self.session = _make_session(3, mode="target",
                                     direction=gb_resolve.DIRECTION_SELF)
        self.session.use_evaluated = False
        self.tcache = gb_operators._GBTargetCache(self.target.name)
        gb_operators._refresh_target_geometry(
            self.session, self.tcache, self.target, _FakeContext())

    def test_signature_stable(self):
        s1 = gb_operators._geometry_sig(self.session, self.tcache, self.target)
        s2 = gb_operators._geometry_sig(self.session, self.tcache, self.target)
        self.assertEqual(s1, s2)

    def test_signature_changes_with_matrix(self):
        s1 = gb_operators._geometry_sig(self.session, self.tcache, self.target)
        moved = self.target.matrix_world.a.copy()
        moved[0, 3] += 1.0
        self.target.matrix_world = _FakeMatrix(moved)
        s2 = gb_operators._geometry_sig(self.session, self.tcache, self.target)
        self.assertNotEqual(s1, s2)


class SingleBallPreviewTests(GBStubTestCase):
    """NU3：单球贡献 vs 组合权重。"""

    def setUp(self):
        super().setUp()
        self.props = _FakeProps()
        self.ctx = _FakeContext()
        self.target = _make_target()
        self.session = _make_session(4, mode="target",
                                     direction=gb_resolve.DIRECTION_SELF)
        self.session.use_evaluated = False
        b1 = _make_ball("GB_Bone_001", 4)
        self.session.ball_names = ["GB_Bone_001"]
        tcache = gb_operators._GBTargetCache(self.target.name)
        gb_operators._refresh_target_geometry(
            self.session, tcache, self.target, self.ctx)
        self.session.targets[self.target.name] = tcache
        gb_operators._sessions[4] = self.session

    def test_combined_preview(self):
        tcache = self.session.targets[self.target.name]
        gb_operators._update_heatmap_colors(
            self.session, tcache, self.target, self.props)
        self.assertIn("组合", tcache.preview_info)

    def test_single_ball_preview(self):
        tcache = self.session.targets[self.target.name]
        gb_operators._update_heatmap_colors(
            self.session, tcache, self.target, self.props,
            single_ball_name="GB_Bone_001")
        self.assertIn("单球 GB_Bone_001", tcache.preview_info)

    def test_unknown_single_ball_merges_with_note(self):
        tcache = self.session.targets[self.target.name]
        gb_operators._update_heatmap_colors(
            self.session, tcache, self.target, self.props,
            single_ball_name="GB_DoesNotExist")
        self.assertIn("组合", tcache.preview_info)


class GeodesicFallbackTests(GBStubTestCase):
    """NU4 回归：负/零均匀缩放不得走世界邻接表快速路径（负 cutoff 语义错误）。"""

    def setUp(self):
        super().setUp()
        self.verts = np.array(
            [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0],
             [0.0, 1.0, 0.0]])
        self.edges = np.array([[0, 1], [1, 2], [2, 3], [3, 0], [0, 2]])
        self.strength, self.falloff = 1.0, 4.6

    def _make_negative_ball(self):
        center = np.array([0.2, 0.2, 0.0])
        neg = -0.5
        m = np.eye(4)
        m[:3, 3] = center
        m[0, 0] = m[1, 1] = m[2, 2] = neg
        ball = _make_ball("GB_Ball_Neg", 99, matrix=_FakeMatrix(m))
        ball.scale = _Vec3(neg, neg, neg)
        ball.gb_ball.strength = self.strength
        ball.gb_ball.falloff_k = self.falloff
        return ball, m

    def test_negative_uniform_scale_falls_back_to_slow_path(self):
        """负均匀缩放必须回退逐球 geodesic_field（负 cutoff 会产出错误场）。"""
        ball, m = self._make_negative_ball()
        tcache = gb_operators._GBTargetCache("T")
        tcache.verts_world = self.verts
        tcache.edge_verts = self.edges

        result = gb_operators._geodesic_field_for_ball(
            ball, tcache, self.verts)
        slow = gb_core.geodesic_field(
            self.verts, m, self.strength, self.falloff, self.edges)
        np.testing.assert_allclose(result, slow, atol=1e-12)
        # 快速路径未被使用（未触发世界邻接表构建）
        self.assertIsNone(tcache.adjacency)

    def test_positive_uniform_scale_still_uses_fast_path(self):
        """正值均匀缩放仍走快速路径，且结果与慢路径一致（防过度收紧）。"""
        center = np.array([0.2, 0.2, 0.0])
        pos = 0.9
        m = np.eye(4)
        m[:3, 3] = center
        m[0, 0] = m[1, 1] = m[2, 2] = pos
        ball = _make_ball("GB_Ball_Pos", 98, matrix=_FakeMatrix(m))
        ball.scale = _Vec3(pos, pos, pos)
        ball.gb_ball.strength = self.strength
        ball.gb_ball.falloff_k = self.falloff
        tcache = gb_operators._GBTargetCache("T")
        tcache.verts_world = self.verts
        tcache.edge_verts = self.edges

        result = gb_operators._geodesic_field_for_ball(
            ball, tcache, self.verts)
        slow = gb_core.geodesic_field(
            self.verts, m, self.strength, self.falloff, self.edges)
        np.testing.assert_allclose(result, slow, atol=1e-9)
        self.assertIsNotNone(tcache.adjacency)   # 快速路径生效


class LifecycleTests(GBStubTestCase):
    """NU5：undo/redo 一致性、孤儿清理、删除匹配/清理不遗留处理器。"""

    def test_undo_validation_cleans_session_without_root(self):
        session = _make_session(5, mode="target",
                                direction=gb_resolve.DIRECTION_SELF)
        ball = _make_ball("GB_Bone_001", 5)
        session.ball_names = [ball.name]
        gb_operators._sessions[5] = session
        gb_operators._sync_state()
        # 根不存在（undo 删掉了）→ 会话应被清理，球对象保留待 CleanupOrphans
        gb_operators._validate_sessions_after_undo()
        self.assertNotIn(5, gb_operators._sessions)

    def test_undo_validation_adopts_restored_ball(self):
        session = _make_session(6, mode="target",
                                direction=gb_resolve.DIRECTION_SELF)
        # 真实 StartFromDebug 形状：root 也带 gb_vg_name（持久重建属性）
        root = _FakeObj("GB_Session_Bone")
        root["gb_session_root"] = True
        root["gb_session_id"] = 6
        root["gb_vg_name"] = "Bone"
        data.objects.append(root)
        session.session_root_name = root.name
        session.ball_names = []
        gb_operators._sessions[6] = session
        # undo 恢复的球（会话列表里没有）
        ball = _make_ball("GB_Bone_002", 6)
        gb_operators._validate_sessions_after_undo()
        self.assertIn(ball.name, session.ball_names)
        # 根绝不能作为球被收养（与 _build_session_from_objects 语义一致）
        self.assertNotIn(root.name, session.ball_names)
        # 根仍在 → 会话保留
        self.assertIn(6, gb_operators._sessions)

    def test_undo_validation_does_not_adopt_root_as_ball(self):
        """回归：_sync_session_objects 收养扫描必须排除 gb_session_root。

        StartFromDebug 的 root 带 gb_vg_name；若无排除，root 会被收进
        ball_names（球列表污染、后续场计算把空物体当球处理）。
        """
        session = _make_session(13, mode="target",
                                direction=gb_resolve.DIRECTION_SELF)
        root = _FakeObj("GB_Session_Bone")
        root["gb_session_root"] = True
        root["gb_session_id"] = 13
        root["gb_vg_name"] = "Bone"           # 对齐真实形状
        data.objects.append(root)
        ball = _make_ball("GB_Bone_001", 13)
        session.session_root_name = root.name
        session.ball_names = [ball.name]
        gb_operators._sessions[13] = session

        gb_operators._sync_session_objects(session)

        self.assertEqual(session.ball_names, [ball.name])

    def test_restore_orphan_session_from_persisted_objects(self):
        target = _make_target("TargetMesh")
        parent = _FakeObj("Debug_Match_X")
        parent["vgtp_target_name"] = "TargetMesh"
        data.objects.append(parent)

        root = _FakeObj("GB_Session_Bone")
        root["gb_session_root"] = True
        root["gb_session_id"] = 9
        root["gb_mode"] = "target"
        root["gb_vg_name"] = "Bone"
        root["gb_debug_parent"] = parent.name
        root["gb_use_evaluated"] = False
        root["gb_direction"] = gb_resolve.DIRECTION_SELF
        data.objects.append(root)
        _make_ball("GB_Bone_001", 9)

        gb_operators._restore_orphan_sessions(_FakeContext())
        self.assertIn(9, gb_operators._sessions)
        session = gb_operators._sessions[9]
        self.assertEqual(session.vg_name, "Bone")
        self.assertIn("TargetMesh", session.targets)
        self.assertEqual(session.direction, gb_resolve.DIRECTION_SELF)
        self.assertEqual(len(session.ball_names), 1)

    def test_restore_skips_without_targets(self):
        root = _FakeObj("GB_Session_Orphan")
        root["gb_session_root"] = True
        root["gb_session_id"] = 11
        root["gb_mode"] = "target"
        root["gb_vg_name"] = "Bone"
        root["gb_debug_parent"] = "Debug_Gone"  # 调试父已删除
        root["gb_direction"] = gb_resolve.DIRECTION_SELF
        data.objects.append(root)
        _make_ball("GB_Orphan_001", 11)
        gb_operators._restore_orphan_sessions(_FakeContext())
        self.assertNotIn(11, gb_operators._sessions)

    def test_cleanup_removes_sessions_and_timer_state(self):
        session = _make_session(12, mode="target",
                                direction=gb_resolve.DIRECTION_SELF)
        ball = _make_ball("GB_Bone_001", 12)
        root = _FakeObj("GB_Session_Bone")
        root["gb_session_root"] = True
        root["gb_session_id"] = 12
        data.objects.append(root)
        session.ball_names = [ball.name]
        session.session_root_name = root.name
        gb_operators._sessions[12] = session
        gb_operators._sync_state()

        gb_operators._cleanup_session(12)
        self.assertNotIn(12, gb_operators._sessions)
        self.assertEqual(gb_operators._state, "idle")

    def test_handlers_register_and_unregister(self):
        # 用 gb_operators 实际持有的 bpy 模块对象（discover 下 sys.modules 可能被换）
        handlers = gb_operators.bpy.app.handlers
        gb_operators.register_app_handlers()
        self.assertIn(gb_operators._on_undo_post, handlers.undo_post)
        self.assertIn(gb_operators._on_undo_post, handlers.redo_post)
        self.assertIn(gb_operators._on_undo_post, handlers.load_post)
        gb_operators.unregister_app_handlers()
        self.assertNotIn(gb_operators._on_undo_post, handlers.undo_post)

    def test_shutdown_unregisters_all_handlers(self):
        """复现 t8 阻塞级缺陷：shutdown() 必须完整注销 undo/redo/load 处理器。

        修复前 shutdown() 调用不存在的 _unregister_app_handlers() 抛 NameError
        （被 toolkit/__init__.py 裸 except 吞掉），三条 handler 列表内回调残留、
        注册标志不复位 —— 本测试在修复前必须 fail。
        """
        handlers = gb_operators.bpy.app.handlers
        gb_operators.unregister_app_handlers()          # 清基线
        gb_operators.register_app_handlers()
        self.assertTrue(gb_operators._handlers_registered)
        for lst in (handlers.undo_post, handlers.redo_post, handlers.load_post):
            self.assertEqual(lst.count(gb_operators._on_undo_post), 1)

        gb_operators.shutdown()                          # 插件注销路径

        self.assertFalse(gb_operators._handlers_registered)
        for name, lst in (("undo_post", handlers.undo_post),
                          ("redo_post", handlers.redo_post),
                          ("load_post", handlers.load_post)):
            self.assertNotIn(gb_operators._on_undo_post, lst, name)

    def test_handler_register_unregister_idempotent(self):
        """重复注册/注销幂等：不重复追加、不重复移除、注册标志复位后可再注册。"""
        handlers = gb_operators.bpy.app.handlers
        gb_operators.unregister_app_handlers()
        gb_operators.register_app_handlers()
        gb_operators.register_app_handlers()     # 第二次注册 no-op
        for lst in (handlers.undo_post, handlers.redo_post, handlers.load_post):
            self.assertEqual(lst.count(gb_operators._on_undo_post), 1)
        gb_operators.unregister_app_handlers()
        gb_operators.unregister_app_handlers()   # 第二次注销 no-op
        for lst in (handlers.undo_post, handlers.redo_post, handlers.load_post):
            self.assertNotIn(gb_operators._on_undo_post, lst)
        self.assertFalse(gb_operators._handlers_registered)
        # 标志已复位 → 可再次注册（模拟重复 enable/disable 插件）
        gb_operators.register_app_handlers()
        self.assertTrue(gb_operators._handlers_registered)
        self.assertIn(gb_operators._on_undo_post, handlers.undo_post)
        gb_operators.unregister_app_handlers()
        self.assertFalse(gb_operators._handlers_registered)


if __name__ == "__main__":
    unittest.main()