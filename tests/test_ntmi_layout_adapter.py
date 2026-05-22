import importlib.util
import sys
import unittest
from pathlib import Path

import numpy as np


module_path = Path(__file__).resolve().parents[1] / "blueprint" / "ntmi_layout_adapter.py"
spec = importlib.util.spec_from_file_location("ntmi_layout_adapter", module_path)
ntmi_layout_adapter = importlib.util.module_from_spec(spec)
sys.modules["ntmi_layout_adapter"] = ntmi_layout_adapter
spec.loader.exec_module(ntmi_layout_adapter)


class NTMILayoutAdapterTests(unittest.TestCase):
    def test_local_loop_indices_for_export_range_rebases_part_global_vertices_to_mesh_local_vertices(self):
        exported_loop_indices = np.asarray([10, 11, 12], dtype=np.int32)
        export_indices = np.arange(3, 6, dtype=np.int32)

        local_loop_indices = ntmi_layout_adapter.local_loop_indices_for_export_range(
            exported_loop_indices,
            export_indices,
            start_vertex=3,
        )

        np.testing.assert_array_equal(local_loop_indices, np.asarray([10, 11, 12], dtype=np.int32))

    def test_local_loop_indices_for_export_range_returns_empty_when_range_exceeds_mesh_vertices(self):
        exported_loop_indices = np.asarray([10, 11, 12], dtype=np.int32)
        export_indices = np.arange(3, 7, dtype=np.int32)

        local_loop_indices = ntmi_layout_adapter.local_loop_indices_for_export_range(
            exported_loop_indices,
            export_indices,
            start_vertex=3,
        )

        self.assertEqual(local_loop_indices.size, 0)


if __name__ == "__main__":
    unittest.main()
