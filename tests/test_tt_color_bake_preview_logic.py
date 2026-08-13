# -*- coding: utf-8 -*-
import importlib.util
import builtins
import sys
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_tt_color_bake_preview_logic_test_pkg"
for package_name in (PKG, f"{PKG}.toolkit"):
    package = _install_module(package_name)
    package.__path__ = []


_fake_bpy = types.SimpleNamespace(
    props=types.SimpleNamespace(IntProperty=lambda **_kwargs: None),
    types=types.SimpleNamespace(
        Operator=object,
        RenderSettings=types.SimpleNamespace(
            bl_rna=types.SimpleNamespace(
                properties={
                    "engine": types.SimpleNamespace(
                        enum_items=[types.SimpleNamespace(identifier="BLENDER_EEVEE")]
                    )
                }
            )
        ),
    ),
    context=types.SimpleNamespace(scene=types.SimpleNamespace(render=types.SimpleNamespace(engine="BLENDER_EEVEE"))),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module("bmesh")


module_path = Path(__file__).resolve().parents[1] / "toolkit" / "tt_color_bake.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.toolkit.tt_color_bake", module_path)
tt_color_bake = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = tt_color_bake
spec.loader.exec_module(tt_color_bake)


class _FakeLink:
    def __init__(self, from_node, to_node):
        self.from_node = from_node
        self.to_node = to_node


class _FakeSocket:
    def __init__(self, name="", linked_from=None):
        self.name = name
        self.links = []
        self.is_linked = linked_from is not None
        if linked_from is not None:
            self.links.append(_FakeLink(linked_from, None))


