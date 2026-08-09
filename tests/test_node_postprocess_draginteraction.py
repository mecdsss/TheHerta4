"""拖拽交互后处理节点的纯逻辑测试（stub bpy，绕过 Blender 环境）。

加载策略：在 sys.modules 预置 `_ssmt_root` 包命名空间（blueprint/common/toolkit/utils，
仅 __path__，不执行任何真实 __init__.py），让 from . / from .. 相对导入按文件路径解析真实
模块；bpy 链上的 node_base / node_postprocess_base / toolkit.gb_core 用按需 stub 或文件级
加载，绕开会 import bpy 的 __init__ 链。

覆盖计划 v3.4 §9 的关键断言：
- 必需段齐全、二次执行幂等；
- 钩子注入位置（ib= 之后、第一个 run=/drawindexed= 之前）；ib=null 段不注入；
- ObjectMap 布局 (1+N)×16B、分区局部索引空间 (firstIndex=0, indexCount, mode=7, objectID=0)；
- Bake 偏移 = i×⌊part_count/8⌋、gs-t1 为 part IB 资源名；
- 稀疏区域 ABI：稳定 ID、Top-4 权重、ZoneParams 与 256 区域容量；
- ini 侧裁剪：无 dump 行、无 cs-u4/u7 绑定、有 cs-u2/u6/t73/t74、w84=0、x74=0、x75=0；
- boot-clear 块存在且幂等；grabbable 始终显式生成。
"""

import importlib.util
import os
import struct
import sys
import types
import unittest
from collections import OrderedDict
from pathlib import Path

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]


def _install_stub_bpy():
    """安装最小 bpy stub（满足节点模块的 bpy 引用）。

    discover 模式下其他测试可能先装了不兼容的 bpy stub（如 SimpleNamespace），
    这里检测兼容性，不兼容则替换。
    """
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

    bpy.types = _Types()

    class _Utils:
        @staticmethod
        def register_class(cls): pass
        @staticmethod
        def unregister_class(cls): pass

    bpy.utils = _Utils()
    bpy.data = types.SimpleNamespace(objects=None, node_groups=None)
    bpy.context = types.SimpleNamespace(scene=None)

    sys.modules["bpy"] = bpy
    sys.modules["bpy.types"] = bpy.types
    sys.modules["bpy.props"] = bpy.props


def _install_utils_error_stub(root):
    """utils.ssmt_error_utils（object_prefix_helper 依赖）。"""
    name = f"{root}.utils.ssmt_error_utils"
    if name in sys.modules:
        return
    mod = types.ModuleType(name)

    class SSMTErrorUtils:
        @staticmethod
        def log_and_raise(*a, **k):
            raise RuntimeError(a[0] if a else "SSMTError")

    mod.SSMTErrorUtils = SSMTErrorUtils
    sys.modules[name] = mod
    sys.modules[f"{root}.utils"].ssmt_error_utils = mod


def _install_node_base_stub(root):
    """node_postprocess_base 只依赖 node_base.SSMTNodeBase → 用 stub 满足。"""
    name = f"{root}.blueprint.node_base"
    if name in sys.modules:
        return
    nb = types.ModuleType(name)

    class SSMTNodeBase:
        pass

    nb.SSMTNodeBase = SSMTNodeBase
    sys.modules[name] = nb
    sys.modules[f"{root}.blueprint"].node_base = nb


def _install_toolkit_gb_core(root):
    """gb_core 纯 numpy（无 bpy），按文件加载挂到 toolkit stub 包下。"""
    name = f"{root}.toolkit.gb_core"
    if name in sys.modules:
        return
    path = REPO_ROOT / "toolkit" / "gb_core.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    sys.modules[f"{root}.toolkit"].gb_core = mod


def _load_drag_module():
    """用纯 stub 包路径加载节点模块（不执行任何真实 __init__.py）。"""
    _install_stub_bpy()
    import importlib

    root = "_ssmt_root"
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

    _install_utils_error_stub(root)
    _install_node_base_stub(root)
    _install_toolkit_gb_core(root)

    return importlib.import_module(f"{root}.blueprint.node_postprocess_draginteraction")


def _make_node(mod, **props):
    """实例化节点类并注入属性默认值（绕过 bpy 属性系统）。"""
    node = mod.SSMTNode_PostProcess_DragInteraction.__new__(mod.SSMTNode_PostProcess_DragInteraction)
    defaults = dict(
        hash_values="", mod_namespace="", grab_key="ALT", grab_gesture="LMB", poke_gesture="RMB",
        enable_poke=True, enable_hand_cursor=False, enable_viewport_probe=True,
        drag_system_mode_default=2, drag_mode_initialized=True,
        drag_mode_variable_name="ssmtdrag_drag_enabled",
        mode_toggle_key="f8",
        shapekey_drive_move_sensitivity=0.02,
        ui_detected_variable_name="ssmtdrag_ui_detected",
        ui_zone_variable_name="ssmtdrag_ui_zone",
        phys_grab_damping=0.86, phys_grab_spring=0.176,
        phys_release_damping=0.96, phys_release_spring=0.055,
        phys_release_kick=0.12, phys_target_follow=1.10,
        mult_radius=1.0, mult_strength=0.333, mult_spring=0.333, mult_damping=1.0,
        zone_objects=[], bake_reference_object=None, mask_plateau=0.0,
    )
    defaults.update(props)
    for k, v in defaults.items():
        object.__setattr__(node, k, v)
    return node


def _base_sections(hash_value="abc123", base_name="abc123-43191", vertex_count=14078):
    """构造一份标准 ZZMI ini sections（含 VLR、Position 资源、IB 绘制段）。"""
    return OrderedDict([
        ("[Constants]", ["global $active = 0"]),
        (f"[TextureOverride_{hash_value}_{base_name}_VertexLimitRaise]", [
            f"hash = {hash_value}",
            "override_byte_stride = 40",
            f"override_vertex_count = {vertex_count}",
            "uav_byte_stride = 4",
        ]),
        (f"[TextureOverride_{hash_value}_{base_name}A]", [
            f"hash = {hash_value}",
            "match_first_index = 0",
            f"ib = Resource{base_name}AIB",
            "run = CommandListSkinTexture",
            "drawindexed = 52688, 0, 0",
        ]),
        (f"[TextureOverride_{hash_value}_{base_name}B]", [
            f"hash = {hash_value}",
            "match_first_index = 52688",
            f"ib = Resource{base_name}BIB",
            # 生产形态：TheHerta4 的 drawindexed 偏移恒为 part 局部 0，
            # firstIndex 必须来自 match_first_index（回归：曾被 drawindexed 偏移覆盖成全 0）
            "drawindexed = 12000, 0, 0",
        ]),
        (f"[TextureOverride_{hash_value}_{base_name}Skip]", [
            f"hash = {hash_value}",
            "match_first_index = 0",
            "ib = null",
        ]),
        (f"[Resource_{hash_value}_Position]", [
            "type = Buffer",
            "stride = 40",
            f"filename = Meshes/{base_name}-Position.buf",
        ]),
        (f"[Resource{base_name}AIB]", ["type = Buffer", "format = DXGI_FORMAT_R32_UINT"]),
        (f"[Resource{base_name}BIB]", ["type = Buffer", "format = DXGI_FORMAT_R32_UINT"]),
        ("[Present]", []),
    ])


def _cross_ib_sections():
    return OrderedDict([
        ("[Constants]", []),
        ("[TextureOverride_sourcehash_sourcehash_VertexLimitRaise]", [
            "hash = sourcehash",
            "override_vertex_count = 1000",
        ]),
        ("[TextureOverride_targethash_targethash_VertexLimitRaise]", [
            "hash = targethash",
            "override_vertex_count = 2000",
        ]),
        ("[TextureOverride_targethash_0]", [
            "hash = targethash",
            "match_first_index = 0",
            "ib = Resource_targethash_0_Index",
            "; [mesh:LOD0.targethash-300-0.Target_copy] [vertex_count:200]",
            "drawindexed = 300, 0, 0",
            "ib = Resource_sourcehash_0_Index",
            "vb0 = ResourceBodyVB_sourcehash_0",
            "; [mesh:LOD0.sourcehash-120-0.SourceA_copy] [vertex_count:80]",
            "drawindexed = 90, 0, 0",
            "; [mesh:LOD0.sourcehash-120-0.SourceB_copy] [vertex_count:40]",
            "drawindexed = 30, 90, 0",
        ]),
        ("[Resource_sourcehash_Position]", [
            "type = Buffer", "stride = 40", "filename = Meshes/sourcehash-Position.buf",
        ]),
        ("[Resource_targethash_Position]", [
            "type = Buffer", "stride = 40", "filename = Meshes/targethash-Position.buf",
        ]),
        ("[Resource_sourcehash_0_Index]", ["type = Buffer", "format = DXGI_FORMAT_R32_UINT"]),
        ("[Resource_targethash_0_Index]", ["type = Buffer", "format = DXGI_FORMAT_R32_UINT"]),
        ("[Present]", []),
    ])


class DragNodeLocateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()

    def test_parse_hash_values_extracts_ib(self):
        node = _make_node(self.mod)
        self.assertEqual(node._parse_hash_values("abc123, def456"), ["abc123", "def456"])

    def test_collect_draw_parts_skips_ib_null(self):
        node = _make_node(self.mod)
        sections = _base_sections()
        parts = node._collect_draw_parts(sections, "abc123")
        # 跳过 ib=null 段，收集 A/B 两个 part
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0]["index_count"], 52688)
        self.assertEqual(parts[1]["first_index"], 52688)
        self.assertEqual(parts[0]["ib_resource"], "Resourceabc123-43191AIB")

    def test_collect_draw_parts_combines_multiple_draws_in_one_ib_section(self):
        node = _make_node(self.mod)
        sections = _base_sections()
        section_name = "[TextureOverride_abc123_abc123-43191A]"
        sections[section_name][-1:] = [
            "drawindexed = 53994, 0, 0",
            "drawindexed = 3618, 53994, 0",
        ]

        part = node._collect_draw_parts(sections, "abc123")[0]

        self.assertEqual(part["ib_first_index"], 0)
        self.assertEqual(part["index_count"], 57612)

    def test_collect_draw_parts_uses_range_envelope_for_reused_and_unordered_draws(self):
        node = _make_node(self.mod)
        sections = _base_sections()
        section_name = "[TextureOverride_abc123_abc123-43191A]"
        sections[section_name][-1:] = [
            "DrawIndexed = 120, 300, 0",
            "drawindexed = 300, 0, 0",
            "DRAWINDEXED = 120, 300, 0",
        ]

        part = node._collect_draw_parts(sections, "abc123")[0]

        self.assertEqual(part["ib_first_index"], 0)
        self.assertEqual(part["index_count"], 420)

    def test_cross_ib_draws_are_owned_by_mesh_prefix_instead_of_section_hash(self):
        node = _make_node(self.mod)
        sections = _cross_ib_sections()

        source_parts = node._collect_draw_parts(sections, "sourcehash")
        target_parts = node._collect_draw_parts(sections, "targethash")

        self.assertEqual(len(source_parts), 1)
        self.assertEqual(source_parts[0]["ib_resource"], "Resource_sourcehash_0_Index")
        self.assertEqual(source_parts[0]["index_count"], 120)
        self.assertEqual(source_parts[0]["first_index"], 0)
        self.assertEqual(len(target_parts), 1)
        self.assertEqual(target_parts[0]["ib_resource"], "Resource_targethash_0_Index")
        self.assertEqual(target_parts[0]["index_count"], 300)

    def test_cross_ib_shader_replacement_draws_follow_mesh_prefix_and_run_section(self):
        node = _make_node(self.mod)
        sections = _cross_ib_sections()
        lines = sections["[TextureOverride_targethash_0]"]
        first_source_draw = lines.index("drawindexed = 90, 0, 0")
        second_source_draw = lines.index("drawindexed = 30, 90, 0")
        lines[first_source_draw] = "run = CustomShader_SourceA"
        lines[second_source_draw] = "run = CustomShader_SourceB"
        sections["[CustomShader_SourceA]"] = ["handling = skip", "drawindexed = 90, 0, 0"]
        sections["[CustomShader_SourceB]"] = ["handling = skip", "drawindexed = 30, 90, 0"]

        source_parts = node._collect_draw_parts(sections, "sourcehash")

        self.assertEqual(len(source_parts), 1)
        self.assertEqual(source_parts[0]["ib_resource"], "Resource_sourcehash_0_Index")
        self.assertEqual(source_parts[0]["index_count"], 120)
        self.assertEqual(
            source_parts[0]["hook_anchor_comment"],
            "; [mesh:LOD0.sourcehash-120-0.SourceA_copy] [vertex_count:80]",
        )

    def test_get_vertex_count_from_vlr(self):
        node = _make_node(self.mod)
        sections = _base_sections()
        self.assertEqual(node._get_vertex_count(sections, "abc123"), 14078)

    def test_read_position_buf_uses_custom_stride_and_position_offset(self):
        import tempfile

        node = _make_node(self.mod)
        node._get_vertex_struct_definition = lambda: (
            "struct VertexAttributes {\n"
            "    float padding;\n"
            "    float3 position;\n"
            "    float2 extra;\n"
            "};"
        )
        sections = OrderedDict({
            "[ResourceCustomPosition]": [
                "type = Buffer",
                "stride = 24",
                "filename = Meshes/custom-Position.buf",
            ],
        })
        comp = {"base_resource": "ResourceCustomPosition"}
        records = np.asarray([
            [99.0, 1.0, 2.0, 3.0, 10.0, 11.0],
            [98.0, 4.0, 5.0, 6.0, 12.0, 13.0],
        ], dtype=np.float32)

        with tempfile.TemporaryDirectory() as temp_dir:
            buffer_path = Path(temp_dir) / "Meshes" / "custom-Position.buf"
            buffer_path.parent.mkdir()
            records.tofile(buffer_path)
            positions = node._read_position_buf(temp_dir, sections, comp, vertex_count=2)

        np.testing.assert_array_equal(
            positions,
            np.asarray([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]], dtype=np.float32),
        )

    def test_read_removes_old_drag_tail_but_preserves_other_postprocess_modules(self):
        import tempfile

        node = _make_node(self.mod)
        content = "\n".join([
            "[Constants]",
            "global $active = 0",
            "[TextureOverride_Test]",
            "hash = abc123",
            "ib = ResourceTestIB",
            "\t; --- DRAG HOOK BEGIN ---",
            "\trun = CustomShaderDragBakeOld",
            "\t; --- DRAG HOOK END ---",
            "drawindexed = 3, 0, 0",
            self.mod.DRAG_TAIL_MARKER,
            "[ResourceOldDrag]",
            "type = Buffer",
            "; --- AUTO-APPENDED HEALTH DETECTION MODULE ---",
            "[ResourceHealth]",
            "type = Buffer",
            self.mod.DRAG_TAIL_MARKER,
            "[ResourceOldDragDuplicate]",
            "type = Buffer",
        ])

        with tempfile.TemporaryDirectory() as td:
            ini_path = Path(td) / "test.ini"
            ini_path.write_text(content, encoding="utf-8")
            sections, preserved_tail, _preserved_driver = node._read_ini_to_ordered_dict(str(ini_path))

        self.assertIn("[Constants]", sections)
        self.assertIn("drawindexed = 3, 0, 0", sections["[TextureOverride_Test]"])
        self.assertFalse(any("DRAG HOOK" in line for line in sections["[TextureOverride_Test]"]))
        self.assertFalse(any("CustomShaderDragBakeOld" in line for line in sections["[TextureOverride_Test]"]))
        self.assertNotIn("[ResourceOldDrag]", sections)
        self.assertNotIn(self.mod.DRAG_TAIL_MARKER, preserved_tail)
        self.assertNotIn("[ResourceOldDragDuplicate]", preserved_tail)
        self.assertIn("; --- AUTO-APPENDED HEALTH DETECTION MODULE ---", preserved_tail)
        self.assertIn("[ResourceHealth]", preserved_tail)


    def test_read_and_write_preserve_anim_driver_top_block(self):
        import tempfile

        node = _make_node(self.mod)
        driver_block = (
            "; --- ANIMATION DRIVER SECTION ---\n"
            "[Constants]\n"
            "global $driver_state = 0\n"
            "[Present]\n"
            "post $driver_state = 0\n"
            "; --- END ANIMATION DRIVER SECTION ---\n"
        )
        body = (
            "[Constants]\n"
            "global $body_state = 0\n"
            "[TextureOverride_Test]\n"
            "hash = abc123\n"
            "ib = ResourceTestIB\n"
            "drawindexed = 3, 0, 0\n"
        )

        with tempfile.TemporaryDirectory() as td:
            ini_path = Path(td) / "test.ini"
            ini_path.write_text(driver_block + "\n" + body, encoding="utf-8")
            sections, preserved_tail, preserved_driver = node._read_ini_to_ordered_dict(str(ini_path))
            node._write_ordered_dict_to_ini(sections, str(ini_path), preserved_tail, preserved_driver)
            written = ini_path.read_text(encoding="utf-8")

        self.assertIn("; --- ANIMATION DRIVER SECTION ---", written)
        self.assertIn("; --- END ANIMATION DRIVER SECTION ---", written)
        self.assertIn("global $driver_state = 0", written)
        self.assertIn("global $body_state = 0", written)
        self.assertNotIn("global $driver_state", sections["[Constants]"])

class DragNodeObjectMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()

    def test_object_map_layout_1_plus_n(self):
        node = _make_node(self.mod)
        sections = _base_sections()
        comp = node._locate_components(sections, ["abc123"])[0]
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            node._write_object_map(td, sections, comp)
            buf = Path(td) / "Meshes" / "abc123-43191ObjectMap.buf"
            data = buf.read_bytes()
        # (1+N)×16B = (1+2)×16 = 48B
        self.assertEqual(len(data), 48)
        header = struct.unpack('<ffff', data[:16])
        self.assertEqual(header[0], 2.0)  # part 数
        rec1 = struct.unpack('<ffff', data[16:32])
        self.assertEqual(rec1, (0.0, 52688.0, 7.0, 0.0))   # firstIndex, indexCount, mode=7, objectID
        rec2 = struct.unpack('<ffff', data[32:48])
        # 分区局部语义（对齐原作 ObjectMap 契约）：firstIndex 恒 0（detect 着色器
        # indexBase = firstIndex + tri*3，各部件 IB 为独立分区文件），objectID 恒 0
        #（着色器 entry.w==0 时回退 objectID=firstIndex=0，统一匹配碰撞档案条目 0）
        self.assertEqual(rec2, (0.0, 12000.0, 7.0, 0.0))

class DragNodeEmitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()

    def _emit(self, **props):
        node = _make_node(self.mod, **props)
        sections = _base_sections()
        comps = node._locate_components(sections, ["abc123"])
        node._emit_sections(sections, comps, "testns")
        return node, sections, comps

    @staticmethod
    def _zone_item(zone_id, weight=1.0, enabled=True, grabbable=True):
        settings = types.SimpleNamespace(
            enabled=enabled,
            brush_strength=weight,
            brush_falloff_k=4.6,
            radius=0.0,
            strength=0.0,
            max_offset=0.0,
            falloff=0.0,
            damping=0.0,
            grabbable=grabbable,
        )
        empty = types.SimpleNamespace(
            name=f"zone_{zone_id}",
            ssmt_drag_zone=settings,
            matrix_world=np.eye(4),
        )
        return types.SimpleNamespace(zone_id=zone_id, zone_object=empty)

    def test_required_sections_present(self):
        _, sections, _ = self._emit()
        cn = "abc123_43191"
        required = [
            f"[CustomShaderDragDetect{cn}_testns]",
            f"[CustomShaderDragJiggle{cn}_testns]",
            f"[CustomShaderDragPinDetected_testns]",
            f"[CustomShaderDragUpdateScreenJiggle_testns]",
            f"[CustomShaderDragPinComponent{cn}_testns]",
            f"[CommandListDragPinDetected_testns]",
            f"[CommandListDragCursorUpdate_testns]",
            f"[ResourceDragJiggleTempVB0_{cn}_testns]",
            "[KeyDragInputManagerLMB_testns]",
            "[KeyDragInputManagerModifier_testns]",
        ]
        for sec in required:
            self.assertIn(sec, sections, f"缺少必需段 {sec}")

    def test_shapekey_drive_dir_resource_and_bindings(self):
        zone = self._zone_item(0)
        node, sections, comps = self._emit(
            enable_shapekey_drive=True,
            zone_objects=[zone],
        )
        node._emit_present_and_constants(sections, comps, "testns")

        self.assertEqual(
            sections["[ResourceDragShapeKeyDrive_testns]"],
            ["type = RWBuffer", "format = R32_FLOAT", "array = 5"],
        )
        # 方向缓冲 = 区域×档位×5槽（4方向 + 1无方向）+ 1（末位槽存上一帧按键状态）
        self.assertEqual(
            sections["[ResourceDragShapeKeyDir_testns]"],
            ["type = RWBuffer", "format = R32_FLOAT", "array = 6"],
        )
        self.assertEqual(
            sections["[ResourceDragShapeKeyClickCount_testns]"],
            ["type = RWBuffer", "format = R32_UINT", "array = 1"],
        )
        self.assertEqual(
            sections["[ResourceDragShapeKeyActiveDir_testns]"],
            ["type = RWBuffer", "format = R32_UINT", "array = 1"],
        )

        cs = sections["[CustomShaderDragShapeKeyDrive_testns]"]
        self.assertIn("cs-u0 = ResourceDragShapeKeyDrive_testns", cs)
        self.assertIn("cs-u1 = ResourceDragShapeKeyDir_testns", cs)
        self.assertIn("cs-u2 = ResourceDragShapeKeyClickCount_testns", cs)
        self.assertIn("cs-u3 = ResourceDragShapeKeyActiveDir_testns", cs)
        self.assertIn("post cs-u1 = null", cs)
        self.assertIn("z77 = $ssmtdrag_drag_enabled_testns", cs)
        self.assertIn("w77 = $ssmtdrag_lmb_down_testns", cs)
        self.assertIn("x78 = $ssmtdrag_x_down_testns", cs)

        pin = sections["[CommandListDragPinDetected_testns]"]
        self.assertIn("\tclear = ResourceDragShapeKeyDrive_testns 0.0", pin)
        self.assertIn("\tclear = ResourceDragShapeKeyDir_testns 0.0", pin)

        present = sections["[Present]"]
        gate_idx = next(
            i for i, line in enumerate(present) if "--- DRAG INTERACTION GATE BEGIN ---" in line
        )
        gate_block = present[gate_idx:gate_idx + 80]
        mode_idx = gate_block.index("if $ssmtdrag_drag_enabled_testns != 1")
        self.assertIn("\tclear = ResourceDragShapeKeyDrive_testns 0.0", gate_block[mode_idx:mode_idx + 4])
        self.assertIn("\tclear = ResourceDragShapeKeyDir_testns 0.0", gate_block[mode_idx:mode_idx + 4])

    def test_shapekey_drive_mouse_displacement_present_lines(self):
        zone = self._zone_item(0)
        node, sections, comps = self._emit(
            enable_shapekey_drive=True,
            zone_objects=[zone],
            shapekey_drive_move_sensitivity=0.02,
        )
        node._emit_present_and_constants(sections, comps, "testns")

        present = "\n".join(sections["[Present]"])
        self.assertIn("$ssmtdrag_shapekey_dy_testns = $cursorY - $ssmtdrag_shapekey_prev_y_testns", present)
        self.assertIn("$ssmtdrag_shapekey_dx_testns = $cursorX - $ssmtdrag_shapekey_prev_x_testns", present)

        constants = "\n".join(sections["[Constants]"])
        self.assertIn("global $ssmtdrag_shapekey_dy_testns = 0", constants)
        self.assertIn("global $ssmtdrag_shapekey_dx_testns = 0", constants)
        self.assertIn("global $ssmtdrag_shapekey_prev_y_testns = 0", constants)
        self.assertIn("global $ssmtdrag_shapekey_prev_x_testns = 0", constants)

        cs = sections["[CustomShaderDragShapeKeyDrive_testns]"]
        self.assertIn("x79 = $ssmtdrag_shapekey_dy_testns", cs)
        self.assertIn("y79 = $ssmtdrag_shapekey_dx_testns", cs)
        self.assertNotIn("w79", cs)
        self.assertNotIn("x77", cs)
        self.assertNotIn("y77", cs)
        self.assertIn("x80 = 0.02", cs)

    def test_shapekey_drive_zone_stage_counts_auto_derived_from_shapekey_nodes(self):
        zone = self._zone_item(0)
        node = _make_node(
            self.mod,
            enable_shapekey_drive=True,
            zone_objects=[zone],
        )
        # 同树形态键节点：A 区域0 档位 2、B 区域0 档位 1、C 方向形态键（不计入档位）、
        # D 未开启拖拽驱动（档位 3 不应计入）
        sk_a = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_ShapeKey",
            drag_drive_enabled=True,
            shapekey_variable_items=[
                types.SimpleNamespace(shape_key_name="A", drag_zone_id=0, drag_dir_id="-1", drag_click_stage=2),
            ],
        )
        sk_b = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_ShapeKey",
            drag_drive_enabled=True,
            shapekey_variable_items=[
                types.SimpleNamespace(shape_key_name="B", drag_zone_id=0, drag_dir_id="-1", drag_click_stage=1),
            ],
        )
        sk_dir = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_ShapeKey",
            drag_drive_enabled=True,
            shapekey_variable_items=[
                types.SimpleNamespace(shape_key_name="C", drag_zone_id=0, drag_dir_id="0", drag_click_stage=3),
            ],
        )
        sk_off = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_ShapeKey",
            drag_drive_enabled=False,
            shapekey_variable_items=[
                types.SimpleNamespace(shape_key_name="D", drag_zone_id=0, drag_dir_id="-1", drag_click_stage=3),
            ],
        )
        node.id_data = types.SimpleNamespace(nodes=[sk_a, sk_b, sk_dir, sk_off])
        # 区域 0 无方向档位最大 2；方向形态键档位 3 不计入
        self.assertEqual(node._drag_drive_zone_stage_counts(), {0: 2})
        total, bases, counts = node._drag_drive_buffer_layout()
        self.assertEqual(total, 6)   # 4 方向 + 2 无方向档位
        self.assertEqual(bases, [0])
        self.assertEqual(counts, [2])

        sections = _base_sections()
        comps = node._locate_components(sections, ["abc123"])
        node._emit_sections(sections, comps, "testns")
        node._emit_present_and_constants(sections, comps, "testns")

        self.assertEqual(
            sections["[ResourceDragShapeKeyDrive_testns]"],
            ["type = RWBuffer", "format = R32_FLOAT", "array = 6"],
        )
        self.assertEqual(
            sections["[ResourceDragShapeKeyDir_testns]"],
            ["type = RWBuffer", "format = R32_FLOAT", "array = 7"],
        )
        cs = sections["[CustomShaderDragShapeKeyDrive_testns]"]
        self.assertIn("cs-t68 = ResourceDragShapeKeyZoneStageCounts_testns", cs)
        self.assertNotIn("z79", cs)

    def test_shapekey_drive_shader_stage_cycle_and_no_direction_set(self):
        shader_path = os.path.join("Toolset", "drag_interaction", "rzm_shapekey_drive.hlsl")
        if not os.path.exists(shader_path):
            self.skipTest("shader missing")
        with open(shader_path, encoding="utf-8") as f:
            content = f.read()
        # 点击档位按区域独立（ZoneStageCounts）在 0..N 间循环：0=清空，第 N+1 次点击回到 0
        self.assertIn("uint zoneStageCount = max(1u, ZoneStageCounts[hoverZone]);", content)
        self.assertIn("uint newStage = oldStage >= zoneStageCount ? 0u : oldStage + 1u;", content)
        self.assertNotIn("(oldStage % stageCount) + 1u", content)
        # 无方向槽：命中该档位时置 1，非活动档位归 0（不是翻转）
        self.assertIn("if (activeStage == stage && zonePressed)", content)
        self.assertIn("ShapeKeyDrive[ndIdx] = 1.0;", content)
        self.assertIn("ShapeKeyDrive[ndIdx] = 0.0;", content)
        self.assertNotIn("cur > 0.5 ? 0.0 : 1.0", content)
        # 每区域独立段：4 方向槽 + N 无方向槽，运行基址前缀和
        self.assertIn("runningBase += 4u + zoneStageCount;", content)
        self.assertIn("Buffer<uint> ZoneStageCounts        : register(t68);", content)

    def test_shapekey_drive_shader_zones_are_independent(self):
        shader_path = os.path.join("Toolset", "drag_interaction", "rzm_shapekey_drive.hlsl")
        if not os.path.exists(shader_path):
            self.skipTest("shader missing")
        with open(shader_path, encoding="utf-8") as f:
            content = f.read()
        # 各区域独立段基址 + 独立档位数
        self.assertIn("uint zoneBase = runningBase;", content)
        self.assertIn("uint zoneStageCount = max(1u, ZoneStageCounts[zone]);", content)
        # 无方向槽置位必须同时满足“本区域命中按下”（zoneHit && pressed），
        # 防止在其他区域按下时误置本区域槽
        self.assertIn("bool zoneHit = hasHit && zone == hoverZone;", content)
        self.assertIn("bool zonePressed = zoneHit && pressed;", content)
        self.assertIn("if (activeStage == stage && zonePressed)", content)
        # 方向槽位移积分同样只在 zoneHit 时执行，其余区域保持不积分
        self.assertIn("if (zoneHit)", content)

    def test_mode_toggle_key_generates_cycle_section(self):
        _, sections, _ = self._emit()
        key_lines = sections["[KeyDragInputManagerModeToggle_testns]"]
        self.assertEqual(key_lines, [
            "key = f8",
            "type = cycle",
            "$ssmtdrag_drag_enabled_testns = 0,1,2",
        ])

    def test_mode_toggle_key_custom_mode_variable(self):
        _, sections, _ = self._emit(drag_mode_variable_name="$custom_drag_mode")
        key_lines = sections["[KeyDragInputManagerModeToggle_testns]"]
        self.assertIn("$custom_drag_mode = 0,1,2", key_lines)

    def test_mode_toggle_key_update_and_always_generate(self):
        node = _make_node(self.mod, mode_toggle_key="f9")
        sections = _base_sections()
        comps = node._locate_components(sections, ["abc123"])
        node._emit_sections(sections, comps, "testns")
        self.assertEqual(sections["[KeyDragInputManagerModeToggle_testns]"][0], "key = f9")

        # 再次导出保持幂等（不重复、不追加）
        node._emit_sections(sections, comps, "testns")
        self.assertEqual(len(sections["[KeyDragInputManagerModeToggle_testns]"]), 3)

        # 清空时也始终生成（键名直接用字段内容）
        node2 = _make_node(self.mod, mode_toggle_key="")
        node2._emit_sections(sections, comps, "testns")
        key_lines = sections["[KeyDragInputManagerModeToggle_testns]"]
        self.assertEqual(key_lines, [
            "key = ",
            "type = cycle",
            "$ssmtdrag_drag_enabled_testns = 0,1,2",
        ])

    def test_tempvb0_empty_declaration(self):
        _, sections, _ = self._emit()
        lines = sections["[ResourceDragJiggleTempVB0_abc123_43191_testns]"]
        self.assertEqual(lines, ["type = RWBuffer"])  # 空声明段：无 format/array

    def test_bake_offsets_part_local(self):
        _, sections, _ = self._emit()
        # part A: index_count=52688 → step = 52688//8 = 6586
        s1 = sections["[CustomShaderDragBakeSample1_abc123_43191P0_testns]"]
        self.assertIn("y26 = 6586", s1)
        self.assertIn("drawindexed = 1, 6586, 0", s1)
        self.assertIn("gs-t1 = Resourceabc123-43191AIB", s1)
        # part B 用自己的 IB 资源名
        s1b = sections["[CustomShaderDragBakeSample1_abc123_43191P1_testns]"]
        self.assertIn("gs-t1 = Resourceabc123-43191BIB", s1b)

    def test_bake_offsets_cover_multiple_draws_in_one_ib_section(self):
        node = _make_node(self.mod)
        sections = _base_sections()
        section_name = "[TextureOverride_abc123_abc123-43191A]"
        sections[section_name][-1:] = [
            "drawindexed = 53994, 0, 0",
            "drawindexed = 3618, 53994, 0",
        ]
        comps = node._locate_components(sections, ["abc123"])

        node._emit_sections(sections, comps, "testns")

        sample = sections["[CustomShaderDragBakeSample1_abc123_43191P0_testns]"]
        self.assertIn("y26 = 7201", sample)
        self.assertIn("drawindexed = 1, 7201, 0", sample)

    def test_sparse_zone_resources_replace_fixed_register_block(self):
        _, sections, _ = self._emit()
        jig = "\n".join(sections["[CustomShaderDragJiggleabc123_43191_testns]"])
        self.assertIn("cs-t65 = ResourceDragJiggleZoneIDs", jig)
        self.assertIn("cs-t66 = ResourceDragJiggleZoneWeights", jig)
        self.assertIn("cs-t75 = ResourceDragZoneParams", jig)
        self.assertIn("x119 = 0", jig)
        self.assertNotIn("x77 = ", jig)
        self.assertNotIn("x99 = ", jig)

    def test_x72_asymmetry_mirrors_original(self):
        # 显式传 mult_radius 以与默认值解耦：Jiggle y72 来自 mult_radius 属性，
        # UpdateScreenJiggle y72 恒为硬编码 1.0（不对称，照原作）
        _, sections, _ = self._emit(mult_radius=0.333)
        jig = "\n".join(sections["[CustomShaderDragJiggleabc123_43191_testns]"])
        usj = "\n".join(sections["[CustomShaderDragUpdateScreenJiggle_testns]"])
        self.assertIn("y72 = 0.333", jig)   # Jiggle y72 = mult_radius
        self.assertIn("y72 = 1.0", usj)      # UpdateScreenJiggle y72 = 1.0（不对称，照原作）

    def test_ini_side_trims(self):
        _, sections, _ = self._emit()
        jig = "\n".join(sections["[CustomShaderDragJiggleabc123_43191_testns]"])
        det = "\n".join(sections["[CustomShaderDragDetectabc123_43191_testns]"])
        usj = "\n".join(sections["[CustomShaderDragUpdateScreenJiggle_testns]"])
        # 无 dump 行、无 cs-u4/u7 绑定
        for text in (jig, det, usj):
            self.assertNotIn("dump =", text)
            self.assertNotIn("cs-u4", text)
            self.assertNotIn("cs-u7", text)
        # 有 cs-u2/u6/t73/t74
        self.assertIn("cs-u6 = ResourceDragJiggleState", jig)
        self.assertIn("cs-t73 = ResourceDragPathVectors", jig)
        self.assertIn("cs-t74 = ResourceDragPathProgressState", jig)
        self.assertIn("cs-u2 = ResourceDragDebugDetect", det)
        # w84=0、x74=0、x75=0
        self.assertIn("w84 = 0", usj)
        self.assertIn("x74 = 0", jig)
        self.assertIn("x75 = 0", jig)
        # VIEWPORT_VALID 必须为 1（回归：x86=0 会让检测主循环被 ValidViewportCursor 永远拒绝）
        self.assertIn("x86 = 1", det)
        self.assertNotIn("x86 = 0", det)

    def test_sparse_mask_resource_sections(self):
        _, sections, _ = self._emit()
        cn = "abc123_43191"
        ids = sections[f"[ResourceDragJiggleZoneIDs_{cn}_testns]"]
        weights = sections[f"[ResourceDragJiggleZoneWeights_{cn}_testns]"]
        self.assertIn("format = R32G32B32A32_UINT", ids)
        self.assertIn("format = R32G32B32A32_FLOAT", weights)
        self.assertIn("[ResourceDragZoneParams_testns]", sections)
        self.assertFalse(any("JiggleMasks" in name for name in sections))

    def test_stable_zone_ids_migrate_and_preserve_high_ids(self):
        self.assertEqual(self.mod.MAX_ZONES, 256)
        explicit = self._zone_item(200)
        legacy = self._zone_item(-1)
        disabled = self._zone_item(17, enabled=False)
        node = _make_node(self.mod, zone_objects=[explicit, legacy, disabled])
        entries = node._collect_enabled_zone_entries()
        self.assertEqual([zone_id for zone_id, _ in entries], [0, 200])
        self.assertEqual(legacy.zone_id, 0)
        self.assertEqual(disabled.zone_id, 17)

        highest = self._zone_item(255)
        node = _make_node(self.mod, zone_objects=[highest])
        sections = _base_sections()
        comps = node._locate_components(sections, ["abc123"])
        node._emit_sections(sections, comps, "testns")
        self.assertIn("array = 256", sections["[ResourceDragPathProgressState_testns]"])
        jig = "\n".join(sections["[CustomShaderDragJiggleabc123_43191_testns]"])
        self.assertIn("y119 = 256", jig)

        all_items = [self._zone_item(zone_id) for zone_id in range(256)]
        node = _make_node(self.mod, zone_objects=all_items)
        all_entries = node._collect_enabled_zone_entries()
        self.assertEqual(len(all_entries), 256)
        self.assertEqual([zone_id for zone_id, _ in all_entries], list(range(256)))

    def test_zone_page_is_clamped_after_out_of_range_input_and_removal(self):
        node = _make_node(self.mod, zone_objects=[object() for _ in range(17)])
        object.__setattr__(node, "zone_page", 15)
        self.assertEqual(self.mod._zone_page_state(node), (1, 2))
        self.assertEqual(node.zone_page, 15)  # draw 路径只读，不在绘制中改 RNA
        self.assertEqual(self.mod._clamp_zone_page(node), (1, 2))
        self.assertEqual(node.zone_page, 1)

        node.zone_objects.pop()
        self.assertEqual(self.mod._clamp_zone_page(node), (0, 1))
        self.assertEqual(node.zone_page, 0)

        self.assertEqual(self.mod._clamp_zone_page(node, -10), (0, 1))

    def test_sparse_bake_keeps_four_strongest_zone_weights(self):
        import tempfile
        zone_ids = [0, 1, 2, 3, 4, 255]
        items = [self._zone_item(zone_id, weight=(index + 1) / 10.0) for index, zone_id in enumerate(zone_ids)]
        node = _make_node(self.mod, zone_objects=items, surface_propagate=False)
        node._check_zone_radius_scale = lambda zones: False
        node._read_position_buf = lambda *args: np.zeros((3, 3), dtype=np.float32)
        node._get_reference_matrix_inv = lambda comp: None
        node._get_non_mirror_mirror = lambda: None
        node._buffer_dir = lambda sections, comp: "Meshes"
        node._evaluate_zone_field = lambda positions, empty, *args, **kwargs: np.full(
            positions.shape[0], empty.ssmt_drag_zone.brush_strength, dtype=np.float32
        )
        comp = {"vertex_count": 3, "base_name": "sample", "comp_name": "sample", "parts": []}

        with tempfile.TemporaryDirectory() as td:
            legacy_dir = Path(td) / "Meshes"
            legacy_dir.mkdir(parents=True)
            for index in range(3):
                (legacy_dir / f"sampleJiggleMasks{index}.buf").write_bytes(b"legacy")
            node._write_jiggle_masks(td, {}, comp, "testns")
            ids = np.fromfile(Path(td) / "Meshes" / "sampleJiggleZoneIDs.buf", dtype=np.uint32).reshape(-1, 4)
            weights = np.fromfile(Path(td) / "Meshes" / "sampleJiggleZoneWeights.buf", dtype=np.float32).reshape(-1, 4)
            self.assertFalse(any((legacy_dir / f"sampleJiggleMasks{index}.buf").exists() for index in range(3)))

        for row_ids, row_weights in zip(ids, weights):
            kept = {int(zone_id): float(weight) for zone_id, weight in zip(row_ids, row_weights)}
            self.assertEqual(set(kept), {2, 3, 4, 255})
            self.assertAlmostEqual(kept[255], 0.6, places=6)

    def test_zone_field_applies_export_space_matrix_to_empty(self):
        node = _make_node(self.mod, surface_propagate=False)
        settings = types.SimpleNamespace(brush_strength=1.0, brush_falloff_k=4.6)
        empty_matrix = np.eye(4)
        empty_matrix[:3, 3] = [0.069, -0.139, 1.143]
        empty = types.SimpleNamespace(name="SSMT_DragZone_0", matrix_world=empty_matrix)
        positions = np.asarray([[0.069, 1.143, 0.139]], dtype=np.float32)
        export_matrix = np.asarray([
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, -1.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ])

        field = node._evaluate_zone_field(
            positions,
            empty,
            settings,
            None,
            None,
            export_matrix=export_matrix,
        )

        np.testing.assert_allclose(field, [1.0])

    def test_sparse_bake_rejects_all_zero_configured_zones(self):
        import tempfile
        item = self._zone_item(0)
        node = _make_node(self.mod, zone_objects=[item], surface_propagate=False)
        node._check_zone_radius_scale = lambda zones: False
        node._read_position_buf = lambda *args: np.zeros((3, 3), dtype=np.float32)
        node._get_reference_matrix_inv = lambda comp: None
        node._get_export_space_matrix = lambda: np.eye(4)
        node._get_non_mirror_mirror = lambda: None
        node._buffer_dir = lambda sections, comp: "Meshes"
        node._evaluate_zone_field = lambda *args, **kwargs: None
        comp = {"vertex_count": 3, "base_name": "sample", "comp_name": "sample", "parts": []}

        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(RuntimeError, "all configured drag zones produced zero weights"):
                node._write_jiggle_masks(td, {}, comp, "testns")
            self.assertFalse((Path(td) / "Meshes" / "sampleJiggleZoneWeights.buf").exists())

    def test_zone_params_buffer_uses_stable_id_and_grabbable_flag(self):
        import tempfile
        item = self._zone_item(255, grabbable=False)
        settings = item.zone_object.ssmt_drag_zone
        settings.radius = 0.25
        settings.strength = 1.5
        settings.max_offset = 0.75
        settings.falloff = 2.0
        settings.damping = 0.8
        node = _make_node(self.mod, zone_objects=[item])
        with tempfile.TemporaryDirectory() as td:
            node._write_zone_resources(td, "testns")
            params = np.fromfile(Path(td) / "res" / "drag_interaction" / "ZoneParams_testns.buf", dtype=np.float32).reshape(-1, 4)
        self.assertEqual(params.shape, (512, 4))
        np.testing.assert_allclose(params[510], [0.25, 1.5, 0.75, 2.0])
        np.testing.assert_allclose(params[511], [0.8, 0.0, 0.0, 1.0])

    def test_sparse_zone_shaders_have_no_legacy_twelve_zone_clamp(self):
        shader_dir = REPO_ROOT / "Toolset" / "drag_interaction"
        detect = (shader_dir / "rzm_object_detect.hlsl").read_text(encoding="utf-8")
        jiggle = (shader_dir / "rzm_jiggle_interaction.hlsl").read_text(encoding="utf-8")
        screen = (shader_dir / "rzm_jiggle_screen_state.hlsl").read_text(encoding="utf-8")
        self.assertIn("Buffer<uint4>                      gJiggleZoneIDs", detect)
        self.assertIn("uint candidates[12]", detect)
        self.assertIn("Buffer<float4> ZoneParams", jiggle)
        self.assertIn("Buffer<float4> ZoneParams", screen)
        for source in (detect, jiggle, screen):
            self.assertNotIn("0.0, 11.0", source)
            self.assertNotIn("ZONE_STRENGTH_R2", source)

    def test_grab_gesture_modes(self):
        """三种抓取手势模式的 Present 判定行（回归：原作 COMBO 语义 左右键同按/X）。"""
        for gesture, expect in (
            ('LMB', "$ssmtdrag_lmb_down_testns == 1 || $ssmtdrag_x_down_testns == 1"),
            ('RMB', "$ssmtdrag_rmb_down_testns == 1 || $ssmtdrag_x_down_testns == 1"),
            ('COMBO', "($ssmtdrag_lmb_down_testns == 1 && $ssmtdrag_rmb_down_testns == 1) || $ssmtdrag_x_down_testns == 1"),
        ):
            node = _make_node(self.mod, grab_gesture=gesture)
            sections = _base_sections()
            comps = node._locate_components(sections, ["abc123"])
            node._emit_present_and_constants(sections, comps, "testns")
            present_text = "\n".join(sections["[Present]"])
            self.assertIn(expect, present_text, f"手势模式 {gesture} 的判定行缺失")

    def test_default_rmb_poke_pushes_into_mesh(self):
        node = _make_node(self.mod, grab_gesture='LMB', poke_gesture='RMB')
        sections = _base_sections()
        comps = node._locate_components(sections, ["abc123"])
        node._emit_present_and_constants(sections, comps, "testns")
        present = "\n".join(sections["[Present]"])

        self.assertIn(
            "if $ssmtdrag_rmb_prev_testns == 1 && $ssmtdrag_rmb_down_testns == 0\n"
            "\t\t$ssmtdrag_poke_sign_testns = -1",
            present,
        )
        self.assertNotIn(
            "if $ssmtdrag_lmb_prev_testns == 1 && $ssmtdrag_lmb_down_testns == 0\n"
            "\t\t$ssmtdrag_poke_sign_testns = 1",
            present,
        )

    def test_poke_gesture_can_select_lmb_or_both_buttons_and_always_pushes_inward(self):
        for gesture, expect_lmb, expect_rmb in (
            ('LMB', True, False),
            ('BOTH', True, True),
        ):
            node = _make_node(self.mod, grab_gesture='COMBO', poke_gesture=gesture)
            sections = _base_sections()
            comps = node._locate_components(sections, ["abc123"])
            node._emit_present_and_constants(sections, comps, "testns")
            present = "\n".join(sections["[Present]"])
            lmb_release = (
                "if $ssmtdrag_lmb_prev_testns == 1 && $ssmtdrag_lmb_down_testns == 0\n"
                "\t\t$ssmtdrag_poke_sign_testns = -1"
            )
            rmb_release = (
                "if $ssmtdrag_rmb_prev_testns == 1 && $ssmtdrag_rmb_down_testns == 0\n"
                "\t\t$ssmtdrag_poke_sign_testns = -1"
            )
            self.assertEqual(lmb_release in present, expect_lmb, gesture)
            self.assertEqual(rmb_release in present, expect_rmb, gesture)

    def test_bake_rt_matches_original(self):
        """BakeRT 必须与原作 ResourceLLBakeRT 一致（bind_flags/mode/mips/format 全齐）。"""
        _, sections, _ = self._emit()
        lines = sections["[ResourceDragBakeRT_testns]"]
        self.assertIn("bind_flags = render_target shader_resource", lines)
        self.assertIn("format = DXGI_FORMAT_R32G32B32A32_FLOAT", lines)
        self.assertIn("mode = mono", lines)
        self.assertIn("mips = 1", lines)
        self.assertIn("width = 8", lines)
        self.assertIn("height = 2", lines)
        # 非法关键字不得出现
        for line in lines:
            self.assertFalse(line.strip().startswith("uav"), line)
            self.assertFalse(line.strip().startswith("render_target ="), line)

    def test_boot_clear_present_and_idempotent(self):
        node, sections, comps = self._emit()
        pin = "\n".join(sections["[CommandListDragPinDetected_testns]"])
        self.assertIn("if $ssmtdrag_booted_testns == 0", pin)
        self.assertIn("clear = ResourceDragDetectID_testns 0.0", pin)
        # 二次 emit 幂等（setdefault + 段存在跳过）
        before = {k: list(v) for k, v in sections.items()}
        node._emit_sections(sections, comps, "testns")
        for k, v in before.items():
            self.assertEqual(sections[k], v, f"段 {k} 二次 emit 后变化")

        self.assertIn(self.mod.DRAG_TAIL_MARKER, sections)
        self.assertEqual(sum(key == self.mod.DRAG_TAIL_MARKER for key in sections), 1)

    def test_ui_drag_bridge_exports_detected_and_zone_globals(self):
        """模型区域拖动 UI 依赖稳定的 GPU -> INI 全局变量桥接。"""
        node, sections, comps = self._emit()
        node._emit_present_and_constants(sections, comps, "testns")

        constants = "\n".join(sections["[Constants]"])
        present = "\n".join(sections["[Present]"])
        readback = "\n".join(sections.get("[CommandListDragUIReadback_testns]", []))
        self.assertIn("global $ssmtdrag_ui_detected_testns = -1", constants)
        self.assertIn("global $ssmtdrag_ui_zone_testns = -1", constants)
        self.assertIn(
            "store = $ssmtdrag_ui_detected_testns, ref ResourceDragPinnedDetectID_testns, 0",
            readback,
        )
        self.assertIn(
            "store = $ssmtdrag_ui_zone_testns, ref ResourceDragPinnedDetectInfo_testns, 31",
            readback,
        )
        self.assertIn("if $ssmtdrag_ui_detected_testns >= 0", readback)
        self.assertIn("$ssmtdrag_ui_detected_testns = -1", readback)
        self.assertIn("$ssmtdrag_ui_zone_testns = -1", readback)
        self.assertIn("type = StructuredBuffer", sections["[ResourceDragPinnedDetectID_testns]"])
        self.assertIn("stride = 4", sections["[ResourceDragPinnedDetectID_testns]"])
        self.assertIn("type = StructuredBuffer", sections["[ResourceDragPinnedDetectInfo_testns]"])
        self.assertIn("stride = 16", sections["[ResourceDragPinnedDetectInfo_testns]"])

        # 旧版 Present 已存在时仍可幂等补齐桥接，而不是被提前 return 跳过。
        legacy_sections = _base_sections()
        legacy_sections["[Present]"] = [
            "; --- DRAG PRESENT BEGIN ---",
            "post $ssmtdrag_drawn_testns = 0",
            "; --- DRAG PRESENT END ---",
        ]
        node._emit_present_and_constants(legacy_sections, comps, "testns")
        readback = "\n".join(legacy_sections.get("[CommandListDragUIReadback_testns]", []))
        self.assertEqual(readback.count("DRAG UI BRIDGE BEGIN"), 1)
        node._emit_present_and_constants(legacy_sections, comps, "testns")
        self.assertEqual(readback.count("DRAG UI BRIDGE BEGIN"), 1)

    def test_drag_runtime_mode_one_keeps_detection_but_disables_deformation(self):
        node, sections, comps = self._emit(drag_system_mode_default=1, enable_hand_cursor=True)
        node._emit_present_and_constants(sections, comps, "testns")

        constants = "\n".join(sections["[Constants]"])
        present = "\n".join(sections["[Present]"])
        pin = "\n".join(sections["[CommandListDragPinDetected_testns]"])
        hook = "\n".join(node._build_hook_block(comps[0], 0, "testns"))
        self.assertIn("global persist $ssmtdrag_drag_enabled_testns = 1", constants)
        self.assertIn("if $ssmtdrag_drag_enabled_testns < 2", present)
        self.assertIn("$isMouseButtonDown = 0", present)
        self.assertIn("$ssmtdrag_poke_sign_testns = 0", present)
        self.assertIn("$ssmtdrag_rmb_lone_hold_testns = 0", present)
        self.assertIn("$ssmtdrag_drag_enabled_testns >= 1", pin)
        self.assertIn("run = CustomShaderDragPinDetected_testns", pin)
        self.assertIn("$ssmtdrag_drag_enabled_testns >= 1", hook)
        self.assertIn("$ssmtdrag_drag_enabled_testns >= 2", hook)

    def test_drag_runtime_mode_zero_and_custom_variables_disable_entire_system(self):
        node, sections, comps = self._emit(
            drag_system_mode_default=0,
            drag_mode_variable_name="$custom_drag_mode",
            ui_detected_variable_name="$custom_hit_id",
            ui_zone_variable_name="$custom_zone_id",
        )
        node._emit_present_and_constants(sections, comps, "testns")

        constants = "\n".join(sections["[Constants]"])
        present = "\n".join(sections["[Present]"])
        readback = "\n".join(sections.get("[CommandListDragUIReadback_testns]", []))
        pin = "\n".join(sections["[CommandListDragPinDetected_testns]"])
        hook = "\n".join(node._build_hook_block(comps[0], 0, "testns"))
        self.assertIn("global persist $custom_drag_mode = 0", constants)
        self.assertIn("global $custom_hit_id = -1", constants)
        self.assertIn("global $custom_zone_id = -1", constants)
        self.assertNotIn("global persist $ssmtdrag_drag_enabled_testns", constants)
        self.assertIn("if $custom_drag_mode < 1", present)
        self.assertIn("if $custom_drag_mode >= 1", pin)
        self.assertIn("if $custom_drag_mode >= 1", hook)
        self.assertIn("if $custom_drag_mode >= 2", hook)
        self.assertIn("store = $custom_hit_id, ref ResourceDragPinnedDetectID_testns, 0", readback)
        self.assertIn("store = $custom_zone_id, ref ResourceDragPinnedDetectInfo_testns, 31", readback)

    def test_drag_runtime_switch_defaults_on_and_upgrades_legacy_present(self):
        node, sections, comps = self._emit()
        legacy_present = [
            "; --- DRAG PRESENT BEGIN ---",
            "if $ssmtdrag_mode_testns == 1",
            "\tpre run = CommandListDragPinDetected_testns",
            "endif",
            "post $ssmtdrag_drawn_testns = 0",
            "; --- DRAG PRESENT END ---",
        ]
        sections["[Present]"] = legacy_present
        node._emit_present_and_constants(sections, comps, "testns")
        constants = "\n".join(sections["[Constants]"])
        present = "\n".join(sections["[Present]"])
        self.assertIn("global persist $ssmtdrag_drag_enabled_testns = 2", constants)
        self.assertEqual(present.count("DRAG INTERACTION GATE BEGIN"), 1)
        self.assertLess(present.index("DRAG INTERACTION GATE BEGIN"), present.index("pre run = CommandListDragPinDetected_testns"))
        node._emit_present_and_constants(sections, comps, "testns")
        self.assertEqual("\n".join(sections["[Present]"]).count("DRAG INTERACTION GATE BEGIN"), 1)

    def test_legacy_boolean_drag_enabled_migrates_to_three_level_mode(self):
        mod = self.mod
        legacy_off = _make_node(
            mod,
            drag_enabled_default=False,
            drag_system_mode_default=2,
            drag_mode_initialized=False,
        )
        self.assertEqual(legacy_off._default_drag_system_mode(), 1)
        self.assertEqual(legacy_off.drag_system_mode_default, 1)
        self.assertTrue(legacy_off.drag_mode_initialized)

        legacy_on = _make_node(
            mod,
            drag_enabled_default=True,
            drag_system_mode_default=0,
            drag_mode_initialized=False,
        )
        self.assertEqual(legacy_on._default_drag_system_mode(), 2)
        self.assertEqual(legacy_on.drag_system_mode_default, 2)

    def test_no_zone_fallback_zone0_remains_grabbable(self):
        import tempfile
        node = _make_node(self.mod)
        with tempfile.TemporaryDirectory() as td:
            node._write_zone_resources(td, "testns")
            params = np.fromfile(Path(td) / "res" / "drag_interaction" / "ZoneParams_testns.buf", dtype=np.float32).reshape(-1, 4)
        self.assertEqual(params.shape, (2, 4))
        self.assertEqual(params[1].tolist(), [0.0, 1.0, 0.0, 1.0])

    def test_all_used_globals_declared(self):
        """发射的所有段中被引用的 $ 变量必须在 [Constants] 有 global 声明。
        回归：$ssmtdrag_viewport_valid 曾未声明——3DMigoto 中变量跨 run= 命令列表
        边界传递必须声明 global，否则子列表置 1 后调用方仍读 0 → 视口恒无效 →
        cursor 恒 (-1,-1) → 检测永不命中且手型光标锚定屏幕外（导出模组无效果的根因）。
        本测试覆盖该 bug 的整个类别，防止任何变量漏声明。"""
        import re as _re
        builtin = {"$time", "$cursor_x", "$cursor_y", "$cursor_window_x", "$cursor_window_y",
                   "$cursor_screen_x", "$cursor_screen_y", "$window_width", "$window_height",
                   "$res_width", "$res_height", "$rt_width", "$rt_height", "$draw_type",
                   "$DRAW_TYPE", "$INDEX_COUNT", "$FIRST_INDEX", "$FIRST_VERTEX",
                   "$INSTANCE_COUNT", "$FIRST_INSTANCE"}
        # 同时覆盖 enable_hand_cursor=True（用户实际导出配置）以纳入手部 globals。
        # 注意 [Constants] 全局由 _emit_present_and_constants 写入，必须两步都调。
        node, sections, comps = self._emit(enable_hand_cursor=True)
        node._emit_present_and_constants(sections, comps, "testns")
        declared = set()
        for l in sections.get("[Constants]", []):
            m = _re.match(r"\s*global(?:\s+persist)?\s+(\$\w+)", l)
            if m:
                declared.add(m.group(1))
        undeclared = {}
        for name, lines in sections.items():
            locals_ = set()
            for l in lines:
                m = _re.match(r"\s*local\s+(\$\w+)", l)
                if m:
                    locals_.add(m.group(1))
            for l in lines:
                s = l.strip()
                if not s or s.startswith(";"):
                    continue
                for v in _re.findall(r"\$\w+", s):
                    if v in locals_ or v in builtin:
                        continue
                    if v not in declared:
                        undeclared.setdefault(v, []).append(name)
        self.assertFalse(undeclared, f"未声明变量: {undeclared}")


class DragNodeInjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()

    def test_hook_insert_position_after_ib_before_run(self):
        node = _make_node(self.mod)
        sections = _base_sections()
        comp = node._locate_components(sections, ["abc123"])[0]
        node._inject_draw_hooks(sections, comp, "testns")
        lines = sections["[TextureOverride_abc123_abc123-43191A]"]
        ib_idx = next(i for i, l in enumerate(lines) if l.strip().startswith("ib ="))
        hook_idx = next(i for i, l in enumerate(lines) if "DRAG HOOK BEGIN" in l)
        run_idx = next(i for i, l in enumerate(lines) if l.strip().startswith("run ="))
        draw_idx = next(i for i, l in enumerate(lines) if l.strip().startswith("drawindexed ="))
        self.assertTrue(ib_idx < hook_idx < run_idx < draw_idx)

    def test_hook_skip_ib_null_and_idempotent(self):
        node = _make_node(self.mod)
        sections = _base_sections()
        comp = node._locate_components(sections, ["abc123"])[0]
        node._inject_draw_hooks(sections, comp, "testns")
        # ib=null 段未被收集为 part，故不含 hook
        skip_lines = sections["[TextureOverride_abc123_abc123-43191Skip]"]
        self.assertFalse(any("DRAG HOOK" in l for l in skip_lines))
        # 幂等
        node._inject_draw_hooks(sections, comp, "testns")
        a_lines = sections["[TextureOverride_abc123_abc123-43191A]"]
        self.assertEqual(sum(1 for l in a_lines if "DRAG HOOK BEGIN" in l), 1)

    def test_cross_ib_source_and_target_hooks_follow_their_mesh_prefixes(self):
        node = _make_node(self.mod)
        sections = _cross_ib_sections()
        components = node._locate_components(sections, ["sourcehash", "targethash"])

        node._emit_sections(sections, components, "testns")
        for comp in components:
            node._inject_draw_hooks(sections, comp, "testns")

        lines = sections["[TextureOverride_targethash_0]"]
        target_comment = lines.index("; [mesh:LOD0.targethash-300-0.Target_copy] [vertex_count:200]")
        source_comment = lines.index("; [mesh:LOD0.sourcehash-120-0.SourceA_copy] [vertex_count:80]")
        target_hook = next(i for i, line in enumerate(lines) if "DRAG HOOK BEGIN targethashP0_testns" in line)
        source_hook = next(i for i, line in enumerate(lines) if "DRAG HOOK BEGIN sourcehashP0_testns" in line)

        self.assertLess(target_hook, target_comment)
        self.assertLess(target_comment, source_hook)
        self.assertLess(source_hook, source_comment)
        self.assertEqual(sum("DRAG HOOK BEGIN" in line for line in lines), 2)

    def test_full_regeneration_cycle_keeps_one_tail_and_fresh_hooks(self):
        import tempfile

        node = _make_node(self.mod)
        initial_sections = _base_sections()
        section_name = "[TextureOverride_abc123_abc123-43191A]"
        initial_sections[section_name][-1:] = [
            "drawindexed = 53994, 0, 0",
            "drawindexed = 3618, 53994, 0",
        ]

        with tempfile.TemporaryDirectory() as td:
            ini_path = Path(td) / "test.ini"
            node._write_ordered_dict_to_ini(initial_sections, str(ini_path))

            for _generation in range(2):
                sections, preserved_tail, _preserved_driver = node._read_ini_to_ordered_dict(str(ini_path))
                comps = node._locate_components(sections, ["abc123"])
                node._emit_sections(sections, comps, "testns")
                for comp in comps:
                    node._inject_draw_hooks(sections, comp, "testns")
                node._emit_present_and_constants(sections, comps, "testns")
                node._write_ordered_dict_to_ini(sections, str(ini_path), preserved_tail)

                output = ini_path.read_text(encoding="utf-8")
                self.assertEqual(output.count(self.mod.DRAG_TAIL_MARKER), 1)
                self.assertEqual(output.count("DRAG HOOK BEGIN"), 2)
                self.assertEqual(output.count("DRAG HOOK END"), 2)
                self.assertEqual(output.count("[ResourceDragDetectID_testns]"), 1)
                self.assertIn("drawindexed = 1, 7201, 0", output)


