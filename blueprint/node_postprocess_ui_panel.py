import bpy
import os
import glob
import hashlib
import re
import shutil
import zipfile
from collections import OrderedDict

from .node_postprocess_base import SSMTNode_PostProcess_Base


class SSMTNode_PostProcess_UIPanel(SSMTNode_PostProcess_Base):
    bl_idname = 'SSMTNode_PostProcess_UIPanel'
    bl_label = 'UI面板注入'
    bl_description = '将 UI 构造器（网页）生成的 INI 配置与 res/font 资源自动追加到导出配置表'

    UI_PANEL_MARKER_PREFIX = "; --- AUTO-APPENDED UI PANEL"
    # 这些段是共享定义，多个面板/后处理节点复用同一份，不做改名或重复追加。
    SHARED_SECTION_NAMES = frozenset({
        "Constants",
        "CustomShaderDraw",
        "CustomShaderFx",
    })

    panel_name: bpy.props.StringProperty(
        name="面板名称",
        default="UIPanel",
        description="面板标识，用于生成去重标记。多个 UI 面板注入节点必须使用不同的名称",
    )
    panel_folder: bpy.props.StringProperty(
        name="面板目录",
        subtype='DIR_PATH',
        default="",
        description="网页导出目录：包含 ui_config_*.txt（自动取日期最新的一份）以及 ui_assets_*.zip（自动解压到模组目录）或已解压的 res/、font/ 子目录",
    )
    merge_constants: bpy.props.BoolProperty(
        name="合并全局变量",
        default=True,
        description="将面板 [Constants] 中的 global 声明合并到配置表；"
        "配置表已声明的同名变量以配置表为准（初始值不被面板覆盖）",
    )

    def draw_buttons(self, context, layout):
        layout.prop(self, "panel_name")
        layout.prop(self, "panel_folder")
        layout.prop(self, "merge_constants")
        layout.separator()
        layout.label(text="自动使用目录中最新的配置文件", icon='FILE_REFRESH')
        layout.label(text="（ui_config_*.txt 按日期取最新）")
        layout.label(text="检测到 ui_assets_*.zip 时自动取最新一份解压", icon='PACKAGE')
        layout.separator()
        layout.label(text="导出时会自动：", icon='EXPORT')
        layout.label(text="1. 追加面板 INI 段到配置表")
        layout.label(text="2. 解压/复制 res 与 font 资源到模组目录")

    # ------------------------------------------------------------------
    # 标记与块管理
    # ------------------------------------------------------------------

    def get_panel_marker(self) -> str:
        name = str(getattr(self, "panel_name", "") or "").strip() or "UIPanel"
        name = " ".join(name.splitlines()).strip() or "UIPanel"
        return f"{self.UI_PANEL_MARKER_PREFIX} {name} ---"

    @classmethod
    def _is_known_append_marker(cls, line: str) -> bool:
        stripped = str(line or "").strip()
        if stripped.startswith(cls.UI_PANEL_MARKER_PREFIX):
            return True
        return stripped in cls.AUTO_APPENDED_SECTION_MARKERS

    def _split_own_block(self, content: str):
        """按自己的标记切分：返回 (保留内容, 本节点旧块)。

        旧块 = 从本节点标记（含标记前一行分隔线）到下一个已知追加标记或文件末尾。
        """
        lines = content.splitlines(keepends=True)
        marker = self.get_panel_marker()
        start_index = None
        for index, line in enumerate(lines):
            if marker in line:
                start_index = index
                break
        if start_index is None:
            return content, ""
        marker_index = start_index
        # 标记前一行通常是本块的分隔注释行
        if start_index > 0 and lines[start_index - 1].strip().startswith(";"):
            start_index -= 1
        end_index = len(lines)
        for index in range(marker_index + 1, len(lines)):
            if self._is_known_append_marker(lines[index]):
                end_index = index
                break
        kept = "".join(lines[:start_index] + lines[end_index:])
        removed = "".join(lines[start_index:end_index])
        return kept, removed

    @classmethod
    def _split_appended_tail(cls, content: str):
        lines = content.splitlines(keepends=True)
        for index, line in enumerate(lines):
            if cls._is_known_append_marker(line):
                return "".join(lines[:index]), "".join(lines[index:])
        return content, ""

    # ------------------------------------------------------------------
    # INI 解析
    # ------------------------------------------------------------------

    @staticmethod
    def parse_sections(text: str) -> "OrderedDict[str, list[str]]":
        sections = OrderedDict()
        current_section = None
        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()
            if stripped.startswith('[') and stripped.endswith(']') and len(stripped) > 2:
                current_section = stripped[1:-1]
                sections.setdefault(current_section, [])
            elif current_section is not None:
                sections[current_section].append(line)
        return sections

    @staticmethod
    def _extract_preamble(text: str) -> list[str]:
        preamble = []
        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if stripped.startswith('[') and stripped.endswith(']') and len(stripped) > 2:
                break
            preamble.append(raw_line.rstrip())
        return preamble

    @staticmethod
    def _strip_inline_comment(value: str) -> str:
        quote = ""
        for index, char in enumerate(str(value or "")):
            if char in "\"'":
                if not quote:
                    quote = char
                elif quote == char:
                    quote = ""
            elif char == ';' and not quote:
                return value[:index]
        return value

    @staticmethod
    def _extract_referenced_files(sections) -> list:
        """收集 INI 中引用的相对资源路径。

        覆盖 Resource 段的 filename = ./res/x.png、着色器段的 vs/ps = ./res/x.hlsl。
        仅保留带扩展名的相对路径；跳过 null（3DMigoto 着色器槽的"禁用"值）与绝对/协议路径。
        """
        referenced = []
        path_pattern = re.compile(r'^(?:filename|vs|ps|hs|ds|gs|cs)\s*=\s*(.+)$', re.IGNORECASE)
        for section_lines in sections.values():
            for line in section_lines:
                match = path_pattern.match(line.strip())
                if not match:
                    continue
                raw_path = SSMTNode_PostProcess_UIPanel._strip_inline_comment(match.group(1))
                raw_path = raw_path.strip().strip('"').strip("'")
                if not raw_path or raw_path.casefold() == 'null':
                    continue
                if re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', raw_path):
                    continue  # 协议/盘符绝对路径，不作为相对资源处理
                if not re.search(r'\.[A-Za-z0-9]+$', raw_path):
                    continue  # 非文件路径取值
                referenced.append(raw_path)
        return referenced

    @staticmethod
    def _resolve_reference(root_dir: str, ref_path: str) -> str:
        if os.path.isabs(ref_path):
            return os.path.normpath(ref_path)
        return os.path.normpath(os.path.join(root_dir, ref_path))

    @staticmethod
    def _resolve_bounded_path(root_dir: str, relative_path: str, description: str) -> str:
        if os.path.isabs(relative_path):
            raise ValueError(f"{description}包含非法绝对路径: {relative_path}")

        root_path = os.path.realpath(os.path.abspath(root_dir))
        target_path = os.path.realpath(os.path.abspath(os.path.join(root_path, relative_path)))
        try:
            is_within_root = os.path.normcase(os.path.commonpath([root_path, target_path])) == os.path.normcase(root_path)
        except ValueError:
            is_within_root = False
        if not is_within_root:
            raise ValueError(f"{description}包含非法路径: {relative_path}")
        return target_path

    # ------------------------------------------------------------------
    # 合并
    # ------------------------------------------------------------------

    def _merge_panel_sections(self, target_sections: "OrderedDict[str, list[str]]",
                              panel_sections: "OrderedDict[str, list[str]]",
                              merge_constants: bool = True):
        """把面板段合并进目标段集合，返回 (追加顺序的段名列表, 警告列表)。"""
        warnings = []
        append_order = []
        for section_name, section_lines in panel_sections.items():
            if section_name in target_sections:
                if section_name == "Constants" and merge_constants:
                    self._merge_global_lines(target_sections["Constants"], section_lines)
                    continue
                if section_name in self.SHARED_SECTION_NAMES:
                    if target_sections[section_name] == section_lines:
                        warnings.append(f"段 [{section_name}] 已存在且内容相同，跳过重复追加")
                    else:
                        warnings.append(
                            f"段 [{section_name}] 已存在但内容不同，保留配置表中的现有定义"
                        )
                    continue
                if section_name == "Present":
                    self._replace_present_block(target_sections["Present"], section_lines)
                    warnings.append("面板 [Present] 逻辑已合并进配置表已有的 [Present]")
                    continue
                raise ValueError(
                    f"UI面板段 [{section_name}] 与配置表已有段重名。"
                    "同一个网页预设应包含所有面板（组件编号会自动递增）；"
                    "如需多个独立面板配置，请分别导出并在网页中合并，或修改段名避免冲突。"
                )
            target_sections[section_name] = list(section_lines)
            append_order.append(section_name)
        return append_order, warnings

    def _present_block_markers(self):
        panel_name = str(getattr(self, "panel_name", "") or "UIPanel").strip() or "UIPanel"
        panel_id = hashlib.sha1(panel_name.encode("utf-8")).hexdigest()[:12]
        return (
            f"; --- SSMT UI PANEL PRESENT {panel_id} BEGIN ---",
            f"; --- SSMT UI PANEL PRESENT {panel_id} END ---",
        )

    def _replace_present_block(self, target_lines: list, panel_lines: list):
        begin_marker, end_marker = self._present_block_markers()
        cleaned = []
        inside_own_block = False
        for line in target_lines:
            stripped = str(line or "").strip()
            if stripped == begin_marker:
                inside_own_block = True
                continue
            if inside_own_block:
                if stripped == end_marker:
                    inside_own_block = False
                continue
            cleaned.append(line)

        # 兼容首次引入归属标记前生成的配置：删除完全相同的旧面板 Present 片段。
        old_block = list(panel_lines)
        if old_block:
            index = 0
            while index <= len(cleaned) - len(old_block):
                if cleaned[index:index + len(old_block)] == old_block:
                    del cleaned[index:index + len(old_block)]
                    continue
                index += 1

        if cleaned and cleaned[-1].strip():
            cleaned.append("")
        cleaned.append(begin_marker)
        cleaned.extend(panel_lines)
        cleaned.append(end_marker)
        target_lines[:] = cleaned

    # 匹配 global 声明中的变量名，如 "global persist $active = 1" 中的 "$active"
    _GLOBAL_DECLARATION_PATTERN = re.compile(
        r'^\s*global\s+(?:persist\s+)?(\$[A-Za-z0-9_]+)', re.IGNORECASE
    )

    @classmethod
    def _merge_global_lines(cls, target_lines: list, panel_lines: list):
        """合并面板 [Constants] 的 global 声明到配置表，以主配置表为准。

        主配置表（蓝图输出节点生成的 INI）已声明的变量，面板中的同名声明一律
        跳过——即使初始值不同，也保留主配置表的定义；面板独有的变量声明才追加。
        """
        existing_names = set()
        for line in target_lines:
            match = cls._GLOBAL_DECLARATION_PATTERN.match(line)
            if match:
                existing_names.add(match.group(1))
        for line in panel_lines:
            match = cls._GLOBAL_DECLARATION_PATTERN.match(line)
            if not match:
                continue
            var_name = match.group(1)
            if var_name in existing_names:
                continue
            target_lines.append(line)
            existing_names.add(var_name)

    # ------------------------------------------------------------------
    # 导出执行
    # ------------------------------------------------------------------

    # 网页导出的配置命名：ui_config_<hash>_<毫秒时间戳>.txt，按末尾时间戳判断新旧
    _UI_CONFIG_TIMESTAMP_PATTERN = re.compile(
        r'_(\d{10,})(?=\.(?:txt|ini)$)', re.IGNORECASE
    )

    def _find_panel_ini_path(self) -> str:
        """在面板目录中查找日期最新的一份面板配置文件。

        仅匹配网页导出的 ui_config_*.txt/.ini（目录里可能有其他文件，严格限前缀）。
        候选文件按文件名自带的时间戳取最新；没有时间戳的文件退回用修改时间比较。
        """
        folder = self._get_panel_folder()
        if not folder or not os.path.isdir(folder):
            raise ValueError("UI面板注入节点未设置有效的面板目录 (panel_folder)")

        candidates = []
        for pattern in ("ui_config_*.txt", "ui_config_*.ini"):
            candidates.extend(glob.glob(os.path.join(folder, pattern)))
        if not candidates:
            raise ValueError(f"面板目录中未找到 INI 配置文件: {folder}")

        def sort_key(path):
            match = self._UI_CONFIG_TIMESTAMP_PATTERN.search(os.path.basename(path))
            if match:
                return (1, float(match.group(1)))
            try:
                return (0, os.path.getmtime(path))
            except OSError:
                return (0, 0.0)

        return max(candidates, key=sort_key)

    # 网页导出的资源压缩包命名：ui_assets_<毫秒时间戳>.zip，按文件名时间戳判断新旧；
    # 目录里可能有其他文件，压缩包与配置文件一样严格限前缀
    _UI_ASSETS_TIMESTAMP_PATTERN = re.compile(
        r'_(\d{10,})(?=\.zip$)', re.IGNORECASE
    )
    # 压缩包内的配置文件条目只读取不解压，不作为模组资源写出
    _UI_CONFIG_ENTRY_PATTERN = re.compile(r'^ui_config_.*\.(?:txt|ini)$', re.IGNORECASE)

    def _get_panel_folder(self) -> str:
        folder = str(getattr(self, "panel_folder", "") or "").strip()
        if not folder:
            return ""
        try:
            return bpy.path.abspath(folder)
        except Exception:
            return folder

    def _find_ui_assets_zip_path(self) -> str:
        """在面板目录中查找日期最新的资源压缩包。

        仅匹配网页导出的 ui_assets_*.zip（目录里可能有其他文件，严格限前缀）；
        优先按文件名自带的时间戳取最新，没有时间戳的退回用修改时间比较。
        """
        folder = self._get_panel_folder()
        if not folder or not os.path.isdir(folder):
            return ""
        candidates = glob.glob(os.path.join(folder, "ui_assets_*.zip"))
        if not candidates:
            return ""

        def sort_key(path):
            match = self._UI_ASSETS_TIMESTAMP_PATTERN.search(os.path.basename(path))
            if match:
                return (1, float(match.group(1)))
            try:
                return (0, os.path.getmtime(path))
            except OSError:
                return (0, 0.0)

        return max(candidates, key=sort_key)

    def _load_panel_config_text(self, zip_path: str):
        """读取面板配置文本，返回 (文本内容, 来源描述)。

        优先使用面板目录中松散的 ui_config_*.txt/.ini；目录中没有时，
        改从 ui_assets_*.zip 压缩包内读取日期最新的一份配置条目。
        """
        folder = self._get_panel_folder()
        if not folder or not os.path.isdir(folder):
            raise ValueError("UI面板注入节点未设置有效的面板目录 (panel_folder)")
        try:
            panel_ini_path = self._find_panel_ini_path()
        except ValueError:
            panel_ini_path = ""
        if panel_ini_path:
            with open(panel_ini_path, 'r', encoding='utf-8-sig') as f:
                return f.read(), os.path.basename(panel_ini_path)
        if zip_path:
            entry_name = self._find_latest_config_entry_in_zip(zip_path)
            if entry_name:
                with zipfile.ZipFile(zip_path, 'r') as zfile:
                    text = zfile.read(entry_name).decode('utf-8-sig')
                return text, f"{os.path.basename(zip_path)} 内的 {entry_name}"
        raise ValueError(f"面板目录中未找到 INI 配置文件: {folder}")

    def _find_latest_config_entry_in_zip(self, zip_path: str) -> str:
        """在压缩包内查找日期最新的 ui_config_*.txt/.ini 条目（严格限前缀）。"""
        with zipfile.ZipFile(zip_path, 'r') as zfile:
            entry_names = [
                str(name).replace('\\', '/')
                for name in zfile.namelist()
                if not str(name).endswith('/')
            ]

        def base_name(name):
            return name.rsplit('/', 1)[-1]

        candidates = [
            name for name in entry_names
            if self._UI_CONFIG_ENTRY_PATTERN.match(base_name(name))
        ]
        if not candidates:
            return ""

        def sort_key(name):
            match = self._UI_CONFIG_TIMESTAMP_PATTERN.search(base_name(name))
            if match:
                return (1, float(match.group(1)))
            return (0, 0.0)

        return max(candidates, key=sort_key)

    @staticmethod
    def _normalize_reference_path(path) -> str:
        normalized = str(path or "").replace('\\', '/').strip()
        while normalized.startswith('./'):
            normalized = normalized[2:]
        return normalized

    def _extract_ui_assets_zip(self, zip_path: str, mod_export_path: str, panel_sections) -> int:
        """把 ui_assets 压缩包解压到模组导出目录，返回解压的文件数。

        解压前校验 INI 引用的资源都在压缩包内；配置条目（ui_config_*.txt/.ini）
        不解压；拒绝越出目标目录的条目（zip slip）。
        """
        referenced = {
            self._normalize_reference_path(ref)
            for ref in self._extract_referenced_files(panel_sections)
        }
        target_root = os.path.realpath(os.path.abspath(mod_export_path))
        extracted_count = 0
        with zipfile.ZipFile(zip_path, 'r') as zfile:
            file_names = [
                str(name).replace('\\', '/')
                for name in zfile.namelist()
                if not str(name).endswith('/')
            ]
            normalized_entries = {self._normalize_reference_path(name) for name in file_names}
            missing = sorted(ref for ref in referenced if ref not in normalized_entries)
            if missing:
                raise ValueError(
                    f"UI面板资源压缩包缺少 INI 引用的文件: {', '.join(missing)} "
                    f"(压缩包: {os.path.basename(zip_path)})"
                )
            for info in zfile.infolist():
                if info.is_dir():
                    continue
                entry_name = str(info.filename).replace('\\', '/')
                if not entry_name or entry_name.endswith('/'):
                    continue
                if self._UI_CONFIG_ENTRY_PATTERN.match(entry_name.rsplit('/', 1)[-1]):
                    continue
                target_path = self._resolve_bounded_path(
                    target_root,
                    entry_name,
                    "UI面板资源压缩包",
                )
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                with zfile.open(info, 'r') as source, open(target_path, 'wb') as target:
                    shutil.copyfileobj(source, target)
                extracted_count += 1
        return extracted_count

    def execute_postprocess(self, mod_export_path):
        print(f"UI面板注入后处理节点开始执行，Mod导出路径: {mod_export_path}")

        ini_files = glob.glob(os.path.join(mod_export_path, "*.ini"))
        if not ini_files:
            print("路径中未找到任何.ini文件")
            return
        target_ini_file = ini_files[0]

        zip_path = self._find_ui_assets_zip_path()
        panel_text, config_source = self._load_panel_config_text(zip_path)
        panel_sections = self.parse_sections(panel_text)
        if not panel_sections:
            print(f"面板 INI 为空或无法解析: {config_source}")
            return

        # 有压缩包时资源直接解压到模组目录；否则退回松散文件复制
        if zip_path:
            extracted_count = self._extract_ui_assets_zip(zip_path, mod_export_path, panel_sections)
            print(f"UI面板资源压缩包已解压: {os.path.basename(zip_path)} "
                  f"({extracted_count} 个文件 -> {mod_export_path})")
        else:
            self._copy_referenced_files(panel_sections, self._get_panel_folder(), mod_export_path)

        with open(target_ini_file, 'r', encoding='utf-8-sig') as f:
            target_text = f.read()
        kept_text, _removed = self._split_own_block(target_text)
        base_text, preserved_tail = self._split_appended_tail(kept_text)
        target_preamble = self._extract_preamble(base_text)
        target_sections = self.parse_sections(base_text)

        append_order, warnings = self._merge_panel_sections(
            target_sections, panel_sections,
            merge_constants=bool(getattr(self, "merge_constants", True)),
        )
        for warning in warnings:
            print(f"  [警告] {warning}")

        # 组装回写内容：保留原样文本 + 追加本节点块
        # 已合并进 target_sections 的面板段只写一次（统一放在本节点标记块内）
        appended = set(append_order)
        rebuilt_lines = list(target_preamble)
        if rebuilt_lines and rebuilt_lines[-1].strip():
            rebuilt_lines.append("")
        for section_name, section_lines in target_sections.items():
            if section_name in appended:
                continue
            rebuilt_lines.append(f"[{section_name}]")
            rebuilt_lines.extend(section_lines)
            rebuilt_lines.append("")

        new_block = self._build_appended_block(panel_sections, append_order)
        final_lines = rebuilt_lines
        if new_block:
            final_lines.append("")
            final_lines.extend(new_block)

        final_text = "\n".join(final_lines)
        if preserved_tail:
            if final_text and not final_text.endswith("\n"):
                final_text += "\n"
            final_text += preserved_tail

        with open(target_ini_file, 'w', encoding='utf-8') as f:
            f.write(final_text)

        print(f"UI面板配置已追加到: {os.path.basename(target_ini_file)} "
              f"(来源: {config_source}, 段数: {len(panel_sections)})")

    def _build_appended_block(self, panel_sections, append_order) -> list:
        marker = self.get_panel_marker()
        lines = [
            "; ==============================================================================",
            marker,
            "; ==============================================================================",
            "",
        ]
        for section_name in append_order:
            lines.append(f"[{section_name}]")
            lines.extend(panel_sections[section_name])
            lines.append("")
        return lines

    def _copy_referenced_files(self, panel_sections, panel_root: str, mod_export_path: str):
        referenced = self._extract_referenced_files(panel_sections)
        copied_count = 0
        for ref_path in referenced:
            source = self._resolve_bounded_path(panel_root, ref_path, "UI面板资源引用")
            if not os.path.isfile(source):
                raise ValueError(
                    f"UI面板引用的资源文件不存在: {ref_path} (查找位置: {source})。"
                    "请先解压网页导出的 ui_assets_*.zip 到面板目录。"
                )
            target = self._resolve_bounded_path(mod_export_path, ref_path, "UI面板资源目标")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            try:
                if not (os.path.exists(target) and os.path.samefile(source, target)):
                    import shutil
                    shutil.copy2(source, target)
                copied_count += 1
            except OSError as exc:
                raise ValueError(f"复制 UI面板资源失败 {ref_path}: {exc}") from exc
        if copied_count:
            print(f"UI面板资源复制完成: {copied_count} 个文件 -> {mod_export_path}")


classes = (
    SSMTNode_PostProcess_UIPanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
