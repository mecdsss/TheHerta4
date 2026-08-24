from ...blueprint.model import BluePrintModel
from ...common.submesh_model import SubMeshModel
from ...common.drawib_model import DrawIBModel
from dataclasses import dataclass,field
from ...common.global_config import GlobalConfig
from ...common.global_properties import GlobalProterties
from ...blueprint.export_helper import BlueprintExportHelper

from ...common.buffer_export_helper import BufferExportHelper
from ...common.draw_call_model import DrawCallModel
from ...common.global_key_count_helper import GlobalKeyCountHelper
from ...common.m_ini_helper import M_IniHelper
from ...common.m_ini_helper_gui import M_IniHelperGUI
from ...common.m_ini_builder import M_IniBuilder,M_IniSection, M_SectionType
from .export_helper import ExportHelper
from ...utils.json_utils import JsonUtils
from ...utils.timer_utils import TimerUtils

import bpy
import os
import re
import shutil

@dataclass
class ExportEFMI:

    blueprint_model:BluePrintModel

    submesh_model_list:list[SubMeshModel] = field(default_factory=list,init=False)
    drawib_model_list:list[DrawIBModel] = field(default_factory=list,init=False)

    def __post_init__(self):
        # EFMI 骨骼合并（复选框开启时）：为「需要生成但没有对象」的部件自动创建
        # 极限小三角面占位对象（对齐 ZZMI 机制；必须在组装 SubMeshModel 之前注入，
        # 占位部件才能照常进合并骨架：EntryPoint 照常触发、只画不可见小三角）
        self._efmi_stub_object_names = []
        if GlobalProterties.import_merged_vgmap():
            try:
                self._efmi_stub_object_names = self._ensure_stub_objects_for_missing_parts()
            except Exception as e:
                print(f"[EFMI骨骼合并] 占位小三角面创建失败（继续原流程）: {e}")
                self._efmi_stub_object_names = []

        self.submesh_model_list = ExportHelper.parse_submesh_model_list_from_blueprint_model(self.blueprint_model)
        # EFMI 直接复用已经解析好的 SubMeshModel，避免同一轮导出把几何解析做两遍。
        self.drawib_model_list = ExportHelper.parse_drawib_model_list_from_submesh_model_list(
            submesh_model_list=self.submesh_model_list,
            combine_ib=False,
        )
        print("SubMeshModel列表初始化完成，共有 " + str(len(self.submesh_model_list)) + " 个SubMeshModel")

        self.cross_ib_info_dict = self.blueprint_model.cross_ib_info_dict
        self.cross_ib_method_dict = self.blueprint_model.cross_ib_method_dict
        self.has_cross_ib = self.blueprint_model.has_cross_ib
        self.cross_ib_mapping_objects = self.blueprint_model.cross_ib_mapping_objects
        self.cross_ib_vb_condition_mapping = self.blueprint_model.cross_ib_vb_condition_mapping
        self.cross_ib_source_to_target_dict = self.blueprint_model.cross_ib_source_to_target_dict
        self.cross_ib_object_vb_condition = self.blueprint_model.cross_ib_object_vb_condition
        self.cross_ib_target_info = self.blueprint_model.cross_ib_target_info
        self.cross_ib_match_mode = self.blueprint_model.cross_ib_match_mode
        self.cross_ib_object_names = self.blueprint_model.cross_ib_object_names

        self.shader_replace_info_list = getattr(self.blueprint_model, "shader_replace_info_list", [])
        self.shader_replace_object_names = getattr(self.blueprint_model, "shader_replace_object_names", set())
        self.shader_replace_object_info_map = getattr(self.blueprint_model, "shader_replace_object_info_map", {})
        self.has_shader_replace = getattr(self.blueprint_model, "has_shader_replace", False)

        print(f"[CrossIB EFMI] 初始化: has_cross_ib={self.has_cross_ib}")
        print(f"[CrossIB EFMI] cross_ib_info_dict={self.cross_ib_info_dict}")
        print(f"[CrossIB EFMI] cross_ib_object_names={self.cross_ib_object_names}")
        print(f"[CrossIB EFMI] cross_ib_mapping_objects={self.cross_ib_mapping_objects}")

    # ------------------------------------------------------------------
    # 占位小三角面（骨骼合并模式：部件无对象时补齐，对齐 ZZMI 机制）
    # ------------------------------------------------------------------

    def _ensure_stub_objects_for_missing_parts(self) -> list[str]:
        """为「需要生成但没有对象」的部件创建极限小三角面占位对象。

        合并骨架模式下用户可自由 join/删改。占位规则（与 ZZMI 一致）：
        - **部分缺失的 DrawIB**：缺失组件直接补占位（其几何显然被同 DrawIB 的
          幸存对象接管）；
        - **整个 DrawIB 缺席**：看它绑定姿势顶点坐标/VGMap 全局骨骼 id 是否被现存
          对象实际引用——被引用 = 几何被合并进了别的对象 → 全组件补占位（EntryPoint
          照常触发、画不可见小三角，抑制原版绘制防重影）；零引用 = 用户故意不生成
          → 不插桩（该 DrawIB 不进 mod，游戏内显示原版）。
        无反查数据（json 无 VGMap）的缺席 DrawIB 一律不插桩。

        多 LOD 语义（2026-08 实测定案）：LOD0 / LOD1 相互独立——每个 LOD 目录
        （LOD0/LOD1/...）有自己的 DrawIB-Component.json，各自按上述规则独立
        插桩；「被引用」判定只查**同 LOD** 现存对象的顶点组（跨 LOD 组 id 各自
        从 0 起会碰撞，混查会误判），几何判定无命名空间问题可全局查。
        返回创建的对象名列表（export() 结束后清理）。
        """
        workspace_root = GlobalConfig.path_workspace_folder()
        ordered = getattr(self.blueprint_model, "ordered_draw_obj_data_model_list", None)
        if ordered is None:
            return []

        # 收集每个 LOD 目录（+ 根目录兜底）的 DrawIB-Component.json
        lod_component_maps: dict[str, dict] = {}
        if os.path.isdir(workspace_root):
            for entry in os.scandir(workspace_root):
                if not entry.is_dir():
                    continue
                if not re.match(r"^LOD\d+$", entry.name):
                    continue
                map_path = os.path.join(entry.path, "DrawIB-Component.json")
                if not os.path.isfile(map_path):
                    continue
                payload = JsonUtils.LoadFromFile(map_path)
                if isinstance(payload, dict) and payload:
                    lod_component_maps[entry.name] = payload
        root_map_path = os.path.join(workspace_root, "DrawIB-Component.json")
        if os.path.isfile(root_map_path):
            payload = JsonUtils.LoadFromFile(root_map_path)
            if isinstance(payload, dict) and payload:
                lod_component_maps.setdefault("", payload)
        if not lod_component_maps:
            return []

        # 现存对象按 LOD 分组（bare unique_str）
        present_by_lod: dict[str, set[str]] = {}
        for draw_call in ordered:
            try:
                unique_str = str(draw_call.get_workspace_unique_str() or "")
            except Exception:
                continue
            if not unique_str:
                continue
            lod_name = ""
            bare = unique_str
            if unique_str.upper().startswith("LOD") and "." in unique_str:
                dot_idx = unique_str.index(".")
                prefix = unique_str[:dot_idx]
                if prefix[3:].isdigit():
                    lod_name, bare = prefix, unique_str[dot_idx + 1:]
            present_by_lod.setdefault(lod_name, set()).add(bare)

        # 自愈：清掉上次导出异常残留的占位对象，避免被当成真实部件
        # （只认 EFMI_STUB 标记，不依赖对象名前缀——根目录部件 stub 无 LOD 前缀）
        for obj in list(bpy.data.objects):
            if obj.get("EFMI_STUB"):
                bpy.data.objects.remove(obj, do_unlink=True)

        used_group_ids_by_lod = None  # 惰性计算：首个全缺 DrawIB 需要判定时才算
        present_positions = None

        def _get_used_group_ids(lod_name: str) -> set[int]:
            nonlocal used_group_ids_by_lod
            if used_group_ids_by_lod is None:
                used_group_ids_by_lod = self._collect_used_group_ids_by_lod(ordered)
            return used_group_ids_by_lod.get(lod_name, set())

        created = []
        for lod_name in sorted(lod_component_maps.keys()):
            component_map = lod_component_maps[lod_name]
            search_dir = os.path.join(workspace_root, lod_name) if lod_name else workspace_root
            present = present_by_lod.get(lod_name, set())
            lod_label = lod_name or "根目录"

            for draw_ib, comp_dict in component_map.items():
                members = sorted(str(v) for v in (comp_dict or {}).values())
                if not members:
                    continue

                if any(member in present for member in members):
                    # 部分缺失：缺失组件补占位
                    stub_members = [member for member in members if member not in present]
                else:
                    # 整个 DrawIB 缺席：判定几何是否被合并进其它对象
                    # 主判据 = 顶点坐标存在性（部件独有，无误判）；
                    # 位置数据缺失时回退 VGMap 引用判定（只查同 LOD 对象的组 id，
                    # 跨 LOD 组 id 各自从 0 起，混查会因命名空间碰撞误判）。
                    absorbed = False
                    positions = self._load_drawib_bind_positions(draw_ib, search_dir)
                    if positions is not None and len(positions) > 0:
                        if present_positions is None:
                            present_positions = self._collect_present_positions(ordered)
                        absorbed = self._is_drawib_absorbed_by_geometry(positions, present_positions)
                    else:
                        vg_values = self._load_drawib_vg_values(draw_ib, search_dir)
                        absorbed = bool(vg_values and vg_values & _get_used_group_ids(lod_name))

                    if absorbed:
                        stub_members = members
                        print(
                            f"[EFMI骨骼合并] DrawIB {draw_ib}（{lod_label}）没有对象，"
                            f"但其几何/骨骼被其它模型引用（已被合并），全组件补占位小三角面"
                        )
                    else:
                        print(
                            f"[EFMI骨骼合并] DrawIB {draw_ib}（{lod_label}）无对象且几何未被合并，"
                            f"按用户意图不生成"
                        )
                        continue

                for member in stub_members:
                    obj_name = self._create_stub_object(member, lod_name)
                    if obj_name:
                        ordered.append(DrawCallModel(obj_name=obj_name))
                        created.append(obj_name)
                        print(
                            f"[EFMI骨骼合并] 部件 {member}（{lod_label}）没有对应对象，"
                            f"已创建极限小三角面占位（游戏内不可见）"
                        )
        return created

    def _load_drawib_vg_values(self, draw_ib: str, search_dir: str) -> set[int]:
        """读取 DrawIB 全部组件写回的 VGMap 全局骨骼 id 集合（无数据返回空）。

        search_dir 为所属 LOD 的目录（LOD1 部件必须查 LOD1/，硬编码 LOD0 会漏）。
        """
        values = set()
        if not os.path.isdir(search_dir):
            return values
        for name in os.listdir(search_dir):
            if not name.startswith(draw_ib + "-"):
                continue
            submesh_dir = os.path.join(search_dir, name)
            if not os.path.isdir(submesh_dir):
                continue
            for type_dir in os.listdir(submesh_dir):
                if not type_dir.startswith("TYPE_"):
                    continue
                json_path = os.path.join(submesh_dir, type_dir, name + ".json")
                if not os.path.isfile(json_path):
                    continue
                payload = JsonUtils.LoadFromFile(json_path)
                vg_map = payload.get("VGMap") or {}
                for v in vg_map.values():
                    try:
                        values.add(int(v))
                    except (TypeError, ValueError):
                        continue
        return values

    def _load_drawib_bind_positions(self, draw_ib: str, search_dir: str):
        """读取 DrawIB 首个组件的绑定姿势顶点坐标（采样，用于几何存在性判定）。

        search_dir 为所属 LOD 的目录（LOD1 部件必须查 LOD1/，硬编码 LOD0 会漏）。
        """
        import numpy

        if not os.path.isdir(search_dir):
            return None
        for name in sorted(os.listdir(search_dir)):
            if not name.startswith(draw_ib + "-"):
                continue
            submesh_dir = os.path.join(search_dir, name)
            if not os.path.isdir(submesh_dir):
                continue
            for type_dir in os.listdir(submesh_dir):
                if not type_dir.startswith("TYPE_"):
                    continue
                type_path = os.path.join(submesh_dir, type_dir)
                json_path = os.path.join(type_path, name + ".json")
                pos_path = os.path.join(type_path, name + "-Position.buf")
                if not os.path.isfile(json_path) or not os.path.isfile(pos_path):
                    continue
                payload = JsonUtils.LoadFromFile(json_path)
                stride = 0
                for category_buffer in payload.get("CategoryBufferList", []):
                    for element in category_buffer.get("D3D11ElementList", []):
                        if str(element.get("Category", "") or "").strip().lower() == "position":
                            stride += int(element.get("ByteWidth", 0) or 0)
                if stride <= 0:
                    return None
                raw = numpy.fromfile(pos_path, dtype=numpy.uint8)
                if len(raw) == 0 or len(raw) % stride != 0:
                    return None
                verts = raw.reshape(-1, stride)[:, 0:12].copy().view(numpy.float32).reshape(-1, 3)
                if len(verts) > 256:
                    sample_idx = numpy.linspace(0, len(verts) - 1, 256).astype(numpy.int64)
                    verts = verts[sample_idx]
                return verts
        return None

    def _collect_present_positions(self, ordered):
        """收集蓝图内全部对象的顶点坐标（numpy Nx3）。"""
        import numpy

        chunks = []
        for draw_call in ordered:
            try:
                obj_name = draw_call.get_blender_obj_name()
            except Exception:
                continue
            obj = bpy.data.objects.get(obj_name) if obj_name else None
            mesh = getattr(obj, "data", None) if obj is not None else None
            vertices = getattr(mesh, "vertices", None)
            if not vertices:
                continue
            coords = numpy.empty(len(vertices) * 3, dtype=numpy.float32)
            vertices.foreach_get("co", coords)
            chunks.append(coords.reshape(-1, 3))
        if not chunks:
            return None
        return numpy.concatenate(chunks, axis=0)

    def _is_drawib_absorbed_by_geometry(self, positions, present_positions) -> bool:
        """几何存在性判定：该 DrawIB 绑定姿势顶点坐标（采样）有 >=30% 出现在现存
        对象的网格里（<=1e-4 近似）= 几何被合并进别的对象。
        """
        import numpy

        if present_positions is None or len(present_positions) == 0:
            return False
        sample = positions.astype(numpy.float64)
        present = present_positions.astype(numpy.float64)
        hits = 0
        chunk = 64
        for start in range(0, len(sample), chunk):
            part = sample[start:start + chunk]
            diff = numpy.abs(part[:, None, :] - present[None, :, :]).max(axis=2)
            hits += int((diff < 1e-4).any(axis=1).sum())
        ratio = hits / len(sample)
        return ratio >= 0.3

    def _collect_used_group_ids_by_lod(self, ordered) -> dict[str, set[int]]:
        """收集蓝图内全部对象实际引用（权重>0）的顶点组 id，按 LOD 分组。

        跨 LOD 组 id 各自从 0 起（命名空间独立），判定某 LOD 的缺席 DrawIB
        是否被吸收时必须只用**同 LOD** 对象的组 id，混查会因编号碰撞误判。
        """
        used: dict[str, set[int]] = {}
        for draw_call in ordered:
            try:
                obj_name = draw_call.get_blender_obj_name()
                unique_str = str(draw_call.get_workspace_unique_str() or "")
            except Exception:
                continue
            lod_name = ""
            if unique_str.upper().startswith("LOD") and "." in unique_str:
                dot_idx = unique_str.index(".")
                prefix = unique_str[:dot_idx]
                if prefix[3:].isdigit():
                    lod_name = prefix
            obj = bpy.data.objects.get(obj_name) if obj_name else None
            mesh = getattr(obj, "data", None) if obj is not None else None
            vertices = getattr(mesh, "vertices", None)
            if vertices is None:
                continue
            bucket = used.setdefault(lod_name, set())
            for vertex in vertices:
                for group_elem in vertex.groups:
                    if group_elem.weight > 0:
                        bucket.add(group_elem.group)
        return used

    def _create_stub_object(self, bare_unique_str: str, lod_name: str = "LOD0") -> str:
        """创建占位对象：3 顶点 1 三角面（1e-6 尺度），权重全给组 "0"。

        对象名 = <LOD>.<bare>（ObjectPrefixHelper 可解析出带 LOD 前缀的
        workspace unique_str，保证 stub 部件从自己 LOD 的 json 读取骨骼元数据）。
        """
        workspace_unique_str = (
            f"{lod_name}.{bare_unique_str}" if lod_name else bare_unique_str
        )

        mesh = bpy.data.meshes.new(name="EFMI_STUB_MESH_" + workspace_unique_str)
        mesh.from_pydata(
            [(0.0, 0.0, 0.0), (1e-6, 0.0, 0.0), (0.0, 1e-6, 0.0)],
            [],
            [(0, 1, 2)],
        )
        mesh.update()

        obj = bpy.data.objects.new(name=workspace_unique_str, object_data=mesh)
        obj["EFMI_STUB"] = 1
        obj["3DMigoto:WorkspaceUniqueStr"] = workspace_unique_str
        vertex_group = obj.vertex_groups.new(name="0")
        vertex_group.add([0, 1, 2], 1.0, 'REPLACE')

        try:
            bpy.context.collection.objects.link(obj)
        except Exception:
            bpy.context.scene.collection.objects.link(obj)
        return obj.name

    def _cleanup_stub_objects(self):
        """导出结束后移除占位对象（含 mesh 数据）。"""
        for obj_name in self._efmi_stub_object_names:
            obj = bpy.data.objects.get(obj_name)
            if obj is None:
                continue
            mesh = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if mesh is not None and mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        if self._efmi_stub_object_names:
            print(f"[EFMI骨骼合并] 已清理 {len(self._efmi_stub_object_names)} 个占位小三角面对象")
        self._efmi_stub_object_names = []

    def generate_buffer_files(self):
        buf_output_folder = GlobalConfig.path_generatemod_buffer_folder()

        # 清理上次导出的残留 .buf：本次 INI 只引用本次导出的部件，旧缓冲
        # （如上次导出了 LOD1 全部、这次只导出部分）会残留在 Meshes/ 里误导
        # 排查且白占空间。INI 由同一次导出重新生成，全量重写自洽。
        try:
            if os.path.isdir(buf_output_folder):
                removed_count = 0
                for name in os.listdir(buf_output_folder):
                    if not name.endswith(".buf"):
                        continue
                    try:
                        os.remove(os.path.join(buf_output_folder, name))
                        removed_count += 1
                    except OSError:
                        continue
                if removed_count:
                    print(f"[EFMI] 已清理 {removed_count} 个上次导出的残留 .buf")
        except Exception as e:
            print(f"[EFMI] 清理残留缓冲失败（继续导出）: {e}")

        for submesh_model in self.submesh_model_list:
            print("ExportEFMI: 导出SubMeshModel，Unique标识: " + submesh_model.unique_str)

            ib_name = getattr(submesh_model, "workspace_unique_str", "") or submesh_model.unique_str
            ib_filename = ib_name + "-Index.buf"
            ib_filepath = os.path.join(buf_output_folder, ib_filename)
            BufferExportHelper.write_buf_ib_r32_uint(submesh_model.ib, ib_filepath)

            for category, category_buf in submesh_model.category_buffer_dict.items():
                category_buf_filename = submesh_model.unique_str + "-" + category + ".buf"
                category_buf_filepath = os.path.join(buf_output_folder, category_buf_filename)
                with open(category_buf_filepath, 'wb') as f:
                    category_buf.tofile(f)

    def _get_submesh_ib_key(self, submesh_model):
        if self.cross_ib_match_mode == 'INDEX_COUNT':
            return f"indexcount_{submesh_model.match_index_count}"
        else:
            return f"{submesh_model.match_draw_ib}_{submesh_model.match_first_index}"

    def _append_drawindexed_instanced_with_shader_replace(self, section, drawcall_list, draw_offset_dict):
        """将 drawcall 列表写入 section，对着色器替换物体使用 run 逻辑替代 instanced 绘制。"""
        if not self.has_shader_replace:
            for drawindexed_str in M_IniHelper.get_drawindexed_instanced_str_list(
                drawcall_list, obj_name_draw_offset_dict=draw_offset_dict,
            ):
                section.append(drawindexed_str)
            return

        resolved_drawcalls = [
            (
                drawcall,
                M_IniHelper.get_draw_call_shader_replace_info_list(
                    drawcall,
                    shader_replace_object_names=self.shader_replace_object_names,
                    shader_replace_object_info_map=self.shader_replace_object_info_map,
                    shader_replace_info_list=self.shader_replace_info_list,
                ),
            )
            for drawcall in drawcall_list
        ]
        for dc, obj_infos in resolved_drawcalls:
            if not obj_infos:
                for drawindexed_str in M_IniHelper.get_drawindexed_instanced_str_list(
                    [dc],
                    obj_name_draw_offset_dict=draw_offset_dict,
                ):
                    section.append(drawindexed_str)
                continue

            draw_offset = dc.index_offset
            if draw_offset_dict:
                draw_offset = draw_offset_dict.get(dc.obj_name, dc.index_offset)

            display_name = str(getattr(dc, 'obj_name', '') or '')
            section.append(f"; [mesh:{display_name}] [vertex_count:{dc.vertex_count}]")

            for info in obj_infos:
                condition_str = dc.get_condition_str()
                indent = "  " if condition_str else ""
                if condition_str:
                    section.append(f"if {condition_str}")
                run_lines = M_IniHelper.get_shader_replace_run_logic(
                    info,
                    dc.match_draw_ib or "0",
                    dc.match_first_index if dc.match_first_index else "0",
                    info.get('component_index', 0),
                    dc.index_count,
                    draw_offset,
                )
                for line in run_lines:
                    section.append(f"{indent}{line}")
                if condition_str:
                    section.append("endif")
            section.append("")

    def _get_all_cross_ib_identifiers(self):
        all_identifiers = set()

        if self.cross_ib_match_mode == 'INDEX_COUNT':
            for source_key, target_key_list in self.cross_ib_info_dict.items():
                if source_key.startswith('indexcount_'):
                    index_count = source_key.replace('indexcount_', '')
                    all_identifiers.add(index_count)
                for target_key in target_key_list:
                    if target_key.startswith('indexcount_'):
                        index_count = target_key.replace('indexcount_', '')
                        all_identifiers.add(index_count)

            for submesh_model in self.submesh_model_list:
                if submesh_model.match_index_count:
                    all_identifiers.add(submesh_model.match_index_count)
        else:
            for source_ib, target_ib_list in self.cross_ib_info_dict.items():
                source_hash = source_ib.split("_")[0]
                all_identifiers.add(source_hash)
                for target_ib in target_ib_list:
                    target_hash = target_ib.split("_")[0]
                    all_identifiers.add(target_hash)

            for drawib_model in self.drawib_model_list:
                all_identifiers.add(drawib_model.draw_ib)

        return all_identifiers

    def _get_vb_condition_for_mapping(self, source_ib_key, target_ib_key, condition_type='source'):
        mapping_key = (source_ib_key, target_ib_key)
        condition_info = self.cross_ib_vb_condition_mapping.get(mapping_key, {})
        if condition_type == 'source':
            return condition_info.get('source', "if vs == 200 || vs == 201 || vs == 204")
        else:
            return condition_info.get('target', "if vs == 202 || vs == 203")

    def _get_vb_condition_for_object(self, obj_name, source_ib_key, target_ib_key, condition_type='source'):
        object_mapping_key = (obj_name, source_ib_key, target_ib_key)
        condition_info = self.cross_ib_object_vb_condition.get(object_mapping_key, {})
        if condition_type == 'source':
            return condition_info.get('source', "if vs == 200 || vs == 201 || vs == 204")
        else:
            return condition_info.get('target', "if vs == 202 || vs == 203")

    def _split_drawcalls_by_cross_ib(self, drawcall_model_list, source_ib_key=None, target_ib_key=None):
        cross_ib_drawcalls = []
        non_cross_ib_drawcalls = []

        cross_ib_mapping_objects = self.cross_ib_mapping_objects

        for drawcall_model in drawcall_model_list:
            obj_name = drawcall_model.obj_name if hasattr(drawcall_model, 'obj_name') else str(drawcall_model)

            is_cross_ib = False
            if source_ib_key:
                if target_ib_key:
                    mapping_key = (source_ib_key, target_ib_key)
                    if mapping_key in cross_ib_mapping_objects:
                        if obj_name in cross_ib_mapping_objects[mapping_key]:
                            is_cross_ib = True
                else:
                    for (src_key, tgt_key), obj_names in cross_ib_mapping_objects.items():
                        if src_key == source_ib_key and obj_name in obj_names:
                            is_cross_ib = True
                            break
            else:
                if obj_name in self.cross_ib_object_names:
                    is_cross_ib = True

            if is_cross_ib:
                cross_ib_drawcalls.append(drawcall_model)
            else:
                non_cross_ib_drawcalls.append(drawcall_model)

        return cross_ib_drawcalls, non_cross_ib_drawcalls

    def _group_drawcalls_by_cross_ib_target(self, drawcall_model_list, source_ib_key, target_ib_keys):
        grouped = {}
        cross_ib_mapping_objects = self.cross_ib_mapping_objects

        for drawcall_model in drawcall_model_list:
            obj_name = drawcall_model.obj_name if hasattr(drawcall_model, 'obj_name') else str(drawcall_model)

            for target_ib_key in target_ib_keys:
                mapping_key = (source_ib_key, target_ib_key)
                if mapping_key in cross_ib_mapping_objects:
                    if obj_name in cross_ib_mapping_objects[mapping_key]:
                        vb_condition = self._get_vb_condition_for_object(obj_name, source_ib_key, target_ib_key, 'source')
                        group_key = (target_ib_key, vb_condition)
                        if group_key not in grouped:
                            grouped[group_key] = []
                        grouped[group_key].append(drawcall_model)
                        break

        return grouped

    @staticmethod
    def _get_source_cross_ib_variants(vb_condition):
        """Split the EFMI capture VS from replay VS stages."""
        condition = str(vb_condition or "").strip()
        if not condition:
            return []

        condition_match = re.fullmatch(r"if\s+(.+)", condition, re.IGNORECASE)
        if not condition_match:
            return [(condition, "CustomShader_ExtractCB1", 2)]

        filters = []
        for term in condition_match.group(1).split("||"):
            term_match = re.fullmatch(
                r"\s*\(?\s*vs\s*==\s*(\d+)\s*\)?\s*",
                term,
                re.IGNORECASE,
            )
            if not term_match:
                return [(condition, "CustomShader_ExtractCB1", 2)]
            filter_index = int(term_match.group(1))
            if filter_index not in filters:
                filters.append(filter_index)

        if 200 not in filters:
            return [(condition, "CustomShader_ExtractCB1", 2)]

        variants = [("if vs == 200", "CustomShader_ExtractCaptureCB1", 1)]
        replay_filters = [filter_index for filter_index in filters if filter_index != 200]
        if replay_filters:
            replay_condition = "if " + " || ".join(
                f"vs == {filter_index}" for filter_index in replay_filters
            )
            variants.append((replay_condition, "CustomShader_ExtractCB1", 2))
        return variants


    def _append_source_cross_ib_replay(self, section, vb_condition, objects, source_identifier):
        for condition, extract_shader, cb_slot in self._get_source_cross_ib_variants(vb_condition):
            indent = "    " if condition else ""
            if condition:
                section.append(condition)
            section.append(f"{indent}run = {extract_shader}")
            section.append(f"{indent}cs-t2 = ResourceID_{source_identifier}")
            section.append(f"{indent}run = CustomShader_RecordBones_{source_identifier}")
            section.append(f"{indent}run = CustomShader_RedirectCB1_{source_identifier}")
            section.append(f"{indent}vs-t0 = ResourceFakeT0_SRV_{source_identifier}")
            section.append(f"{indent}vs-cb{cb_slot} = ResourceFakeCB1_{source_identifier}")
            section.append(";所有需要跨 Ib 的物体引用")
            self._append_drawindexed_instanced_with_shader_replace(section, objects, None)
            if condition:
                section.append("endif")

    def _generate_cross_ib_block_for_source(self, source_identifier, drawcall_model_list, source_ib_key=None, target_ib_key=None):
        lines = []

        cross_ib_drawcalls, non_cross_ib_drawcalls = self._split_drawcalls_by_cross_ib(
            drawcall_model_list,
            source_ib_key=source_ib_key
        )

        target_ib_keys = list(self.cross_ib_source_to_target_dict.get(source_ib_key, []) or [])
        if target_ib_key and target_ib_key not in target_ib_keys:
            target_ib_keys.append(target_ib_key)

        grouped_drawcalls = self._group_drawcalls_by_cross_ib_target(cross_ib_drawcalls, source_ib_key, target_ib_keys)

        class _ListSectionAdapter:
            def __init__(self, target_lines):
                self._target_lines = target_lines

            def append(self, line):
                self._target_lines.append(line)

        section_adapter = _ListSectionAdapter(lines)

        for (tgt_ib_key, vb_condition), objects in grouped_drawcalls.items():
            if not objects:
                continue

            lines.append(";跨 iB 区域")
            self._append_source_cross_ib_replay(
                section_adapter,
                vb_condition,
                objects,
                source_identifier,
            )

        lines.append(";不需要跨 Ib 的物体引用")

        if non_cross_ib_drawcalls:
            self._append_drawindexed_instanced_with_shader_replace(
                section_adapter,
                non_cross_ib_drawcalls,
                None,
            )

        lines.append("")
        lines.append("post vs-cb1 = null")
        lines.append("post vs-cb2 = null")
        lines.append("post vs-t0 = null")
        lines.append("post cs-t2 = null")

        return lines

    def _append_cross_ib_fake_resources(self, present_section, all_identifiers):
        identifier_count = len(all_identifiers)
        max_base_offset = max(0, identifier_count - 1) * 1000
        fake_t0_array_size = max(200000, max_base_offset + 100000 + 768)

        present_section.append("[ResourceDumpedCB1_UAV]")
        present_section.append("type = RWStructuredBuffer")
        present_section.append("stride = 16")
        present_section.append("array = 4096")
        present_section.new_line()

        present_section.append("[ResourceDumpedCB1_SRV]")
        present_section.append("type = Buffer")
        present_section.append("stride = 16")
        present_section.append("array = 4096")
        present_section.new_line()

        for identifier in sorted(all_identifiers):
            present_section.append(f"[ResourceFakeCB1_UAV_{identifier}]")
            present_section.append("type = RWStructuredBuffer")
            present_section.append("stride = 16")
            present_section.append("array = 4096")
            present_section.new_line()

            present_section.append(f"[ResourceFakeCB1_{identifier}]")
            present_section.append("type = Buffer")
            present_section.append("stride = 16")
            present_section.append("format = R32G32B32A32_UINT")
            present_section.append("array = 4096")
            present_section.new_line()

            present_section.append(f"[ResourceFakeT0_UAV_{identifier}]")
            present_section.append("type = RWStructuredBuffer")
            present_section.append("stride = 16")
            present_section.append(f"array = {fake_t0_array_size}")
            present_section.new_line()

            present_section.append(f"[ResourceFakeT0_SRV_{identifier}]")
            present_section.append("type = StructuredBuffer")
            present_section.append("stride = 16")
            present_section.append(f"array = {fake_t0_array_size}")
            present_section.new_line()

        present_section.append("[ResourceFakeT0_UAV]")
        present_section.append("type = RWStructuredBuffer")
        present_section.append("stride = 16")
        present_section.append(f"array = {fake_t0_array_size}")
        present_section.new_line()

        present_section.append("[ResourceFakeT0_SRV]")
        present_section.append("type = StructuredBuffer")
        present_section.append("stride = 16")
        present_section.append(f"array = {fake_t0_array_size}")
        present_section.new_line()

        present_section.append("[ResourcePrev_SRV]")
        present_section.append("type = StructuredBuffer")
        present_section.append("stride = 16")
        present_section.append(f"array = {fake_t0_array_size}")
        present_section.new_line()

    def _add_cross_ib_present_section(self, ini_builder):
        if not self.has_cross_ib:
            return

        present_section = M_IniSection(M_SectionType.CrossIBPresent)
        present_section.append(";特殊追加固定区域")

        all_identifiers = self._get_all_cross_ib_identifiers()
        self._append_cross_ib_fake_resources(present_section, all_identifiers)

        present_section.append("[CustomShader_ExtractCB1]")
        present_section.append("vs = ./res/extract_cb1_vs.hlsl")
        present_section.append("ps = ./res/extract_cb1_ps.hlsl")
        present_section.append("ps-u7 = ResourceDumpedCB1_UAV")
        present_section.append("depth_enable = false")
        present_section.append("blend = ADD SRC_ALPHA INV_SRC_ALPHA")
        present_section.append("cull = none")
        present_section.append("topology = point_list")
        present_section.append("draw = 4096, 0")
        present_section.append("ps-u7 = null")
        present_section.append("ResourceDumpedCB1_SRV = copy ResourceDumpedCB1_UAV")
        present_section.new_line()

        present_section.append("[CustomShader_ExtractCaptureCB1]")
        present_section.append("vs = ./res/extract_capture_cb1_vs.hlsl")
        present_section.append("ps = ./res/extract_cb1_ps.hlsl")
        present_section.append("ps-u7 = ResourceDumpedCB1_UAV")
        present_section.append("depth_enable = false")
        present_section.append("blend = ADD SRC_ALPHA INV_SRC_ALPHA")
        present_section.append("cull = none")
        present_section.append("topology = point_list")
        present_section.append("draw = 4096, 0")
        present_section.append("ps-u7 = null")
        present_section.append("ResourceDumpedCB1_SRV = copy ResourceDumpedCB1_UAV")
        present_section.new_line()

        for identifier in sorted(all_identifiers):
            present_section.append(f"[CustomShader_RecordBones_{identifier}]")
            present_section.append("cs = ./res/record_bones_cs.hlsl")
            present_section.append("cs-t0 = vs-t0")
            present_section.append("cs-t1 = ResourceDumpedCB1_SRV")
            present_section.append(f"cs-u1 = ResourceFakeT0_UAV_{identifier}")
            present_section.append("dispatch = 12, 1, 1")
            present_section.append("cs-u1 = null")
            present_section.append("cs-t0 = null")
            present_section.append("cs-t1 = null")
            present_section.append(f"ResourceFakeT0_SRV_{identifier} = copy ResourceFakeT0_UAV_{identifier}")
            present_section.new_line()

            present_section.append(f"[CustomShader_RedirectCB1_{identifier}]")
            present_section.append("cs = ./res/redirect_cb1_cs.hlsl")
            present_section.append("cs-t0 = ResourceDumpedCB1_SRV")
            present_section.append(f"ResourceFakeCB1_UAV_{identifier} = copy ResourceDumpedCB1_SRV")
            present_section.append(f"cs-u0 = ResourceFakeCB1_UAV_{identifier}")
            present_section.append("dispatch = 4, 1, 1")
            present_section.append("cs-u0 = null")
            present_section.append("cs-t0 = null")
            present_section.append(f"ResourceFakeCB1_{identifier} = copy ResourceFakeCB1_UAV_{identifier}")
            present_section.new_line()

        shader_overrides = [
            ("ShaderOverridevs1000", "f11c7e1dbf876a69", "200"),
            ("ShaderOverridevs1001", "303f45d5266d0369", "201"),
            ("ShaderOverridevs1002", "7b3a141f99cd9b39", "201"),
            ("ShaderOverridevs1003", "1479b2b594b9c91a", "202"),
            ("ShaderOverridevs1004", "c6e55aaa8f4b3218", "202"),
            ("ShaderOverridevs1005", "784f11ae11c97112", "203"),
            ("ShaderOverridevs1006", "f1b10202c73c72c3", "204"),
            ("ShaderOverridevs1007", "12ad3cc5f56f853c", "204"),
            ("ShaderOverridevs1008", "86cb3bc0a3e2e013", "204"),
            ("ShaderOverridevs1009", "906a3976f3e33cfb", "204"),
            ("ShaderOverridevs1010", "0ba16985f9f74f8d", "204"),
            ("ShaderOverridevs1011", "06c94dd56f447210", "204"),
            ("ShaderOverridevs1012", "f47b1f797f5831d0", "204"),
        ]

        for name, hash_val, filter_idx in shader_overrides:
            present_section.append(f"[{name}]")
            present_section.append(f"hash = {hash_val}")
            present_section.append(f"filter_index = {filter_idx}")
            present_section.append("allow_duplicate_hash = overrule")
            present_section.new_line()

        ini_builder.append_section(present_section)


    def _add_cross_ib_resource_id_sections(self, ini_builder):
        if not self.has_cross_ib:
            return

        resource_id_section = M_IniSection(M_SectionType.ResourceID)
        resource_id_section.append(";特殊追加身份证区域")

        all_identifiers = set()

        if self.cross_ib_match_mode == 'INDEX_COUNT':
            for source_key, target_key_list in self.cross_ib_info_dict.items():
                if source_key.startswith('indexcount_'):
                    index_count = source_key.replace('indexcount_', '')
                    all_identifiers.add(index_count)
                for target_key in target_key_list:
                    if target_key.startswith('indexcount_'):
                        index_count = target_key.replace('indexcount_', '')
                        all_identifiers.add(index_count)

            for submesh_model in self.submesh_model_list:
                if submesh_model.match_index_count:
                    all_identifiers.add(submesh_model.match_index_count)
        else:
            for source_ib, target_ib_list in self.cross_ib_info_dict.items():
                source_hash = source_ib.split("_")[0]
                all_identifiers.add(source_hash)
                for target_ib in target_ib_list:
                    target_hash = target_ib.split("_")[0]
                    all_identifiers.add(target_hash)

            for drawib_model in self.drawib_model_list:
                all_identifiers.add(drawib_model.draw_ib)

        sorted_identifiers = sorted(list(all_identifiers))

        for idx, identifier in enumerate(sorted_identifiers):
            resource_id_section.append(f"[ResourceID_{identifier}]")
            resource_id_section.append("type = Buffer")
            resource_id_section.append("format = R32_FLOAT")
            resource_id_section.append(f"data = {idx * 1000}.0")
            resource_id_section.new_line()

        ini_builder.append_section(resource_id_section)

    def _find_source_submesh_by_ib_key(self, source_ib_key):
        for submesh_model in self.submesh_model_list:
            submesh_ib_key = self._get_submesh_ib_key(submesh_model)
            if submesh_ib_key == source_ib_key:
                return submesh_model
        return None

    def _find_source_drawib_by_ib_key(self, source_ib_key):
        if self.cross_ib_match_mode == 'INDEX_COUNT':
            index_count = source_ib_key.replace('indexcount_', '') if source_ib_key.startswith('indexcount_') else None
            if index_count:
                for drawib_model in self.drawib_model_list:
                    for submesh in drawib_model.submesh_model_list:
                        if submesh.match_index_count == index_count:
                            return drawib_model
            return None
        else:
            source_hash = source_ib_key.split("_")[0]
            for drawib_model in self.drawib_model_list:
                if drawib_model.draw_ib == source_hash:
                    return drawib_model
            return None

    @staticmethod
    def _lod_name_from_unique_str(unique_str: str) -> str:
        """解析 unique_str 的 LOD 前缀（'LOD0.xxx' -> 'LOD0'；无前缀 -> ''）。

        与 WorkSpaceHelper.parse_lod_unique_str 语义一致；用于把合并骨架组件
        按 LOD 分组（每 LOD 一套独立 MergedSkeleton 配置，见
        _add_merged_skeleton_section）。
        """
        normalized = str(unique_str or "").strip()
        if normalized.upper().startswith("LOD") and "." in normalized:
            dot_idx = normalized.index(".")
            potential = normalized[:dot_idx]
            if potential[3:].isdigit():
                return potential
        return ""

    @staticmethod
    def _validated_blendindices_layouts(submesh_models, context: str):
        """返回实际 BLENDINDICES 布局；同一运行时命令列表内必须完全一致。"""
        submesh_models = list(submesh_models)
        if not submesh_models:
            raise RuntimeError(f"{context}: 未找到对应子网格，无法确定 BLENDINDICES 布局")
        expected = None
        for submesh_model in submesh_models:
            game_type = getattr(submesh_model, "d3d11_game_type", None)
            if game_type is None:
                raise RuntimeError(f"{context}: 子网格缺少 GameType")
            layouts = tuple(game_type.get_blendindices_layouts())
            if not layouts:
                raise RuntimeError(
                    f"{context}: {getattr(submesh_model, 'unique_str', '?')} "
                    "不含 BLENDINDICES 布局"
                )
            if expected is None:
                expected = layouts
            elif layouts != expected:
                raise RuntimeError(
                    f"{context}: 同一 LOD 的 BLENDINDICES 布局不一致: "
                    f"{expected} != {layouts}"
                )
        return expected or ()

    def _get_merged_skeleton_component_info(self):
        """收集 EFMI 骨骼合并（Merged Skeleton）组件信息。

        多 LOD 语义：构建端在各自 dump 上先建立原始候选对应，LOD0 执行一次
        去重，LOD1 按对应关系同步分区；运行时槽位仍各自从 0 起，导出端按 LOD
        分组生成多套独立合并骨架配置。
        此处组件携带 lod 字段、component_id 按 LOD 组内分配（每 LOD 一套骨架，
        id 只在组内有意义）。
        仅收集 vg_count > 0（反查已写回）的子网格。
        """
        components = []
        if GlobalProterties.import_merged_vgmap():
            for submesh_model in self.submesh_model_list:
                vg_count = int(getattr(submesh_model, "vg_count", 0) or 0)
                if vg_count > 0:
                    components.append({
                        "unique_str": submesh_model.unique_str,
                        "lod": self._lod_name_from_unique_str(submesh_model.unique_str),
                        "vg_offset": int(getattr(submesh_model, "vg_offset", 0) or 0),
                        "vg_count": vg_count,
                    })
        # 按 (LOD, vg_offset) 排序；component_id 按 LOD 组内分配
        components.sort(key=lambda c: (c["lod"], c["vg_offset"]))
        component_id_dict: dict[str, int] = {}
        group_counter: dict[str, int] = {}
        for comp in components:
            lod = comp["lod"]
            component_id_dict[comp["unique_str"]] = group_counter.get(lod, 0)
            group_counter[lod] = group_counter.get(lod, 0) + 1
        return components, component_id_dict

    def _add_merged_skeleton_section(self, ini_builder, command_lists_section=None):
        """生成 EFMI 骨骼合并（Merged Skeleton）INI 段（对齐 EFMI 1.4.1 运行时契约）。

        内容（每 LOD 一套，名字加 _<LOD> 后缀）：Constants（$component_count/
        $bones_count/$max_instance_count/$merged_skeleton_initialized——初始化变量
        也必须按 LOD 独立，否则后一套骨架的 Initialize 会被前一套的
        initialized=1 跳过、组件 offset/count 池永不写入）
        + 5 个 Pool + Pool_ObjectSpatialIdentity（空间实例识别输入池，同样按
        LOD 独立，避免两套骨架的实例 id 互踩）
        + ResourceMergedSkeletonDataRW + CommandList_MergedSkeleton_ConnectComponent
        （守卫初始化 + 绑定 pools + AttachComponent + ElementFormat 16 位）+
        CommandListInitializeMergedSkeleton（逐组件写 vg_offset/vg_count，LodRemaps 全 null）。

        多 LOD 语义：LOD0/LOD1 的原始对应先确定 LOD0 基准分区，再同步到 LOD1；
        导出时仍按 LOD 各自生成独立的 Resource/Pool/CommandList/粘合层，
        互不引用、互不混用。构建端 vg 槽位依旧各自从 0 起；组件 id 组内分配，
        EntryPoint 只挂本组件所属 LOD 的粘合层。
        无 LOD 前缀的组件（单 LOD/根目录工作空间）后缀为空，与旧版输出完全兼容。

        另向 command_lists_section 追加官方绘制管线粘合层（每 LOD 一套）
        [CommandList_Component_DrawInstances_<LOD>]：命名空间配置赋值（component_count/
        bones_count/instance_count——运行时只读 EFMIv1 命名空间内的值，漏赋 bones_count
        会让合并骨骼按 0 根计算）+ Component_ReadConfig + 空间实例识别 +
        ConnectComponent 回调挂载 + run Component_DrawInstances（运行时接管逐实例
        迭代与 MergedSkeleton_Apply）。按用户要求不做 DRAW_TYPE 通道门控，全通道生效。
        """
        components = self.merged_skeleton_components
        if not components:
            return

        # 按 LOD 分组（组内保持 vg_offset 序）
        lod_groups: dict[str, list[dict]] = {}
        for comp in components:
            lod_groups.setdefault(comp["lod"], []).append(comp)

        section = M_IniSection(M_SectionType.MergedSkeleton)

        for lod, lod_components in sorted(lod_groups.items()):
            suffix = "_" + lod if lod else ""
            component_count = len(lod_components)
            # 骨骼总数口径修正：取本 LOD 内 max(vg_offset + vg_count)。
            # 导出子集时 vg_offset 是本 LOD 全局槽位（可能远超导出内 sum(vg_count)），
            # 合并骨架缓冲与逐实例区域数学必须覆盖组件声明的最大槽位，否则越界空转。
            bones_count = max(comp["vg_offset"] + comp["vg_count"] for comp in lod_components)
            max_instance_count = 8  # 与参考插件 cfg.max_instance_count 一致

            section.append("[Constants]")
            section.append(f"global $component_count{suffix} = {component_count}")
            section.append(f"global $bones_count{suffix} = {bones_count}")
            section.append(f"global $max_instance_count{suffix} = {max_instance_count}")
            section.append(f"global $merged_skeleton_initialized{suffix} = 0")
            section.new_line()

            section.append(f"[Pool_MergedSkeleton_Component_VertexGroupOffsets{suffix}]")
            section.append(f"pool_size = $component_count{suffix}")
            section.new_line()

            section.append(f"[Pool_MergedSkeleton_Component_VertexGroupCounts{suffix}]")
            section.append(f"pool_size = $component_count{suffix}")
            section.new_line()

            section.append(f"[Pool_MergedSkeleton_Component_LodRemaps{suffix}]")
            section.append(f"pool_size = $component_count{suffix} * $\\EFMIv1\\cfg_ms_max_lod_level_count")
            section.new_line()

            section.append(f"[Pool_MergedSkeleton_Instance_UpdateFrame{suffix}]")
            section.append(f"pool_size = $component_count{suffix} * $max_instance_count{suffix}")
            section.new_line()

            section.append(f"[Pool_MergedSkeleton_Instance_LodLevel{suffix}]")
            section.append(f"pool_size = $component_count{suffix} * $max_instance_count{suffix}")
            section.new_line()

            # 空间实例识别输入池（官方管线必需：MergedSkeleton_Apply 经
            # PoolSpatialIdentity_SpatialIds[$draw_call_instance_id] 取实例 id，
            # 该池只能由 SpatialIdentity_IdentifyComponentInstances 以此池为输入填充；
            # 按 LOD 独立，避免两套骨架的实例 id 互踩）
            section.append(f"[Pool_ObjectSpatialIdentity{suffix}]")
            section.append(f"pool_size = $max_instance_count{suffix} * $\\EFMIv1\\cfg_spatial_instance_load_ratio")
            section.append("pool_index_type = spatial")
            section.append("pool_spatial_radius = $\\EFMIv1\\cfg_spatial_base_radius")
            section.append("pool_expiration_timeout_frames = $\\EFMIv1\\cfg_spatial_expiration_frames")
            section.append("pool_expiration_reset_elements = $\\EFMIv1\\cfg_spatial_expiration_reset")
            section.append("pool_expiration_refresh_on_read = $\\EFMIv1\\cfg_spatial_expiration_read_refresh")
            section.append("pool_variable_default_value = $\\EFMIv1\\cfg_spatial_detault_value")
            section.new_line()

            section.append(f"[ResourceMergedSkeletonDataRW{suffix}]")
            section.append("type = RWBuffer")
            section.append("format = R32G32B32A32_FLOAT")
            section.append(
                f"array = ($\\EFMIv1\\cfg_ms_implicit_bones_count + $\\EFMIv1\\cfg_ms_skeletons_count "
                f"* $bones_count{suffix} * $max_instance_count{suffix}) * $\\EFMIv1\\cfg_ms_bone_entry_size"
            )
            section.new_line()

            section.append(f"[CommandList_MergedSkeleton_ConnectComponent{suffix}]")
            section.append(f"if !$merged_skeleton_initialized{suffix}")
            section.append(f"    $merged_skeleton_initialized{suffix} = 1")
            section.append(f"    run = CommandListInitializeMergedSkeleton{suffix}")
            section.append("endif")
            section.append(f"Pool\\EFMIv1\\Input_MergedSkeleton_Component_VertexGroupOffsets = ref Pool_MergedSkeleton_Component_VertexGroupOffsets{suffix}")
            section.append(f"Pool\\EFMIv1\\Input_MergedSkeleton_Component_VertexGroupCounts = ref Pool_MergedSkeleton_Component_VertexGroupCounts{suffix}")
            section.append(f"Pool\\EFMIv1\\Input_MergedSkeleton_Component_LodRemaps = ref Pool_MergedSkeleton_Component_LodRemaps{suffix}")
            section.append(f"Pool\\EFMIv1\\Input_MergedSkeleton_Instance_UpdateFrame = ref Pool_MergedSkeleton_Instance_UpdateFrame{suffix}")
            section.append(f"Pool\\EFMIv1\\Input_MergedSkeleton_Instance_LodLevel = ref Pool_MergedSkeleton_Instance_LodLevel{suffix}")
            section.append(f"Resource\\EFMIv1\\Output_MergedSkeleton = ref ResourceMergedSkeletonDataRW{suffix}")
            section.append("run = CommandList\\EFMIv1\\MergedSkeleton_AttachComponent")
            section.append("; BLENDINDICES layouts after merged-skeleton widening")
            lod_unique_strs = {comp["unique_str"] for comp in lod_components}
            lod_submesh_models = [
                model for model in self.submesh_model_list
                if model.unique_str in lod_unique_strs
            ]
            blend_layouts = self._validated_blendindices_layouts(
                lod_submesh_models,
                f"[EFMI骨骼合并] {lod or '单 LOD'}",
            )
            for semantic_index, element_format, extract_slot in blend_layouts:
                section.append(
                    f"{extract_slot}->ElementFormat(BLENDINDICES, {semantic_index}) = "
                    f"{element_format}"
                )
            section.new_line()

            section.append(f"[CommandListInitializeMergedSkeleton{suffix}]")
            section.append(f"Resource\\EFMIv1\\OutputMergedSkeleton_Template = ref ResourceMergedSkeletonDataRW{suffix}")
            section.append("run = CommandList\\EFMIv1\\InitializeMergedSkeleton")
            section.append("local $lod_level_count = $\\EFMIv1\\cfg_ms_max_lod_level_count")
            section.append("local $component_id")
            for component_id, comp in enumerate(lod_components):
                section.append(f"$component_id = {component_id}")
                section.append(f"$Pool_MergedSkeleton_Component_VertexGroupOffsets{suffix}[$component_id] = {comp['vg_offset']}")
                section.append(f"$Pool_MergedSkeleton_Component_VertexGroupCounts{suffix}[$component_id] = {comp['vg_count']}")
                section.append(f"Pool_MergedSkeleton_Component_LodRemaps{suffix}[$component_id*$lod_level_count+0] = null")
            section.new_line()

            # 官方绘制管线粘合层（每 LOD 一套）：运行时 Component_DrawInstances
            # 逐实例迭代、每实例 MergedSkeleton_Apply 后才回调组件绘制
            # （CommandList_Draw_<部件前缀>）。identification_min_components 默认 4
            # 是按整角色设定的；导出子集（如只有 2 个组件）时必须下调，否则空间
            # 识别永远集不齐组件位数，实例被判 Unknown 而始终绘制原始网格
            # （表现为"模组完全不生效"）。此处按本 LOD 组件数取 min(component_count, 4)。
            if command_lists_section is not None:
                command_lists_section.append(f"[CommandList_Component_DrawInstances{suffix}]")
                command_lists_section.append("handling = skip")
                command_lists_section.append(f"$\\EFMIv1\\component_count = $component_count{suffix}")
                command_lists_section.append(f"$\\EFMIv1\\bones_count = $bones_count{suffix}")
                command_lists_section.append(f"$\\EFMIv1\\instance_count = $max_instance_count{suffix}")
                command_lists_section.append("run = CommandList\\EFMIv1\\Object_ReadConfig")
                command_lists_section.append("$\\EFMIv1\\custom_mesh_scale = 1.00")
                command_lists_section.append(
                    "$\\EFMIv1\\identification_min_components = " + str(min(component_count, 4))
                )
                command_lists_section.append("run = CommandList\\EFMIv1\\Component_ReadConfig")
                command_lists_section.append(
                    f"Pool\\EFMIv1\\Input_ObjectSpatialIdentity = ref Pool_ObjectSpatialIdentity{suffix}"
                )
                command_lists_section.append(
                    "run = CommandList\\EFMIv1\\SpatialIdentity_IdentifyComponentInstances"
                )
                command_lists_section.append(
                    f"CommandList\\EFMIv1\\Callback_MergedSkeleton_ConnectComponent = "
                    f"ref CommandList_MergedSkeleton_ConnectComponent{suffix}"
                )
                command_lists_section.append("run = CommandList\\EFMIv1\\Component_DrawInstances")
                command_lists_section.new_line()

        ini_builder.append_section(section)


    def _append_submesh_draw_bindings(self, section, submesh_model, drawib_model):
        """子网格绘制绑定：OverrideTextures + ib/vb 缓冲绑定 + 贴图槽位绑定。

        骨骼合并模式下作为 CommandList_Draw_<部件前缀> 的回调主体（运行时逐实例
        MergedSkeleton_Apply 换绑合并骨架后调用）。非合并模式的 TextureOverrideIB
        段内仍保留一份内联实现（输出内容相同），未收敛以隔离回归风险。
        """
        section.append("run = CommandList\\EFMIv1\\OverrideTextures")

        ib_resource_name = "Resource_" + submesh_model.unique_str.replace("-","_") + "_Index"
        section.append("ib = " + ib_resource_name)

        for category in submesh_model.category_buffer_dict.keys():
            category_slot = submesh_model.d3d11_game_type.CategoryExtractSlotDict.get(category,"unknown_slot")
            category_resource_name = "Resource_" + submesh_model.unique_str.replace("-","_")  + "_" + category
            section.append(category_slot + " = " + category_resource_name)

        unique_str = submesh_model.unique_str
        section.append("vb3 = Resource_" + unique_str.replace('-', '_') + "_Position")

        if not GlobalProterties.forbid_auto_texture_ini() and drawib_model is not None:
            texture_markup_info_list = drawib_model.get_submesh_texture_markup_info_list(submesh_model)
            if GlobalProterties.use_rabbitfx_slot():
                for texture_markup_info in texture_markup_info_list:
                    if not M_IniHelper.is_slot_binding_mark_type(getattr(texture_markup_info, "mark_type", "")):
                        continue
                    if texture_markup_info.mark_name == "DiffuseMap":
                        section.append("Resource\\RabbitFx\\Diffuse = ref " + texture_markup_info.get_resource_name())
                    elif texture_markup_info.mark_name == "LightMap":
                        section.append("Resource\\RabbitFx\\LightMap = ref " + texture_markup_info.get_resource_name())
                    elif texture_markup_info.mark_name == "NormalMap":
                        section.append("Resource\\RabbitFx\\NormalMap = ref " + texture_markup_info.get_resource_name())

                section.append("run = CommandList\\RabbitFx\\SetTextures")

                for texture_markup_info in texture_markup_info_list:
                    if not M_IniHelper.is_slot_binding_mark_type(getattr(texture_markup_info, "mark_type", "")):
                        continue
                    if texture_markup_info.mark_name in ["DiffuseMap", "LightMap", "NormalMap"]:
                        pass
                    else:
                        slot = texture_markup_info.mark_slot
                        if slot and not slot.lower().startswith("ps-t"):
                            num_match = re.search(r'\d+', slot)
                            if num_match:
                                slot = "ps-t" + num_match.group()
                            else:
                                slot = "ps-t" + slot
                        section.append(slot + " = " + texture_markup_info.get_resource_name())
            else:
                for texture_markup_info in texture_markup_info_list:
                    if not M_IniHelper.is_slot_binding_mark_type(getattr(texture_markup_info, "mark_type", "")):
                        continue
                    section.append(texture_markup_info.mark_slot + " = " + texture_markup_info.get_resource_name())

    def generate_ini_file(self):
        ini_builder = M_IniBuilder()

        # EFMI 骨骼合并（Merged Skeleton）组件信息初始化
        self.merged_skeleton_components, self.merged_skeleton_component_id_dict = (
            self._get_merged_skeleton_component_info()
        )
        self.has_merged_skeleton = len(self.merged_skeleton_components) > 0
        if self.has_merged_skeleton:
            lod_bones: dict[str, int] = {}
            for c in self.merged_skeleton_components:
                lod = c["lod"]
                lod_bones[lod] = max(
                    lod_bones.get(lod, 0), c["vg_offset"] + c["vg_count"]
                )
            lod_summary = ", ".join(
                f"{lod or '根'}: {count} 槽" for lod, count in sorted(lod_bones.items())
            )
            print(
                f"[EFMI骨骼合并] 合并骨架: {len(self.merged_skeleton_components)} 个组件, "
                f"按 LOD 独立分组: {lod_summary}"
            )

        drawib_drawibmodel_dict = {
            drawib_model.draw_ib: drawib_model
            for drawib_model in self.drawib_model_list
        }
        draw_ib_active_index_dict = {
            drawib_model.draw_ib: index
            for index, drawib_model in enumerate(self.drawib_model_list)
        }

        if self.has_cross_ib:
            self._add_cross_ib_present_section(ini_builder)
            self._add_cross_ib_resource_id_sections(ini_builder)

        M_IniHelper.generate_hash_style_texture_ini(
            ini_builder=ini_builder,
            drawib_drawibmodel_dict=drawib_drawibmodel_dict,
        )
        M_IniHelper.generate_shared_slot_style_texture_ini(
            ini_builder=ini_builder,
            drawib_drawibmodel_dict=drawib_drawibmodel_dict,
        )

        self._integrate_object_swap_ini_hook(ini_builder)

        texture_override_ib_section = M_IniSection(M_SectionType.TextureOverrideIB)

        # EFMI 骨骼合并：官方运行时架构的组件绘制回调段（CommandList_Draw_<部件前缀>）
        # 与粘合层（CommandList_Component_DrawInstances，在 _add_merged_skeleton_section 追加）
        merged_command_lists = M_IniSection(M_SectionType.CommandList)

        for submesh_model in self.submesh_model_list:
            drawib_model = drawib_drawibmodel_dict.get(submesh_model.match_draw_ib)
            active_index = draw_ib_active_index_dict.get(submesh_model.match_draw_ib, 0)

            current_ib_key = self._get_submesh_ib_key(submesh_model)

            is_source_ib = current_ib_key in self.cross_ib_info_dict
            source_ib_list_for_target = self.cross_ib_target_info.get(current_ib_key, [])
            is_target_ib = len(source_ib_list_for_target) > 0

            if self.cross_ib_match_mode == 'INDEX_COUNT':
                current_identifier = submesh_model.match_index_count
            else:
                current_identifier = submesh_model.match_draw_ib

            # ===== EFMI 骨骼合并组件：EntryPoint + 运行时回调绘制（官方 1.4.1 架构）=====
            # 按用户要求不做 DRAW_TYPE 通道门控：所有通道均生效。
            merged_component_id = (
                self.merged_skeleton_component_id_dict.get(submesh_model.unique_str)
                if self.has_merged_skeleton
                else None
            )
            if merged_component_id is not None:
                # 段名用部件前缀（unique_str），与 Resource_<前缀>_* 命名约定一致，
                # 直接能看出是哪个部件；数字 component_id 仅用于运行时变量
                # （按 LOD 组内分配，只在本 LOD 的粘合层内有意义）。
                component_prefix = submesh_model.unique_str.replace("-", "_")
                entrypoint_section_name = "TextureOverride_EntryPoint_" + component_prefix
                draw_command_name = "CommandList_Draw_" + component_prefix
                component_lod = self._lod_name_from_unique_str(submesh_model.unique_str)
                lod_suffix = "_" + component_lod if component_lod else ""
                texture_override_ib_section.append("[" + entrypoint_section_name + "]")
                texture_override_ib_section.append("hash = " + submesh_model.match_draw_ib)
                texture_override_ib_section.append("match_first_index = " + submesh_model.match_first_index)
                texture_override_ib_section.append("match_index_count = " + submesh_model.match_index_count)
                # 原始绘制压制直接放 EntryPoint（本机所有可用 mod 的实证写法）：
                # 嵌套 CommandList 内的 handling=skip 在部分 3Dmigoto 分支不一定生效，
                # 粘合层里仍保留一份作为双保险。
                texture_override_ib_section.append("handling = skip")
                texture_override_ib_section.append(f"$\\EFMIv1\\component_id = {merged_component_id}")
                texture_override_ib_section.append("$\\EFMIv1\\gpu_posed = 1")
                texture_override_ib_section.append(
                    "CommandList\\EFMIv1\\Callback_Component_DrawCustom = ref " + draw_command_name
                )
                texture_override_ib_section.append(
                    "run = CommandList_Component_DrawInstances" + lod_suffix
                )
                if len(self.blueprint_model.keyname_mkey_dict.keys()) != 0:
                    texture_override_ib_section.append("$active" + str(active_index) + " = 1")
                    if GlobalProterties.generate_branch_mod_gui():
                        texture_override_ib_section.append("$ActiveCharacter = 1")
                texture_override_ib_section.new_line()

                if (is_source_ib or is_target_ib) and self.has_cross_ib:
                    # 跨 IB 重定向管线（录制骨骼/R redirect）与合并骨架互斥：
                    # 合并骨架下骨骼已统一，组件按自身 IB 直接绘制即可。
                    print(
                        f"[EFMI骨骼合并] 警告: {submesh_model.unique_str} 配置了跨 IB；"
                        "合并骨架模式下跨 IB 重定向不适用，已按自身 IB 直接绘制"
                    )

                # 组件绘制回调主体：运行时逐实例 Apply（换绑合并骨架）后调用
                merged_command_lists.append("[" + draw_command_name + "]")
                self._append_submesh_draw_bindings(
                    merged_command_lists, submesh_model, drawib_model
                )
                self._append_drawindexed_instanced_with_shader_replace(
                    merged_command_lists,
                    submesh_model.drawcall_model_list,
                    None,
                )
                merged_command_lists.new_line()
                continue

            texture_override_ib_section.append("[TextureOverride_" + submesh_model.unique_str.replace("-","_") + "]")
            texture_override_ib_section.append("hash = " + submesh_model.match_draw_ib)
            texture_override_ib_section.append("match_first_index = " + submesh_model.match_first_index)
            texture_override_ib_section.append("match_index_count = " + submesh_model.match_index_count)
            texture_override_ib_section.append("handling = skip")

            # EFMI 骨骼合并升宽配套（非组件兜底）：合并组件已在上方走 EntryPoint 分支，
            # 这里只服务"升宽但无反查数据"的子网格——仅输出 ElementFormat 行（数据侧升宽，无运行时挂载）。
            if getattr(submesh_model, "blendindices_widened", False):
                for semantic_index, element_format, extract_slot in (
                    self._validated_blendindices_layouts(
                        [submesh_model],
                        f"[EFMI骨骼合并] {submesh_model.unique_str}",
                    )
                ):
                    texture_override_ib_section.append(
                        f"{extract_slot}->ElementFormat(BLENDINDICES, {semantic_index}) = "
                        f"{element_format}"
                    )

            if is_target_ib:
                texture_override_ib_section.append("analyse_options = deferred_ctx_immediate dump_rt dump_cb dump_vb dump_ib buf txt dds dump_tex dds symlink")

            texture_override_ib_section.append("run = CommandList\\EFMIv1\\OverrideTextures")

            ib_resource_name = "Resource_" + submesh_model.unique_str.replace("-","_") + "_Index"
            texture_override_ib_section.append("ib = " + ib_resource_name)

            for category in submesh_model.category_buffer_dict.keys():
                category_slot = submesh_model.d3d11_game_type.CategoryExtractSlotDict.get(category,"unknown_slot")
                category_resource_name = "Resource_" + submesh_model.unique_str.replace("-","_")  + "_" + category
                texture_override_ib_section.append(category_slot + " = " + category_resource_name)

            unique_str = submesh_model.unique_str
            texture_override_ib_section.append("vb3 = Resource_" + unique_str.replace('-', '_') + "_Position")

            if not GlobalProterties.forbid_auto_texture_ini() and drawib_model is not None:
                texture_markup_info_list = drawib_model.get_submesh_texture_markup_info_list(submesh_model)
                if GlobalProterties.use_rabbitfx_slot():
                    for texture_markup_info in texture_markup_info_list:
                        if not M_IniHelper.is_slot_binding_mark_type(getattr(texture_markup_info, "mark_type", "")):
                            continue
                        if texture_markup_info.mark_name == "DiffuseMap":
                            texture_override_ib_section.append("Resource\\RabbitFx\\Diffuse = ref " + texture_markup_info.get_resource_name())
                        elif texture_markup_info.mark_name == "LightMap":
                            texture_override_ib_section.append("Resource\\RabbitFx\\LightMap = ref " + texture_markup_info.get_resource_name())
                        elif texture_markup_info.mark_name == "NormalMap":
                            texture_override_ib_section.append("Resource\\RabbitFx\\NormalMap = ref " + texture_markup_info.get_resource_name())
                    
                    texture_override_ib_section.append("run = CommandList\\RabbitFx\\SetTextures")
                    
                    for texture_markup_info in texture_markup_info_list:
                        if not M_IniHelper.is_slot_binding_mark_type(getattr(texture_markup_info, "mark_type", "")):
                            continue
                        if texture_markup_info.mark_name in ["DiffuseMap", "LightMap", "NormalMap"]:
                            pass
                        else:
                            slot = texture_markup_info.mark_slot
                            if slot and not slot.lower().startswith("ps-t"):
                                num_match = re.search(r'\d+', slot)
                                if num_match:
                                    slot = "ps-t" + num_match.group()
                                else:
                                    slot = "ps-t" + slot
                            texture_override_ib_section.append(slot + " = " + texture_markup_info.get_resource_name())
                else:
                    for texture_markup_info in texture_markup_info_list:
                        if not M_IniHelper.is_slot_binding_mark_type(getattr(texture_markup_info, "mark_type", "")):
                            continue
                        texture_override_ib_section.append(texture_markup_info.mark_slot + " = " + texture_markup_info.get_resource_name())

            is_both_source_and_target = is_source_ib and is_target_ib and self.has_cross_ib

            if is_both_source_and_target:
                cross_ib_drawcalls, non_cross_ib_drawcalls = self._split_drawcalls_by_cross_ib(
                    submesh_model.drawcall_model_list,
                    source_ib_key=current_ib_key
                )

                target_ib_keys = self.cross_ib_source_to_target_dict.get(current_ib_key, [])
                grouped_source_drawcalls = self._group_drawcalls_by_cross_ib_target(
                    cross_ib_drawcalls, current_ib_key, target_ib_keys
                )

                for (target_ib_key, vb_condition), objects in grouped_source_drawcalls.items():
                    if not objects:
                        continue

                    texture_override_ib_section.append(";跨 iB 区域")
                    self._append_source_cross_ib_replay(
                        texture_override_ib_section,
                        vb_condition,
                        objects,
                        current_identifier,
                    )

                texture_override_ib_section.append(";不需要跨 Ib 的物体引用")

                if non_cross_ib_drawcalls:
                    self._append_drawindexed_instanced_with_shader_replace(
                        texture_override_ib_section,
                        non_cross_ib_drawcalls,
                        None,
                    )

                if is_target_ib and source_ib_list_for_target:
                    self._append_target_cross_ib_blocks(
                        texture_override_ib_section, source_ib_list_for_target, current_ib_key
                    )

                texture_override_ib_section.append("")
                texture_override_ib_section.append("post vs-cb1 = null")
                texture_override_ib_section.append("post vs-cb2 = null")
                texture_override_ib_section.append("post vs-t0 = null")
                texture_override_ib_section.append("post cs-t2 = null")

            elif is_source_ib and self.has_cross_ib:
                target_ib_keys = self.cross_ib_source_to_target_dict.get(current_ib_key, [])
                target_ib_key = target_ib_keys[0] if target_ib_keys else None
                cross_ib_lines = self._generate_cross_ib_block_for_source(
                    current_identifier, submesh_model.drawcall_model_list,
                    source_ib_key=current_ib_key, target_ib_key=target_ib_key
                )
                for line in cross_ib_lines:
                    texture_override_ib_section.append(line)

            elif is_target_ib and self.has_cross_ib and source_ib_list_for_target:
                all_target_drawcalls = submesh_model.drawcall_model_list
                if all_target_drawcalls:
                    self._append_drawindexed_instanced_with_shader_replace(
                        texture_override_ib_section,
                        all_target_drawcalls,
                        None,
                    )

                self._append_target_cross_ib_blocks(
                    texture_override_ib_section, source_ib_list_for_target, current_ib_key
                )

                texture_override_ib_section.append("")
                texture_override_ib_section.append("post vs-cb1 = null")
                texture_override_ib_section.append("post vs-cb2 = null")
                texture_override_ib_section.append("post vs-t0 = null")
                texture_override_ib_section.append("post cs-t2 = null")

            else:
                self._append_drawindexed_instanced_with_shader_replace(
                    texture_override_ib_section,
                    submesh_model.drawcall_model_list,
                    None,
                )

            if len(self.blueprint_model.keyname_mkey_dict.keys()) != 0:
                texture_override_ib_section.append("$active" + str(active_index) + " = 1")
                if GlobalProterties.generate_branch_mod_gui():
                    texture_override_ib_section.append("$ActiveCharacter = 1")

            texture_override_ib_section.new_line()

        ini_builder.append_section(texture_override_ib_section)
        if self.has_merged_skeleton:
            ini_builder.append_section(merged_command_lists)

        resource_buffer_section = M_IniSection(M_SectionType.ResourceBuffer)
        buffer_folder_name = BlueprintExportHelper.get_current_buffer_folder_name()
        for submesh_model in self.submesh_model_list:
            ib_resource_name = "Resource_" + submesh_model.unique_str.replace("-","_") + "_Index"
            resource_buffer_section.append("[" + ib_resource_name + "]")
            resource_buffer_section.append("type = Buffer")
            resource_buffer_section.append("format = DXGI_FORMAT_R32_UINT")
            ib_name = getattr(submesh_model, "workspace_unique_str", "") or submesh_model.unique_str
            resource_buffer_section.append("filename = " + buffer_folder_name + "\\" + ib_name + "-Index.buf")
            resource_buffer_section.new_line()

            for category in submesh_model.category_buffer_dict.keys():
                category_resource_name = "Resource_" + submesh_model.unique_str.replace("-","_")  + "_" + category
                stride = submesh_model.d3d11_game_type.CategoryStrideDict.get(category,0)
                resource_buffer_section.append("[" + category_resource_name + "]")
                resource_buffer_section.append("type = Buffer")
                resource_buffer_section.append("stride = " + str(stride))
                resource_buffer_section.append("filename = " + buffer_folder_name + "\\" + submesh_model.unique_str + "-" + category + ".buf")
                resource_buffer_section.new_line()

        if not GlobalProterties.forbid_auto_texture_ini():
            resource_texture_section = M_IniSection(M_SectionType.ResourceTexture)
            appended_resource_names = set()
            for drawib_model in self.drawib_model_list:
                for submesh_model in drawib_model.submesh_model_list:
                    for texture_markup_info in drawib_model.get_submesh_texture_markup_info_list(submesh_model):
                        if getattr(texture_markup_info, "mark_type", "") != "Slot":
                            continue
                        resource_name = texture_markup_info.get_resource_name()
                        if resource_name in appended_resource_names:
                            continue
                        appended_resource_names.add(resource_name)
                        resource_texture_section.append("[" + texture_markup_info.get_resource_name() + "]")
                        resource_texture_section.append("filename = Textures/" + texture_markup_info.mark_filename)
                        resource_texture_section.new_line()
            ini_builder.append_section(resource_texture_section)

        ini_builder.append_section(resource_buffer_section)

        for drawib_model in self.drawib_model_list:
            M_IniHelper.move_slot_style_textures(draw_ib_model=drawib_model)

        GlobalKeyCountHelper.generated_mod_number = len(self.drawib_model_list)
        M_IniHelper.add_branch_key_sections(
            ini_builder=ini_builder,
            key_name_mkey_dict=self.blueprint_model.keyname_mkey_dict,
        )
        M_IniHelperGUI.add_branch_mod_gui_section(
            ini_builder=ini_builder,
            key_name_mkey_dict=self.blueprint_model.keyname_mkey_dict,
        )

        if self.has_shader_replace:
            M_IniHelper.add_shader_replace_sections(
                ini_builder=ini_builder,
                shader_replace_info_list=self.shader_replace_info_list,
                shader_replace_object_names=self.shader_replace_object_names,
                draw_call_models=self.blueprint_model.ordered_draw_obj_data_model_list,
                mod_export_path=GlobalConfig.path_generate_mod_folder(),
                use_instanced_draw=True,
                shader_replace_object_info_map=self.shader_replace_object_info_map,
                draw_call_offset_map=M_IniHelper.build_draw_call_offset_map(self.drawib_model_list),
            )

        # EFMI 骨骼合并（Merged Skeleton）段：在保存前追加（对齐 EFMI 1.4.1 运行时契约：
        # 命名空间配置 + 空间实例识别 + ConnectComponent 回调挂载，绘制走官方逐实例管线）
        if self.has_merged_skeleton:
            self._add_merged_skeleton_section(ini_builder, merged_command_lists)

        ini_filepath = os.path.join(GlobalConfig.path_generate_mod_folder(), GlobalConfig.get_workspace_name() + ".ini")
        ini_builder.save_to_file(ini_filepath)

        if self.has_cross_ib:
            self._copy_cross_ib_hlsl_files()

    def _append_target_cross_ib_blocks(self, section, source_ib_list_for_target, current_ib_key):
        for source_ib_key in source_ib_list_for_target:
            if self.cross_ib_match_mode == 'INDEX_COUNT':
                source_identifier = source_ib_key.replace('indexcount_', '') if source_ib_key.startswith('indexcount_') else source_ib_key.split("_")[0]
            else:
                source_hash = source_ib_key.split("_")[0]
                source_identifier = source_hash

            source_submesh = self._find_source_submesh_by_ib_key(source_ib_key)
            source_drawib_model = self._find_source_drawib_by_ib_key(source_ib_key)

            if not source_submesh or not source_drawib_model:
                continue

            cross_drawcalls, _ = self._split_drawcalls_by_cross_ib(
                source_submesh.drawcall_model_list,
                source_ib_key=source_ib_key,
                target_ib_key=current_ib_key
            )

            if not cross_drawcalls:
                continue

            grouped_cross_drawcalls = {}
            for drawcall_model in cross_drawcalls:
                obj_name = drawcall_model.obj_name if hasattr(drawcall_model, 'obj_name') else str(drawcall_model)
                vb_condition_target = self._get_vb_condition_for_object(obj_name, source_ib_key, current_ib_key, 'target')
                if vb_condition_target not in grouped_cross_drawcalls:
                    grouped_cross_drawcalls[vb_condition_target] = []
                grouped_cross_drawcalls[vb_condition_target].append(drawcall_model)

            for vb_condition_target, objects in grouped_cross_drawcalls.items():
                if not objects or not vb_condition_target:
                    continue

                section.append(f";跨 IB 身份块,绘制 {source_identifier} 需要跨 Ib 的物体引用")
                section.append(vb_condition_target)
                section.append(f"    cs-t2 = ResourceID_{source_identifier}")
                section.append(f"    run = CustomShader_RedirectCB1_{source_identifier}")
                section.append(f"    vs-t0 = ResourceFakeT0_SRV_{source_identifier}")
                section.append(f"    vs-cb2 = ResourceFakeCB1_{source_identifier}")
                section.append("    ;跨 IB 块数据区域")

                source_unique_str = source_submesh.unique_str
                section.append(f"    vb0 = Resource_{source_unique_str.replace('-', '_')}_Position")
                section.append(f"    vb1 = Resource_{source_unique_str.replace('-', '_')}_Texcoord")
                section.append(f"    vb2 = Resource_{source_unique_str.replace('-', '_')}_Blend")
                section.append(f"    vb3 = Resource_{source_unique_str.replace('-', '_')}_Position")
                src_ib_resource_name = "Resource_" + source_unique_str.replace('-', '_') + "_Index"
                section.append(f"    ib = {src_ib_resource_name}")

                section.append(";所有需要跨 Ib 的物体引用")

                self._append_drawindexed_instanced_with_shader_replace(
                    section,
                    objects,
                    getattr(source_drawib_model, "obj_name_draw_offset", None),
                )

                section.append("endif")

    def _copy_cross_ib_hlsl_files(self):
        addon_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
        source_dir = os.path.join(addon_dir, "Toolset")

        if not os.path.exists(source_dir):
            print(f"[CrossIB] 警告: Toolset目录不存在: {source_dir}")
            return

        hlsl_files = [
            'extract_cb1_ps.hlsl',
            'extract_cb1_vs.hlsl',
            'extract_capture_cb1_vs.hlsl',
            'record_bones_cs.hlsl',
            'redirect_cb1_cs.hlsl'
        ]

        refresh_hlsl_files = {
            'extract_cb1_vs.hlsl',
            'extract_capture_cb1_vs.hlsl',
        }

        mod_export_path = GlobalConfig.path_generate_mod_folder()
        res_dir = os.path.join(mod_export_path, "res")
        os.makedirs(res_dir, exist_ok=True)

        copied_count = 0
        for hlsl_file in hlsl_files:
            source_file = os.path.join(source_dir, hlsl_file)
            target_file = os.path.join(res_dir, hlsl_file)

            if os.path.exists(source_file):
                if hlsl_file in refresh_hlsl_files or not os.path.exists(target_file):
                    shutil.copy2(source_file, target_file)
                    print(f"[CrossIB] 已复制: {hlsl_file}")
                    copied_count += 1
                else:
                    print(f"[CrossIB] 文件已存在，跳过: {hlsl_file}")
            else:
                print(f"[CrossIB] 警告: 源文件不存在: {source_file}")

        print(f"[CrossIB] 共复制 {copied_count} 个HLSL文件到 {res_dir}")


    def _integrate_object_swap_ini_hook(self, ini_builder: M_IniBuilder):
        try:
            from ...blueprint.node_swap_ini import SwapKeyINIIntegrator
            from ...blueprint.export_helper import BlueprintExportHelper

            blueprint_tree = BlueprintExportHelper.get_current_blueprint_tree()
            if not blueprint_tree:
                return

            registry = getattr(self.blueprint_model, '_swap_key_registry', None)

            SwapKeyINIIntegrator.integrate_to_export(ini_builder, blueprint_tree, registry=registry)

        except ImportError:
            pass
        except Exception as e:
            from ...utils.log_utils import LOG
            LOG.warning(f"⚠️ 物体切换节点 INI 集成钩子执行失败: {e}")

    def export(self):
        try:
            TimerUtils.start_stage("缓冲文件生成")
            self.generate_buffer_files()
            TimerUtils.end_stage("缓冲文件生成")

            TimerUtils.start_stage("INI配置生成")
            self.generate_ini_file()
            TimerUtils.end_stage("INI配置生成")
        finally:
            self._cleanup_stub_objects()

    def export_buffers_only(self):
        """只导出 Buffer 文件，不生成 INI 配置"""
        try:
            TimerUtils.start_stage("缓冲文件生成")
            self.generate_buffer_files()
            TimerUtils.end_stage("缓冲文件生成")
        finally:
            self._cleanup_stub_objects()
