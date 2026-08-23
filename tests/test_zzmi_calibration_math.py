"""ZZMI 校准版 attach 的校准数学验证（numpy 镜像 HLSL 公式）。

校准公式（Toolset/zzmi_merged_skeleton_attach_calibrated.hlsl 同款）：
  palette（列向量约定）：object = Rm @ bind + tm；骨骼 12 floats = 3 行 (旋转行, 平移分量)
  cb1（行向量约定，实测）：world = object @ R + t；R = rows 0-2 xyz，t = row 3 xyz
  校准：M' = C × M，C = U_dst^-1 × U_src（U = (R^T, t) 列向量 4x4，刚体逆 R^-1 = R^T）
    C_rot = Rd @ Rs.T；C_t = Rd @ (t_src - t_dst)
    Rm' = C_rot @ Rm；tm' = C_rot @ tm + C_t
  世界不变性：U_dst × (M' @ p) ≡ U_src × (M @ p)。

真实数据用例（FrameAnalysis-2026-08-19-122152）：
- 身体组 cb1 = de8e7949，头部组 cb1 = 7d18e062，palette = e018278f（b20f90ea）
- 同组校准 = 恒等（直拷等价）
- 跨组校准的世界不变性
- cb1 形态校验（row3.w != 1 的块拒绝校准）
"""

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path

import numpy

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "zzmi_calib_test_pkg"


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

DUMP = r"K:\SSMT-Package-master\3Dmigoto\ZZZ\FrameAnalysis-2026-08-19-122152\deduped"
HAVE_DUMP = os.path.isdir(DUMP)


# ------------------------------------------------------------------
# HLSL 公式的 numpy 镜像（与 calibrated hlsl 逐行对应）
# ------------------------------------------------------------------

def load_bone_3x4(bone12):
    """12 floats -> (Rm 3x3, tm 3)（列向量约定：object = Rm @ bind + tm）。"""
    rows = numpy.asarray(bone12, dtype=numpy.float64).reshape(3, 4)
    return rows[:, :3], rows[:, 3]


def load_cb1_transform(floats16):
    """cb1 前 16 floats -> (R 3x3 行向量旋转, t 平移)（行向量约定：world = obj @ R + t）。"""
    m = numpy.asarray(floats16, dtype=numpy.float64).reshape(4, 4)
    return m[:3, :3], m[3, :3]


def calibrate_bone(bone12, cb1_src16, cb1_dst16, calibrate_enabled=True, clamp=2.0):
    """HLSL main() 的镜像：返回校准后骨骼 (3,4) 或直拷（开关关闭/形态无效/平移差超钳制）。

    兜底层级（与 calibrated hlsl 一致）：总开关关闭 -> 直拷；cb1 形态无效 -> 直拷；
    |t_src - t_dst| > clamp（2m，同角色合法组间差 <1m，共享数组/跨实例内容动辄数米）-> 直拷。
    """
    if not calibrate_enabled:
        return numpy.asarray(bone12, dtype=numpy.float64).reshape(3, 4)
    rs, t_src = load_cb1_transform(cb1_src16)
    rd, t_dst = load_cb1_transform(cb1_dst16)
    # HLSL 守卫：row3.w == 1（buf 中 row3 = [t, w]，w 在 flat[15]）
    if abs(cb1_src16[15] - 1.0) > 1e-3 or abs(cb1_dst16[15] - 1.0) > 1e-3:
        return numpy.asarray(bone12, dtype=numpy.float64).reshape(3, 4)
    if numpy.linalg.norm(t_src - t_dst) > clamp:
        return numpy.asarray(bone12, dtype=numpy.float64).reshape(3, 4)
    c_rot = rd @ rs.T          # Rd × Rs^T
    c_t = rd @ (t_src - t_dst)  # Rd × (t_src - t_dst)
    rm, tm = load_bone_3x4(bone12)
    rm_out = c_rot @ rm
    tm_out = c_rot @ tm + c_t
    out = numpy.zeros((3, 4))
    out[:, :3] = rm_out
    out[:, 3] = tm_out
    return out


def to_world_object(bone12, point):
    """列向量：object = Rm @ bind + tm。"""
    rm, tm = load_bone_3x4(bone12)
    return rm @ point + tm


def to_world(cb1_16, obj_point):
    """行向量：world = obj @ R + t == R.T @ obj + t。"""
    r, t = load_cb1_transform(cb1_16)
    return r.T @ obj_point + t


