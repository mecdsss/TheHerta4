import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_ntemi_workspace_import_routing_test_pkg"
for package_name in (PKG, f"{PKG}.ui", f"{PKG}.ui.ntmi_modimp", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeOperatorBase:
    pass


class _FakeCollection:
    def __init__(self, name):
        self.name = name
        self.children = types.SimpleNamespace(link=lambda _child: None)


class _FakeImportedObject(dict):
    def __init__(self, name=""):
        super().__init__()
        self.name = name
        self.data = types.SimpleNamespace(name=name)
        self.type = "MESH"
        self.users_collection = []

    def as_pointer(self):
        return id(self)


class _FakeNode:
    def __init__(self):
        self.inputs = [types.SimpleNamespace(is_linked=False)]
        self.outputs = [object()]
        self.parent = None
        self.location = (0, 0)
        self.label = ""

    def update(self):
        return None


class _FakeNodeGroup:
    def __init__(self, name):
        self.name = name
        self.use_fake_user = False
        self.nodes = types.SimpleNamespace(
            new=lambda _type: _FakeNode(),
            remove=lambda _node: None,
        )
        self.links = types.SimpleNamespace(new=lambda *_args, **_kwargs: None)


_fake_bpy = types.SimpleNamespace(
    data=types.SimpleNamespace(
        node_groups=types.SimpleNamespace(new=lambda name, type: _FakeNodeGroup(name)),
    ),
    context=types.SimpleNamespace(
        scene=types.SimpleNamespace(
            collection=types.SimpleNamespace(children=types.SimpleNamespace(link=lambda _coll: None)),
        )
    ),
    types=types.SimpleNamespace(Operator=_FakeOperatorBase, Collection=object, OperatorFileListElement=object),
    props=types.SimpleNamespace(
        StringProperty=lambda **_kwargs: None,
        CollectionProperty=lambda **_kwargs: None,
    ),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module("bpy_extras")


class _FakeImportHelper:
    pass


_install_module("bpy_extras.io_utils", ImportHelper=_FakeImportHelper)

_install_module(
    f"{PKG}.utils.collection_utils",
    CollectionColor=types.SimpleNamespace(Blue="Blue", Orange="Orange", Red="Red"),
    CollectionUtils=types.SimpleNamespace(
        create_new_collection=lambda collection_name, color_tag=None: _FakeCollection(collection_name),
        select_collection_objects=lambda _collection: None,
    ),
)
_install_module(
    f"{PKG}.utils.json_utils",
    JsonUtils=types.SimpleNamespace(SaveToFile=lambda **_kwargs: None),
)
_install_module(
    f"{PKG}.utils.timer_utils",
    TimerUtils=types.SimpleNamespace(Start=lambda *_args: None, End=lambda *_args: None),
)
_install_module(
    f"{PKG}.utils.translate_utils",
    TR=types.SimpleNamespace(translate=lambda text: text),
)
_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(
        path_workspace_folder=lambda: "X:/Workspace/NTEMI/Test",
        get_workspace_name=lambda: "测试工作空间",
        logic_name="NTEMI",
    ),
)
_install_module(
    f"{PKG}.common.global_properties",
    GlobalProterties=types.SimpleNamespace(enable_non_mirror_workflow=lambda: False),
)
_install_module(
    f"{PKG}.common.logic_name",
    LogicName=types.SimpleNamespace(NTEMI="NTEMI"),
)
_install_module(
    f"{PKG}.common.non_mirror_workflow",
    NonMirrorWorkflowHelper=types.SimpleNamespace(process_imported_objects=lambda _objects: None),
)
_install_module(
    f"{PKG}.common.object_prefix_helper",
    ObjectPrefixHelper=types.SimpleNamespace(
        extract_prefix_info=lambda _name: None,
        replace_prefix=lambda display_name, workspace_unique_str, *_args: workspace_unique_str if not display_name else workspace_unique_str,
    ),
)
_install_module(
    f"{PKG}.common.ssmt_import_helper",
    SSMTImportHelper=types.SimpleNamespace(create_mesh_from_json=lambda **_kwargs: None),
)
_install_module(
    f"{PKG}.common.workspace_helper",
    WorkSpaceHelper=types.SimpleNamespace(
        create_and_get_workspace_collection=lambda: _FakeCollection("Workspace"),
        get_drawib_aliasname_dict=lambda: {},
        get_drawib_tabname_dict=lambda: {},
    ),
)

_created_mesh_calls = []
_bone_merge_calls = []


def _fake_create_mesh_with_modimp_props(**kwargs):
    _created_mesh_calls.append(kwargs)
    obj = _FakeImportedObject(kwargs.get("workspace_unique_str", ""))
    obj["modimp_workspace_unique_str"] = kwargs.get("workspace_unique_str", "")
    obj["modimp_region_first_index"] = kwargs["draw_call_meta"].first_index
    obj["modimp_region_index_count"] = kwargs["draw_call_meta"].index_count
    return obj


def _fake_bone_merge_postprocess(**kwargs):
    _bone_merge_calls.append(kwargs)


_draw_calls = [
    types.SimpleNamespace(
        lod_name="LOD0",
        submesh_folder_name="0bebac08-1002-0",
        folder_path="X:/Workspace/NTEMI/Test/LOD0/0bebac08-1002-0/TYPE_GPU",
        draw_ib="0bebac08",
        first_index=0,
        index_count=1002,
        display_name="0bebac08-1002-0",
        alias_name="",
        component="1002",
    ),
    types.SimpleNamespace(
        lod_name="LOD1",
        submesh_folder_name="a351bef7-4500-0",
        folder_path="X:/Workspace/NTEMI/Test/LOD1/a351bef7-4500-0/TYPE_GPU",
        draw_ib="a351bef7",
        first_index=0,
        index_count=4500,
        display_name="a351bef7-4500-0",
        alias_name="",
        component="4500",
    ),
]

_install_module(
    f"{PKG}.ui.ntmi_modimp.ntemi_importer",
    NTEMIImportHelper=types.SimpleNamespace(create_mesh_with_modimp_props=_fake_create_mesh_with_modimp_props),
    NtemiDrawCallMeta=object,
    _discover_draw_calls=lambda _workspace_root, _drawib_aliasname_dict=None: list(_draw_calls),
    _load_frame_analysis_dir_map=lambda _workspace_root: {
        "0bebac08": "K:/FrameAnalysis/LOD0",
        "a351bef7": "K:/FrameAnalysis/LOD1",
    },
    _resolve_deduped_texture_dir=lambda _workspace_root, lod_name="LOD0": f"X:/Workspace/NTEMI/Test/{lod_name}/DedupedTextures",
    _resolve_frame_analysis_dir=lambda _workspace_root: "K:/FrameAnalysis/Fallback",
    _load_component_name_map=lambda lod_dir: {"lod_dir": lod_dir},
    _perform_bone_merge_postprocess=_fake_bone_merge_postprocess,
    NTEMI_PROFILE_ID="yihuan",
)
_install_module(
    f"{PKG}.ui.ui_prefix_quick_ops",
    PrefixQuickOpsHelper=types.SimpleNamespace(merge_prefixes_from_objects=lambda _context, _objects: None),
)


module_path = Path(__file__).resolve().parents[1] / "ui" / "ui_func_import_ssmt.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.ui.ui_func_import_ssmt", module_path)
ui_func_import_ssmt = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ui_func_import_ssmt
spec.loader.exec_module(ui_func_import_ssmt)


