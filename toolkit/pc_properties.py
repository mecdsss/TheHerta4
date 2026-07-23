# -*- coding: utf-8 -*-
"""点云姿态匹配：Blender 属性组定义。"""
import bpy


def _poll_mesh(self, obj):
    return obj is not None and obj.type == 'MESH'


def _poll_armature(self, obj):
    return obj is not None and obj.type == 'ARMATURE'


def _history_cursor_update(self, context):
    """进度条拖动回调：只记录目标步，由计时器去抖执行 seek（防重入）。"""
    try:
        from . import pc_operators
        pc_operators.request_seek_from_cursor(self.history_cursor)
    except Exception:
        pass


class PC_BoneItem(bpy.types.PropertyGroup):
    """骨骼列表项。"""
    name: bpy.props.StringProperty(name="骨骼名")
    enabled: bpy.props.BoolProperty(name="启用", default=True)
    kind: bpy.props.StringProperty(name="类型", default="deform")  # deform | controller
    lock_info: bpy.props.StringProperty(name="锁定摘要", default="")
    has_constraints: bpy.props.BoolProperty(name="带约束", default=False)


class PC_Properties(bpy.types.PropertyGroup):
    """点云姿态匹配主属性组（挂 Scene.pc_props）。"""

    # -- 目标与采样 ------------------------------------------------------
    a_object: bpy.props.PointerProperty(
        name="模型A（无骨架）", type=bpy.types.Object, poll=_poll_mesh,
        description="目标网格物体（静态点云）")
    b_armature: bpy.props.PointerProperty(
        name="骨架B", type=bpy.types.Object, poll=_poll_armature,
        description="被调整的骨架物体；蒙皮网格经 Armature 修改器自动查找")
    sample_count: bpy.props.IntProperty(
        name="采样点数", default=8000, min=500, max=100000,
        description="A/B 各自独立的空间体素采样上限；两侧点数可以不同，不依赖顶点编号或一一对应")
    threshold_mode: bpy.props.EnumProperty(
        name="匹配阈值",
        items=[('AUTO', "自动", "阈值取 A 包围盒对角线的 1%"),
               ('MANUAL', "手动", "手动指定距离阈值")],
        default='AUTO')
    threshold: bpy.props.FloatProperty(
        name="距离阈值", default=0.02, min=0.0, precision=4,
        description="最近点距离小于该值即算重合；同一值也用于体素采样与严格体素诊断")

    # -- 优化参数 ------------------------------------------------------
    max_angle_deg: bpy.props.FloatProperty(
        name="单步最大角(°)", default=3.0, min=0.1, max=30.0)
    max_scale_delta: bpy.props.FloatProperty(
        name="单步最大缩放", default=0.05, min=0.001, max=0.5)
    max_translation: bpy.props.FloatProperty(
        name="单步最大位移比", default=0.02, min=0.001, max=0.2,
        description="相对 A 包围盒对角线的比例")
    learning_rate: bpy.props.FloatProperty(
        name="学习率", default=1.0, min=0.01, max=1.0)
    prior_rotation: bpy.props.FloatProperty(
        name="旋转先验", default=0.7, min=0.0, max=1.0,
        description="初期优先旋转；随收益自动漂移")
    prior_scale: bpy.props.FloatProperty(
        name="缩放先验", default=0.2, min=0.0, max=1.0)
    prior_location: bpy.props.FloatProperty(
        name="位移先验", default=0.1, min=0.0, max=1.0)

    # -- 运行节奏 ------------------------------------------------------
    steps_per_tick: bpy.props.IntProperty(
        name="步/刷新", default=1, min=1, max=1000,
        description="每次计时器触发执行的迭代步数（调大加速，视图仍按刷新率更新）")
    tick_interval: bpy.props.FloatProperty(
        name="刷新间隔(秒)", default=0.05, min=0.005, max=1.0)

    # -- 历史 ------------------------------------------------------------
    snapshot_interval: bpy.props.IntProperty(
        name="快照间隔", default=500, min=10, max=10000)
    max_history: bpy.props.IntProperty(
        name="历史上限", default=300000, min=1000, max=5000000,
        description="超过后把最老历史烘焙成单个快照")

    # -- 骨骼列表 ------------------------------------------------------
    bone_list: bpy.props.CollectionProperty(type=PC_BoneItem)
    bone_list_index: bpy.props.IntProperty(name="骨骼列表索引", default=0)

    # -- 运行时显示（面板只读展示） --------------------------------------
    cur_step: bpy.props.IntProperty(name="当前步", default=0)
    cur_f1: bpy.props.FloatProperty(name="当前重合率", default=0.0, precision=4)
    best_f1: bpy.props.FloatProperty(name="最佳重合率", default=0.0, precision=4)
    best_step: bpy.props.IntProperty(name="最佳步", default=0)
    status_text: bpy.props.StringProperty(name="状态", default="未初始化")

    history_cursor: bpy.props.FloatProperty(
        name="迭代进度", default=0.0, min=0.0, max=1.0, subtype='FACTOR',
        update=_history_cursor_update,
        description="拖动回退/前进到任意迭代步（运行中自动跟随）")
    history_total: bpy.props.IntProperty(name="总步数", default=0)
    history_view_step: bpy.props.IntProperty(name="游标对应步", default=0)

    sched_w_rot: bpy.props.FloatProperty(name="旋转权重", default=0.0, precision=3)
    sched_w_scale: bpy.props.FloatProperty(name="缩放权重", default=0.0, precision=3)
    sched_w_loc: bpy.props.FloatProperty(name="位移权重", default=0.0, precision=3)
    sched_phase: bpy.props.StringProperty(name="优化阶段", default="仅旋转")
    sched_phase_status: bpy.props.StringProperty(name="阶段收敛状态", default="等待首次检查")
    bone_curriculum_status: bpy.props.StringProperty(
        name="当前骨骼课程", default="等待迭代")
    bone_curriculum_detail: bpy.props.StringProperty(
        name="骨骼课程详情", default="")
    last_move_summary: bpy.props.StringProperty(
        name="最近一步", default="等待第一次迭代")
    last_move_detail: bpy.props.StringProperty(
        name="最近变换量", default="")
    residual_mean: bpy.props.FloatProperty(name="平均残差", default=0.0, precision=5)
    debug_overlap_text: bpy.props.StringProperty(
        name="当前场景重合率", default="未计算")

    # -- 性能加速 ------------------------------------------------------
    backend_mode: bpy.props.EnumProperty(
        name="计算后端",
        items=[('AUTO', "自动", "有 CUDA torch 用显卡，否则 numpy(BLAS)"),
               ('NUMPY', "强制 NumPy", "CPU 分块 GEMM（BLAS 多线程）"),
               ('TORCH', "强制 Torch(CUDA)", "显卡加速；不可用时回退 numpy")],
        default='TORCH')
    use_headless: bpy.props.BoolProperty(
        name="高速模式(虚拟骨架)", default=True,
        description="迭代全程纯 numpy 虚拟骨架，每 tick 批量迭代后同步一次视口；"
                    "控制器骨/带约束骨不参与（需实时模式）；构建期验证失败自动回退")
    use_fast_lbs: bpy.props.BoolProperty(
        name="快速蒙皮(LBS)", default=True,
        description="自算蒙皮替代每步 depsgraph 网格重读；构建期自动验证，不通过则回退")
    use_approximate_fallback: bpy.props.BoolProperty(
        name="近似高速回退", default=True,
        description="当 Preserve Volume/约束导致 LBS/虚拟骨架验证失败时，仍启用近似高速批跑，并在每个刷新周期做一次真实校正")
    minibatch_size: bpy.props.IntProperty(
        name="小批量评估", default=1024, min=0, max=8192,
        description="每步用于重合率估计的随机样本数（0=关闭，每步全量精确评估）")
    full_eval_interval: bpy.props.IntProperty(
        name="全量锚点间隔", default=500, min=10, max=100000,
        description="小批量模式下每隔多少步做一次全量精确评估并刷新最佳记录")
    phase_eval_interval: bpy.props.IntProperty(
        name="阶段检查间隔", default=50, min=10, max=10000,
        description="每隔多少步用固定验证点检查一次重合率是否收敛")
    phase_plateau_delta: bpy.props.FloatProperty(
        name="阶段最小收益", default=0.0001, min=0.000001, max=0.01,
        precision=6,
        description="最佳重合率提升小于该值时记为一次无明显收益")
    phase_plateau_checks: bpy.props.IntProperty(
        name="连续无收益次数", default=3, min=1, max=20,
        description="连续多少次重合率检查无明显收益后进入下一阶段")
    approximate_realign_interval: bpy.props.IntProperty(
        name="真实校正间隔(步)", default=20, min=1, max=100000,
        description="近似高速模式下每隔多少步用 Blender 真实评估校正一次；值越小越稳，越大越快")
    perf_info: bpy.props.StringProperty(name="性能信息", default="")


pc_properties_list = (
    PC_BoneItem,
    PC_Properties,
)
