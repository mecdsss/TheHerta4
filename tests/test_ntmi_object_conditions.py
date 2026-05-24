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


PKG = "_ntmi_object_conditions_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.ui", f"{PKG}.ui.ntmi_modimp"):
    package = _install_module(package_name)
    package.__path__ = []


_install_module(
    "bpy",
    data=types.SimpleNamespace(objects={}, collections=[]),
    context=types.SimpleNamespace(scene=types.SimpleNamespace()),
)
_install_module(
    f"{PKG}.common.logic_name",
    LogicName=types.SimpleNamespace(NTEMI="NTEMI"),
)
_install_module(f"{PKG}.blueprint.model", BluePrintModel=type("BluePrintModel", (), {}))
_install_module(f"{PKG}.common.draw_call_model", DrawCallModel=type("DrawCallModel", (), {}))
_install_module(
    f"{PKG}.common.object_prefix_helper",
    ObjectPrefixHelper=types.SimpleNamespace(
        extract_prefix_info=lambda _name: None,
        parse_prefix_parts=lambda _prefix: {},
    ),
)
_install_module(f"{PKG}.ui.ntmi_modimp.prefix_property_cache", get_prefix_record_props=lambda _name: {})


module_path = Path(__file__).resolve().parents[1] / "ui" / "ntmi_modimp" / "export_tree_builder.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.ui.ntmi_modimp.export_tree_builder", module_path)
export_tree_builder = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = export_tree_builder
spec.loader.exec_module(export_tree_builder)


class _FakeWorkKey:
    def __init__(self, key_name, tmp_value, is_swapkey=True, condition_operator="&&"):
        self.key_name = key_name
        self.tmp_value = tmp_value
        self.is_swapkey = is_swapkey
        self.condition_operator = condition_operator


class NTMIObjectConditionTests(unittest.TestCase):
    def test_condition_from_work_keys_dedupes_identical_swap_conditions(self):
        work_keys = [
            _FakeWorkKey("$swapkey9", 0),
            _FakeWorkKey("$swapkey9", 0),
            _FakeWorkKey("$swapkey9", 1),
        ]

        condition = export_tree_builder._condition_from_work_keys(work_keys)

        self.assertEqual(condition, "$swapkey9 == 0 && $swapkey9 == 1")

    def test_collect_object_conditions_keeps_objects_separate(self):
        build_result = types.SimpleNamespace(
            source_records=[
                types.SimpleNamespace(
                    region_records=[
                        types.SimpleNamespace(
                            object_conditions={
                                "main_copy": "$swapkey9 == 0",
                                "chain1_copy": "$swapkey9 == 1",
                            }
                        ),
                        types.SimpleNamespace(
                            object_conditions={
                                "main_copy": "$swapkey9 == 1",
                            }
                        ),
                    ]
                )
            ]
        )

        collected = export_tree_builder.collect_object_conditions(build_result)

        self.assertEqual(collected["main_copy"], "$swapkey9 == 0 || $swapkey9 == 1")
        self.assertEqual(collected["chain1_copy"], "$swapkey9 == 1")

    def test_conditions_are_not_wrapped_in_extra_parentheses(self):
        self.assertEqual(export_tree_builder._wrap_condition_clause("$swapkey9 == 2 && $swapkey1 == 0"), "$swapkey9 == 2 && $swapkey1 == 0")

    def test_collect_object_conditions_preserves_nested_boolean_precedence(self):
        build_result = types.SimpleNamespace(
            source_records=[
                types.SimpleNamespace(
                    region_records=[
                        types.SimpleNamespace(
                            object_conditions={
                                "main_copy": "($swapkey9 == 0 || $swapkey9 == 1) && $state == 2",
                            }
                        ),
                        types.SimpleNamespace(
                            object_conditions={
                                "main_copy": "$swapkey3 == 1",
                            }
                        ),
                    ]
                )
            ]
        )

        collected = export_tree_builder.collect_object_conditions(build_result)

        self.assertEqual(
            collected["main_copy"],
            "(($swapkey9 == 0 || $swapkey9 == 1) && $state == 2) || $swapkey3 == 1",
        )


if __name__ == "__main__":
    unittest.main()
