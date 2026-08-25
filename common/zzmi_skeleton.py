"""
ZZMI（绝区零）骨骼合并支持模块

数据流（实测结论，详见项目根目录 ZZMI骨骼合并计划书.md）：
- ZZZ 渲染为三段式管线：CS 形变 pass -> pointlist 蒙皮变形 pass（下称 deform pass）
  -> 渲染 draw。骨骼 palette 只存在于 **deform pass 的 vs-t0**（每部件一份，
  12 floats / 4x3 矩阵 = 48 字节每骨骼）；渲染 draw 只读 SO 蒙皮输出，无骨骼数据。
- 工作空间子网格 json 自带 join 线索：
  `CategoryHash.Position` == deform pass 的 vb0 资源 hash（路径 A）；
  `VertexLimitVB` == deform pass 的 SO 输出资源 hash（路径 B）；
  兜底用 `LOD0/ComponentName_DrawCallIndexList.json` 的渲染 draw（先按目标 IB 门控）
  -> vb0 -> SO（路径 C）。

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
  1:1 配对，跨组绝不共享骨架）；
- 骨骼 id 为**全局编号**（组基址拼接组内槽位），`VGMap`/`VGOffset` 均为全局口径；
- **无 CB1 校准（2026-08-25 用户拍板）**：运行时每组骨架只写本组骨骼（直拷），
  禁止跨组别骨骼合并——跨组引用会在导出侧大声报警；
- palette buf 复制到 `<子网格>/ModImpRuntime/<bare>-BoneMatrix.buf`（NTEMI/EFMI 缓存模式）。
"""

import hashlib
import os
import re

import numpy

from ..utils.json_utils import JsonUtils
from .efmi_skeleton import EFMIBoneMapBuilder, EFMISkeletonMergeHelper

# 每骨骼矩阵的 float 数（4x3 = 48 字节，已实测确认）
_BONE_MATRIX_FLOATS = 12

