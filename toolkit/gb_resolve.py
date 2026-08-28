# -*- coding: utf-8 -*-
"""高斯权重球：写入目标解析与逐物体写入核心（尽量少依赖 bpy，可用 stub 测试）。

与 gb_operators 的分工（避免把 UI/会话状态当业务真相）：
- gb_operators 持有会话/球/热力图等 bpy 耦合状态与操作符；
- 本模块提供可独立测试的纯逻辑：
    * 方向判定（正向 / 目标自身 / 反向写入源侧）；
    * `源名=目标名` 重命名格式的顶点组查找（角色感知剥前缀）；
    * 反向写入目标解析（单源物体 / 源合集多物体分发）；
    * 缺组分类（区分“匹配关系失效”与“合法缺失可补权”）；
    * 单物体权重写入（REPLACE/EPS/钳制/球外策略/显式建组/拓扑守卫）。

约定（与 gb_core 一致）：权重有效阈值 EPS_WEIGHT=1e-4；
球外（w <= eps）默认保留原值，球内 REPLACE 覆写 min(1.0, w)。
"""
import numpy as np

from . import gb_core

# ---------------------------------------------------------------------------
# 会话方向
# ---------------------------------------------------------------------------

#: 正向：源→目标集合（Source_ 调试物体；写目标侧同名组）
DIRECTION_FORWARD = "forward"
#: 目标自身：在目标物体自身组上编辑（现状兼容，Target_ 调试物体 + AUTO）
DIRECTION_SELF = "self"
#: 反向：目标→源物体 / 源合集多物体分发（Target_ 调试物体 + REVERSE）
DIRECTION_REVERSE = "reverse"

#: 顶点组角色：以“目标组名”视角查找（`源名=目标名` 取右边；反向写源侧
#: 目标组名命中被改名的源侧组）
ROLE_TARGET = "target"
#: 顶点组角色：以“源组名”视角查找（`源名=目标名` 取左边；读源权重/快速权重
#: 的既有语义）
ROLE_SOURCE = "source"
#: 仅精确名匹配（写目标侧默认；`=` 重命名只发生在源侧，目标侧不猜名）
ROLE_ANY = "any"


def decide_direction(marker_name, requested="AUTO"):
    """由调试物体名前缀与请求方向判定会话实际方向。

    Args:
        marker_name: 调试物体名（Source_* / Target_*）。
        requested: 'AUTO'（默认）或 'REVERSE'。

    Returns:
        DIRECTION_FORWARD | DIRECTION_SELF | DIRECTION_REVERSE。

    规则（保持现状兼容）：
    - Source_* 调试物体：永远正向（源→目标集合），反向请求无意义，钳制为正向；
    - Target_* 调试物体：AUTO=目标自身（现状）；REVERSE=反向写回源侧。
    """
    if marker_name.startswith("Source_"):
        return DIRECTION_FORWARD
    if requested == "REVERSE":
        return DIRECTION_REVERSE
    return DIRECTION_SELF


def is_reverse_request(marker_name, requested="AUTO"):
    """是否请求反向写入（仅 Target_ 调试物体 + REVERSE 才成立）。"""
    return marker_name.startswith("Target_") and requested == "REVERSE"


def write_policy(direction, create_missing=False):
    """按会话方向给出写入策略（查找角色 + 是否允许自动建组）。

    规则（R5 防误写保护 + NW2 显式补权）：
    - 正向/目标自身：写目标侧；目标侧组名是普通名（`源名=目标名` 重命名只
      发生在源侧），保持既有“精确名查找，缺失自动创建”行为（ROLE_ANY）；
    - 反向：写源侧——源侧组可能被“应用到原物体”改名为 `源名=目标名`，
      以目标组名（右部）命中既有组；源侧是用户原数据，缺组仅在会话显式
      开启“创建缺失组”时才自动创建，否则跳过后由调用方提示。

    Returns:
        dict: {"role": ROLE_ANY|ROLE_TARGET, "allow_create": bool}
    """
    if direction == DIRECTION_REVERSE:
        return {"role": ROLE_TARGET, "allow_create": bool(create_missing)}
    return {"role": ROLE_ANY, "allow_create": True}


