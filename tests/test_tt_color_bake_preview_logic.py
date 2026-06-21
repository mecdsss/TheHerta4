# -*- coding: utf-8 -*-
import importlib.util
import sys
import types
import unittest
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


if __name__ == "__main__":
    unittest.main()
