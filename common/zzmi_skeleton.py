"""
ZZMI（绝区零）骨骼合并支持模块

数据流（实测结论，详见项目根目录 ZZMI骨骼合并计划书.md）：
- ZZZ 渲染为三段式管线：CS 形变 pass -> pointlist 蒙皮变形 pass（下称 deform pass）
  -> 渲染 draw。骨骼 palette 只存在于 **deform pass 的 vs-t0**（每部件一份，
  12 floats / 4x3 矩阵 = 48 字节每骨骼）；渲染 draw 只读 SO 蒙皮输出，无骨骼数据。
- 工作空间子网格 json 自带 join 线索：
  `CategoryHash.Position` == deform pass 的 vb0 资源 hash（路径 A）；
  `VertexLimitVB` == deform pass 的 SO 输出资源 hash（路径 B）；
  兜底用 `LOD0/ComponentName_DrawCallIndexList.json` 的渲染 draw -> vb0 -> SO（路径 C）。

去重规则（用户拍板 + 实测修正）：
- **同一部件内部绝不去重合并**（同部件索引为提取端权威分配，实测内部零重复）；
- **仅跨部件去重，严格 bitwise（字节级）判等，禁用浮点容差**
  （同一骨骼同帧上传到各 palette 是逐位拷贝；近似但非位等的矩阵是同骨骼
  异帧姿态——如脸部 morph 部件的 deform pass 更晚、动画已推进——容差会误并）。
- **单骨骼刚性部件（单权重物体）追加加权质心门控**（2026-08-24 用户拍板）：
  抓帧瞬间不同锚点骨可能逐位重合（头顶件/前额件/后脑发饰实测同矩阵），bitwise
  会误并成一根，动画分叉时两物体错位联动；刚性部件命中对要求加权质心距离
  < rigid_centroid_tolerance（默认 0.05）才合并，否则各占各槽。刚性部件误拆
  零代价（各自 attach 写同一矩阵，运行时内容恒等），只拆不并是安全方向。
  双方均为多骨骼部件时不加门控（多根同时位等不可能是巧合；真共享骨骼的
  驱动区域质心可相距甚远，实测达 0.25，加门控会误拆真共享）。
- 合并骨架布局：每部件 palette 按 VGOffset 连续摆放（VGOffset = 固定排序下
  前序部件 vg_count 累加），去重只决定顶点引用的全局 id 指向哪个 canonical 槽位，
  重复槽位为死槽（内容恒同，无害）。

产出：
- 每个子网格 json 写回 `VGMap` / `VGOffset` / `VGCount`（缓存，幂等；force 可重建）+
  `SkeletonGroup`（骨架分组号：按渲染 cb1 对象变换分组，palette 与 cb1 逐物体
  1:1 配对，跨组绝不共享骨架；VGMap/VGOffset 均为组内槽位命名空间）；
- palette buf 复制到 `<子网格>/ModImpRuntime/<bare>-BoneMatrix.buf`（NTEMI/EFMI 缓存模式）。
"""

import os
import re
import shutil

import numpy

from ..utils.json_utils import JsonUtils
from .efmi_skeleton import EFMIBoneMapBuilder, EFMISkeletonMergeHelper

# 每骨骼矩阵的 float 数（4x3 = 48 字节，已实测确认）
_BONE_MATRIX_FLOATS = 12


