import json
import os
import re
import shutil
import subprocess

import bpy

TOOLSET_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "Toolset")

DDS_DEFAULT_RULES = [
    {
        "texture_type": "DiffuseMap",
        "pattern": r"(?i)(?:^|[_\-. ])DiffuseMap(?:[_\-. ]|$)",
        "format": "bc7_unorm_srgb",
    },
    {
        "texture_type": "NormalMap",
        "pattern": r"(?i)(?:^|[_\-. ])NormalMap(?:[_\-. ]|$)",
        "format": "r8g8b8a8_unorm",
    },
    {
        "texture_type": "LightMap",
        "pattern": r"(?i)(?:^|[_\-. ])LightMap(?:[_\-. ]|$)",
        "format": "bc7_unorm_srgb",
    },
    {
        "texture_type": "MaterialMap",
        "pattern": r"(?i)(?:^|[_\-. ])MaterialMap(?:[_\-. ]|$)",
        "format": "bc7_unorm",
    },
    {
        "texture_type": "RampMap",
        "pattern": r"(?i)(?:^|[_\-. ])RampMap(?:[_\-. ]|$)",
        "format": "bc7_unorm_srgb",
    },
    {
        "texture_type": "HighLightMap",
        "pattern": r"(?i)(?:^|[_\-. ])HighLightMap(?:[_\-. ]|$)",
        "format": "bc7_unorm_srgb",
    },
    {
        "texture_type": "StockingMap",
        "pattern": r"(?i)(?:^|[_\-. ])StockingMap(?:[_\-. ]|$)",
        "format": "bc7_unorm",
    },
    {
        "texture_type": "Glowmap",
        "pattern": r"(?i)(?:^|[_\-. ])Glowmap(?:[_\-. ]|$)",
        "format": "bc7_unorm_srgb",
    },
    {
        "texture_type": "FXMap",
        "pattern": r"(?i)(?:^|[_\-. ])FXMap(?:[_\-. ]|$)",
        "format": "bc7_unorm",
    },
    {
        "texture_type": "TTLMap",
        "pattern": r"(?i)(?:^|[_\-. ])TTLMap(?:[_\-. ]|$)",
        "format": "bc7_unorm",
    },
    {
        "texture_type": "RoughnessMap",
        "pattern": r"(?i)(?:^|[_\-. ])RoughnessMap(?:[_\-. ]|$)",
        "format": "bc7_unorm",
    },
    {
        "texture_type": "ORMMap",
        "pattern": r"(?i)(?:^|[_\-. ])ORMMap(?:[_\-. ]|$)",
        "format": "bc7_unorm",
    },
]

_SUPPORTED_SOURCE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tga", ".bmp", ".tif", ".tiff"}


def find_texconv():
    props = bpy.context.scene.texture_tools_props

    local_path = os.path.join(TOOLSET_PATH, "texconv.exe")
    if os.path.exists(local_path):
        return local_path

    if props and props.texconv_path and os.path.exists(props.texconv_path):
        return props.texconv_path

    system_path = shutil.which("texconv")
    if system_path:
        return system_path

    return None


def _get_match_targets(filename: str) -> list[str]:
    basename = os.path.basename(filename or "")
    stem, _ext = os.path.splitext(basename)
    targets = []
    for value in (stem, basename):
        value = str(value or "").strip()
        if value and value not in targets:
            targets.append(value)
    return targets


def _pattern_matches(pattern: str, filename: str) -> bool:
    targets = _get_match_targets(filename)
    return any(re.search(pattern, target) for target in targets)


def _validate_custom_rules(props) -> list[str]:
    errors = []
    if not getattr(props, "dds_use_custom_rules", False):
        return errors

    for index, rule in enumerate(getattr(props, "dds_rules", []) or [], start=1):
        if not getattr(rule, "enabled", True):
            continue
        pattern = str(getattr(rule, "pattern", "") or "").strip()
        if not pattern:
            errors.append(f"规则 {index} 缺少正则表达式")
            continue
        try:
            re.compile(pattern)
        except re.error as exc:
            errors.append(f"规则 {index} 正则无效: {exc}")
    return errors


