"""Blender reproduction: 用户场景 —— UV:EFSDF / 顶点色:COLOR,SDFDS / 目标名 COLOR.

运行方式（需 headless Blender）:
    blender --background --factory-startup --python tests/blender_repro_user_scene_vc_uv.py

场景（与用户报告一致）:
    - 单个 UV 层 "EFSDF"（活动 UV）
    - 两个顶点色 "COLOR" 与 "SDFDS"
    - 设置顶点色工具: 名称 COLOR, FULL_COLOR 模式

设计要求: 删除非设定名的顶点色 SDFDS，保留/重建 COLOR，绝不触碰 UV。
Bug 现象（用户报告）: 活动 UV "EFSDF" 被删除。

本脚本验证三条路径:
  A) 当前源码（名称快照 + 逐个重取包装器）→ UV 保留、只剩 COLOR          == 期望 PASS
  B) 旧实现（list() 捕获包装器后逐个 remove）→ UV 被误删                == 期望 FAIL(复现 bug)
  C) 混合场景: 多个 UV + 多个顶点色 + 目标名已存在 → UV 全部保留           == 期望 PASS
"""
import importlib.util
import inspect
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import bpy
import numpy as np

PKG = "_blender_repro_user_vc_pkg"
for name in list(sys.modules):
    if name.startswith(PKG):
        del sys.modules[name]
for name in (PKG, f"{PKG}.toolkit", f"{PKG}.utils"):
    sys.modules[name] = types.ModuleType(name)

import utils.color_attribute_utils as color_attr_utils
import utils.vertex_color_utils as vertex_color_utils
sys.modules[f"{PKG}.utils.color_attribute_utils"] = color_attr_utils
sys.modules[f"{PKG}.utils.vertex_color_utils"] = vertex_color_utils

spec = importlib.util.spec_from_file_location(
    f"{PKG}.toolkit.bmtp_mesh_tools", str(REPO_ROOT / "toolkit" / "bmtp_mesh_tools.py")
)
bmtp = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bmtp
spec.loader.exec_module(bmtp)

# 与 toolkit/bmtp_mesh_tools.py 中逐字一致的当前移除循环（还原场景 B 用）。
CURRENT_REMOVAL = """    if vc_mode == 'FULL_COLOR':
        # 设计要求：FULL_COLOR 清空原有所有顶点色，只保留本次指定的颜色属性。
        # 注意：Blender 在移除一个自定义数据层后会移动其余层，之前通过
        # list()/遍历拿到的旧 Attribute 包装器会变成悬垂指针，直接移除会误删
        # UV 等其他自定义数据层。因此必须按名称逐个重新获取后再移除，并且
        # 先无条件清空 active_color（对象相等比较对 RNA 包装器不可靠）。
        mesh.color_attributes.active_color = None
        for old_name in [a.name for a in mesh.color_attributes]:
            old_attr = mesh.color_attributes.get(old_name)
            if old_attr is not None:
                mesh.color_attributes.remove(old_attr)
"""

OLD_REMOVAL = """    if vc_mode == 'FULL_COLOR':
        # 设计要求：FULL_COLOR 清空原有所有顶点色，只保留本次指定的颜色属性。
        for old_attr in list(mesh.color_attributes):
            if mesh.color_attributes.active_color == old_attr:
                mesh.color_attributes.active_color = None
            mesh.color_attributes.remove(old_attr)
"""


def build_user_scene(tag, extra_uv=False):
    """用户场景: UV EFSDF(活动) + 顶点色 COLOR, SDFDS。"""
    name = f"VCUSER_{tag}"
    if name in bpy.data.meshes:
        bpy.data.meshes.remove(bpy.data.meshes[name])
    mesh = bpy.data.meshes.new(name)
    obj = bpy.data.objects.new(name + "_obj", mesh)
    bpy.context.collection.objects.link(obj)
    verts = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
    mesh.from_pydata(verts, [], [(0, 1, 2, 3)])
    mesh.update()

    u1 = mesh.uv_layers.new(name="EFSDF")
    u1.active = True
    for i, loop in enumerate(mesh.loops):
        u1.data[i].uv = (0.25 * i, 0.5)
    if extra_uv:
        u2 = mesh.uv_layers.new(name="UVMap2")
        for i, loop in enumerate(mesh.loops):
            u2.data[i].uv = (0.1, 0.2)

    c1 = mesh.color_attributes.new(name="COLOR", type="BYTE_COLOR", domain="CORNER")
    c2 = mesh.color_attributes.new(name="SDFDS", type="BYTE_COLOR", domain="CORNER")
    mesh.color_attributes.active_color = c2
    return mesh


def snap(mesh):
    return [u.name for u in mesh.uv_layers], [c.name for c in mesh.color_attributes]


def run_case(tag, revert_to_old, extra_uv=False):
    mesh = build_user_scene(tag, extra_uv=extra_uv)
    uv_before, col_before = snap(mesh)
    kwargs = dict(
        mesh=mesh,
        vc_mode="FULL_COLOR",
        attr_name="COLOR",
        attr_domain="CORNER",
        attr_data_type="BYTE_COLOR",
        color_rgba_srgb=np.asarray([1.0, 0.0, 0.0, 1.0], dtype=np.float32),
        selected_verts=None,
        selected_faces=None,
    )
    if revert_to_old:
        src = inspect.getsource(bmtp._apply_vertex_color_object_mode)
        assert CURRENT_REMOVAL in src, "当前源码与预期修复块不一致，无法还原旧实现"
        patched = src.replace(CURRENT_REMOVAL, OLD_REMOVAL)
        exec(compile(patched, "<reverted_apply_vertex_color>", "exec"), bmtp.__dict__)
    bmtp._apply_vertex_color_object_mode(**kwargs)
    uv_after, col_after = snap(mesh)
    uv_ok = uv_after == uv_before
    col_ok = col_after == ["COLOR"]
    ok = uv_ok and col_ok
    print(
        f"[{tag}] uv_before={uv_before} uv_after={uv_after} "
        f"colors={col_after} => {'PASS' if ok else 'FAIL'}"
        f"{' (UV丢失!)' if not uv_ok else ''}",
        flush=True,
    )
    return ok


results = {}
results["A_current_user_scene"] = run_case("A_current", revert_to_old=False)
results["B_old_loop_user_scene"] = run_case("B_old", revert_to_old=True)  # 期望 FAIL（复现 bug）
results["C_current_multi_uv"] = run_case("C_current_multi", revert_to_old=False, extra_uv=True)

summary = " ".join(f"{k}={'PASS' if v else 'FAIL'}" for k, v in results.items())
print(f"SUMMARY {summary}", flush=True)
ok = results["A_current_user_scene"] and results["C_current_multi_uv"] and not results["B_old_loop_user_scene"]
sys.exit(0 if ok else 1)