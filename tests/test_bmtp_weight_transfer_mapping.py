# -*- coding: utf-8 -*-
"""工具集权重传递必须使用最近面的重心插值。"""

import ast
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "toolkit" / "bmtp_weight_tools.py"


class WeightTransferMappingTests(unittest.TestCase):
    def test_all_data_transfer_calls_use_face_interpolation(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        mapping_value = None
        transfer_mappings = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                if any(
                    isinstance(target, ast.Name)
                    and target.id == "WEIGHT_TRANSFER_VERTEX_MAPPING"
                    for target in node.targets
                ):
                    mapping_value = ast.literal_eval(node.value)
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "data_transfer"
            ):
                continue
            keyword = next(
                (item for item in node.keywords if item.arg == "vert_mapping"),
                None,
            )
            self.assertIsNotNone(keyword)
            self.assertIsInstance(keyword.value, ast.Name)
            transfer_mappings.append(keyword.value.id)

        self.assertEqual(mapping_value, "POLYINTERP_NEAREST")
        self.assertEqual(
            transfer_mappings,
            ["WEIGHT_TRANSFER_VERTEX_MAPPING", "WEIGHT_TRANSFER_VERTEX_MAPPING"],
        )


if __name__ == "__main__":
    unittest.main()