# ZZMI 骨骼合并缓存算法版本：快路径幂等判定必须与本版本一致；
# 策略变更时递增，旧缓存会自动整批重建（同 EFMI 的 VGMapAlgorithmVersion 机制）。
# v2：路径 C 与 CB1 分组均加入目标 DrawIB 门控，旧缓存可能已串入相似模型，
# 必须整批重建以清除污染。
# v3：拒绝同一 DrawIB 对应多个对象 CB1 实例的歧义缓存；这类 IB 可能同时被
# 多个相似模型绘制，继续共用一套全局骨架会把修改写入其它实例。
_ZZMI_VG_MAP_ALGORITHM_VERSION = 3
# 导出侧也需要知道当前缓存口径，不能只依赖导入阶段的幂等门控。
ZZMI_VG_MAP_ALGORITHM_VERSION = _ZZMI_VG_MAP_ALGORITHM_VERSION


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
    _DRAW_INDEXED_SINGLE_RE = re.compile(
        r"^DrawIndexed\(IndexCount:(\d+), StartIndexLocation:(\d+), "
        r"BaseVertexLocation:(\d+)\)$"
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
        self.base_dir = os.path.dirname(os.path.abspath(log_path))
        # draw_index -> {"vb": {slot: hash}, "ib": hash, "so": {slot: hash},
        #                "vs_t0": hash, "vs_shader": hash,
        #                "vertex_count": int|None, "draw_indexed": dict|None}
        self.draws: dict[str, dict] = {}
        # 渲染 draw_index -> vs-cb1 dump 的 (逻辑文件名, 记录路径)。
        # 记录路径在 dump 被搬走后会失效，因此始终保存逻辑名并延迟解析
        # （get_render_cb1_path 按候选路径逐一回退）。
        self.render_cb1_dumps: dict[str, tuple[str, str]] = {}
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
        # IASetIndexBuffer 与其它 IA 状态一样可以跨 DrawIndexed 持久生效。
        # 记录最近一次绑定，供没有重复绑定行的后续 render draw 继承。
        current_ib_hash = ""

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
                    current_ib_hash = ib_match.group(1)
                    self._get_draw(draw_index)["ib"] = current_ib_hash
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
                    info = self._get_draw(draw_index)
                    if not info.get("ib") and current_ib_hash:
                        info["ib"] = current_ib_hash
                    info["draw_indexed"] = {
                        "index_count": int(indexed_match.group(1)),
                        "start_index": int(indexed_match.group(3)),
                        "base_vertex": int(indexed_match.group(4)),
                    }
                    continue

                # 部分 ZZZ/3Dmigoto 构建使用非 Instanced 形式；IB/VB 绑定仍然
                # 是同一套渲染身份信息，不能因为 draw 调用形式不同就丢掉它。
                indexed_single_match = self._DRAW_INDEXED_SINGLE_RE.match(payload)
                if indexed_single_match:
                    info = self._get_draw(draw_index)
                    if not info.get("ib") and current_ib_hash:
                        info["ib"] = current_ib_hash
                    info["draw_indexed"] = {
                        "index_count": int(indexed_single_match.group(1)),
                        "start_index": int(indexed_single_match.group(2)),
                        "base_vertex": int(indexed_single_match.group(3)),
                    }
                    continue

                # 资源 dump 映射
                dump_match = self._DUMP_RE.match(payload)
                if dump_match:
                    src_name = os.path.basename(dump_match.group(1))
                    dst_path = dump_match.group(2)
                    if src_name not in self.dump_map:
                        self.dump_map[src_name] = dst_path
                    # vs-cb1 dump（渲染 draw 的对象变换 CB）：按 draw 索引记录
                    # (逻辑文件名, 记录路径)。不校验记录路径是否存在——dump 被
                    # 搬走后原绝对路径失效，get_render_cb1_path 按候选路径延迟解析。
                    cb1_match = self._CB1_DUMP_RE.match(src_name)
                    if cb1_match:
                        draw_key = cb1_match.group(1)
                        if draw_key not in self.render_cb1_dumps:
                            self.render_cb1_dumps[draw_key] = (src_name, dst_path)
                    continue

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def get_vb_hash(self, draw_index: str, slot: int) -> str:
        info = self.draws.get(draw_index)
        return info["vb"].get(slot, "") if info else ""

    def get_ib_hash(self, draw_index: str) -> str:
        """返回渲染 draw 绑定的 IB hash（用于路径 C 的 DrawIB 门控）。"""
        info = self.draws.get(draw_index)
        return str(info.get("ib", "") or "").strip().lower() if info else ""

    def get_render_draw_indices_for_ib(self, draw_ib: str) -> list[str]:
        """返回当前 dump 中绑定目标 IB 的 indexed render draw。

        工作空间的 ComponentName_DrawCallIndexList 可能来自另一帧；当它过期时，
        仍应以当前 log 的 IB hash 反查对象 CB1，而不是静默回退到旧实例缓存。
        """
        expected = str(draw_ib or "").strip().lower()
        if not expected:
            return []
        return sorted(
            draw_index
            for draw_index, info in self.draws.items()
            if info.get("draw_indexed") is not None
            and str(info.get("ib", "") or "").strip().lower() == expected
        )

    def _deduped_candidates(self, dst_path: str | None) -> list[str]:
        """生成 deduped 候选路径：log 记录的原路径 + dump 目录 deduped/ 下的同名文件。"""
        candidates = []
        if dst_path:
            candidates.append(dst_path)
            basename = os.path.basename(dst_path)
            if basename:
                candidates.append(os.path.join(self.base_dir, "deduped", basename))
        return candidates

    def get_render_cb1_path(self, draw_index: str) -> str | None:
        """渲染 draw 的 vs-cb1 dump 实际路径（对象变换 CB；延迟解析）。

        log 记录的绝对路径在 FrameAnalysis 被搬走后失效：按候选路径
        （记录路径 -> 当前 dump 目录 deduped/<同名文件>）逐一回退。
        未 dump 或全部候选失效返回 None。
        """
        record = self.render_cb1_dumps.get(draw_index)
        if record is None:
            return None
        _logical_name, dst_path = record
        for candidate in self._deduped_candidates(dst_path):
            if os.path.isfile(candidate):
                return candidate
        return None

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
        """按根目录逻辑文件名（如 000002-vs-t0=...buf）查 deduped 实际路径。

        log.txt 里记录的 deduped 绝对路径可能是提取时的路径，FrameAnalysis 被
        搬走后失效：先按 log 记录的路径，失效时用文件名在**当前** dump 目录的
        deduped/ 子目录兜底定位（deduped 文件名是内容 hash，唯一）。
        """
        dst = self.dump_map.get(logical_filename)
        for candidate in self._deduped_candidates(dst):
            if os.path.isfile(candidate):
                return candidate
        # 兜底：按 draw+槽位前缀匹配（逻辑名可能缺 -vs= 尾段）
        prefix = logical_filename.split("=", 1)[0] + "="
        for src, dst2 in self.dump_map.items():
            if src.startswith(prefix):
                for candidate in self._deduped_candidates(dst2):
                    if os.path.isfile(candidate):
                        return candidate
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
        expected_draw_ib: str = "",
    ) -> tuple[str, dict, str]:
        """返回 (draw_index, deform_pass, 命中路径"A"/"B"/"C")；未命中返回 ("", None, "")。

        - 路径 A：子网格 json CategoryHash.Position == deform vb0 hash（最直接）；
        - 路径 B：子网格 json VertexLimitVB == deform SO 输出 hash；
        - 路径 C：渲染 draw（ComponentName_DrawCallIndexList.json）的 vb0 == SO 输出 hash，
          且 IASetIndexBuffer == 目标 DrawIB。

        路径 C 是兜底路径，映射列表可能来自不同帧或包含相似模型的 draw；只按
        SO/vb0 hash 会把另一个模型的 deform pass 归给当前子网格。生产调用必须传入
        目标 DrawIB，先做 IB 门控再做 SO hash join。
        """
        position_hash = str(position_hash or "").strip().lower()
        if position_hash and position_hash in self.by_vb0:
            draw_index, deform_pass = self.by_vb0[position_hash]
            return draw_index, deform_pass, "A"

        vertex_limit_hash = str(vertex_limit_hash or "").strip().lower()
        if vertex_limit_hash and vertex_limit_hash in self.by_so:
            draw_index, deform_pass = self.by_so[vertex_limit_hash]
            return draw_index, deform_pass, "B"

        expected_draw_ib = str(expected_draw_ib or "").strip().lower()
        if not expected_draw_ib:
            # 路径 C 没有其它能确认模型身份的可靠键；宁可不生成，也不能
            # 在相似模型之间按 SO/vb0 hash 猜一个 deform pass。
            return "", None, ""
        for render_draw in render_draw_indices or []:
            if self.parser.get_ib_hash(render_draw) != expected_draw_ib:
                continue
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

    @staticmethod
    def _configured_frame_analysis_paths(workspace_root: str) -> list[str]:
        """读取 ZZMI 工作区显式记录的 FrameAnalysis 路径。"""
        paths: list[str] = []
        config_path = os.path.join(workspace_root, "Config", "FrameAnalysisPath.json")
        if os.path.isfile(config_path):
            try:
                payload = JsonUtils.LoadFromFile(config_path)
                path = str(payload.get("frameAnalysisFolderPath", "") or "").strip()
                if path:
                    paths.append(path)
            except Exception:
                pass

        tabs_dir = os.path.join(workspace_root, "Config", "Tabs")
        if os.path.isdir(tabs_dir):
            for tab_file in sorted(os.listdir(tabs_dir)):
                if not tab_file.startswith("ws-tab-") or not tab_file.endswith(".json"):
                    continue
                try:
                    payload = JsonUtils.LoadFromFile(os.path.join(tabs_dir, tab_file))
                    path = str(payload.get("frameAnalysisFolderPath", "") or "").strip()
                    if path:
                        paths.append(path)
                except Exception:
                    continue
        return paths

    @classmethod
    def resolve_frame_analysis_dir(cls, workspace_root: str) -> str:
        """定位 ZZMI 的 FrameAnalysis；显式失效路径不自动偷换成另一帧。"""
        configured_paths = cls._configured_frame_analysis_paths(workspace_root)
        if configured_paths and not any(os.path.isdir(path) for path in configured_paths):
            # 工作区已经记录过提取源，但该源被用户删除/搬走：优先使用导入时
            # 搬进 ModImpRuntime 的 palette/ObjectCB1 缓存，不能从当前游戏目录
            # 随便挑一个最新 FrameAnalysis，尤其不能把相似模型的 CB1 混进来。
            return ""
        return EFMISkeletonMergeHelper.resolve_frame_analysis_dir(workspace_root)
    _resolve_submesh_json_path = staticmethod(
        EFMISkeletonMergeHelper._resolve_submesh_json_path
    )
    _load_drawcall_index_list = staticmethod(
        EFMISkeletonMergeHelper.load_drawcall_index_list
    )
    _parse_blend_element_info = staticmethod(
        EFMISkeletonMergeHelper.parse_blend_element_info
    )

    @staticmethod
    def _merge_driven_signatures(target: dict, incoming: dict) -> dict:
        """合并同 DrawIB 拆分子网格的局部骨骼驱动签名。"""
        merged = dict(target or {})
        for local_id, new_sig in (incoming or {}).items():
            local_id = int(local_id)
            old_sig = merged.get(local_id)
            if old_sig is None:
                merged[local_id] = dict(new_sig)
                continue

            old_points = numpy.asarray(
                old_sig.get("diffusion_points", []), dtype=numpy.float32
            ).reshape(-1, 3)
            new_points = numpy.asarray(
                new_sig.get("diffusion_points", []), dtype=numpy.float32
            ).reshape(-1, 3)
            points = numpy.concatenate((old_points, new_points), axis=0)
            old_weights = numpy.asarray(
                old_sig.get("diffusion_weights", []), dtype=numpy.float32
            ).reshape(-1)
            new_weights = numpy.asarray(
                new_sig.get("diffusion_weights", []), dtype=numpy.float32
            ).reshape(-1)
            weights = numpy.concatenate((old_weights, new_weights), axis=0)
            if len(points) > 256:
                sample_index = numpy.linspace(0, len(points) - 1, 256, dtype=numpy.int64)
                points = points[sample_index]
                weights = weights[sample_index]

            old_total = float(old_sig.get("weight_total", 0.0) or 0.0)
            new_total = float(new_sig.get("weight_total", 0.0) or 0.0)
            weight_total = old_total + new_total
            if weight_total > 0:
                centroid = (
                    numpy.asarray(old_sig.get("centroid", (0, 0, 0)), dtype=numpy.float64)
                    * old_total
                    + numpy.asarray(new_sig.get("centroid", (0, 0, 0)), dtype=numpy.float64)
                    * new_total
                ) / weight_total
            elif len(points):
                centroid = numpy.mean(points, axis=0, dtype=numpy.float64)
            else:
                centroid = numpy.zeros(3, dtype=numpy.float64)

            vertex_count = int(old_sig.get("vertex_count", 0) or 0) + int(
                new_sig.get("vertex_count", 0) or 0
            )
            if len(points) and len(weights) == len(points) and float(weights.sum()) > 0:
                sampled_total = float(weights.sum())
                sampled_mean_sq = float(
                    (weights * ((points - centroid) ** 2).sum(axis=1)).sum()
                    / sampled_total
                )
                spread = float(numpy.sqrt(max(sampled_mean_sq, 0.0)))
            else:
                spread = max(
                    float(old_sig.get("spread", 0.0) or 0.0),
                    float(new_sig.get("spread", 0.0) or 0.0),
                )
            merged[local_id] = {
                "centroid": numpy.asarray(centroid, dtype=numpy.float32),
                "bbox_min": numpy.minimum(
                    numpy.asarray(old_sig.get("bbox_min", centroid), dtype=numpy.float32),
                    numpy.asarray(new_sig.get("bbox_min", centroid), dtype=numpy.float32),
                ),
                "bbox_max": numpy.maximum(
                    numpy.asarray(old_sig.get("bbox_max", centroid), dtype=numpy.float32),
                    numpy.asarray(new_sig.get("bbox_max", centroid), dtype=numpy.float32),
                ),
                "vertex_count": vertex_count,
                "spread": spread,
                "weight_total": weight_total,
                "mean_weight": weight_total / max(vertex_count, 1),
                "diffusion_points": points,
                "diffusion_weights": weights,
                "diffusion_radius": EFMIBoneMapBuilder._diffusion_radius(points),
                "diffusion_normals": EFMIBoneMapBuilder._estimate_diffusion_normals(points),
            }
        return merged

    @staticmethod
    def _runtime_cache_path(
        submesh_json: dict,
        json_path: str,
        unique_str: str,
        cb1_file_name: str | None = None,
    ) -> str:
        """解析子网格 json 运行时缓存文件的路径（ModImpRuntime 下）。

        默认解析 BoneMatrixFileName 指向的骨骼缓存路径；cb1_file_name="ObjectCB1"
        时解析对象变换 CB 缓存（<bare>-ObjectCB1.buf，优先 json 的 ObjectCB1FileName）。
        只接受纯文件名（拒绝路径穿越）；文件可能不存在，由调用方 isfile 校验。
        """
        if cb1_file_name:
            file_name = str(submesh_json.get("ObjectCB1FileName", "") or "").strip()
            if not file_name or os.path.basename(file_name) != file_name:
                bare_name = unique_str.split(".", 1)[-1]
                file_name = f"{bare_name}-{cb1_file_name}.buf"
        else:
            file_name = str(submesh_json.get("BoneMatrixFileName", "") or "").strip()
            if not file_name or os.path.basename(file_name) != file_name:
                bare_name = unique_str.split(".", 1)[-1]
                file_name = f"{bare_name}-BoneMatrix.buf"
        submesh_dir = os.path.dirname(os.path.dirname(json_path))
        return os.path.join(submesh_dir, "ModImpRuntime", file_name)

    @classmethod
    def _zzmi_cache_intact(cls, submesh_json: dict, json_path: str, unique_str: str) -> bool:
        """ZZMI 缓存快路径完整性校验（schema + 算法版本 + 映射覆盖 + 缓存文件）。

        任何一项缺失都判定缓存不完整，走整批重建——复制骨骼缓存失败或工作空间
        搬迁漏掉 ModImpRuntime 时，绝不允许带着半成品 VGMap 永久幂等跳过。校验项：
        - SkeletonGroup / DeformDrawIndex / OriginalVertexCount 是非负整数；
        - VGMapAlgorithmVersion == 当前算法版本；
        - VGCount / VGOffset 存在且非负；
        - VGMap 键完整覆盖 0..VGCount-1、len(VGMap) == VGCount、全局槽位非负
          （缺键或规范化重复键会让导入/导出映射口径不一致）；
        - BoneMatrixFileName 指向的 ModImpRuntime 文件存在且大小
          >= VGCount * 48 字节（每骨骼 4x3 float32）。
        - 有当前 dump 时，FrameAnalysisLogSignature 必须与 log.txt 内容一致。
        """
        def _strict_int(value) -> int:
            if isinstance(value, bool):
                raise TypeError("bool 不是缓存整数")
            if isinstance(value, float) and not value.is_integer():
                raise ValueError("非整数浮点值")
            return int(value)

        try:
            cache_version = _strict_int(
                submesh_json.get("VGMapAlgorithmVersion", 0) or 0
            )
        except (TypeError, ValueError):
            cache_version = 0
        if cache_version != _ZZMI_VG_MAP_ALGORITHM_VERSION:
            return False
        try:
            metadata_values = (
                submesh_json["SkeletonGroup"],
                submesh_json["DeformDrawIndex"],
                submesh_json["OriginalVertexCount"],
            )
            skeleton_group, deform_draw_index, original_vertex_count = (
                _strict_int(value) for value in metadata_values
            )
        except (KeyError, TypeError, ValueError):
            return False
        if (
            skeleton_group < 0
            or deform_draw_index < 0
            or original_vertex_count < 0
            or skeleton_group > 0xFFFFFFFF
            or deform_draw_index > 0xFFFFFFFF
            or original_vertex_count > 0xFFFFFFFF
        ):
            return False

        vg_map = submesh_json.get("VGMap")
        if not isinstance(vg_map, dict) or not vg_map:
            return False
        try:
            vg_count = _strict_int(submesh_json.get("VGCount"))
            vg_offset = _strict_int(submesh_json.get("VGOffset"))
            mapped = {}
            for key, value in vg_map.items():
                normalized_key = _strict_int(key)
                if normalized_key in mapped:
                    return False
                mapped[normalized_key] = _strict_int(value)
        except (TypeError, ValueError):
            return False
        if (
            vg_count <= 0
            or vg_count > 0xFFFFFFFF
            or vg_offset < 0
            or vg_offset > 0xFFFFFFFF
            or vg_offset + vg_count > 0x100000000
        ):
            return False
        if len(vg_map) != vg_count or len(mapped) != vg_count:
            return False
        if set(mapped.keys()) != set(range(vg_count)):
            return False
        if any(slot < 0 or slot > 0xFFFFFFFF for slot in mapped.values()):
            return False

        bone_matrix_path = cls._runtime_cache_path(submesh_json, json_path, unique_str)
        if not os.path.isfile(bone_matrix_path):
            return False
        if not EFMIBoneMapBuilder.cache_file_size_ok(bone_matrix_path, vg_count):
            return False
        return True

    @staticmethod
    def _frame_analysis_log_signature(log_path: str) -> str:
        """返回当前 FrameAnalysis/log.txt 的内容指纹，用于缓存源一致性校验。"""
        digest = hashlib.sha256()
        with open(log_path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def _zzmi_source_cache_intact(
        cls,
        submesh_json: dict,
        json_path: str,
        unique_str: str,
    ) -> bool:
        """校验可供未来无 dump 重建的 ZZMI CB1 来源缓存契约。

        新版每次生成都会显式写入 ``ObjectCB1CacheValid``：True 必须指向
        可解析的实际 CB1，False 表示当前 dump 没有可用对象变换、应独立分组。
        缺少该字段的是旧版缓存，不能证明目录中的 CB1 就是当时实际参与分组的
        候选；dump 尚在时必须迁移，dump 已删后的重建则按不可信处理。
        """
        marker = submesh_json.get("ObjectCB1CacheValid")
        if not isinstance(marker, bool):
            return False
        if marker is False:
            return True
        cb1_path = cls._runtime_cache_path(
            submesh_json,
            json_path,
            unique_str,
            cb1_file_name="ObjectCB1",
        )
        return bool(
            os.path.isfile(cb1_path)
            and ZZMIBoneMapBuilder.parse_object_transform(cb1_path) is not None
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
        log_path = os.path.join(frame_analysis_dir, "log.txt") if frame_analysis_dir else ""
        parser = None
        resolver = None
        frame_analysis_signature = ""
        if frame_analysis_dir:
            if not os.path.isfile(log_path):
                print(
                    f"[ZZMI骨骼合并] 提示: FrameAnalysis 缺少 log.txt（{log_path}），"
                    "将仅用工作空间缓存重建"
                )
            else:
                parser = ZZMILogParser(log_path)
                resolver = ZZMIDeformResolver(parser)
                frame_analysis_signature = cls._frame_analysis_log_signature(log_path)
                if not resolver.passes:
                    print(
                        "[ZZMI骨骼合并] 提示: FrameAnalysis log 中未识别到 deform pass，"
                        "将仅用工作空间缓存重建"
                    )
                    parser = None
                    resolver = None
        else:
            # dump 目录已被删除：上次导入已把 palette / 对象变换 CB 复制进工作空间
            # ModImpRuntime，缓存完整的子网格可以脱离 dump 重建。
            print(
                "[ZZMI骨骼合并] 提示: 未找到 FrameAnalysis 目录（可能已被删除），"
                "将仅用工作空间缓存重建"
            )

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
        # DeformDrawIndex/OriginalVertexCount 属于导出侧守卫所需 schema（合并网格
        # 的 deform 时序校验 + 渲染 vb1 换绑），同样纳入门控：旧缓存缺字段时
        # 整批重建刷新。
        # 快路径还校验**缓存产物完整性**：算法版本必须与当前一致；BoneMatrixFileName
        # 引用的骨骼缓存文件必须真实存在。否则（复制失败 / 工作空间搬迁漏掉
        # ModImpRuntime / 旧算法缓存）整批重建，绝不允许带着半成品永久幂等跳过。
        groups: dict[str, dict] = {}
        stale = 0
        # 无法定位/无法解析 json 的目标（最后按“未处理目标”计入失败报告，
        # 不能让部分完成被报告成完整成功）
        unresolved_targets: list[str] = []
        seen_members: set[str] = set()

        for unique_str in unique_str_list:
            if unique_str in seen_members:
                continue  # 重复目标只处理一次（计数口径与写回均不重复）
            seen_members.add(unique_str)
            json_path = cls._resolve_submesh_json_path(workspace_root, unique_str)
            if not json_path:
                print(f"[ZZMI骨骼合并] 跳过 {unique_str}: 未找到子网格 json")
                unresolved_targets.append(unique_str)
                continue

            submesh_json = JsonUtils.LoadFromFile(json_path)
            if not isinstance(submesh_json, dict):
                print(f"[ZZMI骨骼合并] 跳过 {unique_str}: 子网格 json 读取失败")
                unresolved_targets.append(unique_str)
                continue

            bare_name = unique_str.split(".", 1)[-1]
            draw_ib = bare_name.split("-")[0] if "-" in bare_name else bare_name
            if not draw_ib:
                print(f"[ZZMI骨骼合并] 跳过 {unique_str}: 无法解析 DrawIB")
                unresolved_targets.append(unique_str)
                continue

            group = groups.get(draw_ib)
            if group is None:
                group = {
                    "palette": None,
                    "vg_count": 0,
                    "original_vertex_counts": {},
                    "members": [],
                    "json_paths": {},
                    "palette_path": "",
                    "draw_index": "",
                    "signatures": {},
                    "transform": None,
                    "cb1_path": "",
                    "cb1_cache_valid": False,
                    "skip_reason": "",
                    "representative": unique_str,
                }
                groups[draw_ib] = group
            group["members"].append(unique_str)
            group["json_paths"][unique_str] = json_path

            cache_intact = cls._zzmi_cache_intact(
                submesh_json, json_path, unique_str
            )
            if (
                cache_intact
                and parser is not None
                and submesh_json.get("FrameAnalysisLogSignature") != frame_analysis_signature
            ):
                cache_intact = False
            if (
                cache_intact
                and parser is not None
                and not cls._zzmi_source_cache_intact(
                    submesh_json, json_path, unique_str
                )
            ):
                # dump 尚在时自动迁移旧版/缺件来源缓存，避免本次幂等跳过后
                # 用户清 VGMap、删 dump 才发现 CB1 不可复现。
                cache_intact = False
            if not force and cache_intact:
                continue  # 该子网格缓存完整（临时计数，见下）
            stale += 1

        up_to_date = len(groups) and stale == 0
        if up_to_date and not force:
            total = sum(len(g["members"]) for g in groups.values())
            if unresolved_targets:
                # 目标目录被删/无法解析时绝不能报“全部已缓存”：这些目标
                # 没有进入任何组，必须显式失败，否则会被幂等快路径静默遗漏。
                shown = unresolved_targets[:5]
                suffix = "…" if len(unresolved_targets) > 5 else ""
                return False, (
                    f"{total} 个子网格已有骨骼合并数据（幂等），但 "
                    f"{len(unresolved_targets)} 个目标无法解析: "
                    f"{'、'.join(shown)}{suffix}"
                )
            return True, f"所有 {total} 个子网格均已有骨骼合并数据（幂等跳过）。"

        # 第二遍：逐组反查 deform pass -> palette，读取 Blend.buf 得 vg_count
        for draw_ib, group in groups.items():
            if group.get("skip_reason") and parser is None:
                continue
            representative = group["representative"]
            json_path = group["json_paths"][representative]
            submesh_json = JsonUtils.LoadFromFile(json_path)
            if not isinstance(submesh_json, dict):
                group["skip_reason"] = "子网格 json 读取失败"
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
            parser_render_draws_for_ib = []
            if parser is not None:
                # 组件索引表可能仍指向上一帧；当前 log 的 IB hash 才是本次
                # CB1/实例判定的权威来源。合并两者，旧索引在后续 IB 门控中会
                # 自动被过滤；新索引则能发现同一 IB 的多个对象实例。
                parser_render_draws_for_ib = parser.get_render_draw_indices_for_ib(draw_ib)
                render_draws = sorted(
                    set(render_draws)
                    | set(parser_render_draws_for_ib)
                )

            draw_index, deform_pass, via = "", None, ""
            if resolver is not None:
                draw_index, deform_pass, via = resolver.resolve(
                    position_hash=position_hash,
                    vertex_limit_hash=vertex_limit_hash,
                    render_draw_indices=render_draws,
                    expected_draw_ib=draw_ib,
                )

            palette_logical = ""
            if deform_pass is not None:
                palette_logical = f"{draw_index}-vs-t0={deform_pass['palette_hash']}"
            palette_path = parser.get_deduped_path(palette_logical) if parser is not None else None
            if not palette_path:
                # dump 被删除/搬走后的兜底：上次导入已把 palette 复制到工作空间
                # ModImpRuntime/<bare>-BoneMatrix.buf（导出侧同款缓存）。同 DrawIB
                # 的拆分子网格共享同一 palette，因此**遍历组内全部成员**找缓存——
                # 代表子网格的缓存丢失时，兄弟子网格的缓存同样有效。
                for member in group["members"]:
                    member_json_path = group["json_paths"].get(member, "")
                    if not member_json_path:
                        continue
                    try:
                        member_json = JsonUtils.LoadFromFile(member_json_path)
                    except Exception:
                        member_json = None
                    if not isinstance(member_json, dict):
                        member_json = {}
                    cached_path = cls._runtime_cache_path(
                        member_json, member_json_path, member
                    )
                    if os.path.isfile(cached_path):
                        palette_path = cached_path
                        print(
                            f"[ZZMI骨骼合并] {draw_ib}: dump 中 palette 缺失，"
                            f"已回退工作空间缓存（{member}）"
                        )
                        break
            if not palette_path:
                print(f"[ZZMI骨骼合并] 跳过 {draw_ib}: palette 缺失 {palette_logical or '(工作空间缓存也不存在)'}")
                group["skip_reason"] = "palette 缺失（dump 与全部成员缓存均无）"
                continue

            # draw_index：dump 可用时来自 deform pass 反查；否则回退上次写回的
            # DeformDrawIndex 元数据（导出侧守卫时序校验依赖它）。同 DrawIB 成员
            # 共享同一序号，代表子网格缺字段时遍历兄弟子网格。
            if not draw_index:
                cached_deform_index = None
                for member in group["members"]:
                    member_json_path = group["json_paths"].get(member, "")
                    if not member_json_path:
                        continue
                    try:
                        member_json = JsonUtils.LoadFromFile(member_json_path)
                    except Exception:
                        member_json = None
                    if not isinstance(member_json, dict):
                        continue
                    cached_deform_index = member_json.get("DeformDrawIndex")
                    if cached_deform_index is not None:
                        break
                try:
                    draw_index = str(int(cached_deform_index)).zfill(6) if cached_deform_index is not None else ""
                except (TypeError, ValueError):
                    draw_index = ""
            if not draw_index:
                print(f"[ZZMI骨骼合并] 跳过 {draw_ib}: 无 deform draw 序号（dump 与缓存均缺失）")
                group["skip_reason"] = "无 deform draw 序号"
                continue

            try:
                palette = ZZMIBoneMapBuilder.load_palette(palette_path)
            except Exception as e:
                print(f"[ZZMI骨骼合并] 跳过 {draw_ib}: 读取 palette 失败: {e}")
                group["skip_reason"] = f"palette 读取失败: {e}"
                continue

            # vg_count：同 DrawIB 的全部拆分子网格共同决定。每个成员可能使用
            # 不同的局部骨骼；只解析代表成员会截短 palette 并生成不完整 VGMap。
            # 每个 Blend.buf 的 BLENDINDICES 有效通道最大索引 + 1。
            # 有效通道 = 索引非哨兵（按数据格式：u1->0xFF / u2->0xFFFF /
            # u4|i4->0xFFFFFFFF）且对应 BLENDWEIGHTS > 0；无权重元素时按
            # “每顶点第一索引权重=1”兜底（与导入侧默认权重语义一致）。
            # 否则 R16_UINT 等格式的空通道哨兵 0xFFFF 会被算成真实骨骼，
            # vg_count 膨胀到 65,536 后因 palette 不足整部件被跳过。
            vg_count = 0
            combined_signatures = {}
            member_parse_failed = False
            for member in group["members"]:
                member_json_path = group["json_paths"][member]
                try:
                    member_json = JsonUtils.LoadFromFile(member_json_path)
                except Exception as e:
                    group["skip_reason"] = f"{member}: 子网格 json 读取失败: {e}"
                    member_parse_failed = True
                    break
                if not isinstance(member_json, dict):
                    group["skip_reason"] = f"{member}: 子网格 json 读取失败"
                    member_parse_failed = True
                    break

                element_info = cls._parse_blend_element_info(member_json)
                if element_info is None:
                    group["skip_reason"] = f"{member}: Blend 类别缺少 BLENDINDICES 元素"
                    member_parse_failed = True
                    break
                blend_buf_path = os.path.join(
                    os.path.dirname(member_json_path),
                    os.path.splitext(os.path.basename(member_json_path))[0] + "-Blend.buf",
                )
                try:
                    blend_indices = EFMIBoneMapBuilder.parse_blendindices_from_buf(
                        blend_buf_path, element_info
                    )
                    blend_layout = EFMIBoneMapBuilder.parse_blend_layout(member_json)
                    blend_weights = EFMIBoneMapBuilder.parse_blendweights_from_buf(
                        blend_buf_path, blend_layout
                    )
                except Exception as e:
                    group["skip_reason"] = f"{member}: 读取 Blend.buf 失败: {e}"
                    member_parse_failed = True
                    break

                valid_mask = EFMIBoneMapBuilder.valid_blend_channels(
                    blend_indices, element_info, blend_weights
                )
                valid_indices = blend_indices[valid_mask].astype(numpy.int64)
                if len(valid_indices) == 0:
                    group["skip_reason"] = f"{member}: BLENDINDICES 无有效通道"
                    member_parse_failed = True
                    break
                vg_count = max(vg_count, int(valid_indices.max()) + 1)
                # 导出侧按成员自己的几何行数做 vb1 换绑判定。
                group["original_vertex_counts"][member] = int(len(blend_indices))

                position_buf_path = os.path.join(
                    os.path.dirname(member_json_path),
                    os.path.splitext(os.path.basename(member_json_path))[0] + "-Position.buf",
                )
                try:
                    member_signatures = EFMIBoneMapBuilder.compute_driven_signatures(
                        position_buf_path, blend_buf_path, member_json
                    )
                    combined_signatures = cls._merge_driven_signatures(
                        combined_signatures, member_signatures
                    )
                except Exception as e:
                    print(
                        f"[ZZMI骨骼合并] 提示 {member}: 驱动签名计算失败"
                        f"（刚性命中对将拆开）: {e}"
                    )

            if member_parse_failed:
                print(f"[ZZMI骨骼合并] 跳过 {draw_ib}: {group['skip_reason']}")
                continue

            if len(palette) < vg_count:
                print(
                    f"[ZZMI骨骼合并] 跳过 {draw_ib}: palette {len(palette)} 根骨骼 < "
                    f"顶点组 {vg_count}（数据不一致）"
                )
                group["skip_reason"] = f"palette 骨骼数 {len(palette)} < 顶点组 {vg_count}"
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
            group["signatures"] = combined_signatures

            # 骨架分组键：渲染 draw 的 vs-cb1 对象→世界矩阵（palette 蒙皮到对象空间，
            # 渲染 VS 用 cb1 摆到世界，两者逐物体 1:1 配对）。取该部件渲染 draw 列表中
            # 第一个可解析的逐部件 cb1 块。当前 dump 明确无有效 CB1 时可独立成组；
            # 仅靠工作空间重建时，标记为有效/未标记的来源缺失必须显式失败。
            # 解析顺序：当前 dump（按候选路径延迟解析）-> 上次导入复制到工作空间的
            # ObjectCB1 缓存（dump 已删除时仍能重建出相同的骨架分组）。同 DrawIB
            # 成员共享同一对象变换，代表子网格缓存缺失时遍历兄弟子网格。
            cb1_cache_paths = []
            cb1_cache_required = False
            cb1_cache_contract_unknown = False
            for member in group["members"]:
                member_json_path = group["json_paths"].get(member, "")
                if not member_json_path:
                    continue
                try:
                    member_json = JsonUtils.LoadFromFile(member_json_path)
                except Exception:
                    member_json = None
                if not isinstance(member_json, dict):
                    member_json = {}
                marker = member_json.get("ObjectCB1CacheValid")
                if marker is True:
                    cb1_cache_required = True
                elif marker is False:
                    # 只有新版明确验证过的 CB1 才能参与 cache-only 分组。False
                    # 表示当前 dump 曾判定无效，必须忽略残留文件。
                    continue
                else:
                    # 未标记的旧版来源无法证明曾参与 SkeletonGroup；有 dump 时
                    # 可以迁移，无 dump 时不得静默按独立对象重分组。
                    cb1_cache_contract_unknown = True
                    continue
                member_cb1 = cls._runtime_cache_path(
                    member_json, member_json_path, member, cb1_file_name="ObjectCB1"
                )
                if os.path.isfile(member_cb1):
                    cb1_cache_paths.append(member_cb1)
            cb1_path = ""
            dump_cb1_seen = False
            first_dump_cb1_path = ""
            dump_transform_candidates = []
            for render_draw in sorted(render_draws):
                # ComponentName 映射可能包含相似模型的渲染 draw；CB1 是对象空间分组键，
                # 不能只按 draw 序号/文件存在就采纳，否则当前部件会被搬进别的模型的
                # SkeletonGroup。只有该渲染 draw 明确绑定目标 DrawIB 时才允许参与分组。
                if parser is not None and parser.get_ib_hash(render_draw) != draw_ib:
                    continue
                candidate = parser.get_render_cb1_path(render_draw) if parser is not None else None
                if candidate:
                    dump_cb1_seen = True
                    if not first_dump_cb1_path:
                        first_dump_cb1_path = candidate
                    transform = ZZMIBoneMapBuilder.parse_object_transform(candidate)
                    if transform is not None:
                        dump_transform_candidates.append((transform, candidate))

            # 一个 DrawIB 可能在同一帧被多个实例绘制；它们的 IB/VB hash 相同，
            # 但对象 CB1 不同。旧实现无条件取第一个 CB1，随后把所有实例当成
            # 一个 SkeletonGroup，导出时修改其中一个实例会污染另一个。没有额外
            # 的实例选择键时，安全策略是拒绝该 DrawIB 的合并缓存，而不是猜一个。
            unique_dump_transforms = {}
            for transform, candidate in dump_transform_candidates:
                unique_dump_transforms.setdefault(transform, candidate)
            if len(unique_dump_transforms) > 1:
                group["skip_reason"] = (
                    f"同一 DrawIB 对应 {len(unique_dump_transforms)} 个不同对象 CB1 实例，"
                    "无法安全确定目标模型；请使用只包含单个实例的 FrameAnalysis"
                )
                group["palette"] = None
                group["cb1_path"] = ""
                group["cb1_cache_valid"] = False
                print(f"[ZZMI骨骼合并] 跳过 {draw_ib}: {group['skip_reason']}")
                continue
            if unique_dump_transforms:
                # 必须缓存“实际用于分组”的同一个有效 CB1。旧实现先记住首个
                # 候选，即使后续候选才有效，也会把错误实例的 CB1 发布到工作空间。
                group["transform"], cb1_path = next(iter(unique_dump_transforms.items()))
                group["cb1_cache_valid"] = True
            if (
                group["transform"] is None
                and parser is not None
                and parser_render_draws_for_ib
                and cb1_cache_required
                and not dump_cb1_seen
            ):
                group["skip_reason"] = (
                    "当前 FrameAnalysis 命中了目标 IB，但没有可用对象 CB1；"
                    "不能回退到其它帧的 ObjectCB1 对象实例缓存"
                )
                group["palette"] = None
                print(f"[ZZMI骨骼合并] 跳过 {draw_ib}: {group['skip_reason']}")
                continue
            if group["transform"] is None and not dump_cb1_seen and parser is None:
                for member_cb1 in cb1_cache_paths:
                    transform = ZZMIBoneMapBuilder.parse_object_transform(member_cb1)
                    if transform is not None:
                        group["transform"] = transform
                        group["cb1_cache_valid"] = True
                        if not cb1_path:
                            cb1_path = member_cb1
                        print(
                            f"[ZZMI骨骼合并] {draw_ib}: dump 中 cb1 缺失，"
                            f"已回退工作空间 ObjectCB1 缓存（{os.path.basename(os.path.dirname(os.path.dirname(member_cb1)))}）"
                        )
                        break
                if (
                    group["transform"] is None
                    and (
                        cb1_cache_required
                        or (cb1_cache_contract_unknown and parser is None)
                    )
                ):
                    group["skip_reason"] = (
                        "工作空间 ObjectCB1 来源缓存缺失、损坏或未标记，"
                        "无法保持原 SkeletonGroup"
                    )
                    # 来源契约不完整时禁止进入 ready_groups；否则后续会把
                    # transform=None 当作独立对象，静默改写正确分组。
                    group["palette"] = None
                    print(f"[ZZMI骨骼合并] 跳过 {draw_ib}: {group['skip_reason']}")
                    continue
            elif group["transform"] is None and first_dump_cb1_path:
                # 当前 dump 确实给出了 CB1，但其内容不适合作为对象变换。
                # 仍刷新原始文件副本（便于诊断/保持“当前源覆盖旧源”），同时
                # 以 ObjectCB1CacheValid=False 明确禁止 cache-only 路径使用它。
                cb1_path = first_dump_cb1_path
            group["cb1_path"] = cb1_path
            if group["transform"] is None:
                print(f"[ZZMI骨骼合并] 提示 {draw_ib}: 未能解析对象变换（渲染 cb1），独立成组")

        ready_groups = {
            draw_ib: group for draw_ib, group in groups.items() if group["palette"] is not None
        }
        unprocessed_groups = {
            draw_ib: (group.get("skip_reason") or "未知原因")
            for draw_ib, group in groups.items()
            if group["palette"] is None
        }
        if not ready_groups:
            failure_parts: list[str] = []
            if unprocessed_groups:
                detail = "、".join(
                    f"{draw_ib}({reason})"
                    for draw_ib, reason in sorted(unprocessed_groups.items())
                )
                failure_parts.append(f"{len(unprocessed_groups)} 个部件未生成: {detail}")
            if unresolved_targets:
                shown = unresolved_targets[:5]
                suffix = "…" if len(unresolved_targets) > 5 else ""
                failure_parts.append(
                    f"{len(unresolved_targets)} 个目标无法解析: {'、'.join(shown)}{suffix}"
                )
            if failure_parts:
                return False, "没有子网格成功生成骨骼数据（" + "；".join(failure_parts) + "）"
            return False, "没有子网格成功生成骨骼数据。"

        # 第三遍：按对象变换分组（跨组绝不共享骨架），组内 bitwise + 刚性门控去重得
        # 组内槽位；然后按组基址拼接成**全局骨骼编号**（Blender 侧全局命名空间，
        # 组内 join 无歧义）。运行时每组骨架为全宽 buffer，**只直拷本组骨骼**——
        # 无 CB1 校准（用户拍板 2026-08-25），禁止跨组别骨骼合并。
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

        # 写回工作空间 json + 复制 palette 缓存（组内所有子网格写相同结果）
        written = 0
        for draw_ib, group in ready_groups.items():
            vg_map = vg_maps.get(draw_ib, {})
            if not vg_map:
                continue
            vg_offset = vg_offsets[draw_ib]
            vg_count = group["vg_count"]
            skeleton_group = group_of[draw_ib]

            for unique_str in group["members"]:
                json_path = group["json_paths"][unique_str]
                submesh_json = JsonUtils.LoadFromFile(json_path)
                if not isinstance(submesh_json, dict):
                    continue

                submesh_json["VGCount"] = vg_count
                submesh_json["VGOffset"] = vg_offset
                submesh_json["VGMap"] = {str(k): int(v) for k, v in sorted(vg_map.items())}
                # 算法版本：快路径幂等判定依据；策略变更时递增版本使旧缓存自动失效
                submesh_json["VGMapAlgorithmVersion"] = _ZZMI_VG_MAP_ALGORITHM_VERSION
                if frame_analysis_signature:
                    submesh_json["FrameAnalysisLogSignature"] = frame_analysis_signature
                # 骨架分组（渲染 cb1 对象变换配对）：导出侧把 deform pass 换绑到本组
                # ResourceZZMergedSkeleton_G<N>；VGMap/VGOffset 为全局骨骼编号（组基址拼接）
                submesh_json["SkeletonGroup"] = skeleton_group
                # 导出侧守卫元数据：本部件 deform pass 的 draw 序号（组内时序校验——
                # 合并网格必须由组内最后一个 deform draw 蒙皮，否则其它部件骨骼
                # 还是上一帧内容）+ 原部件顶点数（渲染 vb1 换绑判定）
                submesh_json["DeformDrawIndex"] = int(group["draw_index"])
                submesh_json["OriginalVertexCount"] = int(
                    group.get("original_vertex_counts", {}).get(unique_str, 0) or 0
                )

                # palette / CB1 / JSON 作为同一可回滚文件事务发布。重建时始终
                # 刷新当前源；任一文件失败都不得留下新旧混合状态或计入 written。
                try:
                    bare_name = unique_str.split(".", 1)[-1]
                    submesh_dir = os.path.dirname(os.path.dirname(json_path))
                    runtime_dir = os.path.join(submesh_dir, "ModImpRuntime")
                    dest_name = f"{bare_name}-BoneMatrix.buf"
                    dest_path = os.path.join(runtime_dir, dest_name)
                    submesh_json["BoneMatrixFileName"] = dest_name
                    cache_entries = [{
                        "source_path": group["palette_path"],
                        "dest_path": dest_path,
                        "vg_count": vg_count,
                        "min_size": 4,
                    }]

                    # 对象变换 CB 是分组稳定性的缓存来源；存在源时同样原子刷新。
                    if group.get("cb1_path"):
                        cb1_dest_name = f"{bare_name}-ObjectCB1.buf"
                        cb1_dest_path = os.path.join(runtime_dir, cb1_dest_name)
                        cache_entries.append({
                            "source_path": group["cb1_path"],
                            "dest_path": cb1_dest_path,
                            "vg_count": 0,
                            "min_size": 4,
                        })
                        submesh_json["ObjectCB1FileName"] = cb1_dest_name
                        submesh_json["ObjectCB1CacheValid"] = bool(
                            group.get("cb1_cache_valid", False)
                        )
                    else:
                        # 显式沉淀“本次没有有效 CB1”；缓存读取必须忽略可能残留的
                        # 旧文件，才能复现 dump 路径的独立分组语义。
                        submesh_json.pop("ObjectCB1FileName", None)
                        submesh_json["ObjectCB1CacheValid"] = False
                    EFMISkeletonMergeHelper._atomic_publish_skeleton_transaction(
                        cache_entries,
                        submesh_json,
                        json_path,
                    )
                except Exception as e:
                    print(f"[ZZMI骨骼合并] 提交运行时缓存/JSON 事务失败 {unique_str}: {e}")
                    continue

                written += 1

        success_message = (
            f"已为 {written} 个子网格生成骨骼合并数据"
            f"（{len(ready_groups)} 个部件 / {len(group_members)} 个骨架组，"
            f"全局共 {sum(group_slots.values())} 槽）"
        )
        # 完整性报告：只要还有目标未处理（部件未生成 / 目标无法解析 / json 写回
        # 失败），就不能报告完整成功——部分完成必须大声暴露，否则调用方会把
        # 缺失 BoneMatrix 的部件当成已生成。
        failure_parts: list[str] = []
        if unprocessed_groups:
            detail = "、".join(
                f"{draw_ib}({reason})"
                for draw_ib, reason in sorted(unprocessed_groups.items())
            )
            failure_parts.append(f"{len(unprocessed_groups)} 个部件未生成: {detail}")
        if unresolved_targets:
            shown = unresolved_targets[:5]
            suffix = "…" if len(unresolved_targets) > 5 else ""
            failure_parts.append(
                f"{len(unresolved_targets)} 个目标无法解析: {'、'.join(shown)}{suffix}"
            )
        ready_member_total = sum(len(group["members"]) for group in ready_groups.values())
        if written < ready_member_total:
            failure_parts.append(
                f"{ready_member_total - written} 个子网格未生成"
                "（缓存发布或 json 写回失败）"
            )
        if failure_parts:
            return False, success_message + "；" + "；".join(failure_parts)
        return written > 0, success_message
