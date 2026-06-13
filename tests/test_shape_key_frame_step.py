import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_shape_key_frame_step_test_pkg"
for package_name in (PKG, f"{PKG}.toolkit"):
    package = _install_module(package_name)
    package.__path__ = []


_install_module(
    "bpy",
    types=types.SimpleNamespace(Operator=object),
    props=types.SimpleNamespace(IntProperty=lambda **_kwargs: None),
)
_install_module(f"{PKG}.toolkit.at_multi_frame_split")


def _load_module(module_name, relative_path):
    module_path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(f"{PKG}.{module_name}", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


shape_key_creation = _load_module("toolkit.at_shape_key_creation", "toolkit/at_shape_key_creation.py")
ATP_OT_AddFrameShapeKeyPair = shape_key_creation.ATP_OT_AddFrameShapeKeyPair
ATP_OT_AddDefaultFrameShapeKeyPairs = shape_key_creation.ATP_OT_AddDefaultFrameShapeKeyPairs


class _FakePair:
    def __init__(self):
        self.end_frame = 0
        self.shape_key_name = ""
        self.is_processed = False


class _FakeCollection(list):
    def add(self):
        item = _FakePair()
        self.append(item)
        return item

    def clear(self):
        del self[:]

    def remove(self, index):
        del self[index]


class ShapeKeyFrameStepTests(unittest.TestCase):
    def test_add_frame_shape_key_pair_uses_configured_step(self):
        props = types.SimpleNamespace(
            frame_shape_key_pairs=_FakeCollection(),
            frame_shape_key_index=0,
            frame_shape_key_step=2,
            multi_object_start_frame=1,
        )
        context = types.SimpleNamespace(scene=types.SimpleNamespace(atp_props=props))

        operator = ATP_OT_AddFrameShapeKeyPair()
        operator.report = lambda *_args, **_kwargs: None

        operator.execute(context)
        operator.execute(context)

        self.assertEqual(
            [(pair.end_frame, pair.shape_key_name) for pair in props.frame_shape_key_pairs],
            [(3, "Motion_Key_1"), (5, "Motion_Key_2")],
        )

    def test_add_default_frame_shape_key_pairs_uses_configured_step(self):
        props = types.SimpleNamespace(
            frame_shape_key_pairs=_FakeCollection(),
            frame_shape_key_index=0,
            frame_shape_key_step=1,
            multi_object_start_frame=1,
        )
        context = types.SimpleNamespace(scene=types.SimpleNamespace(atp_props=props))

        operator = ATP_OT_AddDefaultFrameShapeKeyPairs()
        operator.report = lambda *_args, **_kwargs: None

        operator.execute(context)

        self.assertEqual(
            [(pair.end_frame, pair.shape_key_name) for pair in props.frame_shape_key_pairs],
            [(2, "1"), (3, "2"), (4, "3"), (5, "4"), (6, "5")],
        )


if __name__ == "__main__":
    unittest.main()
