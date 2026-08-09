import importlib.util
import os
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path


def _install_module(name, **attrs):
    """安装 Fake 模块到 sys.modules"""
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_node_postprocess_uv_offset_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeItem:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeCollection(list):
    def add(self):
        item = _FakeItem()
        self.append(item)
        return item

    def remove(self, index):
        del self[index]


_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(PropertyGroup=object, Operator=object, UIList=object),
    props=types.SimpleNamespace(
        StringProperty=lambda **_kwargs: None,
        BoolProperty=lambda **_kwargs: None,
        IntProperty=lambda **_kwargs: None,
        EnumProperty=lambda **_kwargs: None,
        CollectionProperty=lambda **_kwargs: None,
    ),
    data=types.SimpleNamespace(objects={}),
    utils=types.SimpleNamespace(register_class=lambda _cls: None, unregister_class=lambda _cls: None),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module(
    "bpy.types",
    PropertyGroup=object,
    Operator=object,
    UIList=object,
    NodeSocket=object,
)
_install_module(
    f"{PKG}.blueprint.node_postprocess_base",
    SSMTNode_PostProcess_Base=type(
        "_FakePostProcessBase",
        (object,),
        {
            "split_anim_driver_block_content": staticmethod(lambda content: ("", content)),
            "split_auto_appended_tail_content": staticmethod(lambda content: (content, "")),
            "_create_cumulative_backup": lambda self, ini_file_path, mod_export_path: None,
        },
    ),
)
_install_module(
    f"{PKG}.blueprint.variable_registry",
    allocate_uv_offset_variable_name=lambda axis, **_kwargs: f"uv_offset_{str(axis).lower()}",
    mark_variable_name_used=lambda *_args, **_kwargs: None,
    normalize_variable_name=lambda value: str(value or "").strip(),
)
_install_module(
    f"{PKG}.common.mod_path_compat",
    ensure_resource_alias_section=lambda sections, resource_name, alias_suffix, source_candidates=None: _ensure_alias(
        sections, resource_name, alias_suffix, source_candidates,
    ),
)


def _fake_extract_prefix_info(name):
    # 模拟真实 ObjectPrefixHelper：LOD0.a913e9a9-56682-0.Mesh -> 结构化前缀
    if str(name or "").startswith("LOD") and "." in str(name or ""):
        first_dot = str(name).index(".")
        after_lod = str(name)[first_dot + 1:]
        if "-" in after_lod and "." in after_lod:
            unique = after_lod.split(".", 1)[0]
            return (f"LOD0.{unique}", ".", str(name).split(".", 2)[-1])
    return None


def _fake_parse_prefix_parts(prefix):
    prefix = str(prefix or "")
    if prefix.startswith("LOD"):
        parts = prefix.split(".")
        if len(parts) >= 2:
            bare_unique_str = parts[1]
            return {
                "bare_unique_str": bare_unique_str,
                "draw_ib": bare_unique_str.split("-")[0],
            }
    return {}


def _fake_split_name_and_prefix(name, prefix="", separator=""):
    name = str(name or "")
    prefix = str(prefix or "")
    if prefix and name.startswith(prefix):
        return (name, prefix, name[len(prefix) + 1:])
    return (name, "", name)


_install_module(
    f"{PKG}.common.object_prefix_helper",
    ObjectPrefixHelper=types.SimpleNamespace(
        extract_prefix_info=_fake_extract_prefix_info,
        parse_prefix_parts=_fake_parse_prefix_parts,
        split_name_and_prefix=_fake_split_name_and_prefix,
        resolve_source_object_name=lambda name: name,
    ),
)


def _ensure_alias(sections, resource_name, alias_suffix, source_candidates=None):
    alias_section_name = f"[{resource_name}{alias_suffix}]"
    if alias_section_name in sections:
        return alias_section_name
    source_section_name = f"[{resource_name}]"
    if source_section_name in sections:
        sections[alias_section_name] = list(sections[source_section_name])
    return alias_section_name


