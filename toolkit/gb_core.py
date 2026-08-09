# -*- coding: utf-8 -*-
"""高斯权重球：纯 numpy 数学核心（不依赖 bpy，可直接 pytest）。

约定：
- 高斯球在视口是一个 Empty 物体，其世界矩阵的缩放就是影响半径。
  权重在球的局部空间度量：local = M^-1 @ v_h，d = |local|，球心 d=0、球面 d=1。
- 权重公式：w = strength * exp(-falloff_k * d^2)，d >= 1 时严格为 0。
- 所有坐标均为世界坐标 float64；ball_matrix_world 为 4x4 前向世界矩阵（含平移/旋转/缩放）。
"""
import heapq

import numpy as np

# 默认衰减系数：exp(-4.6) ≈ 0.0100，即球面处权重约为中心值的 1%
DEFAULT_FALLOFF_K = 4.6
# 半径统计分位：取"权重 >= 0.5 * 最大权重"顶点到质心距离的 95 分位
RADIUS_PERCENTILE = 95.0
RADIUS_WEIGHT_GATE = 0.5
# 半径钳制，防止退化网格产生 0 半径
MIN_RADIUS = 1e-4
DEFAULT_RADIUS = 0.02
# 写入/预览的有效权重阈值
EPS_WEIGHT = 1e-4


# ---------------------------------------------------------------------------
# 顶点组统计
# ---------------------------------------------------------------------------

def compute_vg_stats(positions, weights):
    """由源顶点组的逐顶点位置与权重计算初始参数。

    Args:
        positions: (N, 3) 世界坐标。
        weights: (N,) 权重，长度与 positions 一致，值域 [0, 1]。

    Returns:
        dict: centroid(3,), max_weight(float), radius(float),
              effective_count(int, 权重>0 的顶点数)。
        全零/空权重时回退：centroid=均值、max_weight=0、radius=DEFAULT_RADIUS。
    """
    positions = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
    weights = np.asarray(weights, dtype=np.float64).reshape(-1)
    n = positions.shape[0]
    if n == 0:
        return {
            "centroid": np.zeros(3, dtype=np.float64),
            "max_weight": 0.0,
            "radius": DEFAULT_RADIUS,
            "effective_count": 0,
        }

    total = float(np.sum(weights))
    if total <= 1e-12:
        return {
            "centroid": np.mean(positions, axis=0),
            "max_weight": 0.0,
            "radius": DEFAULT_RADIUS,
            "effective_count": 0,
        }

    centroid = np.average(positions, axis=0, weights=weights)
    max_weight = float(np.max(weights))

    gate = RADIUS_WEIGHT_GATE * max_weight
    mask = weights >= gate
    effective = int(np.count_nonzero(weights > 0.0))
    if not np.any(mask):
        radius = DEFAULT_RADIUS
    else:
        dists = np.linalg.norm(positions[mask] - centroid[None, :], axis=1)
        radius = float(np.percentile(dists, RADIUS_PERCENTILE))
        if not np.isfinite(radius) or radius < MIN_RADIUS:
            radius = DEFAULT_RADIUS

    return {
        "centroid": centroid,
        "max_weight": max_weight,
        "radius": max(radius, MIN_RADIUS),
        "effective_count": effective,
    }


def initial_ball_params(stats, fallback_location=None):
    """由统计量给出一个新球的初始参数。

    Returns:
        dict: location(3,), strength, radius(作为 Empty 的均匀 scale)。
        无有效权重时 location 回退到 fallback_location（通常是调试物体位置）。
    """
    center = np.asarray(stats.get("centroid", np.zeros(3)), dtype=np.float64).reshape(3)
    strength = float(stats.get("max_weight", stats.get("strength", 1.0)))
    has_weights = stats.get("effective_count")
    if has_weights is None:
        has_weights = 1 if strength > 1e-12 else 0
    if has_weights <= 0 and fallback_location is not None:
        center = np.asarray(fallback_location, dtype=np.float64).reshape(3)
    if strength <= 1e-12:
        strength = 1.0  # 退化输入不产生强度 0 的废球
    return {
        "location": center,
        "center": center,  # 别名
        "strength": strength,
        "radius": float(stats.get("radius", DEFAULT_RADIUS)),
    }