def _format_is_srgb(dds_format: str) -> bool:
    return "_srgb" in str(dds_format or "").strip().lower()


def _texconv_colorspace_flags() -> list[str]:
    """输入贴图一律按原始数值读取（--ignore-srgb）：渲染器/导出流程写出的
    PNG 装的就是最终值，转换只更换容器格式、不改变数值，保证转换前后颜色一致。
    （实测验证：渲染器输出的 PNG 为原始值直出，并非 sRGB 编码；用 --srgb-in
    会被误做一次 sRGB->线性解码，导致整体变暗。）"""
    return ["--ignore-srgb"]


def _apply_image_colorspace(image, dds_format: str):
    """Blender 内的显示色彩空间跟随输出文件的实际编码。"""
    try:
        image.colorspace_settings.name = "sRGB" if _format_is_srgb(dds_format) else "Non-Color"
    except Exception:
        pass


def resolve_dds_target(filename: str, props) -> tuple[str, str, str]:
    if getattr(props, "dds_use_custom_rules", False):
        for rule in getattr(props, "dds_rules", []) or []:
            if not getattr(rule, "enabled", True):
                continue
            pattern = str(getattr(rule, "pattern", "") or "").strip()
            if not pattern:
                continue
            try:
                if _pattern_matches(pattern, filename):
                    rule_format = str(getattr(rule, "format", "") or "").strip()
                    # 自定义规则完全由用户说了算：pattern 只负责识别名称，
                    # format 决定输出格式，不再从文件名推断类型做色彩空间干预
                    return "custom", rule_format or "bc7_unorm", pattern
            except re.error:
                continue

    targets = _get_match_targets(filename)
    best_rule = None
    best_start = float("inf")
    for rule in DDS_DEFAULT_RULES:
        try:
            pattern = rule["pattern"]
            for target in targets:
                match = re.search(pattern, target)
                if match:
                    if match.start() < best_start:
                        best_start = match.start()
                        best_rule = rule
                    break
        except re.error:
            continue

    if best_rule is not None:
        return best_rule["texture_type"], best_rule["format"], best_rule["texture_type"]

    return "default", "bc7_unorm", "Default"


