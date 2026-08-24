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
- `build_merged_skeleton_vg_map`：跨子网格按骨骼矩阵内容和接触位置权重扩散去重，
  构建 local->global 的 vg_map / vg_offset / vg_count（矩阵硬门控 + 扩散确认，
  _DEDUP_ENABLED 控制总开关）。

产出：
- 每个子网格 json 写回 `VGMap` / `VGOffset` / `VGCount`（缓存，幂等；提取端重导可覆盖）。
- 骨骼池 buffer 复制到 `<submesh>/ModImpRuntime/<bare>-BoneMatrix.buf`（参照 NTEMI 缓存模式）。

多 LOD：LOD0 / LOD1 用**自己**的 FrameAnalysis dump 提取目录（Config/WorkPageTabs.json
的 tab 名即 LOD 名，每个 tab 的 Config/Tabs/<tabid>.json 记录自己的路径）读取原始
候选；先用部件加权点云一对一配对，再在部件内部按局部权重中心建立对应。默认只在
LOD0 执行权重扩散去重，LOD1 将该分区投影到自己的原始槽位；关闭分组投影选项时，
两侧都保留同一份部件对应账本但各自独立去重。LOD0 是基准；LOD1 的有效对应组数
不得少于 LOD0，额外细分允许存在。导出端仍按 LOD 生成多套独立合并骨架配置，JSON
另写入 EFMILODCorrespondence 账本供复核。
"""

# 注解延迟求值（PEP 563）：避免类定义期解析 numpy.ndarray 等注解，
# 防止在 sys.modules['numpy'] 被 stub 的进程（如全量 unittest discover）中导入失败。
from __future__ import annotations

import os
import re
import shutil
import tempfile
import numpy

from ..utils.json_utils import JsonUtils

# 每骨骼矩阵的 float 数（4x3）
_BONE_MATRIX_FLOATS = 12
# 每个骨骼段的 float4 数（256 骨骼 x 3 float4）
_BONE_SEGMENT_FLOAT4 = 256 * 3
# instance config 中骨骼段偏移所在的 float4 行（第 6 行）
_INSTANCE_CONFIG_BONE_OFFSET_ROW = 5
# 写回 json 的算法版本。旧版只保存 VGMap，没有扩散采样判据，不能继续
# 作为当前策略的幂等缓存使用；版本不匹配时 ensure_skeleton_data 自动重算。
# v14：在 v13 的层级兼容最近点和 Component 对内一对一动态收紧基础上，
# 全局并入顺序也按矩阵差优先；矩阵歧义只按 1e-3 → 1e-4 → 1e-5 → 1e-6
# 有限级联，不进入 1e-7。
_VG_MAP_ALGORITHM_VERSION = 14
_MATRIX_AMBIGUITY_FLOOR = 1e-6

# 跨 LOD 原始候选对应层版本。它和 VGMap 算法版本分开记录，便于以后只调整
# 对应评分而不误把旧的运行时槽位当成新布局。
_CROSS_LOD_LAYOUT_VERSION = 3

# 跨子网格骨骼去重总开关。
# 分层判据（矩阵硬门控 + 权重扩散确认）：矩阵 diff >= match_tolerance 永不合并；
# bitwise 相同在缺少扩散字段时兼容直接合并；有扩散字段也要通过接触位置
# 权重一致性确认；近似矩阵同样做扩散确认。
# 注意：2026-08 曾实测误判（390/393 案例）而临时整体关闭；后查明当时观测数据
# 被陈旧缓存污染（网格与 json 账本不一致），现已随官方运行时架构重写一并恢复。
# 其后的"多维度投票"判据（几何维度可推翻矩阵不一致）经"测试"工作空间 08-10 dump
# 实测产生 42 组矩阵不可兼容的误并（195 组中），已废止并回到分层判据。
# 再遇误判先查数据一致性（清除 VGMap 缓存重导），再考虑关开关。
# 变更策略或 Position/Blend 数据后，VGMapAlgorithmVersion 会让旧结果自动失效；
# 也可用面板的清理按钮提前删除缓存。
# 权重扩散去重是 EFMI 合并模式的默认行为。关闭只用于诊断/回滚；关闭后
# build_vg_maps 仍返回安全的恒等映射，不会改变原始蒙皮。
_DEDUP_ENABLED = True


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
    def blend_index_sentinel(np_type: str) -> int:
        """BLENDINDICES 数据格式对应的无效通道哨兵值（uint32 空间）。

        解析器把索引统一 astype(uint32)：SINT 的 -1 经无符号转换后同样变成全 1
        位（0xFFFFFFFF），因此 i4 与 u4 共用同一哨兵。
        """
        np_type = str(np_type or "").strip().lower()
        if np_type == "u1":
            return 0xFF
        if np_type == "u2":
            return 0xFFFF
        return 0xFFFFFFFF  # u4 / i4

    @staticmethod
    def parse_blend_layout(submesh_json_dict: dict) -> dict | None:
        """解析 Blend 类别的 BLENDINDICES + BLENDWEIGHTS 布局。

        返回 {
            "stride": 顶点行总字节数,
            "bi_offset": BLENDINDICES 字节偏移,
            "bi_np": numpy dtype 字符串（'u1'/'u2'/'u4'/'i4'）,
            "bi_channels": 索引通道数,
            "bw_offset": BLENDWEIGHTS 字节偏移（无权重元素时为 None）,
            "bw_np": 权重 dtype（无权重元素时为 None）,
            "bw_channels": 权重通道数,
            "bw_div": 权重归一化除数,
        }；无法解析返回 None。
        """
        blend_stride = 0
        bi_offset = None
        bi_layout = None
        bw_offset = None
        bw_layout = None
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
                    bi_layout = EFMIBoneMapBuilder._blend_index_layout_entry(fmt)
                elif semantic.startswith("BLENDWEIGHT"):
                    bw_offset = off
                    if fmt == "R16G16B16A16_UNORM":
                        bw_layout, bw_div = ("u2", 4), 65535.0
                    elif fmt == "R32G32B32A32_FLOAT":
                        bw_layout, bw_div = ("f4", 4), 1.0
                    elif fmt == "R32G32_FLOAT":
                        bw_layout, bw_div = ("f4", 2), 1.0
                    elif fmt == "R8G8B8A8_UNORM":
                        bw_layout, bw_div = ("u1", 4), 255.0
                off += width
            break

        if blend_stride <= 0 or bi_offset is None or not bi_layout:
            return None
        return {
            "stride": blend_stride,
            "bi_offset": bi_offset,
            "bi_np": bi_layout[0],
            "bi_channels": bi_layout[1],
            "bw_offset": bw_offset,
            "bw_np": bw_layout[0] if bw_layout else None,
            "bw_channels": bw_layout[1] if bw_layout else 0,
            "bw_div": bw_div,
        }

    @staticmethod
    def _blend_index_layout_entry(fmt: str) -> tuple[str, int] | None:
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
        return layout.get(fmt)

    @staticmethod
    def layout_element_info(blend_layout: dict) -> dict:
        """把 parse_blend_layout 的布局转成 parse_blendindices_from_buf 的元素信息。"""
        return {
            "byte_offset": int(blend_layout["bi_offset"]),
            "byte_width": int(blend_layout["bi_channels"])
            * numpy.dtype(blend_layout["bi_np"]).itemsize,
            "stride": int(blend_layout["stride"]),
            "np_type": blend_layout["bi_np"],
            "component_count": int(blend_layout["bi_channels"]),
        }

    @staticmethod
    def parse_blendweights_from_buf(
        blend_buf_path: str, blend_layout: dict | None
    ) -> numpy.ndarray | None:
        """从 Blend.buf 读取 BLENDWEIGHTS 数组（(vertex_count, bw_channels) float32）。

        blend_layout 由 parse_blend_layout 产出；布局缺失或没有 BLENDWEIGHTS
        元素时返回 None（调用方按“每顶点第一索引权重=1”兜底）。
        """
        if not blend_layout or blend_layout.get("bw_offset") is None or not blend_layout.get("bw_np"):
            return None
        if not os.path.isfile(blend_buf_path):
            raise FileNotFoundError(f"Blend buffer 不存在: {blend_buf_path}")

        stride = int(blend_layout["stride"])
        raw = numpy.fromfile(blend_buf_path, dtype=numpy.uint8)
        if len(raw) % stride != 0:
            raise ValueError(
                f"Blend buffer 大小与 stride 不对齐: {blend_buf_path} "
                f"({len(raw)} % {stride})"
            )
        vertex_count = len(raw) // stride
        rows = raw.reshape(vertex_count, stride)
        bw_np, bw_channels = blend_layout["bw_np"], int(blend_layout["bw_channels"])
        bw_byte_width = bw_channels * numpy.dtype(bw_np).itemsize
        weights = numpy.frombuffer(
            rows[:, blend_layout["bw_offset"]:blend_layout["bw_offset"] + bw_byte_width].tobytes(),
            dtype=numpy.dtype(bw_np),
        ).reshape(vertex_count, bw_channels).astype(numpy.float32) / float(blend_layout["bw_div"])
        return weights

    @staticmethod
    def cache_file_size_ok(cache_path: str, vg_count: int) -> bool:
        """骨骼缓存文件大小合理性：float32 流（4 字节对齐）且 >= vg_count * 48。

        48 字节 = 每骨骼 4x3 float32。EFMI 缓存是整池拷贝（远大于下限），
        ZZMI 缓存是 palette 拷贝（通常等于下限）；被截断/损坏的文件都会
        在这里被判不通过，由写回阶段重新复制。
        """
        try:
            size = os.path.getsize(cache_path)
        except OSError:
            return False
        return size % 4 == 0 and size >= int(vg_count) * _BONE_MATRIX_FLOATS * 4

    @staticmethod
    def valid_blend_channels(
        blend_indices: numpy.ndarray,
        element_info: dict,
        blend_weights: numpy.ndarray | None = None,
    ) -> numpy.ndarray:
        """返回 BLENDINDICES 数组的有效通道布尔掩码（与索引同形状）。

        有效通道 = 索引不是该数据格式的哨兵值（0xFF/0xFFFF/0xFFFFFFFF）且
        对应权重 > 0。没有 BLENDWEIGHTS 元素时按“每顶点第一索引权重=1、其余
        通道权重=0”兜底（与导入侧 mesh_create_helper 的默认权重语义一致）。
        """
        indices = numpy.asarray(blend_indices)
        if indices.ndim == 1:
            indices = indices.reshape(-1, 1)
        np_type = str(element_info.get("np_type", "u4") or "u4").strip().lower()
        sentinel = EFMIBoneMapBuilder.blend_index_sentinel(np_type)
        if blend_weights is not None:
            weights = numpy.asarray(blend_weights, dtype=numpy.float32)
            if weights.ndim == 1:
                weights = weights.reshape(-1, 1)
        else:
            # 无 BLENDWEIGHTS 元素：按“每顶点第一索引权重=1、其余通道=0”兜底
            weights = numpy.zeros((indices.shape[0], indices.shape[1]), dtype=numpy.float32)
            weights[:, 0] = 1.0

        mask = numpy.zeros(indices.shape, dtype=bool)
        for channel in range(indices.shape[1]):
            weight_col = weights[:, channel] if channel < weights.shape[1] else weights[:, 0]
            column = indices[:, channel]
            if np_type == "i4":
                # SINT 经 uint32 转换后，任何负值都落在高位；-1 只是其中一种
                # 无效标记，这里把全部负数回绕值一并过滤。
                index_valid = column < 0x80000000
            else:
                index_valid = column != sentinel
            mask[:, channel] = (
                index_valid
                & (weight_col > 0)
                & numpy.isfinite(weight_col)
            )
        return mask

    @staticmethod
    def compute_driven_centroids(
        position_buf_path: str,
        blend_buf_path: str,
        submesh_json_dict: dict,
    ) -> dict[int, numpy.ndarray]:
        """计算每个局部骨骼的驱动签名和权重扩散采样。

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
        """计算每个局部骨骼的"驱动签名"（质心回退 + 权重扩散确认）。

        返回 {local_vg_id(int): {
            "centroid": 加权质心(3,),
            "bbox_min": 包围盒最小(3,),
            "bbox_max": 包围盒最大(3,),
            "vertex_count": 驱动顶点数,
            "diffusion_points": 正权重扩散采样点,
            "diffusion_weights": 对应原始权重,
            "diffusion_normals": 点云局部 PCA 表面法向（不可判定时为 NaN）
        }}（绑定姿态坐标）。

        原理：同一骨骼跨部件驱动时，绑定姿态空间中的权重扩散场会在接触表面
        保持一致；整体质心可能因为“巨大的平面 + 散落物体”而完全不同，不能
        单独作为判据。不同骨骼即使几何相邻，接触位置的原始权重通常不一致。
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
        # 统一走 parse_blend_layout / parse_blendindices_from_buf /
        # parse_blendweights_from_buf：中央格式表覆盖 R16G16_UINT、R32G32_UINT
        # 等全部解析器支持格式，哨兵按数据格式判定（不再硬编码 0xFFFF）。
        blend_layout = EFMIBoneMapBuilder.parse_blend_layout(submesh_json_dict)
        if not blend_layout:
            return empty
        element_info = EFMIBoneMapBuilder.layout_element_info(blend_layout)
        try:
            blend_indices = EFMIBoneMapBuilder.parse_blendindices_from_buf(
                blend_buf_path, element_info
            )
            blend_weights = EFMIBoneMapBuilder.parse_blendweights_from_buf(
                blend_buf_path, blend_layout
            )
        except Exception:
            return empty
        if len(blend_indices) != vertex_count:
            return empty

        bi_channels = blend_layout["bi_channels"]
        valid_mask = EFMIBoneMapBuilder.valid_blend_channels(
            blend_indices, element_info, blend_weights
        )
        indices = blend_indices.astype(numpy.int64)
        if blend_weights is not None:
            weights = numpy.asarray(blend_weights, dtype=numpy.float32)
        else:
            weights = numpy.zeros((len(blend_indices), bi_channels), dtype=numpy.float32)
            weights[:, 0] = 1.0

        # ---- 每 local 的驱动顶点集合（质心 + 包围盒 + 权重扩散采样）----
        # 采样不是把整组顶点复制到每个候选的临时对象，而是保留正权重
        # 的 (position, weight) 对。build_vg_maps 会在接触位置做最近邻
        # 扩散检测；固定上限保证大型平面不会让跨部件两两比较爆炸。
        max_diffusion_samples = 256
        accum: dict[int, dict] = {}
        for c in range(indices.shape[1]):
            idx_col = indices[:, c]
            w_col = weights[:, c] if c < weights.shape[1] else weights[:, 0]
            valid = (
                valid_mask[:, c]
                & numpy.isfinite(positions).all(axis=1)
            )
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
                entry = accum.setdefault(int(local), {
                    "weighted_sum": numpy.zeros(3, dtype=numpy.float64),
                    "weight_total": 0.0,
                    "weighted_sq_pos": 0.0,  # Σ w_i |p_i|²（用于扩散半径）
                    "bbox_min": numpy.full(3, numpy.inf),
                    "bbox_max": numpy.full(3, -numpy.inf),
                    "vertex_count": 0,
                    "points": [],
                    "weights": [],
                })
                entry["weighted_sum"] += (pts * ws_[:, None]).sum(axis=0)
                entry["weight_total"] += w_sum
                entry["weighted_sq_pos"] += float((ws_ * (pts ** 2).sum(axis=1)).sum())
                entry["bbox_min"] = numpy.minimum(entry["bbox_min"], pts.min(axis=0))
                entry["bbox_max"] = numpy.maximum(entry["bbox_max"], pts.max(axis=0))
                entry["vertex_count"] += int(mask.sum())
                entry["points"].extend(pts.astype(numpy.float32, copy=False).tolist())
                entry["weights"].extend(ws_.astype(numpy.float32, copy=False).tolist())

        result = {}
        for local, e in accum.items():
            if e["weight_total"] <= 0:
                continue
            centroid = e["weighted_sum"] / e["weight_total"]
            # 扩散半径：加权 RMS 半径 spread² = Σw|p|²/Σw - |c|²
            mean_sq = e["weighted_sq_pos"] / e["weight_total"]
            var = max(float(mean_sq - float((centroid ** 2).sum())), 0.0)
            spread = float(numpy.sqrt(var))
            diffusion_points = numpy.asarray(e["points"], dtype=numpy.float32)
            diffusion_weights = numpy.asarray(e["weights"], dtype=numpy.float32)
            if len(diffusion_points) > max_diffusion_samples:
                # 均匀抽样保留整片扩散区域，而不是只取最高权重的中心，
                # 这样“平面 + 散落物体”的接触边界不会被丢掉。
                sample_idx = numpy.linspace(
                    0, len(diffusion_points) - 1, max_diffusion_samples,
                    dtype=numpy.int64,
                )
                diffusion_points = diffusion_points[sample_idx]
                diffusion_weights = diffusion_weights[sample_idx]
            result[local] = {
                "centroid": centroid.astype(numpy.float32),
                "bbox_min": e["bbox_min"].astype(numpy.float32),
                "bbox_max": e["bbox_max"].astype(numpy.float32),
                "vertex_count": e["vertex_count"],
                "spread": spread,  # 扩散矢量球半径（权重强度衰减的扩散路径范围）
                "weight_total": float(e["weight_total"]),  # 权重强度
                "mean_weight": float(e["weight_total"]) / max(e["vertex_count"], 1),
                "diffusion_points": diffusion_points,
                "diffusion_weights": diffusion_weights,
                "diffusion_radius": EFMIBoneMapBuilder._diffusion_radius(diffusion_points),
                "diffusion_normals": EFMIBoneMapBuilder._estimate_diffusion_normals(
                    diffusion_points
                ),
            }
        return result

    @staticmethod
    def _diffusion_radius(points: numpy.ndarray) -> float:
        """估计一个扩散采样的空间影响半径。

        EFMI 的 Position.buf 是绑定姿态空间，网格密度因部件而异；用
        包围盒尺度/采样数估计局部间距，并限制在合理范围，避免稀疏部件
        的单个远点把整个场错误连起来。
        """
        if len(points) < 2:
            return 0.05
        extent = float(numpy.linalg.norm(
            numpy.max(points, axis=0) - numpy.min(points, axis=0)
        ))
        spacing = extent / max(float(numpy.sqrt(len(points))), 1.0)
        return float(numpy.clip(spacing * 0.25, 0.02, 0.20))

    @staticmethod
    def _estimate_diffusion_normals(
        points: numpy.ndarray,
        neighbor_count: int = 8,
    ) -> numpy.ndarray:
        """从点云局部 PCA 估计表面法向；无法判定为表面时返回 NaN。

        这不是网格拓扑法向（Position.buf 不携带面/边连接），但能识别
        “大腿表面/丝袜表面”这种两层近似平行的点云。线状或体积状点云
        不会强行套用表面投影规则，继续走原来的接触距离门控。
        """
        points = numpy.asarray(points, dtype=numpy.float32)
        normals = numpy.full((len(points), 3), numpy.nan, dtype=numpy.float32)
        if len(points) < 4 or points.ndim != 2 or points.shape[1] != 3:
            return normals

        neighbor_count = max(3, min(int(neighbor_count), len(points) - 1))
        for point_idx, point in enumerate(points):
            delta = points - point
            squared = numpy.sum(delta * delta, axis=1)
            order = numpy.argsort(squared)
            neighbors = points[order[1:neighbor_count + 1]]
            centered = neighbors - numpy.mean(neighbors, axis=0)
            covariance = centered.T @ centered / max(len(neighbors), 1)
            try:
                eigenvalues, eigenvectors = numpy.linalg.eigh(covariance)
            except numpy.linalg.LinAlgError:
                continue
            largest = float(eigenvalues[-1])
            if largest <= 1e-10:
                continue
            # 平面：最小特征值远小于最大值；线：中间特征值也接近 0；
            # 体积云：三个特征值相近。只接受真正“面状”的局部邻域。
            if float(eigenvalues[0]) / largest > 0.20:
                continue
            if float(eigenvalues[1]) / largest < 0.10:
                continue
            normal = eigenvectors[:, 0]
            length = float(numpy.linalg.norm(normal))
            if length > 1e-8:
                normals[point_idx] = (normal / length).astype(numpy.float32)
        return normals

    @staticmethod
    def _nearest_diffusion_points(
        source: numpy.ndarray,
        target: numpy.ndarray,
    ) -> tuple[numpy.ndarray, numpy.ndarray]:
        """返回 source 每个点在 target 中的**全局**最近距离和索引。

        旧版只检查所在均匀网格的相邻 27 个 cell；只要相邻 cell 恰好存在
        一个候选，就不会扫描更远 cell，即使后者的欧氏距离实际更小。平行层、
        凹槽边缘和非均匀三角网格都会触发这种漏检。扩散签名最多 256 点，按
        source 分块做精确向量化最近邻既确定又有界，也比逐点 Python 网格循环快。
        """
        if len(source) == 0 or len(target) == 0:
            return (
                numpy.full(len(source), numpy.inf, dtype=numpy.float32),
                numpy.full(len(source), -1, dtype=numpy.int64),
            )
        source = numpy.asarray(source, dtype=numpy.float32)
        target = numpy.asarray(target, dtype=numpy.float32)
        distances = numpy.empty(len(source), dtype=numpy.float32)
        indices = numpy.empty(len(source), dtype=numpy.int64)
        for start in range(0, len(source), 64):
            chunk = source[start:start + 64]
            delta = chunk[:, None, :] - target[None, :, :]
            squared = numpy.sum(delta * delta, axis=2)
            nearest = numpy.argmin(squared, axis=1)
            indices[start:start + len(chunk)] = nearest
            distances[start:start + len(chunk)] = numpy.sqrt(
                squared[numpy.arange(len(chunk)), nearest]
            )
        return distances, indices

    @staticmethod
    def _nearest_compatible_diffusion_points(
        source: numpy.ndarray,
        target: numpy.ndarray,
        source_normals: numpy.ndarray,
        target_normals: numpy.ndarray,
        contact_distance: float,
        layer_distance: float,
        tangent_tolerance: float,
        normal_alignment: float,
    ) -> tuple[numpy.ndarray, numpy.ndarray]:
        """在层间几何约束内查找最近点，而不是先取最近点再做拒绝。

        多层裙摆等点云里，欧氏最近点可能来自相邻的错误层。旧流程选中该点后
        才检查法向/切向约束，失败时不会继续搜索稍远但层级正确的点。这里先对
        每个点对应用距离、切向和法向约束，再从仍兼容的点里取最近者。
        """
        source = numpy.asarray(source, dtype=numpy.float32)
        target = numpy.asarray(target, dtype=numpy.float32)
        if len(source) == 0 or len(target) == 0:
            return (
                numpy.full(len(source), numpy.inf, dtype=numpy.float32),
                numpy.full(len(source), -1, dtype=numpy.int64),
            )

        source_normals = numpy.asarray(source_normals, dtype=numpy.float32)
        target_normals = numpy.asarray(target_normals, dtype=numpy.float32)
        source_shape_ok = source_normals.shape == (len(source), 3)
        target_shape_ok = target_normals.shape == (len(target), 3)
        source_valid_all = (
            numpy.isfinite(source_normals).all(axis=1)
            if source_shape_ok else numpy.zeros(len(source), dtype=bool)
        )
        target_valid_all = (
            numpy.isfinite(target_normals).all(axis=1)
            if target_shape_ok else numpy.zeros(len(target), dtype=bool)
        )
        safe_source_normals = (
            numpy.where(numpy.isfinite(source_normals), source_normals, 0.0)
            if source_shape_ok else numpy.zeros((len(source), 3), dtype=numpy.float32)
        )
        safe_target_normals = (
            numpy.where(numpy.isfinite(target_normals), target_normals, 0.0)
            if target_shape_ok else numpy.zeros((len(target), 3), dtype=numpy.float32)
        )

        distances = numpy.full(len(source), numpy.inf, dtype=numpy.float32)
        indices = numpy.full(len(source), -1, dtype=numpy.int64)
        for start in range(0, len(source), 64):
            chunk = source[start:start + 64]
            chunk_len = len(chunk)
            displacement = target[None, :, :] - chunk[:, None, :]
            squared = numpy.sum(displacement * displacement, axis=2)
            euclidean = numpy.sqrt(squared)

            source_valid = source_valid_all[start:start + chunk_len]
            paired_surface = source_valid[:, None] & target_valid_all[None, :]
            allowed_distance = numpy.where(
                paired_surface,
                float(layer_distance),
                float(contact_distance),
            )
            compatible = euclidean <= allowed_distance

            if numpy.any(source_valid):
                source_normal = safe_source_normals[start:start + chunk_len]
                normal_component = numpy.sum(
                    displacement * source_normal[:, None, :], axis=2
                )
                tangent = (
                    displacement
                    - normal_component[:, :, None] * source_normal[:, None, :]
                )
                tangent_distance = numpy.linalg.norm(tangent, axis=2)
                compatible &= (~source_valid[:, None]) | (
                    tangent_distance <= float(tangent_tolerance)
                )

            if numpy.any(target_valid_all):
                target_component = numpy.sum(
                    displacement * safe_target_normals[None, :, :], axis=2
                )
                tangent = (
                    displacement
                    - target_component[:, :, None] * safe_target_normals[None, :, :]
                )
                tangent_distance = numpy.linalg.norm(tangent, axis=2)
                compatible &= (~target_valid_all[None, :]) | (
                    tangent_distance <= float(tangent_tolerance)
                )

            if numpy.any(paired_surface):
                alignment = numpy.abs(
                    safe_source_normals[start:start + chunk_len]
                    @ safe_target_normals.T
                )
                compatible &= (~paired_surface) | (
                    alignment >= float(normal_alignment)
                )

            compatible_squared = numpy.where(compatible, squared, numpy.inf)
            nearest = numpy.argmin(compatible_squared, axis=1)
            nearest_squared = compatible_squared[numpy.arange(chunk_len), nearest]
            found = numpy.isfinite(nearest_squared)
            if numpy.any(found):
                found_indices = numpy.flatnonzero(found)
                output_indices = start + found_indices
                indices[output_indices] = nearest[found]
                distances[output_indices] = numpy.sqrt(nearest_squared[found])
        return distances, indices

    @classmethod
    def weight_diffusion_similarity(
        cls,
        signature_a: dict | None,
        signature_b: dict | None,
        distance_tolerance: float = 0.05,
        weight_tolerance: float = 0.20,
        min_coverage: float = 0.30,
        layer_distance: float = 0.15,
        tangent_tolerance: float = 0.05,
        normal_alignment: float = 0.70,
        weak_weight_floor: float = 0.25,
        min_support_points: int = 2,
        return_metrics: bool = False,
    ) -> bool | dict:
        """检测两个顶点组在空间接触处是否扩散出一致的权重场。

        每个方向都把本组的正权重点投影到另一组最近的正权重点。普通点云
        仍要求落在接触半径内；当两边能估计出局部表面法向时，允许沿法向
        存在一段层间距，并要求切向投影误差小、两层法向平行。这覆盖
        “大腿表面 + 悬空一小段的丝袜表面”而不要求共享顶点或拓扑连接。
        最终取覆盖率较高的方向。为避免“强权重点淹没弱权重点”，每个正权重
        点的评估权重至少达到该方向最大权重的 ``weak_weight_floor``；同时要求
        至少 ``min_support_points`` 个不同源点与不同目标点匹配。这只是评估用的
        最低影响，不会修改写回的原始蒙皮权重。``return_metrics`` 仅供候选歧义
        消解复用同一次扫描得到的覆盖率、权重误差和空间误差；默认仍返回 bool。
        """
        def finish(
            passes: bool,
            coverage: float = 0.0,
            weight_error: float = float("inf"),
            spatial_error: float = float("inf"),
            coverage_ab: float = 0.0,
            coverage_ba: float = 0.0,
        ):
            metrics = {
                "passes": bool(passes),
                "coverage": float(coverage),
                "weight_error": float(weight_error),
                "spatial_error": float(spatial_error),
                "coverage_ab": float(coverage_ab),
                "coverage_ba": float(coverage_ba),
            }
            return metrics if return_metrics else bool(passes)

        if not signature_a or not signature_b:
            return finish(False)
        points_a = numpy.asarray(signature_a.get("diffusion_points", []), dtype=numpy.float32)
        weights_a = numpy.asarray(signature_a.get("diffusion_weights", []), dtype=numpy.float32)
        points_b = numpy.asarray(signature_b.get("diffusion_points", []), dtype=numpy.float32)
        weights_b = numpy.asarray(signature_b.get("diffusion_weights", []), dtype=numpy.float32)
        if (
            len(points_a) == 0 or len(points_b) == 0
            or len(points_a) != len(weights_a) or len(points_b) != len(weights_b)
            or points_a.ndim != 2 or points_b.ndim != 2
            or points_a.shape[1] != 3 or points_b.shape[1] != 3
            or weights_a.ndim != 1 or weights_b.ndim != 1
        ):
            return finish(False)

        radius_a = float(signature_a.get("diffusion_radius", cls._diffusion_radius(points_a)))
        radius_b = float(signature_b.get("diffusion_radius", cls._diffusion_radius(points_b)))
        contact_distance = max(float(distance_tolerance), radius_a, radius_b)
        # 跨层投影有明确上限；没有成对可靠法向时仍走 contact_distance。
        layer_distance = min(max(float(layer_distance), contact_distance), 0.15)
        tangent_tolerance = max(float(tangent_tolerance), float(distance_tolerance))

        normals_a = numpy.asarray(
            signature_a.get("diffusion_normals", cls._estimate_diffusion_normals(points_a)),
            dtype=numpy.float32,
        )
        normals_b = numpy.asarray(
            signature_b.get("diffusion_normals", cls._estimate_diffusion_normals(points_b)),
            dtype=numpy.float32,
        )
        normals_a_valid = (
            normals_a.ndim == 2 and normals_a.shape == (len(points_a), 3)
            and numpy.isfinite(normals_a).all(axis=1)
        )
        normals_b_valid = (
            normals_b.ndim == 2 and normals_b.shape == (len(points_b), 3)
            and numpy.isfinite(normals_b).all(axis=1)
        )
        # 只有两侧都能提供至少一个可靠法向，才打开层间走廊；单侧/局部
        # 法向缺失的点对继续使用严格接触半径，避免把体积点云当成表面。
        has_surface_normals = bool(
            numpy.any(normals_a_valid) and numpy.any(normals_b_valid)
        )

        def directional(
            source_points,
            source_weights,
            target_points,
            target_weights,
            source_normals,
            target_normals,
        ):
            # 没有可靠表面法向时保持原来的“真实接触”距离；
            # 两层表面都有法向时，允许沿法向存在一小段层间距。
            if has_surface_normals:
                distances, nearest = cls._nearest_compatible_diffusion_points(
                    source_points,
                    target_points,
                    source_normals,
                    target_normals,
                    contact_distance,
                    layer_distance,
                    tangent_tolerance,
                    normal_alignment,
                )
            else:
                distances, nearest = cls._nearest_diffusion_points(
                    source_points, target_points
                )
            valid = nearest >= 0
            if not has_surface_normals:
                valid &= distances <= contact_distance
            # 最近邻不应被强制成双射：同一连续表面在两个部件上经常有完全
            # 不同的三角网格密度，高密度侧的多个点合理投影到低密度侧同一点。
            # 防止“整条槽边吸到一个孤立点”的方式改为要求至少多个不同目标
            # 支持点；只有目标本来就只有一个点时，另用局部范围限制处理。
            # 原始权重总量会让少量弱权重点几乎没有话语权（例如左/右两侧
            # 各有一个很弱的点，强中心点却能把错误匹配“冲淡”）。使用相对
            # 权重下限保留强度排序，同时让每个正权重样本都能影响判定。
            finite_positive = source_weights[numpy.isfinite(source_weights) & (source_weights > 0)]
            if len(finite_positive) == 0:
                return 0.0, float("inf"), float("inf")
            source_peak = float(numpy.max(finite_positive))
            floor = max(source_peak * max(float(weak_weight_floor), 0.0), 0.05)
            evaluation_weights = numpy.where(
                numpy.isfinite(source_weights) & (source_weights > 0),
                numpy.maximum(source_weights, floor),
                0.0,
            )
            total = float(numpy.sum(evaluation_weights))
            # 稀疏的凹槽底/装饰物可能只有 1~2 个正权重采样点，不能因为
            # 全局默认值较高就被强制拆开。两边都很稀疏时按可用点数动态下调，
            # 但仍至少保留一个真正的扩散配对作为证据。
            required_support = min(
                max(int(min_support_points), 1),
                len(source_points),
                len(target_points),
            )
            if total <= 1e-8 or int(numpy.count_nonzero(valid)) < required_support:
                return 0.0, float("inf"), float("inf")
            unique_target_support = len(numpy.unique(nearest[valid]))
            if unique_target_support < required_support:
                return 0.0, float("inf"), float("inf")
            evaluation_w = evaluation_weights[valid]
            # weak_weight_floor 只用于“每个采样点有多少评估影响”，不能
            # 覆写拿来比较的原始权重值；旧实现把 0.01 抬成 0.25 后再与
            # 另一侧真实 0.01 比，凭空制造了 0.24 的误差。
            source_w = source_weights[valid]
            target_w = target_weights[nearest[valid]]
            coverage = float(numpy.sum(evaluation_w) / total)
            error = float(numpy.sum(evaluation_w * numpy.abs(source_w - target_w)) /
                          max(float(numpy.sum(evaluation_w)), 1e-8))
            spatial_error = float(
                numpy.sum(evaluation_w * distances[valid])
                / max(float(numpy.sum(evaluation_w)), 1e-8)
            )
            return coverage, error, spatial_error

        cov_ab, err_ab, spatial_ab = directional(
            points_a, weights_a, points_b, weights_b, normals_a, normals_b
        )
        cov_ba, err_ba, spatial_ba = directional(
            points_b, weights_b, points_a, weights_a, normals_b, normals_a
        )
        if (cov_ab, -err_ab, -spatial_ab) >= (cov_ba, -err_ba, -spatial_ba):
            coverage, error, spatial_error = cov_ab, err_ab, spatial_ab
        else:
            coverage, error, spatial_error = cov_ba, err_ba, spatial_ba
        # 极稀疏的一侧只有一个采样点时没有“多个目标支持点”可用。此时
        # 要求另一侧本身局限在一个接触直径内，并且双向都有覆盖；这样保留
        # 单点小附件，同时拒绝一个点吸附整条长槽边。
        if min(len(points_a), len(points_b)) <= 1:
            larger = points_a if len(points_a) > len(points_b) else points_b
            extent = float(numpy.linalg.norm(
                numpy.max(larger, axis=0) - numpy.min(larger, axis=0)
            ))
            if extent > contact_distance * 2.0:
                return finish(
                    False, coverage, error, spatial_error, cov_ab, cov_ba
                )
            if min(cov_ab, cov_ba) < float(min_coverage):
                return finish(
                    False, coverage, error, spatial_error, cov_ab, cov_ba
                )
        passes = coverage >= float(min_coverage) and error <= float(weight_tolerance)
        return finish(
            passes, coverage, error, spatial_error, cov_ab, cov_ba
        )

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
        match_tolerance: float = 1e-3,
        centroid_tolerance: float = 0.02,
        diffusion_distance: float = 0.05,
        diffusion_weight_tolerance: float = 0.20,
        diffusion_min_coverage: float = 0.30,
        diffusion_layer_distance: float = 0.15,
        diffusion_tangent_tolerance: float = 0.05,
        diffusion_normal_alignment: float = 0.70,
        diffusion_weak_weight_floor: float = 0.25,
        diffusion_min_support_points: int = 2,
        protected_pairs: set[tuple[tuple[str, int], tuple[str, int]]] | None = None,
        constraint_labels: dict[tuple[str, int], object] | None = None,
        deduplicate: bool | None = None,
    ) -> tuple[dict[str, dict], dict[str, int]]:
        """跨子网格按"矩阵硬门控 + 权重扩散确认"去重构建 vg_map（同部件不去重）。

        总开关 _DEDUP_ENABLED（默认 True；关闭时仅用于诊断/回滚）；
        置 False 时退化为恒等映射（local → vg_offset + local），下方并查集逻辑不执行。

        参数:
            unique_str -> (skeleton_buffer, vg_count, weighted_vertex_counts[, signatures])
            - signatures: {local: {centroid, bbox_min/max, vertex_count, spread,
              weight_total, diffusion_points, diffusion_weights}}
              （扩散字段参与近似矩阵判定，其余字段供回退/调试）
            - protected_pairs: 已由跨 LOD 原始候选对应层确认“不可合并”的候选对。
              这些对在本 LOD 内强制断边，用于防止一侧过度去重吞掉另一侧能区分的组。
            - constraint_labels: 跨 LOD 迭代产生的临时语义标签。两个候选都已有
              标签且标签不同时，其并查集组不可合并；无标签候选可附着到一侧，
              但不能再作为桥把两个不同标签的组串起来。
        返回: (vg_maps, vg_offsets)

        去重判据（分层，矩阵是必要条件——几何接近无权推翻矩阵不一致）：
        1. **矩阵 diff >= match_tolerance：永不合并**。
           实测定案"误合并有害、漏合并无害"（容差误并手指两节导致功能丢失；
           漏并仅多占槽位，蒙皮仍正确）。
        2. **矩阵 bitwise 完全相同（diff == 0）**：有扩散采样时仍需通过接触
           权重一致性；缺少采样时兼容参考插件直接合并。
        3. **0 < diff < match_tolerance：优先做权重扩散确认**。
           将每个组的正权重点视为空间扩散场，在另一个组的正权重点上做最近邻
           采样；普通点云要求接触半径内，能估计局部表面法向时允许沿法向
           存在层间距，但切向误差和法向夹角必须通过；接触位置覆盖率达到 30%，
           原始权重平均误差不超过 0.20 才合并。这能识别“大腿 + 丝袜”而不要求
           共享顶点或整体质心相同。
        4. 没有扩散采样时回退到加权质心距离 < centroid_tolerance；两者都缺失
           则保守不合并（bitwise 完全相同仍保留直接合并的兼容语义）。

        废止记录：此前的"多维度投票"（矩阵/质心/包围盒/扩散球 vote>=2）把矩阵
        降为可输的一票——几何接近度会同时通过并推翻矩阵反对票；实测"测试"工作空间
        08-10 dump 上 195 个合并组中 42 组矩阵差异 > 1e-3（最高 0.27）。当前扩散
        判据只在矩阵硬门内使用“接触位置权重值”确认，不允许几何接近单独促成合并。

        **同部件冲突拒绝**（硬性规则）：并查集 union 时检查，组内同部件最多 1 个 local，
        防"同位置功能骨骼"（如手指两节）合并及链式绕过。

        **歧义候选一对一消解**：每对 Component 内先收集所有通过硬门控的边，再同步
        收紧证据等级、矩阵差、权重误差、覆盖缺口和空间误差。A:1 同时命中 B:8/B:9
        时只保留扩散相似度更高的一条；完全相同才用稳定 id 决胜，结果不依赖遍历先后。

        **连续扩散图判定**：每个成员必须通过至少一条权重扩散边连接到组，
        并在并入后对整组重新做连通性检查；不要求平面和每一个凹槽底直接
        两两相交，允许“平面 → 槽壁/槽底”的连续桥接，同时禁止孤立断点。

        参数（可实测调整）:
            match_tolerance: 矩阵硬门控上限（默认 1e-3，达到即不合并）。
            centroid_tolerance: 无扩散采样时的质心回退阈值（默认 0.02）。
            diffusion_distance: 接触点基础距离阈值（默认 0.05）。
            diffusion_weight_tolerance: 接触点原始权重平均误差（默认 0.20）。
            diffusion_min_coverage: 一侧扩散场需被另一侧覆盖的最小比例（默认 0.30）。
            diffusion_layer_distance: 平行/错位表面允许的法向扩散走廊（默认 0.15）。
            diffusion_tangent_tolerance: 表面投影允许的切向误差（默认 0.05）。
            diffusion_normal_alignment: 两层局部法向最小绝对点积（默认 0.70）。
            diffusion_weak_weight_floor: 弱权重点的最小评估权重，占该方向最大
                权重的比例（默认 0.25；同时有 0.05 的绝对下限）。
            diffusion_min_support_points: 每个方向至少需要的唯一配对点数（默认 2，
                对稀疏组按实际采样数动态下调到至少 1）。
            deduplicate: 显式设为 False 时只建立恒等槽位映射，供目标 LOD 同步使用，
                不运行权重扩散/并查集去重；省略时使用全局 _DEDUP_ENABLED。
        """
        # 收集所有骨骼候选
        candidates: list[dict] = []
        offset = 0
        vg_offsets: dict[str, int] = {}

        for unique_str in sorted(submesh_skeletons.keys()):
            entry = submesh_skeletons[unique_str]
            skeleton_buffer = entry[0]
            vg_count = entry[1]
            weighted_vertex_counts = entry[2] if len(entry) > 2 else None
            signatures = entry[3] if len(entry) > 3 else {}

            if skeleton_buffer is None or vg_count <= 0:
                continue
            if len(skeleton_buffer) < vg_count:
                print(
                    f"[EFMI骨骼合并] 警告: {unique_str} 骨骼段仅 {len(skeleton_buffer)} 根骨骼，"
                    f"但声明了 {vg_count} 个顶点组，跳过该子网格参与合并。"
                )
                continue

            vg_offsets[unique_str] = offset
            for vg_id in range(vg_count):
                bone = skeleton_buffer[vg_id]
                if numpy.all(bone == 0):
                    continue
                weighted_count = (
                    int(weighted_vertex_counts[vg_id])
                    if weighted_vertex_counts is not None and vg_id < len(weighted_vertex_counts)
                    else 0
                )
                candidates.append({
                    "unique_str": unique_str,
                    "local_vg_id": vg_id,
                    "global_vg_id": offset + vg_id,
                    "weighted_vertex_count": weighted_count,
                    "bone": bone,
                    "signature": signatures.get(vg_id),
                })
            offset += vg_count

        n = len(candidates)
        if n == 0:
            return {}, {}

        dedup_enabled = _DEDUP_ENABLED if deduplicate is None else bool(deduplicate)
        if not dedup_enabled:
            # 恒等映射：每根骨骼独占全局槽位（候选收集阶段已按
            # global_vg_id = vg_offset + local_vg_id 分配），不做任何合并。
            if deduplicate is None:
                print(
                    f"[EFMI骨骼合并] 顶点组去重已全局关闭（_DEDUP_ENABLED=False），"
                    f"{n} 根骨骼全部独占槽位（恒等映射，无任何合并）。"
                )
            identity_maps: dict[str, dict] = {}
            for cand in candidates:
                identity_maps.setdefault(cand["unique_str"], {})[
                    cand["local_vg_id"]
                ] = cand["global_vg_id"]
            return identity_maps, vg_offsets

        parent = list(range(n))
        group_submeshes: list[set] = [{candidates[i]["unique_str"]} for i in range(n)]
        # 每组的所有成员索引（用于合并后的权重扩散连通性校验）
        group_members: list[list[int]] = [[i] for i in range(n)]

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        mats = numpy.stack([c["bone"] for c in candidates]).astype(numpy.float64)

        def _sig(idx):
            return candidates[idx].get("signature")

        def evaluate(i, j) -> dict:
            """分层判据（两两独立）及后续歧义消解所需的连续指标。

            - 矩阵 diff >= match_tolerance：永不合并（几何接近无权推翻）；
            - 矩阵 bitwise 完全相同：有扩散采样时仍验证权重场，无采样时兼容直接通过；
            - 近似：优先验证权重扩散，无扩散时才用加权质心距离（缺签名保守拒绝）。
            """
            matrix_diff = float(numpy.abs(mats[i] - mats[j]).max())
            result = {
                "passes": False,
                "evidence_rank": 3,
                "matrix_diff": matrix_diff,
                "coverage": 0.0,
                "weight_error": float("inf"),
                "spatial_error": float("inf"),
            }
            if matrix_diff >= match_tolerance:
                return result
            si, sj = _sig(i), _sig(j)
            if matrix_diff == 0.0 and (si is None or sj is None):
                # 没有 Position/Blend 扩散证据时保留参考插件的 bitwise
                # 兼容语义；有证据则必须验证接触位置的权重场。
                result.update({
                    "passes": True,
                    "evidence_rank": 1,
                    "weight_error": 0.0,
                    "spatial_error": 0.0,
                })
                return result
            if si is None or sj is None:
                return result

            # 两边都有 Position/Blend 生成的扩散采样时，扩散场是主判据；
            # 质心仅作为旧缓存/测试签名的兼容回退，不能覆盖已观测到的
            # 接触位置权重冲突。
            has_diffusion = (
                len(si.get("diffusion_points", [])) > 0
                and len(si.get("diffusion_weights", [])) > 0
                and len(sj.get("diffusion_points", [])) > 0
                and len(sj.get("diffusion_weights", [])) > 0
            )
            if has_diffusion:
                # 包围盒完全分离且间隙超过扩散半径时不可能存在接触
                # 证据，先在这里剪枝，避免对所有候选执行最近邻扫描。
                try:
                    a_min = numpy.asarray(si["bbox_min"], dtype=numpy.float64)
                    a_max = numpy.asarray(si["bbox_max"], dtype=numpy.float64)
                    b_min = numpy.asarray(sj["bbox_min"], dtype=numpy.float64)
                    b_max = numpy.asarray(sj["bbox_max"], dtype=numpy.float64)
                    gap_vec = numpy.maximum(numpy.maximum(a_min - b_max, b_min - a_max), 0.0)
                    gap = float(numpy.linalg.norm(gap_vec))
                    radius = max(
                        float(diffusion_distance),
                        float(diffusion_layer_distance),
                        EFMIBoneMapBuilder._diffusion_radius(
                            numpy.asarray(si["diffusion_points"], dtype=numpy.float32)
                        ),
                        EFMIBoneMapBuilder._diffusion_radius(
                            numpy.asarray(sj["diffusion_points"], dtype=numpy.float32)
                        ),
                    )
                    if gap > radius:
                        return result
                except (KeyError, TypeError, ValueError):
                    # 外部调用者可只提供 diffusion_points/weights；字段不全
                    # 时交给最近邻函数做保守判定。
                    pass
                metrics = EFMIBoneMapBuilder.weight_diffusion_similarity(
                    si,
                    sj,
                    distance_tolerance=diffusion_distance,
                    weight_tolerance=diffusion_weight_tolerance,
                    min_coverage=diffusion_min_coverage,
                    layer_distance=diffusion_layer_distance,
                    tangent_tolerance=diffusion_tangent_tolerance,
                    normal_alignment=diffusion_normal_alignment,
                    weak_weight_floor=diffusion_weak_weight_floor,
                    min_support_points=diffusion_min_support_points,
                    return_metrics=True,
                )
                result.update({
                    "passes": bool(metrics["passes"]),
                    "evidence_rank": 0,
                    "coverage": float(metrics["coverage"]),
                    "weight_error": float(metrics["weight_error"]),
                    "spatial_error": float(metrics["spatial_error"]),
                })
                return result
            if matrix_diff == 0.0:
                result.update({
                    "passes": True,
                    "evidence_rank": 1,
                    "weight_error": 0.0,
                    "spatial_error": 0.0,
                })
                return result
            dist = float(numpy.linalg.norm(
                si["centroid"].astype(numpy.float64) - sj["centroid"].astype(numpy.float64)
            ))
            result.update({
                "passes": dist < centroid_tolerance,
                "evidence_rank": 2,
                "weight_error": 0.0,
                "spatial_error": dist,
            })
            return result

        pair_evaluation_cache: dict[tuple[int, int], dict] = {}

        normalized_protected_pairs: set[tuple[tuple[str, int], tuple[str, int]]] = set()
        for pair in protected_pairs or ():
            try:
                left, right = pair
                left_key = (str(left[0]), int(left[1]))
                right_key = (str(right[0]), int(right[1]))
            except (TypeError, ValueError, IndexError):
                continue
            if left_key == right_key:
                continue
            normalized_protected_pairs.add(
                tuple(sorted((left_key, right_key)))
            )

        normalized_constraint_labels = {}
        for candidate_key, label in (constraint_labels or {}).items():
            try:
                key = (str(candidate_key[0]), int(candidate_key[1]))
            except (TypeError, ValueError, IndexError):
                continue
            normalized_constraint_labels[key] = label

        def _candidate_label(idx: int):
            candidate = candidates[idx]
            return normalized_constraint_labels.get((
                str(candidate["unique_str"]), int(candidate["local_vg_id"])
            ))

        group_labels: list[set] = []
        for idx in range(n):
            label = _candidate_label(idx)
            group_labels.append(set() if label is None else {label})

        def is_protected(i: int, j: int) -> bool:
            left = (str(candidates[i]["unique_str"]), int(candidates[i]["local_vg_id"]))
            right = (str(candidates[j]["unique_str"]), int(candidates[j]["local_vg_id"]))
            return tuple(sorted((left, right))) in normalized_protected_pairs

        def pair_evaluation(a, b) -> dict:
            key = (a, b) if a < b else (b, a)
            if key not in pair_evaluation_cache:
                if is_protected(a, b):
                    pair_evaluation_cache[key] = {
                        "passes": False,
                        "evidence_rank": 3,
                        "matrix_diff": float("inf"),
                        "coverage": 0.0,
                        "weight_error": float("inf"),
                        "spatial_error": float("inf"),
                    }
                else:
                    pair_evaluation_cache[key] = evaluate(a, b)
            return pair_evaluation_cache[key]

        def pair_passes(a, b) -> bool:
            return bool(pair_evaluation(a, b)["passes"])

        def _continuous_members(members: list[int]) -> bool:
            """检查合并出来的 VG 是否存在权重扩散断点。

            这是一个无向图：顶点是原始顶点组，边是通过矩阵硬门控和
            权重扩散确认的配对。只有整组连通才允许写回同一个 global VG；
            因而“平面 + 多个槽底”可以通过各自的局部桥接合并，但没有任何
            扩散证据的孤立物体永远不会被带进来。
            """
            if len(members) <= 1:
                return True
            visited = {members[0]}
            stack = [members[0]]
            member_set = set(members)
            while stack:
                current = stack.pop()
                for other in member_set - visited:
                    if candidates[current]["unique_str"] == candidates[other]["unique_str"]:
                        continue
                    if pair_passes(current, other):
                        visited.add(other)
                        stack.append(other)
            return len(visited) == len(member_set)

        def try_union(a, b):
            ra, rb = find(a), find(b)
            if ra == rb:
                return
            # 冲突拒绝：合并后某子网格在同组会有 >1 个 local → 拒绝
            if group_submeshes[ra] & group_submeshes[rb]:
                return
            # 跨 LOD 语义冲突拒绝：不同标签的两组不能被无标签候选桥接。
            if len(group_labels[ra] | group_labels[rb]) > 1:
                return
            # 只要两组之间存在一条真实扩散桥即可尝试合并；随后对合并结果
            # 做整组连通性复核。这样不会把平面和每个凹槽底强行当作完全图，
            # 也不会允许没有任何桥接的孤立成员混入。
            if not any(
                pair_passes(mi, mj)
                for mi in group_members[ra]
                for mj in group_members[rb]
            ):
                return
            merged_members = group_members[ra] + group_members[rb]
            if not _continuous_members(merged_members):
                return
            parent[ra] = rb
            group_submeshes[rb] = group_submeshes[ra] | group_submeshes[rb]
            group_members[rb] = merged_members
            group_labels[rb] = group_labels[ra] | group_labels[rb]

        def edge_dimensions(edge) -> tuple[float, ...]:
            """把通过边投影到可共同收紧的无量纲“不相似度”维度。

            矩阵差放在第一维；候选冲突时先按有限矩阵级联消解，只有
            矩阵仍无法区分时才读取后续扩散维度。
            """
            _i, _j, evaluation = edge
            matrix_error = float(evaluation["matrix_diff"]) / max(
                float(match_tolerance), 1e-8
            )
            evidence_rank = float(evaluation["evidence_rank"])
            if int(evaluation["evidence_rank"]) == 0:
                weight_error = float(evaluation["weight_error"]) / max(
                    float(diffusion_weight_tolerance), 1e-8
                )
                coverage_error = max(0.0, 1.0 - float(evaluation["coverage"]))
                spatial_error = float(evaluation["spatial_error"]) / max(
                    float(diffusion_distance),
                    float(diffusion_layer_distance),
                    1e-8,
                )
            elif int(evaluation["evidence_rank"]) == 2:
                # 无扩散采样的质心回退仍可参与确定性一对一选择，但其证据
                # 等级低于真实扩散场，空间误差按自己的通过阈值归一化。
                weight_error = 0.0
                coverage_error = 1.0
                spatial_error = float(evaluation["spatial_error"]) / max(
                    float(centroid_tolerance), 1e-8
                )
            else:
                # bitwise 相同但无采样时没有扩散维度可比较；最后由矩阵及
                # 稳定的候选 id 决胜，保留旧数据的兼容合并语义。
                weight_error = 0.0
                coverage_error = 1.0
                spatial_error = 0.0
            return (
                matrix_error,
                evidence_rank,
                weight_error,
                coverage_error,
                spatial_error,
            )

        def choose_ambiguous_edge(edges: list[tuple[int, int, dict]]):
            """先有限收紧矩阵，再逐维收紧扩散指标，直到只剩唯一候选。

            初始集合已经通过 ``match_tolerance`` 硬门控。矩阵歧义阶段最多
            从 1e-3 逐级收紧到 1e-6；只要某一级只剩一个候选就立即返回，
            后续扩散维度不能把它抢走。到 1e-6 仍有多个候选时，才同步
            收紧证据等级、权重误差、覆盖缺口和空间误差；最后用稳定 id
            处理完全相同或维度互有胜负的候选。
            """
            contenders = list(edges)
            dimensions = {id(edge): edge_dimensions(edge) for edge in contenders}

            matrix_thresholds = [float(match_tolerance)]
            matrix_threshold = float(match_tolerance)
            while matrix_threshold > _MATRIX_AMBIGUITY_FLOOR:
                next_threshold = max(
                    _MATRIX_AMBIGUITY_FLOOR,
                    matrix_threshold / 10.0,
                )
                if next_threshold >= matrix_threshold:
                    break
                matrix_thresholds.append(next_threshold)
                matrix_threshold = next_threshold
                if matrix_threshold <= _MATRIX_AMBIGUITY_FLOOR:
                    break

            for matrix_threshold in matrix_thresholds:
                narrowed = [
                    edge for edge in contenders
                    if float(edge[2]["matrix_diff"]) < matrix_threshold
                ]
                if narrowed:
                    contenders = narrowed
                    if len(contenders) == 1:
                        return contenders[0]

            # 到矩阵下限仍无法区分，矩阵维度冻结，只处理后续维度。
            for factor in (0.75, 0.50, 0.25, 0.10, 0.05, 0.01, 0.0):
                mins = tuple(
                    min(dimensions[id(edge)][dimension] for edge in contenders)
                    for dimension in range(1, 5)
                )
                maxs = tuple(
                    max(dimensions[id(edge)][dimension] for edge in contenders)
                    for dimension in range(1, 5)
                )
                limits = tuple(
                    mins[dimension - 1]
                    + (maxs[dimension - 1] - mins[dimension - 1]) * float(factor)
                    for dimension in range(1, 5)
                )
                narrowed = [
                    edge for edge in contenders
                    if all(
                        dimensions[id(edge)][dimension] <= limits[dimension - 1] + 1e-9
                        for dimension in range(1, 5)
                    )
                ]
                if narrowed:
                    contenders = narrowed
                    if len(contenders) == 1:
                        return contenders[0]

            def stable_quality(edge):
                vector = dimensions[id(edge)]
                return (max(vector), sum(vector), vector, edge[0], edge[1])

            return min(contenders, key=stable_quality)

        # 每对 Component 先形成一个一对一候选集。同一个 A local 即使同时
        # 命中 B:8/B:9，也只能保留动态收紧后的最佳边；反向同理。之后仍由
        # 并查集的 group_submeshes 约束保证跨多个 Component 的组内唯一性。
        edges_by_component_pair: dict[
            tuple[str, str], list[tuple[int, int, dict]]
        ] = {}
        for i in range(n):
            for j in range(i + 1, n):
                component_i = str(candidates[i]["unique_str"])
                component_j = str(candidates[j]["unique_str"])
                if component_i == component_j:
                    continue
                evaluation = pair_evaluation(i, j)
                if not evaluation["passes"]:
                    continue
                labels = group_labels[i] | group_labels[j]
                if len(labels) > 1:
                    continue
                component_pair = tuple(sorted((component_i, component_j)))
                edges_by_component_pair.setdefault(component_pair, []).append(
                    (i, j, evaluation)
                )

        selected_edges: list[tuple[int, int, dict]] = []
        for component_pair in sorted(edges_by_component_pair):
            remaining = list(edges_by_component_pair[component_pair])
            while remaining:
                chosen = choose_ambiguous_edge(remaining)
                selected_edges.append(chosen)
                chosen_members = {chosen[0], chosen[1]}
                remaining = [
                    edge for edge in remaining
                    if not chosen_members.intersection((edge[0], edge[1]))
                ]

        # 全局并入顺序也必须保持“矩阵优先”。之前这里先比较所有维度的
        # 最大值，可能让扩散误差较小但矩阵差明显更大的浮层边先占用槽位，
        # 使真正的同骨骼边在 try_union 的子网格冲突检查中被拒绝。
        selected_edges.sort(key=lambda edge: (
            edge_dimensions(edge),
            edge[0],
            edge[1],
        ))
        for i, j, _evaluation in selected_edges:
            if find(i) != find(j):
                try_union(i, j)

        # 分组。并查集合并过程中已经逐次检查过连通性；这里再做一次最终
        # 权重扩散图校验，并把任何意外断开的组件拆回独立 global VG，避免
        # “中途通过、最终写回却有孤岛”的缓存/调用方回归。
        raw_groups: dict[int, list[int]] = {}
        for i in range(n):
            raw_groups.setdefault(find(i), []).append(i)

        groups: dict[int, list[int]] = {}
        next_group_id = 0
        for members in raw_groups.values():
            remaining = set(members)
            while remaining:
                seed = next(iter(remaining))
                component = {seed}
                stack = [seed]
                remaining.remove(seed)
                while stack:
                    current = stack.pop()
                    for other in tuple(remaining):
                        if candidates[current]["unique_str"] == candidates[other]["unique_str"]:
                            continue
                        if pair_passes(current, other):
                            remaining.remove(other)
                            component.add(other)
                            stack.append(other)
                groups[next_group_id] = sorted(component)
                next_group_id += 1

        # 每组 canonical = 权重顶点数最多的候选
        vg_maps: dict[str, dict] = {}
        for root, members in groups.items():
            canonical_idx = max(members, key=lambda i: candidates[i]["weighted_vertex_count"])
            canonical_global = candidates[canonical_idx]["global_vg_id"]
            for i in members:
                cand = candidates[i]
                vg_maps.setdefault(cand["unique_str"], {})[cand["local_vg_id"]] = canonical_global

        return vg_maps, vg_offsets

    @staticmethod
    def build_lod_maps_from_reference(
        reference_submesh_skeletons: dict[str, tuple],
        reference_vg_maps: dict[str, dict],
        target_submesh_skeletons: dict[str, tuple],
        correspondence: dict,
        reference_lod: str | None = None,
        target_lod: str | None = None,
    ) -> tuple[dict[str, dict], dict[str, int]]:
        """按参考 LOD 的去重分区生成目标 LOD 映射。

        目标侧只建立原始槽位的恒等映射，然后把已有的一对一原始候选对应
        投影到参考侧 global group。这样 LOD1 不再独立运行权重扩散/并查集，
        但仍使用自己的槽位编号和自己的骨骼矩阵池；未被对应覆盖的额外 LOD1
        候选保留恒等槽位，不伪造参考侧缺失的顶点组。
        """
        target_maps, target_offsets = EFMIBoneMapBuilder.build_vg_maps(
            target_submesh_skeletons,
            deduplicate=False,
        )
        if not target_maps or not reference_vg_maps:
            return target_maps, target_offsets

        reference_lod = str(
            reference_lod or correspondence.get("reference_lod", "") or ""
        ).strip()
        if target_lod is None:
            lod_names = {
                str(row.get("target_lod", "") or "").strip()
                for row in correspondence.get("matches", []) or []
            }
            target_lod = next(iter(sorted(lod_names)), "")
        target_lod = str(target_lod or "").strip()

        # 先为每个参考 global group 选一个确定性的目标槽位。primary
        # correspondence 已保证一个目标原始候选只对应一个参考候选；同一参考组
        # 的多个目标候选表示 LOD1 额外细分，全部投到该组的第一个目标槽位。
        target_slot_by_reference_group: dict[int, int] = {}
        projected_rows = []
        for row in correspondence.get("matches", []) or []:
            if reference_lod and str(row.get("reference_lod", "") or "") != reference_lod:
                continue
            if target_lod and str(row.get("target_lod", "") or "") != target_lod:
                continue
            reference_unique = str(row.get("reference_unique_str", "") or "")
            target_unique = str(row.get("target_unique_str", "") or "")
            if reference_unique not in reference_submesh_skeletons:
                continue
            try:
                reference_local = int(row.get("reference_local_vg_id", 0) or 0)
                target_local = int(row.get("target_local_vg_id", 0) or 0)
            except (TypeError, ValueError):
                continue
            reference_group = reference_vg_maps.get(reference_unique, {}).get(reference_local)
            target_slot = target_maps.get(target_unique, {}).get(target_local)
            if reference_group is None or target_slot is None:
                continue
            reference_group = int(reference_group)
            target_slot = int(target_slot)
            projected_rows.append((
                reference_group,
                target_slot,
                target_unique,
                target_local,
            ))
            previous = target_slot_by_reference_group.get(reference_group)
            if previous is None or target_slot < previous:
                target_slot_by_reference_group[reference_group] = target_slot

        for reference_group, _target_slot, target_unique, target_local in projected_rows:
            canonical_target = target_slot_by_reference_group.get(reference_group)
            if canonical_target is None:
                continue
            if target_local in target_maps.get(target_unique, {}):
                target_maps[target_unique][target_local] = canonical_target
        return target_maps, target_offsets

    @staticmethod
    def build_cross_lod_correspondence(
        lod_submesh_skeletons: dict[str, dict[str, tuple]],
        reference_lod: str = "LOD0",
        match_tolerance: float = 1.25,
        centroid_tolerance: float = 0.10,
    ) -> dict:
        """用原始顶点组的矩阵 + 加权中心建立跨 LOD 对应，并生成去重保护边。

        这里故意接收 *未去重* 的 ``submesh_skeletons``。如果先在每个 LOD
        内部去重，某一侧已经吞掉的候选就无法再被另一侧矩阵区分出来。返回值中
        ``protected_pairs`` 保留为诊断/兼容字段；联合导入路径不再把它交给两侧
        独立去重，而是用 ``matches`` 将 LOD0 的分区同步到目标 LOD。

        对应不是按文件夹名配对：LOD 提取时组件 hash、局部索引和顶点数量都可能
        轻微变化。先为每个部件聚合其权重扩散点云，使用对称最近邻几何距离、包围盒
        和整体中心做一对一部件匹配；再**只在匹配的部件对内部**按局部加权中心配对
        原始顶点组，矩阵只作为第二排序键和异常过滤。LOD 之间的矩阵本身可能因为
        简化网格/捕获帧不同而有明显差异，因此这里的 1.25 仅是跨 LOD 参考门槛，不会
        改变各 LOD 内部 build_vg_maps 的 1e-3 硬门控。
        不要求两个 LOD 的整片驱动区域完全重合（例如平面和贴合物体）。
        同一参考候选在目标 LOD 可以有多个命中，表示目标 LOD 的额外细分；反向
        保护只在两个候选分别对应不同参考候选时触发。

        返回字段是纯 Python/JSON 友好的结构：
        ``reference_lod``、``matches``、``protected_pairs``、``counts``、
        ``unmatched_reference``、``unmatched_by_lod``。
        """
        if not lod_submesh_skeletons:
            return {
                "reference_lod": "",
                "part_matches": [],
                "unmatched_reference_parts": [],
                "unmatched_target_parts": {},
                "matches": [],
                "protected_pairs": {},
                "counts": {},
                "unmatched_reference": [],
                "unmatched_by_lod": {},
            }

        lod_names = sorted(str(name) for name in lod_submesh_skeletons.keys())
        reference_lod = str(reference_lod or "").strip()
        if reference_lod not in lod_names:
            reference_lod = "LOD0" if "LOD0" in lod_names else lod_names[0]

        def _flatten(lod_name: str) -> list[dict]:
            result = []
            parts = lod_submesh_skeletons.get(lod_name, {}) or {}
            for unique_str in sorted(parts.keys()):
                entry = parts[unique_str]
                if not entry or len(entry) < 2:
                    continue
                skeleton = entry[0]
                vg_count = int(entry[1] or 0)
                weighted = entry[2] if len(entry) > 2 else None
                signatures = entry[3] if len(entry) > 3 else {}
                if skeleton is None or vg_count <= 0 or len(skeleton) < vg_count:
                    continue
                for local in range(vg_count):
                    bone = numpy.asarray(skeleton[local], dtype=numpy.float64)
                    if bone.size == 0 or numpy.all(bone == 0):
                        continue
                    count = 0
                    if weighted is not None and local < len(weighted):
                        try:
                            count = int(weighted[local])
                        except (TypeError, ValueError):
                            count = 0
                    signature = signatures.get(local) if isinstance(signatures, dict) else None
                    centroid = None
                    spread = 0.0
                    diffusion_points = numpy.empty((0, 3), dtype=numpy.float64)
                    diffusion_weights = numpy.empty((0,), dtype=numpy.float64)
                    if isinstance(signature, dict):
                        raw_centroid = signature.get("centroid")
                        if raw_centroid is not None:
                            try:
                                value = numpy.asarray(raw_centroid, dtype=numpy.float64)
                                if value.shape == (3,) and numpy.isfinite(value).all():
                                    centroid = value
                            except (TypeError, ValueError):
                                centroid = None
                        try:
                            spread = max(float(signature.get("spread", 0.0) or 0.0), 0.0)
                        except (TypeError, ValueError):
                            spread = 0.0
                        try:
                            raw_points = numpy.asarray(
                                signature.get("diffusion_points", []),
                                dtype=numpy.float64,
                            )
                            raw_weights = numpy.asarray(
                                signature.get("diffusion_weights", []),
                                dtype=numpy.float64,
                            )
                            if (
                                raw_points.ndim == 2
                                and raw_points.shape[1] == 3
                                and raw_weights.ndim == 1
                                and len(raw_points) == len(raw_weights)
                            ):
                                valid = numpy.isfinite(raw_points).all(axis=1) & numpy.isfinite(raw_weights)
                                valid &= raw_weights > 0
                                diffusion_points = raw_points[valid]
                                diffusion_weights = raw_weights[valid]
                        except (TypeError, ValueError):
                            diffusion_points = numpy.empty((0, 3), dtype=numpy.float64)
                            diffusion_weights = numpy.empty((0,), dtype=numpy.float64)
                    result.append({
                        "lod": lod_name,
                        "unique_str": str(unique_str),
                        "local_vg_id": int(local),
                        "bone": bone,
                        "weighted_vertex_count": count,
                        "centroid": centroid,
                        "spread": spread,
                        "diffusion_points": diffusion_points,
                        "diffusion_weights": diffusion_weights,
                    })
            return result

        flattened = {lod: _flatten(lod) for lod in lod_names}
        counts = {lod: len(items) for lod, items in flattened.items()}
        reference_candidates = flattened.get(reference_lod, [])

        # 先建立子网格/节点级对应。跨 LOD 文件夹名是不同 hash，不能直接拼接；
        # 部件整体中心通常比单个骨骼中心稳定，也能避免两个局部骨骼恰好共心时
        # 被分到不同部件。
        part_candidates = {}
        for lod_name, candidates_for_lod in flattened.items():
            by_part = {}
            for candidate in candidates_for_lod:
                by_part.setdefault(candidate["unique_str"], []).append(candidate)
            part_candidates[lod_name] = by_part

        def _part_descriptor(items):
            """建立部件级点云描述，避免把不同部件的局部组放到同一候选池。"""
            center_items = [item for item in items if item["centroid"] is not None]
            center = None
            total_weight = 0.0
            if center_items:
                center_weights = numpy.asarray([
                    max(int(item["weighted_vertex_count"]), 1)
                    for item in center_items
                ], dtype=numpy.float64)
                center_points = numpy.stack([
                    item["centroid"] for item in center_items
                ]).astype(numpy.float64)
                center = numpy.average(center_points, axis=0, weights=center_weights)
                total_weight = float(center_weights.sum())

            point_chunks = []
            weight_chunks = []
            for item in items:
                points = numpy.asarray(item.get("diffusion_points", []), dtype=numpy.float64)
                weights = numpy.asarray(item.get("diffusion_weights", []), dtype=numpy.float64)
                if (
                    points.ndim == 2 and points.shape[1] == 3
                    and weights.ndim == 1 and len(points) == len(weights)
                    and len(points) > 0
                ):
                    # 每个局部组最多贡献 64 个点，部件级匹配只需要形状指纹，
                    # 不把全部平面点云复制进 11×11 的比较矩阵。
                    stride = max(int(numpy.ceil(len(points) / 64.0)), 1)
                    points = points[::stride][:64]
                    weights = weights[::stride][:64]
                    valid = numpy.isfinite(points).all(axis=1) & numpy.isfinite(weights)
                    valid &= weights > 0
                    if numpy.any(valid):
                        point_chunks.append(points[valid])
                        weight_chunks.append(weights[valid])
                elif item["centroid"] is not None:
                    point_chunks.append(numpy.asarray(item["centroid"], dtype=numpy.float64)[None, :])
                    weight_chunks.append(numpy.asarray([
                        max(int(item["weighted_vertex_count"]), 1)
                    ], dtype=numpy.float64))

            if point_chunks:
                points = numpy.concatenate(point_chunks, axis=0)
                weights = numpy.concatenate(weight_chunks, axis=0)
                if len(points) > 512:
                    sample_idx = numpy.linspace(0, len(points) - 1, 512, dtype=numpy.int64)
                    points = points[sample_idx]
                    weights = weights[sample_idx]
                if center is None:
                    center = numpy.average(points, axis=0, weights=weights)
                total_weight = max(total_weight, float(weights.sum()))
                bbox_min = numpy.min(points, axis=0)
                bbox_max = numpy.max(points, axis=0)
            elif center is not None:
                points = center[None, :].astype(numpy.float64)
                weights = numpy.asarray([max(total_weight, 1.0)], dtype=numpy.float64)
                bbox_min = center.copy()
                bbox_max = center.copy()
            else:
                points = numpy.empty((0, 3), dtype=numpy.float64)
                weights = numpy.empty((0,), dtype=numpy.float64)
                bbox_min = None
                bbox_max = None

            return {
                "center": center,
                "count": len(items),
                "weight": total_weight,
                "points": points,
                "weights": weights,
                "bbox_min": bbox_min,
                "bbox_max": bbox_max,
            }

        part_descriptors = {
            lod_name: {
                unique_str: _part_descriptor(items)
                for unique_str, items in by_part.items()
            }
            for lod_name, by_part in part_candidates.items()
        }

        def _part_cloud_distance(source, target):
            source_points = source["points"]
            target_points = target["points"]
            if len(source_points) == 0 or len(target_points) == 0:
                return None

            def nearest_median(points_a, points_b):
                distances = []
                for start in range(0, len(points_a), 64):
                    chunk = points_a[start:start + 64]
                    delta = chunk[:, None, :] - points_b[None, :, :]
                    squared = numpy.sum(delta * delta, axis=2)
                    distances.extend(numpy.sqrt(numpy.min(squared, axis=1)).tolist())
                return float(numpy.median(numpy.asarray(distances, dtype=numpy.float64)))

            return 0.5 * (
                nearest_median(source_points, target_points)
                + nearest_median(target_points, source_points)
            )

        def _part_pair_score(source_lod, source, target_lod, target):
            source_desc = part_descriptors[source_lod][source]
            target_desc = part_descriptors[target_lod][target]
            center_distance = None
            if source_desc["center"] is not None and target_desc["center"] is not None:
                center_distance = float(numpy.linalg.norm(
                    source_desc["center"] - target_desc["center"]
                ))
                if center_distance > max(float(centroid_tolerance) * 8.0, 0.75):
                    return None

            cloud_distance = _part_cloud_distance(source_desc, target_desc)
            bbox_gap = 0.0
            if (
                source_desc["bbox_min"] is not None
                and source_desc["bbox_max"] is not None
                and target_desc["bbox_min"] is not None
                and target_desc["bbox_max"] is not None
            ):
                gap_vec = numpy.maximum(
                    numpy.maximum(source_desc["bbox_min"] - target_desc["bbox_max"],
                                  target_desc["bbox_min"] - source_desc["bbox_max"]),
                    0.0,
                )
                bbox_gap = float(numpy.linalg.norm(gap_vec))

            source_extent = (
                numpy.zeros(3, dtype=numpy.float64)
                if source_desc["bbox_min"] is None
                else source_desc["bbox_max"] - source_desc["bbox_min"]
            )
            target_extent = (
                numpy.zeros(3, dtype=numpy.float64)
                if target_desc["bbox_min"] is None
                else target_desc["bbox_max"] - target_desc["bbox_min"]
            )
            extent_scale = max(float(numpy.linalg.norm(source_extent)),
                               float(numpy.linalg.norm(target_extent)), 0.05)
            extent_error = float(numpy.linalg.norm(source_extent - target_extent)) / extent_scale
            count_ratio = max(
                source_desc["count"] / max(target_desc["count"], 1),
                target_desc["count"] / max(source_desc["count"], 1),
            )
            count_term = min(float(numpy.log(max(count_ratio, 1.0))), 4.0) * 0.05
            # 点云形状是主判据；中心/bbox/部件数量只做稳定器，防止两个部件
            # 恰好整体中心接近时被错误交换。
            score = 0.0 if cloud_distance is None else cloud_distance
            score += bbox_gap * 0.25
            score += (0.0 if center_distance is None else center_distance * 0.10)
            score += extent_error * 0.05 + count_term
            return score

        def _match_parts(source_lod, target_lod):
            source_parts = sorted(part_descriptors.get(source_lod, {}).keys())
            target_parts = sorted(part_descriptors.get(target_lod, {}).keys())
            rows = []
            for source in source_parts:
                for target in target_parts:
                    score = _part_pair_score(source_lod, source, target_lod, target)
                    if score is not None:
                        rows.append((score, source, target))
            rows.sort(key=lambda row: (row[0], row[1], row[2]))

            # 11×11 规模使用精确的一对一最小代价分配，避免贪心先占用
            # 一个近似部件后把后续部件错配；部件数量不等时退化为贪心。
            score_by_pair = {(source, target): score for score, source, target in rows}
            if len(source_parts) == len(target_parts) and len(source_parts) <= 12:
                states = {0: (0.0, [])}
                for source_index, source in enumerate(source_parts):
                    next_states = {}
                    for mask, (total, selected) in states.items():
                        for target_index, target in enumerate(target_parts):
                            if mask & (1 << target_index):
                                continue
                            score = score_by_pair.get((source, target))
                            if score is None:
                                continue
                            new_mask = mask | (1 << target_index)
                            candidate = (total + score, selected + [(source, target, score)])
                            previous = next_states.get(new_mask)
                            if previous is None or candidate[0] < previous[0]:
                                next_states[new_mask] = candidate
                    states = next_states
                full_mask = (1 << len(target_parts)) - 1
                if full_mask in states:
                    selected = states[full_mask][1]
                    return (
                        {source: target for source, target, _score in selected},
                        {(source, target): float(score) for source, target, score in selected},
                    )

            used_source, used_target = set(), set()
            mapping = {}
            selected_scores = {}
            for _score, source, target in rows:
                if source in used_source or target in used_target:
                    continue
                used_source.add(source)
                used_target.add(target)
                mapping[source] = target
                selected_scores[(source, target)] = float(_score)
            return mapping, selected_scores

        def _pair_score(left: dict, right: dict):
            matrix_diff = float(numpy.max(numpy.abs(left["bone"] - right["bone"])))
            if matrix_diff >= float(match_tolerance):
                return None
            # 没有任何权重中心时不能靠宽松的跨 LOD 矩阵门槛硬配；这类数据
            # 只有逐位矩阵才足够安全，否则把“缺少几何证据”当成对应关系。
            if left["centroid"] is None or right["centroid"] is None:
                if matrix_diff >= 1e-3:
                    return None
            centroid_distance = None
            if left["centroid"] is not None and right["centroid"] is not None:
                centroid_distance = float(numpy.linalg.norm(
                    left["centroid"] - right["centroid"]
                ))
            # 同一根骨骼在两个 LOD 的大平面/附件顶点数可能相差很大；权重数量
            # 只用于同分候选的轻微排序，不作为硬门槛。
            weight_ratio = 0.0
            if left["weighted_vertex_count"] > 0 and right["weighted_vertex_count"] > 0:
                ratio = max(
                    left["weighted_vertex_count"] / right["weighted_vertex_count"],
                    right["weighted_vertex_count"] / left["weighted_vertex_count"],
                )
                weight_ratio = min(float(numpy.log(max(ratio, 1.0))), 4.0)
            center_term = 0.0
            if centroid_distance is not None:
                center_term = min(
                    centroid_distance / max(float(centroid_tolerance), 1e-6),
                    8.0,
                )
                # 这里已经限定在同一个“部件节点”内；部件可能是大平面，
                # 同一根骨骼的散落附件中心相距很远，因此不能再用绝对中心距
                # 做硬拒绝，只把它作为候选排序项。
            # 跨 LOD 以加权中心为第一排序键，矩阵只做次级稳定器；不能把
            # LOD0/LOD1 的捕获姿态差异误当成不同骨骼的硬拒绝。
            score = center_term + matrix_diff / max(float(match_tolerance), 1e-8) * 0.05
            score += weight_ratio * 0.03
            return score, matrix_diff, centroid_distance

        protected_by_lod: dict[str, set[tuple[tuple[str, int], tuple[str, int]]]] = {
            lod: set() for lod in lod_names
        }
        all_matches = []
        part_matches = []

        for target_lod in lod_names:
            if target_lod == reference_lod:
                continue
            target_candidates = flattened.get(target_lod, [])
            reference_part_to_target, part_pair_scores = _match_parts(
                reference_lod, target_lod
            )
            for (reference_part, target_part), score in sorted(part_pair_scores.items()):
                part_matches.append({
                    "reference_lod": reference_lod,
                    "target_lod": target_lod,
                    "reference_unique_str": reference_part,
                    "target_unique_str": target_part,
                    "score": float(score),
                    "reference_group_count": len(part_candidates.get(reference_lod, {}).get(reference_part, [])),
                    "target_group_count": len(part_candidates.get(target_lod, {}).get(target_part, [])),
                })
            pair_rows = []
            for ref in reference_candidates:
                for target in target_candidates:
                    if reference_part_to_target.get(ref["unique_str"]) != target["unique_str"]:
                        continue
                    scored = _pair_score(ref, target)
                    if scored is None:
                        continue
                    score, matrix_diff, centroid_distance = scored
                    pair_rows.append((
                        score,
                        ref,
                        target,
                        matrix_diff,
                        centroid_distance,
                    ))

            # 先做稳定的一对一主匹配：一个目标候选不能被两个参考候选抢走。
            # 额外目标候选随后仍可通过各自最佳参考建立“LOD1 多于 LOD0”的关系，
            # 但不会因此制造错误的保护边。
            pair_rows.sort(key=lambda row: (
                row[0],
                -row[1]["weighted_vertex_count"],
                -row[2]["weighted_vertex_count"],
                row[1]["unique_str"],
                row[1]["local_vg_id"],
                row[2]["unique_str"],
                row[2]["local_vg_id"],
            ))
            used_ref = set()
            used_target = set()
            primary = []
            for row in pair_rows:
                ref, target = row[1], row[2]
                ref_key = (ref["unique_str"], ref["local_vg_id"])
                target_key = (target["unique_str"], target["local_vg_id"])
                if ref_key in used_ref or target_key in used_target:
                    continue
                used_ref.add(ref_key)
                used_target.add(target_key)
                primary.append(row)

            # 每个目标候选都保留一个最佳参考（包括额外候选）。
            best_ref_for_target = {}
            for row in pair_rows:
                target = row[2]
                target_key = (target["unique_str"], target["local_vg_id"])
                if target_key not in best_ref_for_target:
                    best_ref_for_target[target_key] = row

            target_rows_by_ref: dict[tuple[str, int], list[dict]] = {}
            for row in pair_rows:
                ref, target = row[1], row[2]
                ref_key = (ref["unique_str"], ref["local_vg_id"])
                target_rows_by_ref.setdefault(ref_key, []).append(target)
            for ref_key in target_rows_by_ref:
                target_rows_by_ref[ref_key].sort(
                    key=lambda item: (item["unique_str"], item["local_vg_id"])
                )

            best_target_for_ref = {}
            for row in pair_rows:
                ref = row[1]
                ref_key = (ref["unique_str"], ref["local_vg_id"])
                if ref_key not in best_target_for_ref:
                    best_target_for_ref[ref_key] = row

            primary_target_for_ref = {
                (row[1]["unique_str"], row[1]["local_vg_id"]): row[2]
                for row in primary
            }
            primary_ref_for_target = {
                (row[2]["unique_str"], row[2]["local_vg_id"]): row[1]
                for row in primary
            }

            def _cross_side_is_distinct(left: dict, right: dict, threshold: float) -> bool:
                """判断另一侧是否提供了足够强的“拆分证据”。

                中心相距很远本身不能拆分：同一根骨骼可以同时驱动大平面和
                散落附件。只有另一侧的矩阵也出现明显分离时才回传保护边；
                这样跨 LOD 姿态的小幅矩阵变化不会把正常的重复骨骼全部拆散。
                """
                return float(numpy.max(numpy.abs(left["bone"] - right["bone"]))) >= float(threshold)

            def _current_side_can_merge(left: dict, right: dict) -> bool:
                # 保护边只约束本侧原本有机会通过矩阵硬门的候选；不同矩阵
                # 已经由 build_vg_maps 拒绝，不应让跨 LOD 层扩大拆分范围。
                return float(numpy.max(numpy.abs(left["bone"] - right["bone"]))) < 1e-3

            # 参考侧有两个候选，而目标侧分别存在两个可区分候选时，禁止参考
            # 侧把它们并成一组；反过来也一样。这正是“用另一侧矩阵补齐”
            # 的约束来源。
            for ref_a_index in range(len(reference_candidates)):
                ref_a = reference_candidates[ref_a_index]
                ref_a_key = (ref_a["unique_str"], ref_a["local_vg_id"])
                target_a = primary_target_for_ref.get(ref_a_key)
                if target_a is None:
                    continue
                target_a_key = (target_a["unique_str"], target_a["local_vg_id"])
                for ref_b in reference_candidates[ref_a_index + 1:]:
                    ref_b_key = (ref_b["unique_str"], ref_b["local_vg_id"])
                    target_b = primary_target_for_ref.get(ref_b_key)
                    if target_b is None:
                        continue
                    target_b_key = (target_b["unique_str"], target_b["local_vg_id"])
                    if (
                        target_a_key != target_b_key
                        and _current_side_can_merge(ref_a, ref_b)
                        # LOD0 是基准，只有 LOD1 矩阵出现明显分叉才拆它，
                        # 避免跨帧姿态差异把基准侧拆得过碎。
                        and _cross_side_is_distinct(target_a, target_b, 0.75)
                    ):
                        protected_by_lod[reference_lod].add(
                            tuple(sorted((ref_a_key, ref_b_key)))
                        )

            target_to_refs: dict[tuple[str, int], list[dict]] = {}
            for row in pair_rows:
                target = row[2]
                target_key = (target["unique_str"], target["local_vg_id"])
                ref = row[1]
                target_to_refs.setdefault(target_key, []).append(ref)
            for target_key, refs in target_to_refs.items():
                refs = sorted(refs, key=lambda item: (item["unique_str"], item["local_vg_id"]))
                for ref_a_index in range(len(refs)):
                    ref_a = refs[ref_a_index]
                    ref_a_key = (ref_a["unique_str"], ref_a["local_vg_id"])
                    for ref_b in refs[ref_a_index + 1:]:
                        ref_b_key = (ref_b["unique_str"], ref_b["local_vg_id"])
                        # 一个目标候选对应多个参考候选，不能在目标侧无中生有
                        # 地拆分；只有目标侧本身存在不同候选时才添加保护边，
                        # 该情况由下面的反向扫描覆盖。
                        if ref_a_key == ref_b_key:
                            continue

            for target_a_index in range(len(target_candidates)):
                target_a = target_candidates[target_a_index]
                target_a_key = (target_a["unique_str"], target_a["local_vg_id"])
                ref_a = primary_ref_for_target.get(target_a_key)
                if ref_a is None:
                    continue
                ref_a_key = (ref_a["unique_str"], ref_a["local_vg_id"])
                for target_b in target_candidates[target_a_index + 1:]:
                    target_b_key = (target_b["unique_str"], target_b["local_vg_id"])
                    ref_b = primary_ref_for_target.get(target_b_key)
                    if ref_b is None:
                        continue
                    ref_b_key = (ref_b["unique_str"], ref_b["local_vg_id"])
                    if (
                        ref_a_key != ref_b_key
                        and _current_side_can_merge(target_a, target_b)
                        # LOD1 允许比 LOD0 更细；当参考侧能区分时优先保留
                        # 目标侧的细分，确保目标组数不会因独立去重而变少。
                        and _cross_side_is_distinct(ref_a, ref_b, 0.24)
                    ):
                        protected_by_lod[target_lod].add(
                            tuple(sorted((target_a_key, target_b_key)))
                        )

            for row in primary:
                ref, target = row[1], row[2]
                all_matches.append({
                    "reference_lod": reference_lod,
                    "target_lod": target_lod,
                    "reference_unique_str": ref["unique_str"],
                    "reference_local_vg_id": int(ref["local_vg_id"]),
                    "target_unique_str": target["unique_str"],
                    "target_local_vg_id": int(target["local_vg_id"]),
                    "reference_component": ref["unique_str"],
                    "target_component": target["unique_str"],
                    "component_score": float(part_pair_scores.get((
                        ref["unique_str"], target["unique_str"]
                    ), 0.0)),
                    "score": float(row[0]),
                    "matrix_diff": float(row[3]),
                    "centroid_distance": (
                        None if row[4] is None else float(row[4])
                    ),
                })

        unmatched_reference = []
        matched_ref_keys = {
            (item["reference_unique_str"], item["reference_local_vg_id"])
            for item in all_matches
        }
        for candidate in reference_candidates:
            key = (candidate["unique_str"], candidate["local_vg_id"])
            if key not in matched_ref_keys:
                unmatched_reference.append({
                    "unique_str": candidate["unique_str"],
                    "local_vg_id": int(candidate["local_vg_id"]),
                })

        unmatched_by_lod = {}
        for lod_name in lod_names:
            if lod_name == reference_lod:
                continue
            matched_targets = {
                (item["target_unique_str"], item["target_local_vg_id"])
                for item in all_matches if item["target_lod"] == lod_name
            }
            unmatched_by_lod[lod_name] = [
                {
                    "unique_str": candidate["unique_str"],
                    "local_vg_id": int(candidate["local_vg_id"]),
                }
                for candidate in flattened.get(lod_name, [])
                if (candidate["unique_str"], candidate["local_vg_id"]) not in matched_targets
            ]

        serialized_protected = {}
        for lod_name, pairs in protected_by_lod.items():
            serialized_protected[lod_name] = [
                [list(left), list(right)]
                for left, right in sorted(pairs)
            ]
        matched_part_pairs = {
            (row["reference_unique_str"], row["target_unique_str"])
            for row in part_matches
        }
        matched_reference_parts = {row[0] for row in matched_part_pairs}
        matched_target_parts = {row[1] for row in matched_part_pairs}
        return {
            "reference_lod": reference_lod,
            "part_matches": part_matches,
            "unmatched_reference_parts": [
                part for part in sorted(part_candidates.get(reference_lod, {}))
                if part not in matched_reference_parts
            ],
            "unmatched_target_parts": {
                lod_name: [
                    part for part in sorted(part_candidates.get(lod_name, {}))
                    if part not in matched_target_parts
                ]
                for lod_name in lod_names if lod_name != reference_lod
            },
            "matches": all_matches,
            "protected_pairs": protected_by_lod,
            "protected_pairs_json": serialized_protected,
            "counts": counts,
            "unmatched_reference": unmatched_reference,
            "unmatched_by_lod": unmatched_by_lod,
        }

    @staticmethod
    def _build_cross_lod_constraint_labels(
        correspondence: dict,
        lod_name: str,
        vg_maps: dict[str, dict],
    ) -> dict[tuple[str, int], tuple]:
        """从另一侧当前 global group 生成本侧下一轮的单调约束标签。"""
        labels = {}
        reference_lod = correspondence.get("reference_lod", "LOD0")
        for row in correspondence.get("matches", []) or []:
            if lod_name == reference_lod:
                unique_str = row.get("reference_unique_str", "")
                local_id = int(row.get("reference_local_vg_id", 0) or 0)
                other_unique = row.get("target_unique_str", "")
                other_local = int(row.get("target_local_vg_id", 0) or 0)
                other_lod = row.get("target_lod", "")
            else:
                if row.get("target_lod") != lod_name:
                    continue
                unique_str = row.get("target_unique_str", "")
                local_id = int(row.get("target_local_vg_id", 0) or 0)
                other_unique = row.get("reference_unique_str", "")
                other_local = int(row.get("reference_local_vg_id", 0) or 0)
                other_lod = row.get("reference_lod", reference_lod)
            other_map = vg_maps.get(other_unique, {})
            if other_local not in other_map and str(other_local) not in other_map:
                continue
            other_group = other_map.get(other_local, other_map.get(str(other_local)))
            labels[(str(unique_str), local_id)] = (str(other_lod), int(other_group))
        return labels


class EFMISkeletonMergeHelper:
    """EFMI 骨骼合并总流程：定位 FrameAnalysis -> 解析 log -> 构建映射 -> 写回工作空间。"""

    @staticmethod
    def _atomic_publish_cache(
        source_path: str,
        dest_path: str,
        *,
        vg_count: int = 0,
        min_size: int = 4,
    ) -> None:
        """原子刷新运行时缓存，并在提交前校验完整性。

        重建事务不能复用“同尺寸即最新”的旧缓存；源、目标不同时始终复制到同目录
        临时文件，校验通过后再 ``os.replace``。源就是目标（dump 已删除后的缓存回退）
        时只做完整性校验，避免自拷贝破坏唯一副本。
        """
        source_path = os.path.abspath(str(source_path or ""))
        dest_path = os.path.abspath(str(dest_path or ""))
        if not source_path or not os.path.isfile(source_path):
            raise FileNotFoundError(f"骨骼缓存源文件不存在: {source_path}")

        def _validate(path: str) -> None:
            size = os.path.getsize(path)
            if size < int(min_size) or size % 4 != 0:
                raise OSError(f"缓存文件大小无效: {path} ({size} bytes)")
            if not EFMIBoneMapBuilder.cache_file_size_ok(path, int(vg_count)):
                raise OSError(
                    f"缓存文件不足以容纳 {int(vg_count)} 根骨骼: {path} ({size} bytes)"
                )

        if source_path == dest_path:
            _validate(source_path)
            return

        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(dest_path)}.",
            suffix=".tmp",
            dir=os.path.dirname(dest_path),
        )
        os.close(fd)
        try:
            shutil.copy2(source_path, temp_path)
            _validate(temp_path)
            os.replace(temp_path, dest_path)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

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
    def _parse_lod_name(unique_str: str) -> str:
        """解析 unique_str 的 LOD 前缀（'LOD0.xxx' -> 'LOD0'；无前缀 -> ''）。

        与 WorkSpaceHelper.parse_lod_unique_str 语义一致，但本模块需保持无 bpy
        依赖（单测以 stub 包加载），故本地实现。
        """
        normalized = str(unique_str or "").strip()
        if normalized.upper().startswith("LOD") and "." in normalized:
            dot_idx = normalized.index(".")
            potential = normalized[:dot_idx]
            if potential[3:].isdigit():
                return potential
        return ""

    @classmethod
    def resolve_frame_analysis_dirs_by_lod(
        cls, workspace_root: str
    ) -> tuple[dict[str, str], str]:
        """解析每 LOD 的 FrameAnalysis 目录映射 + 默认目录。

        多 LOD 语义（2026-08 实测定案）：不同 LOD 对应各自独立的 dump 提取目录。
        官方工具按工作页（tab）管理：Config/WorkPageTabs.json 的 tab.name 即 LOD 名
        （'LOD0'/'LOD1'），每个 tab 在 Config/Tabs/<tabid>.json 里记录自己的
        frameAnalysisFolderPath（该 LOD 的 dump 提取目录）；而工作空间级
        Config/FrameAnalysisPath.json 只记录"当前活动 tab"的路径——拿它喂所有 LOD
        会导致其它 LOD 全部查错目录（实测 LOD0 数据在 tab1 的 dump、LOD1 在 tab2 的
        dump，用任一单一路径都会漏掉另一侧）。

        返回 (lod_map, default_dir)：
        - lod_map: {tab/LOD 名 -> FrameAnalysis 目录}（仅收录目录有效的条目）；
        - default_dir: 现有 resolve_frame_analysis_dir 的解析结果
          （Config/FrameAnalysisPath.json -> Tabs 扫描 -> 最新 FrameAnalysis-* 兜底），
          供无 LOD 前缀的子网格与未在 tab 中登记的 LOD 使用。
        """
        lod_map: dict[str, str] = {}
        work_page_path = os.path.join(workspace_root, "Config", "WorkPageTabs.json")
        tabs_dir = os.path.join(workspace_root, "Config", "Tabs")
        if os.path.isfile(work_page_path) and os.path.isdir(tabs_dir):
            try:
                payload = JsonUtils.LoadFromFile(work_page_path)
                for tab in payload.get("tabs", []) or []:
                    tab_name = str(tab.get("name", "") or "").strip()
                    tab_id = str(tab.get("id", "") or "").strip()
                    if not tab_name or not tab_id:
                        continue
                    tab_file = os.path.join(tabs_dir, tab_id + ".json")
                    if not os.path.isfile(tab_file):
                        continue
                    try:
                        tab_payload = JsonUtils.LoadFromFile(tab_file)
                    except Exception:
                        continue
                    fa_path = str(
                        tab_payload.get("frameAnalysisFolderPath", "") or ""
                    ).strip()
                    if fa_path and os.path.isdir(fa_path):
                        lod_map[tab_name] = fa_path
            except Exception:
                pass
        default_dir = cls.resolve_frame_analysis_dir(workspace_root)
        return lod_map, default_dir

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
        lod_group_projection: bool = True,
    ) -> tuple[bool, str]:
        """为 EFMI 工作空间的子网格生成并写回骨骼合并数据（幂等）。

        多 LOD 语义：unique_str_list 按 LOD 前缀分组，每组用**自己的 dump** 解析
        原始骨骼候选；先按部件点云建立原始候选对应。lod_group_projection=True
        时只对 LOD0 执行一次权重扩散去重，再将分区投影到 LOD1；为 False 时
        两边各自独立执行去重。没有对应的额外 LOD1 候选保留恒等槽位。
        运行时槽位仍按 LOD 独立从 0 起，避免把不同 dump 的骨骼池混写；对应事实
        写入 EFMILODCorrespondence/EFMILOD* 字段，供导出和问题复核。

        返回 (是否成功, 描述)。
        """
        if not unique_str_list:
            return False, "没有子网格需要处理。"

        lod_map, default_dir = cls.resolve_frame_analysis_dirs_by_lod(workspace_root)
        if not default_dir and not lod_map:
            return False, (
                "未找到 FrameAnalysis 目录：请检查工作空间 "
                f"{os.path.join(workspace_root, 'Config', 'FrameAnalysisPath.json')}"
            )

        # 按 LOD 分组（同 LOD 内保持输入顺序）
        groups: dict[str, list[str]] = {}
        for unique_str in unique_str_list:
            groups.setdefault(cls._parse_lod_name(unique_str), []).append(unique_str)
        lod_group_projection = bool(lod_group_projection)

        # 多 LOD 先收集原始候选，再建立跨 LOD 对应。分组投影模式下 LOD0 是唯一
        # 执行权重扩散去重的基准侧；LOD1 只把该分区投影到自己的原始槽位，避免
        # 两侧各自计算后出现“同一对应关系被拆成两套 global group”的回退。独立
        # 模式仍使用同一份部件对应账本，但两侧分别调用 build_vg_maps。
        # 已有完整联合缓存时保持幂等，不重复读取大型 Position/Blend 缓冲。
        if len(groups) > 1:
            joint_cache_ready = True
            for group_list in groups.values():
                for unique_str in group_list:
                    json_path = cls._resolve_submesh_json_path(workspace_root, unique_str)
                    if not json_path:
                        # 任一输入子网格 json 缺失都必须使联合缓存失效：
                        # 缺失者会被跳过重建，其余子网格若仍幂等跳过，
                        # 两侧槽位口径将不一致。
                        joint_cache_ready = False
                        break
                    try:
                        payload = JsonUtils.LoadFromFile(json_path)
                    except Exception:
                        joint_cache_ready = False
                        break
                    if not isinstance(payload, dict):
                        joint_cache_ready = False
                        break
                    if not cls._efmi_cache_intact(payload, json_path, unique_str):
                        joint_cache_ready = False
                        break
                    try:
                        lod_layout_version = int(
                            payload.get("EFMILODLayoutVersion", 0) or 0
                        )
                    except (TypeError, ValueError):
                        lod_layout_version = 0
                    if lod_layout_version != _CROSS_LOD_LAYOUT_VERSION:
                        joint_cache_ready = False
                        break
                    if bool(payload.get("EFMILODProjection", False)) != lod_group_projection:
                        joint_cache_ready = False
                        break
                if not joint_cache_ready:
                    break
            if joint_cache_ready and not force:
                return True, "全部子网格已有跨 LOD 骨骼合并缓存（VGMap），无需重新生成。"

            collected_by_lod = {}
            group_meta_by_lod = {}
            for lod_name in sorted(groups.keys()):
                group_list = groups[lod_name]
                frame_analysis_dir = lod_map.get(lod_name) or default_dir
                label = lod_name or "工作空间根目录"
                if not frame_analysis_dir:
                    print(
                        f"[EFMI骨骼合并] {label}: 无 FrameAnalysis 目录，"
                        f"跳过 {len(group_list)} 个子网格"
                    )
                    collected_by_lod[lod_name] = {}
                    group_meta_by_lod[lod_name] = {}
                    continue
                log_path = os.path.join(frame_analysis_dir, "log.txt")
                if not os.path.isfile(log_path):
                    print(
                        f"[EFMI骨骼合并] {label}: FrameAnalysis 缺少 log.txt: {log_path}"
                    )
                    collected_by_lod[lod_name] = {}
                    group_meta_by_lod[lod_name] = {}
                    continue
                parser = EFMILogParser(log_path)
                collected, metadata, _skipped = cls._ensure_skeleton_data_for_group(
                    workspace_root=workspace_root,
                    unique_str_list=group_list,
                    parser=parser,
                    force=True,
                    collect_only=True,
                )
                collected_by_lod[lod_name] = collected
                group_meta_by_lod[lod_name] = metadata

            correspondence = EFMIBoneMapBuilder.build_cross_lod_correspondence(
                collected_by_lod,
                reference_lod="LOD0",
            )
            maps_by_lod = {}
            offsets_by_lod = {}
            reference_lod = correspondence.get("reference_lod", "")
            reference_skeletons = collected_by_lod.get(reference_lod, {})
            if lod_group_projection:
                reference_maps, reference_offsets = EFMIBoneMapBuilder.build_vg_maps(
                    reference_skeletons,
                ) if reference_skeletons else ({}, {})
                maps_by_lod[reference_lod] = reference_maps
                offsets_by_lod[reference_lod] = reference_offsets
                for lod_name, skeletons in collected_by_lod.items():
                    if lod_name == reference_lod:
                        continue
                    if not skeletons:
                        maps_by_lod[lod_name] = {}
                        offsets_by_lod[lod_name] = {}
                        continue
                    maps_by_lod[lod_name], offsets_by_lod[lod_name] = (
                        EFMIBoneMapBuilder.build_lod_maps_from_reference(
                            reference_skeletons,
                            reference_maps,
                            skeletons,
                            correspondence,
                            reference_lod=reference_lod,
                            target_lod=lod_name,
                        )
                    )
            else:
                # 诊断/兼容模式：保留跨部件点云对应账本，但两侧不共享分区，
                # 各自使用原有权重扩散判定独立生成 VGMap。
                for lod_name, skeletons in collected_by_lod.items():
                    if not skeletons:
                        maps_by_lod[lod_name] = {}
                        offsets_by_lod[lod_name] = {}
                        continue
                    maps_by_lod[lod_name], offsets_by_lod[lod_name] = (
                        EFMIBoneMapBuilder.build_vg_maps(skeletons)
                    )

            provisional_counts = {
                lod_name: len({
                    int(global_id)
                    for local_map in maps.values()
                    for global_id in local_map.values()
                })
                for lod_name, maps in maps_by_lod.items()
            }
            reference_lod = correspondence.get("reference_lod", "")
            baseline_group_count = int(provisional_counts.get(reference_lod, 0) or 0)
            correspondence["baseline_group_count"] = baseline_group_count
            correspondence["group_count_by_lod"] = dict(provisional_counts)
            correspondence["projection_enabled"] = lod_group_projection

            group_results: list[str] = []
            total_written = 0
            total_skipped = 0
            for lod_name in sorted(groups.keys()):
                group_list = groups[lod_name]
                frame_analysis_dir = lod_map.get(lod_name) or default_dir
                label = lod_name or "工作空间根目录"
                if not frame_analysis_dir or not os.path.isfile(
                    os.path.join(frame_analysis_dir, "log.txt")
                ):
                    # 不可用的 LOD 必须显式进入结果，不能静默 continue——
                    # 否则外层会把部分完成误判为全部完成。
                    group_results.append(
                        f"{label}: FrameAnalysis 不可用，{len(group_list)} 个目标未生成骨骼数据"
                    )
                    continue
                parser = EFMILogParser(os.path.join(frame_analysis_dir, "log.txt"))
                written, skipped, message = cls._ensure_skeleton_data_for_group(
                    workspace_root=workspace_root,
                    unique_str_list=group_list,
                    parser=parser,
                    force=True,
                    vg_maps_override=maps_by_lod.get(lod_name),
                    vg_offsets_override=offsets_by_lod.get(lod_name),
                    cross_lod_info=correspondence,
                )
                total_written += written
                total_skipped += skipped
                group_results.append(f"{label}: {message}")

            expected_targets = len(set(unique_str_list))
            processed = total_written + total_skipped
            if processed == 0:
                if group_results:
                    return False, "没有子网格成功生成骨骼数据（" + "；".join(group_results) + "）"
                return False, "没有子网格成功生成骨骼数据。"
            message = "；".join(group_results)
            if processed < expected_targets:
                message += f"；共 {expected_targets - processed} 个目标未生成骨骼数据"
            return processed == expected_targets, message

        group_results: list[str] = []
        total_written = 0
        total_skipped = 0
        for lod_name in sorted(groups.keys()):
            group_list = groups[lod_name]
            frame_analysis_dir = lod_map.get(lod_name) or default_dir
            label = lod_name or "工作空间根目录"
            if not frame_analysis_dir:
                print(
                    f"[EFMI骨骼合并] {label}: 无 FrameAnalysis 目录，"
                    f"跳过 {len(group_list)} 个子网格"
                )
                group_results.append(
                    f"{label}: FrameAnalysis 不可用，{len(group_list)} 个目标未生成骨骼数据"
                )
                continue
            log_path = os.path.join(frame_analysis_dir, "log.txt")
            if not os.path.isfile(log_path):
                print(
                    f"[EFMI骨骼合并] {label}: FrameAnalysis 缺少 log.txt: {log_path}"
                )
                group_results.append(
                    f"{label}: FrameAnalysis 缺少 log.txt，{len(group_list)} 个目标未生成骨骼数据"
                )
                continue
            parser = EFMILogParser(log_path)
            written, skipped, message = cls._ensure_skeleton_data_for_group(
                workspace_root=workspace_root,
                unique_str_list=group_list,
                parser=parser,
                force=force,
            )
            total_written += written
            total_skipped += skipped
            group_results.append(f"{label}: {message}")

        expected_targets = len(set(unique_str_list))
        processed = total_written + total_skipped
        if processed == 0:
            if group_results:
                return False, "没有子网格成功生成骨骼数据（" + "；".join(group_results) + "）"
            return False, "没有子网格成功生成骨骼数据。"
        message = "；".join(group_results)
        if processed < expected_targets:
            message += f"；共 {expected_targets - processed} 个目标未生成骨骼数据"
        # 全部命中缓存（无新写入）也视为成功；但只要存在未处理目标就必须失败
        return processed == expected_targets, message

    @classmethod
    def _ensure_skeleton_data_for_group(
        cls,
        workspace_root: str,
        unique_str_list: list[str],
        parser: EFMILogParser,
        force: bool = False,
        protected_pairs: set[tuple[tuple[str, int], tuple[str, int]]] | None = None,
        constraint_labels: dict[tuple[str, int], object] | None = None,
        vg_maps_override: dict[str, dict] | None = None,
        vg_offsets_override: dict[str, int] | None = None,
        collect_only: bool = False,
        cross_lod_info: dict | None = None,
    ) -> tuple[int, int, str] | tuple[dict[str, tuple], dict[str, dict], int]:
        """为单个 LOD 组的子网格生成并写回骨骼合并数据（幂等）。

        返回 (written, skipped, 描述消息)；vg_offsets 在该组内从 0 起分配，
        与其它 LOD 组完全独立。collect_only=True 时只返回原始候选和元数据，
        不写文件，供跨 LOD 对应阶段使用。
        """
        if not unique_str_list:
            return 0, 0, "没有子网格需要处理。"

        # 收集每个子网格的信息
        submesh_skeletons: dict[str, tuple] = {}
        submesh_meta: dict[str, dict] = {}
        skipped = 0

        # 幂等门控（整组原子语义）：单 LOD 组内所有子网格共享同一次 build_vg_maps
        # 分配的全局槽位，绝不允许“部分子网格用缓存、部分子网格重算”——重算的
        # 子网格会从槽位 0 重新编号，与缓存子网格的 VGOffset 碰撞（实测复现：
        # A VGOffset=0、B VGOffset=1，仅 B 失效后 B 被单独重算成 VGOffset=0）。
        # 因此：全部缓存完整才整批跳过；任一子网格缓存失效则整组全部重算。
        resolved_cache: dict[str, bool] = {}
        for unique_str in unique_str_list:
            json_path = cls._resolve_submesh_json_path(workspace_root, unique_str)
            if not json_path:
                # 目标无法定位必须记为缓存失效：否则"全部已缓存"的批跳过
                # 会把该目标静默遗漏、误报整组缓存完整。
                resolved_cache[unique_str] = False
                continue
            try:
                cached_json = JsonUtils.LoadFromFile(json_path)
            except Exception:
                resolved_cache[unique_str] = False
                continue
            if not isinstance(cached_json, dict):
                resolved_cache[unique_str] = False
                continue
            resolved_cache[unique_str] = cls._efmi_cache_intact(
                cached_json, json_path, unique_str
            )
        if (
            not force
            and not collect_only
            and resolved_cache
            and all(resolved_cache.values())
        ):
            skipped = len(resolved_cache)
            return 0, skipped, (
                f"全部 {skipped} 个子网格已有骨骼合并缓存（VGMap），无需重新生成。"
            )

        seen_targets: set[str] = set()
        for unique_str in unique_str_list:
            if unique_str in seen_targets:
                continue
            seen_targets.add(unique_str)
            json_path = cls._resolve_submesh_json_path(workspace_root, unique_str)
            if not json_path:
                print(f"[EFMI骨骼合并] 跳过 {unique_str}: 未找到子网格 json")
                continue

            submesh_json = JsonUtils.LoadFromFile(json_path)
            if not isinstance(submesh_json, dict):
                continue

            # 整组原子语义下这里不再单独跳过：组内任一子网格缓存失效时
            # 全组重算（见上方门控注释）。存在旧缓存但失效的子网格打印原因，
            # 方便排查（版本过期 / schema 缺字段 / BoneMatrix 文件缺失等）。
            if submesh_json.get("VGMap"):
                print(
                    f"[EFMI骨骼合并] {unique_str}: VGMap 缓存不完整或版本失效，"
                    f"按算法版本 {_VG_MAP_ALGORITHM_VERSION} 整组重算"
                )

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

            # 取第一个有有效骨骼数据的 drawcall，并记录实际成功的 draw_index。
            # 后备 drawcall 成功后元数据仍必须指向它——骨骼池复制（vs-t0）按
            # draw_index 反查，指向第一个失败的候选会拿到错误骨骼池或根本没有。
            skeleton_buffer = None
            skeleton_draw_index = None
            for draw_index in drawcall_index_list:
                candidate_buffer = EFMIBoneMapBuilder(parser).get_skeleton_buffer(draw_index)
                if candidate_buffer is not None:
                    skeleton_buffer = candidate_buffer
                    skeleton_draw_index = draw_index
                    break

            if skeleton_buffer is None or skeleton_draw_index is None:
                print(f"[EFMI骨骼合并] 跳过 {unique_str}: 无法从 FrameAnalysis 读取骨骼数据")
                continue

            try:
                blend_indices = EFMIBoneMapBuilder.parse_blendindices_from_buf(blend_buf_path, element_info)
                blend_layout = EFMIBoneMapBuilder.parse_blend_layout(submesh_json)
                blend_weights = EFMIBoneMapBuilder.parse_blendweights_from_buf(
                    blend_buf_path, blend_layout
                )
            except Exception as e:
                print(f"[EFMI骨骼合并] 跳过 {unique_str}: 读取 Blend.buf 失败: {e}")
                continue

            # vg_count 复用与 ZZMI 相同的有效通道判定（按数据格式排除哨兵 +
            # 正权重过滤）：u1 的 0xFF / u2 的 0xFFFF / u4|i4 的 0xFFFFFFFF
            # 都不再被算成真实骨骼。
            valid_mask = EFMIBoneMapBuilder.valid_blend_channels(
                blend_indices, element_info, blend_weights
            )
            valid_indices = blend_indices[valid_mask].astype(numpy.int64)
            if len(valid_indices) == 0:
                print(f"[EFMI骨骼合并] 跳过 {unique_str}: BLENDINDICES 无有效通道")
                continue
            vg_count = int(valid_indices.max()) + 1

            # 先与骨骼段长度比对，再 bincount——bincount 的 minlength=vg_count
            # 会按 vg_count 分配内存，损坏数据（如未被哨兵覆盖的巨值索引）
            # 必须在这里被骨骼段上限拦截，否则可能尝试分配数十 GB。
            if len(skeleton_buffer) < vg_count:
                print(
                    f"[EFMI骨骼合并] 跳过 {unique_str}: 骨骼段 {len(skeleton_buffer)} < 顶点组 {vg_count}"
                )
                continue
            weighted_vertex_counts = numpy.bincount(valid_indices, minlength=vg_count)

            # 权重扩散去重签名（读 Position.buf + Blend.buf 计算每骨骼的
            # 正权重采样场；质心/包围盒仅作回退与剪枝）
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
                # 实际成功读到骨骼数据的 draw_index（后备 drawcall 成功时
                # 绝不能回落到 drawcall_index_list[0] 的失败候选）
                "draw_index": skeleton_draw_index,
            }

        if collect_only:
            # 联合 LOD 流程需要保留去重前的完整候选；这里不写任何缓存。
            return submesh_skeletons, submesh_meta, skipped

        if not submesh_skeletons:
            return 0, 0, "没有子网格成功生成骨骼数据。"

        # 组内跨子网格去重构建 vg_map（组内从 0 起分配槽位，与其它 LOD 组互不影响）。
        # 联合 LOD 写回阶段传入预计算映射：LOD0 使用唯一一次真实去重结果，
        # LOD1 使用按对应关系投影后的映射，避免重新跑一套权重扩散并查集。
        if vg_maps_override is not None and vg_offsets_override is not None:
            vg_maps = vg_maps_override
            vg_offsets = vg_offsets_override
        else:
            vg_maps, vg_offsets = EFMIBoneMapBuilder.build_vg_maps(
                submesh_skeletons,
                protected_pairs=protected_pairs,
                constraint_labels=constraint_labels,
            )

        # 写回工作空间 json + 复制骨骼池缓存
        written = 0
        written_targets: set[str] = set()
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

            # VGOffset = 该子网格在组内全局骨架中的起始（取去重时分配的槽位，保证与 vg_map 一致）
            vg_offset = vg_offsets.get(unique_str, 0)

            submesh_json["VGCount"] = vg_count
            submesh_json["VGOffset"] = vg_offset
            submesh_json["VGMap"] = {str(k): int(v) for k, v in sorted(vg_map.items())}
            submesh_json["VGMapAlgorithmVersion"] = _VG_MAP_ALGORITHM_VERSION
            submesh_json["VGMapDedupEnabled"] = bool(_DEDUP_ENABLED)

            # 联合 LOD 对应只写诊断/后续处理元数据，不改变本 LOD 的运行时
            # offset 布局。这样 LOD0/LOD1 仍可各自挂载自己的骨骼池，同时保留
            # “哪一个原始组对应哪一个基准组、是否存在缺口”的事实账本。
            if cross_lod_info:
                reference_lod = str(cross_lod_info.get("reference_lod", "") or "")
                current_lod = cls._parse_lod_name(unique_str)
                baseline_count = int(cross_lod_info.get("baseline_group_count", 0) or 0)
                count_by_lod = cross_lod_info.get("group_count_by_lod", {}) or {}
                actual_count = int(count_by_lod.get(current_lod, 0) or 0)
                # 账本口径遵守 LOD0 基准：LOD1 如果原始候选确实少，不能把
                # 少出来的语义槽位悄悄当成“已对应”。Actual 保留真实去重数，
                # GroupCount 是后续对应层使用的有效槽位下限。
                current_count = max(actual_count, baseline_count) \
                    if current_lod != reference_lod else actual_count
                correspondence_rows = {}
                for row in cross_lod_info.get("matches", []) or []:
                    if row.get("reference_lod") == reference_lod and current_lod == reference_lod:
                        if row.get("reference_unique_str") != unique_str:
                            continue
                        local_id = row.get("reference_local_vg_id")
                        correspondence_rows[str(local_id)] = {
                            "lod": row.get("target_lod", ""),
                            "unique_str": row.get("target_unique_str", ""),
                            "local_vg_id": int(row.get("target_local_vg_id", 0) or 0),
                            "reference_component": row.get("reference_component", unique_str),
                            "target_component": row.get(
                                "target_component", row.get("target_unique_str", "")
                            ),
                            "component_score": float(row.get("component_score", 0.0) or 0.0),
                            "matrix_diff": float(row.get("matrix_diff", 0.0) or 0.0),
                            "centroid_distance": row.get("centroid_distance"),
                        }
                    elif row.get("target_lod") == current_lod:
                        if row.get("target_unique_str") != unique_str:
                            continue
                        local_id = row.get("target_local_vg_id")
                        correspondence_rows[str(local_id)] = {
                            "lod": row.get("reference_lod", ""),
                            "unique_str": row.get("reference_unique_str", ""),
                            "local_vg_id": int(row.get("reference_local_vg_id", 0) or 0),
                            "reference_component": row.get(
                                "reference_component", row.get("reference_unique_str", "")
                            ),
                            "target_component": row.get("target_component", unique_str),
                            "component_score": float(row.get("component_score", 0.0) or 0.0),
                            "matrix_diff": float(row.get("matrix_diff", 0.0) or 0.0),
                            "centroid_distance": row.get("centroid_distance"),
                        }
                submesh_json["EFMILODLayoutVersion"] = _CROSS_LOD_LAYOUT_VERSION
                submesh_json["EFMILODReference"] = reference_lod
                submesh_json["EFMILODProjection"] = bool(
                    cross_lod_info.get("projection_enabled", True)
                )
                submesh_json["EFMILODBaselineGroupCount"] = baseline_count
                submesh_json["EFMILODGroupCount"] = current_count
                submesh_json["EFMILODActualGroupCount"] = actual_count
                submesh_json["EFMILODMissingBaselineCount"] = max(
                    baseline_count - actual_count, 0
                ) if current_lod != reference_lod else 0
                submesh_json["EFMILODCorrespondence"] = correspondence_rows

            # 缓存发布与 JSON 写回属于同一事务；发布失败的目标不得计入 written。
            try:
                pool_path = cls._resolve_skeleton_pool_path(parser, meta["draw_index"])
                if not pool_path:
                    raise FileNotFoundError(
                        f"draw {meta['draw_index']} 未解析到骨骼池 buffer"
                    )
                runtime_dir = os.path.join(meta["submesh_dir"], "ModImpRuntime")
                dest_name = f"{meta['bare_name']}-BoneMatrix.buf"
                dest_path = os.path.join(runtime_dir, dest_name)
                cls._atomic_publish_cache(
                    pool_path,
                    dest_path,
                    vg_count=vg_count,
                )
                submesh_json["BoneMatrixFileName"] = dest_name
            except Exception as e:
                print(f"[EFMI骨骼合并] 复制骨骼池缓存失败 {unique_str}: {e}")
                continue

            try:
                JsonUtils.SaveToFile(json_dict=submesh_json, filepath=json_path)
                written += 1
                written_targets.add(unique_str)
            except Exception as e:
                print(f"[EFMI骨骼合并] 写回 json 失败 {unique_str}: {e}")

        # 完整性：written + skipped 必须覆盖全部请求目标；重建过程中任何
        # 读取失败/生成失败的目标都会让 unprocessed > 0，由外层据此判定失败。
        unprocessed_targets = sorted(set(unique_str_list) - written_targets)
        unprocessed_count = len(unprocessed_targets)
        message = f"已为 {written} 个子网格生成骨骼合并数据"
        if skipped:
            message += f"（跳过已缓存 {skipped} 个）"
        if unprocessed_count > 0:
            shown = unprocessed_targets[:5]
            suffix = "…" if len(unprocessed_targets) > 5 else ""
            message += (
                f"；{unprocessed_count} 个目标未生成骨骼数据: "
                f"{'、'.join(shown)}{suffix}"
            )
        return written, skipped, message

    @staticmethod
    def _runtime_cache_path(submesh_json: dict, json_path: str, unique_str: str) -> str:
        """解析子网格 json 的 BoneMatrixFileName 指向的实际缓存路径。

        只接受纯文件名（拒绝路径穿越）；指向 ModImpRuntime 下的文件。
        返回路径字符串（文件可能不存在，由调用方 isfile 校验）。
        """
        file_name = str(submesh_json.get("BoneMatrixFileName", "") or "").strip()
        if not file_name or os.path.basename(file_name) != file_name:
            bare_name = unique_str.split(".", 1)[-1]
            file_name = f"{bare_name}-BoneMatrix.buf"
        submesh_dir = os.path.dirname(os.path.dirname(json_path))
        return os.path.join(submesh_dir, "ModImpRuntime", file_name)

    @classmethod
    def _efmi_cache_intact(cls, submesh_json: dict, json_path: str, unique_str: str) -> bool:
        """EFMI 缓存快路径完整性校验（版本 + schema + 映射覆盖 + 骨骼缓存文件）。

        与 ZZMI 的 _zzmi_cache_intact 同构：任何一项缺失都判定缓存不完整，
        整批重建——骨骼池复制失败 / 工作空间搬迁漏掉 ModImpRuntime / 旧算法
        缓存都不允许带着半成品 VGMap 永久幂等跳过。校验项：
        - VGMapAlgorithmVersion == 当前算法版本、VGMapDedupEnabled == 全局开关；
        - VGCount/VGOffset 存在且非负；
        - VGMap 非空、键为 0..VGCount-1 的子集且槽位非负（EFMI 会跳过全零
          矩阵骨骼，因此键可以合法地不满集，但不能越界）；
        - BoneMatrixFileName 指向的 ModImpRuntime 文件存在且大小
          >= VGCount * 48 字节（每骨骼 4x3 float32）。
        """
        try:
            cache_version = int(submesh_json.get("VGMapAlgorithmVersion", 0) or 0)
        except (TypeError, ValueError):
            cache_version = 0
        if cache_version != _VG_MAP_ALGORITHM_VERSION:
            return False
        cache_dedup_enabled = submesh_json.get("VGMapDedupEnabled")
        if not isinstance(cache_dedup_enabled, bool) or cache_dedup_enabled != bool(_DEDUP_ENABLED):
            return False

        vg_map = submesh_json.get("VGMap")
        if not isinstance(vg_map, dict) or not vg_map:
            return False
        try:
            vg_count = int(submesh_json.get("VGCount"))
            vg_offset = int(submesh_json.get("VGOffset"))
            mapped = {int(key): int(value) for key, value in vg_map.items()}
        except (TypeError, ValueError):
            return False
        if vg_count <= 0 or vg_offset < 0:
            return False
        # EFMI 的 build_vg_maps 会跳过全零矩阵骨骼（零矩阵不参与候选），
        # 因此合法缓存的 VGMap 键可以是 0..VGCount-1 的子集，但不能越界、
        # 不能为空、槽位必须非负。
        if not mapped or len(mapped) > vg_count:
            return False
        if not set(mapped.keys()) <= set(range(vg_count)):
            return False
        if any(slot < 0 for slot in mapped.values()):
            return False

        cache_path = cls._runtime_cache_path(submesh_json, json_path, unique_str)
        if not os.path.isfile(cache_path):
            return False
        if not EFMIBoneMapBuilder.cache_file_size_ok(cache_path, vg_count):
            return False
        return True

    @classmethod
    def clear_vgmap_cache(cls, workspace_root: str) -> tuple[int, int]:
        """删除工作空间内所有子网格 json 的 VGMap/VGOffset/VGCount/SkeletonGroup 缓存键。

        用途：去重策略变更（或去重关闭）后，手动清掉缓存即可强制下次导入
        按当前策略重新生成；正常导入也会通过 VGMapAlgorithmVersion 自动
        使旧策略缓存失效。SkeletonGroup 是 ZZMI 分组版字段（EFMI json 没有，
        一并列出无副作用）。
        ModImpRuntime/*-BoneMatrix.buf 与 BoneMatrixFileName 不删：
        那是原始骨骼池拷贝，与去重策略无关，重新生成时会复用/覆写同名文件。

        返回 (清理的子网格 json 数, 扫描的 json 文件总数)。
        """
        cleaned = 0
        scanned = 0
        if not workspace_root or not os.path.isdir(workspace_root):
            return cleaned, scanned
        for dirpath, dirnames, filenames in os.walk(workspace_root):
            # Config 目录是工作空间配置（FrameAnalysisPath.json / Tabs 等），
            # 与子网格缓存无关，直接跳过。
            dirnames[:] = [d for d in dirnames if d != "Config"]
            for filename in filenames:
                if not filename.endswith(".json"):
                    continue
                path = os.path.join(dirpath, filename)
                scanned += 1
                try:
                    payload = JsonUtils.LoadFromFile(path)
                except Exception:
                    continue
                if not isinstance(payload, dict) or "VGMap" not in payload:
                    continue
                for key in (
                    "VGMap", "VGOffset", "VGCount", "VGMapAlgorithmVersion",
                    "VGMapDedupEnabled", "SkeletonGroup", "EFMILODLayoutVersion",
                    "EFMILODReference", "EFMILODProjection", "EFMILODBaselineGroupCount", "EFMILODGroupCount",
                    "EFMILODActualGroupCount", "EFMILODMissingBaselineCount",
                    "EFMILODCorrespondence"
                ):
                    payload.pop(key, None)
                try:
                    JsonUtils.SaveToFile(filepath=path, json_dict=payload)
                    cleaned += 1
                except Exception as e:
                    print(f"[EFMI骨骼合并] 清理 VGMap 缓存失败 {path}: {e}")
        return cleaned, scanned

    @classmethod
    def _resolve_skeleton_pool_path(cls, parser: EFMILogParser, draw_index: str) -> str | None:
        skeleton_t0_hash = parser.get_vs_t0(draw_index)
        if not skeleton_t0_hash:
            return None
        pool_logical = f"{draw_index}-vs-t0={skeleton_t0_hash}"
        return parser.get_deduped_path(pool_logical)