def estimate_falloff_k(positions, weights, centroid, radius, max_weight,
                       k_min=0.5, k_max=8.0, default=DEFAULT_FALLOFF_K):
    """从原始权重分布拟合高斯衰减系数 k。

    模型：w = max_weight * exp(-k * (d/radius)^2)，d 为顶点到质心的距离。
    对每个中段权重样本解出 k_i = -ln(w_i/max_weight) / (d_i/radius)^2，取中位数。

    采样排除：近中心点（d→0 时 k_i 奇异）、近零权重（ln(0) 发散）、
    近满权重（-ln(w/max)→0 无信息）。无有效样本时回退 default。
    结果钳制到 [k_min, k_max]（与 GB_BallSettings.falloff_k 属性范围一致）。
    """
    positions = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
    weights = np.asarray(weights, dtype=np.float64).reshape(-1)
    if (positions.shape[0] == 0 or max_weight <= 1e-12 or radius <= 1e-12):
        return float(default)

    c = np.asarray(centroid, dtype=np.float64).reshape(1, 3)
    d = np.linalg.norm(positions - c, axis=1)
    mask = ((weights > 0.05 * max_weight)
            & (weights < 0.95 * max_weight)
            & (d > 0.05 * radius))
    if not np.any(mask):
        return float(default)

    k_i = -np.log(weights[mask] / max_weight) / (d[mask] / radius) ** 2
    k = float(np.median(k_i))
    if not np.isfinite(k):
        return float(default)
    return float(np.clip(k, k_min, k_max))


def combine_weight_sources(sources):
    """合并多个来源物体的 (positions, weights)（合集匹配的多物体聚合）。

    与顶点组匹配节点的临时合并物体语义一致：同名顶点组的权重跨物体合并，
    位置均为世界坐标。

    Args:
        sources: list[(positions (Ni,3), weights (Ni,))]，可为空。

    Returns:
        (positions (N,3), weights (N,)) float64；空列表返回两个空数组。
    """
    pos_list = [np.asarray(p, dtype=np.float64).reshape(-1, 3) for p, _ in sources]
    w_list = [np.asarray(w, dtype=np.float64).reshape(-1) for _, w in sources]
    if not pos_list:
        return np.zeros((0, 3), dtype=np.float64), np.zeros(0, dtype=np.float64)
    return np.vstack(pos_list), np.concatenate(w_list)


# ---------------------------------------------------------------------------
# 高斯场
# ---------------------------------------------------------------------------

def _to_ball_local(pts, ball_matrix_world):
    """把世界坐标变换到球局部空间 (N,3)；矩阵不可逆时返回 None。"""
    m = np.asarray(ball_matrix_world, dtype=np.float64).reshape(4, 4)
    try:
        m_inv = np.linalg.inv(m)
    except np.linalg.LinAlgError:
        return None
    if not np.all(np.isfinite(m_inv)):
        return None
    pts = np.asarray(pts, dtype=np.float64).reshape(-1, 3)
    hom = np.concatenate([pts, np.ones((pts.shape[0], 1), dtype=np.float64)], axis=1)
    local = hom @ m_inv.T
    w4 = local[:, 3]
    w4[np.abs(w4) < 1e-12] = 1.0
    return local[:, :3] / w4[:, None]


