import os
import tempfile

import bpy

from ...common.draw_call_model import DrawCallModel
from ...common.global_config import GlobalConfig
from ...common.global_key_count_helper import GlobalKeyCountHelper
from ...common.global_properties import GlobalProterties
from ...common.m_ini_builder import M_IniBuilder, M_IniSection, M_SectionType
from ...common.m_ini_helper import M_IniHelper
from ...common.m_ini_helper_gui import M_IniHelperGUI
from ...utils.json_utils import JsonUtils
from ...utils.timer_utils import TimerUtils
from .unity import ExportUnity

# 导出模块在 Blender 启动时可以直接读取反查模块的版本；轻量 fake/旧插件环境
# 可能未加载该模块，使用同一当前版本常量仍保持“陈旧缓存拒绝”这一安全默认。
try:
    from ...common.zzmi_skeleton import ZZMI_VG_MAP_ALGORITHM_VERSION
except Exception:  # pragma: no cover - 仅兼容无完整 Blender 依赖的导入环境
    ZZMI_VG_MAP_ALGORITHM_VERSION = 3


class ZZMITextureMarkName:
    DiffuseMap = "DiffuseMap"
    NormalMap = "NormalMap"
    LightMap = "LightMap"
    MaterialMap = "MaterialMap"
    StockingMap = "StockingMap"


