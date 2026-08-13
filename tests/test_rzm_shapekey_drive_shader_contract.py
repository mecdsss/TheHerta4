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


if __name__ == "__main__":
    unittest.main()
