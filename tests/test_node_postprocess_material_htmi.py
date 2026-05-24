# -*- coding: utf-8 -*-
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from collections import OrderedDict
from pathlib import Path


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_node_postprocess_material_htmi_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.utils", f"{PKG}.common"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeBpyDataObjects(dict):
    pass


_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(PropertyGroup=object, Operator=object),
    props=types.SimpleNamespace(
        StringProperty=lambda **_kwargs: None,
        BoolProperty=lambda **_kwargs: None,
        IntProperty=lambda **_kwargs: None,
        CollectionProperty=lambda **_kwargs: None,
    ),
    data=types.SimpleNamespace(objects=_FakeBpyDataObjects()),
    path=types.SimpleNamespace(abspath=lambda value: value),
    utils=types.SimpleNamespace(register_class=lambda _cls: None, unregister_class=lambda _cls: None),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module(f"{PKG}.blueprint.node_postprocess_base", SSMTNode_PostProcess_Base=object)
_install_module(
    f"{PKG}.utils.log_utils",
    LOG=types.SimpleNamespace(
        debug=lambda *_args, **_kwargs: None,
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
    ),
)
_prefix_cache_state = {"props": {}}
_install_module(
    f"{PKG}.ui",
)
_install_module(
    f"{PKG}.ui.ntmi_modimp",
)
_install_module(
    f"{PKG}.ui.ntmi_modimp.prefix_property_cache",
    get_prefix_record_props=lambda _name: dict(_prefix_cache_state["props"]),
)
_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(logic_name="HTMI"),
)
_install_module(
    f"{PKG}.common.logic_name",
    LogicName=types.SimpleNamespace(NTEMI="NTEMI"),
)


module_path = Path(__file__).resolve().parents[1] / "blueprint" / "node_postprocess_material.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.node_postprocess_material", module_path)
node_postprocess_material = importlib.util.module_from_spec(spec)
sys.modules[f"{PKG}.blueprint.node_postprocess_material"] = node_postprocess_material
spec.loader.exec_module(node_postprocess_material)


class _FakeImage:
    def __init__(self, filepath):
        self.filepath = filepath
        self.name = os.path.basename(filepath)


class _FakeTextureNode:
    type = "TEX_IMAGE"

    def __init__(self, filepath):
        self.image = _FakeImage(filepath)


class _FakeNodeTree:
    def __init__(self, texture_path):
        self.nodes = [_FakeTextureNode(texture_path)] if texture_path else []


class _FakeMaterial:
    def __init__(self, name="ImportedObject_Material", texture_path=""):
        self.name = name
        self.use_nodes = bool(texture_path)
        self.node_tree = _FakeNodeTree(texture_path) if texture_path else None


class _FakeMaterialSlot:
    def __init__(self, material):
        self.material = material


class _FakeObject(dict):
    def __init__(self, name, texture_slots, material_names=None, original_object_name=""):
        super().__init__()
        self.name = name
        self.original_object_name = original_object_name
        material_names = material_names or ["ImportedObject_Material"]
        self.material_slots = []
        for material_spec in material_names:
            if isinstance(material_spec, tuple):
                material_name, texture_path = material_spec
            else:
                material_name, texture_path = material_spec, ""
            self.material_slots.append(_FakeMaterialSlot(_FakeMaterial(material_name, texture_path)))
        self["modimp_texture_slots"] = json.dumps(texture_slots)


