import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _install_module(name, **attrs):
    """安装 Fake 模块到 sys.modules"""
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load_module(module_name: str, file_path: Path):
    """从文件路径加载模块"""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _BaseExportStub:
    """导出器测试桩基类"""
    export_calls = 0
    buffer_calls = 0

    def __init__(self, blueprint_model=None):
        self.blueprint_model = blueprint_model

    @classmethod
    def reset(cls):
        cls.export_calls = 0
        cls.buffer_calls = 0

    def export(self):
        type(self).export_calls += 1

    def export_buffers_only(self):
        type(self).buffer_calls += 1


class _ExportEFMI(_BaseExportStub):
    pass


class _ExportGIMI(_BaseExportStub):
    pass


class _ExportHIMI(_BaseExportStub):
    pass


class _ExportIdentityV(_BaseExportStub):
    pass


class _ExportSnowBreak(_BaseExportStub):
    pass


class _ExportSRMI(_BaseExportStub):
    pass


class _ExportUnity(_BaseExportStub):
    pass


class _ExportWWMI(_BaseExportStub):
    pass


class _ExportYYSLS(_BaseExportStub):
    pass


class _ExportZZMI(_BaseExportStub):
    pass


class ExportLogicNameHTMITests(unittest.TestCase):
    """测试 HTMI 逻辑名称下的导出路由：HTMI 应路由到 EFMI 导出器"""

    def setUp(self):
        """每个测试前重置所有导出器桩的调用计数"""
        for cls in (
            _ExportEFMI,
            _ExportGIMI,
            _ExportHIMI,
            _ExportIdentityV,
            _ExportSnowBreak,
            _ExportSRMI,
            _ExportUnity,
            _ExportWWMI,
            _ExportYYSLS,
            _ExportZZMI,
        ):
            cls.reset()

    def test_parallel_export_routes_htmi_to_efmi_exporter(self):
        """测试并行导出模式下 HTMI 被路由到 EFMI 导出器"""
        pkg = "_export_parallel_htmi_test_pkg"
        for package_name in (pkg, f"{pkg}.blueprint", f"{pkg}.common", f"{pkg}.ui", f"{pkg}.ui.universal", f"{pkg}.ui.wwmi", f"{pkg}.utils"):
            package = _install_module(package_name)
            package.__path__ = []

        _install_module("bpy", context=types.SimpleNamespace())
        _install_module(f"{pkg}.common.global_config", GlobalConfig=types.SimpleNamespace(logic_name="HTMI"))
        _install_module(
            f"{pkg}.common.global_properties",
            GlobalProterties=types.SimpleNamespace(
                enable_parallel_preprocess=lambda: False,
                enable_parallel_export_rounds=lambda: False,
            ),
        )
        _install_module(
            f"{pkg}.common.logic_name",
            LogicName=types.SimpleNamespace(
                EFMI="EFMI",
                HTMI="HTMI",
                GIMI="GIMI",
                HIMI="HIMI",
                IdentityV="IdentityV",
                SRMI="SRMI",
                ZZMI="ZZMI",
                WWMI="WWMI",
                NTEMI="NTEMI",
                SnowBreak="SnowBreak",
                YYSLS="YYSLS",
                Naraka="Naraka",
                NarakaM="NarakaM",
                GF2="GF2",
                AILIMIT="AILIMIT",
            ),
        )
        _install_module(f"{pkg}.utils.log_utils", LOG=types.SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None))
        _install_module(f"{pkg}.utils.shapekey_utils", ShapeKeyUtils=types.SimpleNamespace())
        _install_module(f"{pkg}.utils.timer_utils", TimerUtils=types.SimpleNamespace(start_stage=lambda *args, **kwargs: None, end_stage=lambda *args, **kwargs: None))
        _install_module(f"{pkg}.blueprint.model", BluePrintModel=types.SimpleNamespace(clear_object_name_mapping=lambda: None))
        _install_module(
            f"{pkg}.blueprint.export_helper",
            BlueprintExportHelper=types.SimpleNamespace(
                set_runtime_blueprint_tree=lambda *args, **kwargs: None,
                calculate_max_export_count=lambda *args, **kwargs: None,
                collect_connected_preprocess_object_names=lambda *args, **kwargs: [],
                collect_shapekey_objects=lambda *args, **kwargs: None,
                set_all_shapekey_values=lambda *args, **kwargs: None,
                set_current_buffer_folder_name=lambda *args, **kwargs: None,
                set_current_export_index=lambda *args, **kwargs: None,
                get_multi_file_export_object_info=lambda *args, **kwargs: {},
                generate_shapekey_classification_report=lambda *args, **kwargs: None,
                get_node_from_bl_idname=lambda *args, **kwargs: None,
                _is_node_connected_to_output=lambda *args, **kwargs: True,
            ),
        )
        _install_module(
            f"{pkg}.blueprint.preprocess",
            PreProcessHelper=types.SimpleNamespace(
                recover_blueprint_node_references=lambda *args, **kwargs: None,
                execute_preprocess=lambda *args, **kwargs: {},
                update_blueprint_node_references=lambda *args, **kwargs: None,
                cleanup_copies=lambda *args, **kwargs: None,
                has_copies=lambda: False,
                original_to_copy_map={},
                validate_copy_suffix=lambda *_args, **_kwargs: False,
            ),
        )
        _install_module(f"{pkg}.blueprint.preprocess_cache", PreProcessCache=types.SimpleNamespace())
        _install_module(f"{pkg}.blueprint.preprocess_parallel", ParallelPreprocessCoordinator=types.SimpleNamespace())
        _install_module(f"{pkg}.ui.universal.efmi", ExportEFMI=_ExportEFMI)
        _install_module(f"{pkg}.ui.universal.gimi", ExportGIMI=_ExportGIMI)
        _install_module(f"{pkg}.ui.universal.himi", ExportHIMI=_ExportHIMI)
        _install_module(f"{pkg}.ui.universal.identityv", ExportIdentityV=_ExportIdentityV)
        _install_module(f"{pkg}.ui.universal.snowbreak", ExportSnowBreak=_ExportSnowBreak)
        _install_module(f"{pkg}.ui.universal.srmi", ExportSRMI=_ExportSRMI)
        _install_module(f"{pkg}.ui.universal.unity", ExportUnity=_ExportUnity)
        _install_module(f"{pkg}.ui.wwmi.wwmi_export", ExportWWMI=_ExportWWMI)
        _install_module(f"{pkg}.ui.universal.yysls", ExportYYSLS=_ExportYYSLS)
        _install_module(f"{pkg}.ui.universal.zzmi", ExportZZMI=_ExportZZMI)

        module = _load_module(
            f"{pkg}.blueprint.export_parallel",
            Path(__file__).resolve().parents[1] / "blueprint" / "export_parallel.py",
        )

        module.ExportRoundExecutor.export_with_ini(object())
        module.ExportRoundExecutor.export_buffers_only(object())

        self.assertEqual(_ExportEFMI.export_calls, 1)
        self.assertEqual(_ExportEFMI.buffer_calls, 1)
        self.assertEqual(_ExportHIMI.export_calls, 0)
        self.assertEqual(_ExportUnity.export_calls, 0)

    def test_direct_export_routes_htmi_to_efmi_exporter(self):
        """测试直接导出模式下 HTMI 被路由到 EFMI 导出器"""
        pkg = "_direct_export_htmi_test_pkg"
        for package_name in (pkg, f"{pkg}.blueprint", f"{pkg}.common", f"{pkg}.ui", f"{pkg}.ui.universal", f"{pkg}.ui.wwmi", f"{pkg}.utils"):
            package = _install_module(package_name)
            package.__path__ = []

        _install_module(
            "bpy",
            types=types.SimpleNamespace(Operator=object, Node=object),
            props=types.SimpleNamespace(StringProperty=lambda **_kwargs: None),
        )
        _install_module(f"{pkg}.common.global_config", GlobalConfig=types.SimpleNamespace(logic_name="HTMI"))
        _install_module(
            f"{pkg}.common.global_properties",
            GlobalProterties=types.SimpleNamespace(enable_parallel_preprocess=lambda: False),
        )
        _install_module(
            f"{pkg}.common.logic_name",
            LogicName=types.SimpleNamespace(
                EFMI="EFMI",
                HTMI="HTMI",
                NTEMI="NTEMI",
            ),
        )
        _install_module(f"{pkg}.common.global_key_count_helper", GlobalKeyCountHelper=types.SimpleNamespace(initialize=lambda: None))
        _install_module(f"{pkg}.utils.log_utils", LOG=types.SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None, start_collecting=lambda: None, stop_collecting=lambda: None))
        _install_module(f"{pkg}.utils.timer_utils", TimerUtils=types.SimpleNamespace(start_session=lambda *args, **kwargs: None, print_summary=lambda: None))
        _install_module(f"{pkg}.blueprint.direct_export_multifile", DirectMultiFileGenerator=type("DirectMultiFileGenerator", (), {}))
        _install_module(f"{pkg}.blueprint.direct_export_shapekey", DirectShapeKeyGenerator=type("DirectShapeKeyGenerator", (), {}))
        _install_module(
            f"{pkg}.blueprint.export_helper",
            BlueprintExportHelper=types.SimpleNamespace(
                get_current_blueprint_tree=lambda **_kwargs: None,
                collect_shapekey_postprocess_nodes=lambda *_args, **_kwargs: [],
                collect_multi_file_export_nodes=lambda *_args, **_kwargs: [],
                collect_connected_preprocess_object_names=lambda *_args, **_kwargs: [],
                reset_direct_export_runtime_state=lambda *args, **kwargs: None,
                set_runtime_blueprint_tree=lambda *args, **kwargs: None,
                _collect_postprocess_nodes=lambda *_args, **_kwargs: [],
                collect_shapekey_objects=lambda *_args, **_kwargs: None,
                set_all_shapekey_values=lambda *args, **kwargs: None,
                set_current_export_index=lambda *args, **kwargs: None,
            ),
        )
        _install_module(f"{pkg}.blueprint.export_parallel", ExportRoundExecutor=types.SimpleNamespace(), ParallelExportError=RuntimeError)
        _install_module(f"{pkg}.blueprint.model", BluePrintModel=types.SimpleNamespace())
        _install_module(f"{pkg}.blueprint.preprocess", PreProcessHelper=types.SimpleNamespace(collect_target_object_names_strict=lambda names: names))
        _install_module(f"{pkg}.blueprint.preprocess_parallel", ParallelPreprocessCoordinator=types.SimpleNamespace())
        _install_module(f"{pkg}.blueprint.sync", refresh_blueprint_sync_state=lambda *args, **kwargs: None)
        _install_module(f"{pkg}.ui.universal.efmi", ExportEFMI=_ExportEFMI)
        _install_module(f"{pkg}.ui.universal.gimi", ExportGIMI=_ExportGIMI)
        _install_module(f"{pkg}.ui.universal.himi", ExportHIMI=_ExportHIMI)
        _install_module(f"{pkg}.ui.universal.identityv", ExportIdentityV=_ExportIdentityV)
        _install_module(f"{pkg}.ui.universal.snowbreak", ExportSnowBreak=_ExportSnowBreak)
        _install_module(f"{pkg}.ui.universal.srmi", ExportSRMI=_ExportSRMI)
        _install_module(f"{pkg}.ui.universal.unity", ExportUnity=_ExportUnity)
        _install_module(f"{pkg}.ui.wwmi.wwmi_export", ExportWWMI=_ExportWWMI)
        _install_module(f"{pkg}.ui.universal.yysls", ExportYYSLS=_ExportYYSLS)
        _install_module(f"{pkg}.ui.universal.zzmi", ExportZZMI=_ExportZZMI)

        module = _load_module(
            f"{pkg}.blueprint.direct_export",
            Path(__file__).resolve().parents[1] / "blueprint" / "direct_export.py",
        )

        exporter = module._build_exporter(object())

        self.assertIsInstance(exporter, _ExportEFMI)
        self.assertEqual(_ExportHIMI.export_calls, 0)


if __name__ == "__main__":
    unittest.main()
