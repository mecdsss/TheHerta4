import bpy
from bpy.types import NodeSocket, Node

from .node_base import SSMTNodeBase


class SSMTSocketAnimDriver(NodeSocket):
    bl_idname = 'SSMTSocketAnimDriver'
    bl_label = 'Anim Driver Socket'

    def draw_color(self, context, node):
        return (0.2, 0.7, 0.6, 1.0)

    def draw(self, context, layout, node, text):
        layout.label(text=text)


class SSMTNode_AnimDriver_Base(SSMTNodeBase):
    bl_idname = 'SSMTNode_AnimDriver_Base'
    bl_label = 'AnimDriver Base'

    auto_index: bpy.props.IntProperty(
        name="自动索引",
        default=0,
        options={'HIDDEN'},
    )

    @classmethod
    def poll(cls, ntree):
        if ntree.bl_idname != 'SSMTBlueprintTreeType':
            return False
        return ntree.get("is_animation_driver", False)

    def generate_ini_segment(self, connected_nodes=None) -> str:
        raise NotImplementedError("子类必须实现 generate_ini_segment 方法")

    def _get_indexed_nodes(self, tree):
        result = []
        for n in tree.nodes:
            try:
                _ = n.auto_index
                result.append(n)
            except Exception:
                pass
        return result

    def _assign_auto_index(self):
        tree = self.id_data
        if not tree:
            self.auto_index = 1
            return
        all_indexed = sorted(
            self._get_indexed_nodes(tree),
            key=lambda n: n.name
        )
        for i, n in enumerate(all_indexed, 1):
            n.auto_index = i

    def _ensure_valid_index(self):
        tree = self.id_data
        if not tree:
            return
        all_indexed = self._get_indexed_nodes(tree)
        indices = [n.auto_index for n in all_indexed]
        if len(set(indices)) != len(indices) or any(i <= 0 for i in indices):
            sorted_nodes = sorted(all_indexed, key=lambda n: n.name)
            for i, n in enumerate(sorted_nodes, 1):
                n.auto_index = i


classes = (
    SSMTSocketAnimDriver,
    SSMTNode_AnimDriver_Base,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
