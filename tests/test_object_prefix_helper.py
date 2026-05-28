import sys
import types
import unittest
import importlib.util
from pathlib import Path


if "bpy" not in sys.modules:
    sys.modules["bpy"] = types.SimpleNamespace(
        data=types.SimpleNamespace(objects={}),
    )

utils_module = types.ModuleType("TheHerta4.utils.ssmt_error_utils")
utils_module.SSMTErrorUtils = types.SimpleNamespace(
    raise_fatal=lambda message: (_ for _ in ()).throw(RuntimeError(message)),
)
sys.modules.setdefault("TheHerta4", types.ModuleType("TheHerta4"))
sys.modules.setdefault("TheHerta4.common", types.ModuleType("TheHerta4.common"))
sys.modules.setdefault("TheHerta4.utils", types.ModuleType("TheHerta4.utils"))
sys.modules["TheHerta4.utils.ssmt_error_utils"] = utils_module

module_path = Path(__file__).resolve().parents[1] / "common" / "object_prefix_helper.py"
spec = importlib.util.spec_from_file_location(
    "TheHerta4.common.object_prefix_helper",
    module_path,
)
object_prefix_module = importlib.util.module_from_spec(spec)
sys.modules["TheHerta4.common.object_prefix_helper"] = object_prefix_module
spec.loader.exec_module(object_prefix_module)
ObjectPrefixHelper = object_prefix_module.ObjectPrefixHelper


class ObjectPrefixHelperTests(unittest.TestCase):
    def test_lod_prefix_without_blender_suffix_survives_runtime_copy_suffix(self):
        prefix_info = ObjectPrefixHelper.extract_prefix_info("LOD0.ae1ab184-29187-0_copy")

        self.assertEqual(prefix_info, ("LOD0.ae1ab184-29187-0", "."))

    def test_lod_prefix_with_blender_suffix_still_parses_as_slice_prefix(self):
        prefix_info = ObjectPrefixHelper.extract_prefix_info("LOD0.ae1ab184-29187-0.000_copy")

        self.assertEqual(prefix_info, ("LOD0.ae1ab184-29187-0", "."))

    def test_plain_lod_prefix_without_suffix_parses(self):
        prefix_info = ObjectPrefixHelper.extract_prefix_info("LOD0.ae1ab184-29187-0")

        self.assertEqual(prefix_info, ("LOD0.ae1ab184-29187-0", "."))

    def test_lod_prefix_runtime_chain_suffixes_parse_as_full_prefix(self):
        for object_name in (
            "LOD0.ae1ab184-29187-0_chain1",
            "LOD0.ae1ab184-29187-0_dup2",
            "LOD0.ae1ab184-29187-0_chain1_dup2_copy",
            "LOD0.ae1ab184-29187-0_chain1_copy_temp",
            "LOD0.ae1ab184-29187-0_vgtest_copy",
            "LOD0.ae1ab184-29187-0_chain1_vgtest_copy",
        ):
            with self.subTest(object_name=object_name):
                prefix_info = ObjectPrefixHelper.extract_prefix_info(object_name)

                self.assertEqual(prefix_info, ("LOD0.ae1ab184-29187-0", "."))

    def test_build_virtual_object_name_does_not_duplicate_prefix_for_runtime_copy(self):
        node = types.SimpleNamespace(
            object_name="LOD0.fbb18630-24567-0_copy",
            object_prefix="LOD0.fbb18630-24567-0",
            prefix_separator=".",
        )

        self.assertEqual(
            ObjectPrefixHelper.build_virtual_object_name_for_node(node),
            "LOD0.fbb18630-24567-0_copy",
        )


if __name__ == "__main__":
    unittest.main()
