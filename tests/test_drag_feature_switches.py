"""四功能开关条件生成 + R5 BakeSample 段合并 的回归测试。

依据 docs/drag-reload-audit/phase2/n1-链路制图与开关方案.md §9.1（S1-S11）
与 §9.2（R5）、docs/drag-reload-audit/phase2/t6-语法假设验证.md §4.3（变量作用域修复）。
覆盖：
- S1/S3/S9：四开关默认值裁决（形态键/变量/面板 ON、碰撞 OFF）、F4⇒F1 降级、旧值迁移；
- S4/S5/S6/S7/S8：关闭各开关 → 生成 ini 不含对应段族、对应 hlsl 不拷贝、反扫谓词同步；
- S10：_copy_shaders 按 F1/F4 过滤；
- R5：BakeSample 段数 8P→P、8 次参数化 run、[Constants] global 迭代变量、
  检测 CS 的 cs-t3 绑定不断链；
- 全开 → 与现状（legacy enable_shapekey_drive=True）基线等价（除 R5 合并段）。
"""
import re
import shutil
import tempfile
import types
import unittest
from collections import OrderedDict
from pathlib import Path

from tests.test_node_postprocess_draginteraction import (
    _base_sections,
    _load_drag_module,
    _make_node,
)


def _sk_node():
    """同树形态键消费方（drag_drive_enabled=True，方向形态键，属于 F1 消费链）。"""
    return types.SimpleNamespace(
        bl_idname="SSMTNode_PostProcess_ShapeKey",
        drag_drive_enabled=True,
        shapekey_variable_items=[
            types.SimpleNamespace(
                shape_key_name="A", drag_zone_id=0, drag_dir_id="0",
                drag_click_stage=1, export_enabled=True,
            ),
        ],
        get_shape_key_export_variable_name=lambda name: f"$Freq_{name}",
    )


class DragFeatureSwitchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()
        # 拖拽环境的 _ssmt_root.blueprint 指向真实 blueprint 目录，可加载真实
        # anim_driver_click_export（ClickExport 反扫谓词的兼容层）
        import importlib
        cls.click_mod = importlib.import_module(
            "_ssmt_root.blueprint.anim_driver_click_export"
        )

    def _emit_full(self, **props):
        """生成完整 sections（_emit_sections + _emit_present_and_constants）。"""
        node = _make_node(self.mod, **props)
        sections = _base_sections()
        comps = node._locate_components(sections, ["abc123"])
        node._emit_sections(sections, comps, "testns")
        node._emit_present_and_constants(sections, comps, "testns")
        return node, sections, comps

    @staticmethod
    def _all_text(sections):
        return "\n".join(
            "".join(lines) for key, lines in sections.items()
            if isinstance(lines, list)
        )

    # ------------------------------------------------------------------
    # S1/S3/S9：默认值与 F4⇒F1 降级
    # ------------------------------------------------------------------

    def test_feature_defaults_on_with_consumer_and_collision_off(self):
        sk = _sk_node()
        node = _make_node(self.mod)
        node.id_data = types.SimpleNamespace(nodes=[sk])
        # 档一：默认 ON = 能力声明，消费方存在时发射
        self.assertTrue(node._feature_skd(),
                        "形态键联动默认 ON（消费方存在）")
        self.assertTrue(node._feature_var(), "变量联动默认 ON")
        self.assertTrue(node._feature_panel(), "面板联动默认 ON = 现状恒发")
        # 碰撞维持现状 OFF（§4 裁决：不依赖现状 → 保持 OFF）
        self.assertFalse(node.collision_enabled)
        # 新 prop 已挂载到节点类（bpy props 经 __annotations__ 声明）
        cls = self.mod.SSMTNode_PostProcess_DragInteraction
        annotations = getattr(cls, "__annotations__", {}) or {}
        for attr in ("feature_shapekey_link", "feature_variable_link",
                     "feature_panel_link"):
            self.assertIn(attr, annotations, attr)

    def test_feature_skd_off_without_consumer_and_no_legacy(self):
        # 档一：新建空工程（无消费方、无旧值）默认 ON 也视为未配置 → 不发射
        node = _make_node(self.mod)
        self.assertFalse(node._feature_skd())
        self.assertFalse(node._feature_var())
        self.assertTrue(node._feature_panel())

    def test_legacy_enable_shapekey_drive_keeps_emission(self):
        # 迁移期：旧开关显式开启 → 无条件 ON（既有工程导出不变）
        node = _make_node(self.mod, enable_shapekey_drive=True)
        self.assertTrue(node._feature_skd())
        self.assertTrue(node._feature_var())
        node2 = _make_node(self.mod, enable_shapekey_drive=False)
        self.assertFalse(node2._feature_skd())

    def test_var_downgrade_when_shapekey_link_off(self):
        sk = _sk_node()
        node = _make_node(self.mod, feature_shapekey_link=False)
        node.id_data = types.SimpleNamespace(nodes=[sk])
        self.assertFalse(node._feature_skd())
        # F4 ⇒ F1 硬依赖：形态键联动关 → 变量联动强制降级
        self.assertFalse(node._feature_var())
        self.assertIsNone(node.validate_export_configuration())

    def test_explicit_switches_off(self):
        sk = _sk_node()
        node = _make_node(self.mod, feature_shapekey_link=False,
                          feature_variable_link=False, feature_panel_link=False)
        node.id_data = types.SimpleNamespace(nodes=[sk])
        self.assertFalse(node._feature_skd())
        self.assertFalse(node._feature_var())
        self.assertFalse(node._feature_panel())

    # ------------------------------------------------------------------
    # S4/S5：F1/F4 关闭 → 段族消失
    # ------------------------------------------------------------------

    def test_shapekey_link_off_emits_no_f1_family(self):
        sk = _sk_node()
        node = _make_node(self.mod, feature_shapekey_link=False)
        node.id_data = types.SimpleNamespace(nodes=[sk])
        sections = _base_sections()
        comps = node._locate_components(sections, ["abc123"])
        node._emit_sections(sections, comps, "testns")
        node._emit_present_and_constants(sections, comps, "testns")
        text = self._all_text(sections)
        for token in (
            "ResourceDragShapeKeyDrive_",
            "ResourceDragShapeKeyDragLatch_",
            "CustomShaderDragShapeKeyDrive_",
            "$ssmtdrag_shapekey_dy_",
        ):
            self.assertNotIn(token, text)
        present = "\n".join(sections["[Present]"])
        self.assertNotIn("clear = ResourceDragShapeKeyDragLatch_testns", present)
        # F4 连带降级：无 VarSync/readback/seed
        self.assertNotIn("CustomShaderDragShapeKeyVarSync_testns", text)
        self.assertNotIn("CommandListDragShapeKeyVarReadback_testns", text)
        const = "\n".join(sections["[Constants]"])
        self.assertNotIn("$ssmtdrag_seed_pending_testns", const)
        # S8：ClickExport 反扫谓词随 F1/F4 关闭返回 False
        self.assertFalse(self.click_mod._drag_drive_feature_linked(node))

    def test_variable_link_off_keeps_f1_drops_f4(self):
        sk = _sk_node()
        node = _make_node(self.mod, feature_variable_link=False)
        node.id_data = types.SimpleNamespace(nodes=[sk])
        sections = _base_sections()
        comps = node._locate_components(sections, ["abc123"])
        node._emit_sections(sections, comps, "testns")
        node._emit_present_and_constants(sections, comps, "testns")
        # F1 保留
        self.assertIn("[ResourceDragShapeKeyDrive_testns]", sections)
        self.assertIn("[CustomShaderDragShapeKeyDrive_testns]", sections)
        drive = "\n".join(sections["[CustomShaderDragShapeKeyDrive_testns]"])
        # F4 全部消失
        text = self._all_text(sections)
        for token in (
            "CustomShaderDragShapeKeyVarSync_testns",
            "CommandListDragShapeKeyVarReadback_testns",
            "ResourceDragShapeKeyVarPrev_testns",
            "ResourceDragShapeKeyVarSyncMap_testns",
            "ResourceDragShapeKeyZoneActive_testns",
        ):
            self.assertNotIn(token, text)
        present = "\n".join(sections["[Present]"])
        # F4 关时 :4707 pre run 不再引用已删 Readback 段（任务书点名风险实例）
        self.assertNotIn("pre run = CommandListDragShapeKeyVarReadback_testns", present)
        self.assertNotIn("run = CustomShaderDragShapeKeyVarSync_testns", present)
        const = "\n".join(sections["[Constants]"])
        self.assertNotIn("$ssmtdrag_seed_pending_testns", const)
        # 驱动 CS 不携带 F4 播种行
        self.assertNotIn("y80 = $ssmtdrag_seed_pending_testns", drive)
        # S8：ClickExport 反扫为 False
        self.assertFalse(self.click_mod._drag_drive_feature_linked(node))
        # S7：F1 仍在 → 形态键侧谓词有效（真实模块路径由
        # test_node_postprocess_shapekey_dragdrive 覆盖）
        self.assertTrue(node._feature_skd())

    # ------------------------------------------------------------------
    # S6：F3 关闭 → UI 桥消失
    # ------------------------------------------------------------------

    def test_panel_link_off_emits_no_ui_bridge(self):
        node, sections, _ = self._emit_full(feature_panel_link=False)
        const = "\n".join(sections["[Constants]"])
        self.assertNotIn("$ssmtdrag_ui_detected_testns", const)
        self.assertNotIn("$ssmtdrag_ui_zone_testns", const)
        self.assertNotIn("[CommandListDragUIReadback_testns]", sections)
        present = "\n".join(sections["[Present]"])
        self.assertNotIn("post run = CommandListDragUIReadback_testns", present)
        self.assertFalse(node._feature_panel())

    # ------------------------------------------------------------------
    # S10：_copy_shaders 按 F1/F4 过滤 hlsl
    # ------------------------------------------------------------------

    def test_copy_shaders_gates_drive_and_varsync_files(self):
        toolset = Path(tempfile.mkdtemp(prefix="drag_toolset_"))
        all_files = (
            "rzm_gs_probe.hlsl", "rzm_object_detect.hlsl",
            "rzm_pin_detected.hlsl", "rzm_jiggle_screen_state.hlsl",
            "rzm_jiggle_interaction.hlsl", "rzm_vis_publish.hlsl",
            "rzm_shapekey_drive.hlsl", "rzm_shapekey_var_sync.hlsl",
        )
        for fname in all_files:
            (toolset / fname).write_text(
                "struct VertexAttributes { float3 position; };\n", encoding="utf-8")
        base = set(all_files[:6])
        drive = {"rzm_shapekey_drive.hlsl"}
        var_sync = {"rzm_shapekey_var_sync.hlsl"}
        try:
            def _run(node):
                out = Path(tempfile.mkdtemp(prefix="drag_res_"))
                node._get_toolset_dir = lambda: str(toolset)
                node._get_vertex_struct_definition = (
                    lambda: self.mod.DEFAULT_VERTEX_STRUCT)
                node._copy_shaders(str(out))
                return {p.name for p in out.glob("*.hlsl")}

            sk = _sk_node()
            # F1/F4 全关 → 仅基础 6 个
            off = _make_node(self.mod, feature_shapekey_link=False,
                             feature_variable_link=False)
            off.id_data = types.SimpleNamespace(nodes=[sk])
            self.assertEqual(_run(off), base)
            # F1 开 F4 关 → 追加 drive、不含 var_sync
            drive_only = _make_node(self.mod, feature_variable_link=False)
            drive_only.id_data = types.SimpleNamespace(nodes=[sk])
            self.assertEqual(_run(drive_only), base | drive)
            # 全开 → 8 个全拷贝（含两文件均走 struct 替换路径）
            on = _make_node(self.mod)
            on.id_data = types.SimpleNamespace(nodes=[sk])
            self.assertEqual(_run(on), base | drive | var_sync)
        finally:
            shutil.rmtree(toolset, ignore_errors=True)

    # ------------------------------------------------------------------
    # R5：BakeSample 8P → P
    # ------------------------------------------------------------------

    def test_r5_bake_sample_segment_count_p_and_eight_runs(self):
        node, sections, comps = self._emit_full()
        sample_keys = [k for k in sections if "CustomShaderDragBakeSample" in k]
        self.assertEqual(
            sample_keys,
            [
                "[CustomShaderDragBakeSample_abc123_43191P0_testns]",
                "[CustomShaderDragBakeSample_abc123_43191P1_testns]",
            ],
            "BakeSample 段数应从 8P 降到 P",
        )
        for p_idx in (0, 1):
            part_tag = f"abc123_43191P{p_idx}"
            bake = sections[f"[CustomShaderDragBake{part_tag}_testns]"]
            runs = [line for line in bake
                    if line == f"run = CustomShaderDragBakeSample_{part_tag}_testns"]
            self.assertEqual(len(runs), 8, f"{part_tag} bake 段 8 次参数化 run")
            i_var = f"$ssmtdrag_bake_i_{part_tag}_testns"
            self.assertIn(f"{i_var} = 0", bake)
            self.assertIn(f"{i_var} = {i_var} + 1", bake)
            sample = sections[f"[CustomShaderDragBakeSample_{part_tag}_testns]"]
            off_var = f"$ssmtdrag_bake_off_{part_tag}_testns"
            self.assertIn(f"local {off_var}", sample)
            self.assertIn(
                f"{off_var} = $ssmtdrag_bake_base_{part_tag}_testns + "
                f"{i_var} * $ssmtdrag_bake_step_{part_tag}_testns",
                sample,
            )
            self.assertIn(f"x26 = {i_var}", sample)
            self.assertIn(f"y26 = {off_var}", sample)
            self.assertIn(f"drawindexed = 1, {off_var}, 0", sample)
            self.assertIn("gs-t1 = Resourceabc123-43191" + ("A" if p_idx == 0 else "B") + "IB", sample)
        # 8 组参数在单段内由 global 迭代变量推导（t6 §4.3：段间仅 global 可见）
        const = "\n".join(sections["[Constants]"])
        for var_base in ("base", "step", "i"):
            self.assertIn(
                f"global $ssmtdrag_bake_{var_base}_abc123_43191P0_testns", const)
        # 检测 CS 的 cs-t3 绑定不断链（BakeRT 仍是同一个）
        detect = "\n".join(sections["[CustomShaderDragDetectabc123_43191P0_testns]"])
        self.assertIn("cs-t3 = ResourceDragBakeRT_testns", detect)
        # 钩子消费路径不变：仍 run bake 段
        node._inject_draw_hooks(sections, comps[0], "testns")
        hook = "\n".join(sections["[TextureOverride_abc123_abc123-43191A]"])
        self.assertIn("run = CustomShaderDragBakeabc123_43191P0_testns", hook)

    # ------------------------------------------------------------------
    # 全开 = 现状基线等价（除 R5 合并段）
    # ------------------------------------------------------------------

    def test_full_open_matches_legacy_baseline_except_r5_merge(self):
        sk = _sk_node()
        legacy = _make_node(self.mod, enable_shapekey_drive=True)
        legacy.id_data = types.SimpleNamespace(nodes=[sk])
        full = _make_node(self.mod)
        full.id_data = types.SimpleNamespace(nodes=[sk])

        legacy_sections = OrderedDict(
            (k, list(v)) for k, v in _base_sections().items())
        full_sections = OrderedDict(
            (k, list(v)) for k, v in _base_sections().items())
        legacy_comps = legacy._locate_components(legacy_sections, ["abc123"])
        full_comps = full._locate_components(full_sections, ["abc123"])
        legacy._emit_sections(legacy_sections, legacy_comps, "testns")
        legacy._emit_present_and_constants(legacy_sections, legacy_comps, "testns")
        full._emit_sections(full_sections, full_comps, "testns")
        full._emit_present_and_constants(full_sections, full_comps, "testns")

        old_sample_re = re.compile(r"CustomShaderDragBakeSample\d_")
        r5_keys = {
            k for k in list(legacy_sections) + list(full_sections)
            if old_sample_re.search(k)
        }
        r5_keys.update([
            "[CustomShaderDragBakeSample_abc123_43191P0_testns]",
            "[CustomShaderDragBakeSample_abc123_43191P1_testns]",
            "[CustomShaderDragBakeabc123_43191P0_testns]",
            "[CustomShaderDragBakeabc123_43191P1_testns]",
            "[Constants]",
        ])
        # 除 R5 合并段外：段集合与逐段内容逐字节等价
        common_new = [k for k in full_sections if k not in r5_keys]
        common_legacy = [k for k in legacy_sections if k not in r5_keys]
        self.assertEqual(common_new, common_legacy)
        for k in common_new:
            self.assertEqual(legacy_sections[k], full_sections[k], k)
        # 段数/资源数不减（R5 只减 BakeSample 段）
        self.assertGreaterEqual(
            len(full_sections) - len(set(full_sections) & r5_keys),
            len(legacy_sections) - len(set(legacy_sections) & r5_keys),
        )
        # 旧 8 段全部消失
        self.assertFalse(any(old_sample_re.search(k) for k in full_sections))

    # ------------------------------------------------------------------
    # F2 碰撞：x101-104 = 0 安全底线保留
    # ------------------------------------------------------------------

    def test_collision_disabled_keeps_zero_param_else_branch(self):
        node, sections, _ = self._emit_full()  # collision_enabled 默认 False
        jig = "\n".join(sections["[CustomShaderDragJiggleabc123_43191_testns]"])
        for line in ("x101 = 0", "x102 = 0", "x103 = 0", "x104 = 0"):
            self.assertIn(line, jig,
                          "关闭碰撞的 else 分支（x101-104=0）是安全底线，禁止删除")