module_path = Path(__file__).resolve().parents[1] / "blueprint" / "node_postprocess_uv_offset.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.node_postprocess_uv_offset", module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def _make_uv_attrs_node_with_layout(use_common_layout=True):
    uv_attrs_node = module.SSMTNode_PostProcess_UVAttrs()
    uv_attrs_node.uv_attributes = _FakeCollection()
    if use_common_layout:
        for attr_type, attr_name, apply_offset in module.COMMON_ZZMI_UV_LAYOUT:
            item = uv_attrs_node.uv_attributes.add()
            item.attr_type = attr_type
            item.attr_name = attr_name
            item.apply_offset = apply_offset
    else:
        item = uv_attrs_node.uv_attributes.add()
        item.attr_type = 'float2'
        item.attr_name = 'uv'
        item.apply_offset = True
    return uv_attrs_node


def _make_uv_attrs_node_28b():
    """ZZMI 常用 20B 布局 + half2 uv3/half2 uv4（共 28B）。"""
    uv_attrs_node = module.SSMTNode_PostProcess_UVAttrs()
    uv_attrs_node.uv_attributes = _FakeCollection()
    for attr_type, attr_name, apply_offset in module.COMMON_ZZMI_UV_LAYOUT:
        item = uv_attrs_node.uv_attributes.add()
        item.attr_type = attr_type
        item.attr_name = attr_name
        item.apply_offset = apply_offset
    for attr_name in ("uv3", "uv4"):
        item = uv_attrs_node.uv_attributes.add()
        item.attr_type = 'half2'
        item.attr_name = attr_name
        item.apply_offset = True
    return uv_attrs_node


class _FakeLink:
    def __init__(self, from_node):
        self.from_node = from_node


class _FakeSocket:
    def __init__(self, linked, from_node=None, name="", bl_idname="SSMTSocketUVAttrs"):
        self.is_linked = linked
        self.links = [_FakeLink(from_node)] if from_node else []
        self.name = name
        self.bl_idname = bl_idname


class _FakeInputs(list):
    """模拟 bpy.types.NodeInputs：支持 new/remove/迭代。"""

    def new(self, bl_idname, name):
        socket = _FakeSocket(False, None, name=name, bl_idname=bl_idname)
        self.append(socket)
        return socket


