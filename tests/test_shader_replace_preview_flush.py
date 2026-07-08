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


class _FakeTexts(dict):
    def new(self, name):
        text = types.SimpleNamespace(_content="", clear=lambda: None, write=lambda value: None, as_string=lambda: "")
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


class _FakeTextBlock:
    def __init__(self, content):
        self._content = content

    def as_string(self):
        return self._content


class ShaderReplacePreviewFlushTests(unittest.TestCase):
    def test_toggle_preview_flushes_text_to_file_before_clearing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            shader_path = os.path.join(tmpdir, "abcd-ps_replace.txt")
            with open(shader_path, "w", encoding="utf-8") as f:
                f.write("old")

            text_name = "ShaderReplace_Node_World"
            _fake_texts.clear()
            _fake_texts[text_name] = _FakeTextBlock("new")

            node = types.SimpleNamespace(
                name="Node",
                preview_enabled=True,
                active_shader_index=0,
                shader_list=[types.SimpleNamespace(variant_name="World", shader_file_path=shader_path)],
            )
            tree = types.SimpleNamespace(nodes=types.SimpleNamespace(get=lambda _name: node))
            context = types.SimpleNamespace(space_data=types.SimpleNamespace(edit_tree=tree, node_tree=tree))

            operator = shader_replace_module.SSMT_OT_ShaderReplace_TogglePreview()
            operator.node_name = "Node"
            result = operator.execute(context)

            self.assertEqual(result, {"FINISHED"})
            with open(shader_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "new")
            self.assertNotIn(text_name, _fake_texts)


if __name__ == "__main__":
    unittest.main()
