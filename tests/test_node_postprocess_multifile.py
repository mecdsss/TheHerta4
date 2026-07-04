import importlib.util
import sys
import types
import unittest
from collections import OrderedDict
from pathlib import Path


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_node_postprocess_multifile_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common"):
    package = _install_module(package_name)
    package.__path__ = []


_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(),
    props=types.SimpleNamespace(
        StringProperty=lambda **_kwargs: None,
        IntProperty=lambda **_kwargs: None,
    ),
    utils=types.SimpleNamespace(register_class=lambda _cls: None, unregister_class=lambda _cls: None),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module(f"{PKG}.blueprint.node_postprocess_base", SSMTNode_PostProcess_Base=object)

common_mod_path = Path(__file__).resolve().parents[1] / "common" / "mod_path_compat.py"
common_spec = importlib.util.spec_from_file_location(f"{PKG}.common.mod_path_compat", common_mod_path)
common_module = importlib.util.module_from_spec(common_spec)
sys.modules[common_spec.name] = common_module
common_spec.loader.exec_module(common_module)


class _FakeObjectPrefixHelper:
    @staticmethod
    def extract_prefix_info(_value):
        return None

    @staticmethod
    def parse_prefix_parts(_value):
        return {}


_install_module(f"{PKG}.common.object_prefix_helper", ObjectPrefixHelper=_FakeObjectPrefixHelper)

module_path = Path(__file__).resolve().parents[1] / "blueprint" / "node_postprocess_multifile.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.node_postprocess_multifile", module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class NodePostprocessMultiFileTests(unittest.TestCase):
    def test_compute_dispatch_group_count_rounds_up_by_thread_group(self):
        node = module.SSMTNode_PostProcess_MultiFile()

        self.assertEqual(node._compute_dispatch_group_count(0, threads_per_group=16), 1)
        self.assertEqual(node._compute_dispatch_group_count(1, threads_per_group=16), 1)
        self.assertEqual(node._compute_dispatch_group_count(16, threads_per_group=16), 1)
        self.assertEqual(node._compute_dispatch_group_count(17, threads_per_group=16), 2)
        self.assertEqual(node._compute_dispatch_group_count(3786, threads_per_group=16), 237)

    def test_find_existing_base_resource_name_uses_real_resource_section(self):
        node = module.SSMTNode_PostProcess_MultiFile()
        sections = OrderedDict(
            [
                ("[TextureOverride_VB_bcc7e369_bcc7e369_Position]", ["hash = 80f2a2aa"]),
                ("[TextureOverride_VB_bcc7e369_bcc7e369_Position_1]", ["hash = 80f2a2aa"]),
                ("[Resourcebcc7e369Position]", [
                    "type = Buffer",
                    "stride = 40",
                    "filename = Meshes0000/bcc7e369-Position.buf",
                ]),
            ]
        )

        resource_name = node._find_existing_base_resource_name(
            sections,
            "LOD0.bcc7e369-13680-0",
            "bcc7e369",
        )

        self.assertEqual(resource_name, "Resourcebcc7e369Position")

    def test_stale_copy_desc_detection_accepts_lod_hash_for_resource_prefix(self):
        constants_lines = [
            "post TextureOverride_VB_bcc7e369_bcc7e369_Position = copy_desc TextureOverride_VB_bcc7e369_bcc7e369_Position_1",
        ]

        stale_alias_names = []
        for hash_filter in ["LOD0.bcc7e369-13680-0", "bcc7e369"]:
            for alias_name in common_module.collect_stale_texture_override_position_alias_names(
                constants_lines,
                hash_filter,
            ):
                if alias_name not in stale_alias_names:
                    stale_alias_names.append(alias_name)

        self.assertEqual(
            stale_alias_names,
            ["TextureOverride_VB_bcc7e369_bcc7e369_Position_1"],
        )


if __name__ == "__main__":
    unittest.main()
