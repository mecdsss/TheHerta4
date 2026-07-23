# -*- coding: utf-8 -*-
"""PC 模块集成冒烟测试：用 fake bpy/mathutils 加载 bpy 依赖层，
验证跨模块引用与注册列表完整性（不需要真实 Blender）。"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PKG = "theherta4"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _FakeProp:
    """模拟 bpy.props.* 声明函数：返回 None 即可（类注解时赋值）。"""
    def __getattr__(self, item):
        def _factory(*args, **kwargs):
            return None
        return _factory


class _FakeBpyTypes:
    class Operator: pass
    class Panel: pass
    class UIList: pass
    class PropertyGroup: pass
    class Object: pass
    class ID: pass

    def __getattr__(self, item):
        # 任意 bpy.types.X 注解动态解析为占位类（函数签名注解在 def 时求值）
        cls = type(item, (), {})
        setattr(self, item, cls)
        return cls


class PcModuleIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 1) 安装包占位
        for package_name in (PKG, f"{PKG}.toolkit"):
            package = _install_module(package_name)
            package.__path__ = [str(ROOT / package_name.split('.')[-1])] \
                if package_name != PKG else [str(ROOT)]

        # 2) fake mathutils（pc_engine 不依赖，但 bridge 需要）
        import numpy as np
        mathutils = _install_module("mathutils")
        mathutils.Vector = lambda *a: np.array(a[0] if len(a) == 1 else a, dtype=float)
        mathutils.Matrix = lambda *a: np.array(a[0] if len(a) == 1 and a else np.identity(4), dtype=float)
        kd_mod = _install_module("mathutils.kdtree")
        mathutils.kdtree = kd_mod

        # 3) fake bpy
        bpy_mod = _install_module("bpy")
        bpy_mod.types = _FakeBpyTypes()
        bpy_mod.props = _FakeProp()
        bpy_mod.app = types.SimpleNamespace(
            timers=types.SimpleNamespace(register=lambda *a, **k: None,
                                         unregister=lambda *a, **k: None))
        bpy_mod.utils = types.SimpleNamespace(register_class=lambda c: None,
                                              unregister_class=lambda c: None)
        bpy_mod.context = types.SimpleNamespace()
        bpy_bpy_types = _install_module("bpy.types")
        bpy_bpy_props = _install_module("bpy.props")
        bpy_bpy_props.__dict__.update({})
        bpy_app = _install_module("bpy.app")
        bpy_app_timers = _install_module("bpy.app.timers")

        # 4) 按依赖序加载模块（importlib 文件路径模式，与仓库现有测试一致）
        def _load(mod_name, rel_path):
            spec = importlib.util.spec_from_file_location(mod_name, ROOT / rel_path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
            return module

        cls.pc_engine = _load(f"{PKG}.toolkit.pc_engine", "toolkit/pc_engine.py")
        cls.pc_backend = _load(f"{PKG}.toolkit.pc_backend", "toolkit/pc_backend.py")
        cls.pc_virtualrig = _load(f"{PKG}.toolkit.pc_virtualrig", "toolkit/pc_virtualrig.py")
        cls.pc_bridge = _load(f"{PKG}.toolkit.pc_bridge", "toolkit/pc_bridge.py")
        cls.pc_properties = _load(f"{PKG}.toolkit.pc_properties", "toolkit/pc_properties.py")
        cls.pc_operators = _load(f"{PKG}.toolkit.pc_operators", "toolkit/pc_operators.py")
        cls.pc_panel = _load(f"{PKG}.toolkit.pc_panel", "toolkit/pc_panel.py")

    def test_engine_no_bpy_import(self):
        """pc_engine 必须保持纯 numpy：AST 层面不得 import bpy/mathutils。"""
        import ast
        src = (ROOT / "toolkit" / "pc_engine.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split('.')[0])
        self.assertNotIn("bpy", imported)
        self.assertNotIn("mathutils", imported)

    def test_properties_list_complete(self):
        self.assertEqual(len(self.pc_properties.pc_properties_list), 2)
        names = [c.__name__ for c in self.pc_properties.pc_properties_list]
        self.assertIn("PC_BoneItem", names)
        self.assertIn("PC_Properties", names)

    def test_operators_list_count(self):
        self.assertEqual(len(self.pc_operators.pc_operators_list), 14)

    def test_select_pose_bones_uses_blender_5_context_selection(self):
        arm = types.SimpleNamespace(
            mode='POSE',
            data=types.SimpleNamespace(
                bones=[types.SimpleNamespace(name='Selected')]),
        )
        items = [
            types.SimpleNamespace(name='Selected', enabled=False),
            types.SimpleNamespace(name='Unselected', enabled=True),
        ]
        props = types.SimpleNamespace(b_armature=arm, bone_list=items)
        context = types.SimpleNamespace(
            object=arm,
            scene=types.SimpleNamespace(pc_props=props),
            selected_pose_bones=[types.SimpleNamespace(name='Selected')],
            selected_editable_bones=None,
        )
        operator = self.pc_operators.PC_OT_SelectPoseBones()
        operator.report = lambda *_args: None

        result = operator.execute(context)

        self.assertEqual(result, {'FINISHED'})
        self.assertTrue(items[0].enabled)
        self.assertFalse(items[1].enabled)

    def test_panel_list_complete(self):
        names = [c.__name__ for c in self.pc_panel.pc_panel_list]
        self.assertIn("PC_UL_BoneList", names)
        self.assertIn("PC_PT_MainPanel", names)

    def test_panel_references_valid_operators(self):
        """pc_panel 引用的所有操作符必须存在于 pc_operators。"""
        panel = self.pc_panel
        ops = self.pc_operators
        for attr in dir(panel):
            if attr.startswith("PC_OT_"):
                self.assertTrue(hasattr(ops, attr), f"pc_panel 引用了不存在的 {attr}")

    def test_bridge_provides_engine_protocol(self):
        """bridge 必须提供 engine 需要的骨点协议键。"""
        bridge = self.pc_bridge
        for fn in ("build_cache", "read_b_samples", "make_nn_provider",
                   "bone_point_provider_factory", "apply_basis_to_armature",
                   "update_view", "validate_cache", "lock_info_text",
                   "find_skinned_meshes", "get_basis", "set_basis"):
            self.assertTrue(hasattr(bridge, fn), f"pc_bridge 缺少 {fn}")

    def test_engine_public_api(self):
        engine = self.pc_engine
        for name in ("PCBoneSpec", "PCFitConfig", "PCMetric", "PCFitSession",
                     "PCSchedule", "kabsch_rotation", "overlap_metric",
                     "mask_rotation_delta", "brute_force_nn",
                     "TF_ROTATION", "TF_SCALE", "TF_LOCATION"):
            self.assertTrue(hasattr(engine, name), f"pc_engine 缺少 {name}")


    def test_backend_module_api(self):
        backend_mod = self.pc_backend
        self.assertTrue(hasattr(backend_mod, 'NumpyBackend'))
        self.assertTrue(hasattr(backend_mod, 'TorchBackend'))
        self.assertTrue(hasattr(backend_mod, 'select_backend'))
        backend, info = backend_mod.select_backend('NUMPY')
        self.assertFalse(backend.is_gpu)

    def test_engine_minibatch_config(self):
        import dataclasses
        names = {f.name for f in dataclasses.fields(self.pc_engine.PCFitConfig)}
        self.assertIn('minibatch_size', names)
        self.assertIn('full_eval_interval', names)
        self.assertTrue(hasattr(self.pc_engine, 'lbs_transform'))

    def test_bridge_lbs_api(self):
        bridge = self.pc_bridge
        for fn in ('PCLBSData', 'build_lbs', 'lbs_read'):
            self.assertTrue(hasattr(bridge, fn), f'pc_bridge 缺少 {fn}')

    def test_thinning_returns_exact_requested_count(self):
        idx = self.pc_bridge._thin_indices(100, 17)
        self.assertEqual(len(idx), 17)
        self.assertEqual(int(idx[0]), 0)
        self.assertEqual(int(idx[-1]), 99)

    def test_a_b_sampling_counts_are_independently_bounded(self):
        bounded = self.pc_bridge._bounded_sample_count
        self.assertEqual(bounded(700, 800), 700)
        self.assertEqual(bounded(1200, 800), 800)

    def test_spatial_sampling_tracks_structure_not_vertex_density(self):
        import numpy as np
        dense = np.linspace(0.0, 0.1, 90)
        sparse = np.linspace(1.0, 10.0, 10)
        points = np.column_stack((np.concatenate((dense, sparse)),
                                  np.zeros(100), np.zeros(100)))
        idx = self.pc_bridge._spatial_sample_indices(points, 10)
        sampled_x = points[idx, 0]
        self.assertLessEqual(len(idx), 10)
        self.assertGreater(float(sampled_x.max() - sampled_x.min()), 8.0)
        self.assertGreaterEqual(int(np.count_nonzero(sampled_x >= 1.0)), 5)

    def test_virtualrig_module_api(self):
        vrig_mod = self.pc_virtualrig
        for sym in ('PCVirtualRig', 'build_virtual_rig', 'validate_pose_recursion'):
            self.assertTrue(hasattr(vrig_mod, sym), f'pc_virtualrig 缺少 {sym}')
        bridge = self.pc_bridge
        for fn in ('build_virtual_rig_from_cache', 'validate_virtual_rig'):
            self.assertTrue(hasattr(bridge, fn), f'pc_bridge 缺少 {fn}')

    def test_properties_perf_fields(self):
        src_props = (ROOT / 'toolkit' / 'pc_properties.py').read_text(encoding='utf-8')
        for token in ('backend_mode', 'use_fast_lbs', 'use_approximate_fallback', 'minibatch_size',
                      'full_eval_interval', 'perf_info'):
            self.assertIn(token, src_props, f'pc_properties 缺少 {token}')


    def test_no_self_recursive_helpers(self):
        """防批量替换误伤：模块级辅助函数不得自递归调用。"""
        import ast
        src_ops = (ROOT / 'toolkit' / 'pc_operators.py').read_text(encoding='utf-8')
        tree = ast.parse(src_ops)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) \
                        and isinstance(sub.func, ast.Name) \
                        and sub.func.id == node.name:
                    self.fail(f'辅助函数 {node.name} 存在自递归调用（批量替换误伤）')

    def test_refresh_helper_calls_valid(self):
        """_refresh_after_state_change 两个分支必须调用合法刷新函数。"""
        import ast
        src_ops = (ROOT / 'toolkit' / 'pc_operators.py').read_text(encoding='utf-8')
        tree = ast.parse(src_ops)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == '_refresh_after_state_change':
                calls = {sub.func.id for sub in ast.walk(node)
                         if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)}
                self.assertIn('_sync_viewport', calls)
                self.assertNotIn('_refresh_after_state_change', calls)
                return
        self.fail('未找到 _refresh_after_state_change 定义')

    def test_running_history_seek_is_processed_before_iteration(self):
        src = (ROOT / 'toolkit' / 'pc_operators.py').read_text(encoding='utf-8')
        seek_branch = src.index('elif _pending_seek_step is not None:')
        running_branch = src.index("elif _state == 'running':", seek_branch)
        self.assertLess(seek_branch, running_branch)
        self.assertIn('_worker.seek(target)', src)

    def test_step_once_updates_module_state(self):
        ops = self.pc_operators
        original_session = ops._session
        original_worker = ops._worker
        original_state = ops._state
        original_latest = ops._latest_result
        original_apply = ops._apply_enabled_flags
        original_refresh = ops._refresh_after_state_change
        original_update = ops._update_display_props

        class _Session:
            def __init__(self):
                self.count = 0

            def step(self):
                self.count += 1
                return object()

        try:
            ops._session = _Session()
            ops._worker = None
            ops._state = 'running'
            ops._latest_result = 'sentinel'
            ops._apply_enabled_flags = lambda: None
            ops._refresh_after_state_change = lambda _context: None
            ops._update_display_props = lambda _context: None

            operator = ops.PC_OT_StepOnce()
            operator.count = 1
            operator.report = lambda *_args: None
            context = types.SimpleNamespace(
                scene=types.SimpleNamespace(pc_props=types.SimpleNamespace()))

            result = operator.execute(context)

            self.assertEqual(result, {'FINISHED'})
            self.assertEqual(ops._state, 'paused')
            self.assertIsNone(ops._latest_result)
        finally:
            ops._session = original_session
            ops._worker = original_worker
            ops._state = original_state
            ops._latest_result = original_latest
            ops._apply_enabled_flags = original_apply
            ops._refresh_after_state_change = original_refresh
            ops._update_display_props = original_update

    def test_headless_does_not_blanket_disable_controller_bones(self):
        src = (ROOT / 'toolkit' / 'pc_operators.py').read_text(encoding='utf-8')
        self.assertNotIn("spec.kind != 'deform' or not _vrig.has_bone", src)
        self.assertIn("spec.kind = 'deform'", src)


if __name__ == "__main__":
    unittest.main()