class _FakeOperator:
    def __init__(self):
        self.reports = []

    def report(self, level, message):
        self.reports.append((set(level), message))


class NTEMIWorkspaceImportRoutingTests(unittest.TestCase):
    def setUp(self):
        _created_mesh_calls.clear()
        _bone_merge_calls.clear()

    def test_import_routes_each_draw_ib_to_its_tab_frame_analysis(self):
        operator = _FakeOperator()
        workspace_collection = _FakeCollection("Workspace")
        context = types.SimpleNamespace(scene=types.SimpleNamespace(global_properties=types.SimpleNamespace(selected_blueprint_name="")))

        result = ui_func_import_ssmt._import_workspace_full_ntemi(operator, context, workspace_collection)

        self.assertTrue(result)
        self.assertEqual(
            [call["frame_analysis_dir"] for call in _created_mesh_calls],
            ["K:/FrameAnalysis/LOD0", "K:/FrameAnalysis/LOD1"],
        )
        self.assertEqual(
            [(call["draw_ib"], call["frame_analysis_dir"]) for call in _bone_merge_calls],
            [("0bebac08", "K:/FrameAnalysis/LOD0"), ("a351bef7", "K:/FrameAnalysis/LOD1")],
        )
        self.assertEqual(
            [call["workspace_unique_str"] for call in _created_mesh_calls],
            ["LOD0.0bebac08-1002-0", "LOD1.a351bef7-4500-0"],
        )


