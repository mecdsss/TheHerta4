"""EFMI 骨骼合并 × DataType 节点交互：BLENDINDICES 升宽的回归单测（fake 环境）。

覆盖 t3 假设：
- H1 (P0 crash): DataType 节点覆盖的 Blend category D3D11ElementList 若不含
  BLENDINDICES（SemanticName 变了 / 被 TEXCOORD 替换），则 get_blendindices_layouts()
  返回空，EFMI 合并骨架的 _validated_blendindices_layouts（efmi.py:862-885）会 raise
  RuntimeError("... 不含 BLENDINDICES 布局")。此处把"空布局"前提断言出来。
- H2 (P0 crash): DataType 覆盖配置把 BLENDINDICES Format 设成不支持的格式（非
  R8*/R16*/R32*），GPU_PreSkinning 子网格触发 widen_blendindices → ValueError
  （d3d11_gametype.py:162）。
- BENIGN（原先恐惧的"覆盖把升宽后的 R16 打回 R8 → 撕 mesh"）：ORDER 是 override
  先（submesh_metadata.py:186 构建 game_type）→ widen 后（submesh_model.py:123），
  所以 R8 覆盖会被重新升宽到 R16；ElementFormat 行（efmi.py:1056）读到最终布局；
  残留 R8 会命中窄布局守卫（submesh_model.py:134-138）。本组断言没有撕 mesh。
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "_efmi_blendindices_datatype_test_pkg"


def _install_package(name):
    module = types.ModuleType(name)
    module.__path__ = []
    sys.modules[name] = module
    return module


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_real_module(qualname, relpath):
    return _load_module(qualname, REPO_ROOT / relpath)


for package_name in (PKG, f"{PKG}.common", f"{PKG}.utils"):
    _install_package(package_name)

# format_utils / d3d11_element 与既有 test_d3d11_gametype_blendindices 一致。
sys.modules[f"{PKG}.utils.format_utils"] = types.SimpleNamespace(
    FormatUtils=types.SimpleNamespace()
)
_load_real_module(f"{PKG}.common.d3d11_element", "common/d3d11_element.py")
_gametype_module = _load_real_module(
    f"{PKG}.common.d3d11_gametype", "common/d3d11_gametype.py"
)
D3D11GameType = _gametype_module.D3D11GameType


def _element(semantic, semantic_index, fmt, width, category="Blend", slot="vb2"):
    return {
        "SemanticName": semantic,
        "SemanticIndex": semantic_index,
        "Format": fmt,
        "ByteWidth": width,
        "ExtractSlot": slot,
        "ExtractTechnique": "trianglelist",
        "Category": category,
    }


# ---------------------------------------------------------------------------
# DataType 覆盖块的"结果"：build_override_element_list(original, override, draw_ib)
# 会把匹配 category 的 D3D11ElementList 整体替换，其余 category 保留原始。
# 下面的 override_category_buffers 就是直接喂给 from_submesh_json_dict 的
# override_d3d11_element_list 的等价物（真实 export 路径：
# submesh_metadata._build_d3d11_game_type -> submesh_metadata.py:186-197）。
# ---------------------------------------------------------------------------
def _apply_category_override(original_category_buffers, override_category_buffers):
    """复刻 build_override_element_list 的替换语义（node_datatype.py:81-131）。"""
    override_dict = {}
    for buffer_info in override_category_buffers:
        category = buffer_info.get("_category")
        override_dict[category] = buffer_info.get("D3D11ElementList", [])

    result = []
    for buffer_info in original_category_buffers:
        category = buffer_info.get("_category")
        if category in override_dict:
            result.extend([dict(e) for e in override_dict[category]])
        else:
            result.extend([dict(e) for e in buffer_info.get("D3D11ElementList", [])])
    return result


def _make_game_type(original_categories, override_categories=None):
    override_list = (
        _apply_category_override(original_categories, override_categories)
        if override_categories is not None
        else None
    )
    return D3D11GameType.from_submesh_json_dict(
        submesh_json_dict={
            "WorkGameType": "TEST",
            "GPU-PreSkinning": True,
            "CategoryBufferList": [
                {"FileName": "x-Blend.buf", **c} for c in original_categories
            ],
        },
        override_d3d11_element_list=override_list,
    )


class EFMIBlendIndicesDataTypeTests(unittest.TestCase):
    def test_h2_non_widenable_format_raises_value_error(self):
        """H2: DataType 覆盖把 BLENDINDICES 设成非 R8*/R16*/R32* 系格式 ->
        widen 抛 ValueError（d3d11_gametype.py:162）。注意 R32 系会被 startswith("R32")
        跳过（视为已宽），真正触发 ValueError 的是既非 R8/R16/R32 又不在 widen_map 的格式。"""
        game_type = _make_game_type(
            original_categories=[{"_category": "Blend", "D3D11ElementList": []}],
            override_categories=[
                {
                    "_category": "Blend",
                    "D3D11ElementList": [
                        _element("BLENDINDICES", 0, "R10G10B10A2_UINT", 4),
                    ],
                }
            ],
        )
        with self.assertRaisesRegex(
            ValueError, "不支持升宽的 BLENDINDICES 格式"
        ):
            game_type.widen_blendindices()

    def test_h1_override_without_blendindices_yields_empty_layout(self):
        """H1: DataType 覆盖把 Blend 的 D3D11ElementList 换成不含 BLENDINDICES 的
        （此处换成 TEXCOORD），则 get_blendindices_layouts() == [] —— 这正是 EFMI
        合并骨架 _validated_blendindices_layouts 触发 RuntimeError 的前置条件。"""
        game_type = _make_game_type(
            original_categories=[{"_category": "Blend", "D3D11ElementList": []}],
            override_categories=[
                {
                    "_category": "Blend",
                    "D3D11ElementList": [
                        _element("TEXCOORD", 0, "R16G16_FLOAT", 4, category="Blend"),
                    ],
                }
            ],
        )
        # BLENDINDICES 缺失 -> widen 不改（返回 False），布局为空
        self.assertFalse(game_type.widen_blendindices())
        self.assertEqual(game_type.get_blendindices_layouts(), [])

        # 复刻 EFMI 合并骨架对空布局的守卫（efmi.py:862-885）：
        # _validated_blendindices_layouts 对每个子网格 assert 非空，否则 raise。
        def validated_layouts(game_type):
            layouts = tuple(game_type.get_blendindices_layouts())
            if not layouts:
                raise RuntimeError("不含 BLENDINDICES 布局")
            return layouts or ()

        with self.assertRaisesRegex(RuntimeError, "不含 BLENDINDICES 布局"):
            validated_layouts(game_type)

    def test_override_r8_blendindices_is_rewidened_no_tear(self):
        """BENIGN: 覆盖提供 R8G8B8A8_UINT 的 BLENDINDICES，升宽后应为 R16G16B16A16_UINT
        （ORDER: override 先于 widen，打回 R8 会被重新升宽，不构成撕 mesh）。"""
        game_type = _make_game_type(
            original_categories=[{"_category": "Blend", "D3D11ElementList": []}],
            override_categories=[
                {
                    "_category": "Blend",
                    "D3D11ElementList": [
                        _element("BLENDINDICES", 0, "R8G8B8A8_UINT", 4),
                    ],
                }
            ],
        )
        self.assertTrue(game_type.widen_blendindices())
        layouts = game_type.get_blendindices_layouts()
        self.assertEqual(layouts, [(0, "R16G16B16A16_UINT", "vb2")])
        # 无残留 R8 窄布局：合并骨架不会因为"升宽未完成"而 raise（submesh_model.py:134-138）
        narrow = [
            (idx, fmt)
            for idx, fmt, _slot in game_type.get_blendindices_layouts()
            if fmt.startswith("R8")
        ]
        self.assertEqual(narrow, [])

    def test_override_removes_blendindices_semantic_index_zero(self):
        """H1 变体：覆盖把 BLENDINDICES 的 SemanticIndex 改掉（如 0->1），而原 layout
        依赖 index 0；覆盖后只剩 index 1 -> 布局语义错位，合并骨架的 ElementFormat 会
        打在错误的 semantic index 上（efmi.py:1056）。"""
        game_type = _make_game_type(
            original_categories=[
                {
                    "_category": "Blend",
                    "D3D11ElementList": [
                        _element("BLENDINDICES", 0, "R8G8B8A8_UINT", 4),
                    ],
                }
            ],
            override_categories=[
                {
                    "_category": "Blend",
                    "D3D11ElementList": [
                        _element("BLENDINDICES", 1, "R8G8B8A8_UINT", 4),
                    ],
                }
            ],
        )
        game_type.widen_blendindices()
        layouts = game_type.get_blendindices_layouts()
        # 覆盖后 index 0 的 BLENDINDICES 不见了，只剩 index 1（布局被"指名"替换）。
        self.assertNotIn(0, [idx for idx, _f, _s in layouts])
        self.assertIn(1, [idx for idx, _f, _s in layouts])


if __name__ == "__main__":
    unittest.main()
