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
            self.assertIn("ps-t0 = Resource_DiffuseMap_琛ｆ湇00", override_lines)
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
            self.assertIn("ps-t7 = Resource_DiffuseMap_衣服02", override_lines)
            self.assertIn("ps-t2 = Resource_DiffuseMap_衣服02", override_lines)

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
            self.assertEqual(joined.count("ps-t5 = Resource_NormalMap_DiffuseMap_衣服03"), 1)
            self.assertEqual(joined.count("ps-t7 = Resource_DiffuseMap_衣服03"), 1)
            self.assertEqual(joined.count("ps-t2 = Resource_DiffuseMap_衣服03"), 1)
            self.assertEqual(joined.count("ps-t8 = Resource_LightMap_LOD0.14076dfb-111345-159309.切断器.001"), 1)
            self.assertEqual(joined.count("ps-t18 = Resource_HighLightMap_LOD0.14076dfb-111345-159309.切断器.001"), 1)

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