class MergedSkeletonFallbackRoutingTests(unittest.TestCase):
    def _run_efmi_import(self, ensure_ok):
        target = {
            "import_key": "LOD0.aaaabbbb-100-0",
            "submesh_folder_path": "X:/Workspace/EFMI/LOD0/aaaabbbb-100-0",
            "submesh_folder_name": "aaaabbbb-100-0",
            "display_name": "LOD0.aaaabbbb-100-0",
            "import_collection": _FakeCollection("LOD0"),
        }
        create_calls = []
        option_updates = []
        skeleton_module_name = f"{PKG}.common.efmi_skeleton"
        previous_skeleton_module = sys.modules.get(skeleton_module_name)
        _install_module(
            skeleton_module_name,
            EFMISkeletonMergeHelper=types.SimpleNamespace(
                ensure_skeleton_data=lambda **_kwargs: (
                    ensure_ok,
                    "完整" if ensure_ok else "一个目标未生成",
                )
            ),
        )

        global_properties = sys.modules[
            f"{PKG}.common.global_properties"
        ].GlobalProterties
        logic_names = sys.modules[f"{PKG}.common.logic_name"].LogicName
        workspace_helper = sys.modules[
            f"{PKG}.common.workspace_helper"
        ].WorkSpaceHelper
        import_helper = sys.modules[
            f"{PKG}.common.ssmt_import_helper"
        ].SSMTImportHelper

        try:
            with (
                mock.patch.object(ui_func_import_ssmt, "_detect_ntemi_workspace", return_value=False),
                mock.patch.object(
                    ui_func_import_ssmt,
                    "_build_workspace_import_targets",
                    return_value=iter([target]),
                ),
                mock.patch.object(
                    ui_func_import_ssmt.GlobalConfig, "logic_name", "EFMI"
                ),
                mock.patch.object(logic_names, "EFMI", "EFMI", create=True),
                mock.patch.object(logic_names, "ZZMI", "ZZMI", create=True),
                mock.patch.object(
                    global_properties,
                    "import_merged_vgmap",
                    side_effect=lambda: True,
                    create=True,
                ),
                mock.patch.object(
                    global_properties,
                    "efmi_lod_group_projection",
                    side_effect=lambda: True,
                    create=True,
                ),
                mock.patch.object(
                    global_properties,
                    "set_import_merged_vgmap",
                    side_effect=lambda value: option_updates.append(bool(value)),
                    create=True,
                ),
                mock.patch.object(
                    workspace_helper,
                    "get_ordered_gpu_cpu_import_folderpath_list",
                    side_effect=lambda _path: ["X:/Workspace/EFMI/TYPE_GPU_TEST"],
                    create=True,
                ),
                mock.patch.object(
                    import_helper,
                    "create_mesh_from_json",
                    side_effect=lambda **kwargs: create_calls.append(kwargs),
                    create=True,
                ),
            ):
                operator = _FakeOperator()
                context = types.SimpleNamespace(scene=types.SimpleNamespace())
                result = ui_func_import_ssmt.ImprotFromWorkSpaceFull(operator, context)
        finally:
            if previous_skeleton_module is None:
                sys.modules.pop(skeleton_module_name, None)
            else:
                sys.modules[skeleton_module_name] = previous_skeleton_module
        return result, create_calls, option_updates

    def test_failed_pre_generation_forces_whole_batch_ordinary_and_disables_option(self):
        result, create_calls, option_updates = self._run_efmi_import(False)
        self.assertFalse(result)  # fake importer returns None; routing assertions remain valid
        self.assertEqual(len(create_calls), 1)
        self.assertIs(create_calls[0]["use_merged_vgmap"], False)
        self.assertEqual(option_updates, [False])

    def test_successful_pre_generation_keeps_merged_batch(self):
        result, create_calls, option_updates = self._run_efmi_import(True)
        self.assertFalse(result)
        self.assertEqual(len(create_calls), 1)
        self.assertIs(create_calls[0]["use_merged_vgmap"], True)
        self.assertEqual(option_updates, [])


if __name__ == "__main__":
    unittest.main()
