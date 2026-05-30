import importlib.util
import sys
import types
import unittest
from pathlib import Path


class _FakeSocket:
    def __init__(self, default_value=None):
        self.default_value = default_value


class _FakeBackgroundNode:
    bl_idname = "ShaderNodeBackground"

    def __init__(self):
        self.inputs = {"Color": _FakeSocket((0.0, 0.0, 0.0, 1.0))}


_fake_background = _FakeBackgroundNode()
_fake_scene = types.SimpleNamespace(
    render=types.SimpleNamespace(film_transparent=False),
    display_settings=types.SimpleNamespace(display_device="None"),
    view_settings=types.SimpleNamespace(view_transform="Filmic"),
    world=types.SimpleNamespace(
        color=(0.0, 0.0, 0.0),
        use_nodes=True,
        node_tree=types.SimpleNamespace(nodes=[_fake_background]),
    ),
)

_fake_bpy = types.SimpleNamespace(
    context=types.SimpleNamespace(scene=_fake_scene),
)
sys.modules["bpy"] = _fake_bpy


module_path = Path(__file__).resolve().parents[1] / "common" / "import_scene_settings.py"
spec = importlib.util.spec_from_file_location("import_scene_settings", module_path)
import_scene_settings = importlib.util.module_from_spec(spec)
sys.modules["import_scene_settings"] = import_scene_settings
spec.loader.exec_module(import_scene_settings)


class ImportSceneSettingsTests(unittest.TestCase):
    """测试导入场景设置：渲染环境的默认值配置"""

    def test_apply_import_render_environment_sets_expected_scene_defaults(self):
        """测试 apply_import_render_environment 正确设置场景默认值"""
        _fake_scene.render.film_transparent = False
        _fake_scene.display_settings.display_device = "None"
        _fake_scene.view_settings.view_transform = "Filmic"
        _fake_scene.world.color = (0.0, 0.0, 0.0)
        _fake_background.inputs["Color"].default_value = (0.0, 0.0, 0.0, 1.0)

        import_scene_settings.apply_import_render_environment()

        self.assertTrue(_fake_scene.render.film_transparent)
        self.assertEqual(_fake_scene.display_settings.display_device, "sRGB")
        self.assertEqual(_fake_scene.view_settings.view_transform, "Standard")
        self.assertEqual(_fake_scene.world.color, (1.0, 1.0, 1.0))
        self.assertEqual(_fake_background.inputs["Color"].default_value, (1.0, 1.0, 1.0, 1.0))


if __name__ == "__main__":
    unittest.main()