class TT_OT_convert_to_dds(bpy.types.Operator):
    bl_idname = "toolkit.tt_convert_to_dds"
    bl_label = "批量转换为 .dds"
    bl_description = "使用 texconv.exe 将输出目录中的贴图转换或重编码为目标 DDS 格式，并更新图片引用。按原始数值直接转换（--ignore-srgb），不做色彩空间变换，保证颜色不变"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.texture_tools_props
        if not props.output_dir:
            self.report({"ERROR"}, "请先设置输出目录")
            return {"CANCELLED"}

        output_dir_abs = os.path.normpath(bpy.path.abspath(props.output_dir))
        if not os.path.isdir(output_dir_abs):
            self.report({"ERROR"}, f"输出目录不存在: {output_dir_abs}")
            return {"CANCELLED"}

        rule_errors = _validate_custom_rules(props)
        if rule_errors:
            self.report({"ERROR"}, "；".join(rule_errors))
            return {"CANCELLED"}

        texconv_executable = find_texconv()
        if not texconv_executable:
            self.report({"ERROR"}, "未找到 texconv.exe。请将其放入插件目录的 Toolset 子文件夹，或手动指定路径。")
            return {"CANCELLED"}

        supported_extensions = set(_SUPPORTED_SOURCE_EXTENSIONS)
        if props.dds_reencode_existing_dds:
            supported_extensions.add(".dds")

        conversion_map = {}
        converted_files_count = 0
        skipped_files_count = 0
        skipped_unmatched_dds_count = 0

        blend_dir = os.path.normpath(bpy.path.abspath("//"))

        for root, _dirs, files in os.walk(output_dir_abs):
            for filename in files:
                name_no_ext, ext = os.path.splitext(filename)
                ext_lower = ext.lower()
                if ext_lower not in supported_extensions:
                    continue

                old_path = os.path.normpath(os.path.join(root, filename))
                if not old_path.startswith(output_dir_abs):
                    self.report({"WARNING"}, f"跳过输出目录外的文件: {filename}")
                    continue

                if old_path.startswith(blend_dir) and not old_path.startswith(output_dir_abs):
                    self.report({"WARNING"}, f"跳过工程目录内的源文件: {filename}")
                    continue

                new_path = os.path.normpath(os.path.join(root, f"{name_no_ext}.dds"))
                texture_type, dds_format, _matched_by = resolve_dds_target(filename, props)

                if ext_lower == ".dds" and texture_type == "default":
                    skipped_unmatched_dds_count += 1
                    continue

                if not dds_format:
                    dds_format = "bc7_unorm"

                command = [texconv_executable, "-f", dds_format]
                command.extend(_texconv_colorspace_flags())
                command.extend(["-o", root, "-y", old_path])

                try:
                    process = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        check=True,
                        encoding="utf-8",
                        errors="ignore",
                    )
                except subprocess.CalledProcessError as exc:
                    stderr = (exc.stderr or exc.stdout or str(exc)).strip()
                    self.report({"WARNING"}, f"转换文件 {filename} 失败: {stderr}")
                    continue
                except Exception as exc:
                    self.report({"WARNING"}, f"处理文件 {filename} 时出错: {exc}")
                    continue

                if process.returncode != 0:
                    self.report({"WARNING"}, f"转换文件 {filename} 失败: {process.stderr}")
                    continue

                conversion_map[old_path] = (new_path, dds_format)
                converted_files_count += 1

                if props.dds_delete_originals and old_path != new_path:
                    try:
                        os.remove(old_path)
                    except Exception:
                        pass
                else:
                    skipped_files_count += 1 if old_path == new_path else 0

        if converted_files_count == 0:
            self.report({"INFO"}, "在输出目录中未找到可转换的贴图文件。")
            return {"CANCELLED"}

        updated_images_count = 0
        for image in bpy.data.images:
            if image.source != "FILE" or not image.filepath:
                continue
            try:
                abs_filepath = os.path.normpath(bpy.path.abspath(image.filepath_raw))
                if abs_filepath in conversion_map:
                    new_path, dds_format = conversion_map[abs_filepath]
                    image.filepath = new_path
                    image.reload()
                    _apply_image_colorspace(image, dds_format)
                    updated_images_count += 1
            except Exception as exc:
                self.report({"WARNING"}, f"更新图片 '{image.name}' 的路径时出错: {exc}")

        self.report(
            {"INFO"},
            f"成功处理 {converted_files_count} 个贴图文件，更新了 {updated_images_count} 个图片引用。"
            + (" 其中部分 DDS 为原地重编码。" if skipped_files_count else ""),
        )
        if skipped_unmatched_dds_count:
            self.report(
                {"INFO"},
                f"跳过了 {skipped_unmatched_dds_count} 个未命中任何 DDS 规则的现有 DDS 文件。",
            )
        return {"FINISHED"}


class TT_OT_add_dds_rule(bpy.types.Operator):
    bl_idname = "toolkit.tt_add_dds_rule"
    bl_label = "添加DDS规则"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.texture_tools_props
        rule = props.dds_rules.add()
        rule.pattern = ".*"
        rule.format = "bc7_unorm"
        rule.enabled = True
        props.dds_use_custom_rules = True
        return {"FINISHED"}


class TT_OT_remove_dds_rule(bpy.types.Operator):
    bl_idname = "toolkit.tt_remove_dds_rule"
    bl_label = "移除DDS规则"
    bl_options = {"REGISTER", "UNDO"}

    index: bpy.props.IntProperty()

    def execute(self, context):
        props = context.scene.texture_tools_props
        props.dds_rules.remove(self.index)
        return {"FINISHED"}


class TT_OT_reset_dds_rules(bpy.types.Operator):
    bl_idname = "toolkit.tt_reset_dds_rules"
    bl_label = "重置DDS规则"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.texture_tools_props
        props.dds_rules.clear()
        props.dds_use_custom_rules = True

        for rule_data in DDS_DEFAULT_RULES:
            rule = props.dds_rules.add()
            rule.pattern = rule_data["pattern"]
            rule.format = rule_data["format"]
            rule.enabled = True

        return {"FINISHED"}