# ---------------------------------------------------------------------------
# 顶点组查找（`源名=目标名` 兼容）
# ---------------------------------------------------------------------------

def find_vertex_group(obj, name, role=ROLE_ANY):
    """在物体上按名称查找顶点组，兼容 `源名=目标名` 重命名格式。

    查找顺序（确定性，与快速权重的一致语义）：
    1. 精确名匹配（vertex_groups.get(name)）；
    2. role 指定时，对含 '=' 的组名剥前缀：
       - ROLE_SOURCE：左边（源名）与 name 相同（读源权重的既有语义）；
       - ROLE_TARGET：右边（目标名）与 name 相同（反向写回源侧命中
         `源名=目标名` 被改名的组）；
       多个候选取组顺序第一个。

    Args:
        obj: 带 vertex_groups 的物体（真实 bpy 物体或测试 fake）。
        name: 目标组名。
        role: ROLE_ANY / ROLE_SOURCE / ROLE_TARGET。

    Returns:
        命中的顶点组对象；找不到返回 None。
    """
    vg = getattr(obj, "vertex_groups", None)
    if vg is None:
        return None
    exact = vg.get(name)
    if exact is not None:
        return exact
    if role == ROLE_ANY:
        return None
    for cand in vg:
        if "=" not in cand.name:
            continue
        left, right = (p.strip() for p in cand.name.split("=", 1))
        if role == ROLE_SOURCE and left == name:
            return cand
        if role == ROLE_TARGET and right == name:
            return cand
    return None


def read_group_weights(obj, vg_name, role=ROLE_ANY):
    """读取物体某顶点组的逐顶点权重 (N,)。

    Args:
        obj: 带 vertex_groups 与 data.vertices 的网格物体。
        vg_name: 组名（支持 `=` 前缀角色感知查找）。
        role: 查找角色。

    Returns:
        (vg, weights, matched_name)：
        - vg：命中的顶点组；组不存在时为 None（weights/matched_name 亦为 None）。
        - weights：(N,) float64 逐顶点权重。
        - matched_name：实际命中的组名（精确名或 `=` 解析前的原名）。
    """
    vg = find_vertex_group(obj, vg_name, role=role)
    if vg is None:
        return None, None, None
    count = len(obj.data.vertices)
    weights = np.zeros(count, dtype=np.float64)
    for i, v in enumerate(obj.data.vertices):
        for g in getattr(v, "groups", ()):
            if g.group == vg.index:
                weights[i] = g.weight
                break
    return vg, weights, vg.name


# ---------------------------------------------------------------------------
# 反向写入目标解析
# ---------------------------------------------------------------------------

def resolve_reverse_targets(source_objects, exclude_names=()):
    """从源侧物体列表构造反向写入目标（目标→源 / 目标→源合集分发）。

    Args:
        source_objects: 源物体列表（get_debug_source_objects 的结果）。
            合集模式下即为合集内全部网格物体，将逐一作为写入目标。
        exclude_names: 排除名集合——反向写源侧时必须跳过临时合并物体
            （它是匹配计算用的临时拷贝，不是用户原物体，写入无意义）。

    Returns:
        dict: {
            "kind": "none" | "single" | "collection",
            "objects": [obj, ...] 去重且保序（仅 MESH 且带 data.vertices）,
        }
    """
    skip = set(exclude_names or ())
    seen = set()
    objs = []
    for o in source_objects:
        if o is None:
            continue
        if o.name in skip or o.name in seen:
            continue
        if getattr(o, "type", "") != "MESH":
            continue
        verts = getattr(getattr(o, "data", None), "vertices", None)
        if verts is None:
            continue
        seen.add(o.name)
        objs.append(o)
    if not objs:
        kind = "none"
    elif len(objs) == 1:
        kind = "single"
    else:
        kind = "collection"
    return {"kind": kind, "objects": objs}


