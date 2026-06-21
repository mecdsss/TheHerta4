# -*- coding: utf-8 -*-
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


PKG = "_model_cross_ib_method_mapping_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


_install_module(
    "bpy",
    types=types.SimpleNamespace(Node=object, NodeTree=object),
    data=types.SimpleNamespace(),
)
_install_module("bpy.types", Node=object, NodeTree=object)
_install_module(
    f"{PKG}.utils.log_utils",
    LOG=types.SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        debug=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
    ),
)
_install_module(f"{PKG}.common.m_key", M_Key=type("M_Key", (), {}))
_install_module(f"{PKG}.common.draw_call_model", DrawCallModel=type("DrawCallModel", (), {}))
_install_module(
    f"{PKG}.common.object_prefix_helper",
    ObjectPrefixHelper=types.SimpleNamespace(
        get_node_prefix_info=lambda _node: None,
        build_virtual_object_name_for_node=lambda *_args, **_kwargs: "",
        extract_prefix_info=lambda _name: None,
        parse_prefix_parts=lambda _prefix: {},
    ),
)
_install_module(
    f"{PKG}.blueprint.export_helper",
    BlueprintExportHelper=types.SimpleNamespace(),
)
_install_module(
    f"{PKG}.blueprint.node_cross_ib",
    CrossIBMatchMode=types.SimpleNamespace(INDEX_COUNT="INDEX_COUNT", IB_HASH="IB_HASH"),
)


module_path = Path(__file__).resolve().parents[1] / "blueprint" / "model.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.model", module_path)
model_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = model_module
spec.loader.exec_module(model_module)


class ModelCrossIBMethodMappingTests(unittest.TestCase):
    def test_process_cross_ib_nodes_keeps_method_per_mapping_key(self):
        cross_ib_a = types.SimpleNamespace(
            name="CrossA",
            bl_idname="SSMTNode_CrossIB",
            cross_ib_list=[types.SimpleNamespace(source_ib="sourceA-0", target_ib="targetA-0", source_index_count="", target_index_count="")],
            cross_ib_method="VB_COPY",
            match_mode="IB_HASH",
            unsupported_reason="",
            _update_cross_ib_method=lambda: None,
            get_ib_mapping_dict=lambda: {"sourceA_0": ["targetA_0"]},
            get_vb_condition_source=lambda: "if vs == 200",
            get_vb_condition_target=lambda: "if vs == 202",
        )
        cross_ib_b = types.SimpleNamespace(
            name="CrossB",
            bl_idname="SSMTNode_CrossIB",
            cross_ib_list=[types.SimpleNamespace(source_ib="sourceB-0", target_ib="targetB-0", source_index_count="", target_index_count="")],
            cross_ib_method="VB_REF_SO0",
            match_mode="IB_HASH",
            unsupported_reason="",
            _update_cross_ib_method=lambda: None,
            get_ib_mapping_dict=lambda: {"sourceB_0": ["targetB_0"]},
            get_vb_condition_source=lambda: "if vs == 201",
            get_vb_condition_target=lambda: "if vs == 203",
        )

        chain_a = types.SimpleNamespace(
            is_valid=True,
            reached_output=True,
            node_path=[cross_ib_a],
            object_name="ObjA",
            get_export_object_name=lambda: "ObjA",
        )
        chain_b = types.SimpleNamespace(
            is_valid=True,
            reached_output=True,
            node_path=[cross_ib_b],
            object_name="ObjB",
            get_export_object_name=lambda: "ObjB",
        )

        blueprint_model = object.__new__(model_module.BluePrintModel)
        blueprint_model.cross_ib_nodes = [cross_ib_a, cross_ib_b]
        blueprint_model.processing_chains = [chain_a, chain_b]
        blueprint_model.cross_ib_info_dict = {}
        blueprint_model.cross_ib_method_dict = {}
        blueprint_model.cross_ib_mapping_method = {}
        blueprint_model.cross_ib_mapping_objects = {}
        blueprint_model.cross_ib_vb_condition_mapping = {}
        blueprint_model.cross_ib_source_to_target_dict = {}
        blueprint_model.cross_ib_object_vb_condition = {}
        blueprint_model.cross_ib_target_info = {}
        blueprint_model.cross_ib_object_names = set()
        blueprint_model.cross_ib_match_mode = "IB_HASH"
        blueprint_model.has_cross_ib = False
        blueprint_model._get_object_ib_keys = lambda _obj_name: []

        model_module.BluePrintModel._process_cross_ib_nodes(blueprint_model)

        self.assertEqual(
            blueprint_model.cross_ib_mapping_method[("sourceA_0", "targetA_0")],
            "VB_COPY",
        )
        self.assertEqual(
            blueprint_model.cross_ib_mapping_method[("sourceB_0", "targetB_0")],
            "VB_REF_SO0",
        )


if __name__ == "__main__":
    unittest.main()
