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


PKG = "_resource_merge_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint"):
    package = _install_module(package_name)
    package.__path__ = []

_install_module("bpy", types=types.SimpleNamespace())
_install_module(
    f"{PKG}.blueprint.node_postprocess_base",
    SSMTNode_PostProcess_Base=type(
        "SSMTNode_PostProcess_Base",
        (),
        {
            "_create_cumulative_backup": lambda self, ini_file_path, mod_export_path: None,
            "split_auto_appended_tail_content": classmethod(lambda cls, content: (content, "")),
        },
    ),
)

module_path = Path(__file__).resolve().parents[1] / "blueprint" / "node_postprocess_resource_merge.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.node_postprocess_resource_merge", module_path)
resource_merge_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = resource_merge_module
spec.loader.exec_module(resource_merge_module)


class ResourceMergeTests(unittest.TestCase):
    def test_resource_sections_without_hyphen_are_merged(self):
        node = resource_merge_module.SSMTNode_PostProcess_ResourceMerge()

        with tempfile.TemporaryDirectory() as temp_dir:
            textures_dir = os.path.join(temp_dir, "Textures")
            os.makedirs(textures_dir, exist_ok=True)

            texture_a = os.path.join(textures_dir, "a.dds")
            texture_b = os.path.join(textures_dir, "b.dds")
            with open(texture_a, "wb") as file_obj:
                file_obj.write(b"same texture")
            with open(texture_b, "wb") as file_obj:
                file_obj.write(b"same texture")

            ini_path = os.path.join(temp_dir, "Workspace.ini")
            with open(ini_path, "w", encoding="utf-8") as file_obj:
                file_obj.write(
                    "[ResourceDiffuse]\n"
                    "filename = Textures/a.dds\n\n"
                    "[Resource_Light]\n"
                    "filename = Textures/b.dds\n"
                )

            node.process_ini_file(ini_path, temp_dir)

            with open(ini_path, "r", encoding="utf-8") as file_obj:
                content = file_obj.read()

            self.assertIn("filename = Textures/a.dds", content)
            self.assertNotIn("filename = Textures/b.dds", content)
            self.assertFalse(os.path.exists(texture_b))


if __name__ == "__main__":
    unittest.main()