def classify_group_presence(objects, vg_name, role=ROLE_SOURCE):
    """统计写入目标集合中某顶点组的缺失情况（显式补权判定）。

    用于把“合法缺失（可显式补权）”与“匹配关系失效（报错）”区分开：
    调试关系有效但某侧组不存在 = 合法缺失，可勾选显式创建后按高斯球范围补权；
    调试父/目标物体/源集合不存在 = 匹配失败，直接报错。

    Returns:
        dict: {"present": [obj, ...], "missing": [obj, ...], "total": int}
    """
    present, missing = [], []
    for o in objects:
        vg = find_vertex_group(o, vg_name, role=role)
        (present if vg is not None else missing).append(o)
    return {
        "present": present,
        "missing": missing,
        "total": len(present) + len(missing),
    }


# ---------------------------------------------------------------------------
# 单物体权重写入（NW3/NW4 固化的确定性规则）
# ---------------------------------------------------------------------------

def write_field_to_object(obj, vg_name, field, role=ROLE_TARGET,
                          eps=None, create_missing=False, clear_outside=False):
    """把一个权重场写入物体的同名顶点组（正向/反向写路径共用）。

    规则（与既有确认语义一致并固化）：
    - 拓扑守卫：field 长度必须等于物体顶点数，否则跳过（拓扑变化守卫）；
    - 球内（w > eps）：'REPLACE' 覆写 min(1.0, w)；
    - 球外（w <= eps）：默认保留原值；clear_outside=True 时从组移除（球外清零）；
    - 组不存在：create_missing=True 显式新建（角色感知查找已先行尝试）；
      否则跳过——反向写源侧（用户数据）默认不自动建组，需显式开启；
    - 整个场均 <= eps（球全在范围外）：跳过，不写入也不清球外。

    Args:
        obj: 目标网格物体（带 vertex_groups 与 data.vertices）。
        vg_name: 写入的组名（支持 `=` 前缀角色感知查找命中既有组）。
        field: (N,) float64 权重场（球内的合并结果）。
        role: 查找既有组时的角色（反向写源侧=ROLE_TARGET 命中 `源名=目标名`
            右部；正向写目标侧=ROLE_ANY 仅精确名，缺失自动建组）。
        eps: 有效权重阈值（默认 gb_core.EPS_WEIGHT）。
        create_missing: 组不存在时是否显式创建。
        clear_outside: 球外顶点是否从组移除（默认 False = 保留原值）。

    Returns:
        dict: {
            "written": int（实际写入顶点数）,
            "created": bool（本次是否新建了组）,
            "reason": "" | "topology_mismatch" | "no_group" | "empty_field",
        }
        reason 非空表示跳过（topology_mismatch 优先级最高）。
    """
    eps = gb_core.EPS_WEIGHT if eps is None else float(eps)
    field = np.asarray(field, dtype=np.float64).reshape(-1)
    reason = ""

    vert_count = len(obj.data.vertices)
    if field.shape[0] != vert_count:
        reason = "topology_mismatch"
    elif not np.any(field > eps):
        reason = "empty_field"

    if reason:
        return {"written": 0, "created": False, "reason": reason}

    vg = find_vertex_group(obj, vg_name, role=role)
    created = False
    if vg is None:
        if not create_missing:
            return {"written": 0, "created": False, "reason": "no_group"}
        vg = obj.vertex_groups.new(name=vg_name)
        created = True

    count = 0
    for i in range(vert_count):
        w = min(1.0, float(field[i]))
        if w > eps:
            vg.add([i], w, 'REPLACE')
            count += 1

    if clear_outside and count > 0:
        outside = [i for i in range(vert_count) if float(field[i]) <= eps]
        if outside:
            try:
                vg.remove(outside)
            except RuntimeError:
                # 个别 bpy 版本对部分索引抛错时仍要保证不中断写入；
                # 球外清零是可选策略，失败只降级为保留原值
                pass

    return {"written": count, "created": created, "reason": ""}