class CalibrateMathSyntheticTests(unittest.TestCase):
    """合成随机数据（无 dump 依赖）。"""

    def _random_rigid(self, rng, translate_scale=1.0):
        # 随机旋转（QR）+ 平移
        q, _ = numpy.linalg.qr(rng.normal(size=(3, 3)))
        if numpy.linalg.det(q) < 0:
            q[:, 0] = -q[:, 0]
        t = rng.normal(size=3) * translate_scale
        return q, t

    @staticmethod
    def _pack_cb1(r, t):
        """(R 3x3, t 3) -> cb1 16 floats：rows 0-2 = (旋转行, w=0)，row 3 = (t, w=1)。"""
        m = numpy.zeros((4, 4))
        m[:3, :3] = r
        m[3, :3] = t
        m[3, 3] = 1.0
        return m.reshape(-1)

    def test_world_invariance_cross_space(self):
        rng = numpy.random.default_rng(42)
        rs, ts = self._random_rigid(rng)
        rd, td = self._random_rigid(rng)
        cb1_src = self._pack_cb1(rs, ts)
        cb1_dst = self._pack_cb1(rd, td)

        m = numpy.zeros((3, 4))
        m[:, :3], m[:, 3] = self._random_rigid(rng, translate_scale=0.3)
        bone12 = m.reshape(-1)

        calibrated = calibrate_bone(bone12, cb1_src, cb1_dst)
        points = rng.normal(size=(50, 3))
        for p in points:
            w_src = to_world(cb1_src, to_world_object(bone12, p))
            w_dst = to_world(cb1_dst, to_world_object(calibrated.reshape(-1), p))
            self.assertAlmostEqual(float(numpy.abs(w_src - w_dst).max()), 0.0, places=6)

    def test_same_space_is_identity(self):
        rng = numpy.random.default_rng(7)
        rs, ts = self._random_rigid(rng)
        cb1 = self._pack_cb1(rs, ts)
        m = numpy.zeros((3, 4))
        m[:, :3], m[:, 3] = self._random_rigid(rng, translate_scale=0.3)
        bone12 = m.reshape(-1)
        calibrated = calibrate_bone(bone12, cb1, cb1)
        numpy.testing.assert_allclose(calibrated, bone12.reshape(3, 4), atol=1e-6)

    def test_invalid_cb1_falls_back_to_direct_copy(self):
        rng = numpy.random.default_rng(1)
        bone12 = rng.normal(size=12)
        zero_cb1 = numpy.zeros(16)  # row3.w = 0 -> 无效
        out = calibrate_bone(bone12, zero_cb1, zero_cb1)
        numpy.testing.assert_array_equal(out, bone12.reshape(3, 4))

    def test_calibrate_switch_off_is_direct_copy(self):
        """总开关关闭 -> 全部直拷（= 分组版行为，A/B 隔离验证用）。"""
        rng = numpy.random.default_rng(3)
        rs, ts = self._random_rigid(rng)
        rd, td = self._random_rigid(rng)
        cb1_src = self._pack_cb1(rs, ts)
        cb1_dst = self._pack_cb1(rd, td)
        bone12 = rng.normal(size=12)
        out = calibrate_bone(bone12, cb1_src, cb1_dst, calibrate_enabled=False)
        numpy.testing.assert_array_equal(out, bone12.reshape(3, 4))

    def test_far_transform_clamp_falls_back_to_direct_copy(self):
        """平移差 >2m（共享数组 0 号记录/别的实例的错误内容）-> 直拷，不错校准。"""
        rng = numpy.random.default_rng(5)
        rs, ts = self._random_rigid(rng)
        rd, td = self._random_rigid(rng)
        td_far = td + numpy.array([10.0, 0.0, 0.0])  # 相距 10m
        cb1_src = self._pack_cb1(rs, ts)
        cb1_dst_far = self._pack_cb1(rd, td_far)
        bone12 = rng.normal(size=12)
        out = calibrate_bone(bone12, cb1_src, cb1_dst_far)
        numpy.testing.assert_array_equal(out, bone12.reshape(3, 4))
        # 同数据但近距离（0.5m，合法组间差量级）-> 正常校准（结果与直拷不同）
        td_near = td + numpy.array([0.5, 0.0, 0.0])
        cb1_dst_near = self._pack_cb1(rd, td_near)
        out2 = calibrate_bone(bone12, cb1_src, cb1_dst_near)
        self.assertFalse(numpy.allclose(out2, bone12.reshape(3, 4), atol=1e-6))


@unittest.skipUnless(HAVE_DUMP, "提取数据不在本机")
class CalibrateRealDataTests(unittest.TestCase):
    def test_real_cb1_cross_group_calibration(self):
        """真实数据：b20f90ea palette 骨骼从身体组校准进头部组，世界位置不变。"""
        cb1_body = numpy.fromfile(os.path.join(DUMP, "de8e7949.buf"), dtype=numpy.float32)[:16]
        cb1_head = numpy.fromfile(os.path.join(DUMP, "7d18e062.buf"), dtype=numpy.float32)[:16]
        palette = numpy.fromfile(os.path.join(DUMP, "e018278f.buf"), dtype=numpy.float32).reshape(-1, 12)

        rng = numpy.random.default_rng(0)
        points = rng.normal(size=(200, 3)) * 0.5
        for bone in palette[:8]:  # 抽前 8 根
            calibrated = calibrate_bone(bone, cb1_body, cb1_head)
            for p in points:
                w_src = to_world(cb1_body, to_world_object(bone, p))
                w_dst = to_world(cb1_head, to_world_object(calibrated.reshape(-1), p))
                self.assertLess(float(numpy.abs(w_src - w_dst).max()), 1e-4)

    def test_parse_object_transform_reads_same_values(self):
        """parse_object_transform 读到的 16 floats 与原始 buf 一致（布局前提）。"""
        raw = numpy.fromfile(os.path.join(DUMP, "de8e7949.buf"), dtype=numpy.float32)[:16]
        parsed = _zzmi.ZZMIBoneMapBuilder.parse_object_transform(
            os.path.join(DUMP, "de8e7949.buf")
        )
        self.assertIsNotNone(parsed)
        numpy.testing.assert_allclose(numpy.array(parsed), raw, atol=0)


if __name__ == "__main__":
    unittest.main()
