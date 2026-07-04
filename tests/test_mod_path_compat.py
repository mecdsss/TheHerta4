import unittest
from collections import OrderedDict

from common.mod_path_compat import (
    collect_base_position_resource_map,
    collect_stale_texture_override_position_alias_names,
    ensure_resource_alias_section,
    find_base_position_resource_name,
    iter_position_buffer_candidates,
    is_stale_texture_override_position_alias_section,
    is_stale_texture_override_position_copy_desc_line,
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

    def test_find_base_position_resource_name_ignores_texture_override_sections(self):
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

        resource_name = find_base_position_resource_name(
            sections,
            "LOD0.bcc7e369-13680-0",
            base_name="bcc7e369",
            preferred_names=["Resource_bcc7e369_Position", "Resourcebcc7e369Position"],
            fallback_name="Resource_bcc7e369_Position",
        )

        self.assertEqual(resource_name, "Resourcebcc7e369Position")

    def test_stale_texture_override_multifile_output_is_detected(self):
        self.assertTrue(
            is_stale_texture_override_position_alias_section(
                "[TextureOverride_VB_bcc7e369_bcc7e369_Position_1]",
                "LOD0.bcc7e369-13680-0",
            )
        )
        self.assertTrue(
            is_stale_texture_override_position_copy_desc_line(
                "post TextureOverride_VB_bcc7e369_bcc7e369_Position = copy_desc TextureOverride_VB_bcc7e369_bcc7e369_Position_1",
                "LOD0.bcc7e369-13680-0",
            )
        )
        self.assertFalse(
            is_stale_texture_override_position_copy_desc_line(
                "post Resourcebcc7e369Position = copy_desc Resourcebcc7e369Position_1",
                "LOD0.bcc7e369-13680-0",
            )
        )

    def test_collect_stale_texture_override_alias_names_uses_copy_desc_references(self):
        constants_lines = [
            "post TextureOverride_VB_bcc7e369_bcc7e369_Position = copy_desc TextureOverride_VB_bcc7e369_bcc7e369_Position_1",
            "post TextureOverride_VB_bcc7e369_bcc7e369_Position = copy_desc TextureOverride_VB_bcc7e369_bcc7e369_Position_1",
            "post TextureOverride_VB_deadbeef_deadbeef_Position = copy_desc TextureOverride_VB_deadbeef_deadbeef_Position_1",
            "post TextureOverride_VB_bcc7e369_bcc7e369_Position = copy_desc TextureOverride_VB_bcc7e369_bcc7e369_Index_1",
            "post Resourcebcc7e369Position = copy_desc Resourcebcc7e369Position_1",
        ]

        alias_names = collect_stale_texture_override_position_alias_names(
            constants_lines,
            "LOD0.bcc7e369-13680-0",
        )

        self.assertEqual(
            alias_names,
            ["TextureOverride_VB_bcc7e369_bcc7e369_Position_1"],
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
