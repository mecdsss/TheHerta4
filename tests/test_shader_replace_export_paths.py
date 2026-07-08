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


def _load_module(module_name, relative_path):
    module_path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeIniSection:
    def __init__(self, section_type):
        self.SectionType = section_type
        self.SectionName = ""
        self.SectionLineList = []

    def append(self, line):
        self.SectionLineList.append(line)

    def new_line(self):
        self.SectionLineList.append("")

    def empty(self):
        return not any(line not in ("", "\n") for line in self.SectionLineList)


class _FakeIniBuilder:
    def __init__(self):
        self.ini_section_list = []
        self.saved_path = None

    def append_section(self, section):
        self.ini_section_list.append(section)

    def save_to_file(self, path):
        self.saved_path = path


class _FakeSectionType:
    TextureOverrideIB = "TextureOverrideIB"
    TextureOverrideVB = "TextureOverrideVB"
    ResourceBuffer = "ResourceBuffer"
    ResourceTexture = "ResourceTexture"
    Constants = "Constants"
    Key = "Key"
    Present = "Present"
    ShaderReplace = "ShaderReplace"


class _FakeExportUnity:
    def __init__(self, blueprint_model):
        self.blueprint_model = blueprint_model
        self.drawib_model_list = []

    def add_unity_vs_texture_override_vlr_section(self, ini_builder, drawib_model):
        return None

    def add_unity_vs_texture_override_vb_sections(self, ini_builder, drawib_model):
        return None

    def add_unity_vs_resource_vb_sections(self, ini_builder, drawib_model):
        return None

    def add_resource_texture_sections(self, ini_builder, drawib_model):
        return None

    def generate_buffer_files(self, *_args, **_kwargs):
        return None


class _FakeDrawCall:
    def __init__(self, obj_name, *, condition=""):
        self.obj_name = obj_name
        self.vertex_count = 77
        self.index_count = 12
        self.index_offset = 34
        self.match_draw_ib = "drawhash"
        self.match_first_index = "56"
        self._condition = condition

    def get_condition_str(self):
        return self._condition

    def get_drawindexed_str(self, _offset_dict=None):
        return "drawindexed = 12,34,0"

    def get_drawindexed_instanced_str(self, _offset_dict=None):
        return "drawindexedinstanced = 12,INSTANCE_COUNT,34,0,FIRST_INSTANCE"


class _FakeSubmesh:
    def __init__(self, drawcalls):
        self.unique_str = "drawhash-12-56"
        self.match_draw_ib = "drawhash"
        self.match_first_index = "56"
        self.match_index_count = "12"
        self.drawcall_model_list = drawcalls
        self.category_buffer_dict = {"Position": [1], "Texcoord": [2], "Blend": [3]}
        self.d3d11_game_type = types.SimpleNamespace(
            CategoryExtractSlotDict={"Position": "vb0", "Texcoord": "vb1", "Blend": "vb2"},
            CategoryStrideDict={"Position": 40, "Texcoord": 8, "Blend": 32},
        )


class _FakeDrawIBModel:
    def __init__(self, drawcalls):
        self.draw_ib = "drawhash"
        self.draw_ib_alias = "drawhash"
        self.draw_number = 123
        self.d3d11GameType = types.SimpleNamespace(
            OrderedCategoryNameList=["Position", "Texcoord", "Blend"],
            CategoryDrawCategoryDict={"Position": "Position", "Texcoord": "Texcoord", "Blend": "Blend"},
            CategoryExtractSlotDict={"Position": "vb0", "Texcoord": "vb1", "Blend": "vb2"},
            CategoryStrideDict={"Position": 40, "Texcoord": 8, "Blend": 32},
        )
        self.category_hash_dict = {"Position": "p", "Texcoord": "t", "Blend": "b"}
        self.submesh_model_list = [_FakeSubmesh(drawcalls)]
        self.submesh_ib_dict = {"drawhash-12-56": [0, 1, 2]}
        self.obj_name_draw_offset = {}
        self.d3d11_game_type = self.submesh_model_list[0].d3d11_game_type

    def get_submesh_texture_override_suffix(self, submesh_model):
        return submesh_model.unique_str.replace("-", "_")

    def get_submesh_ib_resource_name(self, submesh_model):
        return f"Resource_{submesh_model.unique_str.replace('-', '_')}_Index"

    def get_submesh_texture_markup_info_list(self, _submesh_model):
        return []


