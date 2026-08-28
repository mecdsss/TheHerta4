# -*- coding: utf-8 -*-
"""高斯权重球：Blender 属性组定义。"""
import bpy


def _mark_dirty(self, context):
    """球参数变化回调：只置脏标记，由计时器去抖重算（防重入）。"""
    try:
        from . import gb_operators
        gb_operators.mark_dirty()
    except Exception:
        pass


class GB_BallSettings(bpy.types.PropertyGroup):
    """单个高斯球的参数（挂 Object.gb_ball）。

    半径即物体的 scale（所见即所得：球面 = 权重 ≈ 0 边界），不在此重复存储。
    """
    strength: bpy.props.FloatProperty(
        name="强度", default=1.0, min=0.0, max=2.0,
        description="采样场模式=源权重倍率（1.0 原样保留）；"
                    "解析高斯模式=球心权重绝对值",
        update=_mark_dirty)
    falloff_k: bpy.props.FloatProperty(
        name="衰减系数", default=4.6, min=0.5, max=8.0,
        description="解析高斯模式的衰减系数：越大权重越聚拢，越小越扩散"
                    "（采样场模式不使用，权重由源分布决定）",
        update=_mark_dirty)
    use_source_sampling: bpy.props.BoolProperty(
        name="使用源权重采样", default=True,
        description="采样场模式：球范围内目标顶点直接取最近源顶点的原始权重，"
                    "保留多峰/非对称分布，不穿透网格；关闭则退回解析高斯拟合",
        update=_mark_dirty)
    use_surface_propagation: bpy.props.BoolProperty(
        name="沿表面传播", default=True,
        description="解析高斯模式：权重从球与表面的接触点沿网格表面扩散（测地距离），"
                    "不穿透到球体积覆盖的背面/对侧；关闭回退体积球",
        update=_mark_dirty)
    enabled: bpy.props.BoolProperty(
        name="启用", default=True,
        description="临时停用此球（不参与权重计算）",
        update=_mark_dirty)


class GB_Properties(bpy.types.PropertyGroup):
    """高斯权重球会话属性（挂 Scene.gb_props）。"""

    # -- 状态展示（只读，由 operators 写入）--------------------------------
    status_text: bpy.props.StringProperty(name="状态", default="未开始")
    vg_name: bpy.props.StringProperty(name="顶点组", default="")
    source_info: bpy.props.StringProperty(name="来源", default="")
    island_info: bpy.props.StringProperty(name="网格岛", default="")
    preview_info: bpy.props.StringProperty(name="预览", default="")

    # -- 新建会话行为（双向/合法缺失补权；默认保持现状行为）----------------
    start_direction: bpy.props.EnumProperty(
        name="写入方向",
        items=(
            ('AUTO', "自动", "Source_ 调试物体=正向写入同源目标集合；"
                     "Target_ 调试物体=目标自身（现状行为）"),
            ('REVERSE', "反向（目标→源）", "Target_ 调试物体反向写回原物体/"
                     "原合集多物体分发，需显式选择"),
        ),
        default='AUTO',
        description="从调试物体新建会话时的权重写入方向；反向写源侧属于"
                    "用户原数据修改，需显式选择")
    start_create_missing: bpy.props.BoolProperty(
        name="显式创建缺失组", default=False,
        description="任一侧顶点组不存在时，显式创建并按高斯球范围生成权重"
                    "（合法缺失/未匹配顶点组可补权，不会误判为匹配失败；"
                    "反向写源侧必须开启才会自动建组）")
    clear_outside_on_write: bpy.props.BoolProperty(
        name="写入时球外清零", default=False,
        description="确认写入时把球外顶点（权重场为 0）的该组权重移除；"
                    "默认关闭 = 球外保留原组值",
        update=_mark_dirty)

    # -- 确认行为 ----------------------------------------------------------
    normalize_on_confirm: bpy.props.BoolProperty(
        name="确认后规格化", default=True,
        description="写入权重后对目标物体执行整体权重规格化（导出必需；"
                    "共享顶点处其他顶点组的权重会被同比缩放）")

    # -- 预览 ----------------------------------------------------------------
    heat_opacity: bpy.props.FloatProperty(
        name="热力图不透明度", default=0.85, min=0.0, max=1.0,
        description="目标物体表面权重热力图的整体不透明度",
        update=_mark_dirty)
    only_nearest_island: bpy.props.BoolProperty(
        name="仅影响最近网格岛", default=False,
        description="每个高斯球只把权重写到离自己球心最近的那个不相连网格岛上，"
                    "防止溢出到邻近部件（如身体/眼睛/饰品同物体时）",
        update=_mark_dirty)
    xray_preview: bpy.props.BoolProperty(
        name="透视预览", default=True,
        description="被模型自身遮挡的背面权重以低透明度幽灵层透视显示；"
                    "关闭后只显示可见表面的权重")
    use_evaluated_preview: bpy.props.BoolProperty(
        name="预览使用变形后位置", default=True,
        description="热力图与写入计算使用形态键/骨骼姿态评估后的顶点位置"
                    "（depsgraph 非破坏评估；顶点数改变时自动回退基础网格并提示）。"
                    "关闭则回到 v1 的基础网格位置",
        update=_mark_dirty)
    preview_mode: bpy.props.EnumProperty(
        name="预览模式",
        items=(
            ('COMBINED', "组合权重",
             "显示全部启用球逐点取最大合并后的权重（与写入一致）"),
            ('SINGLE', "单球贡献",
             "只显示当前活动球的独立贡献，便于逐个球调试"),
        ),
        default='COMBINED',
        description="组合权重=max 合并全部启用球；单球贡献=只显示活动球",
        update=_mark_dirty)

    # -- 运行节奏 ------------------------------------------------------------
    tick_interval: bpy.props.FloatProperty(
        name="刷新间隔(秒)", default=0.1, min=0.02, max=1.0,
        description="权重场重算轮询间隔；大网格可调大以降低开销")


gb_properties_list = (
    GB_BallSettings,
    GB_Properties,
)