class UVOffsetNodeTests(unittest.TestCase):
    """测试 UV 偏移节点与 UV 属性定义节点的核心逻辑"""

    def _make_node(self):
        node = module.SSMTNode_PostProcess_UVOffset()
        node.uv_offset_variable_items = _FakeCollection()
        node.uv_objects = _FakeCollection()
        # UVOffset 继承 Base，index 0 始终是后处理 Input 口，动态 UV 属性口从 index 1 开始
        node.inputs = _FakeInputs([_FakeSocket(False, name="Input")])
        return node

    def test_common_zzmi_uv_layout_parses_to_20_bytes(self):
        uv_attrs_node = _make_uv_attrs_node_with_layout(True)
        attributes = uv_attrs_node.get_uv_attributes()
        self.assertEqual(module.uv_attributes_total_bytes(attributes), 20)
        self.assertEqual(
            [(a['name'], a['type'], a['offset'], a['apply_offset']) for a in attributes],
            [
                ('color', 'rgba8', 0, False),
                ('uv0', 'half2', 4, True),
                ('uv1', 'float2', 8, True),
                ('uv2', 'half2', 16, True),
            ],
        )

    def test_uv_attrs_node_custom_attributes(self):
        uv_attrs_node = _make_uv_attrs_node_with_layout(False)
        attributes = uv_attrs_node.get_uv_attributes()
        self.assertEqual(module.uv_attributes_total_bytes(attributes), 8)
        self.assertEqual(attributes[0]['name'], 'uv')
        self.assertEqual(attributes[0]['type'], 'float2')

    def test_uv_offset_node_reads_linked_uv_attrs_node(self):
        uv_attrs_node = _make_uv_attrs_node_with_layout(True)
        node = self._make_node()
        node.inputs.append(_FakeSocket(True, uv_attrs_node, name="UV属性 a913e9a9"))
        attributes = node._get_uv_attributes_for_prefix("a913e9a9")
        self.assertIsNotNone(attributes)
        self.assertEqual(len(attributes), 4)

    def test_generate_uv_apply_code_handles_half2_and_float2(self):
        uv_attrs_node = _make_uv_attrs_node_with_layout(True)
        attributes = uv_attrs_node.get_uv_attributes()
        code, warning = module.SSMTNode_PostProcess_UVOffset._generate_uv_apply_code(attributes, 20)
        self.assertIsNotNone(code)
        self.assertIn("f16tof32", code)
        self.assertIn("f32tof16", code)
        self.assertIn("asfloat(uint2(data[2], data[3]))", code)
        # uv2 (half2 @ 16) 也应参与偏移
        self.assertIn("// half2 uv2 @ 16", code)
        self.assertIn("data[4]", code)
        # color 不参与偏移
        self.assertNotIn("data[0]", code.split("// half2 uv0")[0])

    def test_generate_uv_apply_code_rejects_stride_mismatch(self):
        uv_attrs_node = _make_uv_attrs_node_with_layout(True)
        attributes = uv_attrs_node.get_uv_attributes()
        code, warning = module.SSMTNode_PostProcess_UVOffset._generate_uv_apply_code(attributes, 8)
        self.assertIsNotNone(code)
        self.assertIn("越界", warning or "")

    def test_ensure_uv_offset_variable_map_creates_xy(self):
        node = self._make_node()
        created_count, backfilled_count = node.ensure_uv_offset_variable_map(["X", "Y"])
        self.assertEqual(created_count, 2)
        self.assertEqual(backfilled_count, 0)
        self.assertEqual([item.axis_name for item in node.uv_offset_variable_items], ["X", "Y"])
        self.assertEqual(node.uv_offset_variable_items[0].assigned_variable_name, "uv_offset_x")
        self.assertEqual(node.uv_offset_variable_items[1].assigned_variable_name, "uv_offset_y")

        # 再次执行应幂等
        created_count, _ = node.ensure_uv_offset_variable_map(["X", "Y"])
        self.assertEqual(created_count, 0)

    def test_ensure_uv_offset_variable_map_preserves_custom_names(self):
        node = self._make_node()
        item = node.uv_offset_variable_items.add()
        item.axis_name = "X"
        item.assigned_variable_name = "uv_offset_x"
        item.custom_variable_name = "my_offset_x"
        node.ensure_uv_offset_variable_map(["X", "Y"])
        x_var, y_var = node.get_uv_offset_export_variable_names()
        self.assertEqual(x_var, "$my_offset_x")
        self.assertEqual(y_var, "$uv_offset_y")

    def test_compute_dispatch_group_count(self):
        node = self._make_node()
        self.assertEqual(node._compute_dispatch_group_count(0, threads_per_group=16), 1)
        self.assertEqual(node._compute_dispatch_group_count(16, threads_per_group=16), 1)
        self.assertEqual(node._compute_dispatch_group_count(17, threads_per_group=16), 2)

    def _write_sample_mod(self, tmp_dir):
        meshes_dir = os.path.join(tmp_dir, "Meshes0000")
        os.makedirs(meshes_dir, exist_ok=True)

        ib_path = os.path.join(meshes_dir, "LOD0.a913e9a9-56682-0-Index.buf")
        with open(ib_path, "wb") as f:
            f.write(struct.pack("<10I", *range(100, 110)))

        texcoord_path = os.path.join(meshes_dir, "a913e9a9-Texcoord.buf")
        with open(texcoord_path, "wb") as f:
            f.write(b"\x00" * (100 * 20))

        ini_path = os.path.join(tmp_dir, "mod.ini")
        with open(ini_path, "w", encoding="utf-8") as f:
            f.write("[Constants]\n")
            f.write("global $active0 = 1\n\n")
            f.write("[TextureOverride_a913e9a9_a913e9a9_VertexLimitRaise]\n")
            f.write("override_byte_stride = 40\n")
            f.write("override_vertex_count = 100\n")
            f.write("uav_byte_stride = 4\n\n")
            f.write("[Resourcea913e9a9Texcoord]\n")
            f.write("type = Buffer\n")
            f.write("stride = 20\n")
            f.write("filename = Meshes0000/a913e9a9-Texcoord.buf\n\n")
            f.write("[Resource_LOD0.a913e9a9_56682_0_Index]\n")
            f.write("type = Buffer\n")
            f.write("format = DXGI_FORMAT_R32_UINT\n")
            f.write("filename = Meshes0000/LOD0.a913e9a9-56682-0-Index.buf\n\n")
            f.write("[TextureOverride_LOD0.a913e9a9_56682_0]\n")
            f.write("hash = a913e9a9\n")
            f.write("ib = Resource_LOD0.a913e9a9_56682_0_Index\n")
            f.write("; [mesh:LOD0.a913e9a9-56682-0.Mesh]\n")
            f.write("drawindexed = 10,0,0\n")
        return ini_path

    def _write_sample_mod_multi(self, tmp_dir, specs):
        """按 specs 生成包含多个独立 IB 的模组目录。
        spec: dict(object_name, h_prefix, stride, vertex_count=100, index_count=10)
        """
        meshes_dir = os.path.join(tmp_dir, "Meshes0000")
        os.makedirs(meshes_dir, exist_ok=True)

        for spec in specs:
            object_name = spec["object_name"]
            h_prefix = spec["h_prefix"]
            stride = spec["stride"]
            vertex_count = spec.get("vertex_count", 100)
            index_count = spec.get("index_count", 10)
            unique = object_name.split(".", 1)[1].split("-", 1)[0]
            unique_rest = object_name.split(".", 1)[1].split("-", 1)[1].rsplit(".", 1)[0]

            ib_path = os.path.join(meshes_dir, f"LOD0.{unique}-{unique_rest}-Index.buf")
            with open(ib_path, "wb") as f:
                f.write(struct.pack(f"<{index_count}I", *range(100, 100 + index_count)))

            texcoord_path = os.path.join(meshes_dir, f"{h_prefix}-Texcoord.buf")
            with open(texcoord_path, "wb") as f:
                f.write(b"\x00" * (vertex_count * stride))

        ini_lines = ["[Constants]\n", "global $active0 = 1\n\n"]
        for spec in specs:
            object_name = spec["object_name"]
            h_prefix = spec["h_prefix"]
            stride = spec["stride"]
            vertex_count = spec.get("vertex_count", 100)
            index_count = spec.get("index_count", 10)
            unique = object_name.split(".", 1)[1].split("-", 1)[0]
            unique_rest = object_name.split(".", 1)[1].split("-", 1)[1].rsplit(".", 1)[0]
            safe_rest = unique_rest.replace("-", "_")

            ini_lines.extend([
                f"[TextureOverride_{h_prefix}_{h_prefix}_VertexLimitRaise]\n",
                f"override_byte_stride = {stride * 2}\n",
                f"override_vertex_count = {vertex_count}\n",
                "uav_byte_stride = 4\n\n",
                f"[Resource{h_prefix}Texcoord]\n",
                "type = Buffer\n",
                f"stride = {stride}\n",
                f"filename = Meshes0000/{h_prefix}-Texcoord.buf\n\n",
                f"[Resource_LOD0.{unique}_{safe_rest}_Index]\n",
                "type = Buffer\n",
                "format = DXGI_FORMAT_R32_UINT\n",
                f"filename = Meshes0000/LOD0.{unique}-{unique_rest}-Index.buf\n\n",
                f"[TextureOverride_LOD0.{unique}_{safe_rest}]\n",
                f"hash = {h_prefix}\n",
                f"ib = Resource_LOD0.{unique}_{safe_rest}_Index\n",
                f"; [mesh:{object_name}]\n",
                f"drawindexed = {index_count},0,0\n\n",
            ])

        ini_path = os.path.join(tmp_dir, "mod.ini")
        with open(ini_path, "w", encoding="utf-8") as f:
            f.writelines(ini_lines)
        return ini_path

    def test_execute_postprocess_generates_ini_and_shader(self):
        node = self._make_node()
        item = node.uv_objects.add()
        item.object_name = "LOD0.a913e9a9-56682-0.Mesh"
        node.inputs.append(_FakeSocket(True, _make_uv_attrs_node_with_layout(True), name="UV属性 a913e9a9"))

        with tempfile.TemporaryDirectory() as tmp_dir:
            ini_path = self._write_sample_mod(tmp_dir)
            node.execute_postprocess(tmp_dir)

            with open(ini_path, "r", encoding="utf-8") as f:
                ini_content = f.read()

            self.assertIn("[CustomShader_a913e9a9_UVOffset]", ini_content)
            self.assertIn("x100 = $uv_offset_x", ini_content)
            self.assertIn("x101 = $uv_offset_y", ini_content)
            self.assertIn("[Resourcea913e9a9TexcoordRaw]", ini_content)
            self.assertIn("stride = 4", ini_content)
            self.assertIn("cs-u5 = copy Resourcea913e9a9TexcoordRaw", ini_content)
            self.assertIn("Resourcea913e9a9Texcoord = ref cs-u5", ini_content)
            self.assertIn("Dispatch = 7, 1, 1", ini_content)
            self.assertIn("global persist $uv_offset_x = 0.0", ini_content)
            self.assertIn("post Resourcea913e9a9Texcoord = copy_desc Resourcea913e9a9Texcoord_0", ini_content)
            self.assertIn("post run = CustomShader_a913e9a9_UVOffset", ini_content)
            self.assertIn("run = CustomShader_a913e9a9_UVOffset", ini_content)
            self.assertIn("[Resourcea913e9a9Texcoord_0]", ini_content)

            shader_path = os.path.join(tmp_dir, "res", "uv_offset_a913e9a9.hlsl")
            self.assertTrue(os.path.exists(shader_path))
            with open(shader_path, "r", encoding="utf-8") as f:
                shader_content = f.read()
            self.assertIn("vertex_id >= 100u && vertex_id <= 109u", shader_content)
            self.assertIn("UV_STREAM_UINTS_PER_VERTEX = 5", shader_content)
            self.assertIn("f16tof32", shader_content)
            self.assertIn("asfloat(uint2(data[2], data[3]))", shader_content)
            # COLOR 不应被偏移
            self.assertNotIn("asfloat(uint2(data[0], data[1]))", shader_content)

            # 物体范围已回填到列表项
            self.assertEqual(item.start_vertex, 100)
            self.assertEqual(item.end_vertex, 109)

    def test_execute_postprocess_uses_default_layout_without_uv_attrs_node(self):
        node = self._make_node()
        item = node.uv_objects.add()
        item.object_name = "LOD0.a913e9a9-56682-0.Mesh"

        with tempfile.TemporaryDirectory() as tmp_dir:
            ini_path = self._write_sample_mod(tmp_dir)
            node.execute_postprocess(tmp_dir)
            with open(ini_path, "r", encoding="utf-8") as f:
                ini_content = f.read()
            self.assertIn("[CustomShader_a913e9a9_UVOffset]", ini_content)

            shader_path = os.path.join(tmp_dir, "res", "uv_offset_a913e9a9.hlsl")
            self.assertTrue(os.path.exists(shader_path))
            with open(shader_path, "r", encoding="utf-8") as f:
                shader_content = f.read()
            # 默认布局：COLOR 不偏移，half2 uv0 / float2 uv1 / half2 uv2 都偏移
            self.assertIn("// half2 uv0 @ 4", shader_content)
            self.assertIn("// float2 uv1 @ 8", shader_content)
            self.assertIn("// half2 uv2 @ 16", shader_content)
            self.assertNotIn("asfloat(uint2(data[0], data[1]))", shader_content)

    def test_get_uv_attributes_returns_default_layout_without_node(self):
        node = self._make_node()
        attributes = node._get_uv_attributes_for_prefix("a913e9a9")
        self.assertIsNotNone(attributes)
        self.assertEqual(len(attributes), 4)
        self.assertEqual(module.uv_attributes_total_bytes(attributes), 20)
        self.assertEqual(
            [(a['name'], a['type'], a['offset'], a['apply_offset']) for a in attributes],
            [
                ('color', 'rgba8', 0, False),
                ('uv0', 'half2', 4, True),
                ('uv1', 'float2', 8, True),
                ('uv2', 'half2', 16, True),
            ],
        )

    def test_get_uv_attributes_returns_default_layout_with_empty_uv_attrs_node(self):
        # 接上 UV 属性定义节点但没有任何属性项时，也回退默认布局
        empty_uv_attrs_node = module.SSMTNode_PostProcess_UVAttrs()
        empty_uv_attrs_node.uv_attributes = _FakeCollection()

        node = self._make_node()
        node.inputs.append(_FakeSocket(True, empty_uv_attrs_node, name="UV属性 a913e9a9"))
        attributes = node._get_uv_attributes_for_prefix("a913e9a9")
        self.assertIsNotNone(attributes)
        self.assertEqual(len(attributes), 4)
        self.assertEqual(module.uv_attributes_total_bytes(attributes), 20)
        self.assertEqual(attributes[0]['name'], 'color')
        self.assertEqual(attributes[0]['apply_offset'], False)

    def test_execute_postprocess_uses_default_layout_with_empty_uv_attrs_node(self):
        empty_uv_attrs_node = module.SSMTNode_PostProcess_UVAttrs()
        empty_uv_attrs_node.uv_attributes = _FakeCollection()

        node = self._make_node()
        item = node.uv_objects.add()
        item.object_name = "LOD0.a913e9a9-56682-0.Mesh"
        node.inputs.append(_FakeSocket(True, empty_uv_attrs_node, name="UV属性 a913e9a9"))

        with tempfile.TemporaryDirectory() as tmp_dir:
            ini_path = self._write_sample_mod(tmp_dir)
            node.execute_postprocess(tmp_dir)
            with open(ini_path, "r", encoding="utf-8") as f:
                ini_content = f.read()
            self.assertIn("[CustomShader_a913e9a9_UVOffset]", ini_content)

            shader_path = os.path.join(tmp_dir, "res", "uv_offset_a913e9a9.hlsl")
            self.assertTrue(os.path.exists(shader_path))
            with open(shader_path, "r", encoding="utf-8") as f:
                shader_content = f.read()
            self.assertIn("// half2 uv0 @ 4", shader_content)
            self.assertIn("// float2 uv1 @ 8", shader_content)
            self.assertIn("// half2 uv2 @ 16", shader_content)
            self.assertNotIn("asfloat(uint2(data[0], data[1]))", shader_content)

    def test_execute_postprocess_two_ibs_with_own_uv_attrs_nodes(self):
        """两个不同 IB（20B / 28B）各自连接 UV 属性定义节点，分别生成着色器。"""
        node = self._make_node()
        item_a = node.uv_objects.add()
        item_a.object_name = "LOD0.9004a39a-20472-0.Mesh"
        item_b = node.uv_objects.add()
        item_b.object_name = "LOD0.241deac5-20472-0.Mesh"
        node.inputs.append(_FakeSocket(True, _make_uv_attrs_node_with_layout(True), name="UV属性 9004a39a"))
        node.inputs.append(_FakeSocket(True, _make_uv_attrs_node_28b(), name="UV属性 241deac5"))

        specs = [
            {"object_name": "LOD0.9004a39a-20472-0.Mesh", "h_prefix": "9004a39a", "stride": 20},
            {"object_name": "LOD0.241deac5-20472-0.Mesh", "h_prefix": "241deac5", "stride": 28},
        ]
        with tempfile.TemporaryDirectory() as tmp_dir:
            ini_path = self._write_sample_mod_multi(tmp_dir, specs)
            node.execute_postprocess(tmp_dir)

            with open(ini_path, "r", encoding="utf-8") as f:
                ini_content = f.read()
            self.assertIn("[CustomShader_9004a39a_UVOffset]", ini_content)
            self.assertIn("[CustomShader_241deac5_UVOffset]", ini_content)
            self.assertIn("post run = CustomShader_9004a39a_UVOffset", ini_content)
            self.assertIn("post run = CustomShader_241deac5_UVOffset", ini_content)

            shader_a_path = os.path.join(tmp_dir, "res", "uv_offset_9004a39a.hlsl")
            shader_b_path = os.path.join(tmp_dir, "res", "uv_offset_241deac5.hlsl")
            self.assertTrue(os.path.exists(shader_a_path))
            self.assertTrue(os.path.exists(shader_b_path))

            with open(shader_a_path, "r", encoding="utf-8") as f:
                shader_a = f.read()
            with open(shader_b_path, "r", encoding="utf-8") as f:
                shader_b = f.read()
            self.assertIn("UV_STREAM_UINTS_PER_VERTEX = 5", shader_a)
            self.assertIn("UV_STREAM_UINTS_PER_VERTEX = 7", shader_b)
            # 28B 布局额外偏移 half2 uv3 / half2 uv4
            self.assertIn("// half2 uv3 @ 20", shader_b)
            self.assertIn("// half2 uv4 @ 24", shader_b)
            self.assertNotIn("uv3", shader_a)

    def test_execute_postprocess_28b_prefix_without_uv_attrs_falls_back_and_skips(self):
        """第二个 28B IB 未连接 UV 属性定义节点：回退默认 20B 布局后与 stride 不匹配，跳过。"""
        node = self._make_node()
        item_a = node.uv_objects.add()
        item_a.object_name = "LOD0.9004a39a-20472-0.Mesh"
        item_b = node.uv_objects.add()
        item_b.object_name = "LOD0.241deac5-20472-0.Mesh"
        node.inputs.append(_FakeSocket(True, _make_uv_attrs_node_with_layout(True), name="UV属性 9004a39a"))

        specs = [
            {"object_name": "LOD0.9004a39a-20472-0.Mesh", "h_prefix": "9004a39a", "stride": 20},
            {"object_name": "LOD0.241deac5-20472-0.Mesh", "h_prefix": "241deac5", "stride": 28},
        ]
        with tempfile.TemporaryDirectory() as tmp_dir:
            ini_path = self._write_sample_mod_multi(tmp_dir, specs)
            node.execute_postprocess(tmp_dir)

            with open(ini_path, "r", encoding="utf-8") as f:
                ini_content = f.read()
            self.assertIn("[CustomShader_9004a39a_UVOffset]", ini_content)
            self.assertNotIn("[CustomShader_241deac5_UVOffset]", ini_content)
            self.assertFalse(os.path.exists(os.path.join(tmp_dir, "res", "uv_offset_241deac5.hlsl")))


if __name__ == "__main__":
    unittest.main()
