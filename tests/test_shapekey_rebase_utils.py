import unittest

import numpy as np

from utils.shapekey_rebase_utils import rebase_shape_key_coordinates


class ShapeKeyRebaseUtilsTests(unittest.TestCase):
    """测试形态键坐标重定基功能"""

    def test_rebase_moves_other_keys_onto_new_basis_using_original_offsets(self):
        """测试重定基将其他形态键移动到新 Basis 上，使用原始偏移量"""
        coordinates = {
            "Basis": np.array([[0.0, 0.0, 0.0]], dtype=np.float32),
            "MoveY": np.array([[0.0, 1.0, 0.0]], dtype=np.float32),
            "MoveX": np.array([[2.0, 0.0, 0.0]], dtype=np.float32),
        }

        rebased = rebase_shape_key_coordinates(
            coordinates_by_name=coordinates,
            basis_name="Basis",
            new_basis_name="MoveX",
            remove_new_basis_key=True,
        )

        np.testing.assert_allclose(rebased["Basis"], [[2.0, 0.0, 0.0]])
        np.testing.assert_allclose(rebased["MoveY"], [[2.0, 1.0, 0.0]])
        self.assertNotIn("MoveX", rebased)

    def test_rebase_can_keep_source_shape_key_as_zero_offset_copy(self):
        """测试重定基可以保留源形态键作为零偏移副本"""
        coordinates = {
            "Basis": np.array([[5.0, 0.0, 0.0], [1.0, 2.0, 3.0]], dtype=np.float32),
            "Smile": np.array([[5.0, 1.0, 0.0], [2.0, 4.0, 6.0]], dtype=np.float32),
            "Blink": np.array([[6.0, 0.0, 0.0], [3.0, 2.0, 3.0]], dtype=np.float32),
        }

        rebased = rebase_shape_key_coordinates(
            coordinates_by_name=coordinates,
            basis_name="Basis",
            new_basis_name="Smile",
            remove_new_basis_key=False,
        )

        np.testing.assert_allclose(rebased["Basis"], coordinates["Smile"])
        np.testing.assert_allclose(rebased["Smile"], coordinates["Smile"])
        np.testing.assert_allclose(
            rebased["Blink"],
            np.array([[6.0, 1.0, 0.0], [4.0, 4.0, 6.0]], dtype=np.float32),
        )


if __name__ == "__main__":
    unittest.main()