class _FakeSocketMap(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class _FakeNode:
    def __init__(self, node_type, *, image=None, active=False):
        self.type = node_type
        self.image = image
        self.is_active_output = active
        self.inputs = []
        self.outputs = _FakeSocketMap({"Shader": object(), "Color": object(), "Alpha": object(), "Emission": object()})


class _RestoreSocket:
    def __init__(self, identifier, owner):
        self.identifier = identifier
        self.owner = owner


class _RestoreNode:
    def __init__(self, name, inputs=(), outputs=()):
        self.name = name
        self.inputs = [_RestoreSocket(i, self) for i in inputs]
        self.outputs = [_RestoreSocket(o, self) for o in outputs]


class _RestoreLink:
    def __init__(self, from_node, from_socket, to_node, to_socket):
        self.from_node = from_node
        self.from_socket = from_socket
        self.to_node = to_node
        self.to_socket = to_socket


class _FakeNodeCollection(list):
    def __init__(self, tree):
        super().__init__()
        self._tree = tree

    def get(self, name):
        return next((n for n in self if n.name == name), None)

    def remove(self, node):
        if node not in self:
            return
        super().remove(node)
        # 模拟 Blender：删除节点时连带删除其连线
        for link in list(self._tree.links):
            if link.from_node is node or link.to_node is node:
                self._tree.links.remove(link)


class _FakeLinkCollection(list):
    def new(self, from_socket, to_socket):
        link = _RestoreLink(from_socket.owner, from_socket, to_socket.owner, to_socket)
        self.append(link)
        return link

    def remove(self, link):
        if link in self:
            super().remove(link)


class _FakePixelBuffer:
    def __init__(self, values):
        self.values = list(values)

    def foreach_get(self, target):
        target[:] = self.values

    def foreach_set(self, values):
        self.values = list(values)

    def __getitem__(self, item):
        return self.values[item]

    def __setitem__(self, item, value):
        if isinstance(item, slice):
            self.values = list(value)
        else:
            self.values[item] = value


class _FakeImage:
    def __init__(self, width, height, values):
        self.size = (width, height)
        self.pixels = _FakePixelBuffer(values)


class _FakeMaterial:
    def __init__(self, name):
        self.name = name
        self.use_nodes = True


def _run_bake_execute(material_names, rules=()):
    materials = [_FakeMaterial(name) for name in material_names]
    selected = [
        types.SimpleNamespace(
            type="MESH",
            material_slots=[types.SimpleNamespace(material=material)],
        )
        for material in materials
    ]
    props = types.SimpleNamespace(
        output_dir="",
        color_bake_node_types="ALL",
        color_bake_size=512,
        bake_resolution_use_rules=bool(rules),
        bake_resolution_rules=list(rules),
        color_bake_preview_type="FLAT",
        color_bake_unfold_by_uv=False,
        color_bake_import_to_material=False,
    )
    context = types.SimpleNamespace(
        scene=types.SimpleNamespace(texture_tools_props=props),
        selected_objects=selected,
    )
    operator = tt_color_bake.TT_OT_bake_color_maps()
    reports = []
    renders = []
    operator.report = lambda levels, message: reports.append((set(levels), message))
    operator.render_material_preview = (
        lambda material, output_path, preview_type, bake_size, **_kwargs:
        renders.append((material.name, output_path, preview_type, bake_size)) or True
    )

    with tempfile.TemporaryDirectory() as output_dir:
        props.output_dir = output_dir
        with mock.patch.object(
            tt_color_bake.bpy,
            "path",
            types.SimpleNamespace(abspath=lambda value: value),
            create=True,
        ):
            result = operator.execute(context)

    return result, reports, renders


class _FakeNodeTree:
    def __init__(self):
        self.nodes = _FakeNodeCollection(self)
        self.links = _FakeLinkCollection()

    def add_node(self, name, inputs=(), outputs=()):
        node = _RestoreNode(name, inputs, outputs)
        self.nodes.append(node)
        return node

    def add_link(self, from_node, from_socket_id, to_node, to_socket_id):
        from_socket = next(s for s in from_node.outputs if s.identifier == from_socket_id)
        to_socket = next(s for s in to_node.inputs if s.identifier == to_socket_id)
        self.links.append(_RestoreLink(from_node, from_socket, to_node, to_socket))


class PreviewLogicTests(unittest.TestCase):
    def test_find_last_mix_shader_returns_last_one(self):
        first = _FakeNode("MIX_SHADER")
        second = _FakeNode("MIX_SHADER")
        tree = types.SimpleNamespace(nodes=[first, _FakeNode("BSDF_DIFFUSE"), second])

        self.assertIs(tt_color_bake._find_last_mix_shader(tree), second)

    def test_find_last_mix_shader_returns_none_when_absent(self):
        tree = types.SimpleNamespace(nodes=[_FakeNode("OUTPUT_MATERIAL", active=True)])

        self.assertIsNone(tt_color_bake._find_last_mix_shader(tree))

    def test_find_first_image_texture_upstream_walks_back_from_surface(self):
        tex = _FakeNode("TEX_IMAGE", image=types.SimpleNamespace(alpha_mode="CHANNEL_PACKED"))
        bsdf = _FakeNode("BSDF_DIFFUSE")
        bsdf.inputs = [_FakeSocket("Color", linked_from=tex)]
        output = _FakeNode("OUTPUT_MATERIAL", active=True)
        output.inputs = _FakeSocketMap({"Surface": _FakeSocket("Surface", linked_from=bsdf)})

        self.assertIs(tt_color_bake._find_first_image_texture_upstream(output), tex)

    def test_find_first_image_texture_upstream_returns_none_without_surface_chain(self):
        output = _FakeNode("OUTPUT_MATERIAL", active=True)
        output.inputs = _FakeSocketMap({"Surface": _FakeSocket("Surface")})

        self.assertIsNone(tt_color_bake._find_first_image_texture_upstream(output))

    def test_configure_bake_render_scene_enables_transparent_film(self):
        original_world = object()
        original_scene = types.SimpleNamespace(world=original_world)
        temp_scene = types.SimpleNamespace(
            render=types.SimpleNamespace(engine="", film_transparent=False),
            view_settings=types.SimpleNamespace(view_transform=""),
        )

        tt_color_bake._configure_bake_render_scene(temp_scene, original_scene)

        # 回归：alpha pass 依赖透明背景，否则世界底色会以约 5% 的淡残留混入 alpha 通道
        self.assertTrue(temp_scene.render.film_transparent)
        self.assertIs(temp_scene.world, original_world)
        self.assertEqual(temp_scene.render.engine, "BLENDER_EEVEE")
        self.assertEqual(temp_scene.view_settings.view_transform, "Standard")

    def test_configure_bake_render_scene_disables_eevee_bloom(self):
        original_scene = types.SimpleNamespace(world=None)
        temp_scene = types.SimpleNamespace(
            world=None,
            render=types.SimpleNamespace(engine="", film_transparent=False),
            eevee=types.SimpleNamespace(taa_render_samples=1, use_bloom=True),
            view_settings=types.SimpleNamespace(view_transform=""),
        )

        tt_color_bake._configure_bake_render_scene(temp_scene, original_scene)

        self.assertEqual(temp_scene.eevee.taa_render_samples, 64)
        self.assertFalse(temp_scene.eevee.use_bloom)

    def test_combine_rgb_alpha_unpremultiplies_linear_rgb_and_uses_alpha_pass_red(self):
        # 像素 0：线性直通颜色为 (0.25, 0.5, 1.0)，覆盖率为 0.5，因此 RGB pass 是预乘值。
        rgb_image = _FakeImage(
            2,
            1,
            [0.125, 0.25, 0.5, 0.5, 0.7, 0.2, 0.1, 0.0],
        )
        alpha_image = _FakeImage(
            2,
            1,
            [0.75, 0.1, 0.2, 1.0, 0.25, 0.8, 0.9, 1.0],
        )
        final_image = _FakeImage(2, 1, [0.0] * 8)

        tt_color_bake.TT_OT_bake_color_maps._combine_rgb_alpha(
            rgb_image, alpha_image, final_image
        )

        output = final_image.pixels.values
        expected_rgb = [
            tt_color_bake._linear_to_srgb(0.25),
            tt_color_bake._linear_to_srgb(0.5),
            tt_color_bake._linear_to_srgb(1.0),
        ]
        for actual, expected in zip(output[:3], expected_rgb):
            self.assertAlmostEqual(actual, expected, places=6)
        self.assertAlmostEqual(output[3], 0.75, places=6)
        self.assertEqual(output[4:7], [0.0, 0.0, 0.0])
        self.assertAlmostEqual(output[7], 0.25, places=6)

    def test_linear_to_srgb_clamps_out_of_range_and_handles_transfer_breakpoint(self):
        self.assertEqual(tt_color_bake._linear_to_srgb(-1.0), 0.0)
        self.assertAlmostEqual(tt_color_bake._linear_to_srgb(2.0), 1.0, places=6)
        self.assertAlmostEqual(
            tt_color_bake._linear_to_srgb(0.0031308),
            0.040449936,
            places=6,
        )

    def test_combine_rgb_alpha_uses_python_fallback_without_numpy(self):
        rgb_image = _FakeImage(1, 1, [0.125, 0.25, 0.5, 0.5])
        alpha_image = _FakeImage(1, 1, [0.75, 0.0, 0.0, 1.0])
        final_image = _FakeImage(1, 1, [0.0] * 4)
        original_import = builtins.__import__

        def import_without_numpy(name, *args, **kwargs):
            if name == "numpy":
                raise ImportError("numpy disabled for fallback test")
            return original_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=import_without_numpy):
            tt_color_bake.TT_OT_bake_color_maps._combine_rgb_alpha(
                rgb_image, alpha_image, final_image
            )

        output = final_image.pixels.values
        self.assertAlmostEqual(output[0], tt_color_bake._linear_to_srgb(0.25), places=6)
        self.assertAlmostEqual(output[1], tt_color_bake._linear_to_srgb(0.5), places=6)
        self.assertAlmostEqual(output[2], tt_color_bake._linear_to_srgb(1.0), places=6)
        self.assertAlmostEqual(output[3], 0.75, places=6)

    def test_combine_rgb_alpha_rejects_mismatched_image_dimensions(self):
        rgb_image = _FakeImage(2, 1, [0.0] * 8)
        alpha_image = _FakeImage(1, 1, [0.0] * 4)
        final_image = _FakeImage(2, 1, [0.0] * 8)

        with self.assertRaisesRegex(ValueError, "尺寸必须一致"):
            tt_color_bake.TT_OT_bake_color_maps._combine_rgb_alpha(
                rgb_image, alpha_image, final_image
            )

    def test_batch_bake_allocates_distinct_paths_after_name_sanitization_collision(self):
        result, reports, renders = _run_bake_execute(["Mat A", "Mat/A"])

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(len(renders), 2)
        self.assertEqual(
            [Path(call[1]).name for call in renders],
            ["MatA.png", "MatA_2.png"],
        )
        self.assertTrue(any("2/2" in message for _levels, message in reports))

    def test_batch_bake_skips_invalid_resolution_regex_and_uses_next_rule(self):
        invalid = types.SimpleNamespace(enabled=True, pattern="(", resolution=4096)
        valid = types.SimpleNamespace(enabled=True, pattern=r"^Mat", resolution=1024)

        result, reports, renders = _run_bake_execute(["Material"], [invalid, valid])

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(renders[0][3], 1024)
        self.assertTrue(
            any("正则表达式无效" in message for levels, message in reports if "WARNING" in levels)
        )

    def _build_bake_like_tree(self):
        tree = _FakeNodeTree()
        tex = tree.add_node("TexImage", outputs=["Color"])
        mix = tree.add_node("MixShader", inputs=["Shader1", "Shader2"], outputs=["Shader"])
        out = tree.add_node("Output", inputs=["Surface"])
        tree.add_link(tex, "Color", mix, "Shader1")
        tree.add_link(mix, "Shader", out, "Surface")
        return tree, mix, out

    def test_restore_node_tree_recovers_original_topology(self):
        tree, mix, out = self._build_bake_like_tree()
        saved_names = {n.name for n in tree.nodes}
        saved_specs = tt_color_bake._snapshot_link_specs(tree)

        # 模拟烘焙过程：删除原始连线（其 Python 包装此后即悬垂，不可再访问），
        # 并插入 ShaderToRGB + Emission 临时节点与连线
        tree.links.remove(tree.links[-1])
        s2rgb = tree.add_node("ShaderToRGB", inputs=["Shader"], outputs=["Color", "Alpha"])
        emission = tree.add_node("Emission", inputs=["Color"], outputs=["Emission"])
        tree.add_link(mix, "Shader", s2rgb, "Shader")
        tree.add_link(s2rgb, "Color", emission, "Color")
        tree.add_link(emission, "Emission", out, "Surface")

        tt_color_bake._restore_node_tree(tree, saved_names, saved_specs)

        self.assertEqual({n.name for n in tree.nodes}, saved_names)
        self.assertEqual(tt_color_bake._snapshot_link_specs(tree), saved_specs)

    def test_restore_node_tree_recreates_link_as_fresh_object(self):
        # 回归：批量烘焙闪退（EXCEPTION_ACCESS_VIOLATION @ NodeLink_from_socket_get）——
        # 旧实现持有被 links.remove() 释放的连线包装对象，再读 from_socket 访问已释放内存。
        # 恢复后必须按规格重建全新的连线对象，绝不复用悬垂引用。
        tree, mix, out = self._build_bake_like_tree()
        saved_names = {n.name for n in tree.nodes}
        saved_specs = tt_color_bake._snapshot_link_specs(tree)

        doomed = tree.links[-1]
        tree.links.remove(doomed)

        tt_color_bake._restore_node_tree(tree, saved_names, saved_specs)

        self.assertEqual(len(tree.links), 2)
        restored = next(
            link for link in tree.links
            if link.from_node.name == "MixShader" and link.to_node.name == "Output"
        )
        self.assertIsNot(restored, doomed)
        self.assertEqual(restored.from_socket.identifier, "Shader")
        self.assertEqual(restored.to_socket.identifier, "Surface")

    def test_configure_alpha_pass_scene_uses_opaque_black_background(self):
        temp_scene = types.SimpleNamespace(
            render=types.SimpleNamespace(film_transparent=True),
            world=object(),
        )

        tt_color_bake._configure_alpha_pass_scene(temp_scene)

        # 回归：透明胶片下 PNG 预乘→直通转换会把轮廓抗锯齿边缘还原成全白，
        # 读回后 alpha 变成 1.0，在 UV 岛外形成一圈不透明亮环；必须是不透明黑底
        self.assertFalse(temp_scene.render.film_transparent)
        self.assertIsNone(temp_scene.world)


if __name__ == "__main__":
    unittest.main()
