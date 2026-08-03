import bpy
from bpy.props import BoolProperty, CollectionProperty, FloatProperty, IntProperty, StringProperty

from .anim_driver_base import (
    ANIM_DRIVER_INPUT_SOCKET_NAME,
    ANIM_DRIVER_OUTPUT_SOCKET_NAME,
    SSMTNode_AnimDriver_Base,
)

# 3Dmigoto 命令列表的解析规范：
# - 所有变量以 32 位 float 存储，超过 2^24 的整数会丢失精度；
# - 不支持任何函数，仅支持基础算术（+ - * / // %）、括号与条件判断。
# 因此随机数采用线性同余生成器（LCG）在 INI 端以纯算术计算：
#   x_{n+1} = (4001 * x_n + 12345) mod 16777216
# 模数 16777216 = 2^24，乘数 4001 ≡ 1 (mod 4)，增量 12345 为奇数，
# 周期为完整的 2^24（约 1900 万步）。配合 Schrage 分解（q=4193, r=1023）：
#   x mod m = 4001 * (x mod q) - 1023 * (x // q)   (mod m)
# 余数用 %（fmod 对整数操作数精确），商用减法后除以 q 还原（结果是精确整数）。
# 增量 12345 通过 if/else 折叠进模运算，保证所有中间值严格小于 2^24，
# 在 float32 下与整数 LCG 逐位一致，序列永不越界、永不退化。
_LCG_MODULUS = 16777216  # 2^24
_LCG_MULTIPLIER = 4001
_LCG_INCREMENT = 12345
_LCG_Q = 4193  # modulus // multiplier
_LCG_R = 1023  # modulus - multiplier * q
_LCG_MODULUS_MINUS_INCREMENT = _LCG_MODULUS - _LCG_INCREMENT


def _format_number(value: float) -> str:
    """将浮点数格式化为简洁的 INI 字面量，避免二进制浮点噪声。"""
    text = f"{value:.10f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-", "-0") else text


class RandomDriverTargetItem(bpy.types.PropertyGroup):
    variable_name: StringProperty(
        name="驱动变量",
        description="每帧写入指定范围内随机值的变量名称",
        default="",
    )


class SSMT_UL_RandomDriverTargets(bpy.types.UIList):
    bl_idname = "SSMT_UL_RANDOM_DRIVER_TARGETS"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.prop(item, "variable_name", text="", icon='RNDCURVE')


class SSMT_OT_RandomDriverTargetAdd(bpy.types.Operator):
    bl_idname = "ssmt.random_driver_target_add"
    bl_label = "添加随机驱动变量"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        node.driven_variable_list.add()
        node.driven_variable_list_active = len(node.driven_variable_list) - 1
        return {'FINISHED'}


class SSMT_OT_RandomDriverTargetRemove(bpy.types.Operator):
    bl_idname = "ssmt.random_driver_target_remove"
    bl_label = "删除随机驱动变量"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        idx = node.driven_variable_list_active
        if 0 <= idx < len(node.driven_variable_list):
            node.driven_variable_list.remove(idx)
            node.driven_variable_list_active = min(idx, len(node.driven_variable_list) - 1)
        return {'FINISHED'}


