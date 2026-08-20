"""Blender regression test: 设置顶点色 FULL_COLOR 移除旧顶点色不得破坏 UV 层.

运行方式（需本机装有 headless 可用的 Blender）:
    blender --background --factory-startup --python tests/blender_regression_vc_uv_loss.py

背景:
    Blender 的 color_attributes 集合在移除一个属性后会把其余属性在内部存储中前移，
    通过 list()/遍历提前捕获的 Attribute 包装器会变成悬垂指针。旧实现
    `for old_attr in list(mesh.color_attributes): ... remove(old_attr)` 的第二次
    移除会用悬垂包装器，实际删除的是错误的数据层 —— 表现为 UV 层被一起删掉
    （Blender 5.0.1 实测：2 个顶点色 + 2 个 UV 时，跑完后 uv_layers 只剩 1 个）。
    Blender 4.x 的包装器指向 CustomDataLayer*（同样会被 memmove 移动），属同类问题。

    修复：改为快照『名字』，每次移除前用 color_attributes.get(name) 重新取包装器；
    并且先无条件清空 active_color（RNA 包装器的 == 比较不可靠，实测对 .new() 返回
    的包装器比较恒为 False，旧守卫形同虚设）。

本测试断言两条：
  A) 当前源码（已修复）→ UV 层全部保留，只剩目标顶点色          == PASS
  B) 用 exec 把移除循环还原成旧实现 → UV 层丢失（守护该测试本身）== FAIL(预期)
  退出码 0 当且仅当 A 通过且 B 复现失败。
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

PKG = "_blender_regression_vc_pkg"
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

# 当前修复后的移除循环（与 toolkit/bmtp_mesh_tools.py 中逐字一致，还原场景 B 用）。
OLD_REMOVAL = """    if vc_mode == 'FULL_COLOR':
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

VULNERABLE_REMOVAL = """    if vc_mode == 'FULL_COLOR':
        # 设计要求：FULL_COLOR 清空原有所有顶点色，只保留本次指定的颜色属性。
        for old_attr in list(mesh.color_attributes):
            if mesh.color_attributes.active_color == old_attr:
                mesh.color_attributes.active_color = None
            mesh.color_attributes.remove(old_attr)
"""


def build_mesh(tag):
    name = f"VCREG_{tag}"
    if name in bpy.data.meshes:
        bpy.data.meshes.remove(bpy.data.meshes[name])
    mesh = bpy.data.meshes.new(name)
    obj = bpy.data.objects.new(name + "_obj", mesh)
    bpy.context.collection.objects.link(obj)
    verts = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
    mesh.from_pydata(verts, [], [(0, 1, 2, 3)])
    mesh.update()
    u1 = mesh.uv_layers.new(name="UVMap")
    u2 = mesh.uv_layers.new(name="UV2")
    for i, loop in enumerate(mesh.loops):
        u1.data[i].uv = (0.25 * i, 0.5)
        u2.data[i].uv = (0.1, 0.2)
    mesh.color_attributes.new(name="OldA", type="BYTE_COLOR", domain="CORNER")
    mesh.color_attributes.new(name="OldB", type="BYTE_COLOR", domain="CORNER")
    return mesh


def snap(mesh):
    return [u.name for u in mesh.uv_layers], [c.name for c in mesh.color_attributes]


def run_case(tag, revert_to_vulnerable):
    mesh = build_mesh(tag)
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
    if revert_to_vulnerable:
        src = inspect.getsource(bmtp._apply_vertex_color_object_mode)
        assert OLD_REMOVAL in src, "当前源码与预期修复块不一致，无法还原旧实现"
        patched = src.replace(OLD_REMOVAL, VULNERABLE_REMOVAL)
        exec(compile(patched, "<reverted_apply_vertex_color>", "exec"), bmtp.__dict__)
    bmtp._apply_vertex_color_object_mode(**kwargs)
    uv_names, col_names = snap(mesh)
    ok = uv_names == ["UVMap", "UV2"] and col_names == ["COLOR"]
    print(
        f"[{tag}] uv={uv_names} colors={col_names} => {'PASS' if ok else 'FAIL'}",
        flush=True,
    )
    return ok


fixed_ok = run_case("fixed_source", revert_to_vulnerable=False)
vuln_fail = run_case("reverted_old_loop", revert_to_vulnerable=True)  # 期望 FAIL（复现 bug）

print(
    f"SUMMARY fixed_source={'PASS' if fixed_ok else 'FAIL'} "
    f"reverted_old_loop={'PASS' if vuln_fail else 'FAIL(expect)'}",
    flush=True,
)
sys.exit(0 if (fixed_ok and not vuln_fail) else 1)