class DragNodePreviewTests(unittest.TestCase):
    """权重预览模块：节点收集过滤 + 签名变化检测（不触 GPU 绘制路径）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()

    def _make_zone_empty(self, name="SSMT_DragZone_0", loc=(0.0, 0.0, 0.0), scale=1.0,
                         enabled=True, brush_strength=1.0, brush_falloff_k=4.6):
        m = np.eye(4)
        m[0, 3], m[1, 3], m[2, 3] = loc
        m[0, 0] = m[1, 1] = m[2, 2] = scale
        settings = types.SimpleNamespace(
            enabled=enabled, brush_strength=brush_strength, brush_falloff_k=brush_falloff_k)
        return types.SimpleNamespace(name=name, matrix_world=m, ssmt_drag_zone=settings)

    def _make_preview_node(self, mod, tree_name="Tree", node_name="Drag",
                           weights=True, target=None, collection=None, zone_empties=()):
        node = _make_node(mod)
        object.__setattr__(node, "preview_weights", weights)
        object.__setattr__(node, "preview_target", target)
        object.__setattr__(node, "preview_collection", collection)
        object.__setattr__(node, "id_data", types.SimpleNamespace(name=tree_name))
        object.__setattr__(node, "name", node_name)
        object.__setattr__(node, "zone_objects",
                           [types.SimpleNamespace(zone_object=e) for e in zone_empties])
        return node

    def test_collect_filters_enabled_nodes_with_target(self):
        mod = self.mod
        bpy = mod.bpy  # 模块加载时安装的 stub 实例（sys.modules["bpy"] 可能已被后续加载重建）
        target = types.SimpleNamespace(name="Mesh", matrix_world=np.eye(4),
                                       data=types.SimpleNamespace(vertices=range(100)))
        ok_node = self._make_preview_node(mod, target=target, zone_empties=[self._make_zone_empty()])
        off_node = self._make_preview_node(mod, tree_name="Tree2", node_name="DragOff",
                                           weights=False, target=target)
        no_target = self._make_preview_node(mod, tree_name="Tree3", node_name="DragNoT", weights=True)
        other = types.SimpleNamespace(bl_idname="SSMTNode_PostProcess_Other",
                                      preview_weights=True, preview_target=target)
        bpy.data.node_groups = [
            types.SimpleNamespace(nodes=[ok_node, other]),
            types.SimpleNamespace(nodes=[off_node, no_target]),
        ]
        found = mod._collect_preview_nodes()
        self.assertEqual([(n.id_data.name, n.name) for n in found],
                         [(tree_name, node_name) for tree_name, node_name in [("Tree", "Drag")]])
        self.assertIs(found[0], ok_node)

    def test_signature_sensitive_to_matrix_and_brush(self):
        mod = self.mod
        target = types.SimpleNamespace(name="Mesh", matrix_world=np.eye(4),
                                       data=types.SimpleNamespace(vertices=range(100)))
        empty = self._make_zone_empty()
        node = self._make_preview_node(mod, target=target, zone_empties=[empty])
        base = mod._preview_signature(node)
        # 移动空物体 → 签名变化
        moved = self._make_zone_empty(loc=(1.0, 0.0, 0.0))
        node2 = self._make_preview_node(mod, tree_name="Tree", node_name="Drag",
                                        target=target, zone_empties=[moved])
        self.assertNotEqual(base, mod._preview_signature(node2))
        # 改 brush_strength → 签名变化
        stronger = self._make_zone_empty(brush_strength=2.0)
        node3 = self._make_preview_node(mod, tree_name="Tree", node_name="Drag",
                                        target=target, zone_empties=[stronger])
        self.assertNotEqual(base, mod._preview_signature(node3))
        # 未变 → 签名稳定
        node4 = self._make_preview_node(mod, tree_name="Tree", node_name="Drag",
                                        target=target, zone_empties=[self._make_zone_empty()])
        self.assertEqual(base, mod._preview_signature(node4))

    def test_signature_sensitive_to_target_move(self):
        mod = self.mod
        empty = self._make_zone_empty()
        m = np.eye(4)
        m[2, 3] = 5.0
        node = self._make_preview_node(
            mod, target=types.SimpleNamespace(name="Mesh", matrix_world=m,
                                              data=types.SimpleNamespace(vertices=range(100))),
            zone_empties=[empty])
        base = mod._preview_signature(node)
        m2 = np.eye(4)
        m2[2, 3] = 6.0
        node2 = self._make_preview_node(
            mod, target=types.SimpleNamespace(name="Mesh", matrix_world=m2,
                                              data=types.SimpleNamespace(vertices=range(100))),
            zone_empties=[empty])
        self.assertNotEqual(base, mod._preview_signature(node2))

    def test_collection_preview_recursively_collects_meshes_and_takes_priority(self):
        mod = self.mod
        mesh_a = types.SimpleNamespace(type="MESH", name="A", name_full="A", matrix_world=np.eye(4),
                                       data=types.SimpleNamespace(vertices=range(3)))
        mesh_b = types.SimpleNamespace(type="MESH", name="B", name_full="B", matrix_world=np.eye(4),
                                       data=types.SimpleNamespace(vertices=range(5)))
        light = types.SimpleNamespace(type="LIGHT", name="Light", name_full="Light")
        collection = types.SimpleNamespace(name="PreviewSet", all_objects=[mesh_b, light, mesh_a, mesh_a])
        fallback = types.SimpleNamespace(type="MESH", name="Fallback", matrix_world=np.eye(4),
                                         data=types.SimpleNamespace(vertices=range(1)))
        node = self._make_preview_node(mod, target=fallback, collection=collection)
        self.assertEqual([obj.name for obj in mod._preview_targets(node)], ["A", "B"])
        signature = mod._preview_signature(node)
        self.assertIn("PreviewSet", signature)
        self.assertNotIn("Fallback", str(signature))

    def test_collection_membership_changes_preview_signature(self):
        mod = self.mod
        mesh_a = types.SimpleNamespace(type="MESH", name="A", name_full="A", matrix_world=np.eye(4),
                                       data=types.SimpleNamespace(vertices=range(3)))
        mesh_b = types.SimpleNamespace(type="MESH", name="B", name_full="B", matrix_world=np.eye(4),
                                       data=types.SimpleNamespace(vertices=range(5)))
        collection = types.SimpleNamespace(name="PreviewSet", all_objects=[mesh_a])
        node = self._make_preview_node(mod, collection=collection)
        before = mod._preview_signature(node)
        collection.all_objects.append(mesh_b)
        self.assertNotEqual(before, mod._preview_signature(node))

    def test_preview_tick_debounces_repeated_signature_changes(self):
        mod = self.mod
        node = self._make_preview_node(mod)
        signatures = iter(("initial", "change-1", "change-2", "change-2"))
        rebuilds = []
        clock = iter((0.0, 0.2, 0.4, 1.2))
        original = (
            mod._collect_preview_nodes,
            mod._ensure_preview_handler,
            mod._preview_signature,
            mod._rebuild_preview_batches,
            mod._preview_now,
        )
        mod._preview_sig_cache = None
        mod._preview_pending_signature = None
        mod._preview_pending_since = None
        try:
            mod._collect_preview_nodes = lambda: [node]
            mod._ensure_preview_handler = lambda: None
            mod._preview_signature = lambda _node: next(signatures)
            mod._rebuild_preview_batches = lambda nodes: rebuilds.append(list(nodes))
            mod._preview_now = lambda: next(clock)

            mod._preview_tick()  # 首次开启立即建立预览
            mod._preview_tick()  # 连续新增区域，只记录待处理签名
            mod._preview_tick()  # 签名继续变化，重新开始防抖计时
            mod._preview_tick()  # 稳定超过防抖窗口，只重建一次
        finally:
            (mod._collect_preview_nodes,
             mod._ensure_preview_handler,
             mod._preview_signature,
             mod._rebuild_preview_batches,
             mod._preview_now) = original
            mod._preview_sig_cache = None
            mod._preview_pending_signature = None
            mod._preview_pending_since = None

        self.assertEqual(len(rebuilds), 2)

    def test_collection_preview_follows_surface_propagate(self):
        mod = self.mod
        collection = types.SimpleNamespace(name="PreviewSet", all_objects=[])
        node = self._make_preview_node(mod, collection=collection)
        object.__setattr__(node, "surface_propagate", True)
        self.assertTrue(mod._preview_uses_surface_distance(node, enabled_zone_count=1))
        object.__setattr__(node, "surface_propagate", False)
        self.assertFalse(mod._preview_uses_surface_distance(node, enabled_zone_count=1))

    def test_surface_distance_independent_of_zone_count(self):
        mod = self.mod
        node = self._make_preview_node(mod)
        object.__setattr__(node, "surface_propagate", True)
        self.assertTrue(mod._preview_uses_surface_distance(node, enabled_zone_count=1))
        # 区域数量不影响是否测地（与烘焙一致）
        self.assertTrue(mod._preview_uses_surface_distance(node, enabled_zone_count=100))
        object.__setattr__(node, "surface_propagate", False)
        self.assertFalse(mod._preview_uses_surface_distance(node, enabled_zone_count=100))

    def test_preview_target_field_uses_scene_space(self):
        """预览基于 Blender 当前场景坐标（所见即所得），不做任何导出期变换：
        非镜像 X 镜像补偿、参考物体逆、导出空间矩阵都只属于导出烘焙。
        否则球会被翻到另一侧，沿表面扩散到非当前物体。"""
        mod = self.mod
        node = _make_node(mod)
        object.__setattr__(node, "surface_propagate", True)
        object.__setattr__(node, "mask_plateau", 0.0)
        # 球心 X+1（半径 1）在 A 上；B 在 X-0.5，距球心 1.5 > 1，场景中不相交
        empty = self._make_zone_empty(loc=(1.0, 0.0, 0.0), scale=1.0)
        zones = [(empty, empty.ssmt_drag_zone)]
        verts_a = np.array([[0.9, 0.0, 0.0], [1.0, 0.0, 0.0], [1.1, 0.0, 0.0]], dtype=np.float64)
        verts_b = np.array([[-0.5, 0.0, 0.0], [-0.6, 0.0, 0.0], [-0.5, 0.1, 0.0]], dtype=np.float64)
        tris = np.array([[0, 1, 2]], dtype=np.int64)
        field_a = mod._preview_target_field(node, verts_a, tris, zones, 0.0)
        field_b = mod._preview_target_field(node, verts_b, tris, zones, 0.0)
        self.assertGreater(float(field_a.max()), 0.3)      # A 有沿表面权重
        np.testing.assert_array_equal(field_b, np.zeros(3))  # B 不被接触/扩散

        # 对照：若错误地在预览中只对球施加 X 镜像（顶点保持场景坐标），
        # 球会翻到 X- 一侧错误接触 B —— 正是本 bug 的机制
        ball_mirrored = np.diag([-1.0, 1.0, 1.0, 1.0]) @ np.asarray(empty.matrix_world, dtype=np.float64)
        d_bug = node._zone_distances(verts_b, ball_mirrored, mod.gb_core.edges_from_triangles(tris))
        f_bug = node._shape_field(d_bug, 1.0, 4.6, 0.0)
        self.assertGreater(float(f_bug.max()), 0.0)

    def test_merged_mesh_welds_shared_seam_between_objects(self):
        """集合预览：共享接缝的两个网格必须被焊接为同一个连续表面。
        球命中 A 后应沿表面穿过接缝传播到 B；结果必须与手工焊成单个
        网格计算的场完全一致（即集合 = 一个物体）。"""
        mod = self.mod
        node = _make_node(mod)
        object.__setattr__(node, "surface_propagate", True)
        object.__setattr__(node, "mask_plateau", 0.0)
        # 球心 (0.15,0,0) 半径 0.25：A 沿 x（接缝在 x=0.2），B 从接缝沿 y 拐弯
        empty = self._make_zone_empty(loc=(0.15, 0.0, 0.0), scale=0.25)
        zones = [(empty, empty.ssmt_drag_zone)]
        verts_a = np.array([[0.1, 0.0, 0.0], [0.15, 0.0, 0.0], [0.2, 0.0, 0.0]], dtype=np.float64)
        tris_a = np.array([[0, 1, 2]], dtype=np.int64)
        verts_b = np.array([[0.2, 0.0, 0.0], [0.2, 0.1, 0.0], [0.2, 0.2, 0.0]], dtype=np.float64)
        tris_b = np.array([[0, 1, 2]], dtype=np.int64)

        fields = mod._preview_merged_mesh(
            node,
            [(verts_a, tris_a), (verts_b, tris_b)],
            zones,
            0.0,
        )
        self.assertEqual(len(fields), 2)

        # 手工焊接成单个网格：B[0] 与 A[2] 共享接缝顶点
        welded_verts = np.concatenate([verts_a, verts_b[1:]], axis=0)
        welded_tris = np.array([[0, 1, 2], [2, 3, 4]], dtype=np.int64)
        ref = mod._preview_target_field(node, welded_verts, welded_tris, zones, 0.0)

        np.testing.assert_allclose(fields[0], ref[:3], atol=1e-12)
        # B 的接缝顶点与 A 末端同节点 → 权重一致；B 其余顶点按接缝连续传播
        np.testing.assert_allclose(fields[1], np.concatenate([[ref[2]], ref[3:]]), atol=1e-12)

    def test_merged_mesh_keeps_disconnected_parts_zero(self):
        """集合预览：不共享接缝（互不相连）的网格不能被沿表面传播到，
        只有真正被球接触的物体有权重。"""
        mod = self.mod
        node = _make_node(mod)
        object.__setattr__(node, "surface_propagate", True)
        object.__setattr__(node, "mask_plateau", 0.0)
        empty = self._make_zone_empty(loc=(0.15, 0.0, 0.0), scale=0.25)
        zones = [(empty, empty.ssmt_drag_zone)]
        verts_a = np.array([[0.1, 0.0, 0.0], [0.15, 0.0, 0.0], [0.2, 0.0, 0.0]], dtype=np.float64)
        tris_a = np.array([[0, 1, 2]], dtype=np.int64)
        # B 平移 z=3，与 A 无共享顶点 → 不连通
        verts_b = np.array([[0.1, 0.0, 3.0], [0.15, 0.0, 3.0], [0.2, 0.0, 3.0]], dtype=np.float64)
        tris_b = np.array([[0, 1, 2]], dtype=np.int64)

        fields = mod._preview_merged_mesh(
            node,
            [(verts_a, tris_a), (verts_b, tris_b)],
            zones,
            0.0,
        )
        self.assertGreater(float(fields[0].max()), 0.0)
        np.testing.assert_array_equal(fields[1], np.zeros(3))


class DragNodeMirrorTests(unittest.TestCase):
    """非镜像工作流：区域空物体矩阵 X 镜像补偿（掩码左右颠倒回归）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()

    def _zone_empty(self, loc=(1.0, 0.0, 0.0)):
        m = np.eye(4)
        m[0, 3], m[1, 3], m[2, 3] = loc
        settings = types.SimpleNamespace(brush_strength=1.0, brush_falloff_k=4.6)
        return types.SimpleNamespace(name="SSMT_DragZone_0", matrix_world=m,
                                     ssmt_drag_zone=settings)

    def test_mirror_flips_ball_side(self):
        mod = self.mod
        node = _make_node(mod)
        settings = self._zone_empty().ssmt_drag_zone
        empty = self._zone_empty()
        # 顶点在 X±1.5，球在 X+1（半径 1，d<1 有效）→ 只有 X+1.5 命中
        positions = np.array([[1.5, 0.0, 0.0], [-1.5, 0.0, 0.0]], dtype=np.float32)
        field = node._evaluate_zone_field(positions, empty, settings, None, None)
        self.assertGreater(float(field[0]), 0.0)
        self.assertEqual(float(field[1]), 0.0)
        # 施加 X 镜像 → 球等效到 X-1 → 只有 X-1.5 命中
        mirror = np.diag([-1.0, 1.0, 1.0, 1.0])
        field_m = node._evaluate_zone_field(positions, empty, settings, None, mirror)
        self.assertEqual(float(field_m[0]), 0.0)
        self.assertGreater(float(field_m[1]), 0.0)

    def test_mirror_composes_with_ref_matrix_inv(self):
        mod = self.mod
        node = _make_node(mod)
        settings = self._zone_empty().ssmt_drag_zone
        empty = self._zone_empty()
        # 参考矩阵：X 平移 -0.5 → 球（镜像后 X-1）落到 X-1.5
        ref_inv = np.eye(4)
        ref_inv[0, 3] = -0.5
        mirror = np.diag([-1.0, 1.0, 1.0, 1.0])
        positions = np.array([[1.75, 0.0, 0.0], [-1.75, 0.0, 0.0]], dtype=np.float32)
        field = node._evaluate_zone_field(positions, empty, settings, ref_inv, mirror)
        self.assertEqual(float(field[0]), 0.0)
        self.assertGreater(float(field[1]), 0.0)

    def test_compensation_detection_by_scene_and_ref(self):
        mod = self.mod
        bpy = mod.bpy
        marker = "_ssmt_non_mirror_workflow_processed"
        node = _make_node(mod)
        # 场景网格带标记 → 补偿
        bpy.data.objects = [
            types.SimpleNamespace(type="MESH", get=lambda k, d=False, m=marker: k == m),
            types.SimpleNamespace(type="EMPTY", get=lambda k, d=False: False),
        ]
        mir = node._get_non_mirror_mirror()
        self.assertIsNotNone(mir)
        np.testing.assert_allclose(mir, np.diag([-1.0, 1.0, 1.0, 1.0]))
        # 无标记 → None
        bpy.data.objects = [types.SimpleNamespace(type="MESH", get=lambda k, d=False: False)]
        self.assertIsNone(node._get_non_mirror_mirror())
        # 参考物体（网格）带标记 → 优先命中
        ref = types.SimpleNamespace(type="MESH", get=lambda k, d=False, m=marker: k == m)
        object.__setattr__(node, "bake_reference_object", ref)
        self.assertIsNotNone(node._get_non_mirror_mirror())