class SSMTNode_AnimDriver_Random(SSMTNode_AnimDriver_Base):
    bl_idname = 'SSMTNode_AnimDriver_Random'
    bl_label = '随机驱动'
    bl_description = '每帧为多个变量分别生成指定范围内的伪随机值，可用于上下左右抖动等效果'
    bl_icon = 'RNDCURVE'

    seed: IntProperty(
        name="随机种子",
        description="决定随机序列的初始状态；相同种子会生成相同序列",
        default=13579,
        min=0,
        max=16777215,
    )

    min_value: FloatProperty(
        name="最小值",
        description="随机值范围的下限（含）",
        default=0.0,
    )

    max_value: FloatProperty(
        name="最大值",
        description="随机值范围的上限（含）",
        default=1.0,
    )

    driven_variable_list: CollectionProperty(
        type=RandomDriverTargetItem,
        name="驱动变量列表",
    )

    driven_variable_list_active: IntProperty(
        name="当前驱动变量",
        default=0,
    )

    default_paused: BoolProperty(
        name="默认播放",
        description="节点默认处于播放状态（不勾选则默认暂停，需由动画驱动开关开启）",
        default=True,
    )

    custom_paused_var: StringProperty(
        name="暂停变量",
        description="控制本节点启停的变量名称（1=播放，0=暂停），由动画驱动开关节点切换",
        default="",
    )

    def init(self, context):
        self.inputs.new('SSMTSocketAnimDriver', ANIM_DRIVER_INPUT_SOCKET_NAME)
        self.outputs.new('SSMTSocketAnimDriver', ANIM_DRIVER_OUTPUT_SOCKET_NAME)
        self.width = 300
        self._assign_next_available_index()
        self._ensure_paused_variable_name("random_paused")
        for variable_name in ("$shape_up", "$shape_down", "$shape_left", "$shape_right"):
            item = self.driven_variable_list.add()
            item.variable_name = variable_name

    def copy(self, node):
        self._assign_next_available_index()
        self.custom_paused_var = ""
        self._ensure_paused_variable_name("random_paused")

    def draw_buttons(self, context, layout):
        box = layout.box()
        row = box.row(align=True)
        row.prop(self, "min_value", text="最小值")
        row.prop(self, "max_value", text="最大值")
        box.prop(self, "seed")

        row = box.row(align=True)
        row.prop(self, "default_paused", text="默认播放")
        if self.custom_paused_var.strip():
            row.prop(self, "custom_paused_var", text="")
        else:
            row.label(text=f"$random_paused{self._read_safe_index()}")

        row = box.row(align=True)
        row.label(text="随机驱动变量", icon='RNDCURVE')
        op = row.operator("ssmt.random_driver_target_add", text="", icon='ADD')
        op.node_name = self.name
        op = row.operator("ssmt.random_driver_target_remove", text="", icon='REMOVE')
        op.node_name = self.name

        if self.driven_variable_list:
            box.template_list(
                "SSMT_UL_RANDOM_DRIVER_TARGETS", "",
                self, "driven_variable_list",
                self, "driven_variable_list_active",
                rows=max(4, min(len(self.driven_variable_list), 8)),
            )
            box.label(text="每个变量每帧获得不同的随机值", icon='INFO')
        else:
            box.label(text="至少添加一个目标变量", icon='INFO')

    def generate_ini_segment(self, connected_nodes=None) -> str:
        idx = self._read_safe_index()
        targets = []
        for item in self.driven_variable_list:
            variable_name = str(getattr(item, "variable_name", "") or "").strip()
            if not variable_name:
                continue
            if not variable_name.startswith('$'):
                variable_name = f"${variable_name}"
            if variable_name not in targets:
                targets.append(variable_name)

        if not targets:
            return ""

        min_value = float(getattr(self, "min_value", 0.0) or 0.0)
        max_value = float(getattr(self, "max_value", 1.0) or 1.0)
        if min_value > max_value:
            min_value, max_value = max_value, min_value
        min_str = _format_number(min_value)
        span_str = _format_number(max_value - min_value)

        seed_value = getattr(self, "seed", None)
        if seed_value is None:
            seed_value = 13579
        seed_value = max(0, min(int(seed_value), _LCG_MODULUS - 1))
        seed_var = f"$random_seed{idx}"
        high_var = f"$random_high{idx}"
        low_var = f"$random_low{idx}"

        paused_state = self._resolve_default_play_state(self.default_paused)
        paused_var = str(getattr(self, "custom_paused_var", "") or "").strip()
        if not paused_var:
            paused_var = f"$random_paused{idx}"
        elif not paused_var.startswith('$'):
            paused_var = f"${paused_var}"

        lines = [
            "[Constants]",
            self._format_global_assignment(seed_var, seed_value, persist=True),
            "; LCG 随机序列状态（每帧推进，保证每帧取值不同）",
            self._format_global_assignment(high_var, 0),
            self._format_global_assignment(low_var, 0),
            self._format_global_assignment(paused_var, paused_state, persist=True),
            "; 播放状态（1=播放，0=暂停，由动画驱动开关节点切换）",
            "[Present]",
            f"if {paused_var} == 1",
        ]

        # Schrage 分解把取模拆成基础算术；low 用 %（fmod 对整数操作数精确），
        # high 用减法后除以 q 还原（商为精确整数）。增量通过 if/else 折叠进
        # 模运算，使所有中间值严格 < 2^24，float32 下与整数 LCG 逐位一致。
        # 每个目标变量消耗一步序列，因此同一帧内各变量取值互不相同。
        for target in targets:
            lines.extend([
                f"    {low_var} = {seed_var} % {_LCG_Q}",
                f"    {high_var} = ({seed_var} - {low_var}) / {_LCG_Q}",
                f"    {seed_var} = ({_LCG_MULTIPLIER} * {low_var}) - ({_LCG_R} * {high_var})",
                f"    if {seed_var} < 0",
                f"        {seed_var} = {seed_var} + {_LCG_MODULUS}",
                "    endif",
                f"    if {seed_var} < {_LCG_MODULUS_MINUS_INCREMENT}",
                f"        {seed_var} = {seed_var} + {_LCG_INCREMENT}",
                "    else",
                f"        {seed_var} = {seed_var} - {_LCG_MODULUS_MINUS_INCREMENT}",
                "    endif",
                f"    {target} = {min_str} + ({seed_var} / {_LCG_MODULUS}.0) * {span_str}",
            ])

        # 暂停时把目标变量复位为 0，避免模型停在随机的偏移姿态上。
        lines.append("else")
        for target in targets:
            lines.append(f"    {target} = 0")
        lines.append("endif")
        return "\n".join(lines)


_load_handler_registered = False


@bpy.app.handlers.persistent
def _random_driver_load_handler(dummy):
    for tree in bpy.data.node_groups:
        if tree.bl_idname != 'SSMTBlueprintTreeType':
            continue
        for node in tree.nodes:
            if node.bl_idname == 'SSMTNode_AnimDriver_Random':
                try:
                    SSMTNode_AnimDriver_Base._migrate_dynamic_sockets(node)
                except Exception:
                    pass


classes = (
    RandomDriverTargetItem,
    SSMT_UL_RandomDriverTargets,
    SSMT_OT_RandomDriverTargetAdd,
    SSMT_OT_RandomDriverTargetRemove,
    SSMTNode_AnimDriver_Random,
)


def register():
    global _load_handler_registered
    for cls in classes:
        bpy.utils.register_class(cls)
    if not _load_handler_registered:
        bpy.app.handlers.load_post.append(_random_driver_load_handler)
        _load_handler_registered = True


def unregister():
    global _load_handler_registered
    if _load_handler_registered:
        bpy.app.handlers.load_post.remove(_random_driver_load_handler)
        _load_handler_registered = False
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
