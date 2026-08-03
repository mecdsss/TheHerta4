# -*- coding: utf-8 -*-
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "Toolset" / "mod_chinese_to_english.py"
SPEC = importlib.util.spec_from_file_location("mod_chinese_to_english", MODULE_PATH)
tool = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tool
SPEC.loader.exec_module(tool)


class ModChineseToEnglishTests(unittest.TestCase):
    def test_recursive_conversion_keeps_folders_and_fixes_references(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            texture_dir = root / "中文文件夹" / "Textures"
            texture_dir.mkdir(parents=True)
            texture = texture_dir / "身体贴图.dds"
            texture.write_bytes(b"texture")
            ini = root / "中文文件夹" / "角色中文.ini"
            ini.write_text(
                "[资源身体]\r\n"
                "filename = Textures/身体贴图.dds\r\n"
                "[TextureOverride身体]\r\n"
                "this = 资源身体\r\n",
                encoding="utf-8-sig",
                newline="",
            )

            plan = tool.build_plan(root)
            backup = tool.apply_plan(plan)

            target = texture.with_name(tool.replace_non_ascii_runs(texture.name))
            self.assertFalse(texture.exists())
            self.assertTrue(target.is_file())
            self.assertTrue(ini.is_file(), "INI 文件名不应修改")
            self.assertTrue(texture_dir.is_dir(), "文件夹名称不应修改")
            converted = ini.read_text(encoding="utf-8-sig")
            self.assertIn(f"filename = Textures/{target.name}", converted)
            self.assertNotIn("资源身体", converted)
            self.assertNotIn("TextureOverride身体", converted)
            self.assertIsNotNone(backup)
            self.assertTrue((backup / "中文文件夹" / "角色中文.ini.bak").is_file())

    def test_chinese_directory_in_reference_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            texture_dir = root / "贴图目录"
            texture_dir.mkdir()
            texture = texture_dir / "脸.png"
            texture.write_bytes(b"png")
            ini = root / "mod.ini"
            ini.write_text("filename = 贴图目录/脸.png\n", encoding="utf-8")

            plan = tool.build_plan(root)
            tool.apply_plan(plan)
            converted = ini.read_text(encoding="utf-8")

            self.assertIn("贴图目录/", converted)
            self.assertTrue((texture_dir / tool.replace_non_ascii_runs(texture.name)).is_file())

    def test_missing_referenced_chinese_texture_is_skipped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ini = root / "mod.ini"
            original = "[中文]\nfilename = Textures/缺失.dds\n"
            ini.write_text(original, encoding="utf-8")

            plan = tool.build_plan(root)
            tool.apply_plan(plan)

            converted = ini.read_text(encoding="utf-8")
            self.assertNotIn("[中文]", converted)
            self.assertIn("filename = Textures/缺失.dds", converted)
            self.assertEqual(len(plan.skipped_missing_references), 1)
            self.assertFalse(plan.renames)

    def test_missing_texture_does_not_block_existing_texture_in_same_ini(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            texture_dir = root / "Texture"
            texture_dir.mkdir()
            existing = texture_dir / "身体.dds"
            existing.write_bytes(b"dds")
            ini = root / "mod.ini"
            ini.write_text(
                "[Resource脸]\nfilename = Texture/NormalMap_脸.dds\n"
                "[Resource身体]\nfilename = Texture/身体.dds\n",
                encoding="utf-8",
            )

            plan = tool.build_plan(root)
            tool.apply_plan(plan)

            target = next(iter(plan.renames.values()))
            converted = ini.read_text(encoding="utf-8")
            self.assertTrue(target.is_file())
            self.assertIn(f"filename = Texture/{target.name}", converted)
            self.assertIn("filename = Texture/NormalMap_脸.dds", converted)
            self.assertEqual(len(plan.skipped_missing_references), 1)

    def test_non_texture_filename_path_is_not_broken(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            resource = root / "中文缓存.buf"
            resource.write_bytes(b"buffer")
            ini = root / "mod.ini"
            ini.write_text("[中文资源]\nfilename = 中文缓存.buf\n", encoding="utf-8")

            plan = tool.build_plan(root)
            tool.apply_plan(plan)

            converted = ini.read_text(encoding="utf-8")
            self.assertIn("filename = 中文缓存.buf", converted)
            self.assertTrue(resource.is_file())
            self.assertNotIn("[中文资源]", converted)

    def test_shader_and_include_paths_are_not_translated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ini = root / "mod.ini"
            ini.write_text(
                "[中文着色器]\n"
                "cs = ./中文目录/着色器.hlsl ; 中文注释\n"
                "include = 中文目录/配置.ini\n",
                encoding="utf-8",
            )

            plan = tool.build_plan(root)
            tool.apply_plan(plan)
            converted = ini.read_text(encoding="utf-8")

            self.assertIn("cs = ./中文目录/着色器.hlsl", converted)
            self.assertIn("include = 中文目录/配置.ini", converted)
            self.assertNotIn("[中文着色器]", converted)
            self.assertNotIn("中文注释", converted)

    def test_inline_comment_without_space_does_not_hide_texture_extension(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            texture = root / "贴图.dds"
            texture.write_bytes(b"dds")
            ini = root / "mod.ini"
            ini.write_text("filename = 贴图.dds;备注\n", encoding="utf-8")

            plan = tool.build_plan(root)
            tool.apply_plan(plan)
            target = next(iter(plan.renames.values()))
            converted = ini.read_text(encoding="utf-8")

            self.assertTrue(target.is_file())
            self.assertIn(f"filename = {target.name};", converted)
            self.assertNotIn("备注", converted)

    def test_gb18030_encoding_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ini = root / "mod.ini"
            ini.write_bytes("[中文]\r\nkey = 中文\r\n".encode("gb18030"))

            plan = tool.build_plan(root)
            tool.apply_plan(plan)
            raw = ini.read_bytes()

            converted = raw.decode("gb18030")
            self.assertNotIn("中文", converted)
            self.assertIn("\r\n", converted)

    def test_dry_run_does_not_modify_anything(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            texture = root / "贴图.dds"
            texture.write_bytes(b"dds")
            ini = root / "mod.ini"
            original = "filename = 贴图.dds\n"
            ini.write_text(original, encoding="utf-8")

            plan = tool.build_plan(root)
            tool.apply_plan(plan, dry_run=True)

            self.assertTrue(texture.is_file())
            self.assertEqual(ini.read_text(encoding="utf-8"), original)

    def test_existing_english_target_gets_non_destructive_suffix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "贴图.dds"
            source.write_bytes(b"source")
            natural_target = source.with_name(tool.replace_non_ascii_runs(source.name))
            natural_target.write_bytes(b"existing")
            ini = root / "mod.ini"
            ini.write_text("filename = 贴图.dds\n", encoding="utf-8")

            plan = tool.build_plan(root)
            chosen_target = next(iter(plan.renames.values()))
            tool.apply_plan(plan)

            self.assertNotEqual(chosen_target, natural_target)
            self.assertEqual(natural_target.read_bytes(), b"existing")
            self.assertEqual(chosen_target.read_bytes(), b"source")
            self.assertIn(chosen_target.name, ini.read_text(encoding="utf-8"))

    def test_post_validation_failure_rolls_back_files_and_ini(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "贴图.dds"
            source.write_bytes(b"source")
            ini = root / "mod.ini"
            original = "[中文]\nfilename = 贴图.dds\n"
            ini.write_text(original, encoding="utf-8")
            plan = tool.build_plan(root)
            target = next(iter(plan.renames.values()))

            with mock.patch.object(tool, "_post_validate", side_effect=tool.ConversionError("模拟失败")):
                with self.assertRaises(tool.ConversionError):
                    tool.apply_plan(plan)

            self.assertTrue(source.is_file())
            self.assertFalse(target.exists())
            self.assertEqual(ini.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
