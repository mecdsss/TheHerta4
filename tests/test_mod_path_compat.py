import unittest
from collections import OrderedDict

from common.mod_path_compat import (
    collect_base_position_resource_map,
    ensure_resource_alias_section,
    iter_position_buffer_candidates,
)


class ModPathCompatTests(unittest.TestCase):
    """测试 Mod 路径兼容性工具：资源映射、别名和缓冲区候选搜索"""

    def test_collect_base_position_resource_map_keeps_existing_resource_names(self):
        """测试 collect_base_position_resource_map 正确分组已有资源名"""
        sections = OrderedDict(
            [
                ("[Resource_LOD0_c8197c5b_53472_0_Position]", ["type = Buffer"]),
                ("[Resource_LOD0_c8197c5b_53472_0_Position0000]", ["type = Buffer"]),
                ("[Resourcec8197c5bPosition]", ["type = Buffer"]),
            ]
        )

        resource_map = collect_base_position_resource_map(
            sections,
            prefix_extractor=lambda value: value.split("-", 1)[0],
        )

        self.assertEqual(
            resource_map["LOD0"],
            ["Resource_LOD0_c8197c5b_53472_0_Position"],
        )
        self.assertEqual(
            resource_map["c8197c5b"],
            ["Resourcec8197c5bPosition"],
        )

    def test_ensure_resource_alias_section_clones_real_section(self):
        """测试 ensure_resource_alias_section 克隆真实资源段创建别名"""
        sections = OrderedDict(
            [
                ("[Resource_LOD0_c8197c5b_53472_0_Position]", [
                    "type = Buffer",
                    "filename = Meshes0000/LOD0.c8197c5b-53472-0-Position.buf",
                ]),
            ]
        )

        alias_name = ensure_resource_alias_section(
            sections,
            "Resource_LOD0_c8197c5b_53472_0_Position",
            "_0",
        )

        self.assertEqual(alias_name, "[Resource_LOD0_c8197c5b_53472_0_Position_0]")
        self.assertIn(alias_name, sections)
        self.assertEqual(
            sections[alias_name],
            sections["[Resource_LOD0_c8197c5b_53472_0_Position]"],
        )

    def test_iter_position_buffer_candidates_accepts_lod_prefixed_stem(self):
        """测试 iter_position_buffer_candidates 接受 LOD 前缀的文件名作为候选"""
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            filenames = [
                "LOD0.c8197c5b-53472-0-Position.buf",
                "c8197c5b-Position.buf",
                "otherhash-Position.buf",
            ]
            for filename in filenames:
                with open(os.path.join(temp_dir, filename), "wb") as file_obj:
                    file_obj.write(b"")

            candidates = iter_position_buffer_candidates(temp_dir, "c8197c5b")

            self.assertEqual(
                [candidate["filename"] for candidate in candidates],
                [
                    "c8197c5b-Position.buf",
                    "LOD0.c8197c5b-53472-0-Position.buf",
                ],
            )


if __name__ == "__main__":
    unittest.main()
