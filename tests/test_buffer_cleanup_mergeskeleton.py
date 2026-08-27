"""buffer_cleanup 后处理节点 × 合并骨架 .buf 依赖回归单测（fake 环境，纯文件 I/O）。

覆盖合并骨架缓冲依赖：
- 合并骨架产出的 .buf（ZZMI zz_vgmap_<draw_ib>.buf zzmi.py:1455、zz_redirect_texcoord_*.buf
  zzmi.py:1528；EFMI Resource_<prefix>_<category>.buf efmi.py:1460/1469）都是通过
  顶层配置 INI 的 `filename = ...` 行引用的，因此 buffer_cleanup 应**保留**它们
  （BENIGN）。
- `_find_unused_buffers` 递归扫描 `**/*.ini` 与 `**/*.buf`，避免嵌套 INI 引用被漏掉。
- 仍无法识别不通过 `filename =` 声明的运行时隐式缓冲；这类文件不应交给本节点管理。
"""

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "_buffer_cleanup_mergeskeleton_test_pkg"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


# 让 node_postprocess_buffer_cleanup 能 import bpy 与 .node_postprocess_base
_install_module("bpy", types=types.SimpleNamespace())  # 模块只用 bpy 命名空间，无实际调用
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.utils"):
    pkg = _install_module(package_name)
    pkg.__path__ = []

# fake 基类：SSMTNode_BufferCleanup 仅继承，不需要 bpy 节点语义
_install_module(
    f"{PKG}.blueprint.node_postprocess_base",
    SSMTNode_PostProcess_Base=object,
)

_spec = importlib.util.spec_from_file_location(
    f"{PKG}.blueprint.node_postprocess_buffer_cleanup",
    REPO_ROOT / "blueprint" / "node_postprocess_buffer_cleanup.py",
)
_bc = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _bc
_spec.loader.exec_module(_bc)

BufferCleanupNode = _bc.SSMTNode_PostProcess_BufferCleanup


class BufferCleanupMergeSkeletonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bufcleanup_")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        self.meshes = Path(self.tmp) / "Meshes"
        self.meshes.mkdir(parents=True, exist_ok=True)
        # 参考文献：https://docs.python.org/3/library/tempfile.html

    def _ini(self, rel_ini, filename_lines):
        p = Path(self.tmp) / rel_ini
        p.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(f"filename = {line}" for line in filename_lines)
        p.write_text(body, encoding="utf-8")
        return p

    def _buf(self, rel):
        p = self.meshes / rel
        p.write_bytes(b"\x00")
        return p

    def _collect(self):
        node = BufferCleanupNode()
        return node._find_unused_buffers(self.tmp)

    def test_merge_vgmap_and_redirect_buf_are_referenced_and_kept(self):
        """H9 benign: zz_vgmap / zz_redirect_texcoord / EFMI 网格 .buf 被顶层 INI
        filename 行引用 -> _find_unused_buffers 返回列表不含它们。"""
        self._ini("mod.ini", [
            "Meshes/zz_vgmap_aaa.buf",
            "Meshes/zz_redirect_texcoord_aaa_bbb_0.buf",
            "Meshes/Resource_aaa_Position.buf",
        ])
        kept = [self._buf("zz_vgmap_aaa.buf"), self._buf("zz_redirect_texcoord_aaa_bbb_0.buf"),
                self._buf("Resource_aaa_Position.buf")]
        also_unreferenced = self._buf("truly_unreferenced.buf")

        unused = self._collect()
        unused_norm = {os.path.normpath(p) for p in unused}
        for k in kept:
            self.assertNotIn(os.path.normpath(str(k)), unused_norm)
        self.assertIn(os.path.normpath(str(also_unreferenced)), unused_norm)

    def test_nested_ini_reference_is_scanned_and_kept(self):
        """递归删除 .buf 时也必须递归扫描 INI，避免嵌套引用遭误删。"""
        self._ini("Meshes/mesh_sub.ini", ["Meshes/zz_vgmap_bbb.buf"])
        nested_buf = self._buf("zz_vgmap_bbb.buf")

        unused = self._collect()
        unused_norm = {os.path.normpath(p) for p in unused}
        self.assertNotIn(os.path.normpath(str(nested_buf)), unused_norm)

    def test_merge_buf_referenced_by_hash_resource_without_filename_line_is_deleted(self):
        """H9 residual: 运行时不走 filename = 行的 .buf（如 ModImpRuntime 类，按 hash
        引用或内联 data =）不会被 filename 正则捕获 -> 被误判为未引用删除。"""
        self._ini("mod.ini", ["Meshes/zz_vgmap_ccc.buf"])  # 只引用一个
        runtime_buf = self._buf("BoneMatrix.buf")  # 无任何 filename 引用

        unused = self._collect()
        unused_norm = {os.path.normpath(p) for p in unused}
        self.assertIn(os.path.normpath(str(runtime_buf)), unused_norm)

    def test_non_buf_files_are_outside_cleanup_scope(self):
        """缓冲区清理节点不得扫描或删除 DDS 贴图。"""
        texture = self.meshes / "texture.dds"
        texture.write_bytes(b"dds")

        unused_norm = {os.path.normpath(p) for p in self._collect()}
        self.assertNotIn(os.path.normpath(str(texture)), unused_norm)


if __name__ == "__main__":
    unittest.main()
