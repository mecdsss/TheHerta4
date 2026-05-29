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


PKG = "_ntemi_standard_export_blocking_test_pkg"
for package_name in (PKG, f"{PKG}.ui", f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeOperatorBase:
    pass


_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(Operator=_FakeOperatorBase),
    props=types.SimpleNamespace(StringProperty=lambda **_kwargs: None),
    data=types.SimpleNamespace(node_groups=types.SimpleNamespace(get=lambda _name: None)),
)
_install_module("bpy", **_fake_bpy.__dict__)

_install_module(
    f"{PKG}.utils.timer_utils",
    TimerUtils=types.SimpleNamespace(
        start_session=lambda *_args, **_kwargs: None,
        start_stage=lambda *_args, **_kwargs: None,
        end_stage=lambda *_args, **_kwargs: None,
        print_summary=lambda *_args, **_kwargs: None,
    ),
)
_install_module(f"{PKG}.utils.translate_utils", TR=types.SimpleNamespace(translate=lambda text: text))
_install_module(f"{PKG}.utils.command_utils", CommandUtils=types.SimpleNamespace(OpenGeneratedModFolder=lambda: None))
_install_module(
    f"{PKG}.utils.log_utils",
    LOG=types.SimpleNamespace(
        start_collecting=lambda *_args, **_kwargs: None,
        stop_collecting=lambda *_args, **_kwargs: None,
        save_to_text_editor=lambda *_args, **_kwargs: None,
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
    ),
)

_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(
        logic_name="NTEMI",
        read_from_main_json_ssmt4=lambda: None,
        path_generate_mod_folder=lambda: "X:/Mods/Out",
    ),
)
_install_module(f"{PKG}.common.global_key_count_helper", GlobalKeyCountHelper=types.SimpleNamespace(initialize=lambda: None))
_install_module(f"{PKG}.common.global_properties", GlobalProterties=types.SimpleNamespace())
_install_module(
    f"{PKG}.common.logic_name",
    LogicName=types.SimpleNamespace(NTEMI="NTEMI"),
)

_install_module(f"{PKG}.blueprint.model", BluePrintModel=types.SimpleNamespace(clear_object_name_mapping=lambda: None))
_install_module(
    f"{PKG}.blueprint.direct_export",
    execute_direct_export=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("direct export should not run")),
    has_direct_export_mode=lambda _tree: False,
)
_install_module(
    f"{PKG}.blueprint.export_helper",
    BlueprintExportHelper=types.SimpleNamespace(
        get_selected_blueprint_tree=lambda **_kwargs: None,
        get_current_blueprint_tree=lambda **_kwargs: None,
        set_runtime_blueprint_tree=lambda *_args, **_kwargs: None,
        reset_direct_export_runtime_state=lambda *_args, **_kwargs: None,
        has_shapekey_postprocess_node=lambda *_args, **_kwargs: False,
        calculate_max_shapekey_slot_count=lambda *_args, **_kwargs: 0,
        calculate_max_export_count=lambda *_args, **_kwargs: 1,
        has_multi_file_export_nodes=lambda *_args, **_kwargs: False,
        collect_shapekey_objects=lambda *_args, **_kwargs: None,
        multi_file_export_nodes=[],
        runtime_blueprint_tree_name="",
        current_export_index=1,
        get_current_buffer_folder_name=lambda: "",
        set_current_export_index=lambda *_args, **_kwargs: None,
        set_current_buffer_folder_name=lambda *_args, **_kwargs: None,
    ),
)
_install_module(f"{PKG}.blueprint.preprocess", PreProcessHelper=types.SimpleNamespace(cleanup_copies=lambda **_kwargs: None))
_install_module(
    f"{PKG}.blueprint.export_parallel",
    ExportRoundExecutor=types.SimpleNamespace(execute_round=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("standard export should not run"))),
    ParallelExportCoordinator=types.SimpleNamespace(),
    ParallelExportError=RuntimeError,
)
_install_module(f"{PKG}.blueprint.sync", refresh_blueprint_sync_state=lambda **_kwargs: {"tree_count": 0, "updated_count": 0})


module_path = Path(__file__).resolve().parents[1] / "ui" / "ui_func_export.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.ui.ui_func_export", module_path)
ui_func_export = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ui_func_export
spec.loader.exec_module(ui_func_export)


class _FakeNTMIOutputNode:
    bl_idname = "SSMTNode_Result_Output_NTMIModImp"


class _FakeTree:
    def __init__(self, include_ntmi_output: bool):
        self.name = "NTEMI_Blueprint"
        self.nodes = [_FakeNTMIOutputNode()] if include_ntmi_output else []


class _BaseFakeOperator:
    def __init__(self):
        self.reports = []
        self.blueprint_name = ""

    def report(self, level, message):
        self.reports.append((set(level), message))


class NtemiStandardExportBlockingTests(unittest.TestCase):
    def test_generate_mod_blocks_ntemi_and_points_to_modimp_output(self):
        tree = _FakeTree(include_ntmi_output=True)
        ui_func_export.BlueprintExportHelper.get_current_blueprint_tree = lambda **_kwargs: tree

        operator = ui_func_export.SSMTGenerateModBlueprint()
        operator.reports = []
        operator.report = types.MethodType(_BaseFakeOperator.report, operator)

        result = operator.execute(types.SimpleNamespace(scene=types.SimpleNamespace(global_properties=types.SimpleNamespace(selected_blueprint_name=""))))

        self.assertEqual(result, {'CANCELLED'})
        self.assertTrue(operator.reports)
        self.assertIn("NTMI ModImp Output", operator.reports[-1][1])

    def test_quick_export_blocks_ntemi_before_building_temp_tree(self):
        operator = ui_func_export.SSMTQuickExportSelected()
        operator.reports = []
        operator.report = types.MethodType(_BaseFakeOperator.report, operator)

        fake_mesh = types.SimpleNamespace(
            type="MESH",
            name="Mesh",
            as_pointer=lambda: 1,
        )

        result = operator.execute(types.SimpleNamespace(selected_objects=[fake_mesh]))

        self.assertEqual(result, {'CANCELLED'})
        self.assertTrue(operator.reports)
        self.assertIn("NTMI ModImp", operator.reports[-1][1])


if __name__ == "__main__":
    unittest.main()
