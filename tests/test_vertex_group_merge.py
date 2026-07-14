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


PKG = "_vertex_group_merge_test_pkg"
for package_name in (PKG, f"{PKG}.utils", f"{PKG}.toolkit"):
    package = _install_module(package_name)
    package.__path__ = []


_install_module(
    "bpy",
    types=types.SimpleNamespace(Operator=object),
    props=types.SimpleNamespace(BoolProperty=lambda **_kwargs: False),
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
weight_tools_module = _load_module("toolkit.bmtp_weight_tools", "toolkit/bmtp_weight_tools.py")

VertexGroupUtils = vertexgroup_utils_module.VertexGroupUtils
RefreshMergeVertexGroups = weight_tools_module.BMTP_OT_RefreshMergeVertexGroups
MergeVertexGroups = weight_tools_module.BMTP_OT_MergeVertexGroups


class _FakeAssignment:
    def __init__(self, group, weight):
        self.group = group
        self.weight = weight


class _FakeVertex:
    def __init__(self, index, assignments):
        self.index = index
        self.groups = list(assignments)


class _FakeVertexGroup:
    def __init__(self, name, index):
        self.name = name
        self.index = index
        self.removed_indices = []
        self.assigned_weights = {}

    def remove(self, vertex_indices):
        self.removed_indices.extend(vertex_indices)
        for vertex_index in vertex_indices:
            self.assigned_weights.pop(vertex_index, None)

    def add(self, vertex_indices, weight, mode):
        if mode != 'REPLACE':
            raise AssertionError(f"unexpected assignment mode: {mode}")
        for vertex_index in vertex_indices:
            self.assigned_weights[vertex_index] = weight


class _FakeVertexGroups(list):
    def __init__(self, names):
        super().__init__(_FakeVertexGroup(name, index) for index, name in enumerate(names))
        self.active_index = 0
        self.fail_remove_name = None

    def __getitem__(self, key):
        if isinstance(key, str):
            group = self.get(key)
            if group is None:
                raise KeyError(key)
            return group
        return super().__getitem__(key)

    def get(self, name):
        return next((group for group in self if group.name == name), None)

    def new(self, name):
        group = _FakeVertexGroup(name, len(self))
        self.append(group)
        return group

    def remove(self, group):
        if group.name == self.fail_remove_name:
            self.fail_remove_name = None
            raise RuntimeError("injected group removal failure")
        super().remove(group)
        for index, remaining_group in enumerate(self):
            remaining_group.index = index


class _FakeObject:
    def __init__(self, name, group_names, vertices=None, mode='OBJECT'):
        self.name = name
        self.type = 'MESH'
        self.mode = mode
        self.vertex_groups = _FakeVertexGroups(group_names)
        self.data = types.SimpleNamespace(vertices=list(vertices or []))


class _FakeCollection(list):
    def add(self):
        item = types.SimpleNamespace(name="", index=0, selected=False)
        self.append(item)
        return item


class VertexGroupMergeTests(unittest.TestCase):
    def test_merge_sums_weights_clamps_to_one_and_removes_sources(self):
        obj = _FakeObject(
            "Body",
            ["A", "B", "Unrelated"],
            [
                _FakeVertex(0, [_FakeAssignment(0, 0.7), _FakeAssignment(1, 0.6)]),
                _FakeVertex(1, [_FakeAssignment(1, 0.4)]),
                _FakeVertex(2, [_FakeAssignment(2, 0.8)]),
            ],
        )
        target_group = obj.vertex_groups.get("A")

        result = VertexGroupUtils.merge_vertex_groups(obj, ["A", "B"])

        self.assertEqual(result, {
            "target_name": "A",
            "removed_groups": 1,
            "merged_vertices": 2,
        })
        self.assertEqual([group.name for group in obj.vertex_groups], ["A", "Unrelated"])
        self.assertEqual(target_group.removed_indices, [0, 1, 2])
        self.assertEqual(target_group.assigned_weights, {0: 1.0, 1: 0.4})
        self.assertEqual(obj.vertex_groups.active_index, 0)

    def test_merge_rejects_non_object_mode(self):
        obj = _FakeObject("Body", ["A", "B"], mode='EDIT')

        with self.assertRaisesRegex(RuntimeError, "Object Mode"):
            VertexGroupUtils.merge_vertex_groups(obj, ["A", "B"])

        self.assertFalse(MergeVertexGroups.poll(types.SimpleNamespace(active_object=obj)))

    def test_merge_rejects_existing_unselected_target(self):
        obj = _FakeObject("Body", ["A", "B", "Existing"])

        with self.assertRaisesRegex(RuntimeError, "already exists"):
            VertexGroupUtils.merge_vertex_groups(obj, ["A", "B"], "Existing")

    def test_merge_restores_all_groups_and_weights_when_mutation_fails(self):
        obj = _FakeObject(
            "Body",
            ["A", "B", "Unrelated"],
            [
                _FakeVertex(0, [_FakeAssignment(0, 0.7), _FakeAssignment(1, 0.6)]),
                _FakeVertex(1, [_FakeAssignment(1, 0.4)]),
                _FakeVertex(2, [_FakeAssignment(2, 0.8)]),
            ],
        )
        obj.vertex_groups.fail_remove_name = "B"

        with self.assertRaisesRegex(RuntimeError, "rolled back"):
            VertexGroupUtils.merge_vertex_groups(obj, ["A", "B"])

        self.assertEqual([group.name for group in obj.vertex_groups], ["A", "B", "Unrelated"])
        self.assertEqual(obj.vertex_groups.get("A").assigned_weights, {0: 0.7})
        self.assertEqual(obj.vertex_groups.get("B").assigned_weights, {0: 0.6, 1: 0.4})
        self.assertEqual(obj.vertex_groups.get("Unrelated").assigned_weights, {2: 0.8})

    def test_merge_operator_rejects_list_from_previous_active_object(self):
        previous_object = _FakeObject("BodyA", ["A", "B"])
        props = types.SimpleNamespace(
            wt_merge_source_object=previous_object,
            wt_merge_source_object_name="BodyA",
            wt_merge_target_name="",
            wt_merge_vertex_groups=_FakeCollection([
                types.SimpleNamespace(name="A", index=0, selected=True),
                types.SimpleNamespace(name="B", index=1, selected=True),
            ]),
        )
        context = types.SimpleNamespace(
            active_object=_FakeObject("BodyB", ["A", "B"]),
            scene=types.SimpleNamespace(bmtp_props=props),
        )
        reports = []
        operator = MergeVertexGroups()
        operator.report = lambda kinds, message: reports.append((kinds, message))

        result = operator.execute(context)

        self.assertEqual(result, {'CANCELLED'})
        self.assertTrue(any("请先刷新" in message for _kinds, message in reports))

    def test_merge_operator_rejects_different_object_with_same_name(self):
        previous_object = _FakeObject("Body", ["A", "B"])
        active_object = _FakeObject("Body", ["A", "B"])
        props = types.SimpleNamespace(
            wt_merge_source_object=previous_object,
            wt_merge_source_object_name="Body",
            wt_merge_target_name="",
            wt_merge_vertex_groups=_FakeCollection([
                types.SimpleNamespace(name="A", index=0, selected=True),
                types.SimpleNamespace(name="B", index=1, selected=True),
            ]),
        )
        context = types.SimpleNamespace(
            active_object=active_object,
            scene=types.SimpleNamespace(bmtp_props=props),
        )
        operator = MergeVertexGroups()
        operator.report = lambda *_args, **_kwargs: None

        result = operator.execute(context)

        self.assertEqual(result, {'CANCELLED'})
        self.assertEqual([group.name for group in active_object.vertex_groups], ["A", "B"])

    def test_refreshing_different_object_clears_target_and_rebuilds_list(self):
        previous_object = _FakeObject("BodyA", ["A", "B"])
        props = types.SimpleNamespace(
            wt_merge_source_object=previous_object,
            wt_merge_source_object_name="BodyA",
            wt_merge_target_name="Merged",
            wt_merge_vertex_groups=_FakeCollection(),
            wt_merge_vertex_groups_index=8,
        )
        context = types.SimpleNamespace(
            active_object=_FakeObject("BodyB", ["Head", "Neck"]),
            scene=types.SimpleNamespace(bmtp_props=props),
        )
        operator = RefreshMergeVertexGroups()
        operator.report = lambda *_args, **_kwargs: None

        result = operator.execute(context)

        self.assertEqual(result, {'FINISHED'})
        self.assertIs(props.wt_merge_source_object, context.active_object)
        self.assertEqual(props.wt_merge_source_object_name, "BodyB")
        self.assertEqual(props.wt_merge_target_name, "")
        self.assertEqual(
            [(item.name, item.index, item.selected) for item in props.wt_merge_vertex_groups],
            [("Head", 0, False), ("Neck", 1, False)],
        )
        self.assertEqual(props.wt_merge_vertex_groups_index, 1)


if __name__ == "__main__":
    unittest.main()
