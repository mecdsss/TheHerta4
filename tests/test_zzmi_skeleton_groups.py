"""ZZMI 骨架分组（assign_skeleton_groups / parse_object_transform）合成单测。

分组键 = 渲染 cb1 的对象→世界矩阵（rows 0-3，16 floats）：
- 变换逐位相同的部件同组（共享对象空间，palette/cb1 逐物体 1:1 配对）；
- 无变换数据的部件独立成组（不共享 = 安全方向）；
- 组索引按组内最小 draw_ib 排序分配（导入/导出确定性一致）；
- cb1 解析只接受 ≤512B 逐部件块（>512B 是多对象共享变换数组，排除）。
"""

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "zzmi_groups_test_pkg"


def _install_package(name):
    module = types.ModuleType(name)
    module.__path__ = []
    sys.modules[name] = module
    return module


def _load_module(qualname, path):
    spec = importlib.util.spec_from_file_location(qualname, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


for _name in (PKG, f"{PKG}.common", f"{PKG}.utils"):
    _install_package(_name)
_load_module(f"{PKG}.utils.json_utils", REPO_ROOT / "utils" / "json_utils.py")
_load_module(f"{PKG}.common.efmi_skeleton", REPO_ROOT / "common" / "efmi_skeleton.py")
_zzmi = _load_module(f"{PKG}.common.zzmi_skeleton", REPO_ROOT / "common" / "zzmi_skeleton.py")

ZZMIBoneMapBuilder = _zzmi.ZZMIBoneMapBuilder
assign_skeleton_groups = _zzmi.assign_skeleton_groups

# 两组对象变换（平移不同）
TF_BODY = (
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    -15.22, 1.59, -5.51, 1.0,
)
TF_HEAD = (
    0.047, -0.972, -0.230, 0.0,
    -0.151, -0.234, 0.960, 0.0,
    -0.987, -0.010, -0.158, 0.0,
    -15.21, 2.12, -5.56, 1.0,
)


def _write_cb1(path, transform=None, total_float4=16):
    """写一个 cb1 buf：rows 0-3 = transform（None 时全零），其余填零。"""
    rows = numpy.zeros((total_float4, 4), dtype=numpy.float32)
    if transform is not None:
        rows[0:4] = numpy.array(transform, dtype=numpy.float32).reshape(4, 4)
    rows.tofile(path)


class ParseObjectTransformTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zzmi_tf_")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))

    def test_valid_transform_parsed(self):
        path = os.path.join(self.tmp, "cb1.buf")
        _write_cb1(path, TF_BODY)
        result = ZZMIBoneMapBuilder.parse_object_transform(path)
        expected = tuple(
            float(x) for x in numpy.array(TF_BODY, dtype=numpy.float32)
        )
        self.assertEqual(result, expected)

    def test_oversized_buffer_rejected(self):
        """2048B 多对象共享数组必须排除（rows 0-3 未必是本 draw 的对象）。"""
        path = os.path.join(self.tmp, "cb1_big.buf")
        _write_cb1(path, TF_BODY, total_float4=128)  # 2048B
        self.assertIsNone(ZZMIBoneMapBuilder.parse_object_transform(path))

    def test_non_transform_rejected(self):
        """w 列形态不符（平移行 w != 1）的参数块拒绝。"""
        path = os.path.join(self.tmp, "cb1_bad.buf")
        bad = list(TF_BODY)
        bad[15] = 0.0  # row3.w = 0
        _write_cb1(path, tuple(bad))
        self.assertIsNone(ZZMIBoneMapBuilder.parse_object_transform(path))

    def test_missing_file_returns_none(self):
        self.assertIsNone(
            ZZMIBoneMapBuilder.parse_object_transform(os.path.join(self.tmp, "不存在.buf"))
        )


class AssignSkeletonGroupsTests(unittest.TestCase):
    def test_same_transform_same_group(self):
        groups = assign_skeleton_groups({
            "a23aa8a3": TF_BODY, "b20f90ea": TF_BODY, "b30db54e": TF_BODY,
            "454ff522": TF_HEAD, "48625d6d": TF_HEAD,
            "84618ee0": None,
        })
        self.assertEqual(groups["a23aa8a3"], groups["b20f90ea"])
        self.assertEqual(groups["b20f90ea"], groups["b30db54e"])
        self.assertEqual(groups["454ff522"], groups["48625d6d"])
        self.assertNotEqual(groups["a23aa8a3"], groups["454ff522"])
        # 无变换的 84618ee0 独立成组
        self.assertNotEqual(groups["84618ee0"], groups["a23aa8a3"])
        self.assertNotEqual(groups["84618ee0"], groups["454ff522"])
        self.assertEqual(len(set(groups.values())), 3)

    def test_none_transform_parts_are_singletons(self):
        """两个都无变换的部件各自独立（不共享 = 安全方向），不会合并成一组。"""
        groups = assign_skeleton_groups({"aaaaaaaa": None, "bbbbbbbb": None})
        self.assertNotEqual(groups["aaaaaaaa"], groups["bbbbbbbb"])

    def test_group_indices_deterministic_by_min_drawib(self):
        """组索引按组内最小 draw_ib 排序（与字典遍历顺序无关）。"""
        transforms = {
            "dddddddd": TF_HEAD,
            "aaaaaaaa": TF_BODY,
            "bbbbbbbb": TF_BODY,
            "cccccccc": TF_HEAD,
        }
        groups = assign_skeleton_groups(transforms)
        # 身体组最小 draw_ib = aaaaaaaa -> 组 0；头部组最小 cccccccc -> 组 1
        self.assertEqual(groups["aaaaaaaa"], 0)
        self.assertEqual(groups["bbbbbbbb"], 0)
        self.assertEqual(groups["cccccccc"], 1)
        self.assertEqual(groups["dddddddd"], 1)
        # 乱序输入结果一致
        groups2 = assign_skeleton_groups(dict(reversed(list(transforms.items()))))
        self.assertEqual(groups, groups2)


if __name__ == "__main__":
    unittest.main()
