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


PKG = "_shader_replace_preview_flush_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeTextBlock:
    def __init__(self, content=""):
        self._content = content

    def clear(self):
        self._content = ""

    def write(self, value):
        self._content += value

    def as_string(self):
        return self._content


class _FakeTexts(dict):
    def new(self, name):
        text = _FakeTextBlock()
        self[name] = text
        return text

    def remove(self, text):
        for key, value in list(self.items()):
            if value is text:
                del self[key]


_fake_texts = _FakeTexts()
_install_module(
    "bpy",
    data=types.SimpleNamespace(texts=_fake_texts, node_groups=[]),
    path=types.SimpleNamespace(abspath=lambda path: path),
    app=types.SimpleNamespace(timers=types.SimpleNamespace(register=lambda *_args, **_kwargs: None, unregister=lambda *_args, **_kwargs: None)),
    types=types.SimpleNamespace(Node=object, PropertyGroup=object, Operator=object),
    props=types.SimpleNamespace(
        StringProperty=lambda **_kwargs: None,
        CollectionProperty=lambda **_kwargs: None,
        BoolProperty=lambda **_kwargs: None,
        IntProperty=lambda **_kwargs: None,
    ),
)
_install_module("bpy.types", Node=object, PropertyGroup=object, Operator=object)
_install_module(
    "bpy.props",
    StringProperty=lambda **_kwargs: None,
    CollectionProperty=lambda **_kwargs: None,
    BoolProperty=lambda **_kwargs: None,
    IntProperty=lambda **_kwargs: None,
)
_install_module(f"{PKG}.blueprint.node_base", SSMTNodeBase=object, SSMTSocketObject=object)


module_path = Path(__file__).resolve().parents[1] / "blueprint" / "node_shader_replace.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.node_shader_replace", module_path)
shader_replace_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = shader_replace_module
spec.loader.exec_module(shader_replace_module)