class TT_OT_test_dds_rule(bpy.types.Operator):
    bl_idname = "toolkit.tt_test_dds_rule"
    bl_label = "测试DDS规则"
    bl_options = {"REGISTER"}

    def execute(self, context):
        props = context.scene.texture_tools_props

        rule_errors = _validate_custom_rules(props)
        if rule_errors:
            self.report({"ERROR"}, "；".join(rule_errors))
            return {"CANCELLED"}

        test_names = [
            "DiffuseMap_Body.png",
            "Body-DiffuseMap.dds",
            "NormalMap_Face.dds",
            "LightMap_Hair.tga",
            "MaterialMap_Armor.png",
            "Body-ORMMap.png",
            "RampMap_Eye.png",
            "HighLightMap_Hair.png",
            "StockingMap_Leg.bmp",
            "Glowmap_1_Eye.png",
            "FXMap_Body.dds",
            "RoughnessMap_Body.png",
            "UnknownMask.png",
        ]

        result_lines = ["DDS规则测试结果:"]
        for name in test_names:
            texture_type, matched_format, matched_by = resolve_dds_target(name, props)
            result_lines.append(f"  {name} -> {matched_format} ({texture_type} / {matched_by})")

        self.report({"INFO"}, "\n".join(result_lines))
        return {"FINISHED"}


class TT_OT_save_dds_rules(bpy.types.Operator):
    bl_idname = "toolkit.tt_save_dds_rules"
    bl_label = "保存DDS规则"
    bl_options = {"REGISTER"}

    def execute(self, context):
        props = context.scene.texture_tools_props

        if not props.dds_rules_file_path:
            self.report({"ERROR"}, "请先指定规则文件路径")
            return {"CANCELLED"}

        rules_data = []
        for rule in props.dds_rules:
            rules_data.append(
                {
                    "pattern": rule.pattern,
                    "format": rule.format,
                    "enabled": rule.enabled,
                }
            )

        try:
            with open(props.dds_rules_file_path, "w", encoding="utf-8") as file_obj:
                json.dump(rules_data, file_obj, indent=2, ensure_ascii=False)
            self.report({"INFO"}, f"规则已保存到: {props.dds_rules_file_path}")
        except Exception as exc:
            self.report({"ERROR"}, f"保存失败: {exc}")
            return {"CANCELLED"}

        return {"FINISHED"}


class TT_OT_load_dds_rules(bpy.types.Operator):
    bl_idname = "toolkit.tt_load_dds_rules"
    bl_label = "加载DDS规则"
    bl_options = {"REGISTER"}

    def execute(self, context):
        props = context.scene.texture_tools_props

        if not props.dds_rules_file_path:
            self.report({"ERROR"}, "请先指定规则文件路径")
            return {"CANCELLED"}

        if not os.path.exists(props.dds_rules_file_path):
            self.report({"ERROR"}, "规则文件不存在")
            return {"CANCELLED"}

        try:
            with open(props.dds_rules_file_path, "r", encoding="utf-8") as file_obj:
                rules_data = json.load(file_obj)

            props.dds_rules.clear()
            for rule_data in rules_data:
                rule = props.dds_rules.add()
                rule.pattern = rule_data.get("pattern", ".*")
                rule.format = rule_data.get("format", "bc7_unorm")
                rule.enabled = rule_data.get("enabled", True)
            props.dds_use_custom_rules = True

            self.report({"INFO"}, f"已加载 {len(rules_data)} 条规则")
        except Exception as exc:
            self.report({"ERROR"}, f"加载失败: {exc}")
            return {"CANCELLED"}

        return {"FINISHED"}


tt_dds_conversion_list = (
    TT_OT_convert_to_dds,
    TT_OT_add_dds_rule,
    TT_OT_remove_dds_rule,
    TT_OT_reset_dds_rules,
    TT_OT_test_dds_rule,
    TT_OT_save_dds_rules,
    TT_OT_load_dds_rules,
)
