import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_mesh_create_helper_material_graph_test_pkg"
for package_name in (PKG, f"{PKG}.common", f"{PKG}.utils", f"{PKG}.ui", f"{PKG}.ui.wwmi"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeImage:
    def __init__(self, filepath):
        self.filepath = filepath
        self.name = os.path.basename(filepath)
        self.alpha_mode = ""
        self.colorspace_settings = types.SimpleNamespace(is_data=False, name="")


class _FakeImageCollection:
    def load(self, filepath):
        return _FakeImage(filepath)


class _FakeSocket:
    def __init__(self, name=""):
        self.name = name
        self.default_value = None


class _FakeSocketMap(dict):
    def __missing__(self, key):
        socket = _FakeSocket(str(key))
        self[key] = socket
        return socket


class _FakeNode:
    def __init__(self, bl_idname):
        self.bl_idname = bl_idname
        self.type = {
            "ShaderNodeBsdfPrincipled": "BSDF_PRINCIPLED",
            "ShaderNodeOutputMaterial": "OUTPUT_MATERIAL",
            "ShaderNodeTexImage": "TEX_IMAGE",
            "ShaderNodeBsdfDiffuse": "BSDF_DIFFUSE",
            "ShaderNodeBsdfTransparent": "BSDF_TRANSPARENT",
            "ShaderNodeMixShader": "MIX_SHADER",
            "ShaderNodeNormalMap": "NORMAL_MAP",
            "ShaderNodeSeparateColor": "SEPARATE_COLOR",
            "ShaderNodeRGBCurve": "CURVE_RGB",
            "ShaderNodeCombineColor": "COMBINE_COLOR",
            "ShaderNodeMath": "MATH",
        }.get(bl_idname, bl_idname)
        self.inputs = _FakeSocketMap()
        self.outputs = _FakeSocketMap()
        self.location = types.SimpleNamespace(x=0, y=0)
        self.image = None
        self.name = bl_idname
        self.label = bl_idname
        self.width = 140
        self.uv_map = ""
        self.space = ""
        self.operation = ""
        self.use_clamp = False
        self.is_active_output = self.bl_idname == "ShaderNodeOutputMaterial"
        self.mapping = types.SimpleNamespace(
            initialize=lambda: None,
            update=lambda: None,
            curves=[
                types.SimpleNamespace(points=[types.SimpleNamespace(location=(0.0, 0.0)), types.SimpleNamespace(location=(1.0, 1.0))]),
                types.SimpleNamespace(points=[types.SimpleNamespace(location=(0.0, 0.0)), types.SimpleNamespace(location=(1.0, 1.0))]),
                types.SimpleNamespace(points=[types.SimpleNamespace(location=(0.0, 0.0)), types.SimpleNamespace(location=(1.0, 1.0))]),
            ],
        )


class _FakeNodeList(list):
    def new(self, type):
        node = _FakeNode(type)
        self.append(node)
        return node

    def get(self, name):
        for node in self:
            if getattr(node, "name", None) == name:
                return node
        return None

    def remove(self, node):
        list.remove(self, node)


class _FakeLinks:
    def __init__(self):
        self.created = []

    def new(self, from_socket, to_socket):
        self.created.append((from_socket, to_socket))


class _FakeNodeTree:
    def __init__(self):
        self.nodes = _FakeNodeList()
        self.links = _FakeLinks()


class _FakeMaterial:
    def __init__(self, name):
        self.name = name
        self.use_nodes = False
        self.node_tree = _FakeNodeTree()
        self.blend_method = ""
        self.use_transparency_overlap = True


class _FakeMaterialCollection(dict):
    def new(self, name):
        material = _FakeMaterial(name)
        self[name] = material
        return material


class _FakeObjectDataMaterials(list):
    pass


class _FakeObjectData:
    def __init__(self):
        self.materials = _FakeObjectDataMaterials()


class _FakeObject:
    def __init__(self, name):
        self.name = name
        self.data = _FakeObjectData()


_fake_bpy = types.SimpleNamespace(
    data=types.SimpleNamespace(
        images=_FakeImageCollection(),
        materials=_FakeMaterialCollection(),
    ),
    types=types.SimpleNamespace(Collection=object),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module("bpy_extras", io_utils=types.SimpleNamespace(unpack_list=lambda seq: seq, axis_conversion=lambda **_kwargs: None))
_install_module("bpy_extras.io_utils", unpack_list=lambda seq: seq, axis_conversion=lambda **_kwargs: None)
_install_module(f"{PKG}.utils.format_utils", Fatal=RuntimeError, FormatUtils=types.SimpleNamespace())
_install_module(f"{PKG}.utils.mesh_utils", MeshUtils=types.SimpleNamespace())
_install_module(f"{PKG}.utils.obj_utils", ObjUtils=types.SimpleNamespace())
_install_module(
    f"{PKG}.utils.texture_utils",
    TextureUtils=types.SimpleNamespace(find_texture=lambda prefix, suffix, directory: os.path.join(directory, f"{prefix}{suffix}")),
)
_install_module(f"{PKG}.utils.timer_utils", TimerUtils=types.SimpleNamespace(Start=lambda *_args, **_kwargs: None))
_install_module(f"{PKG}.utils.vertexgroup_utils", VertexGroupUtils=types.SimpleNamespace())
_install_module(f"{PKG}.common.global_config", GlobalConfig=types.SimpleNamespace(logic_name="GIMI"))
_install_module(
    f"{PKG}.common.global_properties",
    GlobalProterties=types.SimpleNamespace(use_normal_map=lambda: False),
)
_install_module(
    f"{PKG}.common.logic_name",
    LogicName=types.SimpleNamespace(
        GIMI="GIMI",
        ZZMI="ZZMI",
        IdentityV="IdentityV",
        NTEMI="NTEMI",
        SnowBreak="SnowBreak",
    ),
)
_install_module(f"{PKG}.common.object_prefix_helper", ObjectPrefixHelper=types.SimpleNamespace(
    extract_prefix_info=lambda name: (name.rsplit(".", 1)[0], ".") if "." in str(name) else None,
    parse_prefix_parts=lambda prefix: {
        "draw_ib": prefix.split("-")[0].split(".")[-1] if prefix else "",
        "component": prefix.split("-")[1] if "-" in str(prefix) else "",
    },
))
_install_module(f"{PKG}.common.d3d11_element", D3D11Element=object)
_install_module(f"{PKG}.ui.wwmi.extracted_object", ExtractedObjectHelper=types.SimpleNamespace())


module_path = Path(__file__).resolve().parents[1] / "common" / "mesh_create_helper.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.common.mesh_create_helper", module_path)
mesh_create_helper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mesh_create_helper
spec.loader.exec_module(mesh_create_helper)


class MeshCreateHelperMaterialGraphTests(unittest.TestCase):
    def setUp(self):
        _fake_bpy.data.materials.clear()

    def test_multiple_logic_names_do_not_create_principled_bsdf(self):
        logic_names = ("GIMI", "ZZMI", "IdentityV", "NTEMI", "SnowBreak")

        with tempfile.TemporaryDirectory() as temp_dir:
            diffuse_path = os.path.join(temp_dir, "abc12345-12-DiffuseMap.dds")
            with open(diffuse_path, "wb") as file_obj:
                file_obj.write(b"diffuse")

            for logic_name in logic_names:
                with self.subTest(logic_name=logic_name):
                    obj = _FakeObject(f"{logic_name}_Mesh")
                    mesh_create_helper.MeshCreateHelper.create_bsdf_with_diffuse_linked(
                        obj=obj,
                        mesh_name="LOD0.abc12345-12-0.Body",
                        directory=temp_dir,
                        logic_name=logic_name,
                    )

                    material = obj.data.materials[0]
                    node_types = [node.bl_idname for node in material.node_tree.nodes]
                    self.assertNotIn("ShaderNodeBsdfPrincipled", node_types)
                    self.assertIn("ShaderNodeBsdfDiffuse", node_types)
                    self.assertIn("ShaderNodeOutputMaterial", node_types)
                    image_nodes = [node for node in material.node_tree.nodes if node.bl_idname == "ShaderNodeTexImage"]
                    self.assertEqual(len(image_nodes), 1)
                    self.assertEqual(image_nodes[0].image.colorspace_settings.name, "sRGB")
                    expected_alpha_mode = "NONE" if logic_name == "IdentityV" else "CHANNEL_PACKED"
                    self.assertEqual(image_nodes[0].image.alpha_mode, expected_alpha_mode)
                    if logic_name == "IdentityV":
                        self.assertEqual(material.blend_method, "OPAQUE")
                        self.assertNotIn("ShaderNodeMixShader", node_types)
                        self.assertNotIn("ShaderNodeBsdfTransparent", node_types)
                    else:
                        self.assertEqual(material.blend_method, "BLEND")
                        self.assertFalse(material.use_transparency_overlap)
                        self.assertIn("ShaderNodeMixShader", node_types)
                        self.assertIn("ShaderNodeBsdfTransparent", node_types)


if __name__ == "__main__":
    unittest.main()
