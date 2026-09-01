"""帧序列级锁存语义测试：拖拽×形态键联动。

本文件用 Python 复刻 rzm_shapekey_drive.hlsl 的锁存状态机（latch 三态），
模拟「绑定 → 光标移出区域（仍按住）→ 松开」的帧序列，验证：
1. 绑定后移出命中区域，方向位移驱动不中断（修复前的 bug：当帧丢控）；
2. 按住期间悬停其它区域，驱动仍作用于绑定区域（锁存优先）；
3. level 绑定：先按住后滑入区域同样绑定（与面板联动语义一致）；
4. 松开当帧解除绑定，之后不再驱动；
5. 点击档位推进仍要求按下沿 + 真实命中，锁存不在区域外推进档位；
6. 模式切出（mode != 1）清除锁存；
7. 失臂空窗（dispatch gap：松 Alt/undraw/模式 0）由生成器 else 分支清锁存，
   下次臂动+按住无命中不复活旧绑定（评审 F2）。

锁存编码：0 = 未绑定（boot/失臂清零值即未绑定，评审 F1），否则存 区域id+1。

模型不是独立臆测：每个关键判定都从着色器源码取锚点断言（_assert_anchors），
锚点缺失即测试失败，防止模型与实现漂移。
"""

import unittest
from pathlib import Path


SHADER_PATH = Path(__file__).resolve().parents[1] / "Toolset" / "drag_interaction" / "rzm_shapekey_drive.hlsl"


def _assert_anchors(testcase, source):
    """把模型的每个判定锚定到着色器源码文本；锚点漂移即失败。"""
    # 锁存读取/解码/写回（独立单槽资源，0=未绑定，否则 区域id+1）
    testcase.assertIn("float latchValue = DragLatch[0];", source)
    testcase.assertIn(
        "int boundZone = (latchValue > 0.5) ? (int)floor(latchValue + 0.5) - 1 : -1;",
        source)
    testcase.assertIn("if (boundZone < 0 && realHit)", source)
    testcase.assertIn("boundZone = (int)hoverZone;", source)
    testcase.assertIn("DragLatch[0] = (float)(boundZone + 1);", source)
    # 位移驱动门控 = 锁存绑定
    testcase.assertIn("bool zoneDriven = boundZone >= 0 && zone == (uint)boundZone;", source)
    # 按下沿 = 真实命中 + 按住（档位推进用）
    testcase.assertIn("bool hasHit = realHit && triggerHeld;", source)
    testcase.assertIn("if (pressed && hasHit)", source)
    # 模式切出清锁存
    mode_guard = source.index("if (mode != 1.0)")
    latch_clear = source.index("DragLatch[0] = 0.0;", mode_guard)
    testcase.assertLess(mode_guard, latch_clear)


class DriveLatchModel:
    """rzm_shapekey_drive.hlsl main() 的锁存状态机复刻（锚点见 _assert_anchors）。

    只建模与锁存相关的判定：triggerHeld / realHit / hoverZone / pressed 沿 /
    boundZone 三态（编码 0=未绑定、区域id+1）/ zoneDriven 门控 / ClickCount
    档位推进 / mode!=1 清除 / 失臂 else 清锁存（disarm()）。
    drive_buffer 为每区域 1 个代表槽（方向槽语义：zoneDriven 时按位移累加）。
    """

    def __init__(self, zone_count=2, stage_counts=None):
        self.zone_count = zone_count
        self.stage_counts = stage_counts or [1] * zone_count
        self.latch = 0.0           # DragLatch[0]：0=未绑定，否则 区域id+1
        self.was_held = False      # ShapeKeyDir[lastSlot]
        self.click_count = [0] * zone_count
        self.drive = [0.0] * zone_count

    def disarm(self):
        """失臂帧（松 Alt/undraw/输入模式切换由 PinDetected 臂动门控 else 整清；
        模式 0/1→0 直跳由 Present 终态 else 整清，评审 G1）：驱动 CS 被外层
        门控跳过，生成器 else 分支整清锁存资源；prev-press 槽不动。"""
        self.latch = 0.0

    def frame(self, mode=1, held=False, real_hit=False, hover_zone=0, move=0.0):
        """执行一帧 dispatch，返回 {zone: 本帧是否被位移驱动}。"""
        trigger_held = held
        pressed = trigger_held and not self.was_held

        if mode != 1:
            self.was_held = trigger_held
            self.latch = 0.0
            return {z: False for z in range(self.zone_count)}

        # 锁存三态（与着色器同构；编码 0=未绑定、区域id+1）
        bound_zone = int(self.latch + 0.5) - 1 if self.latch > 0.5 else -1
        if trigger_held:
            if bound_zone < 0 and real_hit:
                bound_zone = hover_zone
            if bound_zone >= self.zone_count:
                bound_zone = -1
        else:
            bound_zone = -1
        self.latch = float(bound_zone + 1)

        has_hit = real_hit and trigger_held
        if pressed and has_hit:
            stage_cap = max(1, self.stage_counts[hover_zone])
            old = self.click_count[hover_zone]
            self.click_count[hover_zone] = 0 if old >= stage_cap else old + 1
        self.was_held = trigger_held

        driven = {}
        for zone in range(self.zone_count):
            zone_driven = bound_zone >= 0 and zone == bound_zone
            driven[zone] = zone_driven
            if zone_driven:
                self.drive[zone] = min(1.0, max(0.0, self.drive[zone] + move))
        return driven


class DriveLatchFrameSequenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SHADER_PATH.read_text(encoding="utf-8")

    def setUp(self):
        _assert_anchors(self, self.source)

    def test_boot_clear_reads_as_unbound(self):
        """评审 F1：boot 清零（0.0）必须解码为未绑定——首个按住 dispatch
        无命中不得假绑 zone 0。"""
        m = DriveLatchModel(zone_count=2)
        self.assertEqual(m.latch, 0.0)  # boot-clear 值
        driven = m.frame(held=True, real_hit=False, move=0.1)
        self.assertEqual(driven, {0: False, 1: False})
        self.assertEqual(m.latch, 0.0)
        self.assertAlmostEqual(m.drive[0], 0.0)

    def test_bind_then_leave_zone_keeps_driving_until_release(self):
        """核心回归：命中绑定后光标移出区域（仍按住），驱动不中断；松开才解除。"""
        m = DriveLatchModel(zone_count=2)
        # 帧 1：按住 + 命中 zone0 → 绑定并驱动
        driven = m.frame(held=True, real_hit=True, hover_zone=0, move=0.1)
        self.assertEqual(driven, {0: True, 1: False})
        self.assertEqual(m.latch, 1.0)  # 编码：zone0 + 1
        # 帧 2-4：光标移出区域（无命中）但仍按住并持续移动 → 驱动不中断
        for _ in range(3):
            driven = m.frame(held=True, real_hit=False, move=0.1)
            self.assertEqual(driven, {0: True, 1: False})
        self.assertAlmostEqual(m.drive[0], 0.4)
        # 帧 5：松开 → 当帧解除绑定，不再驱动
        driven = m.frame(held=False, real_hit=False, move=0.1)
        self.assertEqual(driven, {0: False, 1: False})
        # 帧 6：松开后保持值（缓冲不清零）
        self.assertAlmostEqual(m.drive[0], 0.4)
        self.assertEqual(m.latch, 0.0)

    def test_latch_priority_over_live_hover_on_other_zone(self):
        """绑定 zone0 后悬停 zone1（仍按住）：驱动仍作用于 zone0。"""
        m = DriveLatchModel(zone_count=2)
        m.frame(held=True, real_hit=True, hover_zone=0, move=0.1)
        driven = m.frame(held=True, real_hit=True, hover_zone=1, move=0.1)
        self.assertEqual(driven, {0: True, 1: False})
        self.assertAlmostEqual(m.drive[0], 0.2)
        self.assertAlmostEqual(m.drive[1], 0.0)

    def test_level_triggered_bind_when_sliding_in_while_held(self):
        """先按住（未命中）后滑入区域：无需新按下沿即绑定（对齐面板语义）。"""
        m = DriveLatchModel(zone_count=2)
        driven = m.frame(held=True, real_hit=False, move=0.1)
        self.assertEqual(driven, {0: False, 1: False})
        driven = m.frame(held=True, real_hit=True, hover_zone=1, move=0.1)
        self.assertEqual(driven, {0: False, 1: True})

    def test_click_stage_advance_requires_real_hit_press_edge(self):
        """档位推进不被锁存扩大：绑定后移出区域再按下（区域外）不推进档位。"""
        m = DriveLatchModel(zone_count=2, stage_counts=[2, 1])
        # 按下 + 命中 zone0 → 档位 0→1
        m.frame(held=True, real_hit=True, hover_zone=0)
        self.assertEqual(m.click_count[0], 1)
        # 松开，移到区域外，再按下（无命中）→ 不推进
        m.frame(held=False)
        m.frame(held=True, real_hit=False)
        self.assertEqual(m.click_count[0], 1)
        # 再松开，重新命中按下 → 推进到 2（=stage_cap 上限）
        m.frame(held=False)
        m.frame(held=True, real_hit=True, hover_zone=0)
        self.assertEqual(m.click_count[0], 2)
        # 再按一次 → 回到 0（循环清空）
        m.frame(held=False)
        m.frame(held=True, real_hit=True, hover_zone=0)
        self.assertEqual(m.click_count[0], 0)

    def test_mode_exit_clears_latch(self):
        """切出模式 1（CS 仍运行的模式 2）清除锁存，防止陈旧绑定跨模式残留。"""
        m = DriveLatchModel(zone_count=2)
        m.frame(held=True, real_hit=True, hover_zone=0, move=0.1)
        self.assertEqual(m.latch, 1.0)
        driven = m.frame(mode=2, held=True, real_hit=True, hover_zone=0, move=0.1)
        self.assertEqual(driven, {0: False, 1: False})
        self.assertEqual(m.latch, 0.0)
        # 切回模式 1：不继承旧绑定（需重新命中才绑定）
        driven = m.frame(mode=1, held=True, real_hit=False, move=0.1)
        self.assertEqual(driven, {0: False, 1: False})

    def test_disarm_gap_clears_latch_no_resurrection(self):
        """评审 F2：拖拽中松 Alt（失臂空窗，无 dispatch）→ else 分支清锁存；
        下次臂动+按住但无命中不得复活旧绑定。"""
        m = DriveLatchModel(zone_count=2)
        # 绑定 zone0 并拖了一段
        m.frame(held=True, real_hit=True, hover_zone=0, move=0.2)
        m.frame(held=True, real_hit=False, move=0.2)
        self.assertEqual(m.latch, 1.0)
        # 失臂（松 Alt）：无 dispatch，else 分支清锁存（模型经 disarm 表达）
        m.disarm()
        # 重新臂动 + 仍按住 LMB 但无命中：不得复活 zone0 绑定
        driven = m.frame(held=True, real_hit=False, move=0.2)
        self.assertEqual(driven, {0: False, 1: False})
        self.assertAlmostEqual(m.drive[0], 0.4)  # 保持失臂前的值，不被误驱动
        # 重新臂动 + 命中 zone1：正常绑定新区域
        driven = m.frame(held=True, real_hit=True, hover_zone=1, move=0.1)
        self.assertEqual(driven, {0: False, 1: True})

    def test_mode0_direct_jump_clears_latch_no_resurrection(self):
        """评审 G1：模式 1→0 直跳（无 dispatch、无臂动 else）→ Present 终态 else
        清锁存（模型与失臂同构，经 disarm 表达）；返回模式 1 后臂动+按住但无
        命中不得复活旧绑定。生成器侧 else 的存在性断言见
        test_node_postprocess_draginteraction.py 的 Present 终态 else 断言。"""
        m = DriveLatchModel(zone_count=2)
        # 模式 1 绑定 zone0 并拖了一段
        m.frame(held=True, real_hit=True, hover_zone=0, move=0.2)
        self.assertEqual(m.latch, 1.0)
        # 直跳模式 0：驱动 CS 不 dispatch，Present 终态 else 清锁存（同 disarm）
        m.disarm()
        # 滞留期间 ZoneActive 应已随锁存归 0（var_sync 镜像 0=未绑定）
        self.assertEqual(m.latch, 0.0)
        # 返回模式 1：臂动 + 按住但无命中 → 不得复活 zone0
        driven = m.frame(mode=1, held=True, real_hit=False, move=0.2)
        self.assertEqual(driven, {0: False, 1: False})
        self.assertAlmostEqual(m.drive[0], 0.2)  # 保持拖拽结果，不被误驱动
        # 臂动 + 命中 zone1 → 正常绑定新区域
        driven = m.frame(mode=1, held=True, real_hit=True, hover_zone=1, move=0.1)
        self.assertEqual(driven, {0: False, 1: True})


if __name__ == "__main__":
    unittest.main()
