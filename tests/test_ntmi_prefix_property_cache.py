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


PKG = "_ntmi_prefix_property_cache_test_pkg"
for package_name in (PKG, f"{PKG}.ui", f"{PKG}.ui.ntmi_modimp", f"{PKG}.common"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeText:
    def __init__(self, name):
        self.name = name
        self._content = ""

    def clear(self):
        self._content = ""

    def write(self, value):
        self._content += str(value)

    def as_string(self):
        return self._content


class _FakeTexts(dict):
    def new(self, name):
        text = _FakeText(name)
        self[name] = text
        return text


class _FakeObject(dict):
    def __init__(self, name):
        super().__init__()
        self.name = name


_fake_bpy = types.SimpleNamespace(
    data=types.SimpleNamespace(texts=_FakeTexts()),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module(
    f"{PKG}.common.object_prefix_helper",
    ObjectPrefixHelper=types.SimpleNamespace(
        extract_prefix_info=lambda name: (name.split(".", 1)[0], ".") if "." in str(name) else None,
        normalize_prefix=lambda value: str(value or "").strip(),
        parse_prefix_parts=lambda prefix: {
            "lod_name": prefix.split(".", 1)[0] if "." in str(prefix) else "",
            "bare_unique_str": prefix.split(".", 1)[1] if "." in str(prefix) else str(prefix or ""),
        },
    ),
)
_install_module(
    f"{PKG}.ui.ntmi_modimp.runtime_cache",
    MODIMP_COLLECTOR_PROPS=("modimp_collector_collect_key",),
    MODIMP_PATH_PROPS=("modimp_vb0_buf_path",),
    prefix_identity_matches=lambda target, source: target[1] == source[1] and (
        (target[0] and source[0] in {"", target[0]}) or (not target[0] and source[0] in {"", "lod0"})
    ),
)


module_path = Path(__file__).resolve().parents[1] / "ui" / "ntmi_modimp" / "prefix_property_cache.py"
spec = importlib.util.spec_from_file_location(
    f"{PKG}.ui.ntmi_modimp.prefix_property_cache",
    module_path,
)
cache_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cache_module
spec.loader.exec_module(cache_module)


class PrefixPropertyCacheTests(unittest.TestCase):
    def setUp(self):
        _fake_bpy.data.texts.clear()

    def test_update_and_read_prefix_record_props_round_trip(self):
        obj = _FakeObject("LOD0.abc12345-12-0.Body")
        obj["modimp_vb0_buf_path"] = "X:/Workspace/LOD0/abc12345-12-0/Position.buf"
        obj["modimp_collector_collect_key"] = "collect-1"

        cache_module.update_prefix_record_for_object(obj)
        props = cache_module.get_prefix_record_props("LOD0.abc12345-12-0.Other")

        self.assertEqual(props["modimp_vb0_buf_path"], "X:/Workspace/LOD0/abc12345-12-0/Position.buf")
        self.assertEqual(props["modimp_collector_collect_key"], "collect-1")


if __name__ == "__main__":
    unittest.main()