class ExportZZMI(ExportUnity):
    MERGED_SKELETON_ATTACH_THREADS = 64

    CROSS_IB_METHOD_VB_COPY = "VB_COPY"
    CROSS_IB_METHOD_VB_COPY_CB1 = "VB_COPY_CB1"
    CROSS_IB_METHOD_VB_REF_SO0 = "VB_REF_SO0"

    SUPPORTED_CROSS_IB_METHODS = {
        CROSS_IB_METHOD_VB_COPY,
        CROSS_IB_METHOD_VB_COPY_CB1,
        CROSS_IB_METHOD_VB_REF_SO0,
    }

    @staticmethod
    def _atomic_write_binary(path: str, payload: bytes) -> None:
        """同目录临时文件完整落盘后原子替换，失败时保留旧产物。"""
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(path)}.",
            suffix=".tmp",
            dir=directory,
        )
        try:
            with os.fdopen(fd, "wb") as temp_file:
                temp_file.write(payload)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, path)
        except Exception as exc:
            raise RuntimeError(f"原子发布二进制文件失败 {path}: {exc}") from exc
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    SLOT_FIX_RESOURCE_NAME_DICT = {
        ZZMITextureMarkName.DiffuseMap: r"Resource\ZZMI\Diffuse",
        ZZMITextureMarkName.NormalMap: r"Resource\ZZMI\NormalMap",
        ZZMITextureMarkName.LightMap: r"Resource\ZZMI\LightMap",
        ZZMITextureMarkName.MaterialMap: r"Resource\ZZMI\MaterialMap",
        ZZMITextureMarkName.StockingMap: r"Resource\ZZMI\WengineFx",
    }

    def __init__(self, blueprint_model):
        # ZZMI 骨骼合并（分支选项）：复选框开启时，为「DrawIB 内存在但蓝图里没有对象」
        # 的部件自动创建极限小三角面占位对象（必须在 super().__init__ 组装模型之前注入）
        self.blueprint_model = blueprint_model
        self._zzmi_stub_object_names = []
        self._zzmi_stub_draw_calls = []
        try:
            if GlobalProterties.import_merged_vgmap():
                # 占位是合并骨架渲染身份完整性的硬前提。创建失败时中止导出，
                # 不能回退到旧的 ib=null/IB skip 路径让部件静默消失并串扰其它 hash。
                self._zzmi_stub_object_names = self._ensure_stub_objects_for_missing_parts(blueprint_model)

            super().__init__(blueprint_model)

            self.cross_ib_info_dict = blueprint_model.cross_ib_info_dict
            self.cross_ib_method_dict = blueprint_model.cross_ib_method_dict
            self.cross_ib_mapping_method = getattr(blueprint_model, "cross_ib_mapping_method", {})
            self.has_cross_ib = blueprint_model.has_cross_ib
            self.cross_ib_object_names = blueprint_model.cross_ib_object_names

            self.shader_replace_info_list = getattr(blueprint_model, "shader_replace_info_list", [])
            self.shader_replace_object_names = getattr(blueprint_model, "shader_replace_object_names", set())
            self.shader_replace_object_info_map = getattr(blueprint_model, "shader_replace_object_info_map", {})
            self.has_shader_replace = getattr(blueprint_model, "has_shader_replace", False)

            # ZZMI 骨骼合并（分支选项）：export() 时按复选框 + 反查数据收集组件信息
            self.merged_skeleton_components = []
            self.merged_skeleton_component_id_dict = {}
            self.has_merged_skeleton = False
            # 合并网格自动重定向计划（_build_merged_mesh_redirect_plan 产出，INI 生成时查询）
            self._redirect_carrier_map: dict = {}
            self._redirect_target_map: dict = {}

            print(f"[CrossIB ZZMI] 初始化: has_cross_ib={self.has_cross_ib}")
            print(f"[CrossIB ZZMI] cross_ib_info_dict={self._format_cross_ib_info_dict(self.cross_ib_info_dict)}")
            print(f"[CrossIB ZZMI] cross_ib_object_names={self._format_name_set(self.cross_ib_object_names)}")
        except Exception:
            # 构造失败时 export() 的 finally 尚未接管；对象、mesh 与注入蓝图的
            # DrawCall 必须作为一个事务一起回滚，否则下一次导出会引用已删除对象。
            self._cleanup_stub_objects()
            raise

    # ------------------------------------------------------------------
    # 占位小三角面（合并骨架模式：部件无对象时不再输出 ib=null）
    # ------------------------------------------------------------------

    @staticmethod
    def _draw_call_object_name(draw_call) -> str:
        try:
            return str(draw_call.get_blender_obj_name() or "")
        except Exception:
            return str(getattr(draw_call, "obj_name", "") or "")

    @staticmethod
    def _remove_stub_object_data(obj):
        mesh = getattr(obj, "data", None)
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and getattr(mesh, "users", 0) == 0:
            try:
                bpy.data.meshes.remove(mesh)
            except (AttributeError, RuntimeError):
                pass

    def _purge_stale_stub_state(self, ordered):
        """清理上次异常残留的 stub 对象、mesh 与带标记 DrawCall。"""
        stale_names = set()
        for obj in list(bpy.data.objects):
            if not obj.get("ZZMI_STUB"):
                continue
            stale_names.add(str(obj.name))
            self._remove_stub_object_data(obj)

        if ordered is not None:
            ordered[:] = [
                draw_call
                for draw_call in ordered
                if not getattr(draw_call, "zzmi_stub", False)
                and self._draw_call_object_name(draw_call) not in stale_names
            ]

    def _ensure_stub_objects_for_missing_parts(self, blueprint_model) -> list[str]:
        """为「需要生成但没有对象」的部件创建极限小三角面占位对象。

        合并骨架模式下用户可自由 join/删改。占位规则（用户拍板）：
        - **部分缺失的 DrawIB**：缺失组件直接补占位（其几何显然被同 DrawIB 的
          幸存对象接管）；
        - **整个 DrawIB 缺席**：看它 VGMap 里的全局骨骼 id 是否被现存对象的顶点
          实际引用（权重>0）——被引用 = 几何被合并进了别的对象 → 全组件补占位
          （游戏内不可见的小三角，抑制原版 draw 防止重影）；零引用 = 用户压根
          不想生成 → 保持原样不插桩（该 DrawIB 不进入 mod，游戏内显示原版）。
        无反查数据（json 无 VGMap）的缺席 DrawIB 一律不插桩。
        返回创建的对象名列表（export() 结束后清理）。
        """
        workspace_root = GlobalConfig.path_workspace_folder()
        component_map_path = os.path.join(workspace_root, "LOD0", "DrawIB-Component.json")
        if not os.path.isfile(component_map_path):
            return []
        component_map = JsonUtils.LoadFromFile(component_map_path)
        if not isinstance(component_map, dict) or not component_map:
            return []

        ordered = getattr(blueprint_model, "ordered_draw_obj_data_model_list", None)
        if ordered is None:
            return []

        # 自愈必须早于 present 集合构建；否则残留 DrawCall 会被误判为真实部件，
        # 随后对象又被删除，SubMeshModel 构建必然引用一个不存在的对象。
        self._purge_stale_stub_state(ordered)

        present = set()
        for draw_call in ordered:
            try:
                unique_str = str(draw_call.get_workspace_unique_str() or "")
            except Exception:
                continue
            if unique_str:
                present.add(unique_str.split(".", 1)[-1])

        used_group_ids = None  # 惰性计算：首个全缺 DrawIB 需要判定时才算

        created = []
        for draw_ib, comp_dict in component_map.items():
            members = sorted(str(v) for v in (comp_dict or {}).values())
            if not members:
                continue

            if any(member in present for member in members):
                # 部分缺失：缺失组件补占位
                stub_members = [member for member in members if member not in present]
            else:
                # 整个 DrawIB 缺席：判定几何是否被合并进其它对象
                if used_group_ids is None:
                    used_group_ids = self._collect_used_group_ids(ordered)
                if self._is_drawib_absorbed(draw_ib, workspace_root, used_group_ids):
                    stub_members = members
                    print(
                        f"[ZZMI骨骼合并] DrawIB {draw_ib} 没有对象，但其全局骨骼被其它模型引用"
                        f"（几何已被合并），全组件补占位小三角面"
                    )
                else:
                    print(f"[ZZMI骨骼合并] DrawIB {draw_ib} 无对象且骨骼未被引用，按用户意图不生成")
                    continue

            for member in stub_members:
                obj_name = self._create_stub_object(member)
                if obj_name:
                    # 必须在任何后续构造步骤之前登记到实例；否则批量创建中途
                    # 失败时 helper 尚未返回，__init__ 的异常回滚拿不到先前对象。
                    self._zzmi_stub_object_names.append(obj_name)
                    # 该 DrawCall 可能在 SubMeshModel 完成前就被用于生成 IB override。
                    # 显式填入占位几何的导出计数，避免默认的 0 让占位段退化成
                    # drawindexed = 0；SubMeshModel 后续仍会用真实 mesh 再校准一次。
                    stub_draw_call = DrawCallModel(obj_name=obj_name)
                    stub_draw_call.vertex_count = 3
                    stub_draw_call.index_count = 3
                    stub_draw_call.index_offset = 0
                    stub_draw_call.zzmi_stub = True
                    ordered.append(stub_draw_call)
                    self._zzmi_stub_draw_calls.append(stub_draw_call)
                    created.append(obj_name)
                    print(
                        f"[ZZMI骨骼合并] 部件 {member} 没有对应对象，"
                        f"已创建极限小三角面占位（游戏内不可见）"
                    )
        return created

    def _load_drawib_vg_values(self, draw_ib: str, workspace_root: str) -> set[int]:
        """读取 DrawIB 全部组件写回的 VGMap 全局骨骼 id 集合（无数据返回空）。"""
        values = set()
        lod0_dir = os.path.join(workspace_root, "LOD0")
        if not os.path.isdir(lod0_dir):
            return values
        for name in os.listdir(lod0_dir):
            if not name.startswith(draw_ib + "-"):
                continue
            submesh_dir = os.path.join(lod0_dir, name)
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

    def _is_drawib_absorbed(self, draw_ib: str, workspace_root: str, used_group_ids: set[int]) -> bool:
        """判定整个缺席的 DrawIB 是否被合并进了其它对象。

        判据（用户定义）：该 DrawIB VGMap 的全局骨骼 id 有被现存对象顶点引用（权重>0）。
        全局骨骼编号命名空间下引用判定无歧义；跨组别引用会被
        _warn_cross_group_bone_references 在导出时大声报警（无校准模式下已禁止）。
        """
        vg_values = self._load_drawib_vg_values(draw_ib, workspace_root)
        if not vg_values:
            return False
        return bool(vg_values & used_group_ids)

    def _collect_used_group_ids(self, ordered) -> set[int]:
        """收集蓝图内全部对象实际引用（权重>0）的顶点组 id 集合。"""
        used = set()
        for draw_call in ordered:
            try:
                obj_name = draw_call.get_blender_obj_name()
            except Exception:
                continue
            obj = bpy.data.objects.get(obj_name) if obj_name else None
            if obj is None or obj.get("ZZMI_STUB"):
                continue
            mesh = getattr(obj, "data", None) if obj is not None else None
            vertices = getattr(mesh, "vertices", None)
            if vertices is None:
                continue
            for vertex in vertices:
                for group_elem in vertex.groups:
                    if group_elem.weight <= 0:
                        continue
                    try:
                        group_index = int(group_elem.group)
                        group_name = str(obj.vertex_groups[group_index].name).strip()
                    except (AttributeError, IndexError, TypeError, ValueError):
                        continue
                    if group_name.isdigit():
                        used.add(int(group_name))
        return used

    def _build_shader_replace_base_vertex_map(self) -> dict[int, int]:
        """返回重定向 DrawCall 身份到 base_vertex 的映射。"""
        base_vertex_map: dict[int, int] = {}
        for carrier_ib, redirect_info in (self._redirect_carrier_map or {}).items():
            base_vertex = int(redirect_info.get("base_vertex", 0) or 0)
            for drawib_model in self.drawib_model_list:
                if drawib_model.draw_ib != carrier_ib:
                    continue
                for submesh_model in getattr(drawib_model, "submesh_model_list", []) or []:
                    for draw_call in getattr(submesh_model, "drawcall_model_list", []) or []:
                        base_vertex_map[id(draw_call)] = base_vertex
        return base_vertex_map


    def _create_stub_object(self, bare_unique_str: str) -> str:
        """创建占位对象：3 顶点 1 三角面（1e-6 尺度），权重全给组 "0"。"""
        workspace_unique_str = bare_unique_str
        if not workspace_unique_str.upper().startswith("LOD"):
            workspace_unique_str = "LOD0." + workspace_unique_str

        mesh = None
        obj = None
        try:
            mesh = bpy.data.meshes.new(name="ZZMI_STUB_MESH_" + workspace_unique_str)
            mesh.from_pydata(
                [(0.0, 0.0, 0.0), (1e-6, 0.0, 0.0), (0.0, 1e-6, 0.0)],
                [],
                [(0, 1, 2)],
            )
            mesh.update()

            obj = bpy.data.objects.new(name=workspace_unique_str, object_data=mesh)
            obj["ZZMI_STUB"] = 1
            obj["3DMigoto:WorkspaceUniqueStr"] = workspace_unique_str
            vertex_group = obj.vertex_groups.new(name="0")
            vertex_group.add([0, 1, 2], 1.0, 'REPLACE')

            try:
                bpy.context.collection.objects.link(obj)
            except Exception:
                bpy.context.scene.collection.objects.link(obj)
            return obj.name
        except Exception:
            if obj is not None and bpy.data.objects.get(obj.name) is not None:
                self._remove_stub_object_data(obj)
            elif mesh is not None and getattr(mesh, "users", 0) == 0:
                try:
                    bpy.data.meshes.remove(mesh)
                except (AttributeError, RuntimeError):
                    pass
            raise

    def _cleanup_stub_objects(self):
        """导出结束后移除占位对象、mesh 数据和注入蓝图的 DrawCall。"""
        tracked_draw_calls = list(getattr(self, "_zzmi_stub_draw_calls", []) or [])
        object_names = set(getattr(self, "_zzmi_stub_object_names", []) or [])
        object_names.update(
            self._draw_call_object_name(draw_call)
            for draw_call in tracked_draw_calls
        )
        object_names.discard("")
        tracked_draw_call_ids = {id(draw_call) for draw_call in tracked_draw_calls}
        ordered = getattr(
            getattr(self, "blueprint_model", None),
            "ordered_draw_obj_data_model_list",
            None,
        )
        if ordered is not None:
            ordered[:] = [
                draw_call
                for draw_call in ordered
                if id(draw_call) not in tracked_draw_call_ids
                and not getattr(draw_call, "zzmi_stub", False)
            ]

        for obj_name in sorted(object_names):
            obj = bpy.data.objects.get(obj_name)
            if obj is None:
                continue
            self._remove_stub_object_data(obj)
        if object_names:
            print(f"[ZZMI骨骼合并] 已清理 {len(object_names)} 个占位小三角面对象")
        self._zzmi_stub_object_names = []
        self._zzmi_stub_draw_calls = []

    def _collect_merged_skeleton_components(self):
        """收集 ZZMI 合并骨架组件信息（按 DrawIB 去重，骨架组+vg_offset 排序）。

        双条件门控：复选框 import_merged_vgmap 开启 且 子网格 json 已由反查写回
        VGCount > 0（common/zzmi_skeleton.py 的 ensure_skeleton_data）。
        同 DrawIB 的拆分子网格共享同一 palette/偏移，只取第一个有效值。
        skeleton_group：渲染 cb1 对象变换分组号（json SkeletonGroup 字段），
        每组一套 ResourceZZMergedSkeleton_G<N>，跨组绝不共享。
        返回 (components, {draw_ib: component_id})。
        """
        components = []
        if not GlobalProterties.import_merged_vgmap():
            return components, {}
        for drawib_model in self.drawib_model_list:
            for submesh_model in drawib_model.submesh_model_list:
                if not bool(
                    getattr(submesh_model, "merged_skeleton_metadata_valid", True)
                ):
                    print(
                        f"[ZZMI骨骼合并] 警告 {drawib_model.draw_ib}: "
                        "骨骼合并元数据含非整数/越界值，该部件不进入合并骨架；"
                        "请重新生成骨骼合并缓存"
                    )
                    continue
                vg_count = int(getattr(submesh_model, "vg_count", 0) or 0)
                if vg_count <= 0:
                    continue
                cache_version = getattr(submesh_model, "vg_map_algorithm_version", None)
                if (
                    cache_version is not None
                    and int(cache_version or 0) != ZZMI_VG_MAP_ALGORITHM_VERSION
                ):
                    print(
                        f"[ZZMI骨骼合并] 警告 {drawib_model.draw_ib}: "
                        f"VGMap 缓存版本 {cache_version} != 当前版本 "
                        f"{ZZMI_VG_MAP_ALGORITHM_VERSION}，拒绝导出该部件；"
                        "请先用当前 FrameAnalysis，或仅凭工作区缓存，重新一键导入"
                    )
                    continue
                # 导出侧防线：VGMap 必须完整覆盖 0..vg_count-1 且槽位非负。
                # 缓存正常时由 ensure_skeleton_data 保证；此处兜底拦截陈旧/被
                # 手工改坏的 json——缺键会让 attach CS 的 vg_map.get(local, 0)
                # 静默塌缩到槽位 0，整块蒙皮炸裂，宁可整部件退出合并骨架。
                try:
                    vg_map = {}
                    for raw_key, raw_value in (
                        getattr(submesh_model, "vg_map", {}) or {}
                    ).items():
                        key = int(raw_key)
                        if key in vg_map:
                            raise ValueError(f"规范化后键重复: {key}")
                        vg_map[key] = int(raw_value)
                except (TypeError, ValueError):
                    vg_map = {}
                expected_keys = set(range(vg_count))
                missing_keys = sorted(expected_keys - set(vg_map.keys()))
                extra_keys = sorted(set(vg_map.keys()) - expected_keys)
                negative_slots = [slot for slot in vg_map.values() if slot < 0]
                oversized_slots = [slot for slot in vg_map.values() if slot > 0xFFFFFFFF]
                vg_offset = int(getattr(submesh_model, "vg_offset", 0) or 0)
                skeleton_group = int(getattr(submesh_model, "skeleton_group", 0) or 0)
                if (
                    missing_keys
                    or extra_keys
                    or negative_slots
                    or oversized_slots
                    or vg_offset < 0
                    or skeleton_group < 0
                ):
                    print(
                        f"[ZZMI骨骼合并] 警告 {drawib_model.draw_ib}: VGMap 未完整覆盖 "
                        f"0..{vg_count - 1}（缺失 {missing_keys[:5]}，多余 {extra_keys[:5]}）"
                        "、槽位/偏移/分组越界，"
                        "该部件不进入合并骨架；请重新一键导入刷新骨骼合并缓存"
                    )
                    continue
                components.append({
                    "draw_ib": drawib_model.draw_ib,
                    "unique_str": str(getattr(submesh_model, "unique_str", "") or ""),
                    "vg_offset": vg_offset,
                    "vg_count": vg_count,
                    "skeleton_group": skeleton_group,
                    # 局部骨骼 id -> 全局槽位（attach CS 按此写合并骨架，
                    # 本部件引用的共享 canonical 槽位当帧覆盖）
                    "vg_map": vg_map,
                    # 导出侧守卫元数据（反查写回）：deform pass draw 序号 +
                    # 原部件顶点数；缺省 0（旧缓存未刷新）
                    "deform_draw": int(getattr(submesh_model, "deform_draw_index", 0) or 0),
                    "original_vertex_count": int(
                        getattr(submesh_model, "original_vertex_count", 0) or 0
                    ),
                })
                break
        if components:
            buffer_slots = max(c["vg_offset"] + c["vg_count"] for c in components)
            valid_components = []
            for component in components:
                invalid_slots = sorted({
                    slot for slot in component["vg_map"].values()
                    if slot >= buffer_slots
                })
                if invalid_slots:
                    print(
                        f"[ZZMI骨骼合并] 警告 {component['draw_ib']}: VGMap 槽位 "
                        f"{invalid_slots[:5]} 超出合并骨架范围 0..{buffer_slots - 1}，"
                        "该部件不进入合并骨架；请重新一键导入刷新骨骼合并缓存"
                    )
                    continue
                valid_components.append(component)
            components = valid_components
        components.sort(key=lambda c: (c["skeleton_group"], c["vg_offset"], c["draw_ib"]))
        component_id_dict = {c["draw_ib"]: i for i, c in enumerate(components)}
        return components, component_id_dict

    def _get_submesh_ib_key(self, submesh_model, draw_ib):
        return f"{draw_ib}_{submesh_model.match_first_index}"

    def _append_drawindexed_with_shader_replace(
        self, section, drawcall_list, draw_offset_dict, base_vertex=0
    ):
        """将 drawcall 列表写入 section，对着色器替换物体使用条件运行逻辑替代 drawindexed。

        ``base_vertex`` 用于合并网格自动重定向。保持普通绘制的同一输出路径，
        因此重定向绘制也会生成 mesh 注释、条件块和 shader-replace 逻辑。
        """
        if not self.has_shader_replace:
            drawindexed_kwargs = {"obj_name_draw_offset_dict": draw_offset_dict}
            if base_vertex:
                drawindexed_kwargs["base_vertex"] = base_vertex
            for drawindexed_str in M_IniHelper.get_drawindexed_str_list(drawcall_list, **drawindexed_kwargs):
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
                drawindexed_kwargs = {"obj_name_draw_offset_dict": draw_offset_dict}
                if base_vertex:
                    drawindexed_kwargs["base_vertex"] = base_vertex
                for drawindexed_str in M_IniHelper.get_drawindexed_str_list([dc], **drawindexed_kwargs):
                    section.append(drawindexed_str)
                continue

            draw_offset = dc.index_offset
            if draw_offset_dict:
                draw_offset = draw_offset_dict.get(dc.obj_name, dc.index_offset)

            # 输出物体标识注释（与 get_drawindexed_str_list 格式一致）
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
                    base_vertex,
                )
                for line in run_lines:
                    section.append(f"{indent}{line}")
                if condition_str:
                    section.append("endif")
            section.append("")

    @staticmethod
    def _format_name_set(names) -> list[str]:
        return sorted(str(name) for name in (names or []))

    @staticmethod
    def _format_cross_ib_info_dict(mapping) -> dict[str, list[str]]:
        ordered = {}
        for key in sorted((mapping or {}).keys(), key=str):
            ordered[str(key)] = sorted(str(item) for item in ((mapping or {}).get(key) or []))
        return ordered

    def _get_mapping_method(self, source_ib_key: str, target_ib_key: str) -> str:
        return self.cross_ib_mapping_method.get(
            (source_ib_key, target_ib_key),
            self.CROSS_IB_METHOD_VB_COPY,
        )

    def _get_source_methods(self, source_ib_key: str) -> set[str]:
        methods = {
            method
            for (mapped_source_key, _mapped_target_key), method in self.cross_ib_mapping_method.items()
            if mapped_source_key == source_ib_key
        }
        if not methods and source_ib_key in self.cross_ib_info_dict:
            methods.add(self.CROSS_IB_METHOD_VB_COPY)
        return methods

    def _get_source_body_vb_resource_name(self, source_hash: str, source_first_index: int) -> str:
        return f"ResourceBodyVB_{source_hash}_{source_first_index}"

    def _get_source_cb1_capture_resource_name(self, source_hash: str, source_first_index: int) -> str:
        return f"ResourceCaptureCB1_{source_hash}_{source_first_index}"

    def _get_target_cb1_temp_resource_name(self, target_hash: str, target_first_index: int) -> str:
        return f"ResourceTempCB1_{target_hash}_{target_first_index}"

    def _get_source_so0_resource_name(self, source_hash: str, source_first_index: int) -> str:
        return f"ResourceBodyVB0_{source_hash}_{source_first_index}"

    def _append_source_capture_sections(
        self,
        section: M_IniSection,
        source_hash: str,
        source_first_index: int,
        source_methods: set[str],
    ) -> None:
        if self.CROSS_IB_METHOD_VB_REF_SO0 in source_methods:
            section.append("[" + self._get_source_so0_resource_name(source_hash, source_first_index) + "]")
            section.append("type = Buffer")
            section.append("stride = 40")

        if self.CROSS_IB_METHOD_VB_COPY in source_methods or self.CROSS_IB_METHOD_VB_COPY_CB1 in source_methods:
            section.append("[" + self._get_source_body_vb_resource_name(source_hash, source_first_index) + "]")

        if self.CROSS_IB_METHOD_VB_COPY_CB1 in source_methods:
            section.append("[" + self._get_source_cb1_capture_resource_name(source_hash, source_first_index) + "]")

    def _append_source_capture_lines(
        self,
        section: M_IniSection,
        source_hash: str,
        source_first_index: int,
        source_methods: set[str],
    ) -> None:
        if self.CROSS_IB_METHOD_VB_REF_SO0 in source_methods:
            section.append(
                self._get_source_so0_resource_name(source_hash, source_first_index) + " = ref so0"
            )

        if self.CROSS_IB_METHOD_VB_COPY in source_methods or self.CROSS_IB_METHOD_VB_COPY_CB1 in source_methods:
            section.append(
                self._get_source_body_vb_resource_name(source_hash, source_first_index) + " = copy vb0"
            )

        if self.CROSS_IB_METHOD_VB_COPY_CB1 in source_methods:
            section.append(
                self._get_source_cb1_capture_resource_name(source_hash, source_first_index)
                + " = copy vs-cb1 unless_null"
            )

    def _append_source_capture_override(
        self,
        section: M_IniSection,
        texture_override_name_suffix: str,
        source_hash: str,
        source_first_index: int,
        source_methods: set[str],
    ) -> None:
        section.append("[TextureOverride_" + texture_override_name_suffix + "_copy]")
        section.append("hash = " + source_hash)
        section.append("match_first_index = " + str(source_first_index))
        section.append("match_instance_count = 0")
        self._append_source_capture_lines(
            section,
            source_hash,
            source_first_index,
            source_methods,
        )

    def _append_target_cross_ib_draw(
        self,
        section: M_IniSection,
        method: str,
        source_hash: str,
        source_first_index: int,
        source_ib_resource_name: str,
        target_hash: str,
        target_first_index: int,
    ) -> None:
        section.append("ib = " + source_ib_resource_name)

        if method == self.CROSS_IB_METHOD_VB_REF_SO0:
            source_body_vb0_name = self._get_source_so0_resource_name(source_hash, source_first_index)
            section.append("vb0 = " + source_body_vb0_name)
            section.append("vb1 = Resource" + source_hash + "Texcoord")
            section.append("vb2 = Resource" + source_hash + "Blend")
            section.append("vb3 = " + source_body_vb0_name)
            return

        source_body_vb_name = self._get_source_body_vb_resource_name(source_hash, source_first_index)
        section.append("vb0 = " + source_body_vb_name)
        section.append("vb1 = Resource" + source_hash + "Texcoord")

        if method == self.CROSS_IB_METHOD_VB_COPY_CB1:
            temp_resource_name = self._get_target_cb1_temp_resource_name(target_hash, target_first_index)
            section.append(temp_resource_name + " = ref vs-cb1")
            section.append("vs-cb1 = " + self._get_source_cb1_capture_resource_name(source_hash, source_first_index))
        else:
            section.append("vb2 = Resource" + source_hash + "Blend")
            section.append("vb3 = " + source_body_vb_name)

    def _append_target_cross_ib_cleanup(
        self,
        section: M_IniSection,
        method: str,
        target_hash: str,
        target_first_index: int,
    ) -> None:
        if method == self.CROSS_IB_METHOD_VB_COPY_CB1:
            temp_resource_name = self._get_target_cb1_temp_resource_name(target_hash, target_first_index)
            section.append("vs-cb1 = ref " + temp_resource_name)

    def _find_source_submesh(self, source_ib_key: str):
        source_parts = source_ib_key.split("_")
        source_hash = source_parts[0]
        source_first_index = int(source_parts[1]) if len(source_parts) > 1 else 0

        source_drawib_model = None
        for dib_model in self.drawib_model_list:
            if dib_model.draw_ib == source_hash:
                source_drawib_model = dib_model
                break

        if source_drawib_model is None:
            return None, None, source_hash, source_first_index

        for source_submesh in source_drawib_model.submesh_model_list:
            if str(source_submesh.match_first_index) == str(source_first_index):
                return source_drawib_model, source_submesh, source_hash, source_first_index

        return source_drawib_model, None, source_hash, source_first_index

    def add_unity_vs_texture_override_vb_sections(self, ini_builder: M_IniBuilder, drawib_model):
        d3d11_game_type = drawib_model.d3d11GameType
        draw_ib = drawib_model.draw_ib

        so0_source_resource_names = []
        for submesh_model in drawib_model.submesh_model_list:
            source_ib_key = self._get_submesh_ib_key(submesh_model, draw_ib)
            if self.CROSS_IB_METHOD_VB_REF_SO0 in self._get_source_methods(source_ib_key):
                so0_source_resource_names.append(
                    self._get_source_so0_resource_name(draw_ib, submesh_model.match_first_index)
                )

        texture_override_vb_section = M_IniSection(M_SectionType.TextureOverrideVB)
        texture_override_vb_section.append("; " + draw_ib)
        for category_name in d3d11_game_type.OrderedCategoryNameList:
            category_hash = drawib_model.category_hash_dict.get(category_name, "")
            texture_override_vb_name_suffix = "VB_" + draw_ib + "_" + drawib_model.draw_ib_alias + "_" + category_name
            texture_override_vb_section.append("[TextureOverride_" + texture_override_vb_name_suffix + "]")
            texture_override_vb_section.append("hash = " + category_hash)

            for original_category_name, draw_category_name in d3d11_game_type.CategoryDrawCategoryDict.items():
                if category_name != draw_category_name:
                    continue
                category_original_slot = d3d11_game_type.CategoryExtractSlotDict[original_category_name]
                texture_override_vb_section.append(category_original_slot + " = Resource" + draw_ib + original_category_name)

            draw_category_name = d3d11_game_type.CategoryDrawCategoryDict.get("Blend", None)
            if draw_category_name is not None and category_name == draw_category_name:
                # ZZMI 骨骼合并：deform draw 前把当帧 palette copy 成持久资源，
                # 立即 attach 到本组骨架，并记录该部件本帧已到达。合并网格的
                # 可见 draw 由依赖就绪守卫控制，避免目标先到时读取半成品骨架。
                merged_component = self.merged_skeleton_component_id_dict.get(draw_ib)
                component = (
                    self.merged_skeleton_components[merged_component]
                    if merged_component is not None else None
                )
                if component is not None:
                    component_id = int(merged_component)
                    skeleton_group = int(component["skeleton_group"])
                    seen_var = f"$zz_ms_seen_c{component_id}"
                    texture_override_vb_section.append(
                        f"ResourceZZPalette_{draw_ib} = copy vs-t0 unless_null"
                    )
                    texture_override_vb_section.append(
                        f"run = CustomShaderZZMIMergedSkeletonAttach_C{component_id}"
                    )
                    texture_override_vb_section.append(f"{seen_var} = 1")
                    texture_override_vb_section.append(
                        f"vs-t0 = ResourceZZMergedSkeleton_G{skeleton_group}"
                    )
                redirect_target_plan = self._redirect_target_map.get(draw_ib)
                if redirect_target_plan is not None:
                    # 有真实几何的 target 仍由自身捕获 SO；纯占位 target 则由
                    # 兼容的 carrier 捕获，避免 target 晚到时把有效 carrier SO
                    # 覆盖为空或以 BI4 布局执行 BI16 重放。
                    if redirect_target_plan.get("target_has_real_geometry", True):
                        texture_override_vb_section.append(
                            f"ResourceZZRedirectSO_{draw_ib} = ref so0"
                        )
                # 纯占位 target 的 SO owner 是第一个 carrier。该赋值必须只在
                # owner 挂点出现；若 target 晚到，不能再次覆盖已写入的有效 SO。
                for owner_target_ib, owner_plan in self._redirect_target_map.items():
                    if (
                        owner_plan.get("so_owner_ib") == draw_ib
                        and not owner_plan.get("target_has_real_geometry", True)
                    ):
                        texture_override_vb_section.append(
                            f"ResourceZZRedirectSO_{owner_target_ib} = ref so0"
                        )
                texture_override_vb_section.append("handling = skip")

                # 合并网格自动重定向：carrier 的 deform 退化为 3 顶点 stub draw
                # （保留 copy palette + attach 写当帧骨骼）；target 的 deform 追加
                # 画重定向的合并网格（绑定 carrier 的 vb0/vb2，SO 按序拼接）。
                redirect_carrier = self._redirect_carrier_map.get(draw_ib)
                if redirect_carrier is not None:
                    texture_override_vb_section.append("draw = 3, 0")
                elif redirect_target_plan is not None:
                    pass  # target 自身几何也在 guarded target-SO 重放中统一绘制
                else:
                    texture_override_vb_section.append(
                        "draw = " + str(drawib_model.draw_number) + ", 0"
                    )

                # 合并网格的可见几何不再固定在 target 的 deform 顺序上：
                # 所有依赖组件挂点都尝试，但只有依赖 palette 全部当帧 attach 后
                # 的第一个挂点真正 draw。无论当前是 target 还是 carrier，都把
                # SO 明确绑回已捕获的 target SO，渲染侧数据源保持不变。
                deferred_plans = [
                    (target_ib, plan)
                    for target_ib, plan in self._redirect_target_map.items()
                    if merged_component is not None
                    and int(merged_component) in plan.get("required_component_ids", [])
                ]
                for deferred_target_ib, deferred_plan in deferred_plans:
                    compatible_ids = deferred_plan.get("compatible_component_ids")
                    if (
                        compatible_ids is not None
                        and int(merged_component) not in compatible_ids
                    ):
                        continue
                    required_ids = deferred_plan.get("required_component_ids", [])
                    all_seen = " && ".join(
                        f"$zz_ms_seen_c{cid} == 1" for cid in required_ids
                    )
                    drawn_var = f"$zz_ms_redirect_drawn_{deferred_target_ib}"
                    texture_override_vb_section.append(
                        f"if {all_seen} && {drawn_var} == 0"
                    )
                    texture_override_vb_section.append(f"    {drawn_var} = 1")
                    texture_override_vb_section.append(
                        f"    so0 = ref ResourceZZRedirectSO_{deferred_target_ib}"
                    )
                    for vb0_resource, vb2_resource, draw_count in deferred_plan.get(
                        "deform_draws", []
                    ):
                        texture_override_vb_section.append("    vb2 = " + vb2_resource)
                        texture_override_vb_section.append("    vb0 = " + vb0_resource)
                        texture_override_vb_section.append(
                            "    draw = " + str(draw_count) + ", 0"
                        )
                    texture_override_vb_section.append("    so0 = null")
                    texture_override_vb_section.append("endif")
                for so0_source_resource_name in so0_source_resource_names:
                    texture_override_vb_section.append(so0_source_resource_name + " = ref so0")

            if category_name == d3d11_game_type.CategoryDrawCategoryDict["Position"]:
                if len(self.blueprint_model.keyname_mkey_dict.keys()) != 0:
                    texture_override_vb_section.append("$active0 = 1")
                    if GlobalProterties.generate_branch_mod_gui():
                        texture_override_vb_section.append("$ActiveCharacter = 1")

            texture_override_vb_section.new_line()

        ini_builder.append_section(texture_override_vb_section)

    def add_unity_vs_texture_override_vlr_section(
        self, ini_builder: M_IniBuilder, drawib_model, include_uav_byte_stride: bool = True
    ):
        """VertexLimitRaise 段（覆盖基类）：合并网格自动重定向时按 SO 实际大小声明。

        carrier（被重定向的合并网格挂载 IB）SO 退化为 3 顶点 stub；
        target（组内最后 deform draw 的 IB）SO = 自身真实几何 + 全部重定向
        合并网格之和。
        """
        d3d11_game_type = getattr(drawib_model, "d3d11GameType", None)
        if d3d11_game_type is None or not getattr(d3d11_game_type, "GPU_PreSkinning", False):
            return
        draw_ib = drawib_model.draw_ib
        redirect_carrier = self._redirect_carrier_map.get(draw_ib)
        redirect_target = self._redirect_target_map.get(draw_ib)
        if redirect_carrier is None and redirect_target is None:
            super().add_unity_vs_texture_override_vlr_section(
                ini_builder=ini_builder,
                drawib_model=drawib_model,
                include_uav_byte_stride=include_uav_byte_stride,
            )
            return

        if redirect_carrier is not None:
            carrier_target_plan = self._redirect_target_map.get(
                redirect_carrier.get("target"), {}
            )
            if (
                not carrier_target_plan.get("target_has_real_geometry", True)
                and carrier_target_plan.get("so_owner_ib") == draw_ib
            ):
                vertex_count = carrier_target_plan["so_vertex_count"]
            else:
                vertex_count = 3
        else:
            vertex_count = redirect_target["so_vertex_count"]
        vertexlimit_section = M_IniSection(M_SectionType.TextureOverrideVertexLimitRaise)
        vertexlimit_section.append(
            "[TextureOverride_" + draw_ib + "_" + drawib_model.draw_ib_alias
            + "_VertexLimitRaise]"
        )
        vertexlimit_section.append("hash = " + drawib_model.vertex_limit_hash)
        vertexlimit_section.append(
            "override_byte_stride = "
            + str(d3d11_game_type.CategoryStrideDict["Position"])
        )
        vertexlimit_section.append("override_vertex_count = " + str(vertex_count))
        if include_uav_byte_stride:
            vertexlimit_section.append("uav_byte_stride = 4")
        vertexlimit_section.new_line()
        ini_builder.append_section(vertexlimit_section)

    def _merged_skeleton_groups(self) -> list[int]:
        """当前导出组件涉及的骨架组列表（升序）。"""
        return sorted({c["skeleton_group"] for c in self.merged_skeleton_components})

    # ------------------------------------------------------------------
    # 跨组别引用守卫（无校准模式：禁止跨组别骨骼合并）
    # ------------------------------------------------------------------

    def _collect_drawib_referenced_bone_ids(self, draw_ib: str) -> set[int]:
        """该 DrawIB 全部子网格源对象实际引用（权重>0）的骨骼 id 集合。

        骨骼 id 取顶点组**名字**（导入约定：组名 = 全局骨骼 id；join 按名合并，
        组名恒为骨骼 id，而索引不保证）。非数字组名跳过（不是骨骼）。
        占位小三角面对象（ZZMI_STUB，权重挂在组 "0"）跳过——它是不可见标记，
        不是真实几何，不该触发跨组报警。
        """
        used: set[int] = set()
        for drawib_model in self.drawib_model_list:
            if drawib_model.draw_ib != draw_ib:
                continue
            for submesh_model in drawib_model.submesh_model_list:
                for draw_call in submesh_model.drawcall_model_list:
                    try:
                        obj_name = draw_call.get_blender_obj_name()
                    except Exception:
                        continue
                    obj = bpy.data.objects.get(obj_name) if obj_name else None
                    if obj is None or obj.get("ZZMI_STUB"):
                        continue
                    mesh = getattr(obj, "data", None)
                    vertices = getattr(mesh, "vertices", None)
                    groups = getattr(obj, "vertex_groups", None)
                    if vertices is None or groups is None:
                        continue
                    for vertex in vertices:
                        for group_elem in vertex.groups:
                            if group_elem.weight <= 0:
                                continue
                            if group_elem.group >= len(groups):
                                continue
                            name = str(groups[group_elem.group].name)
                            if not name.isdigit():
                                continue
                            used.add(int(name))
        return used

    def _warn_cross_group_bone_references(self):
        """禁止跨组别骨骼合并（无校准模式）守卫：逐部件校验引用骨骼都在本组内。

        无 CB1 校准的运行时，每组骨架只在 deform pass 直拷本组骨骼；
        顶点引用其它组的骨骼 id 时，对应槽位永远不会被写入 = 原点塌陷。
        检出即大声报警（列出越界骨骼 id 与归属组），不中断导出——
        与 _warn_missing_drawib_parts 同款"让用户看见"口径。
        """
        if not self.merged_skeleton_components:
            return
        # 每组合法骨骼 id 集合 = 该组全部导出组件槽位并集（缺席部件的骨骼不会
        # attach，也不可被引用——同组缺席部件被并入现成对象同样会报警）
        group_legal: dict[int, set[int]] = {}
        id_to_group: dict[int, int] = {}
        for component in self.merged_skeleton_components:
            skeleton_group = component["skeleton_group"]
            legal = group_legal.setdefault(skeleton_group, set())
            for bone_id in range(
                component["vg_offset"], component["vg_offset"] + component["vg_count"]
            ):
                legal.add(bone_id)
                id_to_group.setdefault(bone_id, skeleton_group)

        for component in self.merged_skeleton_components:
            draw_ib = component["draw_ib"]
            skeleton_group = component["skeleton_group"]
            legal = group_legal[skeleton_group]
            offending = sorted(
                bone_id
                for bone_id in self._collect_drawib_referenced_bone_ids(draw_ib)
                if bone_id not in legal
            )
            if not offending:
                continue
            offending_groups = sorted(
                {
                    id_to_group.get(bone_id, "未知（不在导出组件范围）")
                    for bone_id in offending
                }
            )
            print(
                f"[ZZMI骨骼合并] !!! 禁止跨组别骨骼合并: DrawIB {draw_ib} "
                f"（骨架组 G{skeleton_group}）的顶点引用了非本组骨骼 id "
                f"{offending}（归属组: {offending_groups}）——无校准模式下这些槽位"
                f"永远不会被写入本组骨架，游戏内将渲染为原点塌陷。"
            )
            print(
                "[ZZMI骨骼合并] 请只把同一骨架组（相同对象空间）的部件合并到同一对象，"
                "或把这些顶点的权重改刷到本组骨骼。"
            )

    def _warn_merged_mesh_timing(self, unredirected: dict | None = None):
        """无法自动重定向的合并网格时序报警（见 _build_merged_mesh_redirect_plan）。

        可自动重定向的合并网格已由导出器挪到组内最后 deform draw（用户无感，
        任意 IB 挂载均正确）；这里只对**无法**重定向的情况大声报警。
        """
        unredirected = unredirected or {}
        if not unredirected:
            return
        by_group: dict[int, list[tuple[str, str, str]]] = {}
        for component in self.merged_skeleton_components:
            info = unredirected.get(component["draw_ib"])
            if info is None:
                continue
            by_group.setdefault(int(component["skeleton_group"]), []).append(
                (component["draw_ib"], info.get("reason", ""), info.get("target", ""))
            )
        for skeleton_group, entries in by_group.items():
            for draw_ib, reason, target_ib in entries:
                if reason == "incompatible-blend-layout":
                    print(
                        f"[ZZMI骨骼合并] !!! 合并网格无法自动重定向: DrawIB {draw_ib}"
                        f"（骨架组 G{skeleton_group}）与目标挂点的 Blend 输入布局不兼容；"
                        "强行重放会按错误的 BLENDINDICES/BLENDWEIGHT 格式读取并导致爆炸。"
                    )
                    print(
                        "[ZZMI骨骼合并] 请让合并网格挂在组内最后一个 deform draw，"
                        "或重新导入并统一参与重放部件的 Blend 布局后再导出。"
                    )
                    continue
                if reason == "missing-blend-layout":
                    print(
                        f"[ZZMI骨骼合并] !!! 合并网格无法自动重定向: DrawIB {draw_ib}"
                        f"（骨架组 G{skeleton_group}）缺少可验证的 Blend 输入布局；"
                        "为避免按错误的 BLENDINDICES/BLENDWEIGHT 格式重放，已停止该重定向。"
                    )
                    print(
                        "[ZZMI骨骼合并] 请重新导入该角色的全部参与部件，"
                        "确保 GameType 包含有效的 Blend 元素或正数 stride 后再导出。"
                    )
                    continue
                print(
                    f"[ZZMI骨骼合并] !!! 合并网格时序无法自动修复: DrawIB {draw_ib}"
                    f"（骨架组 G{skeleton_group}）引用了其它部件的骨骼，但其 deform "
                    f"pass 早于组内最后一个 deform draw"
                    + (
                        "，且反查缓存缺少 DeformDrawIndex（请先重新执行「骨骼合并"
                        "反查」刷新缓存后再导出）。"
                        if reason == "missing-deform-draw"
                        else "，且该部件配置了跨 IB 重定向（暂不与自动重定向兼容）。"
                    )
                )
                if target_ib:
                    print(
                        "[ZZMI骨骼合并] 手动修复：把合并后的物体改名为组内最后一个 "
                        f"deform draw 部件的子网格名（{target_ib} 或带 _copy 后缀）"
                        "后重新导出。"
                    )

    # ------------------------------------------------------------------
    # 合并网格自动重定向（2026-08-25 设计兑现：合并网格可挂在任意 DrawIB）
    # ------------------------------------------------------------------
    #
    # 背景：palette 是 per-pass 独立 Map 上传的 ring scratch（dump 实测：
    # 同一资源 hash 帧内两次 dump 内容不同），早 pass 时刻读不到晚 pass 部件
    # 的当帧骨骼——所以合并网格（引用组内多个部件骨骼）物理上只能在组内
    # **最后一个 deform draw** 蒙皮。为兑现「用户可自由 join 到任意 IB」的
    # 设计承诺，导出侧自动重定向：
    #   - 合并网格挂载的 DrawIB（carrier）的 deform override 退化为 stub draw
    #     （3 顶点，保留 copy palette + attach 写当帧骨骼）；
    #   - 组内最后一个 deform draw 的 DrawIB（target）的 deform override 追加
    #     画合并网格（绑定 carrier 的 vb0/vb2），其 SO 按 [target 完整导出顶点
    #     （含 stub）][merged...] 拼接；
    #   - carrier 的 render override 保留 carrier 自己的 hash/first_index，并显式
    #     绑定 target RedirectSO（base_vertex = target 完整导出顶点数）；
    #   - target/缺失部件始终保留自己的 hash、IB 和极限小三角占位，不用 ib=null
    #     静默跳过，避免不同物体共享 hash 时发生串扰；
    #   - VertexLimitRaise：carrier = 3，target = SO 总大小。
    # 对用户完全透明：任意 IB 挂载都正确，无需改名。

    def _submesh_is_stub(self, submesh_model) -> bool:
        """子网格是否只有占位小三角面对象（无真实几何）。"""
        saw_confirmed_stub = False
        for draw_call in getattr(submesh_model, "drawcall_model_list", []) or []:
            try:
                obj_name = draw_call.get_blender_obj_name()
            except Exception:
                return False
            obj = bpy.data.objects.get(obj_name) if obj_name else None
            if obj is None or not obj.get("ZZMI_STUB"):
                return False
            saw_confirmed_stub = True
        return saw_confirmed_stub

    def _submesh_exported_vertex_count(self, submesh_model) -> int:
        """子网格导出 buffer 顶点数（去重后；与 drawib_model.vertex_count 口径一致）。"""
        index_vertex_id_dict = getattr(submesh_model, "index_vertex_id_dict", None)
        if index_vertex_id_dict:
            try:
                return int(len(index_vertex_id_dict))
            except TypeError:
                pass
        category_buffer_dict = getattr(submesh_model, "category_buffer_dict", None) or {}
        position_buffer = category_buffer_dict.get("Position")
        d3d11_game_type = getattr(submesh_model, "d3d11_game_type", None)
        if position_buffer is None or d3d11_game_type is None:
            return 0
        position_stride = int(
            (getattr(d3d11_game_type, "CategoryStrideDict", {}) or {}).get("Position", 0) or 0
        )
        if position_stride <= 0:
            return 0
        return int(len(position_buffer) / position_stride)

    def _drawib_exported_vertex_count(self, draw_ib: str) -> int:
        """DrawIB 完整导出顶点数之和，包含用于保持 IB 布局的 stub 顶点。"""
        total = 0
        for drawib_model in self.drawib_model_list:
            if drawib_model.draw_ib != draw_ib:
                continue
            for submesh_model in drawib_model.submesh_model_list:
                total += self._submesh_exported_vertex_count(submesh_model)
        return total

    def _drawib_has_real_geometry(self, draw_ib: str) -> bool:
        """判断 DrawIB 是否包含真实几何（而非全部为 ZZMI 占位子网格）。"""
        for drawib_model in self.drawib_model_list:
            if drawib_model.draw_ib != draw_ib:
                continue
            for submesh_model in drawib_model.submesh_model_list:
                if not self._submesh_is_stub(submesh_model):
                    return True
        return False

    def _drawib_blend_layout_signature(self, draw_ib: str):
        """返回用于 deform 重放的 Blend 输入布局签名。

        自动重定向会在另一个 DrawIB 的 IA 状态下执行 draw；Blend 槽的
        stride/元素布局不兼容时，BLENDINDICES 会被按错误格式解释，结果通常
        是流输出全零。优先比较完整元素，测试桩或旧模型则退化为 stride。
        """
        for drawib_model in self.drawib_model_list:
            if drawib_model.draw_ib != draw_ib:
                continue
            game_type = getattr(drawib_model, "d3d11GameType", None)
            elements = getattr(game_type, "D3D11ElementList", None)
            if elements:
                signature = []
                for element in elements:
                    if str(getattr(element, "Category", "") or "") != "Blend":
                        continue
                    semantic_name = str(
                        getattr(element, "SemanticName", "") or ""
                    ).upper()
                    element_format = str(
                        getattr(element, "Format", "") or ""
                    ).upper()
                    # 不同捕获路径可能把同一组 32 位骨骼索引记录成
                    # UINT/SINT；对非负骨骼编号而言二者的位宽和读取步长相同，
                    # 不应因此把本来兼容的 BI16 挂点拆开。
                    if semantic_name == "BLENDINDICES":
                        element_format = element_format.replace("_UINT", "_INT")
                        element_format = element_format.replace("_SINT", "_INT")
                    signature.append(
                        (
                            semantic_name,
                            int(getattr(element, "SemanticIndex", 0) or 0),
                            element_format,
                            int(getattr(element, "ByteWidth", 0) or 0),
                            str(getattr(element, "ExtractSlot", "") or ""),
                        )
                    )
                if signature:
                    return ("elements", tuple(signature))
            stride_dict = getattr(game_type, "CategoryStrideDict", {}) or {}
            try:
                blend_stride = int(stride_dict.get("Blend", 0) or 0)
            except (TypeError, ValueError):
                blend_stride = 0
            if blend_stride > 0:
                return ("stride", blend_stride)
            return None
        return None

    def _drawib_stub_submeshes(self, draw_ib: str) -> list:
        """DrawIB 的 stub 子网格列表（占位对象，无真实几何）。"""
        result = []
        for drawib_model in self.drawib_model_list:
            if drawib_model.draw_ib != draw_ib:
                continue
            for submesh_model in drawib_model.submesh_model_list:
                if self._submesh_is_stub(submesh_model):
                    result.append(submesh_model)
        return result

    def _drawib_first_match_first_index(self, draw_ib: str) -> list[int]:
        """DrawIB 子网格的 match_first_index 列表（升序；重挂 render override 用）。"""
        indices = []
        for drawib_model in self.drawib_model_list:
            if drawib_model.draw_ib != draw_ib:
                continue
            for submesh_model in drawib_model.submesh_model_list:
                try:
                    indices.append(int(submesh_model.match_first_index))
                except (TypeError, ValueError):
                    continue
        return sorted(indices)

    def _drawib_is_cross_ib(self, draw_ib: str) -> bool:
        """DrawIB 是否参与跨 IB 重定向（source 或 target）——暂不与自动重定向兼容。

        cross_ib_info_dict 的键/值是 ib_key（`<draw_ib>_<first_index>`），按前缀匹配。
        """
        prefix = draw_ib + "_"
        if any(str(key).startswith(prefix) for key in (self.cross_ib_info_dict or {})):
            return True
        return any(
            str(target).startswith(prefix)
            for targets in (self.cross_ib_info_dict or {}).values()
            for target in targets
        )

    def _build_merged_mesh_redirect_plan(self):
        """构建合并网格自动重定向计划。

        返回 (carrier_map, target_map, unredirected)：
        - carrier_map: draw_ib -> {"target": 目标 DrawIB,
                                   "base_vertex": 该合并网格在 target SO 中的偏移,
                                   "target_first_index": 重挂 render 用的 match_first_index,
                                   "vertex_count": 合并网格导出顶点数}
        - target_map: draw_ib -> {"deform_draws": [(vb0 资源名, vb2 资源名, 顶点数), ...],
                                  "so_vertex_count": target SO 总大小（含自身 stub）,
                                  "target_own_vertices": target 完整导出顶点数,
                                  "so_owner_ib": 实际持有 SO 的 DrawIB,
                                  "compatible_component_ids": 可安全执行重放的组件 id}
        - unredirected: draw_ib -> {"reason": str, "target": str|""}（无法自动重定向）
        """
        carrier_map: dict[str, dict] = {}
        target_map: dict[str, dict] = {}
        unredirected: dict[str, dict] = {}

        groups: dict[int, list[dict]] = {}
        for component in self.merged_skeleton_components:
            groups.setdefault(int(component["skeleton_group"]), []).append(component)
        component_id_by_draw_ib = {
            component["draw_ib"]: component_id
            for component_id, component in enumerate(self.merged_skeleton_components)
        }

        for skeleton_group, components in groups.items():
            legal: set[int] = set()
            for component in components:
                for bone_id in range(
                    int(component["vg_offset"]),
                    int(component["vg_offset"]) + int(component["vg_count"]),
                ):
                    legal.add(bone_id)

            with_draw = [c for c in components if int(c.get("deform_draw", 0) or 0) > 0]
            if not with_draw:
                for component in components:
                    if (
                        self._collect_drawib_referenced_bone_ids(component["draw_ib"])
                        - set((component.get("vg_map") or {}).values())
                    ) & legal:
                        unredirected[component["draw_ib"]] = {
                            "reason": "missing-deform-draw",
                            "target": "",
                        }
                continue

            last = max(with_draw, key=lambda c: int(c.get("deform_draw", 0) or 0))
            target_ib = last["draw_ib"]
            # target 的 SO 前缀必须与自身导出 VB/IB 使用同一完整顶点布局。
            # stub 的 remapped IB 也引用这 3 个顶点；若将其排除，紧随其后的
            # carrier 顶点会占据相同索引范围，target 占位 draw 将画出真实几何。
            target_own_vertices = self._drawib_exported_vertex_count(target_ib)
            target_has_real_geometry = self._drawib_has_real_geometry(target_ib)
            target_first_indices = self._drawib_first_match_first_index(target_ib)
            target_first_index = target_first_indices[0] if target_first_indices else 0

            carriers: list[dict] = []
            for component in components:
                referenced = self._collect_drawib_referenced_bone_ids(component["draw_ib"])
                own = set((component.get("vg_map") or {}).values())
                absorbed = (referenced - own) & legal
                if not absorbed:
                    continue  # 未合并其它部件
                if int(component.get("deform_draw", 0) or 0) == int(last["deform_draw"]):
                    continue  # 已挂在最后 pass：无需重定向
                if int(component.get("deform_draw", 0) or 0) <= 0:
                    unredirected[component["draw_ib"]] = {
                        "reason": "missing-deform-draw",
                        "target": last.get("unique_str") or "",
                    }
                    continue  # 缺 DeformDrawIndex：无法确定时序
                if self._drawib_is_cross_ib(component["draw_ib"]) or self._drawib_is_cross_ib(target_ib):
                    unredirected[component["draw_ib"]] = {
                        "reason": "cross-ib",
                        "target": last.get("unique_str") or "",
                    }
                    continue  # 跨 IB 重定向与合并网格自动重定向暂不兼容
                # 合并网格的导出顶点数（该 DrawIB 全部子网格——合并场景下通常一个）
                merged_vertices = 0
                for drawib_model in self.drawib_model_list:
                    if drawib_model.draw_ib != component["draw_ib"]:
                        continue
                    for submesh_model in drawib_model.submesh_model_list:
                        merged_vertices += self._submesh_exported_vertex_count(submesh_model)
                carriers.append({
                    "draw_ib": component["draw_ib"],
                    "vertex_count": merged_vertices,
                })

            if not carriers:
                continue

            # 一段 deferred deform draw 只能在同一种已知 Blend 输入布局下执行。
            # 元数据缺失也不能按“兼容”回退，否则换角色或旧工作空间恰好混入
            # R16/R32、BI4/BI16 时，仍会在运行时静默错读权重。
            replay_draw_ibs = [
                target_ib if target_has_real_geometry else None,
                *(carrier["draw_ib"] for carrier in carriers),
            ]
            replay_layouts = {
                self._drawib_blend_layout_signature(draw_ib)
                for draw_ib in replay_draw_ibs
                if draw_ib
            }
            if None in replay_layouts:
                for carrier in carriers:
                    unredirected[carrier["draw_ib"]] = {
                        "reason": "missing-blend-layout",
                        "target": last.get("unique_str") or "",
                    }
                continue
            known_replay_layouts = {layout for layout in replay_layouts if layout is not None}
            if len(known_replay_layouts) > 1:
                for carrier in carriers:
                    unredirected[carrier["draw_ib"]] = {
                        "reason": "incompatible-blend-layout",
                        "target": last.get("unique_str") or "",
                    }
                continue

            # target 的 SO 布局：[target 完整导出顶点（含 stub）][carrier1 merged]...
            base_vertex = target_own_vertices
            deform_draws = []
            # 纯占位 target 只需要保留 SO 前缀，不能把它的 BI4/BI8 等输入布局
            # 带进后续 carrier 的实际重放；carrier 的 3 顶点 stub draw 会占住
            # 前缀，真实 carrier 几何仍从 base_vertex=3 开始，因此无需 target draw。
            if target_has_real_geometry and target_own_vertices > 0:
                deform_draws.append((
                    f"Resource{target_ib}Position",
                    f"Resource{target_ib}Blend",
                    target_own_vertices,
                ))
            so_total = target_own_vertices
            # 合并几何真正依赖哪些当帧 palette：至少包括所有 carrier，另外
            # 把 carrier 顶点实际引用的全局骨骼所属部件也纳入守卫。这样 target
            # 先到时不会读取半成品；最后一个依赖部件到达的 deform 挂点负责 draw。
            slot_owner: dict[int, int] = {}
            for component_id, component in enumerate(self.merged_skeleton_components):
                if int(component["skeleton_group"]) != int(skeleton_group):
                    continue
                for bone_id in range(
                    int(component["vg_offset"]),
                    int(component["vg_offset"]) + int(component["vg_count"]),
                ):
                    slot_owner.setdefault(bone_id, component_id)
            required_component_ids: set[int] = set()
            target_component_id = component_id_by_draw_ib.get(target_ib)
            if target_component_id is not None and target_has_real_geometry:
                # target 的 deform 段负责捕获 ResourceZZRedirectSO_<target>；
                # 没有它就绪，carrier 即使其它 palette 都到齐也不能回放。
                required_component_ids.add(target_component_id)
            for carrier in carriers:
                carrier_component_id = component_id_by_draw_ib.get(carrier["draw_ib"])
                if carrier_component_id is not None:
                    required_component_ids.add(carrier_component_id)
                for bone_id in self._collect_drawib_referenced_bone_ids(carrier["draw_ib"]):
                    owner_id = slot_owner.get(bone_id)
                    if owner_id is not None:
                        required_component_ids.add(owner_id)
                deform_draws.append((
                    f"Resource{carrier['draw_ib']}Position",
                    f"Resource{carrier['draw_ib']}Blend",
                    carrier["vertex_count"],
                ))
                carrier_map[carrier["draw_ib"]] = {
                    "target": target_ib,
                    "base_vertex": base_vertex,
                    "target_first_index": target_first_index,
                    "vertex_count": carrier["vertex_count"],
                }
                base_vertex += carrier["vertex_count"]
                so_total += carrier["vertex_count"]

            # target 只有占位几何时，合并 SO 必须由一个真实 carrier 挂点拥有。
            # 这样 target 晚到也不会把空的/BI4 的 SO 覆盖掉，carrier 自己的
            # BI16（或同类）输入布局可以在任意兼容挂点完成实际流输出。
            so_owner_ib = target_ib
            if not target_has_real_geometry and carriers:
                so_owner_ib = carriers[0]["draw_ib"]
                owner_component_id = component_id_by_draw_ib.get(so_owner_ib)
                if owner_component_id is not None:
                    required_component_ids.add(owner_component_id)

            # 只有 Blend 输入布局兼容的挂点才允许执行整段 deferred draw。
            # 位置/UV 槽可以不同，但 BI4 与 BW16_BI16 不能混用；后者会把
            # float 权重按单索引解释，D3D11 流输出通常直接变成全零。
            deform_draw_ibs = [
                target_ib if target_has_real_geometry else None,
                *(carrier["draw_ib"] for carrier in carriers),
            ]
            deform_draw_ibs = [draw_ib for draw_ib in deform_draw_ibs if draw_ib]
            deform_layouts = {
                self._drawib_blend_layout_signature(draw_ib)
                for draw_ib in deform_draw_ibs
            }
            compatible_component_ids = []
            if deform_layouts and None not in deform_layouts and len(deform_layouts) == 1:
                common_layout = next(iter(deform_layouts))
                for component_id in sorted(required_component_ids):
                    component_draw_ib = self.merged_skeleton_components[component_id]["draw_ib"]
                    if self._drawib_blend_layout_signature(component_draw_ib) == common_layout:
                        compatible_component_ids.append(component_id)
            else:
                # 元数据不完整时保持旧行为，避免历史工作空间因为缺少布局对象
                # 而突然失去自动重定向；真实模型会在上面的完整签名分支收紧。
                compatible_component_ids = sorted(required_component_ids)
            target_map[target_ib] = {
                "target_ib": target_ib,
                "deform_draws": deform_draws,
                "so_vertex_count": so_total,
                "target_own_vertices": target_own_vertices,
                "target_has_real_geometry": target_has_real_geometry,
                "so_owner_ib": so_owner_ib,
                "required_component_ids": sorted(required_component_ids),
                "compatible_component_ids": compatible_component_ids,
                "so_stride": next(
                    (
                        int(
                            drawib_model.d3d11GameType.CategoryStrideDict.get(
                                "Position", 40
                            )
                        )
                        for drawib_model in self.drawib_model_list
                        if drawib_model.draw_ib == target_ib
                    ),
                    40,
                ),
            }
            print(
                f"[ZZMI骨骼合并] 合并网格自动重定向: "
                f"{[c['draw_ib'] for c in carriers]} -> DrawIB {target_ib}"
                f"（组 G{skeleton_group} 最后 deform draw {last['deform_draw']}，"
                f"SO={so_total} 顶点，base_vertex 依次 "
                f"{[carrier_map[c['draw_ib']]['base_vertex'] for c in carriers]}）"
            )

        return carrier_map, target_map, unredirected

    @staticmethod
    def _redirect_texcoord_resource_name(target_ib: str, carrier_ib: str, base_vertex: int) -> str:
        """返回合并网格 carrier 专用的、已按 base_vertex 对齐的 Texcoord 资源名。"""
        return (
            f"ResourceZZRedirectTexcoord_{target_ib}_{carrier_ib}_{int(base_vertex)}"
        )

    @staticmethod
    def _redirect_texcoord_filename(target_ib: str, carrier_ib: str, base_vertex: int) -> str:
        return f"zz_redirect_texcoord_{target_ib}_{carrier_ib}_{int(base_vertex)}.buf"

    def _build_redirect_texcoord_payload(self, carrier_ib: str, carrier_info: dict) -> tuple[bytes, int]:
        """为 carrier 的 vb1 生成与 RedirectSO 相同顶点偏移的缓冲。

        D3D11 的 ``base_vertex`` 会同时作用于所有顶点输入槽。合并重定向只把
        ``vb0`` 换成 target 的 RedirectSO，而 carrier 原本的 vb1 从第 0 行开始，
        因而会在每个索引上错读 ``base_vertex`` 行。这里在 Texcoord 前补齐同样
        数量的空行，使 ``vb1[index + base_vertex]`` 仍命中 carrier 的 UV 行。
        """
        drawib_model = next(
            (
                model
                for model in self.drawib_model_list
                if model.draw_ib == carrier_ib
            ),
            None,
        )
        if drawib_model is None:
            raise RuntimeError(
                f"[ZZMI骨骼合并] 找不到重定向 carrier DrawIB {carrier_ib}，无法生成 Texcoord 对齐缓冲"
            )

        game_type = getattr(drawib_model, "d3d11GameType", None)
        stride = int(
            (getattr(game_type, "CategoryStrideDict", {}) or {}).get("Texcoord", 0)
            or 0
        )
        if stride <= 0:
            # 没有 Texcoord 输入槽时不需要绑定 vb1；调用方会据此跳过资源。
            return b"", 0

        category_buffer = (getattr(drawib_model, "category_buffer_dict", {}) or {}).get(
            "Texcoord"
        )
        if category_buffer is None:
            raise RuntimeError(
                f"[ZZMI骨骼合并] carrier {carrier_ib} 缺少 Texcoord 缓冲，"
                "不能生成与 RedirectSO 对齐的 vb1"
            )
        if hasattr(category_buffer, "tobytes"):
            category_bytes = category_buffer.tobytes()
        else:
            category_bytes = bytes(category_buffer)

        vertex_count = int(carrier_info.get("vertex_count", 0) or 0)
        if vertex_count < 0 or len(category_bytes) != vertex_count * stride:
            raise RuntimeError(
                f"[ZZMI骨骼合并] carrier {carrier_ib} 的 Texcoord 长度不匹配："
                f"实际 {len(category_bytes)} 字节，期望 {vertex_count}*{stride}"
            )

        base_vertex = int(carrier_info.get("base_vertex", 0) or 0)
        if base_vertex < 0:
            raise RuntimeError(
                f"[ZZMI骨骼合并] carrier {carrier_ib} 的 base_vertex 不能为负数: {base_vertex}"
            )
        return (b"\x00" * (base_vertex * stride)) + category_bytes, stride

    def _write_redirect_texcoord_resources(self) -> list[tuple[str, int, str]]:
        """写出所有 carrier 的对齐 Texcoord，并返回 INI 资源定义。"""
        resource_definitions = []
        mod_meshes_dir = os.path.join(GlobalConfig.path_generate_mod_folder(), "Meshes")
        for carrier_ib, carrier_info in sorted((self._redirect_carrier_map or {}).items()):
            payload, stride = self._build_redirect_texcoord_payload(carrier_ib, carrier_info)
            if stride <= 0:
                continue
            target_ib = carrier_info["target"]
            base_vertex = int(carrier_info.get("base_vertex", 0) or 0)
            resource_name = self._redirect_texcoord_resource_name(
                target_ib, carrier_ib, base_vertex
            )
            filename = self._redirect_texcoord_filename(target_ib, carrier_ib, base_vertex)
            self._atomic_write_binary(os.path.join(mod_meshes_dir, filename), payload)
            resource_definitions.append((resource_name, stride, filename))
        return resource_definitions

    def add_merged_skeleton_sections(self, ini_builder: M_IniBuilder):
        """生成 ZZMI 合并骨架段（组内统一骨架版：全局骨骼编号 + 逐 pass attach）。

        架构（2026-08-24 用户拍板分组；2026-08-25 用户拍板**移除 CB1 校准**；
        2026-08-26 增加合并可见 draw 的依赖就绪守卫，详见计划书）：
        - 骨骼 id = 全局编号（组基址拼接组内槽位）；Blender 侧组内 join 无歧义。
        - 每组一套**全宽**合并骨架 `ResourceZZMergedSkeleton_G<N>`（array = 全局
          max(vg_offset+vg_count)）：**只写本组骨骼**（无任何校准乘）。
        - **禁止跨组别骨骼合并**：各组骨架只含本组骨骼；跨组别引用在导出时大声
          报警（`_warn_cross_group_bone_references`，无校准的运行时这些槽位
          永远不会被写入 = 原点塌陷）。
        - **逐 pass attach + 依赖就绪 draw**：deform 段 copy 当帧 palette →
          立即 run attach CS → 换绑本组骨架。自动重定向的合并可见几何不固定
          绑在某一个 target 顺序上，而是在 carrier/target 挂点中等待其依赖的
          palette 全部当帧到达后只 draw 一次；因此目标先到也不会读取半成品。
          [Present] 只清理本帧到达/绘制标记，不重放持久 palette。
        - 未生成组件无需任何延迟机制，继续走游戏原渲染（当帧 palette）。
        """
        section = M_IniSection(M_SectionType.MergedSkeleton)
        constants_section = M_IniSection(M_SectionType.Constants)
        constants_section.SectionName = "Constants"
        groups = self._merged_skeleton_groups()
        for component_id in range(len(self.merged_skeleton_components)):
            constants_section.append(f"global $zz_ms_seen_c{component_id} = 0")
        for target_ib in sorted(self._redirect_target_map):
            constants_section.append(f"global $zz_ms_redirect_drawn_{target_ib} = 0")
        constants_section.new_line()

        # 全宽口径：全局骨骼编号空间的大小 = 全部组件 max(vg_offset+vg_count)
        # （导出子集时 vg_offset 是工作空间全局槽位，可能远超导出内 sum——
        # 同组 3 部件 0~10/11~30/31~50 且中间缺席时 sum=31 但 max=51，按 max 声明）。
        bones_count = max(c["vg_offset"] + c["vg_count"] for c in self.merged_skeleton_components)

        # 每部件 palette 持久副本资源声明（deform VB 段里 copy vs-t0 写入当帧内容）。
        # type=stride 必须显式声明：副本要作为 CS 的 cs-t0（SRV）按
        # StructuredBuffer<ZZBone3x4>（48 字节/骨骼）读取，空声明的 SRV 视图格式
        # 不受控，会读出垃圾矩阵（蒙皮每帧乱跳）。
        for component in self.merged_skeleton_components:
            section.append(f"[ResourceZZPalette_{component['draw_ib']}]")
            section.append("type = Buffer")
            section.append("stride = 48")
            section.append(f"array = {component['vg_count']}")
            section.new_line()

        # 每部件 vg_map 表（局部骨骼 id -> 合并骨架全局槽位）：attach CS 的 cs-t1
        # 按此写槽位——本部件引用的共享 canonical 槽位当帧覆盖，后续 deform 的
        # 部件读到当帧内容（同帧 bitwise 相同，覆盖无害）。
        # **改用 filename 加载二进制文件（2026-08-23 双帧实证）**：多行 data 在
        # 本 3DMigoto fork 上只写入第 0 个元素（G3 仅 slot 0/79/88 非零，其余
        # 线程 vg_map 读到 0 -> 全部骨骼塌进 slot 0，蒙皮炸裂）。filename 与
        # VB 资源同一加载路径，buffer 大小由文件内容决定，与 format 视图精确
        # 匹配。文件格式：每元素 4×uint32（槽位值, 0, 0, 0）= R32G32B32A32_UINT。
        import struct as _struct

        mod_meshes_dir = os.path.join(GlobalConfig.path_generate_mod_folder(), "Meshes")
        for component in self.merged_skeleton_components:
            vg_map = component.get("vg_map") or {}
            section.append(f"[ResourceZZVgMap_{component['draw_ib']}]")
            section.append("type = Buffer")
            section.append("format = R32G32B32A32_UINT")
            vgmap_filename = f"zz_vgmap_{component['draw_ib']}.buf"
            section.append("filename = Meshes/" + vgmap_filename)
            section.new_line()
            payload = b"".join(
                _struct.pack("<4I", int(vg_map[local]), 0, 0, 0)
                for local in range(component["vg_count"])
            )
            self._atomic_write_binary(
                os.path.join(mod_meshes_dir, vgmap_filename),
                payload,
            )

        # 自动重定向 target 的真实 SO 资源引用。target 先到时先捕获 so0；
        # 依赖齐全后可在任意后续组件挂点把合并 draw 回写到同一 target SO。
        for target_ib in sorted(self._redirect_target_map):
            plan = self._redirect_target_map[target_ib]
            section.append(f"[ResourceZZRedirectSO_{target_ib}]")
            section.append("type = Buffer")
            section.append(f"stride = {int(plan.get('so_stride', 40))}")
            section.new_line()

        # RedirectSO 使用 DrawIndexed 的 base_vertex 读取合并 Position；D3D11 会
        # 将这个偏移同时应用到 vb1，因此必须给每个 carrier 的 Texcoord 前面补
        # 同样数量的顶点行。否则位置与 UV 会错位，表现为 UV 整体乱跳/串块。
        redirect_texcoord_resources = self._write_redirect_texcoord_resources()

        # 每组一套合并骨架（组内统一：只直拷本组骨骼，跨组别禁止合并）。
        for skeleton_group in groups:
            section.append(f"[ResourceZZMergedSkeleton_G{skeleton_group}]")
            section.append("type = RWStructuredBuffer")
            section.append("stride = 48")
            section.append("array = " + str(bones_count))
            section.new_line()

        # 逐部件 attach 段（y1 = vg_count；仅由 deform VB 段调用）。
        # Dispatch 按 HLSL numthreads(64,1,1) 动态取整，避免 palette > 512 时
        # 固定 8 组漏掉尾部骨骼。
        for component_id, component in enumerate(self.merged_skeleton_components):
            vg_count = int(component["vg_count"])
            dispatch_count = max(
                1,
                (vg_count + self.MERGED_SKELETON_ATTACH_THREADS - 1)
                // self.MERGED_SKELETON_ATTACH_THREADS,
            )
            section.append(f"[CustomShaderZZMIMergedSkeletonAttach_C{component_id}]")
            section.append("flags = optimization_level3 all_resources_bound skip_validation")
            section.append("cs = ./res/zzmi_merged_skeleton_attach.hlsl")
            section.append("x1 = 0")
            section.append(f"y1 = {vg_count}")
            section.append(f"cs-t0 = ref ResourceZZPalette_{component['draw_ib']}")
            section.append(f"cs-t1 = ref ResourceZZVgMap_{component['draw_ib']}")
            section.append(
                f"cs-u0 = ref ResourceZZMergedSkeleton_G{component['skeleton_group']}"
            )
            section.append(f"Dispatch = {dispatch_count}, 1, 1")
            section.append("cs-u0 = null")
            section.new_line()

        # [Present] 只清理本帧到达/绘制标记，不再重放可能跨帧/跨对象的 palette 副本。
        present_section = M_IniSection(M_SectionType.Present)
        present_section.SectionName = "Present"
        for component_id in range(len(self.merged_skeleton_components)):
            present_section.append(f"$zz_ms_seen_c{component_id} = 0")
        for target_ib in sorted(self._redirect_target_map):
            present_section.append(f"$zz_ms_redirect_drawn_{target_ib} = 0")
        present_section.new_line()

        ini_builder.append_section(section)
        ini_builder.append_section(constants_section)
        ini_builder.append_section(present_section)

        if redirect_texcoord_resources:
            resource_section = M_IniSection(M_SectionType.ResourceBuffer)
            for resource_name, stride, filename in redirect_texcoord_resources:
                resource_section.append(f"[{resource_name}]")
                resource_section.append("type = Buffer")
                resource_section.append(f"stride = {stride}")
                resource_section.append(f"filename = Meshes/{filename}")
                resource_section.new_line()
            ini_builder.append_section(resource_section)

    def _copy_merged_skeleton_shader_to_mod(self):
        """把 attach CS 着色器（组内直拷版）复制到生成 Mod 的 res/ 目录。"""
        addon_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        shader_src = os.path.join(addon_root, "Toolset", "zzmi_merged_skeleton_attach.hlsl")
        if not os.path.isfile(shader_src):
            raise FileNotFoundError(f"未找到 ZZMI 合并骨架 attach CS 着色器: {shader_src}")
        res_dir = os.path.join(GlobalConfig.path_generate_mod_folder(), "res")
        with open(shader_src, "rb") as shader_file:
            shader_payload = shader_file.read()
        self._atomic_write_binary(
            os.path.join(res_dir, "zzmi_merged_skeleton_attach.hlsl"),
            shader_payload,
        )

    def add_unity_vs_resource_vb_sections(self, ini_builder: M_IniBuilder, drawib_model):
        super().add_unity_vs_resource_vb_sections(ini_builder=ini_builder, drawib_model=drawib_model)

        position_stride = drawib_model.d3d11GameType.CategoryStrideDict.get("Position", 40)
        so0_resource_section = M_IniSection(M_SectionType.ResourceBuffer)
        appended_resource_names = set()
        for submesh_model in drawib_model.submesh_model_list:
            source_ib_key = self._get_submesh_ib_key(submesh_model, drawib_model.draw_ib)
            if self.CROSS_IB_METHOD_VB_REF_SO0 not in self._get_source_methods(source_ib_key):
                continue

            resource_name = self._get_source_so0_resource_name(drawib_model.draw_ib, submesh_model.match_first_index)
            if resource_name in appended_resource_names:
                continue
            appended_resource_names.add(resource_name)

            so0_resource_section.append("[" + resource_name + "]")
            so0_resource_section.append("type = Buffer")
            so0_resource_section.append("stride = " + str(position_stride))
            so0_resource_section.new_line()

        ini_builder.append_section(so0_resource_section)

    def add_unity_vs_texture_override_ib_sections(self, ini_builder: M_IniBuilder, drawib_model):
        texture_override_ib_section = M_IniSection(M_SectionType.TextureOverrideIB)
        draw_ib = drawib_model.draw_ib

        print(f"[CrossIB ZZMI] 处理 draw_ib={draw_ib}, has_cross_ib={self.has_cross_ib}")

        texture_override_ib_section.append("[TextureOverride_IB_" + draw_ib + "]")
        texture_override_ib_section.append("hash = " + draw_ib)
        texture_override_ib_section.append("handling = skip")
        texture_override_ib_section.new_line()

        for submesh_model in drawib_model.submesh_model_list:
            texture_override_name_suffix = drawib_model.get_submesh_texture_override_suffix(submesh_model)
            ib_resource_name = drawib_model.get_submesh_ib_resource_name(submesh_model)

            current_ib_key = self._get_submesh_ib_key(submesh_model, draw_ib)
            is_cross_ib_source = current_ib_key in self.cross_ib_info_dict
            is_cross_ib_target = any(current_ib_key in targets for targets in self.cross_ib_info_dict.values())

            print(
                f"[CrossIB ZZMI] submesh={submesh_model.unique_str}, ib_key={current_ib_key}, "
                f"is_source={is_cross_ib_source}, is_target={is_cross_ib_target}"
            )

            source_ib_list_for_target = []
            if is_cross_ib_target:
                for source_ib, target_ib_list in self.cross_ib_info_dict.items():
                    if current_ib_key in target_ib_list:
                        source_ib_list_for_target.append(source_ib)

            source_methods = self._get_source_methods(current_ib_key) if is_cross_ib_source else set()
            if is_cross_ib_source:
                self._append_source_capture_sections(
                    texture_override_ib_section,
                    draw_ib,
                    submesh_model.match_first_index,
                    source_methods,
                )
            elif self.CROSS_IB_METHOD_VB_COPY_CB1 in {
                self._get_mapping_method(source_ib_key, current_ib_key)
                for source_ib_key in source_ib_list_for_target
            }:
                texture_override_ib_section.append(
                    "[" + self._get_target_cb1_temp_resource_name(draw_ib, submesh_model.match_first_index) + "]"
                )

            if is_cross_ib_source:
                self._append_source_capture_override(
                    texture_override_ib_section,
                    texture_override_name_suffix,
                    draw_ib,
                    submesh_model.match_first_index,
                    source_methods,
                )
                texture_override_ib_section.new_line()

            # 合并网格自动重定向：渲染身份必须仍归属于原始 DrawIB/物体。
            #
            # 变形阶段可以把 carrier 的几何写入 target 的 RedirectSO，但这不
            # 等于渲染阶段也要把 carrier 的 TextureOverride 改挂到 target hash。
            # 以前这里复用 target hash + target first_index，会让不同物体落到同
            # 一个运行时匹配键下：纹理、透明、shader replace 和 mesh 备注互相
            # 覆盖；target 的占位段还会用 ib=null 把对应物体整个跳过。
            #
            # 现在每个段始终使用自己的 hash/first_index。carrier 若需读取合并
            # 后的 SO，显式绑定 RedirectSO；target/缺失部件的占位 IB 保持可见
            # （几何尺寸为 1e-6），不再使用 ib=null 作为“跳过”手段。
            redirect_carrier_info = self._redirect_carrier_map.get(draw_ib)
            override_hash = draw_ib
            override_first_index = submesh_model.match_first_index

            texture_override_ib_section.append("[TextureOverride_" + texture_override_name_suffix + "]")
            texture_override_ib_section.append("hash = " + override_hash)
            texture_override_ib_section.append("match_first_index = " + str(override_first_index))

            if redirect_carrier_info is not None:
                # carrier 的索引仍属于 carrier，但顶点来自 target 的合并 SO。
                # 显式换绑 vb0 后，渲染匹配键仍保持 carrier hash，不会与 target
                # 或同 DrawIB 的其它子网格串台。
                texture_override_ib_section.append(
                    "vb0 = ResourceZZRedirectSO_" + redirect_carrier_info["target"]
                )

            ib_buf = drawib_model.submesh_ib_dict.get(submesh_model.unique_str, None)
            if ib_buf is None or len(ib_buf) == 0:
                if self.has_merged_skeleton:
                    raise RuntimeError(
                        f"[ZZMI骨骼合并] 子网格 {submesh_model.unique_str} 的索引缓冲为空；"
                        "合并骨架导出禁止以 ib=null/IB skip 静默跳过，请重新导出以生成"
                        "对应的物体或极限小三角占位"
                    )
                texture_override_ib_section.append("ib = null")
                texture_override_ib_section.new_line()
                continue

            texture_override_ib_section.append("ib = " + ib_resource_name)

            # 合并网格渲染换绑：导出顶点数超过原部件顶点数时（= 本对象把同组
            # 其它部件的几何也合并了进来），渲染 draw 必须把 vb1 换绑为本 mod
            # 的 Texcoord buffer——游戏原 vb1 只覆盖原部件顶点数，合并网格的
            # 索引会越界读（D3D11 OOB 返回 0，UV 全糊到 (0,0) 角落）。
            # 数量不超时保持游戏原绑定（数据同源，零行为变化）。
            if redirect_carrier_info is not None:
                # DrawIndexed 的 base_vertex 会作用于 vb0/vb1 的所有输入槽；
                # 使用导出阶段补齐前缀的 carrier Texcoord，保证与 RedirectSO
                # 中的 Position 行保持同一顶点索引。
                texcoord_stride = int(
                    drawib_model.d3d11GameType.CategoryStrideDict.get("Texcoord", 0)
                    or 0
                )
                if texcoord_stride > 0:
                    base_vertex = int(redirect_carrier_info.get("base_vertex", 0) or 0)
                    texcoord_resource_name = self._redirect_texcoord_resource_name(
                        redirect_carrier_info["target"], draw_ib, base_vertex
                    )
                    texture_override_ib_section.append(f"vb1 = {texcoord_resource_name}")
            elif (
                int(getattr(submesh_model, "vertex_count", 0) or 0)
                > int(getattr(submesh_model, "original_vertex_count", 0) or 0)
                and int(getattr(submesh_model, "original_vertex_count", 0) or 0) > 0
            ):
                texture_override_ib_section.append(f"vb1 = Resource{draw_ib}Texcoord")

            texture_markup_info_list = drawib_model.get_submesh_texture_markup_info_list(submesh_model)
            if not GlobalProterties.forbid_auto_texture_ini() and texture_markup_info_list:
                slot_fix_enabled = GlobalProterties.zzz_use_slot_fix()
                uses_slot_fix = False

                for texture_markup_info in texture_markup_info_list:
                    if not M_IniHelper.is_slot_binding_mark_type(texture_markup_info.mark_type):
                        continue

                    slot_fix_resource_name = self.SLOT_FIX_RESOURCE_NAME_DICT.get(texture_markup_info.mark_name)
                    if slot_fix_enabled and slot_fix_resource_name is not None:
                        texture_override_ib_section.append(
                            slot_fix_resource_name + " = ref " + texture_markup_info.get_resource_name()
                        )
                        uses_slot_fix = True
                    else:
                        texture_override_ib_section.append(
                            texture_markup_info.mark_slot + " = " + texture_markup_info.get_resource_name()
                        )

                if uses_slot_fix:
                    texture_override_ib_section.append(r"run = CommandList\ZZMI\SetTextures")

            if texture_markup_info_list:
                texture_override_ib_section.append("run = CommandListSkinTexture")

            if is_cross_ib_source:
                non_cross_ib_drawcalls = []
                for drawcall_model in submesh_model.drawcall_model_list:
                    obj_name = drawcall_model.obj_name if hasattr(drawcall_model, "obj_name") else str(drawcall_model)
                    if obj_name not in self.cross_ib_object_names:
                        non_cross_ib_drawcalls.append(drawcall_model)

                print(f"[CrossIB ZZMI] 源块绘制非跨IB物体: {len(non_cross_ib_drawcalls)} 个")
                self._append_drawindexed_with_shader_replace(
                    texture_override_ib_section,
                    non_cross_ib_drawcalls,
                    drawib_model.obj_name_draw_offset,
                )
            else:
                print(f"[CrossIB ZZMI] 非源块绘制物体: {len(submesh_model.drawcall_model_list)} 个")
                if redirect_carrier_info is not None:
                    # 合并网格重定向：drawindexed 带 base_vertex——从 target 的 SO
                    # 中读本合并网格的区段（offset 保持本 submesh 的索引偏移）
                    base_vertex = redirect_carrier_info["base_vertex"]
                    self._append_drawindexed_with_shader_replace(
                        texture_override_ib_section,
                        submesh_model.drawcall_model_list,
                        drawib_model.obj_name_draw_offset,
                        base_vertex=base_vertex,
                    )
                else:
                    self._append_drawindexed_with_shader_replace(
                        texture_override_ib_section,
                        submesh_model.drawcall_model_list,
                        drawib_model.obj_name_draw_offset,
                    )

            if is_cross_ib_target and source_ib_list_for_target:
                print(f"[CrossIB ZZMI] 目标块处理: source_ib_list={source_ib_list_for_target}")

                for source_ib_key in source_ib_list_for_target:
                    print(f"[CrossIB ZZMI] 查找源块: ib_key={source_ib_key}")
                    source_drawib_model, source_submesh, source_hash, source_first_index = self._find_source_submesh(
                        source_ib_key
                    )
                    target_method = self._get_mapping_method(source_ib_key, current_ib_key)

                    if source_submesh:
                        source_ib_resource_name = source_drawib_model.get_submesh_ib_resource_name(source_submesh)
                        self._append_target_cross_ib_draw(
                            texture_override_ib_section,
                            target_method,
                            source_hash,
                            source_first_index,
                            source_ib_resource_name,
                            draw_ib,
                            submesh_model.match_first_index,
                        )

                        cross_ib_drawcalls = []
                        for drawcall_model in source_submesh.drawcall_model_list:
                            obj_name = drawcall_model.obj_name if hasattr(drawcall_model, "obj_name") else str(drawcall_model)
                            if obj_name in self.cross_ib_object_names:
                                cross_ib_drawcalls.append(drawcall_model)

                        print(f"[CrossIB ZZMI] 跨IB物体数量: {len(cross_ib_drawcalls)}")
                        if cross_ib_drawcalls:
                            self._append_drawindexed_with_shader_replace(
                                texture_override_ib_section,
                                cross_ib_drawcalls,
                                source_drawib_model.obj_name_draw_offset,
                            )

                        self._append_target_cross_ib_cleanup(
                            texture_override_ib_section,
                            target_method,
                            draw_ib,
                            submesh_model.match_first_index,
                        )
                    else:
                        print(f"[CrossIB ZZMI] 警告: 未找到源块 submesh for {source_ib_key}")

        ini_builder.append_section(texture_override_ib_section)

    def _warn_missing_drawib_parts(self):
        """检测 DrawIB 内缺失对象的部件（物体被合并/删除/改名导致）并大声报警。

        判定：DrawIBModel 元数据里的部件表（match_first_index_partname_dict）与本次导出
        实际拿到对象的子网格（submesh_model_list 的 match_first_index）比对。
        合并骨架模式下缺失部件应已由初始化阶段注入占位；这里仅用于发现
        占位注入之外的异常输入并提示用户，不负责用空 IB 静默隐藏部件。
        返回缺失清单 [{draw_ib, missing:[(first_index, part_name)], present:[...]}]。
        """
        missing_report = []
        for drawib_model in self.drawib_model_list:
            expected = getattr(drawib_model, "match_first_index_partname_dict", {}) or {}
            if not expected:
                continue
            present = set()
            for submesh_model in drawib_model.submesh_model_list:
                try:
                    present.add(int(submesh_model.match_first_index))
                except (TypeError, ValueError):
                    continue
            missing = []
            for first_index, part_name in sorted(expected.items(), key=lambda kv: int(kv[0])):
                if int(first_index) not in present:
                    missing.append((first_index, str(part_name)))
            if missing:
                missing_report.append({
                    "draw_ib": drawib_model.draw_ib,
                    "missing": missing,
                    "present_count": len(present),
                    "expected_count": len(expected),
                })

        for item in missing_report:
            missing_names = [name for _fi, name in item["missing"]]
            print(
                f"[ZZMI导出] !!! 部件缺失警告: DrawIB {item['draw_ib']} 有 "
                f"{item['expected_count']} 个部件，但只找到 {item['present_count']} 个的对象，"
                f"缺失: {missing_names}"
            )
            print(
                "[ZZMI导出] 合并骨架模式会为这些缺失部件注入极限小三角占位；"
                "若仍出现在此处，说明占位注入未生效，导出的 hash/IB 映射可能不完整。"
                "常见原因：对象被删除或改名，或工作区 DrawIB-Component/VGMap 缓存过期。"
            )
        return missing_report

    def export(self):
        try:
            self._export_impl()
        finally:
            self._cleanup_stub_objects()

    def export_buffers_only(self):
        """多轮导出的纯缓冲路径也必须闭合占位对象事务。"""
        try:
            return super().export_buffers_only()
        finally:
            self._cleanup_stub_objects()

    def _export_impl(self):
        TimerUtils.start_stage("缓冲文件生成")
        self.generate_buffer_files(GlobalConfig.path_generatemod_buffer_folder())
        TimerUtils.end_stage("缓冲文件生成")

        if self.has_cross_ib:
            for node_name, cross_ib_method in self.cross_ib_method_dict.items():
                if cross_ib_method and cross_ib_method not in self.SUPPORTED_CROSS_IB_METHODS:
                    print(
                        f"[CrossIB] 错误: 节点 '{node_name}' 使用的跨 IB 方式 '{cross_ib_method}' 不适用于 ZZMI 模式"
                    )
                    print(
                        f"[CrossIB] ZZMI 模式只支持: {sorted(self.SUPPORTED_CROSS_IB_METHODS)}"
                    )
                    self.has_cross_ib = False
                    break

        print(f"[CrossIB ZZMI] export: has_cross_ib={self.has_cross_ib}")

        # ZZMI 骨骼合并：组件信息收集（复选框 + 反查数据双条件；不满足则完全走旧逻辑）
        self.merged_skeleton_components, self.merged_skeleton_component_id_dict = (
            self._collect_merged_skeleton_components()
        )
        self.has_merged_skeleton = len(self.merged_skeleton_components) > 0
        if self.has_merged_skeleton:
            buffer_slots = max(
                c["vg_offset"] + c["vg_count"] for c in self.merged_skeleton_components
            )
            print(
                f"[ZZMI骨骼合并] 合并骨架: {len(self.merged_skeleton_components)} 个部件, "
                f"缓冲 {buffer_slots} 槽（max(vg_offset+vg_count)）"
            )
            # 跨组别引用守卫（无校准模式）：引用其它组骨骼 = 运行时塌陷，大声报警
            self._warn_cross_group_bone_references()
            # 合并网格自动重定向：挂在早 pass 的合并网格自动挪到组内最后一个
            # deform draw 蒙皮/渲染（任意 IB 挂载均正确，用户无感）
            self._redirect_carrier_map, self._redirect_target_map, unredirected = (
                self._build_merged_mesh_redirect_plan()
            )
            # 无法自动重定向的合并网格（缺反查缓存/跨 IB）大声报警
            self._warn_merged_mesh_timing(unredirected)

        # 部件缺失守卫：正常的合并骨架流程已在 ExportZZMI 初始化阶段为缺失部件
        # 注入极限小三角占位，因此这里仅报告仍未能匹配的异常输入；不会再主动
        # 生成 ib=null 来静默跳过对应物体。
        self._warn_missing_drawib_parts()

        TimerUtils.start_stage("INI配置生成")
        ini_builder = M_IniBuilder()
        drawib_drawibmodel_dict = {drawib_model.draw_ib: drawib_model for drawib_model in self.drawib_model_list}

        M_IniHelper.generate_hash_style_texture_ini(ini_builder=ini_builder, drawib_drawibmodel_dict=drawib_drawibmodel_dict)
        M_IniHelper.generate_shared_slot_style_texture_ini(ini_builder=ini_builder, drawib_drawibmodel_dict=drawib_drawibmodel_dict)
        self._integrate_object_swap_ini_hook(ini_builder)
        for drawib_model in self.drawib_model_list:
            self.add_unity_vs_texture_override_vlr_section(ini_builder=ini_builder, drawib_model=drawib_model)
            self.add_unity_vs_texture_override_vb_sections(ini_builder=ini_builder, drawib_model=drawib_model)
            self.add_unity_vs_texture_override_ib_sections(ini_builder=ini_builder, drawib_model=drawib_model)
            self.add_unity_vs_resource_vb_sections(ini_builder=ini_builder, drawib_model=drawib_model)
            self.add_resource_texture_sections(ini_builder=ini_builder, drawib_model=drawib_model)
            M_IniHelper.move_slot_style_textures(draw_ib_model=drawib_model)
            GlobalKeyCountHelper.generated_mod_number = GlobalKeyCountHelper.generated_mod_number + 1

        M_IniHelper.add_branch_key_sections(ini_builder=ini_builder, key_name_mkey_dict=self.blueprint_model.keyname_mkey_dict)
        M_IniHelper.add_shapekey_ini_sections(ini_builder=ini_builder, drawib_drawibmodel_dict=drawib_drawibmodel_dict)
        M_IniHelperGUI.add_branch_mod_gui_section(ini_builder=ini_builder, key_name_mkey_dict=self.blueprint_model.keyname_mkey_dict)

        if self.has_shader_replace:
            M_IniHelper.add_shader_replace_sections(
                ini_builder=ini_builder,
                shader_replace_info_list=self.shader_replace_info_list,
                shader_replace_object_names=self.shader_replace_object_names,
                draw_call_models=self.blueprint_model.ordered_draw_obj_data_model_list,
                mod_export_path=GlobalConfig.path_generate_mod_folder(),
                shader_replace_object_info_map=self.shader_replace_object_info_map,
                draw_call_offset_map=M_IniHelper.build_draw_call_offset_map(self.drawib_model_list),
                draw_call_base_vertex_map=self._build_shader_replace_base_vertex_map(),
            )

        if self.has_merged_skeleton:
            self.add_merged_skeleton_sections(ini_builder)
            self._copy_merged_skeleton_shader_to_mod()

        ini_builder.save_to_file(os.path.join(GlobalConfig.path_generate_mod_folder(), GlobalConfig.get_workspace_name() + ".ini"))
        TimerUtils.end_stage("INI配置生成")


ModModelZZMI = ExportZZMI
