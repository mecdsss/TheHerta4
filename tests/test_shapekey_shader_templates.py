import unittest
from pathlib import Path


class ShapeKeyShaderTemplateTests(unittest.TestCase):
    def _assert_vertex_bounds_guard(self, shader_filename):
        shader_path = Path(__file__).resolve().parents[1] / "Toolset" / shader_filename
        source = shader_path.read_text(encoding="utf-8")

        self.assertIn("uint vertex_count = rw_buffer.Length;", source)
        self.assertIn("if (i >= vertex_count)", source)
        self.assertIn("return;", source)

    def test_all_runtime_shapekey_shaders_have_vertex_bounds_guard(self):
        for shader_filename in (
            "shapekey_anim_packed.hlsl",
            "shapekey_anim_packed_delta_v3.hlsl",
            "shapekey_anim_packed_delta_v4_optimized.hlsl",
            "shapekey_anim_standard.hlsl",
            "shapekey_anim_standard_delta_v3.hlsl",
            "shapekey_anim_packed_delta_v5_merged.hlsl",
            "shapekey_anim_packed_v5_merged.hlsl",
        ):
            with self.subTest(shader=shader_filename):
                self._assert_vertex_bounds_guard(shader_filename)


if __name__ == "__main__":
    unittest.main()
