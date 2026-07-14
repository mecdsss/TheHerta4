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


PKG = "_vertex_group_cleanup_test_pkg"
for package_name in (PKG, f"{PKG}.utils", f"{PKG}.toolkit"):
    package = _install_module(package_name)
    package.__path__ = []


_install_module(
    "bpy",
    types=types.SimpleNamespace(Operator=object),
)
_install_module("mathutils", Vector=object)
_install_module(f"{PKG}.utils.format_utils", Fatal=RuntimeError)


def _load_module(module_name, relative_path):
    module_path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(f"{PKG}.{module_name}", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


vertexgroup_utils_module = _load_module("utils.vertexgroup_utils", "utils/vertexgroup_utils.py")
vg_create_module = _load_module("toolkit.vg_create", "toolkit/vg_create.py")

VertexGroupUtils = vertexgroup_utils_module.VertexGroupUtils
CleanVertexGroups = vg_create_module.CleanVertexGroups


class _FakeGroupAssignment:
    def __init__(self, group, weight):
        self.group = group
        self.weight = weight


class _FakeVertex:
    def __init__(self, groups):
        self.groups = list(groups)


class _FakeVertexGroup:
    def __init__(self, name, index):
        self.name = name
        self.index = index


class _FakeVertexGroups(list):
    def __contains__(self, item):
        if isinstance(item, str):
            return any(group.name == item for group in self)
        return super().__contains__(item)

    def remove(self, value):
        super().remove(value)
        for index, group in enumerate(self):
            group.index = index

    def get(self, name):
        return next((group for group in self if group.name == name), None)


class _FakeObject:
    def __init__(self, group_names, vertex_group_refs, obj_type="MESH"):
        self.type = obj_type
        self.vertex_groups = _FakeVertexGroups(
            [_FakeVertexGroup(name, index) for index, name in enumerate(group_names)]
        )
        self.data = types.SimpleNamespace(
            vertices=[_FakeVertex(groups) for groups in vertex_group_refs]
        )
        self.update_calls = 0

    def update_from_editmode(self):
        self.update_calls += 1


class VertexGroupCleanupTests(unittest.TestCase):
    def test_get_nonzero_vertex_group_indices_ignores_zero_weights(self):
        obj = _FakeObject(
            ["A", "B", "C"],
            [
                [_FakeGroupAssignment(0, 1.0), _FakeGroupAssignment(1, 0.0)],
                [_FakeGroupAssignment(2, 0.25)],
            ],
        )

        used = VertexGroupUtils.get_nonzero_vertex_group_indices(obj)

        self.assertEqual(used, {0, 2})
        self.assertEqual(obj.update_calls, 1)

    def test_remove_unused_vertex_groups_removes_only_unused_groups(self):
        obj = _FakeObject(
            ["A", "B", "C"],
            [
                [_FakeGroupAssignment(0, 1.0)],
                [_FakeGroupAssignment(2, 0.5)],
            ],
        )

        removed_count = VertexGroupUtils.remove_unused_vertex_groups(obj)

        self.assertEqual(removed_count, 1)
        self.assertEqual([group.name for group in obj.vertex_groups], ["A", "C"])

    def test_remove_all_vertex_groups_does_not_skip_live_collection_items(self):
        obj = _FakeObject(["A", "B", "C", "D"], [])

        VertexGroupUtils.remove_all_vertex_groups(obj)

        self.assertEqual(list(obj.vertex_groups), [])

    def test_clean_vertex_groups_with_zero_cleanup_uses_single_lookup_per_object(self):
        obj = _FakeObject(
            ["Keep", "ZeroWeight", "RemoveByName"],
            [
                [_FakeGroupAssignment(0, 1.0)],
                [_FakeGroupAssignment(2, 0.75)],
            ],
        )
        props = types.SimpleNamespace(
            vg_cleanup_names="RemoveByName",
            vg_cleanup_remove_zero=True,
        )
        context = types.SimpleNamespace(
            selected_objects=[obj],
            scene=types.SimpleNamespace(vg_props=props),
        )

        original_get_nonzero = vg_create_module.VertexGroupUtils.get_nonzero_vertex_group_indices
        calls = []

        def _tracked_get_nonzero(target_obj, weight_threshold=1e-6):
            calls.append((target_obj, weight_threshold))
            return original_get_nonzero(target_obj, weight_threshold)

        vg_create_module.VertexGroupUtils.get_nonzero_vertex_group_indices = _tracked_get_nonzero
        try:
            operator = CleanVertexGroups()
            reports = []
            operator.report = lambda kinds, message: reports.append((kinds, message))

            result = operator.execute(context)

            self.assertEqual(result, {"FINISHED"})
            self.assertEqual(len(calls), 1)
            self.assertEqual([group.name for group in obj.vertex_groups], ["Keep"])
            self.assertTrue(reports)
        finally:
            vg_create_module.VertexGroupUtils.get_nonzero_vertex_group_indices = original_get_nonzero

    def test_clean_vertex_groups_without_zero_cleanup_only_removes_named_groups(self):
        obj = _FakeObject(
            ["Keep", "RemoveMe", "ZeroButKeep"],
            [
                [_FakeGroupAssignment(0, 1.0)],
            ],
        )
        props = types.SimpleNamespace(
            vg_cleanup_names="RemoveMe",
            vg_cleanup_remove_zero=False,
        )
        context = types.SimpleNamespace(
            selected_objects=[obj],
            scene=types.SimpleNamespace(vg_props=props),
        )

        operator = CleanVertexGroups()
        operator.report = lambda *_args, **_kwargs: None

        result = operator.execute(context)

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual([group.name for group in obj.vertex_groups], ["Keep", "ZeroButKeep"])


if __name__ == "__main__":
    unittest.main()