def gaussian_field(verts_world, ball_matrix_world, strength, falloff_k):
    """计算一个球对全部顶点的权重场（解析高斯模式）。

    Args:
        verts_world: (N, 3) 目标顶点世界坐标。
        ball_matrix_world: (4, 4) 球的前向世界矩阵（缩放=半径）。函数内部求逆。
        strength: 中心强度。
        falloff_k: 衰减系数。

    Returns:
        (N,) float64 权重，值域 [0, strength]；局部距离 d>=1 处为 0。
        矩阵不可逆（如缩放为 0）时返回全零。
    """
    verts = np.asarray(verts_world, dtype=np.float64).reshape(-1, 3)
    n = verts.shape[0]
    if n == 0:
        return np.zeros(0, dtype=np.float64)

    local = _to_ball_local(verts, ball_matrix_world)
    if local is None:
        return np.zeros(n, dtype=np.float64)

    d2 = np.einsum("ij,ij->i", local, local)
    field = float(strength) * np.exp(-float(falloff_k) * d2)
    field[d2 >= 1.0] = 0.0
    return field


# ---------------------------------------------------------------------------
# 测地（沿表面传播）权重场
# ---------------------------------------------------------------------------

def edges_from_triangles(tri_indices):
    """从三角形索引提取去重边 (E,2) int64；空输入返回 (0,2)。"""
    t = np.asarray(tri_indices, dtype=np.int64).reshape(-1, 3)
    if t.shape[0] == 0:
        return np.zeros((0, 2), dtype=np.int64)
    e = np.concatenate([t[:, [0, 1]], t[:, [1, 2]], t[:, [2, 0]]], axis=0)
    e.sort(axis=1)
    return np.unique(e, axis=0)


def build_surface_adjacency(local_pts, edge_verts):
    """预构建沿表面传播 Dijkstra 邻接表（含边权重）。
    多次调用可复用同一邻接表（例如多个球共享同一网格拓扑时只构建一次）。
    Returns:
        list[list[(int, float)]]：adj[i] = [(j, weight), ...]，权重为点对距离。
    """
    pts = np.asarray(local_pts, dtype=np.float64).reshape(-1, 3)
    n = pts.shape[0]
    adj = [[] for _ in range(n)]
    edges = np.asarray(edge_verts, dtype=np.int64).reshape(-1, 2)
    if len(edges):
        mask = (edges[:, 0] >= 0) & (edges[:, 0] < n) & (edges[:, 1] >= 0) & (edges[:, 1] < n)
        edges = edges[mask]
        weights = np.linalg.norm(pts[edges[:, 0]] - pts[edges[:, 1]], axis=1)
        for (a, b), w in zip(edges.tolist(), weights.tolist()):
            adj[a].append((b, float(w)))
            adj[b].append((a, float(w)))
    return adj


def surface_distances(local_pts, edge_verts, seed_mask, adjacency=None, allowed_mask=None):
    """多源 Dijkstra 表面距离（球局部坐标度量）。

    种子顶点初始距离 = 其欧氏 |local|，其余顶点从 ∞ 开始沿网格边松弛。
    表面距离 ≥ 欧氏距离恒成立；典型用法是只把“球与表面的接触点”（离球心
    最近的顶点）作种子，球体积内的其余表面（背面/对侧）因表面绕行距离
    ≥1 而自然拿到 0 权重。

    Args:
        local_pts: (N,3) 球局部坐标（d = |local|，球面 d=1）。
        edge_verts: (E,2) int 边顶点索引。
        seed_mask: (N,) bool，种子（球内顶点）。
        adjacency: 可选，build_surface_adjacency 的预构建结果（与 local_pts 同空间），
            多次调用可复用；为 None 时内部构建。
        allowed_mask: 可选 (N,) bool，只允许沿这些顶点传播（例如包含物体列表过滤）；
            None = 全部允许。种子也必须是 allowed。

    Returns:
        (N,) float64 表面距离；未与任何种子连通的顶点为 inf。
    """
    n = local_pts.shape[0]
    dist = np.full(n, np.inf)
    adj = adjacency if adjacency is not None else build_surface_adjacency(local_pts, edge_verts)
    allowed = np.ones(n, dtype=bool) if allowed_mask is None else np.asarray(allowed_mask, dtype=bool).reshape(-1)
    heap = []
    for i in np.nonzero(np.asarray(seed_mask, dtype=bool).reshape(-1))[0]:
        if not allowed[i]:
            continue
        d0 = float(np.linalg.norm(local_pts[i]))
        dist[i] = d0
        heapq.heappush(heap, (d0, int(i)))
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist[u] + 1e-12 or d >= 1.0:
            continue  # 陈旧堆项，或已出球面半径（权重为 0，无需继续传播）
        for v, w in adj[u]:
            if not allowed[v]:
                continue
            nd = d + w
            if nd < dist[v] - 1e-12:
                dist[v] = nd
                heapq.heappush(heap, (nd, v))
    return dist