class DragNodePlateauTests(unittest.TestCase):
    """权重平台化：高斯尖峰 → 平台形 mask（拖拽以鼠标命中点为锚，不被权重峰吸走）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()

    def _node(self):
        return _make_node(self.mod)

    def _unit_ball(self):
        m = np.eye(4)  # 单位球（缩放 1 = 半径 1），球心在原点
        return m

    def test_plateau_zero_degenerates_to_gaussian(self):
        node = self._node()
        # 沿 X 轴采点（d = |x|）
        pts = np.array([[0.0, 0.0, 0.0], [0.3, 0.0, 0.0], [0.6, 0.0, 0.0], [0.9, 0.0, 0.0]], dtype=np.float64)
        g = node._plateau_field(pts, self._unit_ball(), 1.0, 4.6, 0.0)
        ref = self.mod.gb_core.gaussian_field(pts, self._unit_ball(), 1.0, 4.6)
        np.testing.assert_allclose(g, ref, atol=1e-12)

    def test_plateau_flattens_center_and_smooth_edge(self):
        node = self._node()
        pts = np.array([[0.0, 0.0, 0.0], [0.5, 0.0, 0.0], [0.7, 0.0, 0.0],
                        [0.85, 0.0, 0.0], [1.0, 0.0, 0.0], [1.2, 0.0, 0.0]], dtype=np.float64)
        f = node._plateau_field(pts, self._unit_ball(), 1.0, 4.6, 0.7)
        # d ≤ 0.7：满强度平台（对比高斯在 0.5 处已衰减到 ~0.35）
        self.assertAlmostEqual(float(f[0]), 1.0, places=6)
        self.assertAlmostEqual(float(f[1]), 1.0, places=6)
        self.assertAlmostEqual(float(f[2]), 1.0, places=6)
        # 过渡带 0.7~1.0：单调降到 0
        self.assertGreater(float(f[3]), 0.0)
        self.assertLess(float(f[3]), 1.0)
        self.assertEqual(float(f[4]), 0.0)  # d=1 硬截止
        self.assertEqual(float(f[5]), 0.0)  # 球外
        # 高斯对照：0.5 处远小于平台值（证明平台化确实展平了尖峰）
        g = self.mod.gb_core.gaussian_field(pts, self._unit_ball(), 1.0, 4.6)
        self.assertLess(float(g[1]), 0.5)

    def test_plateau_used_by_bake_and_preview_path(self):
        node = self._node()
        object.__setattr__(node, "mask_plateau", 0.7)  # 默认已回退 0（纯高斯），此处显式开平台化
        settings = types.SimpleNamespace(brush_strength=1.0, brush_falloff_k=4.6)
        empty = types.SimpleNamespace(name="z", matrix_world=self._unit_ball(),
                                      ssmt_drag_zone=settings)
        pts = np.array([[0.5, 0.0, 0.0], [2.0, 0.0, 0.0]], dtype=np.float32)
        # _evaluate_zone_field 走平台化（node.mask_plateau=0.7）
        f = node._evaluate_zone_field(pts, empty, settings, None, None)
        self.assertAlmostEqual(float(f[0]), 1.0, places=5)
        self.assertEqual(float(f[1]), 0.0)
        # plateau=0 时退化为高斯：0.5 处 < 1
        object.__setattr__(node, "mask_plateau", 0.0)
        f0 = node._evaluate_zone_field(pts, empty, settings, None, None)
        self.assertLess(float(f0[0]), 0.5)

    def test_radius_scale_mismatch_warns(self):
        node = self._node()
        # 球 scale=0.1
        m = np.eye(4) * 0.1
        m[3, 3] = 1.0
        # radius=0.5 → 5× > 2.5× → 警告
        big = types.SimpleNamespace(
            name="z_big", matrix_world=m,
            ssmt_drag_zone=types.SimpleNamespace(radius=0.5))
        self.assertEqual(node._check_zone_radius_scale([big]), 1)
        # radius=0.15 → 1.5× → 不警告
        ok = types.SimpleNamespace(
            name="z_ok", matrix_world=m,
            ssmt_drag_zone=types.SimpleNamespace(radius=0.15))
        self.assertEqual(node._check_zone_radius_scale([ok]), 0)
        # radius=0 → 继承回退 0.25 → 恰为 2.5× 边界（严格大于才警告）→ 不警告
        inherit = types.SimpleNamespace(
            name="z_inherit", matrix_world=m,
            ssmt_drag_zone=types.SimpleNamespace(radius=0.0))
        self.assertEqual(node._check_zone_radius_scale([inherit]), 0)


class DragNodeGeodesicTests(unittest.TestCase):
    """沿表面传播：_zone_distances 测地路径 vs 体积球回退。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()

    def _mesh(self):
        """front z=0（顶点0-4）、back z=0.5（顶点5-9），片间不连通。"""
        front = np.array([[x * 0.1, 0.0, 0.0] for x in range(5)])
        back = np.array([[x * 0.1, 0.0, 0.5] for x in range(5)])
        verts = np.vstack([front, back]).astype(np.float32)
        tris = np.array([[0, 1, 2], [0, 2, 3], [0, 3, 4],
                         [5, 6, 7], [5, 7, 8], [5, 8, 9]])
        return verts, tris

    def _ball(self, radius=0.6):
        m = np.eye(4) * radius
        m[3, 3] = 1.0
        settings = types.SimpleNamespace(brush_strength=1.0, brush_falloff_k=4.6)
        return types.SimpleNamespace(name="z", matrix_world=m, ssmt_drag_zone=settings)

    def test_surface_mode_kills_back_strip(self):
        node = _make_node(self.mod)
        object.__setattr__(node, "surface_propagate", True)
        verts, tris = self._mesh()
        edges = self.mod.gb_core.edges_from_triangles(tris)
        empty = self._ball()
        f = node._evaluate_zone_field(verts, empty, empty.ssmt_drag_zone, None, None, edges)
        self.assertIsNotNone(f)
        self.assertGreater(float(f[2]), 0.3)                     # 接触面有权重
        np.testing.assert_array_equal(f[5:], np.zeros(5, dtype=np.float32))  # back 全 0

    def test_volume_mode_paints_back_strip(self):
        node = _make_node(self.mod)
        object.__setattr__(node, "surface_propagate", False)
        verts, tris = self._mesh()
        edges = self.mod.gb_core.edges_from_triangles(tris)
        empty = self._ball()
        f = node._evaluate_zone_field(verts, empty, empty.ssmt_drag_zone, None, None, edges)
        self.assertGreater(float(f[7]), 0.0)                     # 体积球穿透到 back

    def test_no_topology_falls_back_to_volume(self):
        node = _make_node(self.mod)
        object.__setattr__(node, "surface_propagate", True)
        verts, _ = self._mesh()
        empty = self._ball()
        f = node._evaluate_zone_field(verts, empty, empty.ssmt_drag_zone, None, None, None)
        self.assertGreater(float(f[7]), 0.0)                     # 无拓扑 → 体积球


if __name__ == "__main__":
    unittest.main()
