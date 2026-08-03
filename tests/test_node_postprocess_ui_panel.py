import importlib.util
import os
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_node_postprocess_ui_panel_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint"):
    package = _install_module(package_name)
    package.__path__ = []

_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(),
    props=types.SimpleNamespace(
        StringProperty=lambda **_kwargs: "",
        BoolProperty=lambda **kwargs: kwargs.get("default", False),
    ),
    utils=types.SimpleNamespace(register_class=lambda _cls: None, unregister_class=lambda _cls: None),
)
_install_module("bpy", **_fake_bpy.__dict__)


class _Base:
    AUTO_APPENDED_SECTION_MARKERS = (
        "; --- AUTO-APPENDED SLIDER CONTROL PANEL ---",
        "; --- AUTO-APPENDED HEALTH DETECTION MODULE ---",
    )


_install_module(f"{PKG}.blueprint.node_postprocess_base", SSMTNode_PostProcess_Base=_Base)

module_path = Path(__file__).resolve().parents[1] / "blueprint" / "node_postprocess_ui_panel.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.node_postprocess_ui_panel", module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

NODE = module.SSMTNode_PostProcess_UIPanel


def _make_node(panel_folder, panel_name="UIPanel", merge_constants=True):
    node = NODE.__new__(NODE)
    node.panel_folder = panel_folder
    node.panel_name = panel_name
    node.merge_constants = merge_constants
    return node


PANEL_INI = """; Generated v66 (Enhanced)
[TextureOverrideCheckHash]
hash = c209c22b
$active = 1
[Resource___glass_panel_png]
filename = ./res/__glass_panel.png
[KeyHelp]
key = no_ctrl no_alt home
type = cycle
run = CommandListToggleHelp

[CommandListToggleHelp]
if $help == 1
    $help = 0
else
    $help = 1
endif

[Constants]
global persist $active
global persist $help = 0
global persist $layout_mode = 0

[Present]
post $active = 0
    $time = time
    if $active == 1
        run = CustomShaderDraw
    endif

[CustomShaderDraw]
hs=null
vs=./res/draw_2d.hlsl
ps=./res/draw_2d.hlsl
blend=ADD SRC_ALPHA INV_SRC_ALPHA

[CustomShaderFx]
hs=null
vs=./res/draw_2d_fx.hlsl
ps=./res/draw_2d_fx.hlsl
blend=ADD SRC_ALPHA ONE
"""


def _write_panel_folder(root: Path) -> Path:
    panel_dir = root / "panel"
    (panel_dir / "res").mkdir(parents=True)
    (panel_dir / "res" / "__glass_panel.png").write_bytes(b"png-bytes")
    (panel_dir / "res" / "draw_2d.hlsl").write_text("shader", encoding="utf-8")
    (panel_dir / "res" / "draw_2d_fx.hlsl").write_text("shader-fx", encoding="utf-8")
    (panel_dir / "ui_config_c209c22b_123.txt").write_text(PANEL_INI, encoding="utf-8")
    return panel_dir


def _write_zip_panel_folder(root: Path, zip_name="ui_assets_1782784000000.zip", entries=None) -> Path:
    panel_dir = root / "panel"
    panel_dir.mkdir(parents=True)
    if entries is None:
        entries = {
            "ui_config_c209c22b_1782784000000.txt": PANEL_INI.encode("utf-8"),
            "res/__glass_panel.png": b"png-bytes",
            "res/draw_2d.hlsl": b"shader",
            "res/draw_2d_fx.hlsl": b"shader-fx",
            "font/__icon.ttf": b"font-bytes",
        }
    with zipfile.ZipFile(panel_dir / zip_name, "w") as zfile:
        for entry_name, payload in entries.items():
            zfile.writestr(entry_name, payload)
    return panel_dir


def _make_mod_dir(root: Path) -> Path:
    mod_dir = root / "mod"
    mod_dir.mkdir()
    (mod_dir / "character.ini").write_text(TARGET_INI, encoding="utf-8")
    return mod_dir