def surface_distances_uniform_scale(adjacency, world_pts, center, scale, allowed_mask=None):
    """沿表面传播快速路径：均匀缩放（旋转/平移/等比缩放）球复用世界坐标邻接表。
    球局部距离 = 世界沿表面距离 / scale（球面 d=1 ⇔ 世界距离 = scale），
    因此用世界边长邻接表跑 Dijkstra、按 scale 截断并归一，结果与逐球构建完全一致。
    只适用于均匀缩放球；非均匀缩放必须回退 surface_distances。
    Args:
        adjacency: build_surface_adjacency(world_pts, edge_verts) 预构建邻接表。
        world_pts: (N,3) 世界坐标顶点（与邻接表同索引）。
        center: (3,) 球心世界坐标。
        scale: 球均匀缩放（球半径 = scale）。
    allowed_mask: 可选 (N,) bool，只允许沿这些顶点传播；None = 全部允许。
    Returns:
        (N,) float64 球局部表面距离；不可达为 inf。
    """
    pts = np.asarray(world_pts, dtype=np.float64).reshape(-1, 3)
    center = np.asarray(center, dtype=np.float64).reshape(3)
    n = pts.shape[0]
    d2 = np.einsum("ij,ij->i", pts - center, pts - center)
    allowed = np.ones(n, dtype=bool) if allowed_mask is None else np.asarray(allowed_mask, dtype=bool).reshape(-1)
    dist = np.full(n, np.inf)
    if not np.any(allowed):
        return dist
    valid_d2 = np.where(allowed, d2, np.inf)
    seed = int(np.argmin(valid_d2))
    d0 = float(np.sqrt(d2[seed]))
    dist[seed] = d0
    heap = [(d0, seed)]
    cutoff = float(scale)
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist[u] + 1e-12 or d >= cutoff:
            continue
        for v, w in adjacency[u]:
            if not allowed[v]:
                continue
            nd = d + w
            if nd < dist[v] - 1e-12:
                dist[v] = nd
                heapq.heappush(heap, (nd, v))
    dist /= cutoff
    return dist


def geodesic_field(verts_world, ball_matrix_world, strength, falloff_k, edge_verts):
    """沿表面传播的高斯权重场（测地版 gaussian_field）。

    与 gaussian_field 同形：w = strength * exp(-falloff_k * d²)，d ≥ 1 严格为 0。
    核心区别：种子不是球内全部顶点，而是“球与表面的接触点”（离球心最近的
    表面顶点），其余顶点按沿网格表面传播的距离衰减——权重只从接触点沿表面
    向四周扩散，不会穿透到球体积覆盖的背面/对侧表面。
    """
    verts = np.asarray(verts_world, dtype=np.float64).reshape(-1, 3)
    n = verts.shape[0]
    if n == 0:
        return np.zeros(0, dtype=np.float64)
    local = _to_ball_local(verts, ball_matrix_world)
    if local is None:
        return np.zeros(n, dtype=np.float64)
    d2 = np.einsum("ij,ij->i", local, local)
    # 种子 = 离球心最近的表面顶点（接触点）；其初始距离 = 其欧氏 |local|
    seeds = np.zeros(n, dtype=bool)
    seeds[int(np.argmin(d2))] = True
    d = surface_distances(local, edge_verts, seeds)
    field = float(strength) * np.exp(-float(falloff_k) * d * d)
    field[d >= 1.0] = 0.0
    return field