class HTMIMaterialPostProcessTests(unittest.TestCase):
    def setUp(self):
        _fake_bpy.data.objects.clear()
        node_postprocess_material.clear_name_mapping_cache()
        _prefix_cache_state["props"] = {}
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "HTMI"

    def test_material_detect_accepts_ntmi_modimp_result_output(self):
        class _FakeSocket:
            bl_idname = "SSMTSocketPostProcess"

            def __init__(self, linked_node=None):
                self.is_linked = linked_node is not None
                self.links = []
                if linked_node is not None:
                    self.links.append(types.SimpleNamespace(from_node=linked_node))

        class _FakeNode(dict):
            def __init__(self, name, bl_idname, inputs=None):
                super().__init__()
                self.name = name
                self.bl_idname = bl_idname
                self.inputs = inputs or []

        object_info = _FakeNode("ObjectInfo", "SSMTNode_Object_Info")
        object_info.object_name = "LOD0.fd054d1d-30030-0.Body"
        result_output = _FakeNode("ResultOutput", "SSMTNode_Result_Output_NTMIModImp", [_FakeSocket(object_info)])
        material_node = node_postprocess_material.SSMT_OT_MaterialDetect()

        self.assertIs(material_node._find_result_output(result_output), result_output)

    def test_htmi_texture_slots_drive_ps_resources_and_only_fxmap_material_drives_ntemifx(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = os.path.join(temp_dir, "sources")
            material_dir = os.path.join(temp_dir, "material_nodes")
            os.makedirs(source_dir)
            os.makedirs(material_dir)
            source_paths = {}
            material_paths = {}
            source_filenames = {
                "DiffuseMap": "ae1ab184_6270_157770-t0-43d1e1eb.dds",
                "LightMap": "ae1ab184_6270_157770-t1-43d1e1eb.dds",
                "FXMap": "ae1ab184_6270_157770-t2-43d1e1eb.dds",
            }
            for mark_name, filename in source_filenames.items():
                path = os.path.join(source_dir, filename)
                with open(path, "wb") as file_obj:
                    file_obj.write(f"slot-{mark_name}".encode("ascii"))
                source_paths[mark_name] = path
                if mark_name == "DiffuseMap":
                    material_filename = "DiffuseMap_衣服00.png"
                else:
                    material_filename = f"material-{mark_name}.dds"
                material_path = os.path.join(material_dir, material_filename)
                with open(material_path, "wb") as file_obj:
                    file_obj.write(f"material-{mark_name}".encode("ascii"))
                material_paths[mark_name] = material_path

            texture_slots = {
                "ps-t0": {
                    "source_path": source_paths["DiffuseMap"],
                    "mark_name": "DiffuseMap",
                    "mark_type": "Slot",
                    "mark_filename": "fd054d1d-30030-0-DiffuseMap.dds",
                },
                "ps-t1": {
                    "source_path": source_paths["LightMap"],
                    "mark_name": "LightMap",
                    "mark_type": "Slot",
                    "mark_filename": "fd054d1d-30030-0-LightMap.dds",
                },
                "ps-t2": {
                    "source_path": source_paths["FXMap"],
                    "mark_name": "FXMap",
                    "mark_type": "Slot",
                    "mark_filename": "fd054d1d-30030-0-FXMap.dds",
                },
            }
            obj = _FakeObject(
                "LOD0.fd054d1d-30030-0.Body",
                texture_slots,
                [
                    ("DiffuseMap_琛ｆ湇00", material_paths["DiffuseMap"]),
                    ("LightMap_Body", material_paths["LightMap"]),
                    ("FXMap_Body", material_paths["FXMap"]),
                ],
            )
            _fake_bpy.data.objects[obj.name] = obj

            sections = OrderedDict(
                [
                    (
                        "[TextureOverride_Test]",
                        [
                            f"[mesh:{obj.name}]",
                            "hash = fd054d1d",
                            "ps-t0 = Resource-old-DiffuseMap",
                            "ps-t1 = Resource-old-LightMap",
                            "ps-t2 = Resource-old-FXMap",
                            "drawindexed = 3, 0, 0",
                        ],
                    ),
                    ("_config_path", temp_dir),
                ]
            )

            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"

            node.process_texture_override_section(
                "[TextureOverride_Test]",
                sections,
                material_group_to_swapkey={},
                swap_key_prefix="$swapkey",
                next_swap_key_num=150,
                used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )

            override_lines = sections["[TextureOverride_Test]"]
            self.assertIn("ps-t0 = Resource_DiffuseMap_琛ｆ湇00", override_lines)
            self.assertIn("ps-t1 = Resource_LightMap_Body", override_lines)
            self.assertNotIn("ps-t2 = Resource-FXMap_Body", override_lines)
            self.assertNotIn("ps-t2 = ResourceTexture_FXMap_Body", override_lines)
            self.assertIn("Resource\\RabbitFX\\FXMap = ref Resource_FXMap_Body", override_lines)
            self.assertNotIn("Resource\\NTEMIFX\\FXMap = ref Resource_DiffuseMap_琛ｆ湇00", override_lines)
            self.assertNotIn("Resource\\NTEMIFX\\FXMap = ref Resource-LightMap_Body", override_lines)

            self.assertEqual(
                sections["[Resource_DiffuseMap_琛ｆ湇00]"],
                ["filename = Textures/DiffuseMap_琛ｆ湇00.png"],
            )
            self.assertEqual(
                sections["[Resource_LightMap_Body]"],
                ["filename = Textures/LightMap_Body.dds"],
            )
            self.assertEqual(
                sections["[Resource_FXMap_Body]"],
                ["filename = Textures/FXMap_Body.dds"],
            )

    def test_ntemi_fxmap_uses_ntemifx_namespace(self):
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "NTEMI"
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = os.path.join(temp_dir, "sources")
            os.makedirs(source_dir)
            fx_path = os.path.join(source_dir, "fx.dds")
            with open(fx_path, "wb") as file_obj:
                file_obj.write(b"fx")

            obj = _FakeObject(
                "GenericMesh",
                [],
                [("FXMap_Generic", fx_path)],
            )
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict(
                [
                    (
                        "[TextureOverride_Generic]",
                        [
                            f"[mesh:{obj.name}]",
                            "hash = 12345678",
                            "drawindexed = 3, 0, 0",
                        ],
                    ),
                    ("_config_path", temp_dir),
                ]
            )

            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"

            node.process_texture_override_section(
                "[TextureOverride_Generic]",
                sections,
                material_group_to_swapkey={},
                swap_key_prefix="$swapkey",
                next_swap_key_num=150,
                used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )

            override_lines = sections["[TextureOverride_Generic]"]
            self.assertIn("Resource\\NTEMIFX\\FXMap = ref Resource_FXMap_Generic", override_lines)
            self.assertIn("run = CommandList\\NTEMIFX\\Run", override_lines)
            self.assertNotIn("Resource\\RabbitFX\\FXMap = ref Resource_FXMap_Generic", override_lines)

    def test_ntemi_fxmap_reset_is_emitted_after_conditional_block(self):
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "NTEMI"
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = os.path.join(temp_dir, "sources")
            os.makedirs(source_dir)
            fx_path = os.path.join(source_dir, "fx.dds")
            with open(fx_path, "wb") as file_obj:
                file_obj.write(b"fx")

            obj = _FakeObject(
                "LOD0.ae1ab184-71202-29187.袜子_copy",
                [],
                [("FXMap_DiffuseMap_袜子", fx_path)],
            )
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict(
                [
                    (
                        "[TextureOverride_Generic]",
                        [
                            f"[mesh:{obj.name}]",
                            "hash = ae1ab184",
                            "if $swapkey9 == 0 && $swapkey1 == 0",
                            "drawindexed = 5676,3090522,0",
                            "endif",
                        ],
                    ),
                    ("_config_path", temp_dir),
                ]
            )

            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"

            node.process_texture_override_section(
                "[TextureOverride_Generic]",
                sections,
                material_group_to_swapkey={},
                swap_key_prefix="$swapkey",
                next_swap_key_num=150,
                used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )

            override_lines = sections["[TextureOverride_Generic]"]
            fx_ref_index = override_lines.index("Resource\\NTEMIFX\\FXMap = ref Resource_FXMap_DiffuseMap_袜子")
            first_run_index = override_lines.index("run = CommandList\\NTEMIFX\\Run")
            if_index = override_lines.index("if $swapkey9 == 0 && $swapkey1 == 0")
            draw_index = override_lines.index("drawindexed = 5676,3090522,0")
            endif_index = override_lines.index("endif")
            reset_ref_index = override_lines.index("Resource\\NTEMIFX\\FXMap = ref null")
            reset_run_index = len(override_lines) - 1 - override_lines[::-1].index("run = CommandList\\NTEMIFX\\Run")

            self.assertLess(fx_ref_index, if_index)
            self.assertLess(first_run_index, if_index)
            self.assertLess(if_index, draw_index)
            self.assertLess(draw_index, endif_index)
            self.assertLess(endif_index, reset_ref_index)
            self.assertLess(reset_ref_index, reset_run_index)




    def test_htmi_fx_slot_without_fxmap_prefix_material_does_not_write_ntemifx(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = os.path.join(temp_dir, "ae1ab184_6270_157770-t5-43d1e1eb.dds")
            with open(source_path, "wb") as file_obj:
                file_obj.write(b"fx")
            material_path = os.path.join(temp_dir, "material-fx.dds")
            with open(material_path, "wb") as file_obj:
                file_obj.write(b"material-fx")

            texture_slots = {
                "t5": {
                    "source_path": source_path,
                    "mark_name": "FXMap",
                    "mark_type": "Slot",
                    "mark_filename": "fd054d1d-30030-0-FXMap.dds",
                },
            }
            obj = _FakeObject(
                "LOD0.fd054d1d-30030-0.Body",
                texture_slots,
                [("FXMap", material_path)],
            )
            _fake_bpy.data.objects[obj.name] = obj

            sections = OrderedDict(
                [
                    (
                        "[TextureOverride_Test]",
                        [
                            f"[mesh:{obj.name}]",
                            "hash = fd054d1d",
                            "ps-t5 = Resource-old-FXMap",
                            "drawindexed = 3, 0, 0",
                        ],
                    ),
                    ("_config_path", temp_dir),
                ]
            )

            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"

            node.process_texture_override_section(
                "[TextureOverride_Test]",
                sections,
                material_group_to_swapkey={},
                swap_key_prefix="$swapkey",
                next_swap_key_num=150,
                used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )

            override_lines = sections["[TextureOverride_Test]"]
            self.assertNotIn("ps-t5 = Resource-old-FXMap", override_lines)

    def test_htmi_texture_slot_can_be_inferred_from_workspace_filename(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = os.path.join(temp_dir, "ae1ab184_6270_157770-t5-43d1e1eb.dds")
            with open(source_path, "wb") as file_obj:
                file_obj.write(b"slot-diffuse")
            material_path = os.path.join(temp_dir, "material-diffuse.dds")
            with open(material_path, "wb") as file_obj:
                file_obj.write(b"material-diffuse")

            texture_slots = {
                "": {
                    "mark_name": "DiffuseMap",
                    "mark_type": "Slot",
                    "mark_filename": os.path.basename(source_path),
                },
            }
            obj = _FakeObject("LOD0.fd054d1d-30030-0.Body", texture_slots, [("DiffuseMap_Body", material_path)])
            _fake_bpy.data.objects[obj.name] = obj

            sections = OrderedDict(
                [
                    (
                        "[TextureOverride_Test]",
                        [
                            f"[mesh:{obj.name}]",
                            "hash = fd054d1d",
                            "ps-t5 = Resource-old-DiffuseMap",
                            "drawindexed = 3, 0, 0",
                        ],
                    ),
                    ("_config_path", temp_dir),
                ]
            )

            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"

            node.process_texture_override_section(
                "[TextureOverride_Test]",
                sections,
                material_group_to_swapkey={},
                swap_key_prefix="$swapkey",
                next_swap_key_num=150,
                used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )

            override_lines = sections["[TextureOverride_Test]"]
            self.assertIn("ps-t5 = Resource_DiffuseMap_Body", override_lines)
            self.assertEqual(
                sections["[Resource_DiffuseMap_Body]"],
                ["filename = Textures/DiffuseMap_Body.dds"],
            )
            self.assertTrue(
                os.path.isfile(os.path.join(temp_dir, "Textures", "DiffuseMap_Body.dds"))
            )
            with open(os.path.join(temp_dir, "Textures", "DiffuseMap_Body.dds"), "rb") as file_obj:
                self.assertEqual(file_obj.read(), b"material-diffuse")

    def test_htmi_uses_each_object_own_material_texture_for_many_objects(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sections = OrderedDict()
            section_lines = []
            texture_slots = {
                "ps-t0": {
                    "mark_name": "DiffuseMap",
                    "mark_type": "Slot",
                    "mark_slot": "ps-t0",
                },
            }

            for index in range(30):
                obj_name = f"LOD0.ae1ab184-{29187 + index}-0.Part{index:02d}"
                material_name = f"DiffuseMap_Part{index:02d}"
                texture_path = os.path.join(temp_dir, f"source-{index:02d}.png")
                with open(texture_path, "wb") as file_obj:
                    file_obj.write(f"material-{index:02d}".encode("ascii"))
                obj = _FakeObject(obj_name, texture_slots, [(material_name, texture_path)])
                _fake_bpy.data.objects[obj.name] = obj
                section_lines.extend(
                    [
                        f"[mesh:{obj.name}]",
                        f"hash = {index:08x}",
                        "ps-t0 = Resource-old-DiffuseMap",
                        "drawindexed = 3, 0, 0",
                    ]
                )

            sections["[TextureOverride_Test]"] = section_lines
            sections["_config_path"] = temp_dir

            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"

            node.process_texture_override_section(
                "[TextureOverride_Test]",
                sections,
                material_group_to_swapkey={},
                swap_key_prefix="$swapkey",
                next_swap_key_num=150,
                used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )

            override_lines = sections["[TextureOverride_Test]"]
            for index in range(30):
                material_name = f"DiffuseMap_Part{index:02d}"
                self.assertIn(f"ps-t0 = Resource_{material_name}", override_lines)
                self.assertEqual(
                    sections[f"[Resource_{material_name}]"],
                    [f"filename = Textures/{material_name}.png"],
                )
                copied_path = os.path.join(temp_dir, "Textures", f"{material_name}.png")
                self.assertTrue(os.path.isfile(copied_path))
                with open(copied_path, "rb") as file_obj:
                    self.assertEqual(file_obj.read(), f"material-{index:02d}".encode("ascii"))

    def test_non_htmi_ps_material_to_resource_still_uses_generic_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            texture_path = os.path.join(temp_dir, "generic-diffuse.png")
            with open(texture_path, "wb") as file_obj:
                file_obj.write(b"generic")

            obj = _FakeObject(
                "GenericMesh",
                {},
                [("DiffuseMap_Generic", texture_path)],
            )
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict(
                [
                    (
                        "[TextureOverride_Generic]",
                        [
                            f"[mesh:{obj.name}]",
                            "hash = 12345678",
                            "ps-t0 = Resource-old-DiffuseMap",
                            "drawindexed = 3, 0, 0",
                        ],
                    ),
                    ("_config_path", temp_dir),
                ]
            )

            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"

            node.process_texture_override_section(
                "[TextureOverride_Generic]",
                sections,
                material_group_to_swapkey={},
                swap_key_prefix="$swapkey",
                next_swap_key_num=150,
                used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )

            override_lines = sections["[TextureOverride_Generic]"]
            self.assertIn("ps-t0 = ResourceTexture_DiffuseMap_Generic", override_lines)
            self.assertEqual(
                sections["[ResourceTexture_DiffuseMap_Generic]"],
                ["filename = Textures/DiffuseMap_Generic.png"],
            )
            self.assertTrue(os.path.isfile(os.path.join(temp_dir, "Textures", "DiffuseMap_Generic.png")))

    def test_non_htmi_generic_path_still_handles_rabbitfx_and_zzmi_refs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diffuse_path = os.path.join(temp_dir, "diffuse.png")
            glow_path = os.path.join(temp_dir, "glow.png")
            for path in (diffuse_path, glow_path):
                with open(path, "wb") as file_obj:
                    file_obj.write(os.path.basename(path).encode("ascii"))

            obj = _FakeObject(
                "GenericMesh",
                {},
                [
                    ("DiffuseMap_Generic", diffuse_path),
                    ("Glowmap_5_Generic", glow_path),
                ],
            )
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict(
                [
                    (
                        "[TextureOverride_Generic]",
                        [
                            f"[mesh:{obj.name}]",
                            "hash = 12345678",
                            "ps-t0 = Resource-old-DiffuseMap",
                            "Resource\\RabbitFX\\Glowmap = ref Resource-old-Glowmap",
                            "Resource\\ZZMI\\DiffuseMap = ref Resource-old-DiffuseMap",
                            "drawindexed = 3, 0, 0",
                        ],
                    ),
                    ("_config_path", temp_dir),
                ]
            )

            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"

            node.process_texture_override_section(
                "[TextureOverride_Generic]",
                sections,
                material_group_to_swapkey={},
                swap_key_prefix="$swapkey",
                next_swap_key_num=150,
                used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )

            override_lines = sections["[TextureOverride_Generic]"]
            self.assertIn("ps-t0 = ResourceTexture_DiffuseMap_Generic", override_lines)
            self.assertIn("Resource\\RabbitFX\\Glowmap = ref Resource_Glowmap_5_Generic", override_lines)
            self.assertIn("Resource\\ZZMI\\DiffuseMap = ref Resource_DiffuseMap_Generic", override_lines)
            self.assertEqual(
                sections["[ResourceTexture_DiffuseMap_Generic]"],
                ["filename = Textures/DiffuseMap_Generic.png"],
            )
            self.assertEqual(
                sections["[Resource_Glowmap_5_Generic]"],
                ["filename = Textures/Glowmap_5_Generic.png"],
            )
            self.assertEqual(
                sections["[Resource_DiffuseMap_Generic]"],
                ["filename = Textures/DiffuseMap_Generic.png"],
            )

    def test_htmi_uses_current_object_material_before_source_candidate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_texture = os.path.join(temp_dir, "source-candidate.png")
            current_texture = os.path.join(temp_dir, "current-object.png")
            with open(source_texture, "wb") as file_obj:
                file_obj.write(b"wrong-source")
            with open(current_texture, "wb") as file_obj:
                file_obj.write(b"correct-current")

            texture_slots = {
                "ps-t0": {
                    "mark_name": "DiffuseMap",
                    "mark_type": "Slot",
                    "mark_slot": "ps-t0",
                },
            }
            source_obj = _FakeObject(
                "SharedSource",
                {},
                [("DiffuseMap_Source", source_texture)],
            )
            obj = _FakeObject(
                "LOD0.ae1ab184-29187-0.Current",
                texture_slots,
                [("DiffuseMap_Current", current_texture)],
                original_object_name="SharedSource",
            )
            _fake_bpy.data.objects[source_obj.name] = source_obj
            _fake_bpy.data.objects[obj.name] = obj

            sections = OrderedDict(
                [
                    (
                        "[TextureOverride_Test]",
                        [
                            f"[mesh:{obj.name}]",
                            "hash = ae1ab184",
                            "ps-t0 = Resource-old-DiffuseMap",
                            "drawindexed = 3, 0, 0",
                        ],
                    ),
                    ("_config_path", temp_dir),
                ]
            )

            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"

            node.process_texture_override_section(
                "[TextureOverride_Test]",
                sections,
                material_group_to_swapkey={},
                swap_key_prefix="$swapkey",
                next_swap_key_num=150,
                used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )

            override_lines = sections["[TextureOverride_Test]"]
            self.assertIn("ps-t0 = Resource_DiffuseMap_Current", override_lines)
            self.assertNotIn("ps-t0 = Resource_DiffuseMap_Source", override_lines)
            self.assertEqual(
                sections["[Resource_DiffuseMap_Current]"],
                ["filename = Textures/DiffuseMap_Current.png"],
            )
            with open(os.path.join(temp_dir, "Textures", "DiffuseMap_Current.png"), "rb") as file_obj:
                self.assertEqual(file_obj.read(), b"correct-current")

    def test_htmi_workspace_slot_multiple_materials_reuse_existing_if_switch_rules(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            obj_name = "LOD0.fd054d1d-30030-0.Body"
            slot_texture_a = os.path.join(temp_dir, "diffuse-a.png")
            slot_texture_b = os.path.join(temp_dir, "diffuse-b.png")
            for path, payload in ((slot_texture_a, b"a"), (slot_texture_b, b"b")):
                with open(path, "wb") as file_obj:
                    file_obj.write(payload)

            obj = _FakeObject(
                obj_name,
                {
                    "ps-t0": {
                        "source_path": slot_texture_a,
                        "mark_name": "DiffuseMap",
                        "mark_type": "Slot",
                        "mark_slot": "ps-t0",
                        "mark_filename": "fd054d1d-30030-0-DiffuseMap.dds",
                    },
                },
                [
                    ("DiffuseMap_Body_A", slot_texture_a),
                    ("DiffuseMap_Body_B", slot_texture_b),
                ],
            )
            _fake_bpy.data.objects[obj.name] = obj

            sections = OrderedDict(
                [
                    (
                        "[TextureOverride_Test]",
                        [
                            f"[mesh:{obj.name}]",
                            "hash = fd054d1d",
                            "ps-t0 = Resource-old-DiffuseMap",
                            "drawindexed = 3, 0, 0",
                        ],
                    ),
                    ("_config_path", temp_dir),
                ]
            )

            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"

            node.process_texture_override_section(
                "[TextureOverride_Test]",
                sections,
                material_group_to_swapkey={},
                swap_key_prefix="$swapkey",
                next_swap_key_num=150,
                used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )

            override_lines = sections["[TextureOverride_Test]"]
            joined = "\n".join(override_lines)
            self.assertIn("if $swapkey150 == 0", joined)
            self.assertIn("if $swapkey150 == 1", joined)
            self.assertIn("ps-t0 = Resource_DiffuseMap_Body_A", joined)
            self.assertIn("ps-t0 = Resource_DiffuseMap_Body_B", joined)

    def test_material_resource_names_replace_spaces_with_underscores(self):
        material = _FakeMaterial("Highlight Metal 12261 01", os.path.join("/tmp", "tex.png"))
        node = node_postprocess_material.SSMTNode_PostProcess_Material()
        self.assertEqual(node._workspace_material_resource_name(material), "Resource-Highlight_Metal_12261_01")
        self.assertEqual(node._ps_texture_material_resource_name(material), "ResourceTexture_Highlight_Metal_12261_01")

    def test_generic_resource_entry_replaces_spaces_with_underscores(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            texture_path = os.path.join(temp_dir, "tex with spaces.png")
            with open(texture_path, "wb") as file_obj:
                file_obj.write(b"generic")

            obj = _FakeObject(
                "GenericMesh",
                {},
                [("LightMap_kk metal 12261 01", texture_path)],
            )
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict(
                [
                    (
                        "[TextureOverride_Generic]",
                        [
                            f"[mesh:{obj.name}]",
                            "hash = 12345678",
                            "ps-t8 = Resource-old-LightMap",
                            "drawindexed = 3, 0, 0",
                        ],
                    ),
                    ("_config_path", temp_dir),
                ]
            )

            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"

            node.process_texture_override_section(
                "[TextureOverride_Generic]",
                sections,
                material_group_to_swapkey={},
                swap_key_prefix="$swapkey",
                next_swap_key_num=150,
                used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )

            override_lines = sections["[TextureOverride_Generic]"]
            self.assertIn("ps-t8 = ResourceTexture_LightMap_kk_metal_12261_01", override_lines)

    def test_htmi_prefers_prefix_cache_texture_slots_over_stale_object_prop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diffuse_path = os.path.join(temp_dir, "diffuse.png")
            light_path = os.path.join(temp_dir, "light.png")
            for path, payload in ((diffuse_path, b"diffuse"), (light_path, b"light")):
                with open(path, "wb") as file_obj:
                    file_obj.write(payload)

            obj_name = "LOD0.fd054d1d-30030-0.Body"
            obj = _FakeObject(
                obj_name,
                {
                    "ps-t0": {
                        "mark_name": "DiffuseMap",
                        "mark_type": "Slot",
                        "mark_slot": "ps-t0",
                        "mark_filename": "stale-DiffuseMap.dds",
                    },
                },
                [
                    ("DiffuseMap_Body", diffuse_path),
                    ("LightMap_Body", light_path),
                ],
            )
            _prefix_cache_state["props"] = {
                "modimp_profile_id": "yihuan",
                "modimp_workspace_unique_str": "LOD0.fd054d1d-30030-0",
                "modimp_texture_slots": json.dumps(
                    {
                        "ps-t0": {
                            "mark_name": "DiffuseMap",
                            "mark_type": "Slot",
                            "mark_slot": "ps-t0",
                            "mark_filename": "fresh-DiffuseMap.dds",
                        },
                        "ps-t1": {
                            "mark_name": "LightMap",
                            "mark_type": "Slot",
                            "mark_slot": "ps-t1",
                            "mark_filename": "fresh-LightMap.dds",
                        },
                    }
                ),
            }
            _fake_bpy.data.objects[obj.name] = obj

            sections = OrderedDict(
                [
                    (
                        "[TextureOverride_Test]",
                        [
                            f"[mesh:{obj.name}]",
                            "hash = fd054d1d",
                            "ps-t0 = Resource-old-DiffuseMap",
                            "ps-t1 = Resource-old-LightMap",
                            "drawindexed = 3, 0, 0",
                        ],
                    ),
                    ("_config_path", temp_dir),
                ]
            )

            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"

            node.process_texture_override_section(
                "[TextureOverride_Test]",
                sections,
                material_group_to_swapkey={},
                swap_key_prefix="$swapkey",
                next_swap_key_num=150,
                used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )

            override_lines = sections["[TextureOverride_Test]"]
            self.assertIn("ps-t0 = Resource_DiffuseMap_Body", override_lines)
            self.assertIn("ps-t1 = Resource_LightMap_Body", override_lines)


if __name__ == "__main__":
    unittest.main()





