import unittest
from pathlib import Path


SHADER_PATH = Path(__file__).resolve().parents[1] / "Toolset" / "drag_interaction" / "rzm_shapekey_drive.hlsl"


class ShapeKeyDriveShaderContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SHADER_PATH.read_text(encoding="utf-8")

    def test_cold_start_seed_runs_before_mode_two_early_return(self):
        seed_start = self.source.index("// 冷启动播种")
        mode_guard = self.source.index("if (mode != 1.0)")
        seed_write = self.source.index("ClickCount[seedZone] = seeded")

        self.assertLess(seed_start, mode_guard)
        self.assertLess(seed_write, mode_guard)

    def test_default_mode_contract_still_preserves_previous_press_state(self):
        mode_guard = self.source.index("if (mode != 1.0)")
        state_update = self.source.index("ShapeKeyDir[lastSlot] = triggerHeld ? 1.0 : 0.0", mode_guard)
        return_index = self.source.index("return;", state_update)

        self.assertLess(mode_guard, state_update)
        self.assertLess(state_update, return_index)

    def test_seed_updates_integer_float_and_one_hot_buffers(self):
        seed_block_start = self.source.index("if (IniParams[80].y > 0.5")
        seed_block_end = self.source.index("// 仅“仅命中”模式", seed_block_start)
        seed_block = self.source[seed_block_start:seed_block_end]

        self.assertIn("ClickCount[seedZone] = seeded", seed_block)
        self.assertIn("ClickCountF[seedZone] = (float)seeded", seed_block)
        self.assertIn("ShapeKeyDrive[oneHotIdx] = 1.0", seed_block)

    # ---- 拖拽绑定锁存（latch）契约：绑定后移出区域不丢控，松开才解除 ----

    def test_dir_buffer_guard_and_latch_resource_guard(self):
        # 方向缓冲末位 prev press state（守卫 +1）；锁存为独立单槽资源（守卫 >=1）
        self.assertIn("dirSlots < lastSlot + 1u", self.source)
        self.assertNotIn("dirSlots < lastSlot + 2u", self.source)
        self.assertIn("DragLatch.GetDimensions(latchSlots);", self.source)
        self.assertIn("if (latchSlots < 1u)", self.source)

    def test_latch_binds_on_held_hit_and_keeps_latched_zone(self):
        # 锁存编码 0=未绑定（boot/失臂清零即未绑定），否则 区域id+1；
        # 按住期间：已有锁存优先（latchValue），未锁存时当前命中即绑定
        self.assertIn("float latchValue = DragLatch[0];", self.source)
        self.assertIn(
            "int boundZone = (latchValue > 0.5) ? (int)floor(latchValue + 0.5) - 1 : -1;",
            self.source)
        bind_block_start = self.source.index("if (triggerHeld)")
        bind_block_end = self.source.index("DragLatch[0] = (float)(boundZone + 1);")
        bind_block = self.source[bind_block_start:bind_block_end]
        self.assertIn("if (boundZone < 0 && realHit)", bind_block)
        self.assertIn("boundZone = (int)hoverZone;", bind_block)

    def test_latch_writeback_runs_every_mode1_dispatch(self):
        # 锁存写回必须每帧执行（含未绑定写 0 的释放沿）
        mode_guard = self.source.index("if (mode != 1.0)")
        writeback = self.source.index("DragLatch[0] = (float)(boundZone + 1);")
        loop_start = self.source.index("uint runningBase = 0u;")
        self.assertLess(mode_guard, writeback)
        self.assertLess(writeback, loop_start)

    def test_mode_exit_clears_latch(self):
        # mode!=1 早退分支：除 prev press state 外必须清锁存（防陈旧绑定跨模式残留）
        mode_guard = self.source.index("if (mode != 1.0)")
        state_update = self.source.index("ShapeKeyDir[lastSlot] = triggerHeld ? 1.0 : 0.0", mode_guard)
        latch_clear = self.source.index("DragLatch[0] = 0.0;", mode_guard)
        return_index = self.source.index("return;", state_update)
        self.assertLess(mode_guard, state_update)
        self.assertLess(state_update, latch_clear)
        self.assertLess(latch_clear, return_index)

    def test_direction_drive_gated_by_latch_not_live_hover(self):
        # 位移积分/主导方向只看锁存绑定（zoneDriven），不再被当帧实时命中截断
        self.assertIn("bool zoneDriven = boundZone >= 0 && zone == (uint)boundZone;", self.source)
        gate = self.source.index("if (zoneDriven)")
        integrate = self.source.index(
            "next = clamp(current + net * moveLen * mouseSensitivity, 0.0, 1.0);")
        self.assertLess(gate, integrate)
        self.assertIn("if (zoneDriven)\n            ActiveDir[zone] = activeDir;", self.source)
        self.assertNotIn("if (zoneHit)\n", self.source)

    def test_click_stage_advance_still_requires_real_hit_press_edge(self):
        # 档位推进保持按下沿 + 真实命中：锁存绑定不在区域外推进档位
        self.assertIn("bool hasHit = realHit && triggerHeld;", self.source)
        self.assertIn("if (pressed && hasHit)", self.source)
        self.assertIn("bool zonePressed = zoneHit && pressed;", self.source)


if __name__ == "__main__":
    unittest.main()
