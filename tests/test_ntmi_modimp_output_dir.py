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


PKG = "_ntmi_modimp_output_dir_test_pkg"
for package_name in (
    PKG,
    f"{PKG}.blueprint",
    f"{PKG}.common",
    f"{PKG}.utils",
    f"{PKG}.ui",
    f"{PKG}.ui.ntmi_modimp",
):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeLog:
    def __init__(self):
        self.messages = []

    def warning(self, message):
        self.messages.append(("warning", str(message)))

    def info(self, message):
        self.messages.append(("info", str(message)))


_fake_log = _FakeLog()
_fake_bpy = types.SimpleNamespace(
    data=types.SimpleNamespace(filepath=""),
    path=types.SimpleNamespace(abspath=lambda path: path),
    types=types.SimpleNamespace(Object=object),
)

_install_module("bpy", **_fake_bpy.__dict__)
_install_module(f"{PKG}.blueprint.export_helper", BlueprintExportHelper=types.SimpleNamespace())
_install_module(f"{PKG}.blueprint.model", BluePrintModel=type("BluePrintModel", (), {}))
_install_module(f"{PKG}.blueprint.preprocess", PreProcessHelper=types.SimpleNamespace())
_install_module(
    f"{PKG}.blueprint.variable_registry",
    ensure_object_swap_variable_name=lambda _node: None,
    get_node_variable_name=lambda _node: "$swapkey0",
)
_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(
        read_from_main_json_ssmt4=lambda: None,
        path_generate_mod_folder=lambda: r"E:\SSMT4\Mods\SSMTGeneratedMod\char_1\\",
    ),
)
_install_module(
    f"{PKG}.common.global_key_count_helper",
    GlobalKeyCountHelper=types.SimpleNamespace(initialize=lambda: None),
)
_install_module(f"{PKG}.common.global_properties", GlobalProterties=types.SimpleNamespace())
_install_module(
    f"{PKG}.common.object_prefix_helper",
    ObjectPrefixHelper=types.SimpleNamespace(
        extract_prefix_info=lambda _name: None,
        parse_prefix_parts=lambda _prefix: {},
    ),
)
_install_module(f"{PKG}.common.workspace_helper", WorkSpaceHelper=types.SimpleNamespace())
_install_module(f"{PKG}.utils.log_utils", LOG=_fake_log)
_install_module(f"{PKG}.utils.timer_utils", TimerUtils=types.SimpleNamespace())
_install_module(
    f"{PKG}.ui.ntmi_modimp.export_tree_builder",
    ExportTreeBuildResult=type("ExportTreeBuildResult", (), {}),
    collect_object_conditions=lambda _source: {},
    build_export_tree=lambda _source: None,
    cleanup_collections=lambda _names: None,
    condition_from_swap_work_keys=lambda _keys: "",
)
_install_module(
    f"{PKG}.ui.ntmi_modimp.ini_swap_patcher",
    ACTIVE_FLAG="$active0",
    patch_ini_file=lambda *args, **kwargs: None,
)
_install_module(
    f"{PKG}.ui.ntmi_modimp.modimp_core",
    detect_mod_importer_dependency=lambda _root="": types.SimpleNamespace(available=True, checked_paths=[]),
    get_export_collection_package=lambda _root="": lambda **kwargs: kwargs,
    resolve_mod_importer_root=lambda _root="": _root,
)
_install_module(
    f"{PKG}.ui.ntmi_modimp.texture_slot_refresh",
    refresh_texture_slots_for_objects=lambda _objects: None,
)
_install_module(
    f"{PKG}.blueprint.ntmi_multifile",
    execute_ntmi_multifile_postprocess=lambda **kwargs: None,
)
_install_module(
    f"{PKG}.blueprint.ntmi_shapekey",
    execute_ntmi_shapekey_postprocess=lambda **kwargs: None,
)


module_path = Path(__file__).resolve().parents[1] / "blueprint" / "ntmi_export_modimp.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.ntmi_export_modimp", module_path)
ntmi_export_modimp = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ntmi_export_modimp
spec.loader.exec_module(ntmi_export_modimp)


