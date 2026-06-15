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


PKG = "_ntmi_shapekey_adapter_test_pkg"
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


_install_module("bpy", data=types.SimpleNamespace(objects={}))
_install_module(f"{PKG}.blueprint.direct_export_shapekey", DirectShapeKeyGenerator=type("DirectShapeKeyGenerator", (), {}))
_install_module(
    f"{PKG}.blueprint.direct_export_shapekey_shared",
    ShapeKeyDirectExportError=RuntimeError,
)
_install_module(
    f"{PKG}.common.d3d11_gametype",
    D3D11GameType=type("D3D11GameType", (), {}),
)
_install_module(
    f"{PKG}.utils.log_utils",
    LOG=types.SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None),
)
_install_module(
    f"{PKG}.blueprint.ntmi_layout_adapter",
    iter_name_variants=lambda name: [name],
    local_loop_indices_for_export_range=lambda *args, **kwargs: [],
    parse_ntmi_part_layouts=lambda *args, **kwargs: {},
)
_install_module(
    f"{PKG}.ui.ntmi_modimp.modimp_core",
    ensure_mod_importer_package=lambda *args, **kwargs: types.SimpleNamespace(__name__="fake_modimp"),
)
_install_module(
    f"{PKG}.ui.ntmi_modimp.ntemi_importer",
    _ensure_ntemi_game_data_converter=lambda *args, **kwargs: None,
)


module_path = Path(__file__).resolve().parents[1] / "blueprint" / "ntmi_shapekey.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.ntmi_shapekey", module_path)
ntmi_shapekey = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ntmi_shapekey
spec.loader.exec_module(ntmi_shapekey)


class NTMIShapeKeyAdapterTests(unittest.TestCase):
    def test_adapter_forwards_compute_dispatch_group_count_to_original_node(self):
        class _OriginalNode:
            def _compute_dispatch_group_count(self, vertex_count, threads_per_group=16):
                vertex_count = int(vertex_count or 0)
                threads_per_group = max(1, int(threads_per_group or 1))
                return max(1, (vertex_count + threads_per_group - 1) // threads_per_group)

        adapter = ntmi_shapekey.NTMIShapeKeyNodeAdapter(
            original_node=_OriginalNode(),
            sections={},
            mod_export_path=".",
            ini_path="./mod.ini",
        )

        self.assertEqual(adapter._compute_dispatch_group_count(17, threads_per_group=16), 2)


if __name__ == "__main__":
    unittest.main()