def sampled_field(verts_world, source_positions, source_weights,
                  ball_matrix_world, strength_scale=1.0):
    """源权重采样场：球范围内目标顶点取最近源顶点的原始权重。

    与解析高斯不同：不把源分布压缩成中心/强度/衰减几个参数，而是直接
    **保留源权重的真实分布**——多峰、非对称、不规则形状都能正确传递；
    权重沿源网格表面传播，不会穿透到网格内部。

    Args:
        verts_world: (N, 3) 目标顶点世界坐标。
        source_positions: (M, 3) 源顶点组非零权重顶点的世界坐标。
        source_weights: (M,) 与 source_positions 对应的原始权重。
        ball_matrix_world: (4, 4) 球的前向世界矩阵（缩放=半径）。
        strength_scale: 倍率（默认 1.0 = 原样保留源权重）。

    Returns:
        (N,) float64 权重：球内顶点 = 最近源顶点权重 × 倍率，球外为 0。
        源点云为空时返回全零。
    """
    verts = np.asarray(verts_world, dtype=np.float64).reshape(-1, 3)
    src_pos = np.asarray(source_positions, dtype=np.float64).reshape(-1, 3)
    src_w = np.asarray(source_weights, dtype=np.float64).reshape(-1)
    n = verts.shape[0]
    if n == 0 or src_pos.shape[0] == 0:
        return np.zeros(n, dtype=np.float64)

    v_local = _to_ball_local(verts, ball_matrix_world)
    if v_local is None:
        return np.zeros(n, dtype=np.float64)
    s_local = _to_ball_local(src_pos, ball_matrix_world)
    if s_local is None:
        return np.zeros(n, dtype=np.float64)

    v_d2 = np.einsum("ij,ij->i", v_local, v_local)
    s_d2 = np.einsum("ij,ij->i", s_local, s_local)

    # 球内目标顶点（d < 1）；球外严格为 0
    inside = v_d2 < 1.0
    if not np.any(inside):
        return np.zeros(n, dtype=np.float64)
    # 球内源点：只在这些源点中找最近邻（球外源点对球内目标不可能更近）
    src_inside = np.nonzero(s_d2 < 1.0)[0]
    if src_inside.size == 0:
        return np.zeros(n, dtype=np.float64)

    v_idx = np.nonzero(inside)[0]
    # 分块最近邻（避免一次性 (K×M×3) 内存爆掉）
    nearest = np.empty(v_idx.size, dtype=np.int64)
    s_local_in = s_local[src_inside]
    s_w_in = src_w[src_inside]
    chunk = 512
    for start in range(0, v_idx.size, chunk):
        end = min(start + chunk, v_idx.size)
        diff = v_local[v_idx[start:end], None, :] - s_local_in[None, :, :]
        d2 = np.einsum("ijk,ijk->ij", diff, diff)
        nearest[start:end] = src_inside[np.argmin(d2, axis=1)]

    field = np.zeros(n, dtype=np.float64)
    field[v_idx] = src_w[nearest] * float(strength_scale)
    return field


def merge_fields_max(fields):
    """多个球的权重场按逐点 max 合并。

    Args:
        fields: list[(N,) ndarray]，可为空。

    Returns:
        (N,) ndarray；空列表返回空数组。
    """
    if not fields:
        return np.zeros(0, dtype=np.float64)
    if len(fields) == 1:
        return np.asarray(fields[0], dtype=np.float64)
    return np.maximum.reduce([np.asarray(f, dtype=np.float64) for f in fields])


# ---------------------------------------------------------------------------
# 网格岛（并查集）
# ---------------------------------------------------------------------------