class NTMIModImpOutputDirTests(unittest.TestCase):
    def setUp(self):
        _fake_log.messages.clear()
        _fake_bpy.data.filepath = ""

    def test_default_output_dir_uses_ssmt_generate_mod_folder(self):
        node = types.SimpleNamespace(use_custom_export_dir=False, export_dir="")

        resolved = ntmi_export_modimp.resolve_ntmi_modimp_output_dir(node)

        self.assertEqual(
            resolved,
            ntmi_export_modimp.os.path.normpath(r"E:\SSMT4\Mods\SSMTGeneratedMod\char_1\\"),
        )

    def test_empty_manual_dir_falls_back_to_ssmt_output_dir(self):
        node = types.SimpleNamespace(use_custom_export_dir=True, export_dir="")

        resolved = ntmi_export_modimp.resolve_ntmi_modimp_output_dir(node)

        self.assertEqual(
            resolved,
            ntmi_export_modimp.os.path.normpath(r"E:\SSMT4\Mods\SSMTGeneratedMod\char_1\\"),
        )
        self.assertTrue(any(level == "warning" for level, _message in _fake_log.messages))

    def test_manual_dir_still_takes_precedence(self):
        node = types.SimpleNamespace(use_custom_export_dir=True, export_dir=r"D:\Manual\Out")

        resolved = ntmi_export_modimp.resolve_ntmi_modimp_output_dir(node)

        self.assertEqual(resolved, ntmi_export_modimp.os.path.normpath(r"D:\Manual\Out"))

    def test_execute_supported_postprocess_nodes_runs_anim_driver_in_chain_order(self):
        calls = []

        shape_node = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_ShapeKey",
            name="ShapeKey",
            execute_postprocess=lambda _output_dir: calls.append(("shape_execute", _output_dir)),
        )
        anim_node = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_AnimDriver",
            name="AnimDriver",
            execute_postprocess=lambda output_dir: calls.append(("anim_execute", output_dir)),
        )

        blueprint_model = types.SimpleNamespace(
            postprocess_nodes=[shape_node, anim_node],
            multi_file_export_nodes=[],
        )

        original_mapping = getattr(ntmi_export_modimp.BluePrintModel, "_object_name_mapping", None)
        ntmi_export_modimp.BluePrintModel._object_name_mapping = {}

        original_shapekey = ntmi_export_modimp.execute_ntmi_shapekey_postprocess
        ntmi_export_modimp.execute_ntmi_shapekey_postprocess = lambda **kwargs: calls.append(
            ("shape_special", kwargs["output_dir"])
        )
        try:
            ntmi_export_modimp._execute_supported_postprocess_nodes(
                blueprint_model=blueprint_model,
                output_dir="E:/Out",
                exporter=None,
            )
        finally:
            ntmi_export_modimp.execute_ntmi_shapekey_postprocess = original_shapekey
            if original_mapping is None:
                delattr(ntmi_export_modimp.BluePrintModel, "_object_name_mapping")
            else:
                ntmi_export_modimp.BluePrintModel._object_name_mapping = original_mapping

        self.assertEqual(
            calls,
            [
                ("shape_special", "E:/Out"),
                ("anim_execute", "E:/Out"),
            ],
        )

    def test_execute_supported_postprocess_nodes_passes_exporter_to_material_node_only(self):
        calls = []
        exporter = types.SimpleNamespace(extra_ps_t2_diffuse_map=True)

        material_node = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_Material",
            name="Material",
            execute_postprocess=lambda output_dir, exporter=None: calls.append(
                ("material", output_dir, getattr(exporter, "extra_ps_t2_diffuse_map", None))
            ),
        )
        cleanup_node = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_BufferCleanup",
            name="Cleanup",
            execute_postprocess=lambda output_dir: calls.append(("cleanup", output_dir)),
        )

        blueprint_model = types.SimpleNamespace(
            postprocess_nodes=[material_node, cleanup_node],
            multi_file_export_nodes=[],
        )

        original_mapping = getattr(ntmi_export_modimp.BluePrintModel, "_object_name_mapping", None)
        ntmi_export_modimp.BluePrintModel._object_name_mapping = {}
        try:
            ntmi_export_modimp._execute_supported_postprocess_nodes(
                blueprint_model=blueprint_model,
                output_dir="E:/Out",
                exporter=exporter,
            )
        finally:
            if original_mapping is None:
                delattr(ntmi_export_modimp.BluePrintModel, "_object_name_mapping")
            else:
                ntmi_export_modimp.BluePrintModel._object_name_mapping = original_mapping

        self.assertEqual(
            calls,
            [
                ("material", "E:/Out", True),
                ("cleanup", "E:/Out"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
