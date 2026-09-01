# -*- coding: utf-8 -*-
"""t22 补测：elif widen 门控死代码修复（B1 真落地）。

规格依据：t21-abort-fix-acceptance.md §2 B1/B1b + t20 行为变更 B1。
被测实现：
1. D3D11GameType.widen_blendindices（common/d3d11_gametype.py:132）——R8→R16 升宽、幂等；
2. submesh_model.py calc_buffer elif 分支（非合并 EFMI 双套导出）**无条件调用**
   widen（t22 移除 import_merged_vgmap 门控）—— 源码级回归断言钉死。

测试纪律：无 bpy（D3D11GameType 纯 Python 加载；源码断言只读文件）；不动既有断言语义。
"""
import json
import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# 模块装载：fake PKG stub（与既有 tests 同构）
# ---------------------------------------------------------------------------
PKG = "dualset_widen_test_pkg"
for _name in (PKG, f"{PKG}.common", f"{PKG}.utils"):
    _m = types.ModuleType(_name)
    _m.__path__ = []
    sys.modules[_name] = _m


def _load_real(qualname, relpath):
    spec = __import__("importlib.util").util.spec_from_file_location(
        qualname, REPO_ROOT / relpath
    )
    module = __import__("importlib.util").util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


_load_real(f"{PKG}.utils.tbn_codec", "utils/tbn_codec.py")
_load_real(f"{PKG}.utils.format_utils", "utils/format_utils.py")
_d3d11_element = _load_real(f"{PKG}.common.d3d11_element", "common/d3d11_element.py")
_d3d11_gametype = _load_real(f"{PKG}.common.d3d11_gametype", "common/d3d11_gametype.py")
D3D11GameType = _d3d11_gametype.D3D11GameType


def _synthetic_gametype(blend_bi_format: str, byte_width: int, gpu_preskinning: bool = True):
    """合成 GPU_PreSkinning gametype（子网格 json 格式，经 CategoryBufferList）。"""
    payload = {
        "GPU-PreSkinning": gpu_preskinning,
        "WorkGameType": "GPU_TEST",
        "CategoryDrawCategoryMap": {"Blend": "Blend"},
        "CategoryBufferList": [
            {
                "FileName": "synthetic-Blend.buf",
                "Type": "Normal",
                "D3D11ElementList": [
                    {"SemanticName": "BLENDINDICES", "SemanticIndex": 0,
                     "Format": blend_bi_format, "ByteWidth": byte_width,
                     "ExtractSlot": "vb0", "ExtractTechnique": "trianglelist",
                     "Category": "Blend", "DrawCategory": "Blend"},
                ],
            },
        ],
    }
    return D3D11GameType.from_submesh_json_dict(payload)


class WidenBlendindicesTests(unittest.TestCase):
    """widen_blendindices 幂等与 R8→R16 升宽（B1 测例核心）。"""

    def test_b1_r8_widened_to_r16(self):
        """B1：R8G8B8A8_UINT（身份>255 场景）→ widen 后 R16G16B16A16_UINT、8B。"""
        gt = _synthetic_gametype("R8G8B8A8_UINT", 4)
        changed = gt.widen_blendindices()
        self.assertTrue(changed, "R8 必须发生升宽")
        elem = [e for e in gt.D3D11ElementList
                if str(e.SemanticName).upper() == "BLENDINDICES"][0]
        self.assertEqual(elem.Format, "R16G16B16A16_UINT")
        self.assertEqual(elem.ByteWidth, 8)
        self.assertEqual(gt.CategoryStrideDict.get("Blend", 0), 8)

    def test_b1_widen_idempotent(self):
        """widen 幂等：二次调用返回 False（不重复修改）。"""
        gt = _synthetic_gametype("R8G8B8A8_UINT", 4)
        first = gt.widen_blendindices()
        second = gt.widen_blendindices()
        self.assertTrue(first)
        self.assertFalse(second, "已 R16 时二次 widen 必须返回 False（幂等）")

    def test_b1b_r16_kept_unchanged(self):
        """B1b：原生 R16（身份≤255 场景）→ widen 返回 False、格式不变。"""
        gt = _synthetic_gametype("R16G16B16A16_UINT", 8)
        changed = gt.widen_blendindices()
        self.assertFalse(changed, "R16 不应被修改（恒等保持）")
        elem = [e for e in gt.D3D11ElementList
                if str(e.SemanticName).upper() == "BLENDINDICES"][0]
        self.assertEqual(elem.Format, "R16G16B16A16_UINT")

    def test_b1_r32_uint_downcast_to_r16(self):
        """R32_UINT 降宽到 R16（合并骨架同 LOD 布局统一语义）。"""
        gt = _synthetic_gametype("R32_UINT", 4)
        changed = gt.widen_blendindices()
        self.assertTrue(changed)
        elem = [e for e in gt.D3D11ElementList
                if str(e.SemanticName).upper() == "BLENDINDICES"][0]
        self.assertEqual(elem.Format, "R16_UINT")


class ElifWidenGateRegressionTests(unittest.TestCase):
    """源码级回归：elif 分支 widen 无复选框门控（B1 真落地钉死）。"""

    def _elif_region(self) -> str:
        src = (REPO_ROOT / "common" / "submesh_model.py").read_text(encoding="utf-8")
        # 定位 elif dualset_eligible 分支（直到 obj_buffer_result 之前）
        m = re.search(
            r"elif dualset_eligible:.*?(?=\n        obj_buffer_result =)",
            src, re.DOTALL,
        )
        self.assertIsNotNone(m, "找不到 elif dualset_eligible 分支")
        return m.group(0)

    def test_elif_widen_unconditional(self):
        """elif 分支含 widen_blendindices() 调用（无条件）。"""
        region = self._elif_region()
        self.assertIn("widen_blendindices()", region,
                      "elif 必须无条件调用 widen_blendindices")

    def test_elif_widen_no_checkbox_gate(self):
        """门控回归：elif 分支代码不得再读 import_merged_vgmap 复选框。

        检查剥离注释后的可执行代码（注释里的解释性文字不算门控）。
        """
        region = self._elif_region()
        # 剥离行内注释（# 起至行尾）；保留多行字符串内的不匹配
        code_lines = []
        for line in region.splitlines():
            code_lines.append(line.split("#", 1)[0])
        code = "\n".join(code_lines)
        self.assertNotIn(
            "import_merged_vgmap", code,
            "elif widen 门控必须移除复选框依赖（t21 B1 死代码修复）",
        )
        self.assertIn("widen_blendindices()", code, "elif 必须无条件调用 widen")

    def test_elif_prepare_after_widen(self):
        """顺序：widen 在 _prepare_merged_skeleton_vertex_groups 之前。"""
        region = self._elif_region()
        widen_pos = region.index("widen_blendindices()")
        prepare_pos = region.index("_prepare_merged_skeleton_vertex_groups")
        self.assertLess(widen_pos, prepare_pos, "widen 必须先于预处理链")


if __name__ == "__main__":
    unittest.main(verbosity=2)