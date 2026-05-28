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


PKG = "_ntmi_texture_slot_refresh_test_pkg"
for package_name in (PKG, f"{PKG}.ui", f"{PKG}.ui.ntmi_modimp", f"{PKG}.common"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeObject(dict):
    def __init__(self, name):
        super().__init__()
        self.name = name
        self.users_collection = []


_install_module("bpy", data=types.SimpleNamespace())
_install_module(
    f"{PKG}.common.workspace_helper",
    WorkSpaceHelper=types.SimpleNamespace(
        get_submesh_folder_path=lambda unique_str: unique_str,
        parse_lod_unique_str=lambda unique_str: (
            "LOD0",
            str(unique_str).split(".", 1)[1] if "." in str(unique_str) else str(unique_str),
        ),
    ),
)
_install_module(
    f"{PKG}.common.submesh_metadata",
    SubmeshMetadataResolver=types.SimpleNamespace(resolve=lambda _unique_str: types.SimpleNamespace(
        extract_gametype_folder_path="",
        texture_markup_info_list=[],
    )),
)
_install_module(
    f"{PKG}.common.texture_metadata_helper",
    TextureMetadataResolver=types.SimpleNamespace(
        normalize_texture_markup_info_list=lambda items: items,
    ),
)
_install_module(
    f"{PKG}.ui.ntmi_modimp.prefix_property_cache",
    update_prefix_record_for_object=lambda *_args, **_kwargs: None,
    replace_prefix_record_props=lambda *_args, **_kwargs: None,
    get_prefix_record_props=lambda _name: {},
    has_prefix_record=lambda _name: False,
)


module_path = Path(__file__).resolve().parents[1] / "ui" / "ntmi_modimp" / "texture_slot_refresh.py"
spec = importlib.util.spec_from_file_location(
    f"{PKG}.ui.ntmi_modimp.texture_slot_refresh",
    module_path,
)
refresh_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = refresh_module
spec.loader.exec_module(refresh_module)


class TextureSlotRefreshTests(unittest.TestCase):
    def setUp(self):
        refresh_module.build_texture_slots_from_workspace_unique = (
            refresh_module.__dict__["build_texture_slots_from_workspace_unique"]
        )
        refresh_module.get_prefix_record_props = lambda _name: {}
        refresh_module.has_prefix_record = lambda _name: False
        refresh_module.replace_prefix_record_props = lambda *_args, **_kwargs: None

    def test_refresh_object_texture_slots_rewrites_runtime_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            type_dir = os.path.join(temp_dir, "TYPE_Test")
            deduped_dir = os.path.join(temp_dir, "DedupedTextures")
            os.makedirs(type_dir)
            os.makedirs(deduped_dir)

            source_path = os.path.join(type_dir, "abc12345-12-0-DiffuseMap.dds")
            with open(source_path, "wb") as file_obj:
                file_obj.write(b"slot")

            refresh_module._resolve_type_dir = lambda _workspace_unique_str: type_dir
            refresh_module._resolve_deduped_texture_dir = lambda _workspace_unique_str: deduped_dir
            refresh_module.SubmeshMetadataResolver = types.SimpleNamespace(
                resolve=lambda _workspace_unique_str: types.SimpleNamespace(
                    texture_markup_info_list=[
                        types.SimpleNamespace(
                            mark_name="DiffuseMap",
                            mark_type="Slot",
                            mark_hash="deadbeef",
                            mark_slot="ps-t0",
                            mark_filename="deadbeef-DiffuseMap.dds",
                        )
                    ]
                )
            )

            obj = _FakeObject("LOD0.abc12345-12-0.Body")
            obj["modimp_workspace_unique_str"] = "LOD0.abc12345-12-0"

            slots = refresh_module.refresh_object_texture_slots(obj)

            self.assertIn("ps-t0", slots)
            self.assertEqual(slots["ps-t0"]["mark_hash"], "deadbeef")
            self.assertEqual(slots["ps-t0"]["source_path"], source_path)
            self.assertIn("modimp_texture_slots", obj)

    def test_refresh_object_texture_slots_clears_stale_runtime_contract_when_slots_missing(self):
        obj = _FakeObject("LOD0.abc12345-12-0.Body")
        obj["modimp_workspace_unique_str"] = "LOD0.abc12345-12-0"
        obj["modimp_texture_slots"] = '{"stale": true}'

        owner = _FakeObject("LOD0.abc12345-12-0")
        owner["modimp_texture_slots"] = '{"stale_owner": true}'
        obj.users_collection = [owner]

        original_builder = refresh_module.build_texture_slots_from_workspace_unique
        refresh_module.build_texture_slots_from_workspace_unique = lambda _workspace_unique_str: {}

        try:
            slots = refresh_module.refresh_object_texture_slots(obj, extra_owners=obj.users_collection)
        finally:
            refresh_module.build_texture_slots_from_workspace_unique = original_builder

        self.assertEqual(slots, {})
        self.assertNotIn("modimp_texture_slots", obj)
        self.assertNotIn("modimp_texture_slots", owner)

    def test_build_texture_slots_keeps_unresolved_slots_for_runtime_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            type_dir = os.path.join(temp_dir, "TYPE_Test")
            deduped_dir = os.path.join(temp_dir, "DedupedTextures")
            os.makedirs(type_dir)
            os.makedirs(deduped_dir)

            source_path = os.path.join(type_dir, "abc12345-12-0-DiffuseMap.dds")
            with open(source_path, "wb") as file_obj:
                file_obj.write(b"slot")

            refresh_module._resolve_type_dir = lambda _workspace_unique_str: type_dir
            refresh_module._resolve_deduped_texture_dir = lambda _workspace_unique_str: deduped_dir
            refresh_module.SubmeshMetadataResolver = types.SimpleNamespace(
                resolve=lambda _workspace_unique_str: types.SimpleNamespace(
                    texture_markup_info_list=[
                        types.SimpleNamespace(
                            mark_name="DiffuseMap",
                            mark_type="Slot",
                            mark_hash="deadbeef",
                            mark_slot="ps-t0",
                            mark_filename="deadbeef-DiffuseMap.dds",
                        ),
                        types.SimpleNamespace(
                            mark_name="LightMap",
                            mark_type="Slot",
                            mark_hash="cafebabe",
                            mark_slot="ps-t1",
                            mark_filename="cafebabe-LightMap.dds",
                        ),
                    ]
                )
            )

            slots = refresh_module.build_texture_slots_from_workspace_unique("LOD0.abc12345-12-0")

            self.assertEqual(slots["ps-t0"]["source_path"], source_path)
            self.assertEqual(slots["ps-t1"]["source_path"], "")
            self.assertEqual(slots["ps-t1"]["extension"], "dds")
            self.assertEqual(slots["ps-t1"]["mark_name"], "LightMap")

    def test_refresh_object_texture_slots_uses_prefix_cache_workspace_unique_before_stale_object_prop(self):
        obj = _FakeObject("LOD0.abc12345-12-0.Body")
        obj["modimp_workspace_unique_str"] = "LOD0.stale-1-0"

        calls = []
        refresh_module.get_prefix_record_props = lambda _name: {
            "modimp_workspace_unique_str": "LOD0.abc12345-12-0",
        }
        refresh_module.has_prefix_record = lambda _name: True
        refresh_module.build_texture_slots_from_workspace_unique = lambda workspace_unique_str: calls.append(workspace_unique_str) or {}

        refresh_module.refresh_object_texture_slots(obj)

        self.assertEqual(calls, ["LOD0.abc12345-12-0"])


if __name__ == "__main__":
    unittest.main()