class ZZMILogParser:
    """解析 ZZZ FrameAnalysis/log.txt，提供 deform pass 与资源绑定查询。

    与 EFMILogParser 的差异：ZZZ 需要 Draw(VertexCount)（pointlist deform pass）、
    SOSetTargets（蒙皮输出）、IASetVertexBuffers/IASetIndexBuffer（逐槽资源 hash），
    不需要 instance config 窗口反查。
    """

    _DRAW_PREFIX_RE = re.compile(r"^(\d{6}) (.*)$")
    _DUMP_RE = re.compile(r"^3DMigoto Dumping Buffer (.+) -> (.+)$")
    _RESOURCE_LINE_RE = re.compile(
        r"^(\d+): (?:view=0x[0-9A-Fa-f]+ )?resource=0x[0-9A-Fa-f]+ hash=([0-9a-f]{8})"
    )
    _DRAW_RE = re.compile(r"^Draw\(VertexCount:(\d+), StartVertexLocation:(\d+)\)$")
    _DRAW_INDEXED_RE = re.compile(
        r"^DrawIndexedInstanced\(IndexCountPerInstance:(\d+), InstanceCount:(\d+), "
        r"StartIndexLocation:(\d+), BaseVertexLocation:(\d+), StartInstanceLocation:(\d+)\)$"
    )
    _IA_VB_RE = re.compile(r"^IASetVertexBuffers\(StartSlot:(\d+), NumBuffers:(\d+),")
    _IA_IB_RE = re.compile(r"^IASetIndexBuffer\(.*\) hash=([0-9a-f]{8})$")
    _SO_RE = re.compile(r"^SOSetTargets\(NumBuffers:(\d+),")
    _SRV_RE = re.compile(r"^(V|P|C|G|H|D)SSetShaderResources\(StartSlot:(\d+), NumViews:(\d+),")
    _VS_SHADER_RE = re.compile(r"^VSSetShader\(.*\) hash=([0-9a-f]+)$")
    # vs-cb1 dump 行（渲染 draw 的对象变换 CB；dump 文件名自带 draw 索引与 hash，
    # 比绑定调用可靠——绑定是持久状态、多数 draw 块里不重发）
    _CB1_DUMP_RE = re.compile(r"^(\d{6})-vs-cb1=([0-9a-f]{8})-.*\.buf$")

    def __init__(self, log_path: str):
        self.log_path = log_path
        # draw_index -> {"vb": {slot: hash}, "ib": hash, "so": {slot: hash},
        #                "vs_t0": hash, "vs_shader": hash,
        #                "vertex_count": int|None, "draw_indexed": dict|None}
        self.draws: dict[str, dict] = {}
        # 渲染 draw_index -> vs-cb1 dump 实际路径（deduped/*.buf，来自 dump 行）
        self.render_cb1_dumps: dict[str, str] = {}
        # 逻辑文件名（根目录 dump 文件名）-> deduped 实际路径
        self.dump_map: dict[str, str] = {}
        self._parse()

    def _get_draw(self, draw_index: str) -> dict:
        info = self.draws.get(draw_index)
        if info is None:
            info = {
                "vb": {},
                "ib": "",
                "so": {},
                "vs_t0": "",
                "vs_shader": "",
                "vertex_count": None,
                "draw_indexed": None,
            }
            self.draws[draw_index] = info
        return info

    def _parse(self):
        if not os.path.isfile(self.log_path):
            raise FileNotFoundError(f"FrameAnalysis log 不存在: {self.log_path}")

        # pending 资源消费状态：("vb",) / ("so",) / ("srv", stage) / None
        # 任何带 draw 前缀的行都会重置/重设 pending；无前缀的资源描述行被当前 pending 消费。
        pending = None
        pending_draw = ""

        with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue

                match = self._DRAW_PREFIX_RE.match(line)
                if not match:
                    # 资源描述行（无 draw 前缀）
                    if pending is not None:
                        stripped = line.strip()
                        desc = self._RESOURCE_LINE_RE.match(stripped)
                        if desc:
                            slot = int(desc.group(1))
                            res_hash = desc.group(2)
                            info = self._get_draw(pending_draw)
                            kind = pending[0]
                            if kind == "vb":
                                info["vb"][slot] = res_hash
                            elif kind == "so":
                                info["so"][slot] = res_hash
                            elif kind == "srv" and pending[1] == "V" and slot == 0:
                                info["vs_t0"] = res_hash
                        # 注意：不在这里清空 pending——多槽资源描述是连续多行，
                        # 由下一行（无论有无前缀）继续消费或重设。
                    continue

                draw_index, payload = match.group(1), match.group(2)
                pending = None
                pending_draw = draw_index

                # 顶点缓冲绑定（内容行跟随，多行）
                vb_match = self._IA_VB_RE.match(payload)
                if vb_match:
                    pending = ("vb",)
                    continue

                # 索引缓冲绑定（同行 hash）
                ib_match = self._IA_IB_RE.match(payload)
                if ib_match:
                    self._get_draw(draw_index)["ib"] = ib_match.group(1)
                    continue

                # Stream-Out 目标绑定（内容行跟随）
                so_match = self._SO_RE.match(payload)
                if so_match:
                    pending = ("so",)
                    continue

                # 着色器资源绑定（内容行跟随；只关心 VS slot 0）
                srv_match = self._SRV_RE.match(payload)
                if srv_match:
                    pending = ("srv", srv_match.group(1))
                    continue

                # VS hash
                vs_match = self._VS_SHADER_RE.match(payload)
                if vs_match:
                    self._get_draw(draw_index)["vs_shader"] = vs_match.group(1)
                    continue

                # pointlist 蒙皮变形 draw
                draw_match = self._DRAW_RE.match(payload)
                if draw_match:
                    self._get_draw(draw_index)["vertex_count"] = int(draw_match.group(1))
                    continue

                # 渲染 draw
                indexed_match = self._DRAW_INDEXED_RE.match(payload)
                if indexed_match:
                    self._get_draw(draw_index)["draw_indexed"] = {
                        "index_count": int(indexed_match.group(1)),
                        "start_index": int(indexed_match.group(3)),
                        "base_vertex": int(indexed_match.group(4)),
                    }
                    continue

                # 资源 dump 映射
                dump_match = self._DUMP_RE.match(payload)
                if dump_match:
                    src_name = os.path.basename(dump_match.group(1))
                    dst_path = dump_match.group(2)
                    if src_name not in self.dump_map:
                        self.dump_map[src_name] = dst_path
                    # vs-cb1 dump（渲染 draw 的对象变换 CB）：按 draw 索引直接记录路径
                    cb1_match = self._CB1_DUMP_RE.match(src_name)
                    if cb1_match and os.path.isfile(dst_path):
                        draw_key = cb1_match.group(1)
                        if draw_key not in self.render_cb1_dumps:
                            self.render_cb1_dumps[draw_key] = dst_path
                    continue

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def get_vb_hash(self, draw_index: str, slot: int) -> str:
        info = self.draws.get(draw_index)
        return info["vb"].get(slot, "") if info else ""

    def get_render_cb1_path(self, draw_index: str) -> str | None:
        """渲染 draw 的 vs-cb1 dump 实际路径（对象变换 CB；未 dump 返回 None）。"""
        return self.render_cb1_dumps.get(draw_index)

    def get_deform_passes(self) -> dict[str, dict]:
        """识别全部 deform pass（pointlist Draw + SO 输出 + vs-t0 palette + vb0）。

        返回 draw_index -> {vertex_count, so_hash, palette_hash, vb0_hash, vb2_hash, vs_hash}
        """
        result = {}
        for draw_index, info in self.draws.items():
            vertex_count = info.get("vertex_count")
            so_hash = info.get("so", {}).get(0, "")
            palette_hash = info.get("vs_t0", "")
            vb0_hash = info.get("vb", {}).get(0, "")
            if vertex_count and so_hash and palette_hash and vb0_hash:
                result[draw_index] = {
                    "vertex_count": vertex_count,
                    "so_hash": so_hash,
                    "palette_hash": palette_hash,
                    "vb0_hash": vb0_hash,
                    "vb2_hash": info.get("vb", {}).get(2, ""),
                    "vs_hash": info.get("vs_shader", ""),
                }
        return result

    def get_deduped_path(self, logical_filename: str) -> str | None:
        """按根目录逻辑文件名（如 000002-vs-t0=...buf）查 deduped 实际路径。"""
        path = self.dump_map.get(logical_filename)
        if path and os.path.isfile(path):
            return path
        # 兜底：按 draw+槽位前缀匹配（逻辑名可能缺 -vs= 尾段）
        prefix = logical_filename.split("=", 1)[0] + "="
        for src, dst in self.dump_map.items():
            if src.startswith(prefix) and os.path.isfile(dst):
                return dst
        return None


