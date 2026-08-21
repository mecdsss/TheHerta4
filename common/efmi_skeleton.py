"""
EFMI（明日方舟：终末地）骨骼合并支持模块

数据来源：
1. SSMT 工作空间：子网格 json（`<drawib>-<index_count>-<first_index>` 目录）+ 子网格 json 内
   CategoryBufferList（Blend 类别 buffer 的 D3D11ElementList）+ 角色级
   `ComponentName_DrawCallIndexList.json`（子网格名 -> drawcall 索引列表）。
2. FrameAnalysis 帧提取 dump（3Dmigoto 原始捕获）：
   - `log.txt`：逐 draw 调用记录（DrawIndexedInstanced / VSSetConstantBuffers1 / VSSetShaderResources /
     3DMigoto Dumping Buffer 资源去重映射），含每个常量缓冲绑定的 first_constant/num_constants 窗口。
   - `deduped/`：去重后的实际数据文件（.buf 原始字节 / .txt 文本 dump）。

骨骼合并算法参照 EFMI-Tools（SpectrumQT）参考插件：
- `migoto_object_builder.get_skeleton_data`：取顶点阶段 num_constants==4096 的常量缓冲
  （instance config），其 first_constant 窗口第 6 个 float4 的 xy 分量（uint32 位型）为骨骼矩阵段偏移；
  骨骼矩阵存于 vs-t0（compute 蒙皮输出 u0 共享），每骨骼 12 floats（4x3 矩阵）。
- `build_merged_skeleton_vg_map`：跨子网格按骨骼矩阵内容去重，构建 local->global 的
  vg_map / vg_offset / vg_count。

产出：
- 每个子网格 json 写回 `VGMap` / `VGOffset` / `VGCount`（缓存，幂等；提取端重导可覆盖）。
- 骨骼池 buffer 复制到 `<submesh>/ModImpRuntime/<bare>-BoneMatrix.buf`（参照 NTEMI 缓存模式）。
"""

import os
import re
import shutil
import numpy

from ..utils.json_utils import JsonUtils

# 每骨骼矩阵的 float 数（4x3）
_BONE_MATRIX_FLOATS = 12
# 每个骨骼段的 float4 数（256 骨骼 x 3 float4）
_BONE_SEGMENT_FLOAT4 = 256 * 3
# instance config 中骨骼段偏移所在的 float4 行（第 6 行）
_INSTANCE_CONFIG_BONE_OFFSET_ROW = 5


