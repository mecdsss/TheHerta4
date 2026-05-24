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


PKG = "_ntemi_importer_materials_test_pkg"
for package_name in (PKG, f"{PKG}.ui", f"{PKG}.ui.ntmi_modimp"):
    package = _install_module(package_name)
    package.__path__ = []
common_package = _install_module(f"{PKG}.common")
common_package.__path__ = []


class _FakeImage:
    def __init__(self, filepath):
        self.filepath = filepath
        self.name = os.path.basename(filepath)
        self.colorspace_settings = types.SimpleNamespace(is_data=False, name="")
        self.alpha_mode = ""
        self.depth = 24


class _FakeImageCollection:
    def load(self, filepath):
        return _FakeImage(filepath)


class _FakeSocket:
    pass


class _FakeSocketMap(dict):
    def __missing__(self, key):
        socket = _FakeSocket()
        self[key] = socket
        return socket


class _FakeNode:
    def __init__(self, bl_idname):
        self.bl_idname = bl_idname
        self.type = {
            "ShaderNodeBsdfPrincipled": "BSDF_PRINCIPLED",
            "ShaderNodeOutputMaterial": "OUTPUT_MATERIAL",
            "ShaderNodeTexImage": "TEX_IMAGE",
            "ShaderNodeBsdfTransparent": "BSDF_TRANSPARENT",
            "ShaderNodeBsdfDiffuse": "BSDF_DIFFUSE",
            "ShaderNodeMixShader": "MIX_SHADER",
            "ShaderNodeNormalMap": "NORMAL_MAP",
        }.get(bl_idname, bl_idname)
        self.inputs = _FakeSocketMap()
        self.outputs = _FakeSocketMap()
        self.location = types.SimpleNamespace(x=0, y=0)
        self.image = None


class _FakeNodeList(list):
    def new(self, type):
        node = _FakeNode(type)
        self.append(node)
        return node

    def remove(self, node):
        list.remove(self, node)


class _FakeLinks:
    def __init__(self):
        self.created = []

    def new(self, input_socket, output_socket):
        self.created.append((input_socket, output_socket))


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


class _FakeObjectData:
    def __init__(self, owner):
        self.materials = self
        self._owner = owner

    def append(self, material):
        self._owner.material_slots.append(types.SimpleNamespace(material=material))


class _FakeObject:
    def __init__(self, name):
        self.name = name
        self.material_slots = []
        self.data = _FakeObjectData(self)


_fake_bpy = types.SimpleNamespace(
    data=types.SimpleNamespace(
        materials=_FakeMaterialCollection(),
        images=_FakeImageCollection(),
    )
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module(
    f"{PKG}.ui.ntmi_modimp.runtime_cache",
    MODIMP_COLLECTOR_PROPS=(),
    MODIMP_PATH_PROPS=(),
    localize_runtime_path_props=lambda path_props, _object_workspace_dir: dict(path_props),
    object_workspace_dir_from_type_dir=lambda type_dir: type_dir,
    object_workspace_dir_from_unique=lambda workspace_root, workspace_unique_str: os.path.join(workspace_root, workspace_unique_str),
)
_install_module(
    f"{PKG}.ui.ntmi_modimp.modimp_core",
    ensure_mod_importer_package=lambda _configured_root="": None,
    resolve_mod_importer_root=lambda: "",
)
_install_module(
    f"{PKG}.ui.ntmi_modimp.prefix_property_cache",
    update_prefix_record_for_object=lambda *_args, **_kwargs: None,
)
_install_module(
    f"{PKG}.ui.ntmi_modimp.texture_slot_refresh",
    build_texture_slots_from_workspace_unique=lambda _workspace_unique_str="": {},
)
_install_module(
    f"{PKG}.common.import_scene_settings",
    apply_import_render_environment=lambda *_args, **_kwargs: None,
)


module_path = Path(__file__).resolve().parents[1] / "ui" / "ntmi_modimp" / "ntemi_importer.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.ui.ntmi_modimp.ntemi_importer", module_path)
ntemi_importer = importlib.util.module_from_spec(spec)
sys.modules[f"{PKG}.ui.ntmi_modimp.ntemi_importer"] = ntemi_importer
spec.loader.exec_module(ntemi_importer)


class NTEMIImporterMaterialTests(unittest.TestCase):
    def setUp(self):
        _fake_bpy.data.materials.clear()

    def test_workspace_texture_marks_only_create_diffuse_material(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diffuse = os.path.join(temp_dir, "diffuse.dds")
            light = os.path.join(temp_dir, "light.dds")
            for path in (diffuse, light):
                with open(path, "wb") as file_obj:
                    file_obj.write(b"texture")

            obj = _FakeObject("LOD0.fd054d1d-30030-0.Body")

            ntemi_importer._apply_material_from_texture_slots(
                obj,
                {
                    "ps-t0": {
                        "source_path": diffuse,
                        "mark_name": "DiffuseMap",
                    },
                    "ps-t1": {
                        "source_path": light,
                        "mark_name": "LightMap",
                    },
                },
            )

            material_names = [slot.material.name for slot in obj.material_slots]
            self.assertIn("DiffuseMap_LOD0.fd054d1d-30030-0.Body_ps-t0", material_names)
            self.assertNotIn("LightMap_LOD0.fd054d1d-30030-0.Body_ps-t1", material_names)
            self.assertEqual(len(material_names), 1)
            material = obj.material_slots[0].material
            node_types = [node.bl_idname for node in material.node_tree.nodes]
            self.assertIn("ShaderNodeTexImage", node_types)
            self.assertIn("ShaderNodeBsdfTransparent", node_types)
            self.assertIn("ShaderNodeBsdfDiffuse", node_types)
            self.assertIn("ShaderNodeMixShader", node_types)
            self.assertNotIn("ShaderNodeBsdfPrincipled", node_types)
            self.assertEqual(material.blend_method, "BLEND")
            self.assertFalse(material.use_transparency_overlap)
            tex_node = next(node for node in material.node_tree.nodes if node.bl_idname == "ShaderNodeTexImage")
            self.assertEqual(tex_node.image.colorspace_settings.name, "sRGB")
            self.assertEqual(tex_node.image.alpha_mode, "CHANNEL_PACKED")


if __name__ == "__main__":
    unittest.main()
