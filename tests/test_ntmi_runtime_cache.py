import os
import tempfile
import unittest

from ui.ntmi_modimp.runtime_cache import (
    MODIMP_RUNTIME_DIR_NAME,
    localize_runtime_path_props,
    object_workspace_dir_from_unique,
    prefix_identity_matches,
)


class NTMIRuntimeCacheTests(unittest.TestCase):
    def test_localize_runtime_path_props_copies_external_files_to_object_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_object_dir = os.path.join(temp_dir, "Workspace", "LOD0", "abc-3-0")
            frame_dump_dir = os.path.join(temp_dir, "FrameAnalysis", "deduped")
            os.makedirs(workspace_object_dir)
            os.makedirs(frame_dump_dir)

            external_buf = os.path.join(frame_dump_dir, "000001-vb0=deadbeef.buf")
            with open(external_buf, "wb") as file_obj:
                file_obj.write(b"runtime")

            localized = localize_runtime_path_props(
                {"modimp_vb0_buf_path": external_buf},
                workspace_object_dir,
            )

            expected_path = os.path.join(
                workspace_object_dir,
                MODIMP_RUNTIME_DIR_NAME,
                "000001-vb0=deadbeef.buf",
            )
            self.assertEqual(localized["modimp_vb0_buf_path"], expected_path)
            self.assertTrue(os.path.isfile(expected_path))
            with open(expected_path, "rb") as file_obj:
                self.assertEqual(file_obj.read(), b"runtime")

    def test_localize_runtime_path_props_keeps_workspace_local_files_in_place(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_object_dir = os.path.join(temp_dir, "Workspace", "LOD0", "abc-3-0")
            os.makedirs(workspace_object_dir)
            local_buf = os.path.join(workspace_object_dir, "abc-3-0-Position.buf")
            with open(local_buf, "wb") as file_obj:
                file_obj.write(b"local")

            localized = localize_runtime_path_props(
                {"modimp_vb0_buf_path": local_buf},
                workspace_object_dir,
            )

            self.assertEqual(localized["modimp_vb0_buf_path"], local_buf)
            self.assertFalse(os.path.isdir(os.path.join(workspace_object_dir, MODIMP_RUNTIME_DIR_NAME)))

    def test_object_workspace_dir_from_lod_unique_str(self):
        workspace_root = os.path.join("X:", "Workspace")

        result = object_workspace_dir_from_unique(workspace_root, "LOD0.abc-3-0")

        self.assertEqual(result, os.path.join(workspace_root, "LOD0", "abc-3-0"))

    def test_prefix_identity_matches_treats_bare_prefix_as_lod0_compatible(self):
        self.assertTrue(
            prefix_identity_matches(
                ("", "abc12345-12-0"),
                ("lod0", "abc12345-12-0"),
            )
        )
        self.assertTrue(
            prefix_identity_matches(
                ("lod0", "abc12345-12-0"),
                ("", "abc12345-12-0"),
            )
        )

    def test_prefix_identity_matches_rejects_different_slice_prefix(self):
        self.assertFalse(
            prefix_identity_matches(
                ("lod0", "abc12345-12-0"),
                ("lod0", "abc12345-12-12"),
            )
        )


if __name__ == "__main__":
    unittest.main()
