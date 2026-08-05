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
    types=types.SimpleNamespace(Operator=object),
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
    ANIM_DRIVER_SECTION_MARKER_START = "; --- ANIMATION DRIVER SECTION ---"
    ANIM_DRIVER_SECTION_MARKER_END = "; --- END ANIMATION DRIVER SECTION ---"

    @classmethod
    def is_known_auto_appended_marker(cls, line: str) -> bool:
        stripped = str(line or "").strip()
        if stripped in cls.AUTO_APPENDED_SECTION_MARKERS:
            return True
        return stripped.startswith("; --- AUTO-APPENDED UI PANEL ")

    @classmethod
    def split_anim_driver_block_content(cls, content):
        lines = str(content or "").splitlines(keepends=True)
        start_index = next(
            (index for index, line in enumerate(lines)
             if cls.ANIM_DRIVER_SECTION_MARKER_START in line),
            None,
        )
        if start_index is None:
            return "", content
        end_index = next(
            (index for index in range(start_index + 1, len(lines))
             if cls.ANIM_DRIVER_SECTION_MARKER_END in lines[index]),
            None,
        )
        if end_index is None:
            return "", content
        driver = "".join(lines[start_index:end_index + 1])
        remaining = "".join(lines[end_index + 1:]).lstrip("\r\n")
        return driver, remaining


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


PANEL_DRAG_INI = """[Present]
    ; --- MODEL DRAG BINDING BEGIN ---
    if $mouse_clicked == 1 && $is_dragging == 0
        if $ssmtdrag_ui_detected_fserfrse >= 0 && $ssmtdrag_ui_zone_fserfrse == 0
            $is_dragging = 9
        endif
    endif
    ; --- MODEL DRAG BINDING END ---
"""


DRAG_TARGET_INI = """[Constants]
global $active = 0
global $ssmtdrag_ui_detected_A = -1
global $ssmtdrag_ui_zone_A = -1

[TextureOverride_drawhash]
hash = drawhash
match_first_index = 56
drawindexed = 12,34,0

[Present]
post $active = 0
; --- DRAG PRESENT BEGIN ---
run = CommandListDragUIReadback_A
; --- DRAG PRESENT END ---
"""


def _write_panel_folder(root: Path) -> Path:
    panel_dir = root / "panel"
    (panel_dir / "res").mkdir(parents=True)
    (panel_dir / "res" / "__glass_panel.png").write_bytes(b"png-bytes")
    (panel_dir / "res" / "draw_2d.hlsl").write_text("shader", encoding="utf-8")
    (panel_dir / "res" / "draw_2d_fx.hlsl").write_text("shader-fx", encoding="utf-8")
    (panel_dir / "ui_config_c209c22b_123.txt").write_text(PANEL_INI, encoding="utf-8")
    return panel_dir


