# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
from typing import List

import bpy

from .node_base import SSMTNodeBase


class SSMTNode_VertexGroupTestSplit(SSMTNodeBase):
    bl_idname = 'SSMTNode_VertexGroupTestSplit'
    bl_label = 'VG Test Split'
    bl_description = 'Use VG Test mapping to expand one runtime export object into multiple per-prefix copies'
    bl_icon = 'MOD_EXPLODE'
    bl_width_min = 300

    def init(self, context):
        self.inputs.new('SSMTSocketObject', "Input")
        self.outputs.new('SSMTSocketObject', "Output")
        self.width = 300

    def draw_buttons(self, context, layout):
        box = layout.box()
        box.label(text="VG Test Split", icon='MOD_EXPLODE')
        box.label(text="Requires numeric-only VG Test mapping on runtime object", icon='INFO')
        box.label(text="Generated runtime/export names always use '_vgtest'", icon='INFO')

    @staticmethod
    def validate_chain_position(chain) -> List[str]:
        errors: List[str] = []
        split_nodes = [node for node in getattr(chain, "node_path", []) or [] if getattr(node, "bl_idname", "") == 'SSMTNode_VertexGroupTestSplit']
        if len(split_nodes) > 1:
            errors.append("Only one VG Test Split node is allowed per chain.")
        return errors


def integrate_vertex_group_test_split_to_blueprint_model(blueprint_model):
    from .vg_test_runtime import VGTestRuntime

    valid_chains = [chain for chain in getattr(blueprint_model, "processing_chains", []) or [] if chain.is_valid and chain.reached_output]
    if not valid_chains:
        return

    expanded_chains = []
    expanded_count = 0

    for chain in valid_chains:
        split_nodes = list(getattr(chain, "vertex_group_test_split_nodes", []) or [])
        if not split_nodes:
            expanded_chains.append(chain)
            continue

        validation_errors = SSMTNode_VertexGroupTestSplit.validate_chain_position(chain)
        if validation_errors:
            raise RuntimeError(" ".join(validation_errors))

        split_node = split_nodes[-1]
        split_results = VGTestRuntime.expand_chain_object_for_export(
            source_object_name=chain.object_name,
            original_object_name=chain.original_object_name or chain.object_name,
        )
        if not split_results:
            raise RuntimeError(f"VG Test Split produced no export objects for '{chain.object_name}'.")

        for split_result in split_results:
            new_chain = copy.deepcopy(chain)
            new_chain.object_name = split_result["object_name"]
            new_chain.original_object_name = split_result["original_object_name"]
            new_chain.export_object_name_override = split_result["export_name"]
            expanded_chains.append(new_chain)
            expanded_count += 1

    if expanded_count <= 0:
        return

    blueprint_model.processing_chains = expanded_chains
    blueprint_model._merge_processing_chains()


classes = (
    SSMTNode_VertexGroupTestSplit,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
