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


PKG = "_ntemi_draw_call_discovery_test_pkg"
for package_name in (PKG, f"{PKG}.ui", f"{PKG}.ui.ntmi_modimp", f"{PKG}.common"):
    package = _install_module(package_name)
    package.__path__ = []

_install_module("bpy", data=types.SimpleNamespace(), types=types.SimpleNamespace(Object=object))
_install_module(
    f"{PKG}.common.import_scene_settings",
    apply_import_render_environment=lambda *_args, **_kwargs: None,
)
_install_module(
    f"{PKG}.ui.ntmi_modimp.runtime_cache",
    MODIMP_COLLECTOR_PROPS=(),
    MODIMP_PATH_PROPS=(),
    localize_runtime_path_props=lambda path_props, _object_workspace_dir: dict(path_props),
    object_workspace_dir_from_type_dir=lambda type_dir: type_dir,
    object_workspace_dir_from_unique=lambda workspace_root, workspace_unique_str: os.path.join(workspace_root, workspace_unique_str),
)
_install_module(
    f"{PKG}.ui.ntmi_modimp.prefix_property_cache",
    replace_prefix_record_props=lambda *_args, **_kwargs: None,
)
_install_module(
    f"{PKG}.ui.ntmi_modimp.texture_slot_refresh",
    build_texture_slots_from_workspace_unique=lambda _workspace_unique_str="": {},
)
_install_module(
    f"{PKG}.ui.ntmi_modimp.modimp_core",
    ensure_mod_importer_package=lambda _configured_root="": None,
    resolve_mod_importer_root=lambda _configured_root="": "",
)

module_path = Path(__file__).resolve().parents[1] / "ui" / "ntmi_modimp" / "ntemi_importer.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.ui.ntmi_modimp.ntemi_importer", module_path)
ntemi_importer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ntemi_importer
spec.loader.exec_module(ntemi_importer)


class NTEMIDrawCallDiscoveryTests(unittest.TestCase):
    def test_discovery_skips_invalid_folder_names_and_finds_first_valid_type_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lod0_dir = os.path.join(temp_dir, "LOD0")
            os.makedirs(lod0_dir, exist_ok=True)

            invalid_dir = os.path.join(lod0_dir, "invalid-folder")
            os.makedirs(os.path.join(invalid_dir, "TYPE_GPU"), exist_ok=True)

            valid_dir = os.path.join(lod0_dir, "abc123-300-7")
            type_a = os.path.join(valid_dir, "TYPE_A")
            type_b = os.path.join(valid_dir, "TYPE_B")
            os.makedirs(type_a, exist_ok=True)
            os.makedirs(type_b, exist_ok=True)
            with open(os.path.join(type_b, "abc123-300-7.json"), "w", encoding="utf-8") as file_obj:
                file_obj.write("{}")

            draw_calls = ntemi_importer._discover_draw_calls(temp_dir, {"abc123": "Alias"})

            self.assertEqual(len(draw_calls), 1)
            draw_call = draw_calls[0]
            self.assertEqual(draw_call.draw_ib, "abc123")
            self.assertEqual(draw_call.index_count, 300)
            self.assertEqual(draw_call.first_index, 7)
            self.assertEqual(draw_call.folder_path, type_b)
            self.assertEqual(draw_call.display_name, "Alias-300-7")

    def test_parse_submesh_folder_name_reports_invalid_numeric_parts(self):
        draw_ib, index_count, first_index, error = ntemi_importer._parse_ntemi_submesh_folder_name("abc-notint-7")

        self.assertIsNone(draw_ib)
        self.assertIsNone(index_count)
        self.assertIsNone(first_index)
        self.assertIn("index_count", error)


if __name__ == "__main__":
    unittest.main()
