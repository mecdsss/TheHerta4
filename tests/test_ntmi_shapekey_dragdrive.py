import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    module.__path__ = []
    module.__package__ = name
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_ntmi_sk_drive_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils", f"{PKG}.ui"):
    _install_module(package_name)

_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(PropertyGroup=object, Operator=object, UIList=object, Node=object),
    props=types.SimpleNamespace(
        StringProperty=lambda **_kw: None,
        BoolProperty=lambda **_kw: None,
        IntProperty=lambda **_kw: None,
        FloatProperty=lambda **_kw: None,
        EnumProperty=lambda **_kw: None,
        CollectionProperty=lambda **_kw: None,
        PointerProperty=lambda **_kw: None,
    ),
    data=types.SimpleNamespace(objects={}, texts=[], node_groups=types.SimpleNamespace(nodes=[])),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module("bpy.props", **_fake_bpy.props.__dict__)
_install_module(
    "bpy.types",
    PropertyGroup=object,
    Operator=object,
    UIList=object,
    Node=object,
    NodeSocket=object,
)
_install_module("bpy.data", **_fake_bpy.data.__dict__)

_install_module(
    f"{PKG}.blueprint.direct_export_shapekey",
    DirectShapeKeyGenerator=object,
)
_install_module(
    f"{PKG}.blueprint.direct_export_shapekey_shared",
    ShapeKeyDirectExportError=Exception,
    _buffer_to_bytes=lambda b: b,
)
_install_module(
    f"{PKG}.common.d3d11_gametype",
    D3D11GameType=object,
)
_install_module(
    f"{PKG}.utils.log_utils",
    LOG=types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
)
_install_module(
    f"{PKG}.blueprint.ntmi_layout_adapter",
    iter_name_variants=lambda name: [name],
    local_loop_indices_for_export_range=lambda *a, **k: [],
    parse_ntmi_part_layouts=lambda *a, **k: {},
)
_install_module(
    f"{PKG}.ui.ntmi_modimp.modimp_core",
    ensure_mod_importer_package=lambda *a, **k: None,
)
_install_module(
    f"{PKG}.ui.ntmi_modimp.ntemi_importer",
    _ensure_ntemi_game_data_converter=lambda *a, **k: None,
)
_install_module(
    f"{PKG}.blueprint.direct_export_runtime_utils",
    normalize_runtime_name=lambda s: str(s or ""),
)
_install_module(
    f"{PKG}.blueprint.direct_export_shapekey_output_mixin",
    DirectShapeKeyOutputMixin=object,
)
_install_module(
    f"{PKG}.blueprint.direct_export_shapekey_runtime_mixin",
    DirectShapeKeyRuntimeMixin=object,
)
_install_module(
    f"{PKG}.blueprint.direct_export_shapekey_sampling_mixin",
    DirectShapeKeySamplingMixin=object,
)
_install_module(
    f"{PKG}.blueprint.export_helper",
    BlueprintExportHelper=object,
)

_MODULE_PATH = Path(__file__).resolve().parents[1] / "blueprint" / "ntmi_shapekey.py"
_spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.ntmi_shapekey", _MODULE_PATH)
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)


class _OriginalNode:
    drag_drive_enabled = True
    DRAG_DRIVE_REGISTER = 100
    DRAG_CLICK_COUNT_REGISTER = 101

    def _create_safe_var_name(self, text, prefix="", existing_names=None):
        return f"{prefix}{str(text or '').replace('-', '_')}"

    def _drag_shapekey_drive_resource_name(self, ini_path=None):
        return "ResourceDragShapeKeyDrive_ns"

    def _drag_shapekey_click_count_resource_name(self, ini_path=None):
        return "ResourceDragShapeKeyClickCount_ns"

    def _drag_drive_zone_ids(self, unique_names):
        return [2, 3, -1]


class NTMIShapeKeyDragDriveTests(unittest.TestCase):
    def setUp(self):
        self.adapter = _module.NTMIShapeKeyNodeAdapter(
            original_node=_OriginalNode(),
            sections={},
            mod_export_path=tempfile.mkdtemp(prefix="ntmi_sk_drive_"),
            ini_path="dummy.ini",
        )

    def test_shader_includes_drive_binding_and_read(self):
        shader_path = os.path.join(tempfile.gettempdir(), "ntmi_shapekey_drive_test.hlsl")
        self.adapter._update_shader_file(
            shader_path,
            {1: {"Breast_L": ["obj1"]}},
            True,
            True,
            ["Breast_L", "Breast_R", "Hip"],
            ["obj1"],
            use_optimized=True,
            merge_slot_files=True,
            drag_drive_enabled=True,
            drag_zone_ids=[2, 3, -1],
        )
        with open(shader_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("Buffer<float> ShapeKeyDrive : register(t100);", content)
        self.assertIn("Buffer<uint> ShapeKeyClickCount : register(t101);", content)
        self.assertIn("SHAPEKEY_ZONE_IDS", content)
        self.assertIn("weight = ShapeKeyDrive[SHAPEKEY_ZONE_IDS[freq_idx] * SHAPEKEY_STAGE_COUNT * SHAPEKEY_DIR_COUNT", content)
        self.assertIn("0xFFFFFFFFu", content)

    def test_skin_commandlist_binds_drive(self):
        generator = _module.NTMIDirectShapeKeyGenerator.__new__(_module.NTMIDirectShapeKeyGenerator)
        generator.node = self.adapter
        generator.target_ini_file = "dummy.ini"
        self.adapter.part_layouts = {
            "abc123": types.SimpleNamespace(
                position_resource="ResourcePart_abc123_Position",
            )
        }
        sections = {
            "[CommandList_SkinParts_X]": [
                "cs-t65 = ResourcePalette_abc123",
                "run = CommandList\\NTMIv1\\SkinFromBoundSlots",
            ]
        }
        generator._patch_skin_commandlists(sections, {"abc123"}, {"abc123": 3})
        patched = "\n".join(sections["[CommandList_SkinParts_X]"])
        self.assertIn("cs-t100 = ResourceDragShapeKeyDrive_ns", patched)
        self.assertIn("cs-t101 = ResourceDragShapeKeyClickCount_ns", patched)
        self.assertIn("cs-t100 = null", patched)
        self.assertIn("cs-t101 = null", patched)


if __name__ == "__main__":
    unittest.main()