class ShaderReplacePreviewFlushTests(unittest.TestCase):
    def setUp(self):
        _fake_texts.clear()
        shader_replace_module._file_signature_cache.clear()
        shader_replace_module.bpy.data.node_groups = []

    def test_toggle_preview_flushes_text_to_file_before_clearing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            shader_path = os.path.join(tmpdir, "abcd-ps_replace.txt")
            with open(shader_path, "w", encoding="utf-8") as f:
                f.write("old")

            node = types.SimpleNamespace(
                name="Node",
                preview_enabled=True,
                active_shader_index=0,
                shader_list=[types.SimpleNamespace(variant_name="World", shader_file_path=shader_path)],
            )
            text_name = shader_replace_module._get_text_block_name(node, node.shader_list[0])
            _fake_texts[text_name] = _FakeTextBlock("new")
            tree = types.SimpleNamespace(nodes=types.SimpleNamespace(get=lambda _name: node))
            context = types.SimpleNamespace(space_data=types.SimpleNamespace(edit_tree=tree, node_tree=tree))

            operator = shader_replace_module.SSMT_OT_ShaderReplace_TogglePreview()
            operator.node_name = "Node"
            result = operator.execute(context)

            self.assertEqual(result, {"FINISHED"})
            with open(shader_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "new")
            self.assertNotIn(text_name, _fake_texts)

    def test_renaming_node_and_tree_during_preview_keeps_original_text_binding(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            shader_path = os.path.join(tmpdir, "renamed-preview.txt")
            with open(shader_path, "w", encoding="utf-8") as f:
                f.write("old")

            item = types.SimpleNamespace(
                variant_name="World",
                shader_file_path=shader_path,
            )
            tree_data = types.SimpleNamespace(name="OriginalTree")
            node = types.SimpleNamespace(
                name="OriginalNode",
                id_data=tree_data,
                preview_enabled=True,
                active_shader_index=0,
                shader_list=[item],
            )
            text_name = shader_replace_module._get_text_block_name(node, item)
            _fake_texts[text_name] = _FakeTextBlock("edited-after-rename")

            node.name = "RenamedNode"
            tree_data.name = "RenamedTree"
            self.assertEqual(
                shader_replace_module._get_text_block_name(node, item),
                text_name,
            )

            tree = types.SimpleNamespace(nodes=types.SimpleNamespace(get=lambda _name: node))
            context = types.SimpleNamespace(
                space_data=types.SimpleNamespace(edit_tree=tree, node_tree=tree)
            )
            operator = shader_replace_module.SSMT_OT_ShaderReplace_TogglePreview()
            operator.node_name = node.name

            result = operator.execute(context)

            self.assertEqual(result, {"FINISHED"})
            self.assertFalse(node.preview_enabled)
            self.assertNotIn(text_name, _fake_texts)
            self.assertEqual(getattr(item, "preview_text_block_name", ""), "")
            with open(shader_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "edited-after-rename")

    def test_copied_node_does_not_inherit_active_preview_binding(self):
        item = types.SimpleNamespace(preview_text_block_name="ShaderReplace_Shared")
        copied_node = types.SimpleNamespace(
            preview_enabled=True,
            shader_list=[item],
        )

        shader_replace_module.SSMTNode_ShaderReplace.copy(copied_node, object())

        self.assertFalse(copied_node.preview_enabled)
        self.assertEqual(item.preview_text_block_name, "")

    def test_load_failure_does_not_create_empty_writable_text_block(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            shader_path = os.path.join(tmpdir, "invalid.txt")
            with open(shader_path, "wb") as f:
                f.write(b"\xff\xfe\x00\x00")

            node = types.SimpleNamespace(
                name="Node",
                active_shader_index=0,
                shader_list=[types.SimpleNamespace(variant_name="World", shader_file_path=shader_path)],
            )

            result = shader_replace_module._load_shader_into_text_block(node)

            self.assertFalse(result)
            text_name = shader_replace_module._get_text_block_name(node, node.shader_list[0])
            self.assertNotIn(text_name, _fake_texts)

    def test_load_resolves_blender_relative_shader_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            shader_path = os.path.join(tmpdir, "relative.txt")
            with open(shader_path, "w", encoding="utf-8") as f:
                f.write("shader")
            original_abspath = shader_replace_module.bpy.path.abspath
            shader_replace_module.bpy.path.abspath = (
                lambda path: shader_path if path == "//relative.txt" else path
            )
            node = types.SimpleNamespace(
                name="Node",
                active_shader_index=0,
                shader_list=[
                    types.SimpleNamespace(
                        variant_name="World",
                        shader_file_path="//relative.txt",
                    )
                ],
            )

            try:
                result = shader_replace_module._load_shader_into_text_block(node)
            finally:
                shader_replace_module.bpy.path.abspath = original_abspath

            text_name = shader_replace_module._get_text_block_name(node, node.shader_list[0])
            self.assertTrue(result)
            self.assertEqual(_fake_texts[text_name].as_string(), "shader")

    def test_same_node_name_in_different_trees_uses_distinct_text_blocks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path_a = os.path.join(tmpdir, "a.txt")
            path_b = os.path.join(tmpdir, "b.txt")
            with open(path_a, "w", encoding="utf-8") as f:
                f.write("shader-a")
            with open(path_b, "w", encoding="utf-8") as f:
                f.write("shader-b")

            node_a = types.SimpleNamespace(
                name="Shader Replace",
                id_data=types.SimpleNamespace(name="TreeA"),
                active_shader_index=0,
                shader_list=[types.SimpleNamespace(variant_name="World", shader_file_path=path_a)],
            )
            node_b = types.SimpleNamespace(
                name="Shader Replace",
                id_data=types.SimpleNamespace(name="TreeB"),
                active_shader_index=0,
                shader_list=[types.SimpleNamespace(variant_name="World", shader_file_path=path_b)],
            )

            self.assertTrue(shader_replace_module._load_shader_into_text_block(node_a))
            self.assertTrue(shader_replace_module._load_shader_into_text_block(node_b))

            text_name_a = shader_replace_module._get_text_block_name(node_a, node_a.shader_list[0])
            text_name_b = shader_replace_module._get_text_block_name(node_b, node_b.shader_list[0])
            self.assertNotEqual(text_name_a, text_name_b)
            self.assertEqual(_fake_texts[text_name_a].as_string(), "shader-a")
            self.assertEqual(_fake_texts[text_name_b].as_string(), "shader-b")

    def test_duplicate_variant_names_in_same_node_use_distinct_text_blocks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path_a = os.path.join(tmpdir, "a.txt")
            path_b = os.path.join(tmpdir, "b.txt")
            with open(path_a, "w", encoding="utf-8") as f:
                f.write("shader-a")
            with open(path_b, "w", encoding="utf-8") as f:
                f.write("shader-b")

            node = types.SimpleNamespace(
                name="Shader Replace",
                id_data=types.SimpleNamespace(name="Tree"),
                active_shader_index=0,
                shader_list=[
                    types.SimpleNamespace(variant_name="World", shader_file_path=path_a),
                    types.SimpleNamespace(variant_name="World", shader_file_path=path_b),
                ],
            )

            self.assertTrue(shader_replace_module._load_shader_into_text_block(node))
            node.active_shader_index = 1
            self.assertTrue(shader_replace_module._load_shader_into_text_block(node))

            text_name_a = shader_replace_module._get_text_block_name(node, node.shader_list[0])
            text_name_b = shader_replace_module._get_text_block_name(node, node.shader_list[1])
            self.assertNotEqual(text_name_a, text_name_b)
            self.assertEqual(_fake_texts[text_name_a].as_string(), "shader-a")
            self.assertEqual(_fake_texts[text_name_b].as_string(), "shader-b")

    def test_text_block_name_is_stable_and_short_for_long_unicode_names(self):
        item = types.SimpleNamespace(variant_name="世界变体" * 30, shader_file_path="shader.txt")
        node = types.SimpleNamespace(
            name="着色器替换节点" * 30,
            id_data=types.SimpleNamespace(name="嵌套蓝图" * 30),
            shader_list=[item],
        )

        first_name = shader_replace_module._get_text_block_name(node, item)
        second_name = shader_replace_module._get_text_block_name(node, item)

        self.assertEqual(first_name, second_name)
        self.assertLessEqual(len(first_name.encode("utf-8")), 63)

    def test_first_timer_tick_preserves_text_edits_after_initial_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            shader_path = os.path.join(tmpdir, "abcd-ps_replace.txt")
            with open(shader_path, "w", encoding="utf-8") as f:
                f.write("old")

            node = types.SimpleNamespace(
                name="Node",
                bl_idname="SSMTNode_ShaderReplace",
                preview_enabled=True,
                active_shader_index=0,
                shader_list=[types.SimpleNamespace(variant_name="World", shader_file_path=shader_path)],
            )
            tree = types.SimpleNamespace(bl_idname="SSMTBlueprintTreeType", nodes=[node])
            shader_replace_module.bpy.data.node_groups = [tree]

            self.assertTrue(shader_replace_module._load_shader_into_text_block(node))
            text_name = shader_replace_module._get_text_block_name(node, node.shader_list[0])
            text_block = _fake_texts[text_name]
            text_block.clear()
            text_block.write("edited")

            shader_replace_module._shader_replace_timer_callback()

            self.assertEqual(text_block.as_string(), "edited")
            with open(shader_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "edited")

    def test_closing_preview_flushes_every_open_shader_text_block(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            world_path = os.path.join(tmpdir, "world.txt")
            nonworld_path = os.path.join(tmpdir, "nonworld.txt")
            for path in (world_path, nonworld_path):
                with open(path, "w", encoding="utf-8") as f:
                    f.write("old")

            node = types.SimpleNamespace(
                name="Node",
                preview_enabled=True,
                active_shader_index=1,
                shader_list=[
                    types.SimpleNamespace(variant_name="World", shader_file_path=world_path),
                    types.SimpleNamespace(variant_name="NonWorld", shader_file_path=nonworld_path),
                ],
            )
            world_text_name = shader_replace_module._get_text_block_name(node, node.shader_list[0])
            nonworld_text_name = shader_replace_module._get_text_block_name(node, node.shader_list[1])
            _fake_texts[world_text_name] = _FakeTextBlock("world-edited")
            _fake_texts[nonworld_text_name] = _FakeTextBlock("nonworld-edited")
            tree = types.SimpleNamespace(nodes=types.SimpleNamespace(get=lambda _name: node))
            context = types.SimpleNamespace(space_data=types.SimpleNamespace(edit_tree=tree, node_tree=tree))

            operator = shader_replace_module.SSMT_OT_ShaderReplace_TogglePreview()
            operator.node_name = "Node"
            result = operator.execute(context)

            self.assertEqual(result, {"FINISHED"})
            with open(world_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "world-edited")
            with open(nonworld_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "nonworld-edited")

    def test_failed_flush_keeps_preview_and_text_block_open(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            node = types.SimpleNamespace(
                name="Node",
                preview_enabled=True,
                active_shader_index=0,
                shader_list=[types.SimpleNamespace(variant_name="World", shader_file_path=tmpdir)],
            )
            text_name = shader_replace_module._get_text_block_name(node, node.shader_list[0])
            _fake_texts[text_name] = _FakeTextBlock("unsaved")
            tree = types.SimpleNamespace(nodes=types.SimpleNamespace(get=lambda _name: node))
            context = types.SimpleNamespace(space_data=types.SimpleNamespace(edit_tree=tree, node_tree=tree))
            reports = []

            operator = shader_replace_module.SSMT_OT_ShaderReplace_TogglePreview()
            operator.node_name = "Node"
            operator.report = lambda kinds, message: reports.append((kinds, message))
            result = operator.execute(context)

            self.assertEqual(result, {"CANCELLED"})
            self.assertTrue(node.preview_enabled)
            self.assertIn(text_name, _fake_texts)
            self.assertTrue(any("写入" in message for _kinds, message in reports))

    def test_shutdown_flushes_and_closes_active_preview_sessions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            shader_path = os.path.join(tmpdir, "shutdown.txt")
            with open(shader_path, "w", encoding="utf-8") as f:
                f.write("old")
            item = types.SimpleNamespace(variant_name="World", shader_file_path=shader_path)
            node = types.SimpleNamespace(
                name="Node",
                bl_idname="SSMTNode_ShaderReplace",
                preview_enabled=True,
                active_shader_index=0,
                shader_list=[item],
            )
            text_name = shader_replace_module._get_text_block_name(node, item)
            _fake_texts[text_name] = _FakeTextBlock("edited-before-unregister")
            shader_replace_module.bpy.data.node_groups = [
                types.SimpleNamespace(
                    bl_idname="SSMTBlueprintTreeType",
                    nodes=[node],
                )
            ]

            result = shader_replace_module._shutdown_preview_sessions()

            self.assertTrue(result)
            self.assertFalse(node.preview_enabled)
            self.assertNotIn(text_name, _fake_texts)
            with open(shader_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "edited-before-unregister")

    def test_preview_mode_blocks_mutating_operators(self):
        item = types.SimpleNamespace(
            variant_name="World",
            shader_file_path="shader.txt",
            shader_hash="original",
        )
        node = types.SimpleNamespace(
            name="Node",
            preview_enabled=True,
            active_shader_index=0,
            shader_list=[item],
        )
        tree = types.SimpleNamespace(nodes=types.SimpleNamespace(get=lambda _name: node))
        context = types.SimpleNamespace(space_data=types.SimpleNamespace(edit_tree=tree, node_tree=tree))

        operators = (
            shader_replace_module.SSMT_OT_ShaderReplace_AddItem(),
            shader_replace_module.SSMT_OT_ShaderReplace_RemoveItem(),
            shader_replace_module.SSMT_OT_ShaderReplace_SelectFile(),
            shader_replace_module.SSMT_OT_ShaderReplace_ParseHash(),
        )
        for operator in operators:
            operator.node_name = "Node"
            operator.item_index = 0
            operator.filepath = "replacement.txt"
            self.assertEqual(operator.execute(context), {"CANCELLED"})

        self.assertEqual(node.shader_list, [item])
        self.assertEqual(item.shader_file_path, "shader.txt")
        self.assertEqual(item.shader_hash, "original")

    def test_preview_rejects_duplicate_shader_file_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            shader_path = os.path.join(tmpdir, "shared.txt")
            with open(shader_path, "w", encoding="utf-8") as f:
                f.write("shader")

            node = types.SimpleNamespace(
                name="Node",
                preview_enabled=False,
                active_shader_index=0,
                shader_list=[
                    types.SimpleNamespace(variant_name="World", shader_file_path=shader_path),
                    types.SimpleNamespace(variant_name="NonWorld", shader_file_path=shader_path),
                ],
            )
            tree = types.SimpleNamespace(nodes=types.SimpleNamespace(get=lambda _name: node))
            context = types.SimpleNamespace(space_data=types.SimpleNamespace(edit_tree=tree, node_tree=tree))
            reports = []
            operator = shader_replace_module.SSMT_OT_ShaderReplace_TogglePreview()
            operator.node_name = "Node"
            operator.report = lambda kinds, message: reports.append((kinds, message))

            result = operator.execute(context)

            self.assertEqual(result, {"CANCELLED"})
            self.assertFalse(node.preview_enabled)
            self.assertEqual(_fake_texts, {})
            self.assertTrue(any("占用" in message for _kinds, message in reports))

    def test_select_and_parse_hash_reject_negative_item_index(self):
        item = types.SimpleNamespace(
            variant_name="World",
            shader_file_path="shader.txt",
            shader_hash="original",
        )
        node = types.SimpleNamespace(
            name="Node",
            preview_enabled=False,
            active_shader_index=0,
            shader_list=[item],
        )
        tree = types.SimpleNamespace(nodes=types.SimpleNamespace(get=lambda _name: node))
        context = types.SimpleNamespace(space_data=types.SimpleNamespace(edit_tree=tree, node_tree=tree))

        select_operator = shader_replace_module.SSMT_OT_ShaderReplace_SelectFile()
        select_operator.node_name = "Node"
        select_operator.item_index = -1
        select_operator.filepath = "replacement.txt"
        parse_operator = shader_replace_module.SSMT_OT_ShaderReplace_ParseHash()
        parse_operator.node_name = "Node"
        parse_operator.item_index = -1

        self.assertEqual(select_operator.execute(context), {"CANCELLED"})
        self.assertEqual(parse_operator.execute(context), {"CANCELLED"})
        self.assertEqual(item.shader_file_path, "shader.txt")
        self.assertEqual(item.shader_hash, "original")


if __name__ == "__main__":
    unittest.main()