class EFMILogParser:
    """解析 FrameAnalysis/log.txt，提供 draw 调用与资源绑定查询。"""

    _DRAW_PREFIX_RE = re.compile(r"^(\d{6}) (.*)$")
    _DUMP_RE = re.compile(r"^3DMigoto Dumping Buffer (.+) -> (.+)$")
    _IB_FILE_RE = re.compile(r"^(\d{6})-ib=([0-9a-f]{8})")
    _CB_BIND_RE = re.compile(
        r"^(\d+): resource=0x[0-9A-Fa-f]+ hash=([0-9a-f]{8}) "
        r"first_constant=(\d+) num_constants=(\d+)$"
    )
    _SRV_BIND_RE = re.compile(
        r"^(\d+): view=0x[0-9A-Fa-f]+ resource=0x[0-9A-Fa-f]+ hash=([0-9a-f]{8})$"
    )
    _DRAW_CALL_RE = re.compile(
        r"^DrawIndexedInstanced\(IndexCountPerInstance:(\d+), InstanceCount:(\d+), "
        r"StartIndexLocation:(\d+), BaseVertexLocation:(\d+), StartInstanceLocation:(\d+)\)$"
    )

    def __init__(self, log_path: str):
        self.log_path = log_path
        self.base_dir = os.path.dirname(os.path.abspath(log_path))
        # draw_index -> DrawCallInfo
        self.draw_calls: dict[str, dict] = {}
        # (draw_index, stage, slot) -> {"hash", "first_constant", "num_constants"}
        self.cb_bindings: dict[tuple[str, str, int], dict] = {}
        # (draw_index, stage, slot) -> {"hash"}
        self.srv_bindings: dict[tuple[str, str, int], dict] = {}
        # 逻辑文件名（根目录 dump 文件名）-> deduped 实际路径
        self.dump_map: dict[str, str] = {}
        self._parse()

    def _parse(self):
        if not os.path.isfile(self.log_path):
            raise FileNotFoundError(f"FrameAnalysis log 不存在: {self.log_path}")

        current_draw = ""
        pending_cb = None  # (stage, slot) 等待下一行资源描述
        pending_srv = None

        with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue

                match = self._DRAW_PREFIX_RE.match(line)
                if not match:
                    # 无 draw 前缀的行：资源描述行（跟随绑定行），在 pending 状态下消费。
                    stripped = line.strip()
                    if pending_cb is not None and stripped:
                        desc = self._CB_BIND_RE.match(stripped)
                        if desc:
                            slot = int(desc.group(1))
                            self.cb_bindings[(pending_cb[0], pending_cb[1], slot)] = {
                                "hash": desc.group(2),
                                "first_constant": int(desc.group(3)),
                                "num_constants": int(desc.group(4)),
                            }
                        pending_cb = None
                        continue
                    if pending_srv is not None and stripped:
                        desc = self._SRV_BIND_RE.match(stripped)
                        if desc:
                            slot = int(desc.group(1))
                            self.srv_bindings[(pending_srv[0], pending_srv[1], slot)] = {
                                "hash": desc.group(2),
                            }
                        pending_srv = None
                        continue
                    continue
                draw_index, payload = match.group(1), match.group(2)

                # 常量缓冲绑定（VSSetConstantBuffers1 等）
                cb_match = re.match(r"^(V|P|C|G|H|D)SSetConstantBuffers1\(StartSlot:(\d+),", payload)
                if cb_match:
                    stage = cb_match.group(1)
                    slot = int(cb_match.group(2))
                    pending_cb = (draw_index, stage, slot)
                    pending_srv = None
                    continue

                # 着色器资源绑定（VSSetShaderResources 等）
                srv_match = re.match(r"^(V|P|C|G|H|D)SSetShaderResources\(StartSlot:(\d+),", payload)
                if srv_match:
                    stage = srv_match.group(1)
                    slot = int(srv_match.group(2))
                    pending_srv = (draw_index, stage, slot)
                    pending_cb = None
                    continue

                # 资源描述行（跟随绑定行，但带 draw 前缀的少见情形）
                if pending_cb is not None:
                    desc = self._CB_BIND_RE.match(payload.strip())
                    if desc:
                        slot = int(desc.group(1))
                        self.cb_bindings[(pending_cb[0], pending_cb[1], slot)] = {
                            "hash": desc.group(2),
                            "first_constant": int(desc.group(3)),
                            "num_constants": int(desc.group(4)),
                        }
                    pending_cb = None
                    continue

                if pending_srv is not None:
                    desc = self._SRV_BIND_RE.match(payload.strip())
                    if desc:
                        slot = int(desc.group(1))
                        self.srv_bindings[(pending_srv[0], pending_srv[1], slot)] = {
                            "hash": desc.group(2),
                        }
                    pending_srv = None
                    continue

                # draw 调用
                draw_match = self._DRAW_CALL_RE.match(payload)
                if draw_match:
                    self.draw_calls[draw_index] = {
                        "index_count": int(draw_match.group(1)),
                        "instance_count": int(draw_match.group(2)),
                        "start_index": int(draw_match.group(3)),
                        "base_vertex": int(draw_match.group(4)),
                        "start_instance": int(draw_match.group(5)),
                    }
                    current_draw = draw_index
                    continue

                # 资源 dump 映射
                dump_match = self._DUMP_RE.match(payload)
                if dump_match:
                    src_name = os.path.basename(dump_match.group(1))
                    dst_path = dump_match.group(2)
                    if src_name not in self.dump_map:
                        self.dump_map[src_name] = dst_path
                    continue

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def get_vs_cb(self, draw_index: str, slot: int) -> dict | None:
        """获取 draw 调用顶点阶段指定槽位的常量缓冲绑定。"""
        return self.cb_bindings.get((draw_index, "V", slot))

    def get_instance_config_cb(self, draw_index: str) -> dict | None:
        """获取 instance config 常量缓冲（num_constants == 4096 的顶点阶段 cb）。"""
        best = None
        for (idx, stage, _slot), binding in self.cb_bindings.items():
            if idx != draw_index or stage != "V":
                continue
            if binding.get("num_constants") == 4096:
                best = binding
                break
        return best

    def get_vs_t0(self, draw_index: str) -> str | None:
        """获取顶点阶段 slot0 纹理（骨骼池）的 hash。"""
        binding = self.srv_bindings.get((draw_index, "V", 0))
        return binding.get("hash") if binding else None

    def get_deduped_path(self, logical_filename: str) -> str | None:
        """按根目录逻辑文件名（如 000015-vs-cb1=...buf）查 deduped 实际路径。

        log.txt 里记录的 deduped 绝对路径可能是提取时的临时缓存路径（如 E:\\SSMT4缓存文件夹\\...），
        dump 被挪动后失效。因此：先按 log 记录的路径，失效时用文件名在 dump 目录的
        deduped/ 子目录兜底定位（deduped 文件名是内容 hash，唯一）。
        """
        dst = self.dump_map.get(logical_filename)
        for candidate in self._deduped_candidates(dst):
            if os.path.isfile(candidate):
                return candidate
        # 兜底：按前缀匹配（同 hash 资源可能对应多个逻辑名）
        prefix = logical_filename.split("=", 1)[0] + "="
        for src, dst2 in self.dump_map.items():
            if src.startswith(prefix):
                for candidate in self._deduped_candidates(dst2):
                    if os.path.isfile(candidate):
                        return candidate
        return None

    def _deduped_candidates(self, dst_path: str | None) -> list[str]:
        """生成 deduped 候选路径：log 记录的原路径 + dump 目录 deduped/ 下的同名文件。"""
        candidates = []
        if dst_path:
            candidates.append(dst_path)
            basename = os.path.basename(dst_path)
            if basename:
                candidates.append(os.path.join(self.base_dir, "deduped", basename))
        return candidates

    def find_drawcalls_by_ib(
        self,
        draw_ib: str,
        index_count: int | None = None,
        first_index: int | None = None,
    ) -> list[str]:
        """按 IB hash（+可选 index_count/first_index）反查 drawcall 索引列表。

        从 dump 文件名（`{idx}-ib=<hash>...`）解析 drawcall -> ib hash 映射，
        再按 DrawIndexedInstanced 的 index_count/first_index 过滤。
        用于 ComponentName_DrawCallIndexList.json 缺失/被重置时的兜底。
        """
        draw_ib = str(draw_ib or "").strip().lower()
        if not draw_ib:
            return []
        candidates = []
        for src in self.dump_map.keys():
            match = self._IB_FILE_RE.match(src)
            if match and match.group(2) == draw_ib:
                candidates.append(match.group(1))
        candidates = sorted(set(candidates))
        if index_count is None and first_index is None:
            return candidates

        matched = []
        for idx in candidates:
            dc = self.draw_calls.get(idx)
            if dc is None:
                continue
            if index_count is not None and dc.get("index_count") != index_count:
                continue
            if first_index is not None and dc.get("start_index") != first_index:
                continue
            matched.append(idx)
        # 精确匹配失败时回退到全部候选（提取切分可能与 draw 参数不完全一致）
        return matched if matched else candidates