def compute_island_ids(vert_count, edge_verts):
    """按边连通性给每个顶点分配岛 ID（并查集，路径压缩 + 按秩合并）。

    Args:
        vert_count: 顶点总数。
        edge_verts: (E, 2) int 数组，每行一条边的两个顶点下标。

    Returns:
        (vert_count,) int64 数组：岛 ID（0..K-1 连续编号，按首次出现顺序）。
        孤立顶点（无边）各自成岛。
    """
    vert_count = int(vert_count)
    parent = list(range(vert_count))
    rank = [0] * vert_count

    def find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if rank[ra] < rank[rb]:
            ra, rb = rb, ra
        parent[rb] = ra
        if rank[ra] == rank[rb]:
            rank[ra] += 1

    edges = np.asarray(edge_verts, dtype=np.int64).reshape(-1, 2)
    for a, b in edges:
        if 0 <= a < vert_count and 0 <= b < vert_count:
            union(int(a), int(b))

    roots = np.array([find(i) for i in range(vert_count)], dtype=np.int64)
    _, ids = np.unique(roots, return_inverse=True)
    return ids.astype(np.int64)


def nearest_island(island_ids, verts_world, center):
    """返回离 center 最近顶点所在的岛 ID；空输入返回 -1。"""
    ids = np.asarray(island_ids, dtype=np.int64).reshape(-1)
    if ids.size == 0:
        return -1
    verts = np.asarray(verts_world, dtype=np.float64).reshape(-1, 3)
    c = np.asarray(center, dtype=np.float64).reshape(1, 3)
    d2 = np.sum((verts - c) ** 2, axis=1)
    return int(ids[int(np.argmin(d2))])


# 直观命名别名（与计划/测试用语一致）
def nearest_island_at(verts_world, island_ids, center):
    """nearest_island 的别名，参数顺序为 (verts, island_ids, center)。"""
    return nearest_island(island_ids, verts_world, center)


def mask_field_to_island(field, island_ids, island_id):
    """把岛外顶点的权重置 0（返回拷贝，不改入参）。"""
    out = np.array(field, dtype=np.float64, copy=True)
    out[np.asarray(island_ids, dtype=np.int64).reshape(-1) != int(island_id)] = 0.0
    return out


# 直观命名别名
def apply_island_mask(weights, island_ids, island_id):
    """mask_field_to_island 的别名。"""
    return mask_field_to_island(weights, island_ids, island_id)


# ---------------------------------------------------------------------------
# 热力图颜色
# ---------------------------------------------------------------------------

# 蓝 -> 青 -> 绿 -> 黄 -> 红 的 5 段色带（RGB）
_COLOR_STOPS = np.array([
    [0.05, 0.10, 0.90],   # 0.00 蓝
    [0.00, 0.80, 0.90],   # 0.25 青
    [0.10, 0.85, 0.15],   # 0.50 绿
    [1.00, 0.85, 0.05],   # 0.75 黄
    [1.00, 0.10, 0.05],   # 1.00 红
], dtype=np.float64)


def weights_to_colors(weights, opacity=0.85):
    """把权重映射为热力图 RGBA 颜色。

    Args:
        weights: (N,) 权重（值域 [0, 1]），按**绝对值**映射——不按最大值归一化，
            否则调整中心强度时整个场同比缩放、归一化后又抵消，热力图上看不到
            任何变化（回归修复）。
        opacity: 最大不透明度（权重=1 处）。

    Returns:
        (N, 4) float64 RGBA；权重 <= 0 的顶点 alpha=0（不显示）。
    """
    w = np.asarray(weights, dtype=np.float64).reshape(-1)
    n = w.shape[0]
    if n == 0:
        return np.zeros((0, 4), dtype=np.float64)

    t = np.clip(w, 0.0, 1.0)

    # 分段线性插值
    seg = t * (len(_COLOR_STOPS) - 1)
    idx = np.clip(seg.astype(np.int64), 0, len(_COLOR_STOPS) - 2)
    frac = (seg - idx)[:, None]
    c0 = _COLOR_STOPS[idx]
    c1 = _COLOR_STOPS[idx + 1]
    rgb = c0 + (c1 - c0) * frac

    alpha = np.where(w > 0.0, float(opacity) * t, 0.0)
    return np.concatenate([rgb, alpha[:, None]], axis=1)
