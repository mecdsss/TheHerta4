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
    """安装 Fake 模块到 sys.modules"""
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
_install_module(
    f"{PKG}.blueprint.node_postprocess_base",
    SSMTNode_PostProcess_Base=type(
        "_FakePostProcessBase",
        (object,),
        {
            "AUTO_APPENDED_SECTION_MARKERS": (
                "; --- AUTO-APPENDED SLIDER CONTROL PANEL ---",
                "; --- AUTO-APPENDED HEALTH DETECTION MODULE ---",
            ),
            "split_auto_appended_tail_content": classmethod(lambda cls, content: (content, "")),
            "split_anim_driver_block_content": classmethod(lambda cls, content: ("", content)),
        },
    ),
)
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
    has_prefix_record=lambda _name: bool(_prefix_cache_state["props"]),
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
    """测试 HTMI 材质后处理管线：纹理槽解析、资源重定向和 NTEMIFX 处理"""

    def setUp(self):
        """每个测试前清空数据和缓存"""
        _fake_bpy.data.objects.clear()
        node_postprocess_material.clear_name_mapping_cache()
        _prefix_cache_state["props"] = {}
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "HTMI"

    def test_material_detect_accepts_ntmi_modimp_result_output(self):
        """测试材质检测接受 NTMI ModImp 结果输出节点类型"""
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
        """测试 HTMI 纹理槽驱动 PS 资源，仅 FXMap 材质驱动 NTEMIFX"""
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
                "ps-t0": {"source_path": source_paths["DiffuseMap"], "mark_name": "DiffuseMap", "mark_type": "Slot", "mark_filename": "fd054d1d-30030-0-DiffuseMap.dds"},
                "ps-t1": {"source_path": source_paths["LightMap"], "mark_name": "LightMap", "mark_type": "Slot", "mark_filename": "fd054d1d-30030-0-LightMap.dds"},
                "ps-t2": {"source_path": source_paths["FXMap"], "mark_name": "FXMap", "mark_type": "Slot", "mark_filename": "fd054d1d-30030-0-FXMap.dds"},
            }
            obj = _FakeObject("LOD0.fd054d1d-30030-0.Body", texture_slots, [
                ("DiffuseMap_琛ｆ湇00", material_paths["DiffuseMap"]),
                ("LightMap_Body", material_paths["LightMap"]),
                ("FXMap_Body", material_paths["FXMap"]),
            ])
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([
                ("[TextureOverride_Test]", [
                    f"[mesh:{obj.name}]", "hash = fd054d1d",
                    "ps-t0 = Resource-old-DiffuseMap", "ps-t1 = Resource-old-LightMap",
                    "ps-t2 = Resource-old-FXMap", "drawindexed = 3, 0, 0",
                ]),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Test]", sections, material_group_to_swapkey={},
                swap_key_prefix="$swapkey", next_swap_key_num=150, used_swap_keys=set(), transparency_sections_to_add=OrderedDict())
            override_lines = sections["[TextureOverride_Test]"]
            mojibake_token = node_postprocess_material.SSMTNode_PostProcess_Material._latin_token_for_text("琛ｆ湇")
            self.assertIn(f"ps-t0 = Resource_DiffuseMap_{mojibake_token}00", override_lines)
            self.assertIn("ps-t1 = Resource_LightMap_Body", override_lines)
            self.assertIn("Resource\\RabbitFX\\FXMap = ref Resource_FXMap_Body", override_lines)

    def test_ntemi_fxmap_uses_ntemifx_namespace(self):
        """测试 NTEMI 逻辑名称下 FXMap 使用 NTEMIFX 命名空间"""
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "NTEMI"
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = os.path.join(temp_dir, "sources"); os.makedirs(source_dir)
            fx_path = os.path.join(source_dir, "fx.dds")
            with open(fx_path, "wb") as file_obj: file_obj.write(b"fx")
            obj = _FakeObject("GenericMesh", [], [("FXMap_Generic", fx_path)])
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([("[TextureOverride_Generic]", [
                f"[mesh:{obj.name}]", "hash = 12345678", "drawindexed = 3, 0, 0"
            ]), ("_config_path", temp_dir)])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Generic]", sections, material_group_to_swapkey={},
                swap_key_prefix="$swapkey", next_swap_key_num=150, used_swap_keys=set(), transparency_sections_to_add=OrderedDict())
            self.assertIn("Resource\\NTEMIFX\\FXMap = ref Resource_FXMap_Generic", sections["[TextureOverride_Generic]"])
            self.assertIn("run = CommandList\\NTEMIFX\\Run", sections["[TextureOverride_Generic]"])

    def test_ntemi_fxmap_reset_is_emitted_after_conditional_block(self):
        """测试 NTEMI FXMap 在条件块后发出重置指令"""
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "NTEMI"
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = os.path.join(temp_dir, "sources"); os.makedirs(source_dir)
            fx_path = os.path.join(source_dir, "fx.dds")
            with open(fx_path, "wb") as file_obj: file_obj.write(b"fx")
            obj = _FakeObject("LOD0.ae1ab184-71202-29187.袜子_copy", [], [("FXMap_DiffuseMap_袜子", fx_path)])
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([("[TextureOverride_Generic]", [
                f"[mesh:{obj.name}]", "hash = ae1ab184", "if $swapkey9 == 0 && $swapkey1 == 0",
                "drawindexed = 5676,3090522,0", "endif",
            ]), ("_config_path", temp_dir)])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Generic]", sections, material_group_to_swapkey={},
                swap_key_prefix="$swapkey", next_swap_key_num=150, used_swap_keys=set(), transparency_sections_to_add=OrderedDict())
            override_lines = sections["[TextureOverride_Generic]"]
            self.assertIn("Resource\\NTEMIFX\\FXMap = ref null", override_lines)

    def test_htmi_fx_slot_without_fxmap_prefix_material_does_not_write_ntemifx(self):
        """测试 HTMI 中无 FXMap 前缀材质的 FX 槽不写 NTEMIFX 引用"""
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = os.path.join(temp_dir, "ae1ab184_6270_157770-t5-43d1e1eb.dds")
            with open(source_path, "wb") as file_obj: file_obj.write(b"fx")
            material_path = os.path.join(temp_dir, "material-fx.dds")
            with open(material_path, "wb") as file_obj: file_obj.write(b"material-fx")
            texture_slots = {"t5": {"source_path": source_path, "mark_name": "FXMap", "mark_type": "Slot", "mark_filename": "fd054d1d-30030-0-FXMap.dds"}}
            obj = _FakeObject("LOD0.fd054d1d-30030-0.Body", texture_slots, [("FXMap", material_path)])
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([("[TextureOverride_Test]", [f"[mesh:{obj.name}]", "hash = fd054d1d", "ps-t5 = Resource-old-FXMap", "drawindexed = 3, 0, 0"]), ("_config_path", temp_dir)])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Test]", sections, material_group_to_swapkey={},
                swap_key_prefix="$swapkey", next_swap_key_num=150, used_swap_keys=set(), transparency_sections_to_add=OrderedDict())
            self.assertNotIn("ps-t5 = Resource-old-FXMap", sections["[TextureOverride_Test]"])

    def test_htmi_texture_slot_can_be_inferred_from_workspace_filename(self):
        """测试 HTMI 纹理槽可以从工作空间文件名推断"""
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = os.path.join(temp_dir, "ae1ab184_6270_157770-t5-43d1e1eb.dds")
            with open(source_path, "wb") as file_obj: file_obj.write(b"slot-diffuse")
            material_path = os.path.join(temp_dir, "material-diffuse.dds")
            with open(material_path, "wb") as file_obj: file_obj.write(b"material-diffuse")
            texture_slots = {"": {"mark_name": "DiffuseMap", "mark_type": "Slot", "mark_filename": os.path.basename(source_path)}}
            obj = _FakeObject("LOD0.fd054d1d-30030-0.Body", texture_slots, [("DiffuseMap_Body", material_path)])
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([("[TextureOverride_Test]", [f"[mesh:{obj.name}]", "hash = fd054d1d", "ps-t5 = Resource-old-DiffuseMap", "drawindexed = 3, 0, 0"]), ("_config_path", temp_dir)])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Test]", sections, ...)
            self.assertIn("ps-t5 = Resource_DiffuseMap_Body", sections["[TextureOverride_Test]"])

    def test_htmi_uses_each_object_own_material_texture_for_many_objects(self):
        """测试 HTMI 对多个对象分别使用各自的材质纹理"""
        with tempfile.TemporaryDirectory() as temp_dir:
            sections = OrderedDict(); section_lines = []
            texture_slots = {"ps-t0": {"mark_name": "DiffuseMap", "mark_type": "Slot", "mark_slot": "ps-t0"}}
            for index in range(30):
                obj_name = f"LOD0.ae1ab184-{29187 + index}-0.Part{index:02d}"
                material_name = f"DiffuseMap_Part{index:02d}"
                texture_path = os.path.join(temp_dir, f"source-{index:02d}.png")
                with open(texture_path, "wb") as file_obj: file_obj.write(f"material-{index:02d}".encode("ascii"))
                obj = _FakeObject(obj_name, texture_slots, [(material_name, texture_path)])
                _fake_bpy.data.objects[obj.name] = obj
                section_lines.extend([f"[mesh:{obj.name}]", f"hash = {index:08x}", "ps-t0 = Resource-old-DiffuseMap", "drawindexed = 3, 0, 0"])
            sections["[TextureOverride_Test]"] = section_lines; sections["_config_path"] = temp_dir
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Test]", sections, material_group_to_swapkey={},
                swap_key_prefix="$swapkey", next_swap_key_num=150, used_swap_keys=set(), transparency_sections_to_add=OrderedDict())
            for index in range(30):
                self.assertIn(f"ps-t0 = Resource_DiffuseMap_Part{index:02d}", sections["[TextureOverride_Test]"])

    def test_non_htmi_ps_material_to_resource_still_uses_generic_path(self):
        """测试非 HTMI 模式下 PS 材质到资源仍使用通用路径"""
        with tempfile.TemporaryDirectory() as temp_dir:
            texture_path = os.path.join(temp_dir, "generic-diffuse.png")
            with open(texture_path, "wb") as file_obj: file_obj.write(b"generic")
            obj = _FakeObject("GenericMesh", {}, [("DiffuseMap_Generic", texture_path)])
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([("[TextureOverride_Generic]", [f"[mesh:{obj.name}]", "hash = 12345678", "ps-t0 = Resource-old-DiffuseMap", "drawindexed = 3, 0, 0"]), ("_config_path", temp_dir)])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Generic]", sections, ...)
            self.assertIn("ps-t0 = ResourceTexture_DiffuseMap_Generic", sections["[TextureOverride_Generic]"])

    def test_non_htmi_generic_path_still_handles_rabbitfx_and_zzmi_refs(self):
        """测试非 HTMI 通用路径仍能处理 RabbitFX 和 ZZMI 引用"""
        with tempfile.TemporaryDirectory() as temp_dir:
            diffuse_path = os.path.join(temp_dir, "diffuse.png"); glow_path = os.path.join(temp_dir, "glow.png")
            for path in (diffuse_path, glow_path):
                with open(path, "wb") as file_obj: file_obj.write(os.path.basename(path).encode("ascii"))
            obj = _FakeObject("GenericMesh", {}, [("DiffuseMap_Generic", diffuse_path), ("Glowmap_5_Generic", glow_path)])
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([("[TextureOverride_Generic]", [f"[mesh:{obj.name}]", "hash = 12345678",
                "ps-t0 = Resource-old-DiffuseMap", "Resource\\RabbitFX\\Glowmap = ref Resource-old-Glowmap",
                "Resource\\ZZMI\\DiffuseMap = ref Resource-old-DiffuseMap", "drawindexed = 3, 0, 0"]), ("_config_path", temp_dir)])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Generic]", sections, ...)
            self.assertIn("Resource\\RabbitFX\\Glowmap = ref Resource_Glowmap_5_Generic", sections["[TextureOverride_Generic]"])
            self.assertIn("Resource\\ZZMI\\DiffuseMap = ref Resource_DiffuseMap_Generic", sections["[TextureOverride_Generic]"])

    def test_htmi_uses_current_object_material_before_source_candidate(self):
        """测试 HTMI 优先使用当前对象的材质而非源候选材质"""
        with tempfile.TemporaryDirectory() as temp_dir:
            source_texture = os.path.join(temp_dir, "source-candidate.png"); current_texture = os.path.join(temp_dir, "current-object.png")
            with open(source_texture, "wb") as file_obj: file_obj.write(b"wrong-source")
            with open(current_texture, "wb") as file_obj: file_obj.write(b"correct-current")
            texture_slots = {"ps-t0": {"mark_name": "DiffuseMap", "mark_type": "Slot", "mark_slot": "ps-t0"}}
            source_obj = _FakeObject("SharedSource", {}, [("DiffuseMap_Source", source_texture)])
            obj = _FakeObject("LOD0.ae1ab184-29187-0.Current", texture_slots, [("DiffuseMap_Current", current_texture)], original_object_name="SharedSource")
            _fake_bpy.data.objects[source_obj.name] = source_obj; _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([("[TextureOverride_Test]", [f"[mesh:{obj.name}]", "hash = ae1ab184", "ps-t0 = Resource-old-DiffuseMap", "drawindexed = 3, 0, 0"]), ("_config_path", temp_dir)])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Test]", sections, ...)
            self.assertIn("ps-t0 = Resource_DiffuseMap_Current", sections["[TextureOverride_Test]"])

    def test_material_lookup_skips_renamed_candidates_without_matching_materials(self):
        node = node_postprocess_material.SSMTNode_PostProcess_Material()
        node.name = "MaterialNode"

        original_name = "LOD0.a6431856-1020-0.Body"
        original_copy_name = f"{original_name}_copy"
        renamed_copy_name = "LOD0.aa9ffb85-13377-0.Body_copy"

        original_obj = _FakeObject(original_name, {}, ["DiffuseMap_Body"])
        original_copy = _FakeObject(original_copy_name, {}, ["DiffuseMap_Body"])
        renamed_copy = _FakeObject(renamed_copy_name, {}, ["NormalMap_Body"])
        original_copy.material_slots.clear()

        for obj in (original_obj, original_copy, renamed_copy):
            _fake_bpy.data.objects[obj.name] = obj

        node.apply_name_mapping({original_copy_name: renamed_copy_name})

        ini_mapping = OrderedDict([("ps-t0", "DiffuseMap")])
        resolved = node.find_object_by_mesh_name(
            renamed_copy_name,
            object_filter=lambda candidate: node._object_has_matching_materials(candidate, ini_mapping),
        )

        self.assertIs(resolved, original_obj)

    def test_htmi_does_not_fallback_to_source_candidate_when_current_object_has_no_materials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_texture = os.path.join(temp_dir, "source-candidate.png")
            with open(source_texture, "wb") as file_obj:
                file_obj.write(b"wrong-source")
            texture_slots = {"ps-t0": {"mark_name": "DiffuseMap", "mark_type": "Slot", "mark_slot": "ps-t0"}}
            source_obj = _FakeObject("SharedSource", {}, [("DiffuseMap_Source", source_texture)])
            obj = _FakeObject("LOD0.ae1ab184-29187-0.Current", texture_slots, [], original_object_name="SharedSource")
            _fake_bpy.data.objects[source_obj.name] = source_obj
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([("[TextureOverride_Test]", [f"[mesh:{obj.name}]", "hash = ae1ab184", "ps-t0 = Resource-old-DiffuseMap", "drawindexed = 3, 0, 0"]), ("_config_path", temp_dir)])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Test]", sections, ...)
            self.assertNotIn("ps-t0 = Resource_DiffuseMap_Source", sections["[TextureOverride_Test]"])
            self.assertFalse(any(line.startswith("ps-t0 = ") for line in sections["[TextureOverride_Test]"]))

    def test_htmi_workspace_slot_multiple_materials_reuse_existing_if_switch_rules(self):
        """测试 HTMI 工作空间槽的多材质复用现有 switch 规则"""
        with tempfile.TemporaryDirectory() as temp_dir:
            obj_name = "LOD0.fd054d1d-30030-0.Body"
            slot_texture_a = os.path.join(temp_dir, "diffuse-a.png"); slot_texture_b = os.path.join(temp_dir, "diffuse-b.png")
            for path, payload in ((slot_texture_a, b"a"), (slot_texture_b, b"b")):
                with open(path, "wb") as file_obj: file_obj.write(payload)
            obj = _FakeObject(obj_name, {"ps-t0": {"source_path": slot_texture_a, "mark_name": "DiffuseMap", "mark_type": "Slot", "mark_slot": "ps-t0", "mark_filename": "fd054d1d-30030-0-DiffuseMap.dds"}},
                [("DiffuseMap_Body_A", slot_texture_a), ("DiffuseMap_Body_B", slot_texture_b)])
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([("[TextureOverride_Test]", [f"[mesh:{obj.name}]", "hash = fd054d1d", "ps-t0 = Resource-old-DiffuseMap", "drawindexed = 3, 0, 0"]), ("_config_path", temp_dir)])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Test]", sections, ...)
            joined = "\n".join(sections["[TextureOverride_Test]"])
            self.assertIn("if $swapkey150 == 0", joined); self.assertIn("if $swapkey150 == 1", joined)

    def test_material_resource_names_replace_spaces_with_underscores(self):
        """测试材质资源名将空格替换为下划线"""
        material = _FakeMaterial("Highlight Metal 12261 01", os.path.join("/tmp", "tex.png"))
        node = node_postprocess_material.SSMTNode_PostProcess_Material()
        self.assertEqual(node._workspace_material_resource_name(material), "Resource-Highlight_Metal_12261_01")

    def test_material_resource_stem_replaces_chinese_with_deterministic_english(self):
        """测试中文材质名替换为确定性的随机英文串，保证引用一致"""
        material_cls = node_postprocess_material.SSMTNode_PostProcess_Material
        cloth_token = material_cls._latin_token_for_text("衣服")
        # 锁定映射算法：纯英文字母、首字母大写、长度固定
        self.assertRegex(cloth_token, r"^[A-Z][a-z]{9}$")
        stem = material_cls._material_resource_stem(_FakeMaterial("DiffuseMap_衣服02"))
        self.assertEqual(stem, f"DiffuseMap_{cloth_token}02")
        self.assertTrue(stem.isascii())
        # 同一中文永远得到同一串字母（跨调用、跨对象），不同中文得到不同串
        self.assertEqual(cloth_token, material_cls._latin_token_for_text("衣服"))
        self.assertNotEqual(cloth_token, material_cls._latin_token_for_text("裤子"))
        # 纯中文材质名也能生成合法纯英文 stem
        pure_chinese_stem = material_cls._material_resource_stem(_FakeMaterial("贴图"))
        self.assertEqual(pure_chinese_stem, material_cls._latin_token_for_text("贴图"))
        self.assertTrue(pure_chinese_stem.isascii())

    def test_chinese_material_generates_pure_english_resource_texture_section(self):
        """测试中文材质生成的 ResourceTexture 引用段落为纯英文且引用与资源段对应"""
        with tempfile.TemporaryDirectory() as temp_dir:
            texture_path = os.path.join(temp_dir, "generic-diffuse.png")
            with open(texture_path, "wb") as file_obj:
                file_obj.write(b"generic")
            obj = _FakeObject("GenericMesh", {}, [("DiffuseMap_贴图", texture_path)])
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([("[TextureOverride_Generic]", [f"[mesh:{obj.name}]", "hash = 12345678", "ps-t0 = Resource-old-DiffuseMap", "drawindexed = 3, 0, 0"]), ("_config_path", temp_dir)])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Generic]", sections, ...)

            material_cls = node_postprocess_material.SSMTNode_PostProcess_Material
            stem = material_cls._material_resource_stem(_FakeMaterial("DiffuseMap_贴图"))
            resource_name = f"ResourceTexture_{stem}"
            # 引用行为纯英文且指向资源段
            reference_line = f"ps-t0 = {resource_name}"
            self.assertIn(reference_line, sections["[TextureOverride_Generic]"])
            self.assertTrue(reference_line.isascii())
            # 资源段存在且 filename 行同为纯英文，贴图文件已按新名字复制
            section_name = f"[{resource_name}]"
            self.assertIn(section_name, sections)
            filename_line = f"filename = Textures/{stem}.png"
            self.assertIn(filename_line, sections[section_name])
            self.assertTrue(filename_line.isascii())
            self.assertTrue(os.path.exists(os.path.join(temp_dir, "Textures", f"{stem}.png")))

    def test_htmi_prefers_prefix_cache_texture_slots_over_stale_object_prop(self):
        """测试 HTMI 优先使用前缀缓存的纹理槽而非过时的对象属性"""
        with tempfile.TemporaryDirectory() as temp_dir:
            diffuse_path = os.path.join(temp_dir, "diffuse.png"); light_path = os.path.join(temp_dir, "light.png")
            for path, payload in ((diffuse_path, b"diffuse"), (light_path, b"light")):
                with open(path, "wb") as file_obj: file_obj.write(payload)
            obj_name = "LOD0.fd054d1d-30030-0.Body"
            obj = _FakeObject(obj_name, {"ps-t0": {"mark_name": "DiffuseMap", "mark_type": "Slot", "mark_slot": "ps-t0", "mark_filename": "stale-DiffuseMap.dds"}},
                [("DiffuseMap_Body", diffuse_path), ("LightMap_Body", light_path)])
            _prefix_cache_state["props"] = {
                "modimp_profile_id": "yihuan",
                "modimp_workspace_unique_str": "LOD0.fd054d1d-30030-0",
                "modimp_texture_slots": json.dumps({
                    "ps-t0": {"mark_name": "DiffuseMap", "mark_type": "Slot", "mark_slot": "ps-t0", "mark_filename": "fresh-DiffuseMap.dds"},
                    "ps-t1": {"mark_name": "LightMap", "mark_type": "Slot", "mark_slot": "ps-t1", "mark_filename": "fresh-LightMap.dds"},
                }),
            }
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([("[TextureOverride_Test]", [f"[mesh:{obj.name}]", "hash = fd054d1d", "ps-t0 = Resource-old-DiffuseMap", "ps-t1 = Resource-old-LightMap", "drawindexed = 3, 0, 0"]), ("_config_path", temp_dir)])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Test]", sections, ...)
            self.assertIn("ps-t0 = Resource_DiffuseMap_Body", sections["[TextureOverride_Test]"])
            self.assertIn("ps-t1 = Resource_LightMap_Body", sections["[TextureOverride_Test]"])

    def test_htmi_does_not_fallback_to_object_texture_slots_when_prefix_cache_exists_but_is_empty(self):
        """测试 HTMI 在前缀缓存存在但为空时不回退到对象纹理槽"""
        with tempfile.TemporaryDirectory() as temp_dir:
            stale_diffuse_path = os.path.join(temp_dir, "stale-diffuse.png")
            with open(stale_diffuse_path, "wb") as file_obj: file_obj.write(b"stale")
            obj_name = "LOD0.fd054d1d-30030-0.Body"
            obj = _FakeObject(obj_name, {"ps-t0": {"mark_name": "DiffuseMap", "mark_type": "Slot", "mark_slot": "ps-t0", "mark_filename": "stale-DiffuseMap.dds", "source_path": stale_diffuse_path}},
                [("DiffuseMap_Body", stale_diffuse_path)])
            _prefix_cache_state["props"] = {"modimp_profile_id": "yihuan", "modimp_workspace_unique_str": "LOD0.fd054d1d-30030-0"}
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([("[TextureOverride_Test]", [f"[mesh:{obj.name}]", "hash = fd054d1d", "ps-t0 = Resource-old-DiffuseMap", "drawindexed = 3, 0, 0"]), ("_config_path", temp_dir)])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Test]", sections, ...)
            self.assertNotIn("ps-t0 = Resource_DiffuseMap_Body", sections["[TextureOverride_Test]"])

    def test_ntmi_modimp_extra_ps_t2_diffuse_map_reuses_generated_diffuse_resource(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diffuse_path = os.path.join(temp_dir, "diffuse.png")
            with open(diffuse_path, "wb") as file_obj:
                file_obj.write(b"diffuse")
            obj = _FakeObject(
                "LOD0.fd054d1d-30030-0.Body",
                {"ps-t0": {"mark_name": "DiffuseMap", "mark_type": "Slot", "mark_slot": "ps-t0"}},
                [("DiffuseMap_Body", diffuse_path)],
            )
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([
                ("[TextureOverride_Test]", [
                    f"[mesh:{obj.name}]",
                    "hash = fd054d1d",
                    "ps-t0 = Resource-old-DiffuseMap",
                    "drawindexed = 3, 0, 0",
                ]),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node._ntmi_modimp_extra_ps_t2_diffuse_map = True
            node.process_texture_override_section("[TextureOverride_Test]", sections, ...)
            override_lines = sections["[TextureOverride_Test]"]
            self.assertIn("ps-t0 = Resource_DiffuseMap_Body", override_lines)
            self.assertIn("ps-t2 = Resource_DiffuseMap_Body", override_lines)

    def test_ntmi_modimp_extra_ps_t2_diffuse_map_handles_missing_workspace_object(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sections = OrderedDict([
                ("[TextureOverride_Test]", [
                    "[mesh:MissingObject]",
                    "hash = fd054d1d",
                    "ps-t0 = Resource-old-DiffuseMap",
                    "drawindexed = 3, 0, 0",
                ]),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node._ntmi_modimp_extra_ps_t2_diffuse_map = True

            node.process_texture_override_section("[TextureOverride_Test]", sections, ...)

            override_lines = sections["[TextureOverride_Test]"]
            self.assertIn("ps-t0 = Resource-old-DiffuseMap", override_lines)

    def test_ntmi_modimp_extra_ps_t2_diffuse_map_reuses_same_switch_variable_for_multiple_diffuse_materials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            texture_a = os.path.join(temp_dir, "diffuse-a.png")
            texture_b = os.path.join(temp_dir, "diffuse-b.png")
            for path, payload in ((texture_a, b"a"), (texture_b, b"b")):
                with open(path, "wb") as file_obj:
                    file_obj.write(payload)
            obj = _FakeObject(
                "LOD0.fd054d1d-30030-0.Body",
                {"ps-t0": {"mark_name": "DiffuseMap", "mark_type": "Slot", "mark_slot": "ps-t0"}},
                [("DiffuseMap_Body_A", texture_a), ("DiffuseMap_Body_B", texture_b)],
            )
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([
                ("[TextureOverride_Test]", [
                    f"[mesh:{obj.name}]",
                    "hash = fd054d1d",
                    "ps-t0 = Resource-old-DiffuseMap",
                    "drawindexed = 3, 0, 0",
                ]),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node._ntmi_modimp_extra_ps_t2_diffuse_map = True
            node.process_texture_override_section("[TextureOverride_Test]", sections, ...)
            joined = "\n".join(sections["[TextureOverride_Test]"])
            self.assertEqual(joined.count("if $swapkey150 == 0"), 1)
            self.assertEqual(joined.count("if $swapkey150 == 1"), 1)
            self.assertIn("ps-t0 = Resource_DiffuseMap_Body_A", joined)
            self.assertIn("ps-t2 = Resource_DiffuseMap_Body_A", joined)
            self.assertIn("ps-t0 = Resource_DiffuseMap_Body_B", joined)
            self.assertIn("ps-t2 = Resource_DiffuseMap_Body_B", joined)

    def test_ntmi_modimp_extra_ps_t2_diffuse_map_uses_actual_diffuse_workspace_slot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diffuse_path = os.path.join(temp_dir, "diffuse.png")
            with open(diffuse_path, "wb") as file_obj:
                file_obj.write(b"diffuse")
            obj = _FakeObject(
                "LOD0.14076dfb-4893-310857.TimeRing",
                {"ps-t7": {"mark_name": "DiffuseMap", "mark_type": "Slot", "mark_slot": "ps-t7"}},
                [("DiffuseMap_衣服02", diffuse_path)],
            )
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([
                ("[TextureOverride_Test]", [
                    f"[mesh:{obj.name}]",
                    "hash = 14076dfb",
                    "ps-t5 = ResourceTexture_14076dfb_4893_310857_T5",
                    "ps-t7 = ResourceTexture_14076dfb_4893_310857_T7",
                    "ps-t8 = ResourceTexture_14076dfb_4893_310857_T8",
                    "drawindexed = 240,0,0",
                ]),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node._ntmi_modimp_extra_ps_t2_diffuse_map = True
            node.process_texture_override_section("[TextureOverride_Test]", sections, ...)
            override_lines = sections["[TextureOverride_Test]"]
            cloth_token = node_postprocess_material.SSMTNode_PostProcess_Material._latin_token_for_text("衣服")
            self.assertIn(f"ps-t7 = Resource_DiffuseMap_{cloth_token}02", override_lines)
            self.assertIn(f"ps-t2 = Resource_DiffuseMap_{cloth_token}02", override_lines)

    def test_ntmi_modimp_extra_ps_t2_diffuse_map_does_not_duplicate_unrelated_ps_slots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            texture_paths = {}
            for key in ("normal", "diffuse", "light", "highlight"):
                path = os.path.join(temp_dir, f"{key}.png")
                with open(path, "wb") as file_obj:
                    file_obj.write(key.encode("ascii"))
                texture_paths[key] = path

            obj = _FakeObject(
                "LOD0.14076dfb-113934-18597.装饰_copy",
                {
                    "ps-t5": {"mark_name": "NormalMap", "mark_type": "Slot", "mark_slot": "ps-t5"},
                    "ps-t7": {"mark_name": "DiffuseMap", "mark_type": "Slot", "mark_slot": "ps-t7"},
                    "ps-t8": {"mark_name": "LightMap", "mark_type": "Slot", "mark_slot": "ps-t8"},
                    "ps-t18": {"mark_name": "HighLightMap", "mark_type": "Slot", "mark_slot": "ps-t18"},
                },
                [
                    ("NormalMap_DiffuseMap_衣服03", texture_paths["normal"]),
                    ("DiffuseMap_衣服03", texture_paths["diffuse"]),
                    ("LightMap_LOD0.14076dfb-111345-159309.切断器.001", texture_paths["light"]),
                    ("HighLightMap_LOD0.14076dfb-111345-159309.切断器.001", texture_paths["highlight"]),
                ],
            )
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([
                ("[TextureOverride_Test]", [
                    f"[mesh:{obj.name}]",
                    "hash = 14076dfb",
                    "ps-t5 = ResourceTexture_14076dfb_113934_18597_T5",
                    "ps-t7 = ResourceTexture_14076dfb_113934_18597_T7",
                    "ps-t8 = ResourceTexture_14076dfb_113934_18597_T8",
                    "ps-t18 = ResourceTexture_14076dfb_113934_18597_T18",
                    "drawindexed = 12993,163653,0",
                ]),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node._ntmi_modimp_extra_ps_t2_diffuse_map = True

            node.process_texture_override_section("[TextureOverride_Test]", sections, ...)

            joined = "\n".join(sections["[TextureOverride_Test]"])
            material_cls = node_postprocess_material.SSMTNode_PostProcess_Material
            cloth_token = material_cls._latin_token_for_text("衣服")
            cutter_token = material_cls._latin_token_for_text("切断器")
            self.assertEqual(joined.count(f"ps-t5 = Resource_NormalMap_DiffuseMap_{cloth_token}03"), 1)
            self.assertEqual(joined.count(f"ps-t7 = Resource_DiffuseMap_{cloth_token}03"), 1)
            self.assertEqual(joined.count(f"ps-t2 = Resource_DiffuseMap_{cloth_token}03"), 1)
            self.assertEqual(joined.count(f"ps-t8 = Resource_LightMap_LOD0.14076dfb-111345-159309.{cutter_token}.001"), 1)
            self.assertEqual(joined.count(f"ps-t18 = Resource_HighLightMap_LOD0.14076dfb-111345-159309.{cutter_token}.001"), 1)

    def test_ntmi_modimp_extra_ps_t2_diffuse_map_adds_base_alias_for_diffuse_workspace_slot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diffuse_path = os.path.join(temp_dir, "diffuse.png")
            with open(diffuse_path, "wb") as file_obj:
                file_obj.write(b"diffuse")
            obj = _FakeObject(
                "LOD0.14076dfb-4893-310857.TimeRing",
                {"ps-t7": {"mark_name": "DiffuseMap", "mark_type": "Slot", "mark_slot": "ps-t7"}},
                [("DiffuseMap_衣服02", diffuse_path)],
            )
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([
                ("[TextureOverride_Test]", [
                    f"[mesh:{obj.name}]",
                    "hash = 14076dfb",
                    "ps-t5 = ResourceTexture_14076dfb_4893_310857_T5",
                    "ps-t7 = ResourceTexture_14076dfb_4893_310857_T7",
                    "ps-t8 = ResourceTexture_14076dfb_4893_310857_T8",
                    "drawindexed = 240,0,0",
                ]),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node._ntmi_modimp_extra_ps_t2_diffuse_map = True
            node.process_texture_override_section("[TextureOverride_Test]", sections, ...)
            override_lines = sections["[TextureOverride_Test]"]
            self.assertIn("ps-t2 = ResourceTexture_14076dfb_4893_310857_T7", override_lines)

    def test_transparency_moves_complete_shader_replace_conditional_block(self):
        obj = _FakeObject("Body_透明0.5", {}, ["ImportedObject_Material"])
        _fake_bpy.data.objects[obj.name] = obj
        sections = OrderedDict([
            ("[TextureOverride_Test]", [
                f"[mesh:{obj.name}]",
                "if $Rain_ps_replace == 1",
                "    run = CustomShader_Rain_World",
                "else",
                "    run = CustomShader_Rain_Normal",
                "endif",
            ]),
            ("_config_path", ""),
        ])
        transparency_sections = OrderedDict()
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
            transparency_sections_to_add=transparency_sections,
        )

        override_lines = sections["[TextureOverride_Test]"]
        self.assertEqual(len(override_lines), 2)
        self.assertTrue(override_lines[1].startswith("run = CustomShaderTransparencyCloth"))
        moved_lines = next(iter(transparency_sections.values()))
        self.assertEqual(moved_lines[-1], "endif")
        self.assertEqual(
            sum(line.strip().startswith("if ") for line in moved_lines),
            sum(line.strip() == "endif" for line in moved_lines),
        )

    def test_transparency_stops_moving_content_after_draw_command(self):
        obj = _FakeObject("Body_透明0.5", {}, ["ImportedObject_Material"])
        _fake_bpy.data.objects[obj.name] = obj
        sections = OrderedDict([
            ("[TextureOverride_Test]", [
                f"[mesh:{obj.name}]",
                r"Resource\ZZMI\Diffuse = ref Resource_DiffuseMap_Color",
                r"run = CommandList\ZZMI\SetTextures",
                "  drawindexed = 1410,277422,0",
                "endif",
                "if $swapkey11 == 1",
                r"  run = CommandList\Unrelated",
                "endif",
            ]),
            ("_config_path", ""),
        ])
        transparency_sections = OrderedDict()
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
            transparency_sections_to_add=transparency_sections,
        )

        moved_lines = next(iter(transparency_sections.values()))
        self.assertEqual(moved_lines[-1].strip(), "drawindexed = 1410,277422,0")
        override_lines = sections["[TextureOverride_Test]"]
        self.assertIn("endif", override_lines)
        self.assertIn("if $swapkey11 == 1", override_lines)
        self.assertIn(r"  run = CommandList\Unrelated", override_lines)

    def test_transparency_keeps_conditional_draw_block_balanced(self):
        obj = _FakeObject("Body_透明0.5", {}, ["ImportedObject_Material"])
        _fake_bpy.data.objects[obj.name] = obj
        sections = OrderedDict([
            ("[TextureOverride_Test]", [
                f"[mesh:{obj.name}]",
                "if $swapkey11 == 1",
                r"  run = CommandList\ZZMI\SetTextures",
                "  drawindexed = 1410,277422,0",
                "endif",
                "if $swapkey12 == 1",
                r"  run = CommandList\Unrelated",
                "endif",
            ]),
            ("_config_path", ""),
        ])
        transparency_sections = OrderedDict()
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
            transparency_sections_to_add=transparency_sections,
        )

        moved_lines = next(iter(transparency_sections.values()))
        self.assertEqual(moved_lines[-1], "endif")
        self.assertEqual(
            sum(line.strip().startswith("if ") for line in moved_lines),
            sum(line.strip() == "endif" for line in moved_lines),
        )
        override_lines = sections["[TextureOverride_Test]"]
        self.assertIn("if $swapkey12 == 1", override_lines)
        self.assertIn(r"  run = CommandList\Unrelated", override_lines)

    def test_transparency_keeps_distinct_blocks_for_duplicate_mesh_names(self):
        obj = _FakeObject("Body_透明0.5", {}, ["ImportedObject_Material"])
        _fake_bpy.data.objects[obj.name] = obj
        sections = OrderedDict([
            ("[TextureOverride_Test]", [
                f"[mesh:{obj.name}]",
                "drawindexed = 3,0,0",
                f"[mesh:{obj.name}]",
                "drawindexed = 6,3,0",
            ]),
            ("_config_path", ""),
        ])
        transparency_sections = OrderedDict()
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
            transparency_sections_to_add=transparency_sections,
        )

        self.assertEqual(len(transparency_sections), 2)
        moved_lines = [line for block in transparency_sections.values() for line in block]
        self.assertIn("drawindexed = 3,0,0", moved_lines)
        self.assertIn("drawindexed = 6,3,0", moved_lines)
        run_targets = [
            line.split("=", 1)[1].strip()
            for line in sections["[TextureOverride_Test]"]
            if line.startswith("run = CustomShaderTransparencyCloth")
        ]
        self.assertEqual(len(run_targets), 2)
        self.assertEqual(len(set(run_targets)), 2)

    def test_transparency_avoids_existing_custom_shader_section_name(self):
        obj = _FakeObject("Body_透明0.5", {}, ["ImportedObject_Material"])
        _fake_bpy.data.objects[obj.name] = obj
        node = node_postprocess_material.SSMTNode_PostProcess_Material()
        base_name, _value = node.extract_transparency_info_from_mesh_name(obj.name)
        sections = OrderedDict([
            ("[TextureOverride_Test]", [
                f"[mesh:{obj.name}]",
                "drawindexed = 3,0,0",
            ]),
            (f"[{base_name}]", ["handling = skip"]),
            ("_config_path", ""),
        ])
        transparency_sections = OrderedDict()
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
            transparency_sections_to_add=transparency_sections,
        )

        self.assertEqual(list(transparency_sections), [f"{base_name}_2"])
        self.assertIn(f"run = {base_name}_2", sections["[TextureOverride_Test]"])

    def test_transparency_shader_name_is_converted_to_english_token(self):
        """透明生成的 CustomShader 段名/run 目标不再包含中文，使用与资源名一致的中文转英文 token"""
        node = node_postprocess_material.SSMTNode_PostProcess_Material()

        shader_name, transparency_value = node.extract_transparency_info_from_mesh_name("Body_透明0.5")

        self.assertEqual(transparency_value, "0.5")
        self.assertTrue(shader_name.startswith("CustomShaderTransparencyClothBody_"))
        self.assertTrue(shader_name.endswith("0.5"))
        self.assertNotIn("透明", shader_name)
        self.assertIn(node._latin_token_for_text("透明"), shader_name)

        complex_name, _ = node.extract_transparency_info_from_mesh_name("LOD0.abc-55002-0.身体.004_010_透明0.75")
        self.assertNotIn("身体", complex_name)
        self.assertNotIn("透明", complex_name)
        self.assertTrue(complex_name.endswith("0.75"))

    def test_ttl_transparent_with_zzmi_materials_creates_new_section(self):
        """FX>>>TTL：_透明0.75 + 四前缀材质生成独立新段，ZZMI 引用改为材质转资源，原段删除"""
        with tempfile.TemporaryDirectory() as temp_dir:
            BS = chr(92)
            material_paths = {}
            for prefix, filename in [
                ("DiffuseMap", "diffuse.png"), ("NormalMap", "normal.png"),
                ("LightMap", "light.png"), ("MaterialMap", "material.png"),
            ]:
                path = os.path.join(temp_dir, filename)
                with open(path, "wb") as file_obj:
                    file_obj.write(filename.encode("ascii"))
                material_paths[prefix] = path

            mesh_name = "LOD0.241deac5-56376-0.中文中文_透明0.75_copy"
            obj = _FakeObject(mesh_name, {}, [
                ("DiffuseMap_衣服", material_paths["DiffuseMap"]),
                ("NormalMap_腿", material_paths["NormalMap"]),
                ("LightMap_袖子", material_paths["LightMap"]),
                ("MaterialMap_身体", material_paths["MaterialMap"]),
            ])
            _fake_bpy.data.objects[obj.name] = obj

            sections = OrderedDict([
                ("[TextureOverride_LOD0.241deac5_56376_0]", [
                    "hash = 241deac5",
                    "match_first_index = 0",
                    "ib = Resource_LOD0.241deac5_56376_0_Index",
                    f"Resource{BS}ZZMI{BS}Diffuse = ref Resource-241deac5-56376-0-DiffuseMap",
                    f"Resource{BS}ZZMI{BS}NormalMap = ref Resource-241deac5-56376-0-NormalMap",
                    f"Resource{BS}ZZMI{BS}LightMap = ref Resource-241deac5-56376-0-LightMap",
                    f"Resource{BS}ZZMI{BS}MaterialMap = ref Resource-241deac5-56376-0-MaterialMap",
                    f"run = CommandList{BS}ZZMI{BS}SetTextures",
                    "run = CommandListSkinTexture",
                    "; [mesh:LOD0.241deac5-56376-0.中文中文_透明0.75_copy] [vertex_count:15618]",
                    "drawindexed = 56376, 0, 0",
                ]),
                ("_config_path", temp_dir),
            ])

            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.fx_to_ttl = True

            expected_section = node._ttl_section_name(obj.name, sections, set())
            node.process_texture_override_section(
                "[TextureOverride_LOD0.241deac5_56376_0]",
                sections,
                material_group_to_swapkey={},
                swap_key_prefix="$swapkey",
                next_swap_key_num=150,
                used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )

            self.assertNotIn("[TextureOverride_LOD0.241deac5_56376_0]", sections)
            self.assertIn(f"[{expected_section}]", sections)
            new_lines = sections[f"[{expected_section}]"]
            self.assertTrue(expected_section.isascii())
            self.assertIn("hash = 241deac5", new_lines)
            self.assertIn("ib = Resource_LOD0.241deac5_56376_0_Index", new_lines)
            self.assertIn("run = CommandListSkinTexture", new_lines)

            diffuse_token = node._latin_token_for_text("衣服")
            normal_token = node._latin_token_for_text("腿")
            light_token = node._latin_token_for_text("袖子")
            material_token = node._latin_token_for_text("身体")
            self.assertIn(f"Resource{BS}ZZMI{BS}Diffuse = ref Resource_DiffuseMap_{diffuse_token}", new_lines)
            self.assertIn(f"Resource{BS}ZZMI{BS}NormalMap = ref Resource_NormalMap_{normal_token}", new_lines)
            self.assertIn(f"Resource{BS}ZZMI{BS}LightMap = ref Resource_LightMap_{light_token}", new_lines)
            self.assertIn(f"Resource{BS}ZZMI{BS}MaterialMap = ref Resource_MaterialMap_{material_token}", new_lines)
            self.assertIn(f"[Resource_DiffuseMap_{diffuse_token}]", sections)
            self.assertIn(f"filename = Textures/DiffuseMap_{diffuse_token}.png", sections[f"[Resource_DiffuseMap_{diffuse_token}]"])

            self.assertIn("; [mesh:LOD0.241deac5-56376-0.中文中文_透明0.75_copy] [vertex_count:15618]", new_lines)
            self.assertIn("$" + BS + "TTL" + BS + "alpha = $TTLAlpha0_75", new_lines)
            self.assertIn("global $TTLAlpha0_75 = 0.75", sections["[Constants]"])
            self.assertIn("$" + BS + "TTL" + BS + "_1 = 56376", new_lines)
            self.assertIn("$" + BS + "TTL" + BS + "_2 = 0", new_lines)
            self.assertIn("run = CommandList" + BS + "TTL" + BS + "Draw", new_lines)
            self.assertNotIn("Resource" + BS + "TTL" + BS + "TransparencyTex", new_lines)

    def test_ttl_if_switch_block_moved_with_condition(self):
        """FX>>>TTL：if/endif 条件块整体迁入新段并包住 _1/_2/run，原段删除"""
        with tempfile.TemporaryDirectory() as temp_dir:
            BS = chr(92)
            diffuse_path = os.path.join(temp_dir, "diffuse.png")
            with open(diffuse_path, "wb") as file_obj:
                file_obj.write(b"diffuse")

            mesh_name = "LOD0.241deac5-56376-0.中文中文_透明0.75_copy"
            obj = _FakeObject(mesh_name, {}, [("DiffuseMap_衣服", diffuse_path)])
            _fake_bpy.data.objects[obj.name] = obj

            sections = OrderedDict([
                ("[TextureOverride_LOD0.241deac5_56376_0]", [
                    "hash = 241deac5",
                    "match_first_index = 0",
                    "ib = Resource_LOD0.241deac5_56376_0_Index",
                    f"Resource{BS}ZZMI{BS}Diffuse = ref Resource-241deac5-56376-0-DiffuseMap",
                    f"run = CommandList{BS}ZZMI{BS}SetTextures",
                    "run = CommandListSkinTexture",
                    "if $swapkey0 == 0 && $swapkey1 == 0",
                    "  ; [mesh:LOD0.241deac5-56376-0.中文中文_透明0.75_copy] [vertex_count:15618]",
                    "  drawindexed = 56376,0,0",
                    "endif",
                ]),
                ("_config_path", temp_dir),
            ])

            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.fx_to_ttl = True

            node.process_texture_override_section(
                "[TextureOverride_LOD0.241deac5_56376_0]",
                sections,
                material_group_to_swapkey={},
                swap_key_prefix="$swapkey",
                next_swap_key_num=150,
                used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )

            self.assertNotIn("[TextureOverride_LOD0.241deac5_56376_0]", sections)
            new_section = next(key for key in sections if key.startswith("[TextureOverrideLOD0_"))
            new_lines = sections[new_section]
            self.assertIn("if $swapkey0 == 0 && $swapkey1 == 0", new_lines)
            self.assertIn("    $" + BS + "TTL" + BS + "_1 = 56376", new_lines)
            self.assertIn("    $" + BS + "TTL" + BS + "_2 = 0", new_lines)
            self.assertIn("    run = CommandList" + BS + "TTL" + BS + "Draw", new_lines)
            self.assertEqual(new_lines[-1], "endif")
            self.assertLess(new_lines.index("$" + BS + "TTL" + BS + "alpha = $TTLAlpha0_75"), new_lines.index("if $swapkey0 == 0 && $swapkey1 == 0"))

    def test_ttl_transparent_plus_fxmap_writes_ttl_ref(self):
        """FX>>>TTL：_透明0.75 且有 FXMap 材质时写 TransparencyTex 引用并生成资源段，无旧 FXMap 流程"""
        with tempfile.TemporaryDirectory() as temp_dir:
            BS = chr(92)
            diffuse_path = os.path.join(temp_dir, "diffuse.png")
            fxmap_path = os.path.join(temp_dir, "fxmap.png")
            for path in (diffuse_path, fxmap_path):
                with open(path, "wb") as file_obj:
                    file_obj.write(os.path.basename(path).encode("ascii"))

            obj = _FakeObject("Body_透明0.5", {}, [
                ("DiffuseMap_衣服", diffuse_path),
                ("FXMap_遮罩", fxmap_path),
            ])
            _fake_bpy.data.objects[obj.name] = obj

            sections = OrderedDict([
                ("[TextureOverride_Body]", [
                    "hash = 241deac5",
                    "match_first_index = 0",
                    "ib = Resource_Body_Index",
                    f"Resource{BS}ZZMI{BS}Diffuse = ref Resource-old-DiffuseMap",
                    f"run = CommandList{BS}ZZMI{BS}SetTextures",
                    "run = CommandListSkinTexture",
                    "[mesh:Body_透明0.5]",
                    "drawindexed = 100, 5, 0",
                ]),
                ("_config_path", temp_dir),
            ])

            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.fx_to_ttl = True

            node.process_texture_override_section(
                "[TextureOverride_Body]",
                sections,
                material_group_to_swapkey={},
                swap_key_prefix="$swapkey",
                next_swap_key_num=150,
                used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )

            fx_token = node._latin_token_for_text("遮罩")
            new_section = next(key for key in sections if key.startswith("[TextureOverrideBody_"))
            new_lines = sections[new_section]
            self.assertIn(f"Resource{BS}TTL{BS}TransparencyTex = ref Resource_FXMap_{fx_token}", new_lines)
            self.assertIn("$" + BS + "TTL" + BS + "alpha = $TTLAlpha0_5", new_lines)
            self.assertIn("global $TTLAlpha0_5 = 0.5", sections["[Constants]"])
            self.assertIn(f"[Resource_FXMap_{fx_token}]", sections)
            self.assertIn(f"filename = Textures/FXMap_{fx_token}.png", sections[f"[Resource_FXMap_{fx_token}]"])
            joined = "\n".join(str(line) for section_lines in sections.values() for line in section_lines)
            self.assertNotIn(r"ResourceRabbitFXFXMap", joined)
            self.assertNotIn(r"ResourceNTEMIFXFXMap", joined)

    def test_ttl_fxmap_only_writes_alpha_1(self):
        """FX>>>TTL：仅有 FXMap 材质（无透明后缀）时 alpha 固定 1.0"""
        with tempfile.TemporaryDirectory() as temp_dir:
            BS = chr(92)
            fxmap_path = os.path.join(temp_dir, "fxmap.png")
            with open(fxmap_path, "wb") as file_obj:
                file_obj.write(b"fxmap")
            obj = _FakeObject("GenericMesh", {}, [("FXMap_Generic", fxmap_path)])
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([
                ("[TextureOverride_Generic]", [
                    "hash = 12345678",
                    "match_first_index = 0",
                    "ib = Resource_GenericMesh_Index",
                    "run = CommandListSkinTexture",
                    "[mesh:GenericMesh]",
                    "drawindexed = 3, 0, 0",
                ]),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.fx_to_ttl = True
            node.process_texture_override_section(
                "[TextureOverride_Generic]", sections, material_group_to_swapkey={},
                swap_key_prefix="$swapkey", next_swap_key_num=150, used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )
            new_lines = sections["[TextureOverrideGenericMesh]"]
            self.assertIn("; GenericMesh", new_lines)
            self.assertIn("Resource" + BS + "TTL" + BS + "TransparencyTex = ref Resource_FXMap_Generic", new_lines)
            self.assertIn("$" + BS + "TTL" + BS + "alpha = $TTLAlpha1_0", new_lines)
            self.assertIn("global $TTLAlpha1_0 = 1.0", sections["[Constants]"])
            self.assertIn("$" + BS + "TTL" + BS + "_1 = 3", new_lines)
            self.assertIn("$" + BS + "TTL" + BS + "_2 = 0", new_lines)
            self.assertNotIn("[TextureOverride_Generic]", sections)

    def test_ttl_section_name_conflict_appends_suffix(self):
        """FX>>>TTL：新段名与现有段冲突时追加 _2"""
        with tempfile.TemporaryDirectory() as temp_dir:
            BS = chr(92)
            fxmap_path = os.path.join(temp_dir, "fxmap.png")
            with open(fxmap_path, "wb") as file_obj:
                file_obj.write(b"fxmap")
            obj = _FakeObject("GenericMesh", {}, [("FXMap_Generic", fxmap_path)])
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([
                ("[TextureOverride_Generic]", [
                    "hash = 12345678",
                    "match_first_index = 0",
                    "ib = Resource_GenericMesh_Index",
                    "[mesh:GenericMesh]",
                    "drawindexed = 3, 0, 0",
                ]),
                ("[TextureOverrideGenericMesh]", ["hash = 99999999"]),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.fx_to_ttl = True
            node.process_texture_override_section(
                "[TextureOverride_Generic]", sections, material_group_to_swapkey={},
                swap_key_prefix="$swapkey", next_swap_key_num=150, used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )
            self.assertIn("[TextureOverrideGenericMesh_2]", sections)
            self.assertNotIn("[TextureOverride_Generic]", sections)

    def test_ttl_multiple_fxmap_materials_use_switch_branches(self):
        """FX>>>TTL：同一物体多个 FXMap 材质生成 $swapkey 分支"""
        with tempfile.TemporaryDirectory() as temp_dir:
            BS = chr(92)
            fx_a = os.path.join(temp_dir, "fxmap-a.png")
            fx_b = os.path.join(temp_dir, "fxmap-b.png")
            for path in (fx_a, fx_b):
                with open(path, "wb") as file_obj:
                    file_obj.write(os.path.basename(path).encode("ascii"))
            obj = _FakeObject("Body", {}, [("FXMap_A", fx_a), ("FXMap_B", fx_b)])
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([
                ("[TextureOverride_Body]", [
                    "hash = 12345678",
                    "match_first_index = 0",
                    "ib = Resource_Body_Index",
                    "[mesh:Body]",
                    "drawindexed = 6, 3, 0",
                ]),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.fx_to_ttl = True
            node.process_texture_override_section(
                "[TextureOverride_Body]", sections, material_group_to_swapkey={},
                swap_key_prefix="$swapkey", next_swap_key_num=150, used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )
            new_lines = sections["[TextureOverrideBody]"]
            self.assertIn("if $swapkey150 == 0", new_lines)
            self.assertIn("if $swapkey150 == 1", new_lines)
            self.assertIn("    Resource" + BS + "TTL" + BS + "TransparencyTex = ref Resource_FXMap_A", new_lines)
            self.assertIn("    Resource" + BS + "TTL" + BS + "TransparencyTex = ref Resource_FXMap_B", new_lines)

    def test_ttl_shared_if_only_transparent_gets_own_if(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            BS = chr(92)
            diffuse_path = os.path.join(temp_dir, "diffuse.png")
            with open(diffuse_path, "wb") as file_obj:
                file_obj.write(b"diffuse")
            transparent_obj = _FakeObject("Body_透明0.5", {}, [("DiffuseMap_衣服", diffuse_path)])
            normal_obj = _FakeObject("Body", {}, [("DiffuseMap_衣服", diffuse_path)])
            _fake_bpy.data.objects[transparent_obj.name] = transparent_obj
            _fake_bpy.data.objects[normal_obj.name] = normal_obj
            sections = OrderedDict([
                ("[TextureOverride_Shared]", [
                    "hash = 241deac5",
                    "match_first_index = 0",
                    "ib = Resource_Shared_Index",
                    f"Resource{BS}ZZMI{BS}Diffuse = ref Resource-old-DiffuseMap",
                    f"run = CommandList{BS}ZZMI{BS}SetTextures",
                    "run = CommandListSkinTexture",
                    "if $swapkey0 == 0",
                    "  [mesh:Body]",
                    "  drawindexed = 100, 0, 0",
                    "  [mesh:Body_透明0.5]",
                    "  drawindexed = 200, 100, 0",
                    "endif",
                ]),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.fx_to_ttl = True
            node.process_texture_override_section(
                "[TextureOverride_Shared]", sections, material_group_to_swapkey={},
                swap_key_prefix="$swapkey", next_swap_key_num=150, used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )
            self.assertIn("[TextureOverride_Shared]", sections)
            shared = sections["[TextureOverride_Shared]"]
            self.assertIn("  [mesh:Body]", shared)
            self.assertIn("  drawindexed = 100, 0, 0", shared)
            self.assertNotIn("[mesh:Body_透明0.5]", shared)
            transparent_section = next(key for key in sections if key.startswith("[TextureOverrideBody_"))
            new_lines = sections[transparent_section]
            self.assertIn("if $swapkey0 == 0", new_lines)
            self.assertIn("    $" + BS + "TTL" + BS + "_1 = 200", new_lines)
            self.assertIn("    $" + BS + "TTL" + BS + "_2 = 100", new_lines)
            self.assertIn("    run = CommandList" + BS + "TTL" + BS + "Draw", new_lines)
            self.assertIn("endif", new_lines)
            self.assertLess(shared.index("  [mesh:Body]"), shared.index("  drawindexed = 100, 0, 0"))

    def test_ttl_same_alpha_value_reuses_same_variable(self):
        """FX>>>TTL：相同透明数值复用同一个 $TTLAlpha 变量，[Constants] 只定义一次"""
        with tempfile.TemporaryDirectory() as temp_dir:
            BS = chr(92)
            diffuse_path = os.path.join(temp_dir, "diffuse.png")
            with open(diffuse_path, "wb") as file_obj:
                file_obj.write(b"diffuse")
            obj_a = _FakeObject("BodyA_透明0.75", {}, [("DiffuseMap_衣服", diffuse_path)])
            obj_b = _FakeObject("BodyB_透明0.75", {}, [("DiffuseMap_衣服", diffuse_path)])
            _fake_bpy.data.objects[obj_a.name] = obj_a
            _fake_bpy.data.objects[obj_b.name] = obj_b
            sections = OrderedDict([
                ("[TextureOverride_Shared]", [
                    "hash = 241deac5",
                    "match_first_index = 0",
                    "ib = Resource_Shared_Index",
                    "[mesh:BodyA_透明0.75]",
                    "drawindexed = 100, 0, 0",
                    "[mesh:BodyB_透明0.75]",
                    "drawindexed = 200, 100, 0",
                ]),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.fx_to_ttl = True
            node.process_texture_override_section(
                "[TextureOverride_Shared]", sections, material_group_to_swapkey={},
                swap_key_prefix="$swapkey", next_swap_key_num=150, used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )
            constants = sections.get("[Constants]", [])
            self.assertEqual(constants.count("global $TTLAlpha0_75 = 0.75"), 1)
            alpha_lines = []
            for key, ls in sections.items():
                if key.startswith("[TextureOverrideBody"):
                    alpha_lines.extend(ls)
            self.assertEqual(alpha_lines.count("$" + BS + "TTL" + BS + "alpha = $TTLAlpha0_75"), 2)

    def test_previous_transparency_tail_is_replaced_without_losing_auto_appended_tail(self):
        node = node_postprocess_material.SSMTNode_PostProcess_Material()
        auto_marker = node.AUTO_APPENDED_SECTION_MARKERS[0]
        transparency_name = "CustomShaderTransparencyClothBody_透明0.5"
        content = (
            "[TextureOverride_Test]\n"
            "hash = abc\n"
            "[mesh:Body_透明0.5]\n"
            f"run = {transparency_name}\n\n"
            f"{node.TRANSPARENCY_SECTION_MARKER}\n"
            f"[{transparency_name}]\n"
            "blend = ADD BLEND_FACTOR INV_BLEND_FACTOR\n"
            "handling = skip\n"
            "; --- Start of Overridden Mesh Content ---\n"
            "drawindexed = 3,0,0\n\n"
            f"{auto_marker}\n"
            "[CommandListTail]\n"
            "run = CommandListTail\n"
        )

        stripped = node._strip_previous_transparency_sections(content)

        self.assertNotIn(f"run = {transparency_name}", stripped)
        self.assertNotIn(f"[{transparency_name}]", stripped)
        self.assertIn("drawindexed = 3,0,0", stripped)
        self.assertIn(auto_marker, stripped)
        self.assertIn("[CommandListTail]", stripped)

    def test_ini_parser_preserves_namespace_preamble(self):
        node = node_postprocess_material.SSMTNode_PostProcess_Material()

        preamble, sections = node._parse_ini_content(
            "namespace = Example\\Mod\n; header\n\n[Constants]\nglobal $x = 1\n"
        )

        self.assertEqual(preamble, ["namespace = Example\\Mod", "; header", ""])
        self.assertEqual(sections["[Constants]"], ["global $x = 1"])

    def test_ini_serialization_is_idempotent_with_namespace_preamble(self):
        node = node_postprocess_material.SSMTNode_PostProcess_Material()
        content = "namespace = Example\\Mod\n; header\n\n[Constants]\nglobal $x = 1\n"

        preamble, sections = node._parse_ini_content(content)
        first_output = node._serialize_ini_content(preamble, sections)
        preamble, sections = node._parse_ini_content(first_output)
        second_output = node._serialize_ini_content(preamble, sections)

        self.assertEqual(second_output, first_output)
        self.assertIn("namespace = Example\\Mod\n; header\n\n[Constants]", second_output)


if __name__ == "__main__":
    unittest.main()