class ZZMIDeformResolver:
    """把工作空间子网格挂载到正确的 deform pass（三条独立 join 路径）。"""

    def __init__(self, parser: ZZMILogParser):
        self.parser = parser
        self.passes = parser.get_deform_passes()
        # deform 输入 vb0 hash -> (draw_index, pass)
        self.by_vb0: dict[str, tuple[str, dict]] = {}
        # deform SO 输出 hash -> (draw_index, pass)
        self.by_so: dict[str, tuple[str, dict]] = {}
        for draw_index, deform_pass in self.passes.items():
            if deform_pass["vb0_hash"] and deform_pass["vb0_hash"] not in self.by_vb0:
                self.by_vb0[deform_pass["vb0_hash"]] = (draw_index, deform_pass)
            if deform_pass["so_hash"] and deform_pass["so_hash"] not in self.by_so:
                self.by_so[deform_pass["so_hash"]] = (draw_index, deform_pass)

    def resolve(
        self,
        position_hash: str = "",
        vertex_limit_hash: str = "",
        render_draw_indices: list[str] | None = None,
    ) -> tuple[str, dict, str]:
        """返回 (draw_index, deform_pass, 命中路径"A"/"B"/"C")；未命中返回 ("", None, "")。

        - 路径 A：子网格 json CategoryHash.Position == deform vb0 hash（最直接）；
        - 路径 B：子网格 json VertexLimitVB == deform SO 输出 hash；
        - 路径 C：渲染 draw（ComponentName_DrawCallIndexList.json）的 vb0 == SO 输出 hash。
        """
        position_hash = str(position_hash or "").strip().lower()
        if position_hash and position_hash in self.by_vb0:
            draw_index, deform_pass = self.by_vb0[position_hash]
            return draw_index, deform_pass, "A"

        vertex_limit_hash = str(vertex_limit_hash or "").strip().lower()
        if vertex_limit_hash and vertex_limit_hash in self.by_so:
            draw_index, deform_pass = self.by_so[vertex_limit_hash]
            return draw_index, deform_pass, "B"

        for render_draw in render_draw_indices or []:
            vb0_hash = self.parser.get_vb_hash(render_draw, 0)
            if vb0_hash and vb0_hash in self.by_so:
                draw_index, deform_pass = self.by_so[vb0_hash]
                return draw_index, deform_pass, "C"

        return "", None, ""


