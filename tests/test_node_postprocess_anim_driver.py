import importlib.util
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


PKG = "_node_postprocess_anim_driver_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common"):
    package = _install_module(package_name)
    package.__path__ = []


_bpy_props = _install_module(
    "bpy.props",
    StringProperty=lambda **_kwargs: None,
)
_bpy_types = _install_module(
    "bpy.types",
    Node=object,
    NodeSocket=object,
    PropertyGroup=object,
    UIList=object,
    Operator=object,
)
_fake_bpy = _install_module(
    "bpy",
    props=_bpy_props,
    types=_bpy_types,
    utils=types.SimpleNamespace(register_class=lambda _cls: None, unregister_class=lambda _cls: None),
    data=types.SimpleNamespace(node_groups={}),
)

_install_module(f"{PKG}.blueprint.node_base", SSMTNodeBase=object)
_install_module(f"{PKG}.blueprint.node_postprocess_base")
_install_module(
    f"{PKG}.blueprint.node_postprocess_base",
    SSMTNode_PostProcess_Base=type(
        "SSMTNode_PostProcess_Base",
        (),
        {
            "split_auto_appended_tail_content": classmethod(lambda cls, content: (content, "")),
            "_create_cumulative_backup": lambda self, ini_file_path, mod_export_path: None,
        },
    ),
)
_install_module(
    f"{PKG}.blueprint.anim_driver_collector",
    AnimationDriverCollector=type("AnimationDriverCollector", (), {}),
)
_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(
        read_from_main_json_ssmt4=lambda: None,
        path_generate_mod_folder=lambda: "",
        get_workspace_name=lambda: "",
    ),
)


def _load_module(module_name, relative_path):
    module_path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(f"{PKG}.{module_name}", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


module = _load_module("blueprint.node_postprocess_anim_driver", "blueprint/node_postprocess_anim_driver.py")
SSMTNode_PostProcess_AnimDriver = module.SSMTNode_PostProcess_AnimDriver


class _FakeBlueprint:
    def __init__(self, name):
        self.name = name
        self.bl_idname = "SSMTBlueprintTreeType"
        self.nodes = []
        self.links = []
        self._props = {"is_animation_driver": True}

    def get(self, key, default=None):
        return self._props.get(key, default)


class NodePostprocessAnimDriverTests(unittest.TestCase):
    def setUp(self):
        _fake_bpy.data.node_groups = {}

    def test_refresh_exported_anim_driver_section_rewrites_existing_section_only(self):
        blueprint = _FakeBlueprint("AnimBlueprint")
        _fake_bpy.data.node_groups[blueprint.name] = blueprint

        node = SSMTNode_PostProcess_AnimDriver()
        node.blueprint_name = blueprint.name
        node._collect_ini_paragraphs = lambda _blueprint: [{"ini_content": "[Constants]\nglobal $foo = 1"}]

        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = Path(temp_dir) / "test.ini"
            ini_path.write_text(
                "\n".join(
                    [
                        "; --- ANIMATION DRIVER SECTION ---",
                        "old driver section",
                        "; --- END ANIMATION DRIVER SECTION ---",
                        "",
                        "[TextureOverrideA]",
                        "hash = 123",
                    ]
                ),
                encoding="utf-8",
            )

            success, message = node.refresh_exported_anim_driver_section(temp_dir)

            self.assertTrue(success)
            self.assertIn("已刷新", message)
            updated = ini_path.read_text(encoding="utf-8")
            self.assertIn("; --- ANIMATION DRIVER SECTION ---", updated)
            self.assertIn("global $foo = 1", updated)
            self.assertNotIn("old driver section", updated)
            self.assertIn("[TextureOverrideA]", updated)
            self.assertIn("hash = 123", updated)

    def test_refresh_exported_anim_driver_section_can_clear_stale_section(self):
        blueprint = _FakeBlueprint("AnimBlueprint")
        _fake_bpy.data.node_groups[blueprint.name] = blueprint

        node = SSMTNode_PostProcess_AnimDriver()
        node.blueprint_name = blueprint.name
        node._collect_ini_paragraphs = lambda _blueprint: []

        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = Path(temp_dir) / "test.ini"
            ini_path.write_text(
                "\n".join(
                    [
                        "; --- ANIMATION DRIVER SECTION ---",
                        "stale driver section",
                        "; --- END ANIMATION DRIVER SECTION ---",
                        "",
                        "[TextureOverrideA]",
                        "hash = 123",
                    ]
                ),
                encoding="utf-8",
            )

            success, message = node.refresh_exported_anim_driver_section(temp_dir)

            self.assertTrue(success)
            self.assertIn("已清除", message)
            updated = ini_path.read_text(encoding="utf-8")
            self.assertNotIn("; --- ANIMATION DRIVER SECTION ---", updated)
            self.assertNotIn("stale driver section", updated)
            self.assertIn("[TextureOverrideA]", updated)
            self.assertIn("hash = 123", updated)

    def test_refresh_exported_anim_driver_section_skips_backup_when_content_is_unchanged(self):
        blueprint = _FakeBlueprint("AnimBlueprint")
        _fake_bpy.data.node_groups[blueprint.name] = blueprint

        node = SSMTNode_PostProcess_AnimDriver()
        node.blueprint_name = blueprint.name
        node._collect_ini_paragraphs = lambda _blueprint: [{"ini_content": "[Constants]\nglobal $foo = 1"}]
        backup_calls = []
        node._create_cumulative_backup = lambda ini_file_path, mod_export_path: backup_calls.append((ini_file_path, mod_export_path))

        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = Path(temp_dir) / "test.ini"
            ini_path.write_text(
                node._build_ini_content(node._collect_ini_paragraphs(blueprint)),
                encoding="utf-8",
            )

            success, message = node.refresh_exported_anim_driver_section(temp_dir)

            self.assertTrue(success)
            self.assertIn("已是最新状态", message)
            self.assertEqual(backup_calls, [])

    def test_find_target_ini_file_rejects_ambiguous_workspace_matches(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            (Path(temp_dir) / "Workspace_A.ini").write_text("", encoding="utf-8")
            (Path(temp_dir) / "Workspace_B.ini").write_text("", encoding="utf-8")

            original_get_workspace_name = module.GlobalConfig.get_workspace_name
            module.GlobalConfig.get_workspace_name = staticmethod(lambda: "Workspace")
            try:
                ini_path, error_message = SSMTNode_PostProcess_AnimDriver._find_target_ini_file(temp_dir)
            finally:
                module.GlobalConfig.get_workspace_name = original_get_workspace_name

            self.assertEqual(ini_path, "")
            self.assertIn("多个匹配当前工作空间的 INI 文件", error_message)


if __name__ == "__main__":
    unittest.main()