class ShaderReplaceExportPathTests(unittest.TestCase):
    def setUp(self):
        self.pkg = "_shader_replace_export_path_test_pkg"
        for package_name in (
            self.pkg,
            f"{self.pkg}.ui",
            f"{self.pkg}.ui.universal",
            f"{self.pkg}.common",
            f"{self.pkg}.utils",
            f"{self.pkg}.blueprint",
        ):
            package = _install_module(package_name)
            package.__path__ = []

        _install_module(
            f"{self.pkg}.common.global_config",
            GlobalConfig=types.SimpleNamespace(
                path_generate_mod_folder=lambda: "E:/mod",
                path_generatemod_buffer_folder=lambda: "E:/mod/Buffer/",
                get_workspace_name=lambda: "Workspace",
            ),
        )
        _install_module(
            f"{self.pkg}.common.global_properties",
            GlobalProterties=types.SimpleNamespace(
                forbid_auto_texture_ini=lambda: True,
                generate_branch_mod_gui=lambda: False,
                use_rabbitfx_slot=lambda: False,
            ),
        )
        _install_module(
            f"{self.pkg}.common.global_key_count_helper",
            GlobalKeyCountHelper=types.SimpleNamespace(generated_mod_number=0),
        )
        _install_module(
            f"{self.pkg}.common.m_ini_builder",
            M_IniBuilder=_FakeIniBuilder,
            M_IniSection=_FakeIniSection,
            M_SectionType=_FakeSectionType,
        )

        self.shader_replace_section_calls = []

        def _record_shader_replace_sections(**kwargs):
            self.shader_replace_section_calls.append(kwargs)

        _install_module(
            f"{self.pkg}.common.m_ini_helper",
            M_IniHelper=types.SimpleNamespace(
                get_drawindexed_str_list=lambda drawcall_list, obj_name_draw_offset_dict=None: [
                    f"drawindexed = {item.obj_name}" for item in drawcall_list
                ],
                get_drawindexed_instanced_str_list=lambda drawcall_list, obj_name_draw_offset_dict=None: [
                    f"drawindexedinstanced = {item.obj_name}" for item in drawcall_list
                ],
                get_shader_replace_run_logic=lambda info, ib_hash, first_index, component, index_count, index_offset, base_vertex=0: [
                    f"if ${info['name_prefix']}_ps_replace == 1",
                    f"    run = CustomShader_{info['name_prefix']}_{ib_hash}_{first_index}_{component}_{index_count}_{index_offset}_{base_vertex}_World",
                    "else",
                    f"    run = CustomShader_{info['name_prefix']}_{ib_hash}_{first_index}_{component}_{index_count}_{index_offset}_{base_vertex}_Normal",
                    "endif",
                ],
                add_shader_replace_sections=_record_shader_replace_sections,
                generate_hash_style_texture_ini=lambda **_kwargs: None,
                generate_shared_slot_style_texture_ini=lambda **_kwargs: None,
                move_slot_style_textures=lambda **_kwargs: None,
                add_branch_key_sections=lambda **_kwargs: None,
                add_shapekey_ini_sections=lambda **_kwargs: None,
                is_slot_binding_mark_type=lambda _mark_type: False,
            ),
        )
        _install_module(
            f"{self.pkg}.common.m_ini_helper_gui",
            M_IniHelperGUI=types.SimpleNamespace(add_branch_mod_gui_section=lambda **_kwargs: None),
        )
        _install_module(f"{self.pkg}.common.drawib_model", DrawIBModel=object)
        _install_module(f"{self.pkg}.common.submesh_model", SubMeshModel=object)
        _install_module(f"{self.pkg}.blueprint.model", BluePrintModel=object)
        _install_module(
            f"{self.pkg}.blueprint.export_helper",
            BlueprintExportHelper=types.SimpleNamespace(get_current_buffer_folder_name=lambda: "Buffer"),
        )
        _install_module(
            f"{self.pkg}.ui.universal.export_helper",
            ExportHelper=types.SimpleNamespace(parse_submesh_model_list_from_blueprint_model=lambda _bp: []),
        )
        _install_module(
            f"{self.pkg}.utils.timer_utils",
            TimerUtils=types.SimpleNamespace(start_stage=lambda *_args, **_kwargs: None, end_stage=lambda *_args, **_kwargs: None),
        )
        _install_module(f"{self.pkg}.common.buffer_export_helper", BufferExportHelper=types.SimpleNamespace())
        _install_module(f"{self.pkg}.ui.universal.unity", ExportUnity=_FakeExportUnity)
        _install_module("bpy", context=types.SimpleNamespace())

        self.zzmi_module = _load_module(
            f"{self.pkg}.ui.universal.zzmi",
            "ui/universal/zzmi.py",
        )
        self.efmi_module = _load_module(
            f"{self.pkg}.ui.universal.efmi",
            "ui/universal/efmi.py",
        )

    def _make_blueprint_model(self):
        return types.SimpleNamespace(
            cross_ib_info_dict={},
            cross_ib_method_dict={},
            cross_ib_mapping_method={},
            has_cross_ib=False,
            cross_ib_object_names=set(),
            cross_ib_mapping_objects={},
            cross_ib_vb_condition_mapping={},
            cross_ib_source_to_target_dict={},
            cross_ib_object_vb_condition={},
            cross_ib_target_info={},
            cross_ib_match_mode="IB_HASH",
            shader_replace_info_list=[{
                "name_prefix": "Rain",
                "toggle_key": "VK_F5",
                "component_index": 0,
                "shaders": [{"variant_name": "World", "shader_hash": "abcd", "env_value": 1, "shader_file_path": ""}],
            }],
            shader_replace_object_names={"mesh_sr"},
            shader_replace_object_info_map={
                "mesh_sr": [{
                    "name_prefix": "Rain",
                    "toggle_key": "VK_F5",
                    "component_index": 0,
                    "shaders": [{"variant_name": "World", "shader_hash": "abcd", "env_value": 1, "shader_file_path": ""}],
                }]
            },
            has_shader_replace=True,
            keyname_mkey_dict={},
            ordered_draw_obj_data_model_list=[_FakeDrawCall("mesh_sr"), _FakeDrawCall("mesh_normal")],
        )

    def test_zzmi_shader_replace_replaces_drawindexed_for_marked_objects(self):
        exporter = self.zzmi_module.ExportZZMI(self._make_blueprint_model())
        section = _FakeIniSection(_FakeSectionType.TextureOverrideIB)

        exporter._append_drawindexed_with_shader_replace(
            section,
            [_FakeDrawCall("mesh_sr"), _FakeDrawCall("mesh_normal")],
            {},
        )

        lines = section.SectionLineList
        self.assertIn("drawindexed = mesh_normal", lines)
        self.assertTrue(any("run = CustomShader_Rain_drawhash_56_0_12_34_0_World" in line for line in lines))
        self.assertFalse(any(line == "drawindexed = mesh_sr" for line in lines))

    def test_efmi_shader_replace_replaces_drawindexedinstanced_for_marked_objects(self):
        exporter = self.efmi_module.ExportEFMI.__new__(self.efmi_module.ExportEFMI)
        exporter.blueprint_model = self._make_blueprint_model()
        exporter.shader_replace_info_list = exporter.blueprint_model.shader_replace_info_list
        exporter.shader_replace_object_names = exporter.blueprint_model.shader_replace_object_names
        exporter.shader_replace_object_info_map = exporter.blueprint_model.shader_replace_object_info_map
        exporter.has_shader_replace = True

        section = _FakeIniSection(_FakeSectionType.TextureOverrideIB)
        exporter._append_drawindexed_instanced_with_shader_replace(
            section,
            [_FakeDrawCall("mesh_sr"), _FakeDrawCall("mesh_normal")],
            {},
        )

        lines = section.SectionLineList
        self.assertIn("drawindexedinstanced = mesh_normal", lines)
        self.assertTrue(any("run = CustomShader_Rain_drawhash_56_0_12_34_0_World" in line for line in lines))
        self.assertFalse(any(line == "drawindexedinstanced = mesh_sr" for line in lines))

    def test_efmi_generate_ini_file_emits_shader_replace_sections(self):
        exporter = self.efmi_module.ExportEFMI.__new__(self.efmi_module.ExportEFMI)
        blueprint_model = self._make_blueprint_model()
        exporter.blueprint_model = blueprint_model
        exporter.submesh_model_list = []
        exporter.drawib_model_list = []
        exporter.cross_ib_info_dict = {}
        exporter.cross_ib_method_dict = {}
        exporter.has_cross_ib = False
        exporter.cross_ib_mapping_objects = {}
        exporter.cross_ib_vb_condition_mapping = {}
        exporter.cross_ib_source_to_target_dict = {}
        exporter.cross_ib_object_vb_condition = {}
        exporter.cross_ib_target_info = {}
        exporter.cross_ib_match_mode = "IB_HASH"
        exporter.cross_ib_object_names = set()
        exporter.shader_replace_info_list = blueprint_model.shader_replace_info_list
        exporter.shader_replace_object_names = blueprint_model.shader_replace_object_names
        exporter.shader_replace_object_info_map = blueprint_model.shader_replace_object_info_map
        exporter.has_shader_replace = True
        exporter._integrate_object_swap_ini_hook = lambda _ini_builder: None

        exporter.generate_ini_file()

        self.assertEqual(len(self.shader_replace_section_calls), 1)
        call = self.shader_replace_section_calls[0]
        self.assertEqual(call["shader_replace_info_list"], blueprint_model.shader_replace_info_list)
        self.assertEqual(call["shader_replace_object_names"], blueprint_model.shader_replace_object_names)
        self.assertEqual(call["draw_call_models"], blueprint_model.ordered_draw_obj_data_model_list)
        self.assertTrue(call["use_instanced_draw"])

if __name__ == "__main__":
    unittest.main()