def assign_skeleton_groups(part_transforms: dict[str, tuple[float, ...] | None]) -> dict[str, int]:
    """按对象变换（渲染 cb1）把 DrawIB 部件分组：返回 draw_ib -> 骨架组索引。

    规则（用户拍板，2026-08-24）：palette 与 cb1 逐物体 1:1 配对——
    共享同一对象空间（变换逐位相同）的部件进同一组、共用一套合并骨架；
    跨组绝不共享（不同空间的矩阵混用会被渲染侧 cb1 摆错位置）。
    无变换数据的部件独立成组（安全方向）。
    组索引按组内最小 draw_ib 排序分配（确定性，导入/导出两侧一致）。
    """
    key_groups: dict[tuple, list[str]] = {}
    for draw_ib, transform in part_transforms.items():
        key = transform if transform is not None else ("__solo__", draw_ib)
        key_groups.setdefault(key, []).append(draw_ib)
    ordered_keys = sorted(key_groups.keys(), key=lambda k: min(key_groups[k]))
    result: dict[str, int] = {}
    for group_index, key in enumerate(ordered_keys):
        for draw_ib in key_groups[key]:
            result[draw_ib] = group_index
    return result


class ZZMIBoneMapBuilder:
    """palette 解析与跨部件 bitwise 去重（同部件绝不去重）。"""

    @staticmethod
    def load_palette(palette_path: str) -> numpy.ndarray:
        """读取 palette buf 为 (N, 12) float32 矩阵数组。"""
        data = numpy.fromfile(palette_path, dtype=numpy.float32)
        if len(data) == 0 or len(data) % _BONE_MATRIX_FLOATS != 0:
            raise ValueError(
                f"palette 大小不能按 {_BONE_MATRIX_FLOATS} floats 切分: {palette_path} "
                f"({len(data)} floats)"
            )
        return data.reshape(-1, _BONE_MATRIX_FLOATS)

    @staticmethod
    def parse_object_transform(cb1_path: str) -> tuple[float, ...] | None:
        """从渲染 draw 的 vs-cb1 dump 解析对象→世界矩阵，返回 16 floats 元组（分组键）。

        实测布局（FrameAnalysis-2026-08-19-122152 逆向）：逐部件 cb1 块的前 4 个
        float4 = 3x4 对象变换（rows 0-2 旋转行 w=0、row 3 平移 w=1）。
        palette 矩阵把顶点蒙皮到该对象空间，渲染 VS 再用本矩阵摆到世界——
        两者逐物体 1:1 配对，共享同一份变换的部件才共享同一对象空间。

        只接受 ≤512 字节的逐部件块（实测 176/256/464/512B）：>512B 的 cb1 是
        **多对象共享变换数组**（draw 用 first_constant 窗口索引），rows 0-3 未必是
        本 draw 的对象，排除。解析失败返回 None（调用方按独立组兜底 = 不共享，安全方向）。
        """
        try:
            if os.path.getsize(cb1_path) > 512:
                return None
            data = numpy.fromfile(cb1_path, dtype=numpy.float32)
        except (OSError, ValueError):
            return None
        if len(data) < 16:
            return None
        m = data[:16].reshape(4, 4)
        # w 列形态：旋转行 w=0、平移行 w=1
        if abs(float(m[3, 3]) - 1.0) > 1e-3:
            return None
        if float(numpy.abs(m[:3, 3]).max()) > 1e-3:
            return None
        # 旋转行 sanity（允许缩放/镜像，排除纯参数块）
        row_norms = numpy.linalg.norm(m[:3, :3].astype(numpy.float64), axis=1)
        if numpy.any((row_norms < 0.05) | (row_norms > 20.0)):
            return None
        return tuple(float(x) for x in data[:16])

    @staticmethod
    def build_vg_maps(
        part_palettes: dict[str, numpy.ndarray],
        part_signatures: dict[str, dict] | None = None,
        rigid_centroid_tolerance: float = 0.05,
    ) -> tuple[dict[str, dict], dict[str, int], int]:
        """跨部件按矩阵 bitwise 判等去重（刚性部件加权质心门控），构建 vg_map / vg_offset / 总槽位。

        参数: part_key -> (N, 12) palette 矩阵数组（part_key 为去重单元，同 DrawIB 的
              多个子网格共享同一 palette，只参与一次）；
              part_signatures: part_key -> {local: 驱动签名}（EFMIBoneMapBuilder.
              compute_driven_signatures 产出，仅用其 centroid 字段）；不传则退化为
              纯 bitwise 旧行为；
              rigid_centroid_tolerance: 刚性部件命中对的质心确认阈值（默认 0.05 米）。
        返回:
            vg_maps: part_key -> {local_vg_id(int): global_vg_id(int)}
            vg_offsets: part_key -> 该部件在合并骨架中的起始槽位
            total_slots: 合并骨架总槽位（Σ vg_count，含死槽）

        规则（用户拍板 + 2026-08-24 增补刚性门控）：
        - 同部件内部绝不去重（同 key 只来自更靠前的其它部件才合并）；
        - 跨部件严格 bitwise（tobytes）判等，无浮点容差；
        - **刚性部件质心门控**：命中对任一方是单骨骼刚性部件（palette 仅 1 根，
          即"单权重物体"——其唯一骨骼就是整个物体的锚点，质心 = 物体位置指纹）时，
          追加加权质心距离确认，距离 >= 阈值则拆开各占各槽。
          背景：抓帧瞬间不同锚点骨可能逐位重合（头顶件/前额件/后脑发饰同矩阵实测案例），
          bitwise 会把它们误并成一根；刚性部件误拆零代价（各自 attach 各自写同一矩阵，
          运行时内容恒等），误并则游戏内动画分叉时两物体错位联动——只拆不并是安全方向。
          门控触发时任一方缺签名 -> 保守拆开。
        - 双方都是多骨骼部件时不加质心门控：整块 palette 多根骨骼同时逐位相同不可能是
          巧合；且真共享骨骼跨部件驱动区域的质心可相距甚远（实测 b20f90ea ↔ a23aa8a3
          达 0.25），多骨骼加门控会误拆真共享。
        - canonical 槽位 = 按 part_key 排序后首次（通过门控的）出现处的槽位（确定性）。
        """
        # bone_key -> 该矩阵字节序列的既有归属者列表：
        # {"part": 部件, "slot": 全局槽位, "rigid": 是否单骨骼刚性部件, "centroid": 加权质心|None}
        owners: dict[bytes, list[dict]] = {}
        vg_maps: dict[str, dict] = {}
        vg_offsets: dict[str, int] = {}
        offset = 0

        def _centroid_of(part_key: str, local_id: int):
            if part_signatures is None:
                return None
            sig = (part_signatures.get(part_key) or {}).get(local_id)
            return None if sig is None else sig["centroid"]

        def _gate_ok(hitter_rigid: bool, hitter_centroid, owner: dict) -> bool:
            """刚性门控：命中对任一方为刚性部件时要求质心贴合，否则纯 bitwise 放行。"""
            if not (hitter_rigid or owner["rigid"]):
                return True
            if part_signatures is None:
                return True
            if hitter_centroid is None or owner["centroid"] is None:
                return False
            dist = float(numpy.linalg.norm(
                hitter_centroid.astype(numpy.float64) - owner["centroid"].astype(numpy.float64)
            ))
            return dist < rigid_centroid_tolerance

        for part_key in sorted(part_palettes.keys()):
            palette = part_palettes[part_key]
            part_rigid = len(palette) == 1
            vg_offsets[part_key] = offset
            vg_map = {}
            for local_id in range(len(palette)):
                bone_key = palette[local_id].tobytes()
                hitter_centroid = _centroid_of(part_key, local_id)
                target_slot = None
                for owner in owners.get(bone_key, ()):
                    if owner["part"] == part_key:
                        continue
                    if _gate_ok(part_rigid, hitter_centroid, owner):
                        # 跨部件命中（且通过刚性门控）：合并到归属者槽位
                        target_slot = owner["slot"]
                        break
                if target_slot is not None:
                    vg_map[local_id] = target_slot
                else:
                    # 新骨骼（或同部件重复/门控拆开的重合骨骼，不合并）：占本部件自己的槽位
                    slot = offset + local_id
                    vg_map[local_id] = slot
                    owners.setdefault(bone_key, []).append({
                        "part": part_key,
                        "slot": slot,
                        "rigid": part_rigid,
                        "centroid": hitter_centroid,
                    })
            vg_maps[part_key] = vg_map
            offset += len(palette)

        return vg_maps, vg_offsets, offset