def _write_panel_folder_text(root: Path, panel_text: str) -> Path:
    panel_dir = root / "panel"
    panel_dir.mkdir(parents=True)
    (panel_dir / "ui_config_c209c22b_123.txt").write_text(panel_text, encoding="utf-8")
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

    def _refresh(self, target_ini=TARGET_INI, panel_dir=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        if panel_dir is None:
            panel_dir = _write_panel_folder(root)
        mod_dir = root / "mod"
        mod_dir.mkdir()
        target_ini_path = mod_dir / "character.ini"
        target_ini_path.write_text(target_ini, encoding="utf-8")

        node = _make_node(str(panel_dir))
        success, message = node.refresh_exported_ui_panel_section(str(mod_dir))
        return success, message, target_ini_path.read_text(encoding="utf-8"), mod_dir

    def test_appends_panel_sections_as_independent_bottom_block(self):
        result_text, _ = self._run()

        # Panel-only sections are present in the own bottom block.
        self.assertIn("[TextureOverrideCheckHash]", result_text)
        self.assertIn("hash = c209c22b", result_text)
        self.assertIn("[Resource___glass_panel_png]", result_text)
        self.assertIn("[KeyHelp]", result_text)
        self.assertIn("[CommandListToggleHelp]", result_text)
        marker = "; --- AUTO-APPENDED UI PANEL UIPanel ---"
        self.assertIn(marker, result_text)
        self.assertIn("[TextureOverride_drawhash]", result_text)

        # Base and panel keep their own singleton sections.
        self.assertEqual(result_text.count("[Constants]"), 2)
        self.assertEqual(result_text.count("[Present]"), 2)
        self.assertIn("global persist $drawhash_ps_replace = 0", result_text)
        self.assertIn("global persist $layout_mode = 0", result_text)
        self.assertIn("$time = time", result_text)
        self.assertEqual(result_text.count("[CustomShaderDraw]"), 1)
        self.assertEqual(result_text.count("[CustomShaderFx]"), 1)
        self.assertGreater(result_text.index(marker), result_text.index("[Present]"))

    def test_moves_existing_drag_present_into_panel_present_before_binding(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            panel_dir = _write_panel_folder_text(root, PANEL_DRAG_INI)
            mod_dir = root / "mod"
            mod_dir.mkdir()
            ini_path = mod_dir / "character.ini"
            ini_path.write_text(DRAG_TARGET_INI, encoding="utf-8")

            node = _make_node(str(panel_dir))
            node.execute_postprocess(str(mod_dir))
            result_text = ini_path.read_text(encoding="utf-8")

        self.assertEqual(result_text.count("DRAG PRESENT BEGIN"), 1)
        self.assertNotIn("fserfrse", result_text)
        self.assertIn("$ssmtdrag_ui_detected_A", result_text)
        self.assertIn(
            "$ssmtdrag_ui_detected_A != -1 && $ssmtdrag_ui_detected_A != 4294967295",
            result_text,
        )
        ui_block = result_text[result_text.index("; --- AUTO-APPENDED UI PANEL UIPanel ---"):]
        self.assertLess(
            ui_block.index("DRAG PRESENT BEGIN"),
            ui_block.index("MODEL DRAG BINDING BEGIN"),
        )

    def test_drag_present_legacy_help_override_replaced_with_alt_gate(self):
        node = _make_node("")
        block = [
            "if $help == 1",
            "    $ssmtdrag_mode_A = 1",
            "else",
            "    $ssmtdrag_mode_A = $ssmtdrag_modifier_down_A",
            "endif",
            "if $ssmtdrag_ui_detected_A >= 0 && $ssmtdrag_ui_zone_A == 0",
            "    $is_dragging = 9",
            "endif",
        ]
        normalized = node._normalize_drag_present_variables(block, {
            "Constants": [
                "global $ssmtdrag_ui_detected_A = -1",
                "global $ssmtdrag_ui_zone_A = -1",
            ],
        })
        joined = "\n".join(normalized)
        self.assertIn("$ssmtdrag_mode_A = $ssmtdrag_modifier_down_A", joined)
        self.assertNotIn("if $help == 1", joined)
        self.assertNotIn("$ssmtdrag_ui_detected_fserfrse", joined)
        self.assertIn(
            "$ssmtdrag_ui_detected_A != -1 && $ssmtdrag_ui_detected_A != 4294967295",
            joined,
        )

    def test_rerun_preserves_relocated_drag_present_block(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            panel_dir = _write_panel_folder_text(root, PANEL_DRAG_INI)
            mod_dir = root / "mod"
            mod_dir.mkdir()
            ini_path = mod_dir / "character.ini"
            ini_path.write_text(DRAG_TARGET_INI, encoding="utf-8")

            first_node = _make_node(str(panel_dir))
            first_node.execute_postprocess(str(mod_dir))
            second_node = _make_node(str(panel_dir))
            second_node.execute_postprocess(str(mod_dir))
            result_text = ini_path.read_text(encoding="utf-8")

        self.assertEqual(result_text.count("DRAG PRESENT BEGIN"), 1)
        ui_block = result_text[result_text.index("; --- AUTO-APPENDED UI PANEL UIPanel ---"):]
        self.assertLess(
            ui_block.index("DRAG PRESENT BEGIN"),
            ui_block.index("MODEL DRAG BINDING BEGIN"),
        )

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
        self.assertEqual(second_text.count("SSMT UI PANEL PRESENT"), 0)

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
        self.assertGreater(
            second_text.index("; --- AUTO-APPENDED UI PANEL UIPanel ---"),
            second_text.index("; --- AUTO-APPENDED HEALTH DETECTION MODULE ---"),
        )

    def test_refresh_rewrites_only_panel_block_and_preserves_tail(self):
        stale_panel = (
            "; --- AUTO-APPENDED UI PANEL UIPanel ---\n"
            "[TextureOverrideCheckHash]\n"
            "hash = stalehash\n"
            "$active = 1\n"
            "\n"
            "[KeyHelp]\n"
            "key = no_ctrl no_alt home\n"
            "\n"
        )
        health_tail = (
            "; --- AUTO-APPENDED HEALTH DETECTION MODULE ---\n"
            "[CommandListHealth]\n"
            "$health = 1\n"
        )
        success, message, result_text, _ = self._refresh(
            TARGET_INI + "\n" + stale_panel + "\n" + health_tail
        )

        self.assertTrue(success, message)
        self.assertNotIn("stalehash", result_text)
        self.assertEqual(result_text.count("[TextureOverrideCheckHash]"), 1)
        self.assertEqual(result_text.count("[KeyHelp]"), 1)
        self.assertIn(health_tail, result_text)
        self.assertEqual(result_text.count("[Constants]"), 2)
        self.assertEqual(result_text.count("[Present]"), 2)
        self.assertIn("global persist $drawhash_ps_replace = 0", result_text)
        self.assertGreater(
            result_text.index("; --- AUTO-APPENDED UI PANEL UIPanel ---"),
            result_text.index("; --- AUTO-APPENDED HEALTH DETECTION MODULE ---"),
        )

    def test_refresh_appends_missing_panel_block_and_preserves_tail(self):
        health_tail = (
            "; --- AUTO-APPENDED HEALTH DETECTION MODULE ---\n"
            "[CommandListHealth]\n"
            "$health = 1\n"
        )
        success, message, result_text, _ = self._refresh(TARGET_INI + "\n" + health_tail)

        self.assertTrue(success, message)
        self.assertIn("; --- AUTO-APPENDED UI PANEL UIPanel ---", result_text)
        self.assertIn(health_tail, result_text)
        self.assertEqual(result_text.count("[TextureOverrideCheckHash]"), 1)
        self.assertEqual(result_text.count("[Constants]"), 2)
        self.assertGreater(
            result_text.index("; --- AUTO-APPENDED UI PANEL UIPanel ---"),
            result_text.index("; --- AUTO-APPENDED HEALTH DETECTION MODULE ---"),
        )

    def test_refresh_keeps_anim_driver_block_independent_from_body(self):
        driver_block = (
            "; --- ANIMATION DRIVER SECTION ---\n"
            "[Constants]\n"
            "global $driver_state = 0\n"
            "\n"
            "[Present]\n"
            "post $driver_state = 0\n"
            "; --- END ANIMATION DRIVER SECTION ---\n"
        )
        health_tail = (
            "; --- AUTO-APPENDED HEALTH DETECTION MODULE ---\n"
            "[CommandListHealth]\n"
            "$health = 1\n"
        )
        success, message, result_text, _ = self._refresh(
            driver_block + "\n" + TARGET_INI + "\n" + health_tail
        )

        self.assertTrue(success, message)
        self.assertEqual(result_text.count("[Constants]"), 3)
        self.assertEqual(result_text.count("[Present]"), 3)
        self.assertEqual(result_text.count("; --- ANIMATION DRIVER SECTION ---"), 1)
        self.assertEqual(result_text.count("; --- END ANIMATION DRIVER SECTION ---"), 1)
        self.assertIn("global $driver_state = 0", result_text)
        self.assertIn("global persist $drawhash_ps_replace = 0", result_text)
        self.assertIn("global persist $active", result_text)
        self.assertLess(
            result_text.index("; --- ANIMATION DRIVER SECTION ---"),
            result_text.index("global persist $drawhash_ps_replace = 0"),
        )
        self.assertLess(
            result_text.index("global persist $drawhash_ps_replace = 0"),
            result_text.index("; --- AUTO-APPENDED HEALTH DETECTION MODULE ---"),
        )

    def test_refresh_operator_is_in_registered_classes(self):
        self.assertIn(module.SSMT_OT_RefreshUIPanelExportSection, module.classes)

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

    def test_shared_shader_sections_are_kept_in_independent_bottom_block(self):
        target = TARGET_INI + """[CustomShaderDraw]
hs=null
vs=./res/draw_2d.hlsl
ps=./res/draw_2d.hlsl
blend=ADD SRC_ALPHA INV_SRC_ALPHA

"""
        result_text, _ = self._run(target_ini=target)
        self.assertEqual(result_text.count("[CustomShaderDraw]"), 2)

    def test_conflicting_non_shared_section_keeps_both_blocks(self):
        target = TARGET_INI + """[TextureOverrideCheckHash]
hash = otherhash
$active = 1

"""
        result_text, _ = self._run(target_ini=target)
        self.assertEqual(result_text.count("[TextureOverrideCheckHash]"), 2)
        self.assertIn("hash = otherhash", result_text)
        self.assertIn("hash = c209c22b", result_text)

    def test_panel_constants_keep_own_values_in_bottom_block(self):
        target = TARGET_INI.replace(
            "global persist $drawhash_ps_replace = 0",
            "global persist $drawhash_ps_replace = 0\nglobal persist $help = 1",
        )
        result_text, _ = self._run(target_ini=target)
        self.assertEqual(result_text.count("[Constants]"), 2)
        self.assertIn("global persist $help = 1", result_text)
        self.assertIn("global persist $help = 0", result_text)
        self.assertIn("global persist $layout_mode = 0", result_text)

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
        self.assertEqual(result_text.count("[Constants]"), 2)
        self.assertEqual(result_text.count("[Present]"), 2)
        self.assertEqual(result_text.count("[CustomShaderDraw]"), 1)
        self.assertEqual(result_text.count("[CustomShaderFx]"), 1)


    def test_multiple_ui_panels_keep_initial_chain_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            panel_dir = _write_panel_folder(root)
            mod_dir = root / "mod"
            mod_dir.mkdir()
            target_ini_path = mod_dir / "character.ini"
            target_ini_path.write_text(TARGET_INI, encoding="utf-8")

            first = _make_node(str(panel_dir), panel_name="FirstPanel")
            first.execute_postprocess(str(mod_dir))
            second = _make_node(str(panel_dir), panel_name="SecondPanel")
            second.execute_postprocess(str(mod_dir))

            result_text = target_ini_path.read_text(encoding="utf-8")
            first_marker = "; --- AUTO-APPENDED UI PANEL FirstPanel ---"
            second_marker = "; --- AUTO-APPENDED UI PANEL SecondPanel ---"
            self.assertLess(result_text.index(first_marker), result_text.index(second_marker))
            self.assertGreater(result_text.index(second_marker), result_text.index("[TextureOverride_drawhash]"))

if __name__ == "__main__":
    unittest.main()