# ----------------------------------------------------------------------
# t11：旧「形态键驱动输出」复选框并入「启用形态键联动」单控件单事实源
# ----------------------------------------------------------------------


class _RecordingUI:
    """吞掉 draw_buttons 的一切链式 UI 调用，记录 prop 属性名与 label 文本。"""

    def __init__(self, log):
        object.__setattr__(self, "_log", log)

    def __getattr__(self, name):
        log = self._log
        if name == "prop":
            def _prop(data, attr, **kw):
                log["props"].append(attr)
            return _prop
        if name == "label":
            def _label(*args, **kw):
                log["labels"].append(args[0] if args else kw.get("text", ""))
            return _label

        def _passthrough(*args, **kw):
            return self
        return _passthrough

    def __setattr__(self, key, value):
        pass


class DragDriveSwitchMergeTests(unittest.TestCase):
    """旧开关 enable_shapekey_drive 不再单独渲染；输出配置项随 _feature_skd() 显隐。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()

    def setUp(self):
        # draw_buttons 末尾幂等启动预览 timer，桩环境里打掉
        self.addCleanup(setattr, self.mod, "_ensure_preview_running",
                        self.mod._ensure_preview_running)
        self.mod._ensure_preview_running = lambda: None

    def _draw(self, node, with_consumer=True):
        object.__setattr__(node, "name", "Drag")
        object.__setattr__(node, "preview_weights", False)  # draw 只读路径，stub 无类属性
        nodes = [_sk_node()] if with_consumer else []
        node.id_data = types.SimpleNamespace(nodes=nodes, name="Tree")
        log = {"props": [], "labels": []}
        node.draw_buttons(None, _RecordingUI(log))
        return log

    def test_single_switch_row_no_legacy_prop(self):
        log = self._draw(_make_node(self.mod))
        self.assertNotIn("enable_shapekey_drive", log["props"],
                         "旧「形态键驱动输出」复选框不再单独渲染")
        self.assertEqual(log["props"].count("feature_shapekey_link"), 1,
                         "「启用形态键联动」是唯一可见开关")
        self.assertIn("shapekey_drive_move_sensitivity", log["props"],
                      "F1 生效时位移灵敏度等输出配置仍渲染")

    def test_drive_box_configs_hidden_when_f1_inactive(self):
        log = self._draw(_make_node(self.mod), with_consumer=False)
        self.assertNotIn("enable_shapekey_drive", log["props"])
        self.assertNotIn("shapekey_drive_move_sensitivity", log["props"],
                         "F1 未生效（无消费方）时输出配置不渲染")
        self.assertTrue(any("未生效" in t for t in log["labels"]),
                        "F1 未生效时驱动 box 给出去向提示")

    def test_legacy_true_still_drives_configs_without_row(self):
        node = _make_node(self.mod, enable_shapekey_drive=True)
        self.assertTrue(node._feature_skd(), "旧显式 True 兼容分支无条件 ON")
        log = self._draw(node)
        self.assertIn("shapekey_drive_move_sensitivity", log["props"])
        self.assertNotIn("enable_shapekey_drive", log["props"])

    def test_migration_still_intact_after_merge(self):
        node = _make_node(self.mod, enable_shapekey_drive=True)
        node.id_data = types.SimpleNamespace(nodes=[_sk_node()], name="Tree")
        self.assertTrue(node._feature_skd())
        node._migrate_feature_defaults()
        self.assertFalse(node.enable_shapekey_drive, "迁移后旧开关退位")
        self.assertTrue(node.feature_shapekey_link, "旧 True 继承进新开关")
        self.assertTrue(node._feature_skd(), "迁移前后 F1 行为不变（有消费方）")
        node._migrate_feature_defaults()  # 幂等
        self.assertTrue(node.feature_shapekey_link)


if __name__ == "__main__":
    unittest.main()