class ZZMISkeletonMergeHelper:
    """ZZMI 骨骼合并总流程：定位 FrameAnalysis -> 解析 log -> 反查 deform pass -> 去重 -> 写回工作空间。"""

    resolve_frame_analysis_dir = staticmethod(
        EFMISkeletonMergeHelper.resolve_frame_analysis_dir
    )
    _resolve_submesh_json_path = staticmethod(
        EFMISkeletonMergeHelper._resolve_submesh_json_path
    )
    _load_drawcall_index_list = staticmethod(
        EFMISkeletonMergeHelper.load_drawcall_index_list
    )
    _parse_blend_element_info = staticmethod(
        EFMISkeletonMergeHelper.parse_blend_element_info
    )

    @classmethod
    def ensure_skeleton_data(
        cls,
        workspace_root: str,
        unique_str_list: list[str],
        force: bool = False,
    ) -> tuple[bool, str]:
        """为 ZZMI 工作空间的子网格生成并写回骨骼合并数据（幂等）。

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

        parser = ZZMILogParser(log_path)
        resolver = ZZMIDeformResolver(parser)
        if not resolver.passes:
            return False, "FrameAnalysis log 中未识别到 deform pass（pointlist 蒙皮变形）。"

        # 第一遍：收集每个子网格信息并按 DrawIB 分组（同 DrawIB 的拆分子网格共享
        # 同一 deform pass / palette / VGMap，只参与一次去重）。
        # drawib -> {"palette", "vg_count", "members": [unique_str...],
        #            "json_paths": {unique_str: path}, "palette_path", "draw_index",
        #            "signatures", "transform"}
        #
        # 幂等 schema 门控（分组版一致性规则）：VGMap 按"组内槽位"写回，一次导入的
        # 所有部件必须共享同一次分组计算——不允许"部分部件用缓存、部分部件新算"
        # （否则两组槽位口径可能不一致）。因此：只要任一目标子网格缺 VGMap 或
        # 缺 SkeletonGroup 字段（旧缓存/新工作空间），就对全部目标重建；全部齐备
        # 才整批幂等跳过（快速路径，不解析 dump）。
        groups: dict[str, dict] = {}
        stale = 0

        for unique_str in unique_str_list:
            json_path = cls._resolve_submesh_json_path(workspace_root, unique_str)
            if not json_path:
                print(f"[ZZMI骨骼合并] 跳过 {unique_str}: 未找到子网格 json")
                continue

            submesh_json = JsonUtils.LoadFromFile(json_path)
            if not isinstance(submesh_json, dict):
                continue

            bare_name = unique_str.split(".", 1)[-1]
            draw_ib = bare_name.split("-")[0] if "-" in bare_name else bare_name
            if not draw_ib:
                print(f"[ZZMI骨骼合并] 跳过 {unique_str}: 无法解析 DrawIB")
                continue

            group = groups.get(draw_ib)
            if group is None:
                group = {
                    "palette": None,
                    "vg_count": 0,
                    "members": [],
                    "json_paths": {},
                    "palette_path": "",
                    "draw_index": "",
                    "signatures": {},
                    "transform": None,
                    "cb1_last_draw_ok": False,
                    "representative": unique_str,
                }
                groups[draw_ib] = group
            group["members"].append(unique_str)
            group["json_paths"][unique_str] = json_path

            if not force and (
                submesh_json.get("VGMap")
                and "SkeletonGroup" in submesh_json
                and "SkeletonGroupCb1SourceIb" in submesh_json
            ):
                continue  # 该子网格缓存为当前 schema（临时计数，见下）
            stale += 1

        up_to_date = len(groups) and stale == 0
        if up_to_date and not force:
            total = sum(len(g["members"]) for g in groups.values())
            return True, f"所有 {total} 个子网格均已有骨骼合并数据（幂等跳过）。"

        # 第二遍：逐组反查 deform pass -> palette，读取 Blend.buf 得 vg_count
        for draw_ib, group in groups.items():
            representative = group["representative"]
            json_path = group["json_paths"][representative]
            submesh_json = JsonUtils.LoadFromFile(json_path)
            if not isinstance(submesh_json, dict):
                continue

            category_hash = submesh_json.get("CategoryHash", {}) or {}
            position_hash = str(category_hash.get("Position", "") or "")
            vertex_limit_hash = str(submesh_json.get("VertexLimitVB", "") or "")

            # 路径 C 的渲染 draw 列表（LOD 目录下的 ComponentName_DrawCallIndexList.json）
            lod_dir = os.path.dirname(os.path.dirname(os.path.dirname(json_path)))
            role_mapping = cls._load_drawcall_index_list(lod_dir)
            render_draws = role_mapping.get(
                os.path.basename(os.path.dirname(os.path.dirname(json_path))), []
            )

            draw_index, deform_pass, via = resolver.resolve(
                position_hash=position_hash,
                vertex_limit_hash=vertex_limit_hash,
                render_draw_indices=render_draws,
            )
            if deform_pass is None:
                print(f"[ZZMI骨骼合并] 跳过 {draw_ib}: 无法挂载到 deform pass（A/B/C 均未命中）")
                continue

            palette_logical = f"{draw_index}-vs-t0={deform_pass['palette_hash']}"
            palette_path = parser.get_deduped_path(palette_logical)
            if not palette_path:
                print(f"[ZZMI骨骼合并] 跳过 {draw_ib}: palette dump 缺失 {palette_logical}")
                continue

            try:
                palette = ZZMIBoneMapBuilder.load_palette(palette_path)
            except Exception as e:
                print(f"[ZZMI骨骼合并] 跳过 {draw_ib}: 读取 palette 失败: {e}")
                continue

            # vg_count：工作空间 Blend.buf 的 BLENDINDICES 最大非负索引 + 1
            element_info = cls._parse_blend_element_info(submesh_json)
            if element_info is None:
                print(f"[ZZMI骨骼合并] 跳过 {draw_ib}: Blend 类别缺少 BLENDINDICES 元素")
                continue
            blend_buf_path = os.path.join(
                os.path.dirname(json_path),
                os.path.splitext(os.path.basename(json_path))[0] + "-Blend.buf",
            )
            try:
                blend_indices = EFMIBoneMapBuilder.parse_blendindices_from_buf(
                    blend_buf_path, element_info
                )
            except Exception as e:
                print(f"[ZZMI骨骼合并] 跳过 {draw_ib}: 读取 Blend.buf 失败: {e}")
                continue

            local_indices = blend_indices.astype(numpy.int64, copy=False).ravel()
            valid_indices = local_indices[local_indices >= 0]
            if len(valid_indices) == 0:
                print(f"[ZZMI骨骼合并] 跳过 {draw_ib}: BLENDINDICES 全为空")
                continue
            vg_count = int(valid_indices.max()) + 1

            if len(palette) < vg_count:
                print(
                    f"[ZZMI骨骼合并] 跳过 {draw_ib}: palette {len(palette)} 根骨骼 < "
                    f"顶点组 {vg_count}（数据不一致）"
                )
                continue
            if len(palette) != vg_count:
                # 实测应恒等；不等时裁到实际用量并提示
                print(
                    f"[ZZMI骨骼合并] 提示 {draw_ib}: palette {len(palette)} 根 > "
                    f"顶点组 {vg_count}，按实际用量切分"
                )
                palette = palette[:vg_count]

            group["palette"] = palette
            group["vg_count"] = vg_count
            group["palette_path"] = palette_path
            group["draw_index"] = draw_index
            group["via"] = via

            # 刚性部件质心门控用的驱动签名（读 Position.buf + Blend.buf 计算每骨骼
            # 驱动点云质心；失败则空签名 -> 该部件的刚性命中对保守拆开，安全方向）
            position_buf_path = os.path.join(
                os.path.dirname(json_path),
                os.path.splitext(os.path.basename(json_path))[0] + "-Position.buf",
            )
            try:
                group["signatures"] = EFMIBoneMapBuilder.compute_driven_signatures(
                    position_buf_path, blend_buf_path, submesh_json
                )
            except Exception as e:
                print(f"[ZZMI骨骼合并] 提示 {draw_ib}: 驱动签名计算失败（刚性命中对将拆开）: {e}")
                group["signatures"] = {}

            # 骨架分组键：渲染 draw 的 vs-cb1 对象→世界矩阵（palette 蒙皮到对象空间，
            # 渲染 VS 用 cb1 摆到世界，两者逐物体 1:1 配对）。取该部件渲染 draw 列表中
            # 第一个可解析的逐部件 cb1 块；全部失败则 None -> 独立成组（不共享，安全）。
            for render_draw in sorted(render_draws):
                cb1_path = parser.get_render_cb1_path(render_draw)
                if not cb1_path:
                    continue
                transform = ZZMIBoneMapBuilder.parse_object_transform(cb1_path)
                if transform is not None:
                    group["transform"] = transform
                    break
            if group["transform"] is None:
                print(f"[ZZMI骨骼合并] 提示 {draw_ib}: 未能解析对象变换（渲染 cb1），独立成组")

            # cb1 捕获代表资格（校准版运行时）：捕获段按 DrawIB 触发、帧内"最后一击"
            # 覆盖生效，所以该部件全拆分子网格的**帧内最后一个渲染 draw** 的 vs-cb1
            # 必须是可解析的逐部件块（>512B 的共享变换数组不行）。
            all_render_draws = sorted({str(d) for d in render_draws if str(d).isdigit()})
            for member in group["members"]:
                member_json_path = group["json_paths"].get(member)
                if not member_json_path or member == representative:
                    continue
                member_lod_dir = os.path.dirname(os.path.dirname(os.path.dirname(member_json_path)))
                member_mapping = cls._load_drawcall_index_list(member_lod_dir)
                member_name = os.path.basename(os.path.dirname(os.path.dirname(member_json_path)))
                for d in member_mapping.get(member_name, []):
                    if str(d).isdigit():
                        all_render_draws.append(str(d))
            if all_render_draws:
                last_draw = max(all_render_draws)
                last_cb1_path = parser.get_render_cb1_path(last_draw)
                group["cb1_last_draw_ok"] = bool(
                    last_cb1_path
                    and ZZMIBoneMapBuilder.parse_object_transform(last_cb1_path) is not None
                )
            else:
                group["cb1_last_draw_ok"] = False

        ready_groups = {
            draw_ib: group for draw_ib, group in groups.items() if group["palette"] is not None
        }
        if not ready_groups:
            return False, "没有子网格成功生成骨骼数据。"

        # 第三遍：按对象变换分组（跨组不共享同一空间槽位；校准版运行时中外来骨骼
        # 经校准乘写入目标组骨架），组内 bitwise + 刚性门控去重得组内槽位；
        # 然后按组基址拼接成**全局骨骼编号**（Blender 侧全局命名空间，join 无歧义，
        # 跨组权重可表达；每组运行时骨架为全宽 buffer，本组直拷 + 外来校准写入）。
        group_of = assign_skeleton_groups(
            {draw_ib: group["transform"] for draw_ib, group in ready_groups.items()}
        )
        group_members: dict[int, list[str]] = {}
        for draw_ib, group_index in group_of.items():
            group_members.setdefault(group_index, []).append(draw_ib)

        # 组内去重（组内槽位 0 起）
        local_maps: dict[str, dict] = {}
        local_offsets: dict[str, int] = {}
        group_slots: dict[int, int] = {}
        for group_index in sorted(group_members):
            members = group_members[group_index]
            gm, go, total = ZZMIBoneMapBuilder.build_vg_maps(
                {draw_ib: ready_groups[draw_ib]["palette"] for draw_ib in members},
                {draw_ib: ready_groups[draw_ib]["signatures"] for draw_ib in members},
            )
            local_maps.update(gm)
            local_offsets.update(go)
            group_slots[group_index] = total

        # 组基址（组索引升序累加）
        group_base: dict[int, int] = {}
        base = 0
        for group_index in sorted(group_members):
            group_base[group_index] = base
            base += group_slots[group_index]

        vg_maps: dict[str, dict] = {}
        vg_offsets: dict[str, int] = {}
        for draw_ib in ready_groups:
            group_index = group_of[draw_ib]
            vg_offsets[draw_ib] = group_base[group_index] + local_offsets[draw_ib]
            vg_maps[draw_ib] = {
                local: group_base[group_index] + slot
                for local, slot in local_maps[draw_ib].items()
            }

        # 每组的 cb1 捕获源部件（校准用对象变换）：成员中"帧内最后一个渲染 draw 的
        # vs-cb1 是可解析逐部件块"者优先（last-wins 覆盖口径下捕获内容才正确）；
        # 无合格成员 -> 空串（该组运行时 attach 自动退化为直拷 = 分组版行为）。
        group_cb1_source: dict[int, str] = {}
        for group_index in sorted(group_members):
            source = ""
            for draw_ib in sorted(group_members[group_index]):
                if ready_groups[draw_ib].get("cb1_last_draw_ok"):
                    source = draw_ib
                    break
            group_cb1_source[group_index] = source

        # 写回工作空间 json + 复制 palette 缓存（组内所有子网格写相同结果）
        written = 0
        for draw_ib, group in ready_groups.items():
            vg_map = vg_maps.get(draw_ib, {})
            if not vg_map:
                continue
            vg_offset = vg_offsets[draw_ib]
            vg_count = group["vg_count"]
            skeleton_group = group_of[draw_ib]
            cb1_source_ib = group_cb1_source.get(skeleton_group, "")

            for unique_str in group["members"]:
                json_path = group["json_paths"][unique_str]
                submesh_json = JsonUtils.LoadFromFile(json_path)
                if not isinstance(submesh_json, dict):
                    continue

                submesh_json["VGCount"] = vg_count
                submesh_json["VGOffset"] = vg_offset
                submesh_json["VGMap"] = {str(k): int(v) for k, v in sorted(vg_map.items())}
                # 骨架分组（渲染 cb1 对象变换配对）：导出侧把 deform pass 换绑到本组
                # ResourceZZMergedSkeleton_G<N>；VGMap/VGOffset 为全局骨骼编号（组基址拼接）
                submesh_json["SkeletonGroup"] = skeleton_group
                # 本组 cb1 捕获源部件（校准时其渲染 draw 处 copy vs-cb1；空串 = 无捕获源）
                submesh_json["SkeletonGroupCb1SourceIb"] = cb1_source_ib

                # 复制 palette buf 到 ModImpRuntime 缓存（NTEMI/EFMI 同款模式）
                try:
                    bare_name = unique_str.split(".", 1)[-1]
                    submesh_dir = os.path.dirname(os.path.dirname(json_path))
                    runtime_dir = os.path.join(submesh_dir, "ModImpRuntime")
                    os.makedirs(runtime_dir, exist_ok=True)
                    dest_name = f"{bare_name}-BoneMatrix.buf"
                    dest_path = os.path.join(runtime_dir, dest_name)
                    if not os.path.isfile(dest_path) or force:
                        shutil.copy2(group["palette_path"], dest_path)
                    submesh_json["BoneMatrixFileName"] = dest_name
                except Exception as e:
                    print(f"[ZZMI骨骼合并] 复制骨骼缓存失败 {unique_str}: {e}")

                try:
                    JsonUtils.SaveToFile(json_dict=submesh_json, filepath=json_path)
                    written += 1
                except Exception as e:
                    print(f"[ZZMI骨骼合并] 写回 json 失败 {unique_str}: {e}")

        return written > 0, (
            f"已为 {written} 个子网格生成骨骼合并数据"
            f"（{len(ready_groups)} 个部件 / {len(group_members)} 个骨架组，"
            f"全局共 {sum(group_slots.values())} 槽）"
        )
