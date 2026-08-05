import unittest

from tests.test_node_postprocess_draginteraction import (
    _base_sections,
    _load_drag_module,
    _make_node,
)


class DragPresentOrderTests(unittest.TestCase):
    def _emit(self, **props):
        node = _make_node(_load_drag_module(), **props)
        sections = _base_sections()
        comps = node._locate_components(sections, ["abc123"])
        node._emit_sections(sections, comps, "testns")
        return node, sections, comps

    def test_drag_present_runs_before_ui_model_binding_marker(self):
        node, sections, comps = self._emit()
        sections["[Present]"] = [
            "    $ui_layout = 1",
            "",
            "    ; --- MODEL DRAG BINDING BEGIN ---",
            "    if $mouse_clicked == 1 && $is_dragging == 0 && $help == 1",
            "        if $ssmtdrag_ui_detected_testns >= 0 && ($ssmtdrag_ui_zone_testns == 0)",
            "            $is_dragging = 9",
            "        endif",
            "    endif",
            "    ; --- MODEL DRAG BINDING END ---",
        ]
        node._emit_present_and_constants(sections, comps, "testns")
        present = "\n".join(sections["[Present]"])
        self.assertLess(present.index("DRAG PRESENT BEGIN"), present.index("MODEL DRAG BINDING BEGIN"))
        self.assertLess(present.index("run = CommandListDragPinDetected_testns"), present.index("MODEL DRAG BINDING BEGIN"))
        self.assertLess(
            present.index("run = CommandListDragCursorUpdate_testns"),
            present.index("run = CommandListDragPinDetected_testns"),
        )

        node._emit_present_and_constants(sections, comps, "testns")
        present = "\n".join(sections["[Present]"])
        self.assertEqual(present.count("DRAG PRESENT BEGIN"), 1)
        self.assertLess(present.index("run = CommandListDragPinDetected_testns"), present.index("MODEL DRAG BINDING BEGIN"))

    def test_drag_present_uses_legacy_binding_line_without_marker(self):
        node, sections, comps = self._emit()
        sections["[Present]"] = [
            "    if $mouse_clicked == 1 && $model_drag_prev_lmb == 0 && $is_dragging == 0 && $help == 1",
            "        if $ssmtdrag_ui_detected_testns >= 0",
            "            $is_dragging = 9",
            "        endif",
            "    endif",
        ]
        node._emit_present_and_constants(sections, comps, "testns")
        present = "\n".join(sections["[Present]"])
        self.assertLess(present.index("DRAG PRESENT BEGIN"), present.index("if $mouse_clicked == 1"))

    def test_drag_present_appends_when_no_ui_binding_marker(self):
        node, sections, comps = self._emit()
        sections["[Present]"] = ["    $ui_layout = 1"]
        node._emit_present_and_constants(sections, comps, "testns")
        present = "\n".join(sections["[Present]"])
        self.assertLess(present.index("$ui_layout = 1"), present.index("DRAG PRESENT BEGIN"))

    def test_ui_tail_legacy_help_mode_override_is_removed_and_references_rewritten(self):
        node, _, _ = self._emit()
        tail = (
            "    if $help == 1\n"
            "        $ssmtdrag_mode_A = 1\n"
            "    else\n"
            "        $ssmtdrag_mode_A = $ssmtdrag_modifier_down_A\n"
            "    endif\n"
            "    ; --- MODEL DRAG BINDING BEGIN ---\n"
            "    if $ssmtdrag_ui_detected_A >= 0 && $ssmtdrag_ui_zone_A == 0\n"
            "        $is_dragging = 9\n"
            "    endif\n"
            "    ; --- MODEL DRAG BINDING END ---\n"
        )
        normalized = node._normalize_ui_drag_references(tail, "testns")
        self.assertIn("$ssmtdrag_ui_detected_testns", normalized)
        self.assertIn("$ssmtdrag_ui_zone_testns", normalized)
        self.assertNotIn("$ssmtdrag_mode_A", normalized)
        self.assertNotIn("$ssmtdrag_modifier_down_A", normalized)
        self.assertNotIn("_A", normalized)

    def test_alt_grab_key_requires_modifier_instead_of_help(self):
        node, sections, comps = self._emit()
        node._emit_present_and_constants(sections, comps, "testns")
        present = "\n".join(sections["[Present]"])
        self.assertIn("$ssmtdrag_mode_testns = $ssmtdrag_modifier_down_testns", present)
        self.assertNotIn("if $help == 1", present.split("DRAG PRESENT BEGIN", 1)[1])

    def test_none_grab_key_keeps_detection_always_on(self):
        node, sections, comps = self._emit(grab_key="NONE")
        node._emit_present_and_constants(sections, comps, "testns")
        present = "\n".join(sections["[Present]"])
        self.assertIn("$ssmtdrag_mode_testns = 1", present)
        self.assertIn("$ssmtdrag_modifier_ok_testns = 1", present)

    def test_ui_readback_command_list_is_emitted_and_post_run_precedes_drawn_reset(self):
        node, sections, comps = self._emit()
        node._emit_present_and_constants(sections, comps, "testns")
        readback = "\n".join(sections.get("[CommandListDragUIReadback_testns]", []))
        self.assertIn("DRAG UI BRIDGE BEGIN", readback)
        self.assertIn("store = $ssmtdrag_ui_detected_testns", readback)
        self.assertIn("store = $ssmtdrag_ui_zone_testns", readback)

        present = "\n".join(sections["[Present]"])
        self.assertLess(present.index("run = CommandListDragUIReadback_testns"),
                        present.index("post $ssmtdrag_drawn_testns = 0"))

    def test_drag_present_is_relocated_into_ui_tail_before_binding_marker(self):
        node, sections, comps = self._emit()
        node._emit_present_and_constants(sections, comps, "testns")
        tail = (
            "    $ui_layout = 1\n"
            "    ; --- MODEL DRAG BINDING BEGIN ---\n"
            "    if $ssmtdrag_ui_detected_testns >= 0\n"
            "        $is_dragging = 9\n"
            "    endif\n"
            "    ; --- MODEL DRAG BINDING END ---\n"
        )
        normalized_tail = node._relocate_drag_present_into_ui_tail(sections, tail)
        self.assertIn("DRAG PRESENT BEGIN", normalized_tail)
        self.assertLess(normalized_tail.index("DRAG PRESENT BEGIN"), normalized_tail.index("MODEL DRAG BINDING BEGIN"))
        self.assertLess(normalized_tail.index("run = CommandListDragPinDetected_testns"), normalized_tail.index("MODEL DRAG BINDING BEGIN"))
        self.assertNotIn("DRAG PRESENT BEGIN", "\n".join(sections["[Present]"]))

    def test_ui_tail_deduplicates_old_binding_blocks_and_removes_prev_lmb_latch(self):
        node, _, _ = self._emit()
        tail = (
            "    ; --- MODEL DRAG BINDING BEGIN ---\n"
            "    if $mouse_clicked == 1 && $model_drag_prev_lmb == 0 && $is_dragging == 0 && $help == 1\n"
            "        if $ssmtdrag_ui_detected_A >= 0 && $ssmtdrag_ui_zone_A == 1\n"
            "            $is_dragging = 9\n"
            "        endif\n"
            "    endif\n"
            "    ; --- MODEL DRAG BINDING END ---\n"
            "    $model_drag_prev_lmb = $mouse_clicked\n"
            "    ; --- MODEL DRAG BINDING BEGIN ---\n"
            "    if $mouse_clicked == 1 && $model_drag_prev_lmb == 0 && $is_dragging == 0 && $help == 1\n"
            "        if $ssmtdrag_ui_detected_A >= 0 && $ssmtdrag_ui_zone_A == 2\n"
            "            $is_dragging = 9\n"
            "        endif\n"
            "    endif\n"
            "    ; --- MODEL DRAG BINDING END ---\n"
        )
        normalized = node._normalize_ui_drag_references(tail, "testns")
        self.assertEqual(normalized.count("MODEL DRAG BINDING BEGIN"), 1)
        self.assertIn("$ssmtdrag_ui_detected_testns >= 0", normalized)
        self.assertIn("$ssmtdrag_ui_zone_testns == 1", normalized)
        self.assertNotIn("$model_drag_prev_lmb == 0", normalized)

    def test_old_ui_tail_gets_virtual_model_drag_cursor_bridge(self):
        node, _, _ = self._emit()
        tail = (
            "global $prev_cursor_x\n"
            "global $prev_cursor_y\n"
            "global $cursor_delta_x\n"
            "global $cursor_delta_y\n"
            "[CustomShaderDraw]\n"
            "    if cursor_x > $d3\n"
            "[Present]\n"
            "    $dx = (cursor_x - $cx) * $aspect\n"
            "    $dy = cursor_y - $cy\n"
            "    ; --- MODEL DRAG BINDING BEGIN ---\n"
            "    if $mouse_clicked == 1 && $is_dragging == 0 && $help == 1\n"
            "        if $ssmtdrag_ui_detected_A >= 0 && ($ssmtdrag_ui_zone_A == 1)\n"
            "            $is_dragging = 9\n"
            "            $drag_action = 2\n"
            "            $model_drag_capture = 1\n"
            "            $drag_dx = 0\n"
            "            $drag_dy = 0\n"
            "        endif\n"
            "    endif\n"
            "    ; --- MODEL DRAG BINDING END ---\n"
            "    $prev_cursor_x = cursor_x\n"
            "    $prev_cursor_y = cursor_y\n"
        )
        normalized = node._normalize_ui_drag_references(tail, "testns")
        self.assertIn("global $model_drag_cursor_x = 0", normalized)
        self.assertIn("$model_drag_cursor_x = cursor_x", normalized)
        self.assertIn("$dx = ($model_drag_cursor_x - $cx)", normalized)
        self.assertIn("$prev_cursor_x = $model_drag_cursor_x", normalized)
        self.assertIn("$model_drag_anchor_x = cursor_x", normalized)
        self.assertIn("$model_drag_ref_x = ($abs_x_8 + $abs_w_8*0.5)", normalized)
        self.assertIn("$prev_cursor_x = ($abs_x_8 + $abs_w_8*0.5)", normalized)
        self.assertIn("$ssmtdrag_ui_detected_testns >= 0", normalized)
        self.assertIn("if cursor_x > $d3", normalized)

    def test_old_ui_tail_virtual_cursor_uses_handle_reference(self):
        node, _, _ = self._emit()
        tail = (
            "global $prev_cursor_x\n"
            "global $prev_cursor_y\n"
            "global $cursor_delta_y\n"
            "global $hs_8\n"
            "global $hh_8\n"
            "global $anim_handle_scale_8\n"
            "global $r_hdl_8_x\n"
            "global $r_hdl_8_y\n"
            "[Present]\n"
            "    ; --- MODEL DRAG BINDING BEGIN ---\n"
            "    if $mouse_clicked == 1 && $is_dragging == 0 && $help == 1\n"
            "        if $ssmtdrag_ui_detected_A >= 0 && ($ssmtdrag_ui_zone_A == 1)\n"
            "            $is_dragging = 9\n"
            "            $drag_action = 2\n"
            "            $model_drag_capture = 1\n"
            "            $drag_dx = 0\n"
            "            $drag_dy = 0\n"
            "        endif\n"
            "    endif\n"
            "    ; --- MODEL DRAG BINDING END ---\n"
            "    $prev_cursor_x = cursor_x\n"
            "    $prev_cursor_y = cursor_y\n"
        )
        normalized = node._normalize_ui_drag_references(tail, "testns")
        self.assertIn("$model_drag_ref_x = ($r_hdl_8_x + ($hs_8*$zoom_global*$anim_handle_scale_8)*0.5)", normalized)
        self.assertIn("$model_drag_ref_y = ($r_hdl_8_y + ($hh_8*$zoom_global*$anim_handle_scale_8)*0.5)", normalized)
        self.assertIn("$prev_cursor_x = ($r_hdl_8_x + ($hs_8*$zoom_global*$anim_handle_scale_8)*0.5)", normalized)
        self.assertIn("$prev_cursor_y = ($r_hdl_8_y + ($hh_8*$zoom_global*$anim_handle_scale_8)*0.5)", normalized)

    def test_old_ui_tail_removes_legacy_model_hit_test_block(self):
        node, _, _ = self._emit()
        tail = (
            "    ; --- MODEL HIT TEST BEGIN ---\n"
            "    if $ssmtdrag_ui_detected_A >= 0 && ($ssmtdrag_ui_zone_A == 1)\n"
            "        $val_8_x = 0\n"
            "        $val_8_y = 0.8\n"
            "    endif\n"
            "    ; --- MODEL HIT TEST END ---\n"
            "    ; --- MODEL DRAG BINDING BEGIN ---\n"
            "    if $ssmtdrag_ui_detected_A >= 0\n"
            "        $is_dragging = 9\n"
            "    endif\n"
            "    ; --- MODEL DRAG BINDING END ---\n"
        )
        normalized = node._normalize_ui_drag_references(tail, "testns")
        self.assertNotIn("MODEL HIT TEST", normalized)
        self.assertNotIn("$val_8_x = 0", normalized)
        self.assertIn("MODEL DRAG BINDING BEGIN", normalized)

    def test_virtual_cursor_injection_is_idempotent(self):
        node, _, _ = self._emit()
        tail = (
            "global $prev_cursor_x\n"
            "global $prev_cursor_y\n"
            "global $cursor_delta_y\n"
            "global $hs_8\n"
            "global $hh_8\n"
            "global $anim_handle_scale_8\n"
            "global $r_hdl_8_x\n"
            "global $r_hdl_8_y\n"
            "[Present]\n"
            "    ; --- MODEL DRAG BINDING BEGIN ---\n"
            "    if $mouse_clicked == 1 && $is_dragging == 0 && $help == 1\n"
            "        if $ssmtdrag_ui_detected_A >= 0 && ($ssmtdrag_ui_zone_A == 1)\n"
            "            $is_dragging = 9\n"
            "            $drag_action = 2\n"
            "            $model_drag_capture = 1\n"
            "            $drag_dx = 0\n"
            "            $drag_dy = 0\n"
            "        endif\n"
            "    endif\n"
            "    ; --- MODEL DRAG BINDING END ---\n"
            "    $prev_cursor_x = cursor_x\n"
            "    $prev_cursor_y = cursor_y\n"
        )
        once = node._normalize_ui_drag_references(tail, "testns")
        twice = node._normalize_ui_drag_references(once, "testns")
        self.assertEqual(twice, once)
        self.assertEqual(once.count("global $model_drag_cursor_x = 0"), 1)
        self.assertEqual(once.count("$model_drag_anchor_x = cursor_x"), 1)
        self.assertEqual(once.count("$prev_cursor_x = ($r_hdl_8_x + ($hs_8*$zoom_global*$anim_handle_scale_8)*0.5)"), 1)


if __name__ == "__main__":
    unittest.main()