class EFMIBoneMapBuilder:
    """构建 EFMI 子网格的骨骼合并映射（vg_map / vg_offset / vg_count）。"""

    def __init__(self, parser: EFMILogParser, blend_formats: dict[str, dict] | None = None):
        self.parser = parser
        # unique_str -> 子网格 Blend 类别解析信息（由调用方提供，避免重复解析 json）
        self.blend_formats = blend_formats or {}

    # ------------------------------------------------------------------
    # 子网格 blendindices 读取
    # ------------------------------------------------------------------

    @staticmethod
    def parse_blendindices_from_buf(blend_buf_path: str, element_info: dict) -> numpy.ndarray:
        """从工作空间 Blend.buf 读取 BLENDINDICES 局部索引数组。

        element_info: {
            "byte_offset": BLENDINDICES 在顶点行内的字节偏移,
            "byte_width": 元素总字节数,
            "stride": 顶点行总字节数,
            "np_type": numpy dtype 字符串（如 'u1'/'u4'）,
            "component_count": 通道数,
        }
        返回 (vertex_count, component_count) 的 uint32 数组。
        """
        if not os.path.isfile(blend_buf_path):
            raise FileNotFoundError(f"Blend buffer 不存在: {blend_buf_path}")

        stride = int(element_info.get("stride", 0) or 0)
        byte_offset = int(element_info.get("byte_offset", 0) or 0)
        byte_width = int(element_info.get("byte_width", 0) or 0)
        np_type = element_info.get("np_type", "")
        component_count = int(element_info.get("component_count", 4) or 4)
        if stride <= 0 or byte_width <= 0 or not np_type:
            raise ValueError(f"无效的 BLENDINDICES 元素信息: {element_info}")

        file_size = os.path.getsize(blend_buf_path)
        if file_size % stride != 0:
            raise ValueError(
                f"Blend buffer 大小与 stride 不对齐: {blend_buf_path} "
                f"({file_size} % {stride})"
            )
        vertex_count = file_size // stride

        raw = numpy.fromfile(blend_buf_path, dtype=numpy.uint8)
        rows = raw.reshape(vertex_count, stride)
        element_bytes = rows[:, byte_offset:byte_offset + byte_width]
        indices = numpy.frombuffer(
            element_bytes.tobytes(), dtype=numpy.dtype(np_type)
        ).reshape(vertex_count, component_count)
        return indices.astype(numpy.uint32, copy=False)

    @staticmethod
    def compute_driven_centroids(
        position_buf_path: str,
        blend_buf_path: str,
        submesh_json_dict: dict,
    ) -> dict[int, numpy.ndarray]:
        """计算每个局部骨骼驱动的顶点加权质心（双维度去重的"驱动区域"指纹）。

        返回: {local_vg_id(int): 加权质心 numpy.ndarray(3)}（绑定姿态坐标）。
        """
        signatures = EFMIBoneMapBuilder.compute_driven_signatures(
            position_buf_path, blend_buf_path, submesh_json_dict
        )
        return {
            local: sig["centroid"] for local, sig in signatures.items()
        }

    @staticmethod
    def compute_driven_signatures(
        position_buf_path: str,
        blend_buf_path: str,
        submesh_json_dict: dict,
    ) -> dict[int, dict]:
        """计算每个局部骨骼的"驱动签名"（三维度去重用）。

        返回 {local_vg_id(int): {
            "centroid": 加权质心(3,),
            "bbox_min": 包围盒最小(3,),
            "bbox_max": 包围盒最大(3,),
            "vertex_count": 驱动顶点数,
        }}（绑定姿态坐标）。

        原理（用户提出）：骨骼的可靠标识 = 蒙皮矩阵 + 骨骼标签 + 驱动的顶点组点云
        （包围盒是否重叠 + 权重聚类核心位置）。同一骨骼跨部件驱动同区域顶点
        （包围盒重叠、质心接近）；不同骨骼即使矩阵相同，其驱动区域也不同
        （包围盒不重叠或质心远离）。
        无 BLENDWEIGHTS 元素的类型按"每顶点第一索引权重=1"处理。
        """
        empty = {}
        if not os.path.isfile(position_buf_path) or not os.path.isfile(blend_buf_path):
            return empty

        # ---- Position 布局（Category=Position 元素，POSITION 在 offset 0，float32 x3）----
        pos_stride = 0
        has_position = False
        for category_buffer in submesh_json_dict.get("CategoryBufferList", []):
            for element in category_buffer.get("D3D11ElementList", []):
                if str(element.get("Category", "") or "").strip().lower() == "position":
                    pos_stride += int(element.get("ByteWidth", 0) or 0)
                    if str(element.get("SemanticName", "") or "").upper() == "POSITION":
                        has_position = True
        if pos_stride <= 0 or not has_position:
            return empty

        pos_raw = numpy.fromfile(position_buf_path, dtype=numpy.uint8)
        vertex_count = len(pos_raw) // pos_stride
        if vertex_count <= 0:
            return empty
        positions = (
            pos_raw.reshape(vertex_count, pos_stride)[:, 0:12]
            .copy().view(numpy.float32).reshape(vertex_count, 3)
        )

        # ---- Blend 布局（BLENDINDICES + BLENDWEIGHTS offset/格式）----
        blend_stride = 0
        bi_offset = None
        bi_np = None
        bw_offset = None
        bw_np = None
        bw_div = 1.0
        for category_buffer in submesh_json_dict.get("CategoryBufferList", []):
            elements = category_buffer.get("D3D11ElementList", [])
            is_blend = any(
                str(e.get("Category", "") or "").strip().lower() == "blend"
                for e in elements
            )
            if not is_blend:
                continue
            off = 0
            for element in elements:
                width = int(element.get("ByteWidth", 0) or 0)
                blend_stride += width
                semantic = str(element.get("SemanticName", "") or "").upper()
                fmt = str(element.get("Format", "") or "").upper()
                if semantic == "BLENDINDICES":
                    bi_offset = off
                    bi_np = {
                        "R8G8B8A8_UINT": ("u1", 4), "R8_UINT": ("u1", 1),
                        "R16G16B16A16_UINT": ("u2", 4), "R16_UINT": ("u2", 1),
                        "R32G32B32A32_UINT": ("u4", 4), "R32_UINT": ("u4", 1),
                        "R32G32B32A32_SINT": ("i4", 4),
                    }.get(fmt)
                elif semantic.startswith("BLENDWEIGHT"):
                    bw_offset = off
                    if fmt == "R16G16B16A16_UNORM":
                        bw_np, bw_div = ("u2", 4), 65535.0
                    elif fmt == "R32G32B32A32_FLOAT":
                        bw_np, bw_div = ("f4", 4), 1.0
                    elif fmt == "R32G32_FLOAT":
                        bw_np, bw_div = ("f4", 2), 1.0
                    elif fmt == "R8G8B8A8_UNORM":
                        bw_np, bw_div = ("u1", 4), 255.0
                off += width
            break

        if blend_stride <= 0 or bi_offset is None or not bi_np:
            return empty

        blend_raw = numpy.fromfile(blend_buf_path, dtype=numpy.uint8)
        n = len(blend_raw) // blend_stride
        if n != vertex_count:
            return empty
        rows = blend_raw.reshape(n, blend_stride)

        bi_np_type, bi_channels = bi_np
        indices = numpy.frombuffer(
            rows[:, bi_offset:bi_offset + (bi_channels * numpy.dtype(bi_np_type).itemsize)].tobytes(),
            dtype=numpy.dtype(bi_np_type),
        ).reshape(n, bi_channels).astype(numpy.int64)

        if bw_offset is not None and bw_np:
            bw_np_type, bw_channels = bw_np
            weights = numpy.frombuffer(
                rows[:, bw_offset:bw_offset + (bw_channels * numpy.dtype(bw_np_type).itemsize)].tobytes(),
                dtype=numpy.dtype(bw_np_type),
            ).reshape(n, bw_channels).astype(numpy.float32) / bw_div
        else:
            weights = numpy.zeros((n, bi_channels), dtype=numpy.float32)
            weights[:, 0] = 1.0

        # ---- 每 local 的驱动顶点集合（用于质心 + 包围盒）----
        accum: dict[int, dict] = {}
        for c in range(indices.shape[1]):
            idx_col = indices[:, c]
            w_col = weights[:, c] if c < weights.shape[1] else weights[:, 0]
            valid = (w_col > 0) & (idx_col >= 0) & (idx_col != 0xFFFF)
            if not numpy.any(valid):
                continue
            v_idx = idx_col[valid]
            v_w = w_col[valid].astype(numpy.float64)
            v_pos = positions[valid].astype(numpy.float64)
            for local in numpy.unique(v_idx):
                mask = v_idx == local
                pts = v_pos[mask]
                ws_ = v_w[mask]
                w_sum = float(ws_.sum())
                if w_sum <= 0:
                    continue
                centroid = (pts * ws_[:, None]).sum(axis=0) / w_sum
                entry = accum.setdefault(int(local), {
                    "weighted_sum": numpy.zeros(3, dtype=numpy.float64),
                    "weight_total": 0.0,
                    "bbox_min": numpy.full(3, numpy.inf),
                    "bbox_max": numpy.full(3, -numpy.inf),
                    "vertex_count": 0,
                })
                entry["weighted_sum"] += centroid * w_sum
                entry["weight_total"] += w_sum
                entry["bbox_min"] = numpy.minimum(entry["bbox_min"], pts.min(axis=0))
                entry["bbox_max"] = numpy.maximum(entry["bbox_max"], pts.max(axis=0))
                entry["vertex_count"] += int(mask.sum())

        return {
            local: {
                "centroid": (e["weighted_sum"] / e["weight_total"]).astype(numpy.float32),
                "bbox_min": e["bbox_min"].astype(numpy.float32),
                "bbox_max": e["bbox_max"].astype(numpy.float32),
                "vertex_count": e["vertex_count"],
            }
            for local, e in accum.items()
            if e["weight_total"] > 0
        }

    # ------------------------------------------------------------------
    # 骨骼矩阵读取
    # ------------------------------------------------------------------

    def get_skeleton_buffer(self, draw_index: str) -> numpy.ndarray | None:
        """读取 draw 调用对应组件骨骼段矩阵数组（(N, 12) floats）。

        流程（对齐参考插件）：
        1. instance config cb（num_constants==4096）→ first_constant 窗口 16 float4
           → 第 6 个 float4 的 xy（uint32 位型）= 骨骼段偏移（float4 单位）；
        2. vs-t0（骨骼池）按偏移取 256*3 float4 → reshape(-1, 12)。
        """
        instance_cb = self.parser.get_instance_config_cb(draw_index)
        if instance_cb is None:
            return None

        first_constant = int(instance_cb.get("first_constant", 0) or 0)
        cb_hash = instance_cb.get("hash", "")

        cb_logical = f"{draw_index}-vs-cb{self._find_cb_slot(draw_index, cb_hash)}={cb_hash}"
        cb_path = self.parser.get_deduped_path(cb_logical)
        if not cb_path:
            return None

        try:
            cb_data = numpy.fromfile(cb_path, dtype=numpy.float32).reshape(-1, 4)
        except Exception:
            return None

        if first_constant + 16 > len(cb_data):
            return None
        instance_config = cb_data[first_constant:first_constant + 16]
        skeleton_offsets = instance_config[_INSTANCE_CONFIG_BONE_OFFSET_ROW][0:2].view(numpy.uint32)

        skeleton_t0_hash = self.parser.get_vs_t0(draw_index)
        if not skeleton_t0_hash:
            return None
        pool_logical = f"{draw_index}-vs-t0={skeleton_t0_hash}"
        pool_path = self.parser.get_deduped_path(pool_logical)
        if not pool_path:
            return None

        try:
            pool_data = numpy.fromfile(pool_path, dtype=numpy.float32).reshape(-1, 4)
        except Exception:
            return None

        # 取第一个非零骨骼段（current 主骨骼段，单帧）
        for offset_value in skeleton_offsets:
            offset = int(offset_value)
            if not offset:
                continue
            data_offset = offset + 3  # GLOBAL_RESERVED_ROWS = 3
            skeleton_raw = pool_data[data_offset:data_offset + _BONE_SEGMENT_FLOAT4]
            usable = (len(skeleton_raw) // 3) * 3
            if usable == 0:
                continue
            skeleton = skeleton_raw[:usable].reshape(-1, _BONE_MATRIX_FLOATS)
            return skeleton
        return None

    def _find_cb_slot(self, draw_index: str, cb_hash: str) -> int:
        for (idx, stage, slot), binding in self.parser.cb_bindings.items():
            if idx == draw_index and stage == "V" and binding.get("hash") == cb_hash:
                return slot
        return 0

    # ------------------------------------------------------------------
    # 跨子网格 vg_map 构建
    # ------------------------------------------------------------------

    @staticmethod
    def build_vg_maps(
        submesh_skeletons: dict[str, tuple],
        match_tolerance: float = 0.0,
        tight_matrix_tolerance: float = 1e-5,
        centroid_tolerance: float = 0.03,
    ) -> dict[str, dict]:
        """跨子网格按骨骼矩阵**字节级（bitwise）判等**去重构建 vg_map（ZZMI 风格）。

        参数:
            unique_str -> (skeleton_buffer, vg_count, weighted_vertex_counts[, signatures])
        返回: (vg_maps, vg_offsets)
            - vg_maps: unique_str -> {local_vg_id(int): global_vg_id(int)}
            - vg_offsets: unique_str -> 该子网格在合并骨架中的起始槽位（与去重同序分配）

        规则（对齐 ZZMI `zzmi_skeleton.py` 实测效果很好的方案，用户拍板采用）：
        - **同一子网格（同部件）内部绝不去重**（同 key 只来自更靠前的其它部件才合并）；
        - **跨部件严格 bitwise（numpy tobytes 字节级）判等，禁用浮点容差**；
        - canonical 槽位 = 按 unique_str 排序后首次出现处的槽位（确定性分配）；
        - 合并语义 = "按矩阵内容去重 + 拼接对齐"（相同对齐、不同拼接、组成大骨架）。

        与之前各方案的对比（实测结论）：
        - 精确匹配（array_equal）≈ bitwise，但本实现直接 tobytes 判等更明确；
        - 容差会误并"同位置功能骨骼"（手指两段差1.79e-07），点云会误拆"同骨骼跨部件
          驱动区域不同"，两帧签名在无多帧数据时不现实——故回到单帧 bitwise + 同部件不去重。

        match_tolerance / tight_matrix_tolerance / centroid_tolerance: 保留参数（历史
        方案遗留），当前 bitwise 实现不使用。
        """
        # bone_key(bytes) -> (unique_str, global_slot)
        slot_of: dict[bytes, tuple[str, int]] = {}
        vg_maps: dict[str, dict] = {}
        vg_offsets: dict[str, int] = {}
        offset = 0

        for unique_str in sorted(submesh_skeletons.keys()):
            entry = submesh_skeletons[unique_str]
            skeleton_buffer = entry[0]
            vg_count = entry[1]

            if skeleton_buffer is None or vg_count <= 0:
                continue
            if len(skeleton_buffer) < vg_count:
                print(
                    f"[EFMI骨骼合并] 警告: {unique_str} 骨骼段仅 {len(skeleton_buffer)} 根骨骼，"
                    f"但声明了 {vg_count} 个顶点组，跳过该子网格参与合并。"
                )
                continue

            vg_offsets[unique_str] = offset
            vg_map: dict[int, int] = {}
            for local_id in range(vg_count):
                bone = skeleton_buffer[local_id]
                # 全零骨骼（无效数据）：占本部件自己的槽位，不参与跨部件合并
                if numpy.all(bone == 0):
                    vg_map[local_id] = offset + local_id
                    continue

                bone_key = bone.tobytes()
                hit = slot_of.get(bone_key)
                if hit is not None and hit[0] != unique_str:
                    # 跨部件命中：合并到首次出现部件的 canonical 槽位
                    vg_map[local_id] = hit[1]
                else:
                    # 新骨骼（或同部件重复，不合并）：占用本部件自己的槽位
                    slot_of[bone_key] = (unique_str, offset + local_id)
                    vg_map[local_id] = offset + local_id

            vg_maps[unique_str] = vg_map
            offset += vg_count

        return vg_maps, vg_offsets


class EFMISkeletonMergeHelper:
    """EFMI 骨骼合并总流程：定位 FrameAnalysis -> 解析 log -> 构建映射 -> 写回工作空间。"""

    @staticmethod
    def resolve_frame_analysis_dir(workspace_root: str) -> str:
        """定位 FrameAnalysis 目录（多候选回退）。

        顺序：
        1. `Config/FrameAnalysisPath.json` 的 frameAnalysisFolderPath（isdir 校验）；
        2. `Config/Tabs/ws-tab-*.json` 的同名字段；
        3. 当前游戏 migoto 目录（GlobalConfig.current_game_migoto_folder）下
           mtime 最新的 `FrameAnalysis-*` 目录（工作空间记录的 dump 被删/挪动后的兜底）。
        """
        def _valid(path: str) -> str:
            path = str(path or "").strip()
            return path if path and os.path.isdir(path) else ""

        config_path = os.path.join(workspace_root, "Config", "FrameAnalysisPath.json")
        if os.path.isfile(config_path):
            try:
                payload = JsonUtils.LoadFromFile(config_path)
                found = _valid(payload.get("frameAnalysisFolderPath", ""))
                if found:
                    return found
            except Exception:
                pass

        tabs_dir = os.path.join(workspace_root, "Config", "Tabs")
        if os.path.isdir(tabs_dir):
            for tab_file in sorted(os.listdir(tabs_dir)):
                if not tab_file.startswith("ws-tab-") or not tab_file.endswith(".json"):
                    continue
                try:
                    payload = JsonUtils.LoadFromFile(os.path.join(tabs_dir, tab_file))
                    found = _valid(payload.get("frameAnalysisFolderPath", ""))
                    if found:
                        return found
                except Exception:
                    continue

        # 兜底：当前游戏 migoto 目录下最新的 FrameAnalysis-*
        try:
            from .global_config import GlobalConfig
            migoto_folder = str(getattr(GlobalConfig, "current_game_migoto_folder", "") or "").strip()
            if migoto_folder and os.path.isdir(migoto_folder):
                candidates = []
                for entry in os.scandir(migoto_folder):
                    if entry.is_dir() and entry.name.startswith("FrameAnalysis-"):
                        try:
                            candidates.append((entry.stat().st_mtime, entry.path))
                        except Exception:
                            continue
                if candidates:
                    candidates.sort(key=lambda item: item[0], reverse=True)
                    return candidates[0][1]
        except Exception:
            pass
        return ""

    @staticmethod
    def load_drawcall_index_list(lod_dir: str) -> dict[str, list[str]]:
        """读取 ComponentName_DrawCallIndexList.json（子网格名 -> drawcall 索引列表）。"""
        path = os.path.join(lod_dir, "ComponentName_DrawCallIndexList.json")
        if not os.path.isfile(path):
            return {}
        try:
            payload = JsonUtils.LoadFromFile(path)
            if isinstance(payload, dict):
                return {
                    str(k): [str(v) for v in (val if isinstance(val, list) else [])]
                    for k, val in payload.items()
                }
        except Exception:
            pass
        return {}

    @staticmethod
    def parse_blend_element_info(submesh_json_dict: dict) -> dict | None:
        """从子网格 json 的 CategoryBufferList 解析 Blend 类别 BLENDINDICES 元素信息。

        工作空间 json 的 CategoryBufferList 没有顶层 Category 字段，
        类别由每个元素的 Category 字段决定；Blend buffer 文件名形如 <name>-Blend.buf。
        """
        for category_buffer in submesh_json_dict.get("CategoryBufferList", []):
            elements = category_buffer.get("D3D11ElementList", [])
            if not elements:
                continue
            # 判定 Blend 类别：元素列表中任一元素 Category == "Blend"
            is_blend = any(
                str(element.get("Category", "") or "").strip().lower() == "blend"
                for element in elements
            )
            if not is_blend:
                continue

            stride = 0
            for element in elements:
                stride += int(element.get("ByteWidth", 0) or 0)
            if stride <= 0:
                return None

            byte_offset = 0
            for element in elements:
                element_width = int(element.get("ByteWidth", 0) or 0)
                semantic_name = str(element.get("SemanticName", "") or "").upper()
                if semantic_name == "BLENDINDICES":
                    fmt = str(element.get("Format", "") or "").upper()
                    np_type, component_count = EFMISkeletonMergeHelper._blend_indices_layout(fmt)
                    if not np_type:
                        return None
                    return {
                        "byte_offset": byte_offset,
                        "byte_width": element_width,
                        "stride": stride,
                        "np_type": np_type,
                        "component_count": component_count,
                    }
                byte_offset += element_width
        return None

    @staticmethod
    def _blend_indices_layout(fmt: str) -> tuple[str, int]:
        """BLENDINDICES 格式 -> (numpy dtype, 通道数)。"""
        layout = {
            "R8G8B8A8_UINT": ("u1", 4),
            "R8_UINT": ("u1", 1),
            "R16G16B16A16_UINT": ("u2", 4),
            "R16G16_UINT": ("u2", 2),
            "R16_UINT": ("u2", 1),
            "R32G32B32A32_UINT": ("u4", 4),
            "R32G32_UINT": ("u4", 2),
            "R32_UINT": ("u4", 1),
            "R32G32B32A32_SINT": ("i4", 4),
            "R32G32_SINT": ("i4", 2),
            "R32_SINT": ("i4", 1),
        }
        return layout.get(fmt, (None, 0))

    @staticmethod
    def _resolve_submesh_json_path(workspace_root: str, unique_str: str) -> str:
        """定位子网格 json（不依赖 bpy/submesh_metadata）。

        规则（与 check_and_get_submesh_json_path 对齐，但无 bpy 依赖）：
        1. Import.json 记录了 unique_str 的数据类型 → `<子网格>/TYPE_<gametype>/<bare>.json`；
        2. 否则：子网格目录下只有一个 TYPE_ 目录含 json 时直接用；多个则拒绝（返回空）。
        unique_str 形如 `LOD0.<drawib>-<n>-<i>` 或 `<drawib>-<n>-<i>`。
        """
        lod_name = ""
        bare = unique_str
        if "." in unique_str and unique_str.split(".", 1)[0].upper().startswith("LOD"):
            lod_name, bare = unique_str.split(".", 1)

        base = workspace_root
        if lod_name:
            base = os.path.join(base, lod_name)
        submesh_dir = os.path.join(base, bare)
        if not os.path.isdir(submesh_dir):
            # 分区工作空间兜底（不含 LOD 前缀，或带 Config.json 的分区）
            return ""

        import_json_path = os.path.join(workspace_root, "Import.json")
        import_json = JsonUtils.LoadFromFile(import_json_path) if os.path.isfile(import_json_path) else {}
        gametype = import_json.get(unique_str, "")

        if gametype:
            candidate = os.path.join(submesh_dir, "TYPE_" + gametype, bare + ".json")
            if os.path.isfile(candidate):
                return candidate

        found = []
        for dirname in os.listdir(submesh_dir):
            if not dirname.startswith("TYPE_"):
                continue
            candidate = os.path.join(submesh_dir, dirname, bare + ".json")
            if os.path.isfile(candidate):
                found.append(candidate)
        if len(found) == 1:
            return found[0]
        return ""

    @classmethod
    def ensure_skeleton_data(
        cls,
        workspace_root: str,
        unique_str_list: list[str],
        force: bool = False,
    ) -> tuple[bool, str]:
        """为 EFMI 工作空间的子网格生成并写回骨骼合并数据（幂等）。

        返回 (是否成功, 描述)。
        """
        if not unique_str_list:
            return False, "没有子网格需要处理。"

        frame_analysis_dir = cls.resolve_frame_analysis_dir(workspace_root)
        if not frame_analysis_dir:
            return False, (
                "未找到 FrameAnalysis 目录：请检查工作空间 "
                f"{os.path.join(workspace_root, 'Config', 'FrameAnalysisPath.json')}"
            )

        log_path = os.path.join(frame_analysis_dir, "log.txt")
        if not os.path.isfile(log_path):
            return False, f"FrameAnalysis 缺少 log.txt: {log_path}"

        parser = EFMILogParser(log_path)

        # 收集每个子网格的信息
        submesh_skeletons: dict[str, tuple] = {}
        submesh_meta: dict[str, dict] = {}
        submesh_json_paths: dict[str, str] = {}
        skipped = 0

        for unique_str in unique_str_list:
            json_path = cls._resolve_submesh_json_path(workspace_root, unique_str)
            if not json_path:
                print(f"[EFMI骨骼合并] 跳过 {unique_str}: 未找到子网格 json")
                continue

            submesh_json = JsonUtils.LoadFromFile(json_path)
            if not isinstance(submesh_json, dict):
                continue

            # 幂等：已有 VGMap 且非强制
            existing_vg_map = submesh_json.get("VGMap")
            if existing_vg_map and not force:
                skipped += 1
                continue

            element_info = cls.parse_blend_element_info(submesh_json)
            if element_info is None:
                print(f"[EFMI骨骼合并] 跳过 {unique_str}: Blend 类别缺少 BLENDINDICES 元素")
                continue

            bare_name = os.path.splitext(os.path.basename(json_path))[0]
            submesh_dir = os.path.dirname(os.path.dirname(json_path))  # TYPE_ 的上一级 = 子网格目录
            blend_buf_path = os.path.join(os.path.dirname(json_path), bare_name + "-Blend.buf")

            # 子网格 -> drawcall 索引（优先角色级映射，其次 LOD 目录映射，最后 dump 反查兜底）
            drawcall_index_list: list[str] = []
            lod_dir = os.path.dirname(submesh_dir)
            role_mapping = cls.load_drawcall_index_list(lod_dir)
            drawcall_index_list = role_mapping.get(os.path.basename(submesh_dir), [])
            if not drawcall_index_list:
                role_mapping_root = cls.load_drawcall_index_list(workspace_root)
                drawcall_index_list = role_mapping_root.get(os.path.basename(submesh_dir), [])

            # 兜底：ComponentName 映射缺失/被重置时，从 dump 按 ib hash + index_count + first_index 反查
            if not drawcall_index_list:
                submesh_name = os.path.basename(submesh_dir)
                parts = submesh_name.split("-")
                if len(parts) >= 3:
                    try:
                        draw_ib = parts[0]
                        index_count = int(parts[1])
                        first_index = int(parts[2])
                        drawcall_index_list = parser.find_drawcalls_by_ib(
                            draw_ib, index_count, first_index
                        )
                        if drawcall_index_list:
                            print(
                                f"[EFMI骨骼合并] {unique_str}: ComponentName 缺失，"
                                f"已从 dump 反查 drawcall {drawcall_index_list[:3]}"
                            )
                    except (ValueError, IndexError):
                        pass

            if not drawcall_index_list:
                print(f"[EFMI骨骼合并] 跳过 {unique_str}: 未找到 drawcall 映射")
                continue

            # 取第一个有有效骨骼数据的 drawcall
            skeleton_buffer = None
            for draw_index in drawcall_index_list:
                skeleton_buffer = EFMIBoneMapBuilder(parser).get_skeleton_buffer(draw_index)
                if skeleton_buffer is not None:
                    break

            if skeleton_buffer is None:
                print(f"[EFMI骨骼合并] 跳过 {unique_str}: 无法从 FrameAnalysis 读取骨骼数据")
                continue

            try:
                blend_indices = EFMIBoneMapBuilder.parse_blendindices_from_buf(blend_buf_path, element_info)
            except Exception as e:
                print(f"[EFMI骨骼合并] 跳过 {unique_str}: 读取 Blend.buf 失败: {e}")
                continue

            local_indices = blend_indices.ravel()
            valid_indices = local_indices[local_indices != 0xFFFF]
            if len(valid_indices) == 0:
                print(f"[EFMI骨骼合并] 跳过 {unique_str}: BLENDINDICES 全为空")
                continue
            vg_count = int(valid_indices.max()) + 1
            weighted_vertex_counts = numpy.bincount(valid_indices, minlength=vg_count)

            if len(skeleton_buffer) < vg_count:
                print(
                    f"[EFMI骨骼合并] 跳过 {unique_str}: 骨骼段 {len(skeleton_buffer)} < 顶点组 {vg_count}"
                )
                continue

            # 三维度去重的"驱动签名"（读 Position.buf + Blend.buf 计算每骨骼驱动点云特征）
            position_buf_path = os.path.join(os.path.dirname(json_path), bare_name + "-Position.buf")
            try:
                centroids = EFMIBoneMapBuilder.compute_driven_signatures(
                    position_buf_path, blend_buf_path, submesh_json
                )
            except Exception as e:
                print(f"[EFMI骨骼合并] {unique_str}: 驱动签名计算失败（退化为纯矩阵匹配）: {e}")
                centroids = {}

            submesh_skeletons[unique_str] = (skeleton_buffer, vg_count, weighted_vertex_counts, centroids)
            submesh_meta[unique_str] = {
                "json_path": json_path,
                "submesh_dir": submesh_dir,
                "bare_name": bare_name,
                "draw_index": drawcall_index_list[0],
            }
            submesh_json_paths[unique_str] = json_path

        if not submesh_skeletons:
            if skipped > 0:
                return True, f"全部 {skipped} 个子网格已有骨骼合并缓存（VGMap），无需重新生成。"
            return False, "没有子网格成功生成骨骼数据。"

        # 跨子网格去重构建 vg_map（返回 vg_offsets，与去重同序分配，保证一致）
        vg_maps, vg_offsets = EFMIBoneMapBuilder.build_vg_maps(submesh_skeletons)

        # 写回工作空间 json + 复制骨骼池缓存
        written = 0
        for unique_str, entry in submesh_skeletons.items():
            skeleton_buffer = entry[0]
            vg_count = entry[1]
            meta = submesh_meta[unique_str]
            json_path = meta["json_path"]
            submesh_json = JsonUtils.LoadFromFile(json_path)
            if not isinstance(submesh_json, dict):
                continue

            vg_map = vg_maps.get(unique_str, {})
            if not vg_map:
                continue

            # VGOffset = 该子网格在全局骨架中的起始（取去重时分配的槽位，保证与 vg_map 一致）
            vg_offset = vg_offsets.get(unique_str, 0)

            submesh_json["VGCount"] = vg_count
            submesh_json["VGOffset"] = vg_offset
            submesh_json["VGMap"] = {str(k): int(v) for k, v in sorted(vg_map.items())}

            # 复制骨骼池 buffer 到 ModImpRuntime 缓存（复用 NTEMI 缓存模式）
            try:
                pool_path = cls._resolve_skeleton_pool_path(parser, meta["draw_index"])
                if pool_path:
                    runtime_dir = os.path.join(meta["submesh_dir"], "ModImpRuntime")
                    os.makedirs(runtime_dir, exist_ok=True)
                    dest_name = f"{meta['bare_name']}-BoneMatrix.buf"
                    dest_path = os.path.join(runtime_dir, dest_name)
                    if not os.path.isfile(dest_path) or force:
                        shutil.copy2(pool_path, dest_path)
                    submesh_json["BoneMatrixFileName"] = dest_name
            except Exception as e:
                print(f"[EFMI骨骼合并] 复制骨骼池缓存失败 {unique_str}: {e}")

            try:
                JsonUtils.SaveToFile(json_dict=submesh_json, filepath=json_path)
                written += 1
            except Exception as e:
                print(f"[EFMI骨骼合并] 写回 json 失败 {unique_str}: {e}")

        return written > 0, (
            f"已为 {written} 个子网格生成骨骼合并数据"
            + (f"（跳过已缓存 {skipped} 个）" if skipped else "")
        )

    @classmethod
    def _resolve_skeleton_pool_path(cls, parser: EFMILogParser, draw_index: str) -> str | None:
        skeleton_t0_hash = parser.get_vs_t0(draw_index)
        if not skeleton_t0_hash:
            return None
        pool_logical = f"{draw_index}-vs-t0={skeleton_t0_hash}"
        return parser.get_deduped_path(pool_logical)
