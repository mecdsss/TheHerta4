"""ShapeKey 与 MultiFile 后处理变量必须使用不同命名空间。"""

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class SwapKeyCrossPostprocessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.multifile_source = (REPO_ROOT / "blueprint/node_postprocess_multifile.py").read_text(
            encoding="utf-8"
        )
        cls.shapekey_source = (REPO_ROOT / "blueprint/node_postprocess_shapekey.py").read_text(
            encoding="utf-8"
        )
        cls.direct_source = (
            REPO_ROOT / "blueprint/direct_export_shapekey_output_mixin.py"
        ).read_text(encoding="utf-8")

    def test_multifile_public_animation_key_remains_backward_compatible(self):
        self.assertRegex(
            self.multifile_source,
            re.compile(r'animation_swapkey:.*?default="\$swapkey100"', re.S),
        )

    def test_shapekey_internal_base_mesh_key_is_namespaced(self):
        for source in (self.shapekey_source, self.direct_source):
            self.assertIn("$ssmt_sk_base_mesh", source)
            self.assertNotIn("global persist $swapkey100 = 1", source)
            self.assertNotRegex(source, r"(?<![A-Za-z0-9_])\$swapkey100\s*==")

    def test_features_no_longer_declare_conflicting_initial_values(self):
        combined = self.multifile_source + "\n" + self.shapekey_source + "\n" + self.direct_source
        self.assertIn("$swapkey100", combined)
        self.assertIn('vars_to_define.add("$ssmt_sk_base_mesh")', combined)
        self.assertIn('f"global persist {var} = 1"', combined)
        self.assertNotIn("global persist $swapkey100 = 1", combined)


if __name__ == "__main__":
    unittest.main()