TARGET_INI = """[TextureOverride_drawhash]
hash = drawhash
match_first_index = 56
drawindexed = 12,34,0

[Constants]
global persist $drawhash_ps_replace = 0

[Present]
post $active = 0

"""


class NodePostprocessUIPanelTests(unittest.TestCase):
    def test_resource_reference_allows_inline_comment(self):
        node = _make_node("")

        references = node._extract_referenced_files({
            "ResourceA": [
                "filename = ./res/a.dds; texture note",
                "ps = './res/shader;variant.hlsl'; shader note",
            ],
        })

        self.assertEqual(references, ["./res/a.dds", "./res/shader;variant.hlsl"])

    def test_panel_config_accepts_utf8_bom_but_rejects_invalid_utf8(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            panel_dir = Path(temp_dir)
            config_path = panel_dir / "ui_config_test_1234567890.txt"
            config_path.write_bytes(b"\xef\xbb\xbf[Present]\nvalue = 1\n")
            node = _make_node(str(panel_dir))

            text, _source = node._load_panel_config_text("")
            self.assertTrue(text.startswith("[Present]"))

            config_path.write_bytes(b"[Present]\nvalue = \xff\n")
            with self.assertRaises(UnicodeDecodeError):
                node._load_panel_config_text("")

    def _run(self, target_ini=TARGET_INI, **node_kwargs):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        panel_dir = _write_panel_folder(root)
        mod_dir = root / "mod"
        mod_dir.mkdir()
        target_ini_path = mod_dir / "character.ini"
        target_ini_path.write_text(target_ini, encoding="utf-8")

        node_kwargs.setdefault("panel_folder", str(panel_dir))
        node = _make_node(**node_kwargs)
        node.execute_postprocess(str(mod_dir))

        result_text = target_ini_path.read_text(encoding="utf-8")
        return result_text, mod_dir

    def test_appends_panel_sections_and_merges_constants(self):
        result_text, _ = self._run()

        # 面板独有段被追加
        self.assertIn("[TextureOverrideCheckHash]", result_text)
        self.assertIn("hash = c209c22b", result_text)
        self.assertIn("[Resource___glass_panel_png]", result_text)
        self.assertIn("[KeyHelp]", result_text)
        self.assertIn("[CommandListToggleHelp]", result_text)
        # 追加块带本节点标记
        self.assertIn("; --- AUTO-APPENDED UI PANEL UIPanel ---", result_text)
        # 原有模型段保留
        self.assertIn("[TextureOverride_drawhash]", result_text)

        # Constants 合并：面板全局变量并入已有 [Constants]，且只有一个 [Constants]
        self.assertEqual(result_text.count("[Constants]"), 1)
        constants = result_text.split("[Constants]")[1].split("[")[0]
        self.assertIn("global persist $drawhash_ps_replace = 0", constants)
        self.assertIn("global persist $layout_mode = 0", constants)

        # Present 合并：面板渲染逻辑并入，且只有一个 [Present]
        self.assertEqual(result_text.count("[Present]"), 1)
        present = result_text.split("[Present]")[1].split("[")[0]
        self.assertIn("$time = time", present)

        # 共享着色器段不重复追加（目标没有这些段，所以只出现一次且来自面板）
        self.assertEqual(result_text.count("[CustomShaderDraw]"), 1)
        self.assertEqual(result_text.count("[CustomShaderFx]"), 1)

    def test_preserves_namespace_and_comments_before_first_section(self):
        target = (
            "namespace = CharacterMod\n"
            "; 头部注释\n"
            "\n"
            + TARGET_INI
        )

        result_text, _ = self._run(target_ini=target)

        self.assertTrue(result_text.startswith("namespace = CharacterMod\n; 头部注释\n"))

    def test_panel_marker_cannot_be_split_by_newline(self):
        node = _make_node("", panel_name="Panel\nInjected")

        marker = node.get_panel_marker()

        self.assertNotIn("\n", marker)
        self.assertIn("Panel Injected", marker)

    def test_copies_referenced_res_files(self):
        _, mod_dir = self._run()
        self.assertEqual((mod_dir / "res" / "__glass_panel.png").read_bytes(), b"png-bytes")
        self.assertEqual(
            (mod_dir / "res" / "draw_2d.hlsl").read_text(encoding="utf-8"), "shader"
        )
        self.assertEqual(
            (mod_dir / "res" / "draw_2d_fx.hlsl").read_text(encoding="utf-8"), "shader-fx"
        )

    def test_rerun_replaces_own_block_without_duplicates(self):
        first_text, mod_dir = self._run()
        # 二次执行（模拟再次导出）
        with tempfile.TemporaryDirectory() as temp_dir:
            panel_dir = _write_panel_folder(Path(temp_dir))
            node = _make_node(str(panel_dir))
            node.execute_postprocess(str(mod_dir))
            second_text = next(mod_dir.glob("*.ini")).read_text(encoding="utf-8")

        self.assertEqual(second_text.count("[TextureOverrideCheckHash]"), 1)
        self.assertEqual(second_text.count("; --- AUTO-APPENDED UI PANEL UIPanel ---"), 1)
        self.assertEqual(second_text.count("[KeyHelp]"), 1)
        # 面板块仅更新一次，不会因为二次运行而追加第二份
        self.assertEqual(second_text.count("hash = c209c22b"), 1)
        self.assertEqual(second_text.count("$time = time"), 1)
        self.assertEqual(second_text.count("SSMT UI PANEL PRESENT"), 2)

    def test_rerun_preserves_following_auto_appended_block_verbatim(self):
        first_text, mod_dir = self._run()
        following_block = (
            "; --- AUTO-APPENDED HEALTH DETECTION MODULE ---\n"
            "[CommandListHealth]\n"
            "$health = 1\n"
        )
        ini_path = next(mod_dir.glob("*.ini"))
        ini_path.write_text(first_text + "\n" + following_block, encoding="utf-8")

        with tempfile.TemporaryDirectory() as temp_dir:
            panel_dir = _write_panel_folder(Path(temp_dir))
            node = _make_node(str(panel_dir))
            node.execute_postprocess(str(mod_dir))

        second_text = ini_path.read_text(encoding="utf-8")
        self.assertIn(following_block, second_text)
        self.assertEqual(second_text.count("[CommandListHealth]"), 1)

    def test_loose_resource_path_cannot_escape_panel_or_mod_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            panel_dir = root / "panel"
            panel_dir.mkdir()
            mod_dir = root / "mod"
            mod_dir.mkdir()
            (root / "outside.txt").write_text("outside", encoding="utf-8")
            node = _make_node(str(panel_dir))

            with self.assertRaisesRegex(ValueError, "非法路径"):
                node._copy_referenced_files(
                    {"ResourceOutside": ["filename = ../outside.txt"]},
                    str(panel_dir),
                    str(mod_dir),
                )

            self.assertFalse((root / "outside-copy.txt").exists())

    def test_shared_shader_sections_not_appended_when_target_already_has_them(self):
        target = TARGET_INI + """[CustomShaderDraw]
hs=null
vs=./res/draw_2d.hlsl
ps=./res/draw_2d.hlsl
blend=ADD SRC_ALPHA INV_SRC_ALPHA

"""
        result_text, _ = self._run(target_ini=target)
        # 内容相同：不重复追加
        self.assertEqual(result_text.count("[CustomShaderDraw]"), 1)

    def test_conflicting_non_shared_section_raises(self):
        target = TARGET_INI + """[TextureOverrideCheckHash]
hash = otherhash
$active = 1

"""
        with self.assertRaisesRegex(ValueError, "TextureOverrideCheckHash"):
            self._run(target_ini=target)

    def test_main_table_wins_when_variable_declared_with_different_value(self):
        # 主配置表已声明同名变量（即使初始值不同），面板声明一律丢弃
        target = TARGET_INI.replace(
            "global persist $drawhash_ps_replace = 0",
            "global persist $drawhash_ps_replace = 0\nglobal persist $help = 1",
        )
        result_text, _ = self._run(target_ini=target)
        constants = result_text.split("[Constants]")[1].split("[")[0]
        # 主配置表的 $help = 1 保留，面板的 $help = 0 声明被丢弃
        self.assertIn("global persist $help = 1", constants)
        self.assertNotIn("global persist $help = 0", constants)
        # 面板独有变量仍追加
        self.assertIn("global persist $layout_mode = 0", constants)

    def test_missing_referenced_resource_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            panel_dir = _write_panel_folder(root)
            (panel_dir / "res" / "__glass_panel.png").unlink()
            mod_dir = root / "mod"
            mod_dir.mkdir()
            (mod_dir / "character.ini").write_text(TARGET_INI, encoding="utf-8")

            node = _make_node(str(panel_dir))
            with self.assertRaisesRegex(ValueError, "资源文件不存在"):
                node.execute_postprocess(str(mod_dir))

    def test_missing_panel_folder_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mod_dir = Path(temp_dir) / "mod"
            mod_dir.mkdir()
            (mod_dir / "character.ini").write_text(TARGET_INI, encoding="utf-8")
            node = _make_node(str(Path(temp_dir) / "nonexistent"))
            with self.assertRaisesRegex(ValueError, "面板目录"):
                node.execute_postprocess(str(mod_dir))

    def test_latest_ui_config_by_filename_timestamp_wins(self):
        # 目录里有多份网页配置时，按文件名时间戳取最新的一份
        with tempfile.TemporaryDirectory() as temp_dir:
            panel_dir = Path(temp_dir)
            older = panel_dir / "ui_config_c209c22b_1782783295046.txt"
            newer = panel_dir / "ui_config_c209c22b_1782784000000.txt"
            older.write_text(PANEL_INI, encoding="utf-8")
            newer.write_text(PANEL_INI, encoding="utf-8")
            # 旧文件的修改时间更新，确认不是按修改时间取的
            os.utime(older, (9999999999, 9999999999))

            node = _make_node(str(panel_dir))
            found = node._find_panel_ini_path()
            self.assertEqual(found, str(newer))

    def test_panel_folder_uses_blender_path_expansion_when_available(self):
        node = _make_node("//panel")
        bpy_module = module.bpy
        old_path = getattr(bpy_module, "path", None)
        bpy_module.path = types.SimpleNamespace(abspath=lambda value: f"expanded:{value}")
        try:
            self.assertEqual(node._get_panel_folder(), "expanded://panel")
        finally:
            if old_path is None:
                delattr(bpy_module, "path")
            else:
                bpy_module.path = old_path

    def test_latest_config_falls_back_to_file_mtime(self):
        # 文件名没有时间戳时，退回用文件修改时间取最新
        with tempfile.TemporaryDirectory() as temp_dir:
            panel_dir = Path(temp_dir)
            older = panel_dir / "ui_config_old.txt"
            newer = panel_dir / "ui_config_new.txt"
            older.write_text(PANEL_INI, encoding="utf-8")
            newer.write_text(PANEL_INI, encoding="utf-8")
            os.utime(older, (1700000000, 1700000000))
            os.utime(newer, (1780000000, 1780000000))

            node = _make_node(str(panel_dir))
            found = node._find_panel_ini_path()
            self.assertEqual(found, str(newer))

    def test_ui_config_files_take_priority_over_other_txt(self):
        # 即使其他 txt 更新，也优先在 ui_config_* 里取最新
        with tempfile.TemporaryDirectory() as temp_dir:
            panel_dir = Path(temp_dir)
            config = panel_dir / "ui_config_c209c22b_1782783295046.txt"
            config.write_text(PANEL_INI, encoding="utf-8")
            unrelated = panel_dir / "readme.txt"
            unrelated.write_text("not a panel config", encoding="utf-8")
            os.utime(unrelated, (9999999999, 9999999999))

            node = _make_node(str(panel_dir))
            found = node._find_panel_ini_path()
            self.assertEqual(found, str(config))

    def test_zip_package_config_and_resources_extract_to_mod_dir(self):
        # 网页下载的压缩包：配置从包内读取，资源直接解压到模组目录
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            panel_dir = _write_zip_panel_folder(root)
            mod_dir = _make_mod_dir(root)

            node = _make_node(str(panel_dir))
            node.execute_postprocess(str(mod_dir))
            result_text = (mod_dir / "character.ini").read_text(encoding="utf-8")

            self.assertIn("[TextureOverrideCheckHash]", result_text)
            self.assertIn("; --- AUTO-APPENDED UI PANEL UIPanel ---", result_text)
            self.assertEqual((mod_dir / "res" / "__glass_panel.png").read_bytes(), b"png-bytes")
            self.assertEqual((mod_dir / "res" / "draw_2d.hlsl").read_bytes(), b"shader")
            self.assertEqual((mod_dir / "res" / "draw_2d_fx.hlsl").read_bytes(), b"shader-fx")
            self.assertEqual((mod_dir / "font" / "__icon.ttf").read_bytes(), b"font-bytes")
            # 压缩包内的配置条目只读取，不作为资源解压到模组目录
            self.assertFalse((mod_dir / "ui_config_c209c22b_1782784000000.txt").exists())

    def test_zip_missing_referenced_resource_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            panel_dir = _write_zip_panel_folder(root, entries={
                "ui_config_c209c22b_1782784000000.txt": PANEL_INI.encode("utf-8"),
                "res/__glass_panel.png": b"png-bytes",
                # 缺少 INI 引用的 draw_2d.hlsl / draw_2d_fx.hlsl
            })
            mod_dir = _make_mod_dir(root)
            node = _make_node(str(panel_dir))
            with self.assertRaisesRegex(ValueError, "压缩包缺少"):
                node.execute_postprocess(str(mod_dir))

    def test_latest_zip_by_filename_timestamp_wins(self):
        # 多个压缩包时按文件名时间戳取最新，而不是按修改时间
        with tempfile.TemporaryDirectory() as temp_dir:
            panel_dir = Path(temp_dir)
            older = panel_dir / "ui_assets_1782783295046.zip"
            newer = panel_dir / "ui_assets_1782784000000.zip"
            for zip_path in (older, newer):
                with zipfile.ZipFile(zip_path, "w") as zfile:
                    zfile.writestr("res/a.png", b"a")
            os.utime(older, (9999999999, 9999999999))

            node = _make_node(str(panel_dir))
            self.assertEqual(node._find_ui_assets_zip_path(), str(newer))

    def test_zip_and_config_require_prefix(self):
        # 目录里可能有其他文件：压缩包与配置文件都严格限前缀，杂文件不参与
        with tempfile.TemporaryDirectory() as temp_dir:
            panel_dir = Path(temp_dir)
            with zipfile.ZipFile(panel_dir / "download.zip", "w") as zfile:
                zfile.writestr("res/a.png", b"a")
            (panel_dir / "notes.txt").write_text(PANEL_INI, encoding="utf-8")
            (panel_dir / "config.ini").write_text(PANEL_INI, encoding="utf-8")

            node = _make_node(str(panel_dir))
            # 非 ui_assets_ 前缀的压缩包不参与选择
            self.assertEqual(node._find_ui_assets_zip_path(), "")
            # 非 ui_config_ 前缀的配置文件不参与选择
            with self.assertRaisesRegex(ValueError, "INI 配置文件"):
                node._find_panel_ini_path()

    def test_prefixed_zip_wins_over_newer_plain_zip(self):
        # 有前缀的压缩包即使更旧也被选中，无前缀的一律忽略
        with tempfile.TemporaryDirectory() as temp_dir:
            panel_dir = Path(temp_dir)
            prefixed = panel_dir / "ui_assets_1782783295046.zip"
            plain = panel_dir / "download.zip"
            for zip_path in (prefixed, plain):
                with zipfile.ZipFile(zip_path, "w") as zfile:
                    zfile.writestr("res/a.png", b"a")
            os.utime(plain, (9999999999, 9999999999))

            node = _make_node(str(panel_dir))
            self.assertEqual(node._find_ui_assets_zip_path(), str(prefixed))

    def test_loose_config_takes_priority_over_zip_config(self):
        # 松散配置文件优先于压缩包内配置；资源仍从压缩包解压
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            panel_dir = _write_zip_panel_folder(root, entries={
                "ui_config_c209c22b_1782784000000.txt": PANEL_INI.replace("c209c22b", "aaaaaaaa").encode("utf-8"),
                "res/__glass_panel.png": b"png-bytes",
                "res/draw_2d.hlsl": b"shader",
                "res/draw_2d_fx.hlsl": b"shader-fx",
            })
            (panel_dir / "ui_config_c209c22b_123.txt").write_text(PANEL_INI, encoding="utf-8")
            mod_dir = _make_mod_dir(root)

            node = _make_node(str(panel_dir))
            node.execute_postprocess(str(mod_dir))
            result_text = (mod_dir / "character.ini").read_text(encoding="utf-8")

            self.assertIn("hash = c209c22b", result_text)
            self.assertNotIn("aaaaaaaa", result_text)
            self.assertEqual((mod_dir / "res" / "__glass_panel.png").read_bytes(), b"png-bytes")

    def test_zip_slip_entry_rejected(self):
        # 压缩包内越出模组目录的条目必须被拒绝，不能写出到目录外
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            panel_dir = _write_zip_panel_folder(root, entries={
                "ui_config_c209c22b_1782784000000.txt": PANEL_INI.encode("utf-8"),
                "res/__glass_panel.png": b"png-bytes",
                "res/draw_2d.hlsl": b"shader",
                "res/draw_2d_fx.hlsl": b"shader-fx",
                "../evil.txt": b"evil",
            })
            mod_dir = _make_mod_dir(root)
            node = _make_node(str(panel_dir))
            with self.assertRaisesRegex(ValueError, "非法路径"):
                node.execute_postprocess(str(mod_dir))
            self.assertFalse((root / "evil.txt").exists())

    def test_real_web_generated_ini_appends_cleanly(self):
        # 用网页真实生成的 INI（K:\UI 构造器 下的产物）做端到端校验
        real_ini = Path("K:/UI 构造器/_tmp_generated_minimal.ini")
        if not real_ini.is_file():
            self.skipTest("网页生成样例不可用")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            panel_dir = root / "panel"
            (panel_dir / "res").mkdir(parents=True)
            panel_text = real_ini.read_text(encoding="utf-8", errors="replace")
            (panel_dir / "ui_config_c209c22b_1.txt").write_text(panel_text, encoding="utf-8")
            # 真实 INI 引用的资源
            for name in ("__glass_panel.png", "__joystick_handle.png",
                         "__collision_post_marker.png", "__fx_white.png",
                         "draw_2d.hlsl", "draw_2d_fx.hlsl"):
                (panel_dir / "res" / name).write_bytes(b"data")
            mod_dir = root / "mod"
            mod_dir.mkdir()
            (mod_dir / "character.ini").write_text(TARGET_INI, encoding="utf-8")

            node = _make_node(str(panel_dir))
            node.execute_postprocess(str(mod_dir))
            result_text = next(mod_dir.glob("*.ini")).read_text(encoding="utf-8")

        self.assertIn("[TextureOverrideCheckHash]", result_text)
        self.assertIn("; --- AUTO-APPENDED UI PANEL UIPanel ---", result_text)
        self.assertEqual(result_text.count("[Constants]"), 1)
        self.assertEqual(result_text.count("[Present]"), 1)
        self.assertEqual(result_text.count("[CustomShaderDraw]"), 1)
        self.assertEqual(result_text.count("[CustomShaderFx]"), 1)


if __name__ == "__main__":
    unittest.main()
